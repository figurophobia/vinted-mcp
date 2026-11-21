from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from vinted_mcp import config, mcp_server as m
from vinted_mcp.client import Vinted
from vinted_mcp.http import Response


def _names(allow_writes: bool) -> set[str]:
    server = m.build_server(allow_writes)
    tools = asyncio.run(server.list_tools())
    return {t.name for t in tools}


def test_writes_hidden_by_default() -> None:
    read = _names(False)
    assert {f.__name__ for f in m._READ_TOOLS} == read
    assert not read & {f.__name__ for f in m._WRITE_TOOLS}


def test_writes_registered_with_flag() -> None:
    assert _names(True) == {f.__name__ for f in m._READ_TOOLS + m._WRITE_TOOLS}


def test_no_checkout_tools() -> None:
    # Reading purchases (list_purchases) is fine; nothing may buy, check out or pay.
    words = {w for n in _names(True) for w in n.split("_")}
    assert not words & {"buy", "checkout", "pay", "payment", "purchase", "bump"}


def test_session_tools_require_login(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VINTED_COOKIES", raising=False)
    m._init(str(tmp_path / "none.json"), False)
    with pytest.raises(RuntimeError):
        m.list_inbox()
    status = m.session_status()
    assert status["logged_in"] is False and status["writes_enabled"] is False
    assert status["session_state"] == "missing"


@pytest.mark.parametrize("source", ["file", "env"])
@pytest.mark.parametrize(("token", "state", "logged_in"), [
    ("h.eyJleHAiOjk5OTk5OTk5OTl9.s", "anonymous", False),  # {"exp":9999999999}
    ("h.eyJzdWIiOiI0MiJ9.s", "signed_in", True),  # {"sub":"42"}
    ("opaque", "unknown", False),
])
def test_session_status_describes_account_identity(tmp_path, monkeypatch, source, token, state, logged_in) -> None:
    path = str(tmp_path / "session.json")
    monkeypatch.delenv("VINTED_COOKIES", raising=False)
    monkeypatch.setenv("VINTED_DOMAIN", "be")
    if source == "env":
        monkeypatch.setenv("VINTED_COOKIES", f"access_token_web={token}")
    else:
        config.save_session({"domain": "be", "cookies": {"access_token_web": token}}, path)
    m._init(path, False)

    status = m.session_status()
    assert status["session_state"] == state
    assert status["logged_in"] is logged_in
    assert status["domain"] == "be" and status["writes_enabled"] is False
    assert token not in str(status)


@pytest.mark.parametrize("body", [{"message": "Access denied.", "message_code": "FORBIDDEN"}, "<html>Challenge</html>"])
def test_search_reports_access_denied(fake, monkeypatch, body) -> None:
    fake.routes[("GET", "/svc-catalogue/items")] = Response(403, body)
    monkeypatch.setattr(m, "_client", Vinted(domain="be", transport=fake))

    with pytest.raises(ToolError, match=r"Vinted returned HTTP 403: Access denied\.") as error:
        asyncio.run(m.build_server(False).call_tool("search", {"text": "jean à pinces"}))

    assert "Don't retry" in str(error.value)
    assert "cookies" not in str(error.value)
    assert len(fake.calls) == 1
