from __future__ import annotations

import hmac
import re
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque

from fastapi import Request

from app.config import Settings

WRITE_METHODS = {"POST", "PATCH", "DELETE"}
AI_ROUTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/videos/\d+/generate$"),
    re.compile(r"^/videos/\d+/assets/[^/]+/regenerate$"),
    re.compile(r"^/videos/\d+/preview/render-draft$"),
    re.compile(r"^/executive-producer/recommendation/run$"),
    re.compile(r"^/research/youtube/run$"),
)
SENSITIVE_GET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/audit$"),
    re.compile(r"^/videos/\d+/audit$"),
    re.compile(r"^/research(?:/.*)?$"),
    re.compile(r"^/executive-producer(?:/.*)?$"),
)


def is_public_path(path: str) -> bool:
    return path == "/health" or path == "/app" or path.startswith("/static/")


def needs_auth(path: str, method: str) -> bool:
    method_upper = method.upper()
    if method_upper in WRITE_METHODS:
        return True
    if method_upper != "GET":
        return False
    return any(pattern.match(path) for pattern in SENSITIVE_GET_PATTERNS)


def is_ai_cost_route(path: str, method: str) -> bool:
    if method.upper() not in WRITE_METHODS:
        return False
    return any(pattern.match(path) for pattern in AI_ROUTE_PATTERNS)


def auth_error_payload() -> dict[str, str]:
    return {"detail": "Missing or invalid X-Internal-API-Key header."}


def check_internal_api_key(settings: Settings, request: Request) -> bool:
    configured_key = (settings.internal_api_key or "").strip()
    if not configured_key:
        return not settings.is_production

    provided_key = (request.headers.get("X-Internal-API-Key") or "").strip()
    if not provided_key:
        return False
    return hmac.compare_digest(configured_key, provided_key)


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._events: defaultdict[str, Deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, *, limit: int, window_seconds: int = 60) -> bool:
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"
