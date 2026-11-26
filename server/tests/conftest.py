from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TOKEN = "test-token-0123456789abcdef"


class FakeClickHouse:
    """Records inserts and answers the deletion lookup from what was inserted."""

    def __init__(self, database: str = "health") -> None:
        self.database = database
        self.tables: dict[str, list[dict[str, Any]]] = {}

    async def ping(self) -> bool:
        return True

    async def execute(self, sql: str, params: dict[str, str] | None = None) -> str:
        return ""

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if rows:
            self.tables.setdefault(table, []).extend(rows)

    async def select(self, sql: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        m = re.search(r"FROM \w+\.(\w+) FINAL WHERE uuid IN", sql)
        if not m:
            return []
        wanted = set(re.findall(r"'([0-9a-f-]{36})'", (params or {}).get("uuids", "")))
        live: dict[str, dict[str, Any]] = {}
        for row in self.tables.get(m.group(1), []):
            if row["deleted"]:
                live.pop(row["uuid"], None)
            else:
                live[row["uuid"]] = row
        return [
            {k: v for k, v in r.items() if k not in ("deleted", "ingested_at")}
            for u, r in live.items()
            if u in wanted
        ]


@pytest.fixture
def fake_ch() -> FakeClickHouse:
    return FakeClickHouse()


@pytest.fixture
def client(fake_ch: FakeClickHouse):
    app = create_app(Settings(api_token=TOKEN, max_body_bytes=200_000), ch=fake_ch)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c
