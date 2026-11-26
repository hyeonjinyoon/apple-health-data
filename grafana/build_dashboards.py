#!/usr/bin/env python3
"""Generate the Apple Health Grafana dashboards (classic JSON model) for the ClickHouse schema.

    python3 grafana/build_dashboards.py                       # -> grafana/dashboards/*.json
    python3 grafana/build_dashboards.py --out DIR --tz Asia/Seoul --datasource-uid apple-health-ch

Stdlib only. Colors are the validated categorical slots (CVD-safe on Grafana's dark surface),
always assigned in fixed order; single-series panels use slot 1.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# ----------------------------------------------------------------------------- constants

SLOT = ["#338ced", "#e76624", "#00a888", "#d28e05", "#e24d8b"]  # blue, orange, aqua, yellow, magenta
NEUTRAL = "#8e8e8e"

D = "${db}"  # database variable (hidden; `?var-db=health_demo` switches to demo data)
TZ = "'${tz}'"  # timezone constant used for day/week boundaries

ACTIVITY_KO = {
    "running": "달리기", "walking": "걷기", "hiking": "하이킹", "cycling": "사이클",
    "swimming": "수영", "yoga": "요가", "pilates": "필라테스", "dance": "댄스",
    "cardio_dance": "카디오 댄스", "functional_strength_training": "기능성 근력",
    "traditional_strength_training": "근력 운동", "core_training": "코어 운동",
    "high_intensity_interval_training": "HIIT", "mixed_cardio": "혼합 유산소",
    "elliptical": "일립티컬", "rowing": "로잉", "stair_climbing": "계단 오르기",
    "stairs": "계단", "step_training": "스텝", "jump_rope": "줄넘기", "flexibility": "유연성",
    "cooldown": "쿨다운", "mind_and_body": "마음 챙김 운동", "tai_chi": "태극권",
    "tennis": "테니스", "badminton": "배드민턴", "table_tennis": "탁구", "pickleball": "피클볼",
    "soccer": "축구", "basketball": "농구", "baseball": "야구", "volleyball": "배구",
    "golf": "골프", "climbing": "클라이밍", "boxing": "복싱", "kickboxing": "킥복싱",
    "martial_arts": "무술", "skating_sports": "스케이팅", "snowboarding": "스노보드",
    "downhill_skiing": "스키", "cross_country_skiing": "크로스컨트리 스키",
    "fitness_gaming": "피트니스 게임", "swim_bike_run": "철인 3종", "hand_cycling": "핸드 사이클",
    "wheelchair_walk_pace": "휠체어(걷기)", "wheelchair_run_pace": "휠체어(달리기)",
    "other": "기타",
}
SLEEP_STAGES = [  # (category, label, color) — stacked bottom-up in this order
    ("asleep_deep", "깊은 수면", SLOT[0]),
    ("asleep_core", "코어 수면", SLOT[1]),
    ("asleep_rem", "렘 수면", SLOT[2]),
    ("awake", "깨어남", SLOT[3]),
    ("asleep_unspecified", "수면(미분류)", SLOT[4]),
]


def sql_array(values) -> str:
    return "[" + ", ".join("'" + v.replace("'", "\\'") + "'" for v in values) + "]"


ACTIVITY_LABEL = (
    f"transform(activity_type, {sql_array(ACTIVITY_KO)}, {sql_array(ACTIVITY_KO.values())}, activity_type)"
)


def day_range(col: str) -> str:
    """Time-range filter widened to whole local days, for daily aggregates."""
    return f"{col} >= toStartOfDay($__fromTime, {TZ}) AND {col} < $__toTime"


def daily_stat_sql(types: dict[str, str], scale: float = 1.0) -> str:
    """Daily sum of hourly stats. `types` maps stat type -> series label."""
    keys = list(types)
    series = f"transform(type, {sql_array(keys)}, {sql_array(types.values())}, type)"
    value = "sum(value)" if scale == 1.0 else f"sum(value) / {scale}"
    return (
        f"SELECT toStartOfDay(bucket_start, {TZ}) AS time, {series} AS series, {value} AS value\n"
        f"FROM {D}.quantity_stats FINAL\n"
        f"WHERE type IN ({', '.join(repr(k) for k in keys)}) AND {day_range('bucket_start')}\n"
        "GROUP BY time, series ORDER BY time"
    )


def daily_sample_avg_sql(types: dict[str, str], scale: float = 1.0) -> str:
    keys = list(types)
    series = f"transform(type, {sql_array(keys)}, {sql_array(types.values())}, type)"
    value = "avg(value)" if scale == 1.0 else f"avg(value) * {scale}"
    return (
        f"SELECT toStartOfDay(start_time, {TZ}) AS time, {series} AS series, {value} AS value\n"
        f"FROM {D}.samples FINAL\n"
        f"WHERE type IN ({', '.join(repr(k) for k in keys)}) AND {day_range('start_time')}\n"
        "GROUP BY time, series ORDER BY time"
    )


def sleep_nights_cte(time_cond: str) -> str:
    """Sleep segments assigned to a night (local date of wake-up, day boundary at 18:00).

    Several apps/devices may record the same night; per night we keep only the source with
    the most asleep time so overlapping records are not double counted.
    """
    return f"""WITH seg AS (
    SELECT toDate(end_time + INTERVAL 6 HOUR, {TZ}) AS night, source_name, category,
           dateDiff('second', start_time, end_time) AS secs, start_time, end_time
    FROM {D}.samples FINAL
    WHERE type = 'sleep_analysis' AND category != 'in_bed' AND {time_cond}
),
best AS (
    SELECT night, argMax(source_name, asleep) AS src
    FROM (SELECT night, source_name, sumIf(secs, category LIKE 'asleep%') AS asleep
          FROM seg GROUP BY night, source_name)
    GROUP BY night
),
nights AS (
    SELECT seg.night AS night, seg.source_name AS source_name, seg.category AS category,
           seg.secs AS secs, seg.start_time AS start_time, seg.end_time AS end_time
    FROM seg INNER JOIN best ON seg.night = best.night AND seg.source_name = best.src
)
"""


SLEEP_IN_RANGE = sleep_nights_cte(
    f"end_time >= toStartOfDay($__fromTime, {TZ}) - INTERVAL 6 HOUR AND end_time < $__toTime"
)
SLEEP_RECENT = sleep_nights_cte("end_time > now() - INTERVAL 3 DAY")

# ----------------------------------------------------------------------------- panel builders


class Board:
    def __init__(self, ds_uid: str):
        self.ds = {"type": "grafana-clickhouse-datasource", "uid": ds_uid}
        self.panels: list[dict] = []
        self._id = 0
        self._x = self._y = self._h = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _newline(self) -> None:
        self._y += self._h
        self._x = self._h = 0

    def row(self, title: str) -> None:
        self._newline()
        self.panels.append(
            {
                "type": "row", "id": self._next_id(), "title": title, "collapsed": False,
                "gridPos": {"x": 0, "y": self._y, "w": 24, "h": 1}, "panels": [],
            }
        )
        self._y += 1

    def add(self, panel: dict, w: int, h: int) -> None:
        if self._x + w > 24:
            self._newline()
        panel["id"] = self._next_id()
        panel["gridPos"] = {"x": self._x, "y": self._y, "w": w, "h": h}
        panel.setdefault("datasource", self.ds)
        for t in panel.get("targets", []):
            t["datasource"] = self.ds
        self.panels.append(panel)
        self._x += w
        self._h = max(self._h, h)


def target(sql: str, kind: str = "table") -> dict:
    ts = kind == "ts"
    return {
        "refId": "A", "editorType": "sql", "format": 0 if ts else 1,
        "queryType": "timeseries" if ts else "table", "rawSql": sql.strip(), "meta": {"builderOptions": {}},
    }


def color(c: str) -> dict:
    return {"mode": "fixed", "fixedColor": c}


def by_name(name: str, *props: tuple[str, object]) -> dict:
    return {"matcher": {"id": "byName", "options": name}, "properties": [{"id": k, "value": v} for k, v in props]}


def stat(title: str, sql: str, unit: str, *, decimals: int | None = 0, description: str = "",
         fields: str = "") -> dict:
    return {
        "type": "stat", "title": title, "description": description,
        "targets": [target(sql)],
        "fieldConfig": {
            "defaults": {
                "unit": unit, "decimals": decimals, "noValue": "–", "color": color("text"),
                "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
            },
            "overrides": [],
        },
        "options": {
            "colorMode": "none", "graphMode": "none", "justifyMode": "center", "textMode": "value",
            "wideLayout": True, "showPercentChange": False,
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": fields, "values": False},
        },
    }


def timeseries(title: str, sql: str, unit: str, *, bars: bool = False, stack: bool = False,
               series_colors: dict[str, str] | None = None, label_field: str | None = "series",
               decimals: int | None = None, points: str = "never", soft_min: float | None = 0,
               description: str = "", overrides: list[dict] | None = None,
               max_points: int | None = None, threshold: float | None = None, bar_align: int = 0) -> dict:
    multi = bool(series_colors) and len(series_colors) > 1
    custom = {
        "drawStyle": "bars" if bars else "line",
        "lineWidth": 1 if bars else 2,
        "fillOpacity": 85 if bars else 0,
        "barAlignment": bar_align,
        "barWidthFactor": 0.7,
        "showPoints": points,
        "pointSize": 7,
        "spanNulls": not bars,
        "lineInterpolation": "linear",
        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
        "axisPlacement": "auto",
        "axisSoftMin": soft_min,
        "gradientMode": "none",
        "thresholdsStyle": {"mode": "line" if threshold is not None else "off"},
    }
    defaults: dict = {"unit": unit, "decimals": decimals, "custom": custom, "color": color(SLOT[0])}
    if threshold is not None:
        defaults["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": "transparent", "value": None}, {"color": NEUTRAL, "value": threshold}],
        }
    if label_field:
        defaults["displayName"] = "${__field.labels.%s}" % label_field
    ovs = [by_name(n, ("color", color(c))) for n, c in (series_colors or {}).items()]
    panel = {
        "type": "timeseries", "title": title, "description": description,
        "targets": [target(sql, "ts")],
        "fieldConfig": {"defaults": defaults, "overrides": ovs + (overrides or [])},
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": multi},
            "tooltip": {"mode": "multi" if multi else "single", "sort": "none"},
        },
    }
    if max_points:
        panel["maxDataPoints"] = max_points
    return panel


def table(title: str, sql: str, *, overrides: list[dict] | None = None, description: str = "") -> dict:
    return {
        "type": "table", "title": title, "description": description,
        "targets": [target(sql)],
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                        "overrides": overrides or []},
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
    }


def bargauge(title: str, sql: str, unit: str, *, decimals: int | None = 1, description: str = "") -> dict:
    return {
        "type": "bargauge", "title": title, "description": description,
        "targets": [target(sql)],
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals, "min": 0, "color": color(SLOT[0]),
                                     "thresholds": {"mode": "absolute", "steps": [{"color": SLOT[0], "value": None}]}},
                        "overrides": []},
        "options": {
            "orientation": "horizontal", "displayMode": "basic", "valueMode": "text", "namePlacement": "left",
            "showUnfilled": True, "sizing": "auto", "minVizHeight": 16, "maxVizHeight": 28,
            "reduceOptions": {"values": True, "calcs": [], "fields": ""},
        },
    }


def barchart(title: str, sql: str, unit: str, x_field: str, *, description: str = "") -> dict:
    return {
        "type": "barchart", "title": title, "description": description,
        "targets": [target(sql)],
        "fieldConfig": {"defaults": {"unit": unit, "decimals": 0, "color": color(SLOT[0]),
                                     "custom": {"fillOpacity": 85, "lineWidth": 0, "gradientMode": "none",
                                                "axisSoftMin": 0}},
                        "overrides": []},
        "options": {
            "xField": x_field, "orientation": "vertical", "showValue": "never", "barWidth": 0.8,
            "groupWidth": 0.7, "stacking": "none", "xTickLabelSpacing": 0,
            "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "single", "sort": "none"},
        },
    }


def templating(ds: dict, tz: str, extra: list[dict] | None = None) -> dict:
    return {
        "list": [
            {
                "type": "custom", "name": "db", "label": "데이터베이스", "hide": 2,
                "query": "health,health_demo",
                "current": {"text": "health", "value": "health"},
                "options": [{"text": v, "value": v, "selected": v == "health"} for v in ("health", "health_demo")],
            },
            {"type": "constant", "name": "tz", "hide": 2, "query": tz, "current": {"text": tz, "value": tz}},
            *(extra or []),
        ]
    }


def query_var(ds: dict, name: str, label: str, sql: str) -> dict:
    return {
        "type": "query", "name": name, "label": label, "datasource": ds, "query": sql, "definition": sql,
        "refresh": 1, "sort": 1, "multi": False, "includeAll": False, "hide": 0, "current": {}, "options": [],
    }


def dashboard(uid: str, title: str, description: str, board: Board, tpl: dict, time_from: str) -> dict:
    return {
        "uid": uid, "title": title, "description": description, "tags": ["apple-health"],
        "timezone": "browser", "editable": True, "graphTooltip": 1, "schemaVersion": 39, "version": 1,
        "refresh": "", "time": {"from": time_from, "to": "now"},
        "links": [{"type": "dashboards", "tags": ["apple-health"], "asDropdown": False, "includeVars": True,
                   "keepTime": True, "title": "Apple Health", "icon": "external link"}],
        "templating": tpl, "annotations": {"list": []}, "panels": board.panels,
    }


# ----------------------------------------------------------------------------- overview


T0 = f"toDate(now(), {TZ})"  # today, local date

# Minutes after 18:00 of the first asleep segment (so 23:30 → 330, 01:30 → 450 — no midnight wrap).
BED_MIN = (f"(toHour(minIf(start_time, category LIKE 'asleep%'), {TZ}) * 60 + "
           f"toMinute(minIf(start_time, category LIKE 'asleep%'), {TZ}) + 360) % 1440")
WAKE_MIN = (f"toHour(maxIf(end_time, category LIKE 'asleep%'), {TZ}) * 60 + "
            f"toMinute(maxIf(end_time, category LIKE 'asleep%'), {TZ})")


def clock_sql(minutes: str, base: str = "18:00") -> str:
    """Minutes after `base` → 'HH:MM'."""
    return (f"formatDateTime(toDateTime('2000-01-01 {base}:00') + "
            f"toIntervalMinute(toUInt32(greatest(round({minutes}), 0))), '%H:%i')")


def per_night_cte(time_cond: str) -> str:
    """Sleep CTEs plus `per`: one row per night (≥ 2 h asleep) with hours and bed/wake minutes."""
    return sleep_nights_cte(time_cond).rstrip() + f""",
per AS (
    SELECT night, night AS d, sumIf(secs, category LIKE 'asleep%') / 3600 AS h, {BED_MIN} AS bed_m, {WAKE_MIN} AS wake_m
    FROM nights GROUP BY night HAVING h >= 2
)
"""


def month_time(col: str) -> str:
    return f"toDateTime(toStartOfMonth({col}, {TZ}), {TZ})"


MONTH_FILL = (f"WITH FILL FROM toDateTime(toStartOfMonth(toDate($__fromTime, {TZ})), {TZ}) "
              f"TO toDateTime(toStartOfMonth(toDate($__toTime, {TZ})), {TZ}) + INTERVAL 1 MONTH STEP INTERVAL 1 MONTH")


def monthly_sample_avg_sql(type_: str, label: str, scale: float = 1.0) -> str:
    value = "avg(value)" if scale == 1.0 else f"avg(value) * {scale}"
    return (f"SELECT {month_time('start_time')} AS time, {value} AS `{label}`\n"
            f"FROM {D}.samples FINAL WHERE type = '{type_}' AND $__timeFilter(start_time)\n"
            "GROUP BY time ORDER BY time")


def monthly_daily_avg_sql(type_: str, label: str) -> str:
    """Monthly average of daily totals, over days the phone recorded steps."""
    return (f"SELECT toDateTime(toStartOfMonth(d), {TZ}) AS time, avg(v) AS `{label}`\n"
            f"FROM (SELECT toDate(bucket_start, {TZ}) AS d, sumIf(value, type = 'step_count') AS steps,\n"
            f"             sumIf(value, type = '{type_}') AS v\n"
            f"      FROM {D}.quantity_stats FINAL\n"
            f"      WHERE type IN ('step_count', '{type_}') AND $__timeFilter(bucket_start)\n"
            f"      GROUP BY d HAVING steps > 0 AND d < {T0})\n"
            "GROUP BY time ORDER BY time")


# ----------------------------------------------------------------------------- scorecard

GOOD, BAD = "#0aac23", "#f18a59"  # status colors (good / serious) — always paired with a text label

# (name, kind, better, unit, source, expression, filter, aggregate, decimals)
#   kind: num = relative % change, pct = percentage points, clock = minutes after 18:00
#   better: up / down / "" (neutral: show the change without a verdict)
SCORECARD = [
    ("수면 시간", "num", "up", "시간", "per", "h", "1", "avg", 1),
    ("6시간 미만으로 잔 밤", "pct", "down", "", "per", "if(h < 6, 100, 0)", "1", "avg", 0),
    ("잠든 시각", "clock", "down", "", "per", "bed_m", "1", "avg", 0),
    ("취침 시각 들쭉날쭉 (표준편차)", "num", "down", "분", "per", "bed_m", "1", "sd", 0),
    ("안정시 심박", "num", "down", " bpm", "samp", "value", "type = 'resting_heart_rate'", "avg", 1),
    ("심박 변이도 (HRV)", "num", "up", " ms", "samp", "value", "type = 'heart_rate_variability'", "avg", 1),
    ("VO₂ max", "num", "up", "", "samp", "value", "type = 'vo2_max'", "avg", 1),
    ("걷기 평균 심박", "num", "down", " bpm", "samp", "value", "type = 'walking_heart_rate_average'", "avg", 1),
    ("운동 시간", "num", "up", "분/일", "daily", "ex", "1", "avg", 1),
    ("운동 기록 횟수", "num", "up", "회/30일", "wk", "1", "1", "count30", 1),
    ("활동 에너지", "num", "up", " kcal/일", "daily", "active", "1", "avg", 0),
    ("걸음 수", "num", "up", "보/일", "daily", "steps", "1", "avg", 0),
    ("스탠드 시간", "num", "up", "분/일", "daily", "stand", "1", "avg", 0),
    ("걷기 비대칭", "num", "down", "%", "samp", "value * 100", "type = 'walking_asymmetry'", "avg", 1),
    ("걷기 속도", "num", "up", " km/h", "samp", "value * 3.6", "type = 'walking_speed'", "avg", 2),
    ("체중", "num", "", " kg", "samp", "value", "type = 'body_mass'", "avg", 1),
    ("제지방량", "num", "up", " kg", "samp", "value", "type = 'lean_body_mass'", "avg", 1),
    ("체지방률", "num", "", "%", "samp", "value * 100", "type = 'body_fat_percentage'", "avg", 1),
]
PERIODS = {  # local dates; `d` is each source's local date (night = wake-up date)
    "cur": f"d >= {T0} - 30",
    "prev": f"d BETWEEN {T0} - 90 AND {T0} - 31",
    "ya": f"d BETWEEN {T0} - 395 AND {T0} - 366",
}


def _agg(agg: str, expr: str, cond: str, decimals: int) -> dict[str, str]:
    out = {}
    for p, pc in PERIODS.items():
        c = f"{cond} AND {pc}"
        if agg == "avg":
            v = f"avgIf({expr}, {c})"
        elif agg == "sd":
            v = f"if(countIf({c}) < 3, nan, stddevPopIf({expr}, {c}))"
        else:  # count30: events per 30 days (the "prev" window is 60 days long)
            v = f"countIf({c})" + (" / 2" if p == "prev" else "")
        out[p] = f"round(toFloat64({v}), {decimals})"
    return out


def _fmt(kind: str, unit: str, x: str) -> str:
    if kind == "clock":
        return f"if(isNaN({x}), '–', {clock_sql(x)})"
    suffix = "%" if kind == "pct" else unit
    return f"if(isNaN({x}), '–', concat(toString({x}), '{suffix}'))"


def _judge(kind: str, better: str, a: str, b: str) -> str:
    if kind == "num":
        delta, thr, suffix, invalid = f"(({a} - {b}) / abs({b}) * 100)", 3, "%", f"isNaN({a}) OR isNaN({b}) OR {b} = 0"
    else:
        delta, thr, suffix = f"({a} - {b})", (3 if kind == "pct" else 15), ("%p" if kind == "pct" else "분")
        invalid = f"isNaN({a}) OR isNaN({b})"
    change = f"concat(if({delta} >= 0, '+', ''), toString(round({delta})), '{suffix}')"
    if not better:
        return f"if({invalid}, '–', concat('변화 ', {change}))"
    up = 1 if better == "up" else 0
    state = f"if(abs({delta}) < {thr}, '유지', if(({delta} > 0) = {up}, '개선', '악화'))"
    return f"if({invalid}, '–', concat({state}, ' ', {change}))"


def scorecard_sql() -> str:
    sample_types = sorted({flt.split("'")[1] for *_, flt, _agg_, _dec in SCORECARD if "'" in flt})
    ctes = per_night_cte("end_time > now() - INTERVAL 400 DAY").rstrip() + f""",
daily AS (
    SELECT toDate(bucket_start, {TZ}) AS d, sumIf(value, type = 'step_count') AS steps,
           sumIf(value, type = 'active_energy') AS active, sumIf(value, type = 'exercise_time') AS ex,
           sumIf(value, type = 'stand_time') AS stand
    FROM {D}.quantity_stats FINAL
    WHERE bucket_start > now() - INTERVAL 400 DAY
    GROUP BY d HAVING steps > 0 AND d < {T0}
),
samp AS (
    SELECT type, toDate(start_time, {TZ}) AS d, value FROM {D}.samples FINAL
    WHERE start_time > now() - INTERVAL 400 DAY AND type IN ({', '.join(f"'{t}'" for t in sample_types)})
),
wk AS (SELECT toDate(start_time, {TZ}) AS d FROM {D}.workouts FINAL WHERE start_time > now() - INTERVAL 400 DAY),
m AS (
"""
    branches = []
    for i, (name, kind, better, unit, src, expr, cond, agg, dec) in enumerate(SCORECARD, 1):
        v = _agg(agg, expr, cond, dec)
        branches.append(
            f"    SELECT {i} AS ord, '{name}' AS name, {v['cur']} AS cur, {v['prev']} AS prev, {v['ya']} AS ya FROM {src}")
    body = "\n    UNION ALL\n".join(branches)

    # Formatting depends on each row's kind/unit, so every output column is one multiIf over `ord`.
    def per_row(fn) -> str:
        parts = [f"ord = {i}, {fn(kind, better, unit)}"
                 for i, (_n, kind, better, unit, *_rest) in enumerate(SCORECARD, 1)]
        return "multiIf(" + ", ".join(parts) + ", '–')"

    return (ctes + body + "\n)\n"
            "SELECT name AS `지표`,\n"
            f"  {per_row(lambda k, b, u: _fmt(k, u, 'cur'))} AS `최근 30일`,\n"
            f"  {per_row(lambda k, b, u: _fmt(k, u, 'prev'))} AS `직전 31–90일`,\n"
            f"  {per_row(lambda k, b, u: _fmt(k, u, 'ya'))} AS `1년 전 같은 30일`,\n"
            f"  {per_row(lambda k, b, u: _judge(k, b, 'cur', 'prev'))} AS `직전 대비`,\n"
            f"  {per_row(lambda k, b, u: _judge(k, b, 'cur', 'ya'))} AS `1년 전 대비`\n"
            "FROM m ORDER BY ord")


def verdict_overrides(*columns: str) -> list[dict]:
    mappings = [
        {"type": "regex", "options": {"pattern": "^개선.*", "result": {"color": GOOD, "index": 0}}},
        {"type": "regex", "options": {"pattern": "^악화.*", "result": {"color": BAD, "index": 1}}},
        {"type": "regex", "options": {"pattern": "^(유지|변화).*", "result": {"color": NEUTRAL, "index": 2}}},
    ]
    return [by_name(c, ("custom.cellOptions", {"type": "color-text"}), ("mappings", mappings),
                    ("custom.width", 150)) for c in columns]


# ----------------------------------------------------------------------------- overview


def overview(ds_uid: str, tz: str) -> dict:
    """Sections ordered by what matters most right now: sleep → heart/fitness → activity → gait → body."""
    b = Board(ds_uid)
    today = f"bucket_start >= toStartOfDay(now(), {TZ})"

    b.row("오늘")
    b.add(stat("지난밤 수면", SLEEP_RECENT + "SELECT sumIf(secs, category LIKE 'asleep%') / 3600 AS v "
               "FROM nights WHERE night = (SELECT max(night) FROM nights)",
               "suffix: 시간", decimals=1, description="가장 최근 밤의 총 수면 시간 (깨어있던 시간 제외). 권장 7–9시간"), 3, 4)
    b.add(stat("안정시 심박", f"SELECT argMax(value, start_time) AS v FROM {D}.samples FINAL "
               "WHERE type = 'resting_heart_rate' AND start_time > now() - INTERVAL 30 DAY",
               "suffix: bpm", description="가장 최근 안정시 심박수 (낮을수록 좋음)"), 3, 4)
    b.add(stat("HRV (7일 평균)", f"SELECT avg(value) AS v FROM {D}.samples FINAL "
               "WHERE type = 'heart_rate_variability' AND start_time > now() - INTERVAL 7 DAY",
               "ms", description="심박 변이도 SDNN, 최근 7일 평균 (높을수록 회복 상태 양호)"), 3, 4)
    b.add(stat("운동 시간", f"SELECT sum(value) AS v FROM {D}.quantity_stats FINAL WHERE type = 'exercise_time' AND {today}",
               "suffix: 분", description="오늘 운동 시간. WHO 권장 주 150분 ≈ 하루 21분"), 3, 4)
    b.add(stat("활동 에너지", f"SELECT sum(value) AS v FROM {D}.quantity_stats FINAL WHERE type = 'active_energy' AND {today}",
               "suffix: kcal"), 3, 4)
    b.add(stat("걸음 수", f"SELECT sum(value) AS v FROM {D}.quantity_stats FINAL WHERE type = 'step_count' AND {today}",
               "locale", description="오늘 0시 이후 걸음 수 (iPhone·Watch 중복 제거)"), 3, 4)
    b.add(stat("체중", f"SELECT argMax(value, start_time) AS v FROM {D}.samples FINAL WHERE type = 'body_mass'",
               "masskg", decimals=1, description="가장 최근 기록"), 3, 4)
    b.add(stat("마지막 수신", f"SELECT toUnixTimestamp64Milli(max(received_at)) AS v FROM {D}.sync_log",
               "dateTimeFromNow", decimals=None, description="iPhone 앱에서 마지막으로 데이터를 받은 시각"), 3, 4)

    b.row("① 수면")
    b.add(timeseries(
        "총 수면 시간",
        SLEEP_IN_RANGE + "SELECT time, asleep AS `수면 시간`,\n"
        "       avg(asleep) OVER (ORDER BY time ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS `7일 평균`\n"
        f"FROM (SELECT toDateTime(night, {TZ}) AS time, sumIf(secs, category LIKE 'asleep%') / 3600 AS asleep\n"
        "      FROM nights GROUP BY time)\nORDER BY time",
        "suffix: 시간", label_field=None, decimals=1, soft_min=None, points="always", threshold=7,
        series_colors={"수면 시간": SLOT[0], "7일 평균": SLOT[1]},
        overrides=[by_name("수면 시간", ("custom.lineWidth", 1))],
        description="회색 선: 권장 최소 7시간"), 12, 9)
    b.add(timeseries(
        "수면 단계",
        SLEEP_IN_RANGE + f"SELECT toDateTime(night, {TZ}) AS time,\n"
        + ",\n".join(f"  sumIf(secs, category = '{c}') / 3600 AS `{label}`" for c, label, _ in SLEEP_STAGES)
        + "\nFROM nights GROUP BY time ORDER BY time",
        "suffix: 시간", bars=True, stack=True, decimals=1, label_field=None,
        series_colors={label: c for _, label, c in SLEEP_STAGES},
        description="기상일 기준. 같은 밤을 여러 앱/기기가 기록했다면 수면 시간이 가장 긴 소스 하나만 사용"), 12, 9)
    b.add(table(
        "수면 기록",
        SLEEP_IN_RANGE + "SELECT toString(night) AS `날짜`,\n"
        f"  formatDateTime(minIf(start_time, category LIKE 'asleep%'), '%H:%i', {TZ}) AS `잠든 시각`,\n"
        f"  formatDateTime(maxIf(end_time, category LIKE 'asleep%'), '%H:%i', {TZ}) AS `깬 시각`,\n"
        "  round(sumIf(secs, category LIKE 'asleep%') / 3600, 1) AS `총 수면(시간)`,\n"
        "  round(sumIf(secs, category = 'asleep_deep') / 60) AS `깊은(분)`,\n"
        "  round(sumIf(secs, category = 'asleep_core') / 60) AS `코어(분)`,\n"
        "  round(sumIf(secs, category = 'asleep_rem') / 60) AS `렘(분)`,\n"
        "  round(sumIf(secs, category = 'awake') / 60) AS `깸(분)`,\n"
        "  any(source_name) AS `기록 소스`\n"
        "FROM nights GROUP BY night ORDER BY night DESC"), 24, 8)

    b.row("② 심장 · 체력")
    b.add(timeseries("안정시 심박 · 걷기 평균 심박",
                     daily_sample_avg_sql({"resting_heart_rate": "안정시 심박", "walking_heart_rate_average": "걷기 평균 심박"}),
                     "suffix: bpm", decimals=0, soft_min=None, points="always",
                     series_colors={"안정시 심박": SLOT[0], "걷기 평균 심박": SLOT[1]},
                     description="둘 다 낮아질수록 심폐 체력이 좋아지는 신호"), 12, 8)
    b.add(timeseries(
        "심박 변이도 (HRV)",
        "SELECT time, v AS `일 평균`, avg(v) OVER (ORDER BY time ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS `7일 평균`\n"
        f"FROM (SELECT toStartOfDay(start_time, {TZ}) AS time, avg(value) AS v FROM {D}.samples FINAL\n"
        f"      WHERE type = 'heart_rate_variability' AND {day_range('start_time')} GROUP BY time)\n"
        "ORDER BY time",
        "ms", label_field=None, decimals=0, soft_min=None, points="always",
        series_colors={"일 평균": SLOT[0], "7일 평균": SLOT[1]},
        overrides=[by_name("일 평균", ("custom.lineWidth", 1))],
        description="SDNN 기준. 높을수록 회복 상태가 좋은 편"), 12, 8)
    b.add(timeseries("VO₂ max", daily_sample_avg_sql({"vo2_max": "VO₂ max"}), "suffix: ml/kg·min",
                     decimals=1, soft_min=None, points="always", description="심폐 체력 추정치 (높을수록 좋음)"), 8, 7)
    b.add(timeseries("혈중 산소 (일 평균)", daily_sample_avg_sql({"oxygen_saturation": "혈중 산소"}), "percentunit",
                     decimals=1, soft_min=None, points="always"), 8, 7)
    b.add(timeseries("호흡수 (일 평균)", daily_sample_avg_sql({"respiratory_rate": "호흡수"}), "suffix: 회/분",
                     decimals=1, soft_min=None, points="always"), 8, 7)
    b.add(timeseries(
        "심박수",
        "SELECT $__timeInterval(start_time) AS time, avg(value) AS `평균`, min(value) AS `최저`, max(value) AS `최고`\n"
        f"FROM {D}.samples FINAL WHERE type = 'heart_rate' AND $__timeFilter(start_time)\n"
        "GROUP BY time ORDER BY time",
        "suffix: bpm", label_field=None, soft_min=None, decimals=0, max_points=400,
        series_colors={"평균": SLOT[0], "최저": SLOT[0], "최고": SLOT[0]},
        overrides=[
            by_name("최고", ("custom.fillBelowTo", "최저"), ("custom.fillOpacity", 18), ("custom.lineWidth", 0)),
            by_name("최저", ("custom.lineWidth", 0)),
        ],
        description="구간별 평균(선)과 최저~최고 범위(음영)"), 24, 8)

    b.row("③ 활동 · 운동")
    b.add(timeseries("일별 운동 시간", daily_stat_sql({"exercise_time": "운동 시간"}), "suffix: 분", bars=True,
                     decimals=0, threshold=21.4, description="회색 선: WHO 권장 주 150분 ≈ 하루 21분"), 12, 8)
    b.add(timeseries(
        "주별 운동 기록 시간",
        f"SELECT toDateTime(toStartOfWeek(start_time, 1, {TZ}), {TZ}) AS time, sum(duration_s) / 60 AS `운동 시간`\n"
        f"FROM {D}.workouts FINAL WHERE {day_range('start_time')} GROUP BY time ORDER BY time",
        "suffix: 분", bars=True, label_field=None, decimals=0, bar_align=1,
        description="Watch로 기록한 운동(근력 운동 등)의 주간 합계, 월요일 시작"), 12, 8)
    b.add(timeseries("일별 에너지 소모", daily_stat_sql({"active_energy": "활동 에너지", "basal_energy": "휴식 에너지"}),
                     "suffix: kcal", bars=True, stack=True, decimals=0,
                     series_colors={"활동 에너지": SLOT[0], "휴식 에너지": SLOT[1]},
                     description="막대 전체 높이 = 총 소모 에너지"), 12, 8)
    b.add(timeseries("일별 걸음 수", daily_stat_sql({"step_count": "걸음 수"}), "locale", bars=True,
                     decimals=0, threshold=10000, description="회색 선: 10,000보"), 12, 8)
    b.add(timeseries("일별 스탠드 시간", daily_stat_sql({"stand_time": "스탠드 시간"}), "suffix: 분", bars=True, decimals=0), 8, 7)
    b.add(timeseries("일별 오른 층수", daily_stat_sql({"flights_climbed": "오른 층수"}), "suffix: 층", bars=True, decimals=0), 8, 7)
    b.add(timeseries("일별 걷기·달리기 거리", daily_stat_sql({"distance_walking_running": "거리"}), "lengthm",
                     bars=True, decimals=1), 8, 7)
    b.add(bargauge(
        "운동 종류별 총 시간",
        f"SELECT {ACTIVITY_LABEL} AS `운동`, sum(duration_s) / 3600 AS `시간`\n"
        f"FROM {D}.workouts FINAL WHERE $__timeFilter(start_time) GROUP BY `운동` ORDER BY `시간` DESC LIMIT 10",
        "suffix: 시간"), 12, 7)
    b.add(barchart(
        "시간대별 평균 걸음 수",
        f"SELECT lpad(toString(toHour(bucket_start, {TZ})), 2, '0') AS hour, "
        "sum(value) / greatest(dateDiff('day', $__fromTime, $__toTime), 1) AS steps\n"
        f"FROM {D}.quantity_stats FINAL WHERE type = 'step_count' AND $__timeFilter(bucket_start)\n"
        "GROUP BY hour ORDER BY hour",
        "locale", "hour", description="선택한 기간의 하루 평균, 시(hour)별"), 12, 7)
    b.add(table(
        "운동 기록",
        f"SELECT start_time AS `시작`, {ACTIVITY_LABEL} AS `운동`, round(duration_s / 60) AS `시간(분)`,\n"
        "  round(energy_kcal) AS `kcal`, round(distance_m / 1000, 2) AS `거리(km)`,\n"
        "  round(avg_heart_rate) AS `평균 심박`, round(max_heart_rate) AS `최고 심박`, source_name AS `기록 기기`\n"
        f"FROM {D}.workouts FINAL WHERE $__timeFilter(start_time) ORDER BY start_time DESC LIMIT 200"), 24, 8)

    b.row("④ 보행 (iPhone 측정)")
    b.add(timeseries("걷기 비대칭 (일 평균)", daily_sample_avg_sql({"walking_asymmetry": "걷기 비대칭"}), "percentunit",
                     decimals=1, soft_min=0, points="always",
                     description="좌우 걸음 시간 차이. iPhone을 바지 주머니에 넣고 걸을 때 측정되며, 휴대 방식에 따라 달라질 수 있음"), 8, 7)
    b.add(timeseries("걷기 속도 (일 평균)", daily_sample_avg_sql({"walking_speed": "걷기 속도"}, scale=3.6), "velocitykmh",
                     decimals=2, soft_min=None, points="always"), 8, 7)
    b.add(timeseries("보폭 (일 평균)", daily_sample_avg_sql({"walking_step_length": "보폭"}, scale=100), "suffix: cm",
                     decimals=1, soft_min=None, points="always"), 8, 7)

    b.row("⑤ 체성분")
    for t, name, unit, scale in (("body_mass", "체중", "masskg", 1), ("lean_body_mass", "제지방량", "masskg", 1),
                                 ("body_fat_percentage", "체지방률", "percentunit", 1), ("body_mass_index", "BMI", "none", 1)):
        b.add(timeseries(
            name,
            f"SELECT start_time AS time, value AS `{name}` FROM {D}.samples FINAL\n"
            f"WHERE type = '{t}' AND $__timeFilter(start_time) ORDER BY time",
            unit, label_field=None, decimals=1, soft_min=None, points="always"), 6, 7)

    b.row("동기화 상태")
    b.add(stat("동기화 기기", f"SELECT argMax(device_name, received_at) AS v FROM {D}.sync_log",
               "none", decimals=None, fields="/.*/"), 8, 4)
    b.add(stat("저장된 샘플", f"SELECT count() AS v FROM {D}.samples FINAL", "locale"), 8, 4)
    b.add(stat("저장된 운동", f"SELECT count() AS v FROM {D}.workouts FINAL", "locale"), 8, 4)
    b.add(timeseries(
        "수신 이력",
        "SELECT $__timeInterval(received_at) AS time, sum(samples) AS `샘플`, sum(stats) AS `시간별 통계`, "
        "sum(workouts) AS `운동`\n"
        f"FROM {D}.sync_log WHERE $__timeFilter(received_at) GROUP BY time ORDER BY time",
        "locale", bars=True, stack=True, label_field=None, decimals=0,
        series_colors={"샘플": SLOT[0], "시간별 통계": SLOT[1], "운동": SLOT[2]}), 12, 8)
    b.add(table(
        "저장된 데이터",
        f"SELECT type AS `타입`, '샘플' AS `구분`, count() AS `건수`, min(start_time) AS `최초`, max(end_time) AS `최신`\n"
        f"FROM {D}.samples FINAL GROUP BY type\n"
        f"UNION ALL SELECT type, '시간별 통계', count(), min(bucket_start), max(bucket_end) FROM {D}.quantity_stats FINAL GROUP BY type\n"
        f"UNION ALL SELECT 'workout', '운동', count(), min(start_time), max(end_time) FROM {D}.workouts FINAL HAVING count() > 0\n"
        "ORDER BY 1, 2"), 12, 8)

    ds = b.ds
    return dashboard("apple-health-overview", "Apple Health", "iPhone 건강 데이터 종합 대시보드 (중요도순)", b,
                     templating(ds, tz), "now-30d")


# ----------------------------------------------------------------------------- trends


def trends(ds_uid: str, tz: str) -> dict:
    """Long-range check: scorecard of key metrics, then monthly trends in priority order."""
    b = Board(ds_uid)

    b.row("핵심 지표 점검표")
    b.add(table(
        "최근 30일 vs 직전 31–90일 vs 1년 전 같은 30일",
        scorecard_sql(),
        overrides=verdict_overrides("직전 대비", "1년 전 대비") + [by_name("지표", ("custom.width", 230))],
        description="대시보드 시간 범위와 무관하게 항상 오늘 기준으로 계산합니다. "
                    "개선/악화는 각 지표의 바람직한 방향 기준이며 ±3%(시각은 ±15분) 이내는 '유지'. "
                    "'변화'는 좋고 나쁨을 판단하지 않는 지표. 의학적 판단이 아닌 추세 참고용입니다."), 24, 19)

    b.row("① 수면 — 월별")
    nights = per_night_cte(f"end_time >= toStartOfDay($__fromTime, {TZ}) - INTERVAL 6 HOUR AND end_time < $__toTime")
    b.add(timeseries(
        "월평균 수면 시간",
        nights + f"SELECT toDateTime(toStartOfMonth(night), {TZ}) AS time, avg(h) AS `평균 수면 시간`\n"
        "FROM per GROUP BY time ORDER BY time",
        "suffix: 시간", label_field=None, decimals=2, soft_min=None, points="always", threshold=7,
        description="회색 선: 권장 최소 7시간. 2시간 이상 잔 밤만 포함"), 8, 8)
    b.add(timeseries(
        "6시간 미만으로 잔 밤의 비율",
        nights + f"SELECT toDateTime(toStartOfMonth(night), {TZ}) AS time, countIf(h < 6) / count() * 100 AS `6시간 미만`\n"
        "FROM per GROUP BY time ORDER BY time",
        "percent", bars=True, label_field=None, decimals=0, bar_align=1), 8, 8)
    b.add(timeseries(
        "평균 잠든 시각 (자정 기준 ± 시간)",
        nights + f"SELECT toDateTime(toStartOfMonth(night), {TZ}) AS time, (avg(bed_m) - 360) / 60 AS `잠든 시각`\n"
        "FROM per GROUP BY time ORDER BY time",
        "suffix: 시", label_field=None, decimals=1, soft_min=None, points="always", threshold=1,
        description="0 = 자정, 1.5 = 01:30, -0.5 = 23:30. 회색 선: 01:00 (현재 기상 시각 기준 7시간 수면을 위한 취침 시각)"), 8, 8)
    b.add(table(
        "최근 12주 수면",
        per_night_cte("end_time > now() - INTERVAL 90 DAY")
        + "SELECT toString(toStartOfWeek(night, 1)) AS `주 (월요일 시작)`, count() AS `기록된 밤`,\n"
        "  round(avg(h), 1) AS `평균 수면(시간)`, countIf(h < 6) AS `6시간 미만(일)`,\n"
        f"  {clock_sql('avg(bed_m)')} AS `평균 잠든 시각`, round(stddevPop(bed_m)) AS `취침 편차(±분)`,\n"
        f"  {clock_sql('avg(wake_m)', base='00:00')} AS `평균 기상 시각`\n"
        "FROM per GROUP BY toStartOfWeek(night, 1) ORDER BY 1 DESC LIMIT 12",
        overrides=[by_name("평균 수면(시간)", ("decimals", 1))]), 24, 14)

    b.row("② 심장 · 체력 — 월별")
    b.add(timeseries("안정시 심박", monthly_sample_avg_sql("resting_heart_rate", "안정시 심박"), "suffix: bpm",
                     label_field=None, decimals=1, soft_min=None, points="always", description="낮을수록 좋음"), 6, 8)
    b.add(timeseries("심박 변이도 (HRV)", monthly_sample_avg_sql("heart_rate_variability", "HRV"), "ms",
                     label_field=None, decimals=1, soft_min=None, points="always", description="높을수록 좋음"), 6, 8)
    b.add(timeseries("VO₂ max", monthly_sample_avg_sql("vo2_max", "VO₂ max"), "suffix: ml/kg·min",
                     label_field=None, decimals=1, soft_min=None, points="always", description="높을수록 좋음"), 6, 8)
    b.add(timeseries("걷기 평균 심박", monthly_sample_avg_sql("walking_heart_rate_average", "걷기 평균 심박"),
                     "suffix: bpm", label_field=None, decimals=1, soft_min=None, points="always",
                     description="같은 걷기에 심박이 낮을수록 체력이 좋음"), 6, 8)

    b.row("③ 활동 · 운동 — 월별 (하루 평균)")
    b.add(timeseries("운동 시간", monthly_daily_avg_sql("exercise_time", "운동 시간"), "suffix: 분", bars=True,
                     label_field=None, decimals=1, bar_align=1, threshold=21.4,
                     description="회색 선: WHO 권장 주 150분 ≈ 하루 21분"), 6, 8)
    b.add(timeseries(
        "운동 기록 횟수",
        f"SELECT {month_time('start_time')} AS time, count() AS `운동 횟수`\n"
        f"FROM {D}.workouts FINAL WHERE $__timeFilter(start_time) GROUP BY time ORDER BY time {MONTH_FILL}",
        "suffix: 회", bars=True, label_field=None, decimals=0, bar_align=1), 6, 8)
    b.add(timeseries("활동 에너지", monthly_daily_avg_sql("active_energy", "활동 에너지"), "suffix: kcal", bars=True,
                     label_field=None, decimals=0, bar_align=1), 6, 8)
    b.add(timeseries("걸음 수", monthly_daily_avg_sql("step_count", "걸음 수"), "locale", bars=True,
                     label_field=None, decimals=0, bar_align=1), 6, 8)

    b.row("④ 보행 — 월별")
    b.add(timeseries(
        "걷기 비대칭",
        f"SELECT {month_time('start_time')} AS time, avg(value) * 100 AS `평균 비대칭`,\n"
        "       countIf(value > 0.2) / count() * 100 AS `20% 초과 측정 비율`\n"
        f"FROM {D}.samples FINAL WHERE type = 'walking_asymmetry' AND $__timeFilter(start_time)\n"
        "GROUP BY time ORDER BY time",
        "percent", label_field=None, decimals=1, points="always",
        series_colors={"평균 비대칭": SLOT[0], "20% 초과 측정 비율": SLOT[1]},
        description="iPhone 측정값. 휴대 방식(주머니/가방)이 바뀌어도 달라질 수 있음"), 12, 8)
    b.add(timeseries("걷기 속도", monthly_sample_avg_sql("walking_speed", "걷기 속도", scale=3.6), "velocitykmh",
                     label_field=None, decimals=2, soft_min=None, points="always"), 6, 8)
    b.add(timeseries("보폭", monthly_sample_avg_sql("walking_step_length", "보폭", scale=100), "suffix: cm",
                     label_field=None, decimals=1, soft_min=None, points="always"), 6, 8)

    b.row("⑤ 체성분 · 알림")
    b.add(timeseries(
        "체중 · 제지방량",
        f"SELECT start_time AS time, transform(type, ['body_mass', 'lean_body_mass'], ['체중', '제지방량'], type) AS series,\n"
        f"       value FROM {D}.samples FINAL\n"
        "WHERE type IN ('body_mass', 'lean_body_mass') AND $__timeFilter(start_time) ORDER BY time",
        "masskg", decimals=1, soft_min=None, points="always",
        series_colors={"체중": SLOT[0], "제지방량": SLOT[1]}), 10, 8)
    b.add(timeseries(
        "체지방률",
        f"SELECT start_time AS time, value AS `체지방률` FROM {D}.samples FINAL\n"
        "WHERE type = 'body_fat_percentage' AND $__timeFilter(start_time) ORDER BY time",
        "percentunit", label_field=None, decimals=1, soft_min=None, points="always"), 7, 8)
    b.add(timeseries(
        "심박 알림 (월별 횟수)",
        f"SELECT {month_time('start_time')} AS time, count() AS `심박 알림`\n"
        f"FROM {D}.samples FINAL\n"
        "WHERE type IN ('high_heart_rate_event', 'low_heart_rate_event', 'irregular_heart_rhythm_event')\n"
        f"  AND $__timeFilter(start_time) GROUP BY time ORDER BY time {MONTH_FILL}",
        "suffix: 회", bars=True, label_field=None, decimals=0, bar_align=1,
        description="Apple Watch의 높은/낮은 심박·불규칙 리듬 알림 합계. "
                    "기록이 없다면 Watch 앱 › 심장에서 알림이 켜져 있는지 확인"), 7, 8)

    ds = b.ds
    return dashboard("apple-health-trends", "Apple Health — 추세 점검",
                     "핵심 건강 지표의 장기 추세와 기간 비교 (중요도순)", b, templating(ds, tz), "now-2y")


# ----------------------------------------------------------------------------- explorer


def explorer(ds_uid: str, tz: str) -> dict:
    b = Board(ds_uid)
    m = "type = '${metric}'"
    sm = "type = '${stat_metric}'"

    b.row("샘플 타입: ${metric}")
    b.add(timeseries(
        "${metric} — 평균 · 범위",
        "SELECT $__timeInterval(start_time) AS time, avg(value) AS `평균`, min(value) AS `최저`, max(value) AS `최고`\n"
        f"FROM {D}.samples FINAL WHERE {m} AND $__timeFilter(start_time) GROUP BY time ORDER BY time",
        "none", label_field=None, soft_min=None, points="auto", max_points=500,
        series_colors={"평균": SLOT[0], "최저": SLOT[0], "최고": SLOT[0]},
        overrides=[
            by_name("최고", ("custom.fillBelowTo", "최저"), ("custom.fillOpacity", 18), ("custom.lineWidth", 0)),
            by_name("최저", ("custom.lineWidth", 0)),
        ]), 24, 9)
    b.add(timeseries(
        "${metric} — 일별 평균",
        f"SELECT toStartOfDay(start_time, {TZ}) AS time, avg(value) AS `일 평균`\n"
        f"FROM {D}.samples FINAL WHERE {m} AND {day_range('start_time')} GROUP BY time ORDER BY time",
        "none", label_field=None, soft_min=None, points="always"), 12, 8)
    b.add(bargauge(
        "${metric} — 소스별 샘플 수",
        f"SELECT if(source_name = '', '(알 수 없음)', source_name) AS `소스`, count() AS `건수`\n"
        f"FROM {D}.samples FINAL WHERE {m} AND $__timeFilter(start_time) GROUP BY `소스` ORDER BY `건수` DESC LIMIT 10",
        "locale", decimals=0), 12, 8)
    b.add(table(
        "${metric} — 최근 샘플",
        "SELECT start_time AS `시작`, end_time AS `종료`, value AS `값`, unit AS `단위`, category AS `분류`,\n"
        "  source_name AS `소스`, device AS `기기`, toJSONString(metadata) AS `메타데이터`\n"
        f"FROM {D}.samples FINAL WHERE {m} AND $__timeFilter(start_time) ORDER BY start_time DESC LIMIT 500"), 24, 10)

    b.row("시간별 통계 타입: ${stat_metric}")
    b.add(timeseries(
        "${stat_metric} — 일별 합계",
        f"SELECT toStartOfDay(bucket_start, {TZ}) AS time, sum(value) AS `일 합계`\n"
        f"FROM {D}.quantity_stats FINAL WHERE {sm} AND {day_range('bucket_start')} GROUP BY time ORDER BY time",
        "locale", bars=True, label_field=None), 12, 8)
    b.add(timeseries(
        "${stat_metric} — 시간별 합계",
        f"SELECT bucket_start AS time, value AS `시간 합계`\n"
        f"FROM {D}.quantity_stats FINAL WHERE {sm} AND $__timeFilter(bucket_start) ORDER BY time",
        "locale", bars=True, label_field=None), 12, 8)

    ds = b.ds
    extra = [
        query_var(ds, "metric", "샘플 타입", f"SELECT DISTINCT type FROM {D}.samples ORDER BY type"),
        query_var(ds, "stat_metric", "통계 타입", f"SELECT DISTINCT type FROM {D}.quantity_stats ORDER BY type"),
    ]
    return dashboard("apple-health-explorer", "Apple Health — 데이터 탐색기",
                     "수집된 모든 HealthKit 타입을 골라 원본 값을 살펴보는 대시보드", b,
                     templating(ds, tz, extra), "now-7d")


# ----------------------------------------------------------------------------- main


def system_timezone() -> str:
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "dashboards")
    ap.add_argument("--tz", default=os.environ.get("HEALTH_TZ") or system_timezone())
    ap.add_argument("--datasource-uid", default="apple-health-ch")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, build in (("apple-health-overview.json", overview), ("apple-health-trends.json", trends),
                        ("apple-health-explorer.json", explorer)):
        path = args.out / name
        path.write_text(json.dumps(build(args.datasource_uid, args.tz), ensure_ascii=False, indent=2) + "\n")
        print(f"wrote {path} (tz={args.tz})")


if __name__ == "__main__":
    main()
