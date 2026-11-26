from __future__ import annotations

import gzip
import json
import zlib

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from conftest import TOKEN

SAMPLE = {
    "uuid": "6f0c3c1e-9d7b-4a51-8f0e-2b1f4c5d6e7f",
    "type": "heart_rate",
    "value": 72,
    "unit": "count/min",
    "start": "2026-11-25T21:00:00.250+09:00",
    "end": "2026-11-25T21:00:00.250+09:00",
    "source": "Apple Watch",
    "device": "Watch7,1",
    "metadata": {"HKMetadataKeyHeartRateMotionContext": "1"},
}
WORKOUT = {
    "uuid": "a1b2c3d4-0000-4000-8000-000000000001",
    "activity_type": "running",
    "start": "2026-11-25T07:00:00Z",
    "end": "2026-11-25T07:30:00Z",
    "duration_s": 1800,
    "energy_kcal": 310.5,
    "distance_m": 5020,
}
STAT = {"type": "step_count", "start": "2026-11-25T03:00:00Z", "end": "2026-11-25T04:00:00Z", "value": 1234, "unit": "count"}


def test_health_needs_no_token(client):
    del client.headers["Authorization"]
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "clickhouse": True}


def test_rejects_missing_or_wrong_token(client):
    assert client.post("/api/v1/ingest", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    del client.headers["Authorization"]
    assert client.post("/api/v1/ingest", json={}).status_code == 401
    assert client.get("/api/v1/ping").status_code == 401


def test_ping(client):
    assert client.get("/api/v1/ping").json() == {"ok": True, "database": "health"}


def test_ingest_maps_rows_and_converts_to_utc(client, fake_ch):
    r = client.post(
        "/api/v1/ingest",
        json={"device_name": "iPhone", "samples": [SAMPLE], "workouts": [WORKOUT], "stats": [STAT]},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"samples": 1, "workouts": 1, "stats": 1, "deleted": 0}

    (row,) = fake_ch.tables["samples"]
    assert row["start_time"] == "2026-11-25 12:00:00.250"  # 21:00 KST -> 12:00 UTC
    assert row["type"] == "heart_rate" and row["value"] == 72.0
    assert row["source_name"] == "Apple Watch"
    assert row["metadata"] == {"HKMetadataKeyHeartRateMotionContext": "1"}
    assert row["deleted"] == 0

    (w,) = fake_ch.tables["workouts"]
    assert w["activity_type"] == "running" and w["avg_heart_rate"] is None
    (s,) = fake_ch.tables["quantity_stats"]
    assert s["bucket_start"] == "2026-11-25 03:00:00.000" and s["value"] == 1234.0

    (log,) = fake_ch.tables["sync_log"]
    assert log["device_name"] == "iPhone"
    assert log["samples"] == 1 and log["min_time"] == "2026-11-25 03:00:00.000"


def test_raw_deflate_body_is_accepted(client, fake_ch):
    raw = json.dumps({"samples": [SAMPLE]}).encode()
    comp = zlib.compressobj(wbits=-zlib.MAX_WBITS)  # what Apple's .zlib produces
    body = comp.compress(raw) + comp.flush()
    r = client.post(
        "/api/v1/ingest",
        content=body,
        headers={"Content-Type": "application/json", "Content-Encoding": "deflate"},
    )
    assert r.status_code == 200, r.text
    assert len(fake_ch.tables["samples"]) == 1


def test_gzip_body_is_accepted(client, fake_ch):
    body = gzip.compress(json.dumps({"stats": [STAT]}).encode())
    r = client.post("/api/v1/ingest", content=body, headers={"Content-Encoding": "gzip"})
    assert r.status_code == 200, r.text


def test_deletion_writes_tombstone_with_same_key(client, fake_ch):
    client.post("/api/v1/ingest", json={"samples": [SAMPLE], "workouts": [WORKOUT]})
    r = client.post(
        "/api/v1/ingest",
        json={"deleted": [SAMPLE["uuid"], WORKOUT["uuid"], "00000000-0000-4000-8000-0000000000ff"]},
    )
    assert r.json()["deleted"] == 2
    live, tomb = fake_ch.tables["samples"]
    assert tomb["deleted"] == 1
    assert (tomb["type"], tomb["start_time"], tomb["uuid"]) == (live["type"], live["start_time"], live["uuid"])
    assert tomb["ingested_at"] > live["ingested_at"]
    assert fake_ch.tables["workouts"][-1]["deleted"] == 1
    assert fake_ch.tables["sync_log"][-1]["deleted_requested"] == 3


def test_validation_errors(client):
    bad = {**SAMPLE, "uuid": "not-a-uuid"}
    assert client.post("/api/v1/ingest", json={"samples": [bad]}).status_code == 422
    backwards = {**STAT, "start": "2026-11-25T05:00:00Z"}
    assert client.post("/api/v1/ingest", json={"stats": [backwards]}).status_code == 422
    assert client.post("/api/v1/ingest", content=b"{not json").status_code == 422


def test_body_too_large(client):
    big = {"samples": [SAMPLE] * 2000}
    assert client.post("/api/v1/ingest", json=big).status_code == 413


def test_decompression_bomb_rejected(fake_ch):
    app = create_app(Settings(api_token=TOKEN, max_decompressed_bytes=10_000), ch=fake_ch)
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/ingest",
            content=zlib.compress(b" " * 1_000_000),
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Encoding": "deflate"},
        )
    assert r.status_code == 413
