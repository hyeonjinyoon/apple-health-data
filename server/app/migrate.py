"""Apply SQL migrations in migrations/ to a ClickHouse database.

    python -m app.migrate --database health

Connection comes from HEALTH_CH_ADMIN_URL / HEALTH_CH_ADMIN_USER / HEALTH_CH_ADMIN_PASSWORD
(or --password-file). Needs a user allowed to CREATE DATABASE / TABLE.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from pathlib import Path

from .clickhouse import ClickHouse
from .config import validate_identifier

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def split_statements(sql: str) -> list[str]:
    no_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in no_comments.split(";") if s.strip()]


async def migrate(ch: ClickHouse, database: str) -> list[str]:
    db = validate_identifier(database)
    await ch.execute(f"CREATE DATABASE IF NOT EXISTS {db}")
    await ch.execute(
        f"CREATE TABLE IF NOT EXISTS {db}.schema_migrations "
        "(name String, applied_at DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY name"
    )
    applied = {r["name"] for r in await ch.select(f"SELECT name FROM {db}.schema_migrations")}
    done: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        for stmt in split_statements(path.read_text().replace("{db}", db)):
            await ch.execute(stmt)
        await ch.execute(f"INSERT INTO {db}.schema_migrations (name) VALUES ('{path.name}')")
        done.append(path.name)
    return done


async def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default=os.environ.get("HEALTH_CH_DATABASE", "health"))
    ap.add_argument("--url", default=os.environ.get("HEALTH_CH_ADMIN_URL", "http://127.0.0.1:8123"))
    ap.add_argument("--user", default=os.environ.get("HEALTH_CH_ADMIN_USER", "default"))
    ap.add_argument("--password-file", type=Path)
    args = ap.parse_args()
    password = (
        args.password_file.read_text().strip()
        if args.password_file
        else os.environ.get("HEALTH_CH_ADMIN_PASSWORD", "")
    )
    ch = ClickHouse(args.url, args.user, password, args.database)
    try:
        done = await migrate(ch, args.database)
    finally:
        await ch.close()
    print(f"[{args.database}] applied: {', '.join(done) if done else '(up to date)'}")


if __name__ == "__main__":
    asyncio.run(_main())
