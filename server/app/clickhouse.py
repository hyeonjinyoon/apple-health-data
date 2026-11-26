"""Minimal async ClickHouse client over the HTTP interface (no native driver needed)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import validate_identifier


class ClickHouseError(RuntimeError):
    pass


class ClickHouse:
    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        database: str,
        timeout: float = 60.0,
    ) -> None:
        self.database = validate_identifier(database)
        self._client = httpx.AsyncClient(
            base_url=url,
            timeout=timeout,
            headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def ping(self) -> bool:
        try:
            r = await self._client.get("/ping")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def execute(self, sql: str, params: dict[str, str] | None = None) -> str:
        """Run a statement. `params` bind to `{name:Type}` placeholders server-side."""
        url_params = {
            # Return UInt64 aggregates (count() etc.) as JSON numbers, not strings.
            "output_format_json_quote_64bit_integers": "0",
            **{f"param_{k}": v for k, v in (params or {}).items()},
        }
        try:
            r = await self._client.post("/", params=url_params, content=sql.encode())
        except httpx.HTTPError as e:
            raise ClickHouseError(f"ClickHouse unreachable: {e}") from e
        if r.status_code != 200:
            raise ClickHouseError(r.text.strip()[:2000])
        return r.text

    async def select(self, sql: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        text = await self.execute(f"{sql}\nFORMAT JSONEachRow", params)
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        validate_identifier(table)
        body = "\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows)
        query = f"INSERT INTO {self.database}.{table} FORMAT JSONEachRow"
        try:
            r = await self._client.post("/", params={"query": query}, content=body.encode())
        except httpx.HTTPError as e:
            raise ClickHouseError(f"ClickHouse unreachable: {e}") from e
        if r.status_code != 200:
            raise ClickHouseError(r.text.strip()[:2000])
