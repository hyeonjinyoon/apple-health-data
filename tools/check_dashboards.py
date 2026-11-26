#!/usr/bin/env python3
"""Run every SQL query of the generated dashboards against ClickHouse (macros expanded).

    python3 tools/check_dashboards.py --database health_demo

Uses the read-only Grafana user (health_reader), so it also checks permissions.
Fails (exit 1) if any query errors; warns on queries that return no rows.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SECRETS = Path.home() / ".local/apple-health/secrets"


def expand(sql: str, db: str, tz: str, t_from: int, t_to: int, variables: dict[str, str]) -> str:
    sql = sql.replace("${db}", db).replace("${tz}", tz)
    for k, v in variables.items():
        sql = sql.replace("${%s}" % k, v)
    sql = re.sub(r"\$__timeFilter\(([^)]+)\)", rf"\1 >= toDateTime({t_from}) AND \1 <= toDateTime({t_to})", sql)
    sql = re.sub(r"\$__timeInterval\(([^)]+)\)", r"toStartOfInterval(toDateTime(\1), INTERVAL 3600 second)", sql)
    return (sql.replace("$__fromTime", f"toDateTime({t_from})").replace("$__toTime", f"toDateTime({t_to})")
            .replace("$__interval_s", "3600"))


def query(url: str, user: str, password: str, sql: str) -> list:
    req = urllib.request.Request(url + "/", data=(sql + "\nFORMAT JSONCompact").encode(), method="POST",
                                 headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["data"]


UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "M": 30 * 86400, "y": 365 * 86400}


def panel_from(panel: dict, default_from: int, t_to: int) -> int:
    """Honor a panel's relative-time override such as "2y" / "90d"."""
    tf = panel.get("timeFrom") or ""
    if len(tf) >= 2 and tf[:-1].isdigit() and tf[-1] in UNIT_SECONDS:
        return t_to - int(tf[:-1]) * UNIT_SECONDS[tf[-1]]
    return default_from


def walk(panels):
    for p in panels:
        yield from walk(p.get("panels", []))
        yield p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", default="health_demo")
    ap.add_argument("--url", default="http://127.0.0.1:8123")
    ap.add_argument("--user", default="health_reader")
    ap.add_argument("--password-file", type=Path, default=SECRETS / "ch_reader_password")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--var", action="append", default=["metric=heart_rate", "stat_metric=step_count"])
    args = ap.parse_args()
    pw = args.password_file.read_text().strip()
    t_to = int(time.time())
    t_from = t_to - args.days * 86400
    variables = dict(v.split("=", 1) for v in args.var)

    failures = empties = total = 0
    for f in sorted((REPO / "grafana/dashboards").glob("*.json")):
        d = json.loads(f.read_text())
        tz = next(v["query"] for v in d["templating"]["list"] if v["name"] == "tz")
        items = [(f"var {v['name']}", v["query"], t_from) for v in d["templating"]["list"] if v["type"] == "query"]
        items += [(p["title"] + (f"  [{p['timeFrom']}]" if p.get("timeFrom") else ""), t["rawSql"], panel_from(p, t_from, t_to))
                  for p in walk(d["panels"]) for t in p.get("targets", [])]
        print(f"\n{f.name}")
        for title, sql, p_from in items:
            total += 1
            try:
                rows = query(args.url, args.user, pw, expand(sql, args.database, tz, p_from, t_to, variables))
            except urllib.error.HTTPError as e:
                failures += 1
                print(f"  FAIL  {title}\n        {e.read().decode().strip()[:400]}")
                continue
            empty = not rows or all(all(c is None for c in r) for r in rows)
            empties += empty
            print(f"  {'EMPTY' if empty else 'ok   '} {title}  ({len(rows)} rows)")
    print(f"\n{total} queries, {failures} failed, {empties} empty")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
