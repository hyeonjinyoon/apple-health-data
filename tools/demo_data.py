#!/usr/bin/env python3
"""Send realistic synthetic Apple Health data to the ingest API (acts like the iPhone app).

    python3 tools/demo_data.py --url http://127.0.0.1:8788 --token-file ~/.local/apple-health/secrets/api_token

Deterministic (seeded, stable UUIDs), so re-running is idempotent. Point it at an ingest
instance whose HEALTH_CH_DATABASE is `health_demo` to keep demo data out of your real data.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import urllib.error
import urllib.request
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

NS = uuid.UUID("8d6f1f2e-3f6b-4c55-9a51-0d1f3b7e5a10")
TZ = timezone(timedelta(hours=9))  # demo user lives in KST
WATCH = {"source": "Apple Watch", "source_bundle": "com.apple.health.demo-watch", "device": "Watch7,1"}
PHONE = {"source": "iPhone", "source_bundle": "com.apple.health.demo-phone", "device": "iPhone16,1"}
OTHER_SLEEP_APP = {"source": "SleepTracker", "source_bundle": "com.example.sleeptracker", "device": ""}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def uid(*parts) -> str:
    return str(uuid.uuid5(NS, "|".join(map(str, parts))))


class Gen:
    def __init__(self, days: int, seed: int) -> None:
        self.rng = random.Random(seed)
        now = datetime.now(TZ)
        self.end = now.replace(minute=0, second=0, microsecond=0)
        self.start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        self.samples: list[dict] = []
        self.stats: list[dict] = []
        self.workouts: list[dict] = []

    def sample(self, type_, value, unit, start, end=None, src=WATCH, category="", meta=None):
        end = end or start
        self.samples.append({
            "uuid": uid(type_, start.isoformat(), src["source"], category), "type": type_, "value": value,
            "unit": unit, "start": iso(start), "end": iso(end), "category": category, **src,
            "metadata": meta or {},
        })

    def stat(self, type_, start, value, unit):
        self.stats.append({"type": type_, "start": iso(start), "end": iso(start + timedelta(hours=1)),
                           "value": round(value, 3), "unit": unit})

    def run(self) -> None:
        r = self.rng
        day = self.start
        weight = 73.5
        while day < self.end:
            n = (day - self.start).days
            weekend = day.weekday() >= 5
            workout_hour = None
            if day.weekday() in (0, 2, 4) or r.random() < 0.25:  # Mon/Wed/Fri routine + extras
                workout_hour = r.choice([7, 7, 18, 19, 20])
                self.workout(day.replace(hour=workout_hour, minute=r.randint(0, 40)))

            # --- hourly cumulative stats
            for h in range(24):
                t = day.replace(hour=h)
                if t >= self.end:
                    break
                awake = 7 <= h <= 23
                base = (0 if not awake else 250 + 450 * math.sin(math.pi * (h - 7) / 16) ** 2)
                if h in (8, 12, 18) and not weekend:
                    base += 900  # commute / lunch walk
                if h == workout_hour:
                    base += 3500
                steps = max(0, r.gauss(base, base * 0.25))
                self.stat("step_count", t, round(steps), "count")
                self.stat("distance_walking_running", t, steps * 0.74, "m")
                self.stat("active_energy", t, steps * 0.045 + (220 if h == workout_hour else 0) + r.uniform(0, 8), "kcal")
                self.stat("basal_energy", t, r.uniform(66, 72), "kcal")
                self.stat("flights_climbed", t, r.choice([0, 0, 0, 1, 2]) if awake else 0, "count")
                self.stat("exercise_time", t, (r.randint(25, 50) if h == workout_hour else r.choice([0, 0, 1, 3])) if awake else 0, "min")
                self.stat("stand_time", t, r.randint(2, 9) if awake else 0, "min")

                # heart rate every 10 minutes
                for m in range(0, 60, 10):
                    ht = t + timedelta(minutes=m)
                    if ht >= self.end:
                        break
                    hr = r.gauss(57, 3) if not awake else r.gauss(74, 6)
                    if h == workout_hour:
                        hr = r.gauss(148, 12)
                    self.sample("heart_rate", round(hr), "count/min", ht,
                                meta={"HKMetadataKeyHeartRateMotionContext": "2" if h == workout_hour else "1"})

            # --- daily discrete metrics
            morning = day.replace(hour=8, minute=30)
            if morning < self.end:
                fitness = n / 60
                self.sample("resting_heart_rate", round(r.gauss(58 - 2 * fitness, 1.5)), "count/min", morning)
                self.sample("walking_heart_rate_average", round(r.gauss(98 - 3 * fitness, 2.5)), "count/min", morning)
                for k in range(3):
                    self.sample("heart_rate_variability", round(r.gauss(48 + 6 * fitness, 9), 1), "ms",
                                day.replace(hour=2 + 2 * k, minute=r.randint(0, 59)))
                for k in range(4):
                    self.sample("oxygen_saturation", round(min(1.0, r.gauss(0.968, 0.012)), 3), "%",
                                day.replace(hour=1 + k, minute=r.randint(0, 59)))
                self.sample("respiratory_rate", round(r.gauss(14.5, 0.8), 1), "count/min", day.replace(hour=4))
                if n % 7 == 3:
                    self.sample("vo2_max", round(41 + 2.5 * fitness + r.gauss(0, 0.4), 1), "ml/(kg*min)",
                                day.replace(hour=19))
                if n % 3 == 0:
                    weight += r.gauss(-0.06, 0.25)
                    self.sample("body_mass", round(weight, 1), "kg", day.replace(hour=7, minute=10), src=PHONE)
                    self.sample("body_mass_index", round(weight / 1.75 ** 2, 1), "count", day.replace(hour=7, minute=10), src=PHONE)
                if n % 7 == 0:
                    self.sample("body_fat_percentage", round(r.gauss(0.205 - 0.01 * fitness, 0.004), 3), "%",
                                day.replace(hour=7, minute=10), src=PHONE)

            self.sleep(day)
            day += timedelta(days=1)

    def sleep(self, day: datetime) -> None:
        """A night ending on `day` morning: Watch stages + iPhone in-bed, sometimes a 3rd-party app too."""
        r = self.rng
        bed = day - timedelta(days=1) + timedelta(hours=23, minutes=r.randint(-40, 70))
        wake_target = day.replace(hour=7, minute=r.randint(0, 50))
        if wake_target > self.end:
            return
        self.sample("sleep_analysis", 0, "", bed - timedelta(minutes=10), wake_target, src=PHONE, category="in_bed")
        t = bed
        cycle = 0
        while t < wake_target:
            plan = [("asleep_core", r.randint(20, 35)), ("asleep_deep", r.randint(10, 35) if cycle < 3 else r.randint(0, 8)),
                    ("asleep_core", r.randint(10, 25)), ("asleep_rem", r.randint(10, 20) + 5 * cycle)]
            if r.random() < 0.35:
                plan.append(("awake", r.randint(1, 6)))
            for cat, minutes in plan:
                if minutes <= 0:
                    continue
                end = min(t + timedelta(minutes=minutes), wake_target)
                value = {"in_bed": 0, "asleep_unspecified": 1, "awake": 2, "asleep_core": 3, "asleep_deep": 4, "asleep_rem": 5}[cat]
                self.sample("sleep_analysis", value, "", t, end, category=cat)
                t = end
                if t >= wake_target:
                    break
            cycle += 1
        if r.random() < 0.2:  # a second app recorded the same night — dashboards must not double count
            self.sample("sleep_analysis", 1, "", bed, wake_target - timedelta(minutes=30),
                        src=OTHER_SLEEP_APP, category="asleep_unspecified")

    def workout(self, start: datetime) -> None:
        r = self.rng
        kind = r.choices(["running", "walking", "traditional_strength_training", "cycling", "yoga"],
                         weights=[4, 3, 3, 2, 1])[0]
        minutes = {"running": r.randint(25, 50), "walking": r.randint(30, 70), "traditional_strength_training": r.randint(35, 60),
                   "cycling": r.randint(40, 90), "yoga": r.randint(20, 45)}[kind]
        if start + timedelta(minutes=minutes) > self.end:
            return
        dist = {"running": minutes * 170, "walking": minutes * 85, "cycling": minutes * 380}.get(kind)
        avg_hr = {"running": 152, "walking": 104, "traditional_strength_training": 118, "cycling": 135, "yoga": 92}[kind]
        self.workouts.append({
            "uuid": uid("workout", start.isoformat()), "activity_type": kind, "start": iso(start),
            "end": iso(start + timedelta(minutes=minutes)), "duration_s": minutes * 60,
            "energy_kcal": round(minutes * r.uniform(6, 11), 1), "distance_m": dist,
            "avg_heart_rate": avg_hr + r.randint(-6, 6), "max_heart_rate": avg_hr + r.randint(15, 30), **WATCH,
        })


def post(url: str, token: str, payload: dict) -> dict:
    comp = zlib.compressobj(wbits=-zlib.MAX_WBITS)  # same raw DEFLATE the iOS app sends
    body = comp.compress(json.dumps(payload).encode()) + comp.flush()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/v1/ingest", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Content-Encoding": "deflate"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()[:500]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--token")
    ap.add_argument("--token-file", type=Path, default=Path("~/.local/apple-health/secrets/api_token"))
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--batch", type=int, default=4000)
    args = ap.parse_args()
    token = args.token or args.token_file.expanduser().read_text().strip()

    g = Gen(args.days, args.seed)
    g.run()
    meta = {"device_id": "demo-device", "device_name": "Demo iPhone", "app_version": "demo"}
    totals = {"samples": 0, "workouts": 0, "stats": 0, "deleted": 0}
    for key in ("samples", "stats", "workouts"):
        items = getattr(g, key)
        for i in range(0, len(items), args.batch):
            res = post(args.url, token, {**meta, key: items[i : i + args.batch]})
            for k in totals:
                totals[k] += res[k]
    print(f"sent {args.days} days of demo data: {totals}")


if __name__ == "__main__":
    main()
