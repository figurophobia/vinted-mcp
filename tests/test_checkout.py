from __future__ import annotations

import asyncio
import json

import pytest

from vinted_mcp import checkout
from vinted_mcp import mcp_server as m
from vinted_mcp.client import Vinted
from vinted_mcp.http import Response, UnknownOutcomeError

from .conftest import FakeTransport, ok

ITEM = 10878995461
TID = 77
PID = "synthetic-purchase"


def quote(total: str = "12004.0", *, card: bool = True, available: bool = True, signals: bool = False,
          address: bool = True) -> dict:
    """A synthetic checkout answer shaped like the web checkout's components."""
    selected = {"pay_in_method": {"payment_method": "credit_card"},
                "credit_card": {"external_code": "card-1", "expired": False, "brand": "VISA", "last_four": "1234"}}
    comp = {
        "pay_button_v2": {"payments_available": available, "total": {"price": {"amount": total, "currency_code": "HUF"}}},
        "order_summary_v2": {"items": [{"title": "Item", "price": {"amount": "11166.0", "currency_code": "HUF"}},
                                       {"title": "Buyer protection", "price": {"amount": "838.0", "currency_code": "HUF"}}],
                             "deductions": []},
        "payment_method": {
            "pay_in_methods": [{"payment_method": "credit_card", "enabled": True}, {"payment_method": "wallet", "enabled": True}],
            "cards": [{"id": "card-1", "expired": False, "brand": "VISA", "last_four": "1234"}],
            "selected_payment_method": selected if card else None,
        },
        "shipping_address": {"address": {"id": 55, "is_complete": True, "city": "Synthville"}} if address else {},
        "shipping_pickup_options": {"selected_pickup_option": 1},
        "shipping_pickup_details": {"pickup_details": {"selected_rate_uuid": "rate-1"}},
    }
    out: dict = {"purchase": {"id": PID}, "checksum": "checksum-1", "checkout": {"components": comp}}
    if signals:
        out["checkout"]["adyen_protect_signals_enabled"] = True
    return out


@pytest.fixture
def hu() -> tuple[Vinted, FakeTransport]:
    fake = FakeTransport()
    fake.domain = "hu"
    v = Vinted(domain="hu", transport=fake)
    v._me = {"id": 42, "login": "me"}
    v.item = lambda ref: {"id": ITEM, "title": "Echo Dot", "seller_id": 9, "can_buy": True, "is_reserved": False}
    fake.routes[("POST", "/messaging/main/inquiries")] = ok({"conversation_id": "c-1", "transaction_id": TID})
    fake.routes[("POST", "/purchases/checkout/build")] = ok(quote(card=False))
    fake.routes[("PUT", f"/purchases/{PID}/checkout")] = ok(quote())
    fake.routes[("GET", "/users/current")] = ok({"user": {"id": 42}})
    return v, fake


@pytest.fixture
def ledger(tmp_path) -> str:
    return str(tmp_path / "checkouts.json")


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _calls(fake: FakeTransport, method: str, suffix: str) -> list[dict]:
    return [c for c in fake.calls if c["method"] == method and c["url"].endswith(suffix)]


# --- currency ------------------------------------------------------------------


def test_offers_use_the_site_currency(hu) -> None:
    v, fake = hu
    fake.routes[("POST", f"/transactions/{TID}/offer_requests")] = ok({"code": 0})
    fake.routes[("POST", "/offers/request_options")] = ok({"code": 0})
    v.send_offer(ITEM, 10049, transaction_id=TID)
    v.offer_constraints(ITEM, 11166, seller_id=9)
    assert _calls(fake, "POST", "/offer_requests")[0]["json"]["offer_request"] == {"price": "10049", "currency": "HUF"}
    assert _calls(fake, "POST", "/request_options")[0]["json"]["price"]["currency_code"] == "HUF"


def test_euro_sites_keep_eur(client: Vinted, fake: FakeTransport) -> None:
    fake.routes[("POST", "/transactions/5/offer_requests")] = ok({"code": 0})
    client.send_offer(1, 35, transaction_id=5)
    assert fake.calls[0]["json"]["offer_request"]["currency"] == "EUR"
    assert Vinted(domain="pl", transport=FakeTransport()).currency == "PLN"


# --- prepare ------------------------------------------------------------------------


def test_prepare_selects_the_saved_card_and_never_pays(hu, ledger) -> None:
    v, fake = hu
    out = checkout.prepare(v, ITEM, ledger_path=ledger)
    assert out["ready_to_pay"] and len(out["confirmation_code"]) == 6
    assert out["total"] == {"amount": "12004.00", "currency": "HUF"}
    assert out["payment"]["card"] == "VISA ****1234"
    assert {"label": "Buyer protection", "amount": "838.00", "currency": "HUF"} in out["lines"]
    assert _calls(fake, "POST", "/purchases/checkout/build")[0]["json"] == {"purchase_items": [{"id": TID, "type": "transaction"}]}
    upd = _calls(fake, "PUT", f"/purchases/{PID}/checkout")
    assert upd and upd[-1]["json"]["components"]["payment_method"] == {"card_id": "card-1", "payment_method": "credit_card"}
    assert not _calls(fake, "POST", "/checkout/payment")
    rec = _read(ledger)[PID]
    assert rec["status"] == "prepared" and rec["code_hash"] != out["confirmation_code"]


def test_prepare_without_a_card_gives_no_code(hu, ledger) -> None:
    v, fake = hu
    no_card = quote(card=False)
    no_card["checkout"]["components"]["payment_method"]["cards"] = []
    fake.routes[("POST", "/purchases/checkout/build")] = ok(no_card)
    fake.routes[("PUT", f"/purchases/{PID}/checkout")] = ok(no_card)
    out = checkout.prepare(v, ITEM, ledger_path=ledger)
    assert not out["ready_to_pay"] and "confirmation_code" not in out
    assert out["checkout_url"].startswith("https://www.vinted.hu/checkout?purchase_id=")
    assert _read(ledger)[PID]["status"] == "review_required"


def test_prepare_refuses_card_signals(hu, ledger) -> None:
    v, fake = hu
    fake.routes[("PUT", f"/purchases/{PID}/checkout")] = ok(quote(signals=True))
    assert "confirmation_code" not in checkout.prepare(v, ITEM, ledger_path=ledger)


def test_prepare_pickup_selects_the_point_vinted_offers(hu, ledger) -> None:
    v, fake = hu
    q = quote()
    det = q["checkout"]["components"]["shipping_pickup_details"]["pickup_details"]
    det["shipping_point"] = {"uuid": "pt-uuid", "code": "PT1", "rate_uuid": "rate-1", "name": "Locker"}
    q["checkout"]["components"]["shipping_pickup_options"]["selected_pickup_option"] = 2
    fake.routes[("PUT", f"/purchases/{PID}/checkout")] = ok(q)
    out = checkout.prepare(v, ITEM, shipping="pickup", ledger_path=ledger)
    comp = _calls(fake, "PUT", f"/purchases/{PID}/checkout")[-1]["json"]["components"]
    assert comp["shipping_pickup_options"] == {"pickup_type": 2}
    assert comp["shipping_pickup_details"] == {"rate_uuid": "rate-1", "point_code": "PT1", "point_uuid": "pt-uuid"}
    assert out["delivery"]["point"]["name"] == "Locker" and out["ready_to_pay"]


# --- pay ------------------------------------------------------------------------------


def test_pay_once_with_code_and_total(hu, ledger) -> None:
    v, fake = hu
    fake.routes[("POST", f"/purchases/{PID}/checkout/payment")] = ok({"payment": {"status": "success"}})
    code = checkout.prepare(v, ITEM, ledger_path=ledger)["confirmation_code"]
    res = checkout.pay(v, PID, code.lower(), 12004, ledger_path=ledger)
    assert res["status"] == "card_payment_confirmed"
    pay = _calls(fake, "POST", "/checkout/payment")
    assert len(pay) == 1 and pay[0]["json"]["checksum"] == "checksum-1" and pay[0]["retry_auth"] is False
    with pytest.raises(checkout.CheckoutError):
        checkout.pay(v, PID, code, 12004, ledger_path=ledger)
    with pytest.raises(checkout.CheckoutError, match="already started"):
        checkout.prepare(v, ITEM, ledger_path=ledger)
    assert len(_calls(fake, "POST", "/checkout/payment")) == 1


@pytest.mark.parametrize("code, total", [("WRONG1", 12004), (None, 12000)])
def test_pay_refuses_wrong_code_or_total(hu, ledger, code, total) -> None:
    v, fake = hu
    real = checkout.prepare(v, ITEM, ledger_path=ledger)["confirmation_code"]
    with pytest.raises(checkout.CheckoutError):
        checkout.pay(v, PID, code or real, total, ledger_path=ledger)
    assert not _calls(fake, "POST", "/checkout/payment")


def test_pay_refuses_a_changed_quote(hu, ledger) -> None:
    v, fake = hu
    code = checkout.prepare(v, ITEM, ledger_path=ledger)["confirmation_code"]
    fake.routes[("PUT", f"/purchases/{PID}/checkout")] = ok(quote("13500.0"))
    with pytest.raises(checkout.CheckoutError, match="changed"):
        checkout.pay(v, PID, code, 12004, ledger_path=ledger)
    assert not _calls(fake, "POST", "/checkout/payment")
    assert _read(ledger)[PID]["status"] == "review_required"


def test_pay_refuses_an_expired_confirmation(hu, ledger, monkeypatch) -> None:
    v, fake = hu
    code = checkout.prepare(v, ITEM, ledger_path=ledger)["confirmation_code"]
    real_time = checkout.time.time
    monkeypatch.setattr(checkout.time, "time", lambda: real_time() + checkout.CONFIRM_TTL + 1)
    with pytest.raises(checkout.CheckoutError, match="expired"):
        checkout.pay(v, PID, code, 12004, ledger_path=ledger)
    assert not _calls(fake, "POST", "/checkout/payment")


def test_card_authentication_hands_back_the_checkout(hu, ledger) -> None:
    v, fake = hu
    fake.routes[("POST", f"/purchases/{PID}/checkout/payment")] = ok(
        {"action": {"type": "native_adyen_payment_3ds", "parameters": {"token": "secret"}}})
    code = checkout.prepare(v, ITEM, ledger_path=ledger)["confirmation_code"]
    res = checkout.pay(v, PID, code, "12004.00", ledger_path=ledger)
    assert res["status"] == "card_authentication_required"
    assert res["checkout_url"].endswith("after_payment_redirect=true")
    assert "secret" not in json.dumps(_read(ledger))


def test_unknown_outcome_blocks_any_new_payment(hu, ledger) -> None:
    v, fake = hu

    def boom(call):
        raise UnknownOutcomeError("connection reset")

    fake.routes[("POST", f"/purchases/{PID}/checkout/payment")] = boom
    code = checkout.prepare(v, ITEM, ledger_path=ledger)["confirmation_code"]
    assert checkout.pay(v, PID, code, 12004, ledger_path=ledger)["status"] == "payment_outcome_unknown"
    with pytest.raises(checkout.CheckoutError, match="already started"):
        checkout.prepare(v, ITEM, ledger_path=ledger)


def test_a_definite_refusal_allows_preparing_again(hu, ledger) -> None:
    v, fake = hu
    fake.routes[("POST", f"/purchases/{PID}/checkout/payment")] = Response(401, {"code": 100})
    code = checkout.prepare(v, ITEM, ledger_path=ledger)["confirmation_code"]
    assert checkout.pay(v, PID, code, 12004, ledger_path=ledger)["status"] == "payment_refused"
    assert checkout.prepare(v, ITEM, ledger_path=ledger)["ready_to_pay"]


@pytest.mark.parametrize("resp, status", [
    (Response(200, {"payment": {"status": "pending"}}), "card_payment_pending"),
    (Response(200, {"payment": {"status": "failure"}}), "card_payment_failed"),
    (Response(200, {"payment": {"status": "odd"}}), "payment_outcome_unknown"),
    (Response(200, {"payment": {"status": "success"}, "errors": ["x"]}), "payment_outcome_unknown"),
    (Response(200, {"action": {"type": "surprise"}}), "payment_outcome_unknown"),
    (Response(500, {"message": "oops"}), "payment_outcome_unknown"),
    (Response(403, "<html>captcha</html>"), "payment_refused"),
])
def test_classify_payment(resp, status) -> None:
    assert checkout.classify_payment(resp)[0] == status


# --- MCP registration ---------------------------------------------------------------


def _names(allow_writes: bool, allow_checkout: bool) -> set[str]:
    return {t.name for t in asyncio.run(m.build_server(allow_writes, allow_checkout).list_tools())}


def test_checkout_tools_need_their_own_flag() -> None:
    checkout_names = {f.__name__ for f in m._CHECKOUT_TOOLS}
    assert not _names(True, False) & checkout_names
    assert checkout_names <= _names(False, True)
