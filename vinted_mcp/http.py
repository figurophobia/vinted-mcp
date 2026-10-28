"""HTTP transport over curl_cffi: browser TLS fingerprint, pacing, safe retries.

Policy:

- Vinted is fronted by Cloudflare + DataDome, so requests go through
  ``curl_cffi`` impersonating Chrome; plain ``urllib``/``requests`` get 403.
- Only reads (``GET``) are retried, on transport errors and 429/502/503/504.
  Writes are sent exactly once: a write that dies mid-flight raises
  :class:`UnknownOutcomeError` because the server may have applied it.
- Every request start is spaced by at least :data:`RATE_PACE` seconds.
- A ``401`` triggers one token refresh (``on_unauthorized``) and one replay.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from curl_cffi import requests as cffi
from curl_cffi.curl import CurlError

RATE_PACE = float(os.environ.get("VINTED_RATE_PACE", "0.6"))


RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


MAX_RETRIES = 3


MAX_RETRY_AFTER = 30.0


_PACE_LOCK = threading.Lock()


_LAST_START = 0.0


class ApiError(Exception):
    """Any failure the caller can report to the user."""


class TransportError(ApiError):
    """Network level failure. ``outcome`` is ``"not_sent"`` or ``"unknown"``."""

    outcome = "not_sent"


class UnknownOutcomeError(TransportError):
    """A write failed mid-flight: the server may or may not have applied it."""

    outcome = "unknown"


@dataclass
class Response:
    """Decoded API answer. ``data`` is parsed JSON (or text for non-JSON bodies)."""

    status: int
    data: Any = None
    headers: dict = field(default_factory=dict)
    url: str = ""
    set_cookies: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        if not 200 <= self.status < 300:
            return False
        # The core API answers 200 with {"code": != 0} on some business errors.
        if isinstance(self.data, dict) and isinstance(self.data.get("code"), int) and self.data["code"] != 0:
            return False
        return True

    @property
    def blocked(self) -> bool:
        """True when the anti-bot layer (not the API) answered."""
        return self.status == 403 and not isinstance(self.data, (dict, list))

    @property
    def error_reason(self) -> str:
        if self.ok:
            return ""
        if self.blocked:
            return "blocked by anti-bot protection (403 HTML); refresh the session cookies (vinted login --browser)"
        d = self.data
        if isinstance(d, dict):
            parts = [str(d.get(k)) for k in ("message", "message_code", "error", "code") if d.get(k) not in (None, "")]
            errs = d.get("errors")
            if isinstance(errs, list) and errs:
                parts.append("; ".join(
                    f"{e.get('field', '')}: {e.get('value') or e.get('message') or e}" if isinstance(e, dict) else str(e)
                    for e in errs
                ))
            if parts:
                return f"HTTP {self.status}: " + " | ".join(parts)
        if self.status == 404:
            return "HTTP 404: not found"
        return f"HTTP {self.status}"

    def raise_for_error(self) -> "Response":
        if not self.ok:
            raise ApiError(self.error_reason)
        return self


def _pace() -> None:
    global _LAST_START
    with _PACE_LOCK:
        now = time.monotonic()
        wait = _LAST_START + RATE_PACE - now
        _LAST_START = max(now, _LAST_START + RATE_PACE)
    if wait > 0:
        time.sleep(wait)


def _decode(r: Any) -> Any:
    ctype = (r.headers.get("content-type") or "").lower()
    text = r.text
    if "json" in ctype or text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text


class Transport:
    """A cookie-carrying curl_cffi session with Vinted's browser headers."""

    def __init__(
        self,
        cookies: Optional[dict[str, str]] = None,
        *,
        domain: str,
        locale: str,
        csrf_token: str,
        impersonate: str = "chrome",
        timeout: float = 30.0,
    ) -> None:
        self.domain = domain
        self.timeout = timeout
        self.on_unauthorized: Optional[Callable[[], bool]] = None
        self.session = cffi.Session(impersonate=impersonate)
        for k, v in (cookies or {}).items():
            self.session.cookies.set(k, v, domain=f".vinted.{domain}")
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*, image/webp",
            "Accept-Language": f"{locale},{locale.split('-')[0]};q=0.9",
            "Locale": locale,
            "X-CSRF-Token": csrf_token,
            "Origin": f"https://www.vinted.{domain}",
            "Referer": f"https://www.vinted.{domain}/",
        })
        anon = (cookies or {}).get("anon_id")
        if anon:
            self.session.headers["X-Anon-Id"] = anon

    # -- cookies ---------------------------------------------------------

    def cookie(self, name: str) -> Optional[str]:
        for c in self.session.cookies.jar:
            if c.name == name:
                return c.value
        return None

    def set_cookie(self, name: str, value: str) -> None:
        """Replace every copy of a cookie (any domain) by one on ``.vinted.<tld>``."""
        for c in list(self.session.cookies.jar):
            if c.name == name:
                self.session.cookies.jar.clear(c.domain, c.path, c.name)
        self.session.cookies.set(name, value, domain=f".vinted.{self.domain}")

    def cookies(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for c in self.session.cookies.jar:
            out[c.name] = c.value
        return out

    # -- requests --------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        multipart: Any = None,
        headers: Optional[dict] = None,
        retry_auth: bool = True,
    ) -> Response:
        method = method.upper()
        safe = method == "GET"
        attempt = 0
        while True:
            attempt += 1
            _pace()
            try:
                r = self.session.request(
                    method, url, params=_clean(params), json=json_body, multipart=multipart,
                    headers=headers, timeout=self.timeout,
                )
            except CurlError as e:
                if safe and attempt <= MAX_RETRIES:
                    time.sleep(min(8.0, 0.5 * 2 ** attempt + random.random()))
                    continue
                if safe:
                    raise TransportError(f"{method} {url}: {e}") from e
                raise UnknownOutcomeError(f"{method} {url}: {e} (the server may have applied it)") from e
            resp = Response(r.status_code, _decode(r), dict(r.headers), url, dict(r.cookies))
            if resp.status == 401 and retry_auth and self.on_unauthorized and self.on_unauthorized():
                retry_auth = False
                attempt -= 1
                continue
            if safe and resp.status in RETRYABLE_STATUSES and attempt <= MAX_RETRIES:
                delay = _retry_after(resp.headers) or min(8.0, 0.5 * 2 ** attempt + random.random())
                if delay > MAX_RETRY_AFTER:
                    return resp
                time.sleep(delay)
                continue
            return resp


def _clean(params: Optional[dict]) -> Optional[dict]:
    if not params:
        return None
    out = {}
    for k, v in params.items():
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (list, tuple, set)):
            if not v:
                continue
            v = ",".join(str(x) for x in v)
        out[k] = v
    return out


def _retry_after(headers: dict) -> Optional[float]:
    v = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None
