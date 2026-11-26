from __future__ import annotations

import os
import re
from dataclasses import dataclass

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_identifier(name: str) -> str:
    """ClickHouse database names are interpolated into SQL, so only plain identifiers are allowed."""
    if not _IDENT.match(name):
        raise ValueError(f"invalid ClickHouse identifier: {name!r}")
    return name


@dataclass(frozen=True)
class Settings:
    api_token: str
    ch_url: str = "http://127.0.0.1:8123"
    ch_user: str = "health_writer"
    ch_password: str = ""
    ch_database: str = "health"
    max_body_bytes: int = 32 * 1024 * 1024
    max_decompressed_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        if len(self.api_token) < 16:
            raise ValueError("HEALTH_API_TOKEN must be at least 16 characters")
        validate_identifier(self.ch_database)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            api_token=env.get("HEALTH_API_TOKEN", ""),
            ch_url=env.get("HEALTH_CH_URL", cls.ch_url),
            ch_user=env.get("HEALTH_CH_USER", cls.ch_user),
            ch_password=env.get("HEALTH_CH_PASSWORD", cls.ch_password),
            ch_database=env.get("HEALTH_CH_DATABASE", cls.ch_database),
            max_body_bytes=int(env.get("HEALTH_MAX_BODY_BYTES", cls.max_body_bytes)),
        )
