"""API gateway - local stand-in for Azure API Management.

Routes /api/<service>/<path> to the internal service, enforcing:
  * per-client rate limits (stricter on authentication endpoints)
  * blocking of /internal/* routes from the outside world
  * correlation ids + X-Forwarded-For for end-to-end tracing
  * security headers; serves the web portal

The equivalent APIM policies live in infra/modules/apim.bicep.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import httpx
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from mediflow.common import config
from mediflow.common.app_factory import create_service_app
from mediflow.common.security import SlidingWindowRateLimiter

FRONTEND = config.ROOT / "frontend"
ROUTABLE = {"identity", "patient", "appointment", "prescription", "billing", "document", "ai", "audit"}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}


def client_ip(request: Request) -> str:
    """Behind Azure's front end (MEDIFLOW_TRUST_PROXY=1) the caller is the first X-Forwarded-For hop."""
    if config.env("MEDIFLOW_TRUST_PROXY") == "1" and request.headers.get("x-forwarded-for"):
        first = request.headers["x-forwarded-for"].split(",")[0].strip()
        return first.rsplit(":", 1)[0] if first.count(":") == 1 else first  # strip ":port"
    return request.client.host if request.client else "unknown"


def create_app(inprocess: dict | None = None):
    """inprocess: {service: ASGI app} - route in memory instead of over HTTP (single-container mode)."""
    app = create_service_app("gateway", "API Gateway")
    general = SlidingWindowRateLimiter(limit=int(config.env("MEDIFLOW_RATE_LIMIT_PER_MIN", "300")), window_seconds=60)
    auth_limiter = SlidingWindowRateLimiter(limit=int(config.env("MEDIFLOW_AUTH_RATE_LIMIT_PER_MIN", "20")),
                                            window_seconds=60)
    if inprocess:
        clients = {s: httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url=f"http://{s}", timeout=30)
                   for s, a in inprocess.items()}
        base = {s: "" for s in inprocess}
    else:
        shared = httpx.AsyncClient(timeout=30)
        clients = {s: shared for s in ROUTABLE}
        base = {s: config.service_url(s) for s in ROUTABLE}

    @app.get("/api/health", tags=["health"])
    async def aggregate_health():
        out = {}
        for svc in sorted(ROUTABLE):
            try:
                r = await clients[svc].get(f"{base[svc]}/health/ready", timeout=2)
                out[svc] = "up" if r.status_code == 200 else f"http {r.status_code}"
            except httpx.HTTPError:
                out[svc] = "down"
        return {"gateway": "up", "services": out}

    @app.api_route("/api/{service}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(service: str, path: str, request: Request):
        if service not in ROUTABLE:
            return JSONResponse({"detail": "unknown service"}, status_code=404)
        if path.startswith("internal") or path.startswith("health"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        ip = client_ip(request)
        limiter = auth_limiter if service == "identity" and path.startswith("auth/") else general
        ok, retry = limiter.allow(f"{ip}:{service if limiter is auth_limiter else '*'}")
        if not ok:
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429, headers={"Retry-After": str(retry)})
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["x-forwarded-for"] = ip
        headers["x-correlation-id"] = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        try:
            upstream = await clients[service].request(request.method, f"{base[service]}/{path}",
                                            params=request.query_params, content=await request.body(), headers=headers)
        except httpx.HTTPError:
            return JSONResponse({"detail": f"{service} service unavailable"}, status_code=503)
        resp_headers = {k: v for k, v in upstream.headers.items()
                        if k.lower() not in HOP_BY_HOP and k.lower() != "content-encoding"}
        return Response(upstream.content, status_code=upstream.status_code, headers=resp_headers)

    if FRONTEND.exists():
        app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(Path(FRONTEND) / "index.html")

    return app
