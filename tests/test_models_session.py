from __future__ import annotations

import json
import os

import pytest

from vinted_mcp import config as cfg
from vinted_mcp.ids import item_id, user_id
from vinted_mcp.models import extract_plugins, item_details_from_page, item_from_api
from vinted_mcp.session import SessionError, build_client, import_cookie_text, parse_cookie_text, session_info


def test_item_refs() -> None:
    assert item_id("https://www.vinted.es/items/1234567893-nintendo-3ds?ref=x") == 1234567893
    assert item_id("1234567893-nintendo-3ds") == 1234567893
    assert item_id(5) == 5
    assert user_id("https://www.vinted.es/member/555000111-buyer1") == 555000111
    with pytest.raises(ValueError):
        item_id("nintendo")


def test_search_item_mapping_uses_item_box() -> None:
    it = item_from_api({
        "id": 1, "title": "3DS", "content_source": "search", "price": {"amount": "20.0", "currency_code": "EUR"},
        "total_item_price": {"amount": "22.0"}, "item_box": {"first_line": "Nintendo", "second_line": "M · Muy bueno"},
        "url": "https://www.vinted.es/items/1-3ds", "user": {"id": 9, "login": "x"},
        "photo": {"url": "https://img/1.webp"},
    })
    assert (it.brand, it.condition, it.size, it.total_price, it.seller_id) == ("Nintendo", "Muy bueno", "M", "22.0", 9)


def test_wardrobe_item_mapping_ignores_view_counter() -> None:
    it = item_from_api({
        "id": 2, "title": "t", "brand": "Nintendo", "status": "Bueno", "is_hidden": True,
        "item_alert_type": "delayed_publication", "path": "/items/2-t",
        "price": {"amount": "465.0", "currency_code": "EUR"}, "item_box": {"first_line": "2 visitas"},
    }, "https://www.vinted.es")
    d = it.to_dict()
    assert d["brand"] == "Nintendo" and d["condition"] == "Bueno"
    assert d["url"] == "https://www.vinted.es/items/2-t"
    assert d["status"] == {"is_hidden": True, "item_alert_type": "delayed_publication"}


def _page(plugins: list, ld: dict) -> str:
    rsc = json.dumps({"plugins": plugins})
    half = len(rsc) // 2
    pushes = "".join(f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>" for part in (rsc[:half], rsc[half:]))
    return f'<script type="application/ld+json">{json.dumps(ld)}</script>{pushes}'


def test_item_page_parsing_joins_flight_chunks() -> None:
    html = _page(
        [
            {"name": "attributes", "data": {"attributes": [{"code": "status", "data": {"value": "Bueno"}}]}},
            {"name": "description", "data": {"description": "Como nueva"}},
            {"name": "ask_seller", "data": {"can_buy": True, "seller_id": "3", "is_reserved": False}},
            {"name": "user_info_header", "data": {"name": "seller", "feedback_count": 9}},
        ],
        {"name": "3DS", "offers": {"price": 1, "priceCurrency": "EUR", "url": "u"}, "brand": {"name": "Nintendo"}},
    )
    assert len(extract_plugins(html)) == 4
    d = item_details_from_page(html, 7)
    assert d["title"] == "3DS" and d["price"] == "1" and d["description"] == "Como nueva"
    assert d["attributes"] == {"status": "Bueno"} and d["seller_id"] == "3" and d["seller"]["name"] == "seller"


@pytest.mark.parametrize("text", [
    "access_token_web=a; refresh_token_web=r; anon_id=x; _ga=1",
    "Cookie: access_token_web=a; refresh_token_web=r; anon_id=x",
    json.dumps({"access_token_web": "a", "refresh_token_web": "r", "anon_id": "x"}),
    json.dumps([{"name": "access_token_web", "value": "a", "domain": ".vinted.es"},
                {"name": "refresh_token_web", "value": "r", "domain": ".vinted.es"},
                {"name": "anon_id", "value": "x", "domain": ".vinted.es"}]),
    ".vinted.es\tTRUE\t/\tTRUE\t0\taccess_token_web\ta\n.vinted.es\tTRUE\t/\tTRUE\t0\trefresh_token_web\tr\n"
    ".vinted.es\tTRUE\t/\tTRUE\t0\tanon_id\tx\n",
])
def test_cookie_formats(text: str) -> None:
    c = parse_cookie_text(text)
    assert c["access_token_web"] == "a" and c["refresh_token_web"] == "r" and c["anon_id"] == "x"


def test_import_and_build_client(tmp_path) -> None:
    path = str(tmp_path / "s" / "session.json")
    tok = _tok("42", 3600)
    s = import_cookie_text(f"access_token_web={tok}; refresh_token_web=r; _ga=drop", "es", path)
    assert s["cookies"] == {"access_token_web": tok, "refresh_token_web": "r"}
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    v = build_client(path=path)
    assert v.logged_in and v.domain == "es"
    assert session_info(path)["logged_in"] is True


def test_anonymous_session_is_rejected(tmp_path) -> None:
    import base64

    anon = "h." + base64.urlsafe_b64encode(b'{"exp": 9999999999}').decode().rstrip("=") + ".s"
    path = tmp_path / "x.json"
    with pytest.raises(SessionError):
        import_cookie_text(f"access_token_web={anon}; refresh_token_web=r", "es", str(path))
    assert not path.exists()


def test_import_without_auth_cookies_fails(tmp_path) -> None:
    with pytest.raises(SessionError):
        import_cookie_text("_ga=1; anon_id=2", "es", str(tmp_path / "x.json"))


def test_build_client_required(tmp_path) -> None:
    with pytest.raises(SessionError):
        build_client(path=str(tmp_path / "none.json"))
    assert build_client(required=False, path=str(tmp_path / "none.json")).logged_in is False


def test_hosts() -> None:
    assert cfg.core_base("fr") == "https://www.vinted.fr/api/v2"
    assert cfg.edge_base("es") == "https://api.vinted.es"


def test_offer_events() -> None:
    from vinted_mcp.models import offer_events

    conv = {"messages": [
        {"data": {"entity_type": "message", "entity": {"body": "hola"}}},
        {"data": {"entity_type": "offer_request_message", "created_at_ts": "t1", "entity": {
            "offer_request_id": 1, "price": {"amount": "35.0", "currency_code": "EUR"}, "status": 30,
            "status_title": "Rechazada", "transaction_id": 9, "user_id": 2, "current": False}}},
        {"data": {"entity_type": "offer_message", "id": 5, "entity": {
            "price": {"amount": "37.0", "currency_code": "EUR"}, "user_id": 3, "current": True}}},
    ]}
    ev = offer_events(conv)
    assert [e["kind"] for e in ev] == ["buyer_offer", "seller_offer"]
    assert ev[0]["status"] == "rejected" and ev[0]["id"] == 1 and ev[0]["transaction_id"] == 9
    assert ev[1]["id"] == 5 and ev[1]["price"] == "37.0" and "status" not in ev[1]


def _tok(sub, exp_in, iat=None):
    import base64
    import time

    now = int(time.time())
    payload = {"sub": sub, "exp": now + exp_in, "iat": now if iat is None else iat}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"h.{body}.s"


T0 = 1_700_000_000  # issue times: smaller = older


def _recovery_client(refresh_status):
    """Client holding tokens issued at T0+100; ``refresh_status(fake)`` decides the refresh answer."""
    from vinted_mcp.client import Vinted
    from vinted_mcp.http import Response

    from .conftest import FakeTransport

    fake = FakeTransport(cookies={"access_token_web": _tok("1", -10, T0 + 100),
                                  "refresh_token_web": _tok("1", 600000, T0 + 100)})

    def answer(call):
        status = refresh_status(fake)
        if status == 200:
            return Response(200, {}, set_cookies={"access_token_web": _tok("1", 3600, T0 + 500),
                                                  "refresh_token_web": _tok("1", 604800, T0 + 500)})
        return Response(status, {} if status != 403 else "<html>blocked</html>")

    fake.routes[("POST", "/web/api/auth/refresh")] = answer
    return Vinted(domain="es", transport=fake), fake


def _refreshes(fake):
    return sum("auth/refresh" in c["url"] for c in fake.calls)


def test_recover_adopts_newer_browser_token_without_refreshing(monkeypatch) -> None:
    from vinted_mcp import session as s

    newer = {"access_token_web": _tok("1", 3000, T0 + 300), "refresh_token_web": _tok("1", 600000, T0 + 300)}
    monkeypatch.setattr(s, "read_browser_cookies", lambda *a: dict(newer))
    v, fake = _recovery_client(lambda f: 200)
    assert s.recover_session(v, "firefox", "es") is True
    assert fake.cookie("access_token_web") == newer["access_token_web"] and _refreshes(fake) == 0


def test_recover_never_adopts_older_browser_tokens(monkeypatch) -> None:
    from vinted_mcp import session as s

    # Still-valid access token but issued before ours: adopting it would bring back a rotated refresh token.
    older = {"access_token_web": _tok("1", 3000, T0 + 50), "refresh_token_web": _tok("1", 600000, T0 + 50)}
    monkeypatch.setattr(s, "read_browser_cookies", lambda *a: dict(older))
    v, fake = _recovery_client(lambda f: 401)
    assert s.recover_session(v, "firefox", "es") is False
    assert fake.cookie("refresh_token_web") != older["refresh_token_web"] and _refreshes(fake) == 1


def test_recover_refreshes_when_browser_has_nothing_newer(monkeypatch) -> None:
    from vinted_mcp import session as s

    monkeypatch.setattr(s, "read_browser_cookies", lambda *a: {})
    v, fake = _recovery_client(lambda f: 200)
    assert s.recover_session(v, "firefox", "es") is True
    assert _jwt_iat(fake.cookie("refresh_token_web")) == T0 + 500


def test_recover_uses_newer_browser_refresh_token_after_rejection(monkeypatch) -> None:
    from vinted_mcp import session as s

    browser_rt = _tok("1", 600000, T0 + 300)
    monkeypatch.setattr(s, "read_browser_cookies",
                        lambda *a: {"access_token_web": _tok("1", -5, T0 + 300), "refresh_token_web": browser_rt})
    v, fake = _recovery_client(lambda f: 200 if f.cookie("refresh_token_web") == browser_rt else 401)
    assert s.recover_session(v, "firefox", "es") is True
    assert _refreshes(fake) == 2 and _jwt_iat(fake.cookie("refresh_token_web")) == T0 + 500


def test_recover_does_not_switch_tokens_on_transient_failure(monkeypatch) -> None:
    from vinted_mcp import session as s

    browser_rt = _tok("1", 600000, T0 + 300)
    monkeypatch.setattr(s, "read_browser_cookies",
                        lambda *a: {"access_token_web": _tok("1", -5, T0 + 300), "refresh_token_web": browser_rt})
    v, fake = _recovery_client(lambda f: 403)  # DataDome page, not a token rejection
    assert s.recover_session(v, "firefox", "es") is False
    assert fake.cookie("refresh_token_web") != browser_rt and _refreshes(fake) == 1


def test_recover_ignores_other_users_token(monkeypatch) -> None:
    from vinted_mcp import session as s

    monkeypatch.setattr(s, "read_browser_cookies",
                        lambda *a: {"access_token_web": _tok("999", 3000, T0 + 300),
                                    "refresh_token_web": _tok("999", 600000, T0 + 300)})
    v, fake = _recovery_client(lambda f: 401)
    assert s.recover_session(v, "firefox", "es") is False


def test_wizard_sessions_do_not_read_the_profile(tmp_path) -> None:
    from vinted_mcp import config
    from vinted_mcp.session import build_client

    path = str(tmp_path / "s.json")
    config.save_session({"domain": "es", "source": "wizard",
                         "cookies": {"access_token_web": _tok("1", 3600), "refresh_token_web": "r"}}, path)
    v = build_client(path=path)
    assert v.http.on_unauthorized == v.refresh


def _jwt_iat(tok):
    from vinted_mcp.session import _jwt

    return _jwt(tok).get("iat")
