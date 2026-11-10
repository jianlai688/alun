"""可观测性：请求指标、结构化日志、就绪检查。"""
from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import Request
from fastapi.responses import Response

try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
    _PROM = True
except Exception:  # pragma: no cover
    _PROM = False

if _PROM:
    HTTP_TOTAL = Counter("alun_http_requests_total", "HTTP requests", ["method", "route", "status"])
    HTTP_LATENCY = Histogram("alun_http_request_duration_seconds", "HTTP latency", ["method", "route"])
    PARSE_TOTAL = Counter("alun_parse_jobs_total", "Parse jobs", ["status"])
    QUEUE_DEPTH = Gauge("alun_queue_depth", "Redis stream depth")
    DB_UP = Gauge("alun_dependency_up", "Dependency health", ["dependency"])


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }, ensure_ascii=False)


def setup_logging(json_logs: bool = True) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if json_logs else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers[:] = [handler]
        logger.propagate = False


def instrument(app) -> None:
    @app.middleware("http")
    async def _metrics(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        started = time.perf_counter()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = request.scope.get("route")
            route_path = getattr(route, "path", request.url.path)
            status = str(getattr(response, "status_code", 500))
            if _PROM:
                HTTP_TOTAL.labels(request.method, route_path, status).inc()
                HTTP_LATENCY.labels(request.method, route_path).observe(elapsed)
            if response is not None:
                response.headers["X-Request-ID"] = request_id
                response.headers["Server-Timing"] = f"app;dur={elapsed * 1000:.1f}"


def metrics() -> Response:
    if not _PROM:
        return Response("prometheus_client not installed\n", status_code=503, media_type="text/plain")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def refresh_dependencies() -> dict:
    from ..app import db
    from . import queue, redis_client, storage
    out = {"database": {"backend": "postgres" if db.DB_BACKEND == "postgres" else "sqlite", "ok": False},
           "redis": redis_client.ping(), "storage": storage.health(),
           "queue": {"backend": "redis" if queue.enabled() else "inline", "ok": True, "depth": queue.depth()}}
    try:
        db.fetch_one("SELECT 1 AS ok")
        out["database"]["ok"] = True
    except Exception as exc:
        out["database"]["error"] = str(exc)[:200]
    if _PROM:
        DB_UP.labels("database").set(1 if out["database"]["ok"] else 0)
        DB_UP.labels("redis").set(1 if out["redis"].get("ok") else 0)
        QUEUE_DEPTH.set(out["queue"]["depth"])
    return out
