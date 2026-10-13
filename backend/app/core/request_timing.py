"""Payload-free request timings, shared with synchronous worker threads."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import logging
from time import perf_counter
from uuid import uuid4


@dataclass
class Timings:
    values: dict[str, float] = field(default_factory=dict)
    sql_count: int = 0


current_timings: ContextVar[Timings | None] = ContextVar("request_timings", default=None)
logger = logging.getLogger("drasil.request")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.propagate = False


@contextmanager
def timed(name):
    start = perf_counter()
    try:
        yield
    finally:
        timings = current_timings.get()
        if timings is not None:
            timings.values[name] = timings.values.get(name, 0) + (perf_counter() - start) * 1000


class RequestTimingMiddleware:
    def __init__(self, app):
        self.app = app
        self.first_request = True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        timings = Timings()
        token = current_timings.set(timings)
        request_id = uuid4().hex
        first_request, self.first_request = self.first_request, False
        start, status = perf_counter(), 500

        async def timed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                timings.values["app"] = (perf_counter() - start) * 1000
                header = ", ".join(f"{name};dur={value:.2f}" for name, value in timings.values.items())
                message["headers"] = [*message.get("headers", []),
                                      (b"x-request-id", request_id.encode()),
                                      (b"server-timing", header.encode())]
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        finally:
            route = scope.get("route")
            logger.info(json.dumps({"event": "http_timing", "requestId": request_id,
                "route": getattr(route, "path", "/api/unmatched"), "method": scope["method"],
                "status": status, "firstRequestInInstance": first_request,
                "importMs": getattr(scope["app"].state, "import_ms", None) if first_request else None,
                "startupMs": getattr(scope["app"].state, "startup_ms", None) if first_request else None,
                "durationMs": round((perf_counter() - start) * 1000, 2),
                "sqlCount": timings.sql_count,
                "timingsMs": {key: round(value, 2) for key, value in timings.values.items()}}))
            current_timings.reset(token)
