"""End-to-end against a real ClickHouse, in a throwaway database.

Runs only when HEALTH_IT_CH_PASSWORD (or HEALTH_IT_CH_PASSWORD_FILE) is set:

    HEALTH_IT_CH_PASSWORD_FILE=~/.local/clickhouse/.password uv run pytest -m integration
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.clickhouse import ClickHouse
from app.config import Settings
from app.main import create_app
from app.migrate import migrate
from conftest import TOKEN

URL = os.environ.get("HEALTH_IT_CH_URL", "http://127.0.0.1:8123")
USER = os.environ.get("HEALTH_IT_CH_USER", "default")
_pw_file = os.environ.get("HEALTH_IT_CH_PASSWORD_FILE")
PASSWORD = (
    Path(_pw_file).expanduser().read_text().strip() if _pw_file else os.environ.get("HEALTH_IT_CH_PASSWORD")
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(PASSWORD is None, reason="set HEALTH_IT_CH_PASSWORD[_FILE] to run"),
]


def run(coro):
    return asyncio.run(coro)


async def _q(db: str, sql: str):
    ch = ClickHouse(URL, USER, PASSWORD or "", db)
    try:
        return await ch.select(sql)
    finally:
        await ch.close()


@pytest.fixture
def db():
    name = f"health_it_{uuid.uuid4().hex[:8]}"

    async def setup():
        ch = ClickHouse(URL, USER, PASSWORD or "", name)
        try:
            assert await migrate(ch, name) == ["001_init.sql"]
            assert await migrate(ch, name) == []  # idempotent
        finally:
            await ch.close()

    async def teardown():
        ch = ClickHouse(URL, USER, PASSWORD or "", name)
        try:
            await ch.execute(f"DROP DATABASE IF EXISTS {name} SYNC")
        finally:
            await ch.close()

    run(setup())
    yield name
    run(teardown())


def test_ingest_dedupes_and_deletes(db):
    app = create_app(Settings(api_token=TOKEN, ch_url=URL, ch_user=USER, ch_password=PASSWORD or "", ch_database=db))
    hr = [
        {
            "uuid": str(uuid.UUID(int=i, version=4)),
            "type": "heart_rate",
            "value": 60 + i,
            "unit": "count/min",
            "start": f"2026-11-25T0{i}:00:00Z",
            "end": f"2026-11-25T0{i}:00:00Z",
            "source": "Apple Watch",
            "metadata": {"k": "v"},
        }
        for i in range(1, 4)
    ]
    sleep = {
        "uuid": str(uuid.uuid4()),
        "type": "sleep_analysis",
        "value": 3,
        "category": "asleep_core",
        "start": "2026-11-24T15:00:00Z",
        "end": "2026-11-24T17:30:00Z",
    }
    stat = {"type": "step_count", "start": "2026-11-25T03:00:00Z", "end": "2026-11-25T04:00:00Z", "value": 100, "unit": "count"}
    workout = {
        "uuid": str(uuid.uuid4()),
        "activity_type": "walking",
        "start": "2026-11-25T07:00:00Z",
        "end": "2026-11-25T07:30:00Z",
        "duration_s": 1800,
    }

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        body = {"samples": [*hr, sleep], "stats": [stat], "workouts": [workout]}
        assert c.post("/api/v1/ingest", json=body).status_code == 200
        # Re-send everything (e.g. after an anchor reset) and update the hourly stat.
        body["stats"] = [{**stat, "value": 250}]
        assert c.post("/api/v1/ingest", json=body).status_code == 200

        rows = run(_q(db, f"SELECT type, count() AS n FROM {db}.samples FINAL GROUP BY type ORDER BY type"))
        assert rows == [{"type": "heart_rate", "n": 3}, {"type": "sleep_analysis", "n": 1}]
        (s,) = run(_q(db, f"SELECT value FROM {db}.quantity_stats FINAL"))
        assert s["value"] == 250

        r = c.post("/api/v1/ingest", json={"deleted": [hr[0]["uuid"], workout["uuid"]]})
        assert r.json()["deleted"] == 2

        (n,) = run(_q(db, f"SELECT count() AS n FROM {db}.samples FINAL WHERE type = 'heart_rate'"))
        assert n["n"] == 2
        (w,) = run(_q(db, f"SELECT count() AS n FROM {db}.workouts FINAL"))
        assert w["n"] == 0
        (m,) = run(_q(db, f"SELECT metadata['k'] AS k FROM {db}.samples FINAL WHERE type = 'heart_rate' LIMIT 1"))
        assert m["k"] == "v"

        summary = c.get("/api/v1/summary").json()
        assert {x["type"]: x["count"] for x in summary["samples"]} == {"heart_rate": 2, "sleep_analysis": 1}
        assert summary["workouts"]["count"] == 0

        (log,) = run(_q(db, f"SELECT sum(samples) AS s, sum(deleted) AS d, count() AS n FROM {db}.sync_log"))
        assert log == {"s": 8, "d": 2, "n": 3}
