# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Minimal HTTP helper built on urllib so the action needs no third-party packages."""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} for {url}: {body[:800]}")
        self.status = status
        self.url = url
        self.body = body


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    timeout: int = 180,
    retries: int = 4,
) -> Response:
    """Issue a request, retrying transient failures with exponential backoff and jitter."""
    data = None
    headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as exc:  # noqa: PERF203 - retry loop
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code not in RETRY_STATUSES or attempt == retries:
                raise HttpError(exc.code, url, body) from exc
            last_error = HttpError(exc.code, url, body)
            delay = _backoff(attempt, exc.headers.get("Retry-After"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == retries:
                raise
            last_error = exc
            delay = _backoff(attempt, None)

        log.warning("%s %s failed (%s); retrying in %.1fs", method, url, last_error, delay)
        time.sleep(delay)

    raise RuntimeError(f"unreachable retry state for {url}") from last_error


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return min(2.0**attempt, 30.0) + random.uniform(0, 1.0)
