"""Turn validated payloads into ClickHouse rows and apply them."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar
from uuid import UUID

from .clickhouse import ClickHouse
from .models import IngestRequest, IngestResponse, Sample, Stat, Workout

T = TypeVar("T")

# Tables that hold HealthKit objects addressable by UUID (and therefore deletable).
DELETABLE_TABLES = ("samples", "workouts")
DELETE_LOOKUP_CHUNK = 1000


def fmt_dt(dt: datetime) -> str:
    """DateTime64(3, 'UTC') text representation."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def chunks(seq: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def sample_row(s: Sample, version: str) -> dict[str, Any]:
    return {
        "uuid": str(s.uuid),
        "type": s.type,
        "value": s.value,
        "unit": s.unit,
        "category": s.category,
        "start_time": fmt_dt(s.start),
        "end_time": fmt_dt(s.end),
        "source_name": s.source,
        "source_bundle": s.source_bundle,
        "device": s.device,
        "metadata": s.metadata,
        "deleted": 0,
        "ingested_at": version,
    }


def workout_row(w: Workout, version: str) -> dict[str, Any]:
    return {
        "uuid": str(w.uuid),
        "activity_type": w.activity_type,
        "start_time": fmt_dt(w.start),
        "end_time": fmt_dt(w.end),
        "duration_s": w.duration_s,
        "energy_kcal": w.energy_kcal,
        "distance_m": w.distance_m,
        "avg_heart_rate": w.avg_heart_rate,
        "max_heart_rate": w.max_heart_rate,
        "source_name": w.source,
        "source_bundle": w.source_bundle,
        "device": w.device,
        "metadata": w.metadata,
        "deleted": 0,
        "ingested_at": version,
    }


def stat_row(s: Stat, version: str) -> dict[str, Any]:
    return {
        "type": s.type,
        "bucket_start": fmt_dt(s.start),
        "bucket_end": fmt_dt(s.end),
        "value": s.value,
        "unit": s.unit,
        "ingested_at": version,
    }


async def apply_deletions(ch: ClickHouse, uuids: Sequence[UUID], version: str) -> int:
    """Write tombstones for HealthKit objects deleted on the phone.

    ReplacingMergeTree only collapses rows that share the full sorting key, but HealthKit
    only reports the UUID of a deleted object. So we look up the live row, copy it with
    deleted=1 and a newer version; `SELECT ... FINAL` then hides it.
    """
    if not uuids:
        return 0
    unique = list(dict.fromkeys(str(u) for u in uuids))
    total = 0
    for table in DELETABLE_TABLES:
        for part in chunks(unique, DELETE_LOOKUP_CHUNK):
            array_literal = "[" + ",".join(f"'{u}'" for u in part) + "]"
            rows = await ch.select(
                f"SELECT * EXCEPT (deleted, ingested_at) FROM {ch.database}.{table} FINAL "
                "WHERE uuid IN {uuids:Array(UUID)}",
                {"uuids": array_literal},
            )
            if rows:
                await ch.insert(table, [{**r, "deleted": 1, "ingested_at": version} for r in rows])
                total += len(rows)
    return total


async def ingest(
    ch: ClickHouse,
    req: IngestRequest,
    *,
    client_ip: str = "",
    now: datetime | None = None,
) -> IngestResponse:
    started = datetime.now(timezone.utc)
    now = now or started
    version = fmt_dt(now)
    # Tombstones must win over rows written in the same request.
    tombstone_version = fmt_dt(now + timedelta(milliseconds=1))

    await ch.insert("samples", [sample_row(s, version) for s in req.samples])
    await ch.insert("workouts", [workout_row(w, version) for w in req.workouts])
    await ch.insert("quantity_stats", [stat_row(s, version) for s in req.stats])
    deleted = await apply_deletions(ch, req.deleted, tombstone_version)

    times = [x.start for x in (*req.samples, *req.workouts, *req.stats)]
    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    await ch.insert(
        "sync_log",
        [
            {
                "received_at": version,
                "device_id": req.device_id,
                "device_name": req.device_name,
                "app_version": req.app_version,
                "client_ip": client_ip,
                "samples": len(req.samples),
                "workouts": len(req.workouts),
                "stats": len(req.stats),
                "deleted": deleted,
                "deleted_requested": len(req.deleted),
                "min_time": fmt_dt(min(times)) if times else None,
                "max_time": fmt_dt(max(times)) if times else None,
                "duration_ms": max(elapsed_ms, 0),
            }
        ],
    )
    return IngestResponse(
        samples=len(req.samples), workouts=len(req.workouts), stats=len(req.stats), deleted=deleted
    )
