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


def daily_sample_avg_sql(types: dict[str, str]) -> str:
    keys = list(types)
    series = f"transform(type, {sql_array(keys)}, {sql_array(types.values())}, type)"
    return (
        f"SELECT toStartOfDay(start_time, {TZ}) AS time, {series} AS series, avg(value) AS value\n"
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


def overview(ds_uid: str, tz: str) -> dict:
    b = Board(ds_uid)
    today = f"bucket_start >= toStartOfDay(now(), {TZ})"

    b.row("오늘")
    b.add(stat("걸음 수", f"SELECT sum(value) AS v FROM {D}.quantity_stats FINAL WHERE type = 'step_count' AND {today}",
               "locale", description="오늘 0시 이후 걸음 수 (iPhone·Watch 중복 제거)"), 3, 4)
    b.add(stat("활동 에너지", f"SELECT sum(value) AS v FROM {D}.quantity_stats FINAL WHERE type = 'active_energy' AND {today}",
               "suffix: kcal"), 3, 4)
    b.add(stat("운동 시간", f"SELECT sum(value) AS v FROM {D}.quantity_stats FINAL WHERE type = 'exercise_time' AND {today}",
               "suffix: 분"), 3, 4)
    b.add(stat("이동 거리", f"SELECT sum(value) AS v FROM {D}.quantity_stats FINAL WHERE type = 'distance_walking_running' AND {today}",
               "lengthm", decimals=1), 3, 4)
    b.add(stat("안정시 심박", f"SELECT argMax(value, start_time) AS v FROM {D}.samples FINAL "
               "WHERE type = 'resting_heart_rate' AND start_time > now() - INTERVAL 30 DAY",
               "suffix: bpm", description="가장 최근 안정시 심박수"), 3, 4)
    b.add(stat("HRV (7일 평균)", f"SELECT avg(value) AS v FROM {D}.samples FINAL "
               "WHERE type = 'heart_rate_variability' AND start_time > now() - INTERVAL 7 DAY",
               "ms", description="심박 변이도 SDNN, 최근 7일 평균"), 3, 4)
    b.add(stat("지난밤 수면", SLEEP_RECENT + "SELECT sumIf(secs, category LIKE 'asleep%') / 3600 AS v "
               "FROM nights WHERE night = (SELECT max(night) FROM nights)",
               "suffix: 시간", decimals=1, description="가장 최근 밤의 총 수면 시간 (깨어있던 시간 제외)"), 3, 4)
    b.add(stat("체중", f"SELECT argMax(value, start_time) AS v FROM {D}.samples FINAL WHERE type = 'body_mass'",
               "masskg", decimals=1, description="가장 최근 기록"), 3, 4)

    b.row("활동")
    b.add(timeseries("일별 걸음 수", daily_stat_sql({"step_count": "걸음 수"}), "locale", bars=True,
                     decimals=0, threshold=10000, description="회색 선: 10,000보"), 12, 8)
    b.add(timeseries("일별 에너지 소모", daily_stat_sql({"active_energy": "활동 에너지", "basal_energy": "휴식 에너지"}),
                     "suffix: kcal", bars=True, stack=True, decimals=0,
                     series_colors={"활동 에너지": SLOT[0], "휴식 에너지": SLOT[1]},
                     description="막대 전체 높이 = 총 소모 에너지"), 12, 8)
    b.add(timeseries("일별 운동 시간", daily_stat_sql({"exercise_time": "운동 시간"}), "suffix: 분", bars=True, decimals=0), 8, 7)
    b.add(timeseries("일별 스탠드 시간", daily_stat_sql({"stand_time": "스탠드 시간"}), "suffix: 분", bars=True, decimals=0), 8, 7)
    b.add(timeseries("일별 오른 층수", daily_stat_sql({"flights_climbed": "오른 층수"}), "suffix: 층", bars=True, decimals=0), 8, 7)
    b.add(timeseries("일별 걷기·달리기 거리", daily_stat_sql({"distance_walking_running": "거리"}), "lengthm",
                     bars=True, decimals=1), 12, 7)
    b.add(barchart(
        "시간대별 평균 걸음 수",
        f"SELECT lpad(toString(toHour(bucket_start, {TZ})), 2, '0') AS hour, "
        "sum(value) / greatest(dateDiff('day', $__fromTime, $__toTime), 1) AS steps\n"
        f"FROM {D}.quantity_stats FINAL WHERE type = 'step_count' AND $__timeFilter(bucket_start)\n"
        "GROUP BY hour ORDER BY hour",
        "locale", "hour", description="선택한 기간의 하루 평균, 시(hour)별"), 12, 7)

    b.row("심장")
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
        description="구간별 평균(선)과 최저~최고 범위(음영)"), 24, 9)
    b.add(timeseries("안정시 심박 · 걷기 평균 심박",
                     daily_sample_avg_sql({"resting_heart_rate": "안정시 심박", "walking_heart_rate_average": "걷기 평균 심박"}),
                     "suffix: bpm", decimals=0, soft_min=None, points="always",
                     series_colors={"안정시 심박": SLOT[0], "걷기 평균 심박": SLOT[1]}), 12, 8)
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
    b.add(timeseries("혈중 산소 (일 평균)", daily_sample_avg_sql({"oxygen_saturation": "혈중 산소"}), "percentunit",
                     decimals=1, soft_min=None, points="always"), 8, 7)
    b.add(timeseries("호흡수 (일 평균)", daily_sample_avg_sql({"respiratory_rate": "호흡수"}), "suffix: 회/분",
                     decimals=1, soft_min=None, points="always"), 8, 7)
    b.add(timeseries("VO₂ max", daily_sample_avg_sql({"vo2_max": "VO₂ max"}), "suffix: ml/kg·min",
                     decimals=1, soft_min=None, points="always", description="심폐 체력 추정치"), 8, 7)

    b.row("수면")
    b.add(timeseries(
        "수면 단계",
        SLEEP_IN_RANGE + f"SELECT toDateTime(night, {TZ}) AS time,\n"
        + ",\n".join(f"  sumIf(secs, category = '{c}') / 3600 AS `{label}`" for c, label, _ in SLEEP_STAGES)
        + "\nFROM nights GROUP BY time ORDER BY time",
        "suffix: 시간", bars=True, stack=True, decimals=1, label_field=None,
        series_colors={label: c for _, label, c in SLEEP_STAGES},
        description="기상일 기준. 같은 밤을 여러 앱/기기가 기록했다면 수면 시간이 가장 긴 소스 하나만 사용"), 16, 9)
    b.add(timeseries(
        "총 수면 시간",
        SLEEP_IN_RANGE + "SELECT time, asleep AS `수면 시간`,\n"
        "       avg(asleep) OVER (ORDER BY time ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS `7일 평균`\n"
        f"FROM (SELECT toDateTime(night, {TZ}) AS time, sumIf(secs, category LIKE 'asleep%') / 3600 AS asleep\n"
        "      FROM nights GROUP BY time)\nORDER BY time",
        "suffix: 시간", label_field=None, decimals=1, soft_min=None, points="always",
        series_colors={"수면 시간": SLOT[0], "7일 평균": SLOT[1]},
        overrides=[by_name("수면 시간", ("custom.lineWidth", 1))]), 8, 9)
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

    b.row("신체")
    for t, name, unit in (("body_mass", "체중", "masskg"), ("body_fat_percentage", "체지방률", "percentunit"),
                          ("body_mass_index", "BMI", "none")):
        b.add(timeseries(
            name,
            f"SELECT start_time AS time, value AS `{name}` FROM {D}.samples FINAL\n"
            f"WHERE type = '{t}' AND $__timeFilter(start_time) ORDER BY time",
            unit, label_field=None, decimals=1, soft_min=None, points="always"), 8, 7)

    b.row("운동")
    b.add(timeseries(
        "주별 운동 시간",
        f"SELECT toDateTime(toStartOfWeek(start_time, 1, {TZ}), {TZ}) AS time, sum(duration_s) / 60 AS `운동 시간`\n"
        f"FROM {D}.workouts FINAL WHERE {day_range('start_time')} GROUP BY time ORDER BY time",
        "suffix: 분", bars=True, label_field=None, decimals=0, bar_align=1, description="월요일 시작 주 기준"), 12, 8)
    b.add(bargauge(
        "운동 종류별 총 시간",
        f"SELECT {ACTIVITY_LABEL} AS `운동`, sum(duration_s) / 3600 AS `시간`\n"
        f"FROM {D}.workouts FINAL WHERE $__timeFilter(start_time) GROUP BY `운동` ORDER BY `시간` DESC LIMIT 10",
        "suffix: 시간"), 12, 8)
    b.add(table(
        "운동 기록",
        f"SELECT start_time AS `시작`, {ACTIVITY_LABEL} AS `운동`, round(duration_s / 60) AS `시간(분)`,\n"
        "  round(energy_kcal) AS `kcal`, round(distance_m / 1000, 2) AS `거리(km)`,\n"
        "  round(avg_heart_rate) AS `평균 심박`, round(max_heart_rate) AS `최고 심박`, source_name AS `기록 기기`\n"
        f"FROM {D}.workouts FINAL WHERE $__timeFilter(start_time) ORDER BY start_time DESC LIMIT 200"), 24, 9)

    b.row("동기화 상태")
    b.add(stat("마지막 수신", f"SELECT toUnixTimestamp64Milli(max(received_at)) AS v FROM {D}.sync_log",
               "dateTimeFromNow", decimals=None, description="iPhone 앱에서 마지막으로 데이터를 받은 시각"), 6, 4)
    b.add(stat("동기화 기기", f"SELECT argMax(device_name, received_at) AS v FROM {D}.sync_log",
               "none", decimals=None, fields="/.*/"), 6, 4)
    b.add(stat("저장된 샘플", f"SELECT count() AS v FROM {D}.samples FINAL", "locale"), 6, 4)
    b.add(stat("저장된 운동", f"SELECT count() AS v FROM {D}.workouts FINAL", "locale"), 6, 4)
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
    return dashboard("apple-health-overview", "Apple Health", "iPhone 건강 데이터 종합 대시보드", b,
                     templating(ds, tz), "now-30d")


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
    for name, build in (("apple-health-overview.json", overview), ("apple-health-explorer.json", explorer)):
        path = args.out / name
        path.write_text(json.dumps(build(args.datasource_uid, args.tz), ensure_ascii=False, indent=2) + "\n")
        print(f"wrote {path} (tz={args.tz})")


if __name__ == "__main__":
    main()
