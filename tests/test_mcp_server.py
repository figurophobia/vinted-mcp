from __future__ import annotations

import asyncio

import pytest

from vinted_mcp import mcp_server as m


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
    names = _names(True)
    assert not any(w in n for n in names for w in ("buy", "checkout", "pay", "purchase"))


def test_session_tools_require_login(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VINTED_COOKIES", raising=False)
    m._init(str(tmp_path / "none.json"), False)
    with pytest.raises(RuntimeError):
        m.list_inbox()
    status = m.session_status()
    assert status["logged_in"] is False and status["writes_enabled"] is False
