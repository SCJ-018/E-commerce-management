# -*- coding: utf-8 -*-
"""Shared, conservative request controls for platform data collectors.

This module deliberately does not spoof browser properties or rotate identities.
It reduces load by spacing requests, honoring Retry-After, and stopping quickly
when a platform signals throttling.  Values can be tuned per deployment with
CRAWLER_MIN_INTERVAL, CRAWLER_JITTER and CRAWLER_COOLDOWN_CAP.
"""
import os
import random
import threading
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Optional


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


class RequestGovernor:
    """Process-local pacing and cooldown guard.

    ``before`` blocks until the next request is allowed. ``after`` should be
    called with the observed HTTP status and headers.  A 429/403/5xx response
    enters a cooldown; callers should not immediately retry outside this guard.
    """

    def __init__(self, name: str = "crawler"):
        self.name = name
        self.min_interval = _float_env("CRAWLER_MIN_INTERVAL", 2.5, 0.1)
        self.jitter = _float_env("CRAWLER_JITTER", 0.35, 0.0)
        self.cooldown_cap = _float_env("CRAWLER_COOLDOWN_CAP", 300.0, 5.0)
        self._next_at = 0.0
        self._cooldown_until = 0.0
        self._lock = threading.Lock()

    def before(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                wait = max(self._next_at, self._cooldown_until) - now
                if wait <= 0:
                    self._next_at = now + self.min_interval + random.uniform(0, self.jitter)
                    return
            time.sleep(min(wait, 60.0))

    @staticmethod
    def _retry_after(headers) -> Optional[float]:
        if not headers:
            return None
        value = headers.get("retry-after") or headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                dt = parsedate_to_datetime(str(value))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def after(self, status: int, headers=None) -> None:
        try:
            status = int(status or 0)
        except (TypeError, ValueError):
            status = 0
        if status == 429 or status == 403 or status >= 500:
            hinted = self._retry_after(headers)
            delay = hinted if hinted is not None else (30.0 if status == 429 else 10.0)
            delay = min(max(delay, 1.0), self.cooldown_cap)
            with self._lock:
                self._cooldown_until = max(self._cooldown_until, time.monotonic() + delay)


def response_headers(response):
    """Best-effort conversion of Playwright response headers to a dict."""
    try:
        return response.headers or {}
    except Exception:
        return {}
