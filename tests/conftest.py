from __future__ import annotations

from typing import Any, Callable, Optional

import pytest

from vinted_mcp.client import Vinted
from vinted_mcp.http import Response


class FakeTransport:
    """Records requests and answers from a route table ``(method, url-suffix) -> Response``."""

    def __init__(self, routes: Optional[dict] = None, cookies: Optional[dict] = None) -> None:
        self.routes: dict = routes or {}
        self.calls: list[dict] = []
        self._cookies = dict(cookies or {"access_token_web": "a", "refresh_token_web": "r"})
        self.on_unauthorized: Optional[Callable[[], bool]] = None
        self.domain = "es"

    def cookie(self, name: str) -> Optional[str]:
        return self._cookies.get(name)

    def cookies(self) -> dict:
        return dict(self._cookies)

    def set_cookie(self, name: str, value: str) -> None:
        self._cookies[name] = value

    def request(self, method: str, url: str, *, params=None, json_body=None, multipart=None, headers=None,
                retry_auth: bool = True) -> Response:
        self.calls.append({"method": method, "url": url, "params": params, "json": json_body,
                           "multipart": multipart, "headers": headers})
        for (m, suffix), resp in self.routes.items():
            if m == method and url.endswith(suffix):
                return resp(self.calls[-1]) if callable(resp) else resp
        return Response(404, {"code": 104, "message": "not found"})


@pytest.fixture
def fake() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def client(fake: FakeTransport) -> Vinted:
    v = Vinted(domain="es", transport=fake)
    v._me = {"id": 42, "login": "me"}
    return v


def ok(data: Any, status: int = 200) -> Response:
    return Response(status, data)
