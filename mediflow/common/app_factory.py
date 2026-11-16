"""Shared FastAPI app construction: telemetry, correlation ids, security headers,
body-size limits, safe error handling and health probes."""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config, context
from .security import SECURITY_HEADERS

MAX_BODY_BYTES = 6 * 1024 * 1024


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "service": context.service_name.get(),
            "correlation_id": context.correlation_id.get(),
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "extra_fields", {}))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def get_logger(service: str) -> logging.Logger:
    logger = logging.getLogger(f"mediflow.{service}")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(JsonFormatter())
        logger.addHandler(stream)
        log_dir = config.data_dir() / "logs"
        log_dir.mkdir(exist_ok=True)
        fh = logging.FileHandler(log_dir / f"{service}.log", encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        logger.addHandler(fh)
        logger.propagate = False
    return logger


def _configure_azure_monitor() -> None:
    """Application Insights distributed tracing when a connection string is present."""
    if not config.env("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return
    try:  # pragma: no cover
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor()
    except ImportError:
        pass


def create_service_app(name: str, title: str) -> FastAPI:
    context.service_name.set(name)
    _configure_azure_monitor()
    log = get_logger(name)
    app = FastAPI(title=f"MediFlow {title}", version="1.0.0",
                  docs_url="/docs" if config.environment() != "production" else None)
    app.state.service_name = name
    app.state.log = log

    @app.middleware("http")
    async def platform_middleware(request: Request, call_next):
        context.service_name.set(name)
        cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        context.correlation_id.set(cid)
        context.client_ip.set(request.headers.get("x-forwarded-for", request.client.host if request.client else ""))
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"extra_fields": {"path": request.url.path}})
            response = JSONResponse({"detail": "internal error", "correlation_id": cid}, status_code=500)
        duration = round((time.perf_counter() - start) * 1000, 1)
        response.headers["x-correlation-id"] = cid
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith(("/api", "/patients", "/internal", "/auth")):
            response.headers["Cache-Control"] = "no-store"
        log.info("request", extra={"extra_fields": {
            "method": request.method, "path": request.url.path,
            "status": response.status_code, "duration_ms": duration,
        }})
        return response

    @app.get("/health/live", tags=["health"])
    def live():
        return {"status": "ok", "service": name}

    @app.get("/health/ready", tags=["health"])
    def ready():
        return {"status": "ready", "service": name}

    return app
