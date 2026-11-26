from __future__ import annotations

import logging
import secrets
import zlib
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from .clickhouse import ClickHouse, ClickHouseError
from .config import Settings
from .ingest import ingest
from .models import IngestRequest, IngestResponse

log = logging.getLogger("health_ingest")


def _decompress(body: bytes, encoding: str, limit: int) -> bytes:
    encoding = encoding.lower().strip()
    if encoding in ("", "identity"):
        return body
    if encoding == "gzip":
        wbits_options = [16 + zlib.MAX_WBITS]
    elif encoding == "deflate":
        # Apple's NSData.compressed(using: .zlib) emits raw DEFLATE (RFC 1951);
        # other clients send zlib-wrapped (RFC 1950). Accept both.
        wbits_options = [-zlib.MAX_WBITS, zlib.MAX_WBITS]
    else:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"unsupported encoding {encoding}")
    for wbits in wbits_options:
        d = zlib.decompressobj(wbits)
        try:
            out = d.decompress(body, limit + 1)
        except zlib.error:
            continue
        if len(out) > limit or d.unconsumed_tail:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "decompressed body too large")
        return out
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "could not decompress body")


def create_app(settings: Settings | None = None, ch: Any | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or Settings.from_env()
        client = ch or ClickHouse(s.ch_url, s.ch_user, s.ch_password, s.ch_database)
        app.state.settings = s
        app.state.ch = client
        log.info("health-ingest ready (database=%s)", client.database)
        try:
            yield
        finally:
            if ch is None:
                await client.close()

    app = FastAPI(title="Apple Health Ingest", version="1.0.0", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    async def require_token(
        request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)
    ) -> None:
        expected: str = request.app.state.settings.api_token
        if creds is None or not secrets.compare_digest(creds.credentials.encode(), expected.encode()):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "invalid token", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.exception_handler(ClickHouseError)
    async def _ch_error(_: Request, exc: ClickHouseError):
        log.error("ClickHouse error: %s", exc)
        return JSONResponse({"detail": f"storage error: {exc}"}, status_code=status.HTTP_502_BAD_GATEWAY)

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        ok = await request.app.state.ch.ping()
        return {"status": "ok" if ok else "degraded", "clickhouse": ok}

    @app.get("/api/v1/ping", dependencies=[Depends(require_token)])
    async def ping(request: Request) -> dict[str, Any]:
        return {"ok": True, "database": request.app.state.ch.database}

    @app.post("/api/v1/ingest", dependencies=[Depends(require_token)], response_model=IngestResponse)
    async def ingest_endpoint(request: Request) -> IngestResponse:
        s: Settings = request.app.state.settings
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > s.max_body_bytes:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "body too large")
        body = await request.body()
        if len(body) > s.max_body_bytes:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "body too large")
        raw = _decompress(body, request.headers.get("content-encoding", ""), s.max_decompressed_bytes)
        try:
            payload = IngestRequest.model_validate_json(raw)
        except ValidationError as e:
            raise RequestValidationError(e.errors(include_url=False)) from e
        client_ip = request.client.host if request.client else ""
        result = await ingest(request.app.state.ch, payload, client_ip=client_ip)
        log.info(
            "ingest from %s (%s): samples=%d workouts=%d stats=%d deleted=%d",
            payload.device_name or "?", client_ip, result.samples, result.workouts, result.stats, result.deleted,
        )
        return result

    @app.get("/api/v1/summary", dependencies=[Depends(require_token)])
    async def summary(request: Request) -> dict[str, Any]:
        ch = request.app.state.ch
        db = ch.database
        samples = await ch.select(
            f"SELECT type, count() AS count, toString(max(end_time)) AS latest "
            f"FROM {db}.samples FINAL GROUP BY type ORDER BY type"
        )
        stats = await ch.select(
            f"SELECT type, count() AS buckets, toString(max(bucket_end)) AS latest "
            f"FROM {db}.quantity_stats FINAL GROUP BY type ORDER BY type"
        )
        workouts = await ch.select(
            f"SELECT count() AS count, toString(max(end_time)) AS latest FROM {db}.workouts FINAL"
        )
        return {"samples": samples, "stats": stats, "workouts": workouts[0] if workouts else {}}

    return app


app = create_app()
