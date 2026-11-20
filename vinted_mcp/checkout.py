"""Checkout: buy one item, gated by an explicit confirmation of the exact total.

Two steps, never one:

1. :func:`prepare` opens the buyer conversation (its transaction carries an
   accepted offer price, if any), builds Vinted's checkout, applies the
   delivery and payment choice and returns the quote: total, price lines,
   delivery, address, card. It stores a local record with a one-time
   confirmation code. Nothing is paid.
2. :func:`pay` starts the payment at most once per transaction, only with that
   code and the total the user confirmed, after re-reading the quote and
   checking it did not change. A payment that may have started is never sent
   again; the record blocks it.

Endpoints (core host), as the web checkout uses them::

    POST /purchases/checkout/build        {"purchase_items": [{"id": <transaction>, "type": "transaction"}]}
    PUT  /purchases/<purchase>/checkout   {"components": {...}}             -> quote + checksum
    POST /purchases/<purchase>/checkout/payment
                                          {"checksum": ..., "payment_options": {"browser_info": {...}}}

Only a saved, non-expired card is paid automatically. Card authentication
(3-D Secure) stays with the member: the bank app, or the Vinted checkout page
the result links to. Every other payment method gets the checkout link to
finish by hand.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import quote as _q

from . import config as _cfg
from .http import ApiError, Response, UnknownOutcomeError
from .ids import item_id as _item_id

#: A prepared checkout can be paid for this long; after that, prepare again.
CONFIRM_TTL = 15 * 60

#: Records that mean "a payment may exist": no new payment on that transaction.
BLOCKING = frozenset({
    "payment_starting", "payment_outcome_unknown", "card_payment_confirmed", "card_payment_pending",
    "card_authentication_required",
})

_CARD_AUTH_ACTIONS = frozenset({
    "sca_required", "sca_challenge", "sca_blocked", "payrails_cvv_resubmission",
    "native_adyen_card_3ds", "native_adyen_payment_3ds", "redirect",
})

_SHIPPING_TYPES = {"home": 1, "pickup": 2}

LEDGER_PATH = os.path.join(_cfg.CONFIG_DIR, "checkouts.json")


class CheckoutError(ApiError):
    """A checkout step was refused locally or by Vinted; nothing was paid."""


# --- local ledger ---------------------------------------------------------


def _ledger_path(path: Optional[str]) -> str:
    return path or LEDGER_PATH


def load_ledger(path: Optional[str] = None) -> dict[str, dict]:
    try:
        with open(_ledger_path(path), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_ledger(ledger: dict[str, dict], path: Optional[str] = None) -> None:
    p = _ledger_path(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def _set_status(ledger: dict, pid: str, status: str, reason: str, path: Optional[str], **extra: Any) -> dict:
    rec = ledger[pid]
    rec.update(status=status, reason=reason, updated_at=int(time.time()), **extra)
    _save_ledger(ledger, path)
    return rec


def _blocking_record(ledger: dict, transaction_id: int) -> Optional[dict]:
    for rec in ledger.values():
        if rec.get("transaction_id") == transaction_id and rec.get("status") in BLOCKING:
            return rec
    return None


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


# --- quote parsing ----------------------------------------------------------


def _get(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _find_key(d: Any, key: str, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(d, dict):
        if d.get(key) not in (None, ""):
            return d[key]
        for v in d.values():
            r = _find_key(v, key, depth + 1)
            if r not in (None, ""):
                return r
    elif isinstance(d, list):
        for v in d:
            r = _find_key(v, key, depth + 1)
            if r not in (None, ""):
                return r
    return None


def money(m: Any) -> Optional[dict]:
    """``{"amount": "12004.0", "currency_code": "HUF"}`` -> ``{"amount": "12004.00", "currency": "HUF"}``."""
    if not isinstance(m, dict) or m.get("amount") in (None, ""):
        return None
    try:
        amount = Decimal(str(m["amount"])).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return {"amount": str(amount), "currency": m.get("currency_code") or m.get("currency")}


def _price_lines(summary: Any, depth: int = 0) -> list[dict]:
    """Labelled amounts from ``order_summary_v2`` (item, shipping, buyer protection, deductions...)."""
    out: list[dict] = []
    if depth > 5:
        return out
    if isinstance(summary, dict):
        price = money(summary.get("price"))
        label = next((summary.get(k) for k in ("title", "label", "name", "description", "type")
                      if isinstance(summary.get(k), str) and summary.get(k)), None)
        if price and label:
            out.append({"label": label, **price})
        for k, v in summary.items():
            if k != "price" and isinstance(v, (dict, list)):
                out += _price_lines(v, depth + 1)
    elif isinstance(summary, list):
        for v in summary:
            out += _price_lines(v, depth + 1)
    return out


def _card_label(card: Any) -> Optional[str]:
    if not isinstance(card, dict):
        return None
    inner = card.get("credit_card") if isinstance(card.get("credit_card"), dict) else card
    brand = next((inner.get(k) for k in ("brand", "card_brand", "type", "card_type") if isinstance(inner.get(k), str)), "")
    last4 = next((str(inner.get(k))[-4:] for k in ("last_four", "last4", "last_four_digits", "card_number_last4",
                                                    "number_last4", "masked_number", "card_number", "masked_pan")
                  if inner.get(k)), "")
    label = " ".join(x for x in (brand, f"****{last4}" if last4 else "") if x)
    return label or None


def _card_id(card: Any) -> Any:
    """The saved card a selected payment method points at (a Vinted reference, never a card number)."""
    if not isinstance(card, dict):
        return None
    if card.get("expired") is True or _get(card, "credit_card", "expired") is True:
        return None
    for path in (("card_id",), ("card", "id"), ("credit_card", "external_code")):
        v = _get(card, *path)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v > 0 and float(v).is_integer():
            return int(v)
        if isinstance(v, str) and v not in ("", "0") and len(v) <= 128:
            return v
    return None


def _provider(method: Any) -> str:
    """``card``/``paypal``/``wallet``/... from a pay-in method, as Vinted names it."""
    if not isinstance(method, dict):
        return ""
    native = str(method.get("payment_method") or "").lower()
    if native == "credit_card":
        return "card"
    if native:
        return native
    code = "_" + str(method.get("code") or "").upper() + "_"
    for name in ("PAYPAL", "GOOGLE_PAY", "APPLE_PAY", "KLARNA", "BLIK", "WALLET", "CARD"):
        if f"_{name}_" in code:
            return name.lower()
    return ""


def parse_quote(raw: Any, *, domain: str, transaction_id: Optional[int] = None) -> dict:
    """Summarise a checkout build/update answer for a human and for :func:`pay`'s checks."""
    raw = raw if isinstance(raw, dict) else {}
    comp = _get(raw, "checkout", "components") or {}
    pid = (_get(raw, "purchase", "id") or _get(raw, "checkout", "purchase_id") or raw.get("purchase_id")
           or _find_key(raw, "purchase_id"))
    checksum = raw.get("checksum") or _get(raw, "checkout", "checksum") or _find_key(raw, "checksum")
    pay_button = comp.get("pay_button_v2") or {}
    total = money(_get(pay_button, "total", "price"))
    summary = comp.get("order_summary_v2")

    payment = comp.get("payment_method") or {}
    selected = payment.get("selected_payment_method") if isinstance(payment.get("selected_payment_method"), dict) else None
    selected_provider = _provider(_get(selected, "pay_in_method")) if selected else ""
    selected_card_id = _card_id(selected) if selected and selected_provider == "card" else None
    methods = []
    for m in payment.get("pay_in_methods") or []:
        if isinstance(m, dict) and m.get("enabled") is not False and not m.get("read_only"):
            prov = _provider(m)
            if prov:
                methods.append({"provider": prov, "payment_method": m.get("payment_method") or prov})
    cards = []
    for c in payment.get("cards") or []:
        if isinstance(c, dict):
            cards.append({"id": c.get("id"), "label": _card_label(c), "expired": bool(c.get("expired"))})

    pickup_type = _get(comp, "shipping_pickup_options", "selected_pickup_option")
    details = _get(comp, "shipping_pickup_details", "pickup_details") or {}
    point = details.get("shipping_point") if isinstance(details.get("shipping_point"), dict) else None
    address = _get(comp, "shipping_address", "address")
    contact = comp.get("shipping_contact") or {}

    delivery: dict[str, Any] = {"type": {1: "home", 2: "pickup"}.get(pickup_type, pickup_type)}
    if point:
        delivery["point"] = {k: point.get(k) for k in ("name", "title", "address", "city", "postal_code", "code")
                             if point.get(k) not in (None, "")}
    option = details.get("shipping_option")
    if isinstance(option, dict):
        delivery["option"] = {k: option.get(k) for k in ("title", "name", "carrier", "carrier_name", "delivery_time")
                              if option.get(k) not in (None, "")}
        if money(option.get("price")):
            delivery["option"]["price"] = money(option.get("price"))

    addr_out = None
    if isinstance(address, dict):
        addr_out = {k: address.get(k) for k in ("name", "line1", "line2", "street", "house_number", "postal_code",
                                                 "city", "country", "country_title", "is_complete")
                    if address.get(k) not in (None, "")}

    phone_needed = contact.get("is_receiver_phone_number_required") is True
    phone_ok = not phone_needed or bool(str(contact.get("phone_number") or "").strip())

    problems = []
    if not pid:
        problems.append("Vinted returned no purchase id")
    if not checksum:
        problems.append("Vinted returned no checksum")
    if not total:
        problems.append("no total in the quote")
    if pay_button.get("payments_available") is not True:
        problems.append("Vinted says payment is not available yet (delivery, address or payment method missing)")
    if not selected:
        problems.append("no payment method selected")
    if delivery["type"] == "home" and not (isinstance(address, dict) and address.get("id") and address.get("is_complete") is not False):
        problems.append("the delivery address is missing or incomplete")
    if delivery["type"] == "pickup" and not (point and details.get("selected_rate_uuid")):
        problems.append("no pickup point selected")
    if not phone_ok:
        problems.append("a phone number is required for this delivery")

    url = raw.get("checkout_url") or _get(raw, "checkout", "url")
    if not (isinstance(url, str) and url.startswith(f"https://www.vinted.{domain}/checkout")):
        url = None
    if not url and pid:
        url = (f"https://www.vinted.{domain}/checkout?purchase_id={_q(str(pid))}"
               + (f"&order_id={int(transaction_id)}&order_type=transaction" if transaction_id else ""))

    conversion = summary.get("currency_conversion") if isinstance(summary, dict) else None
    return {
        "purchase_id": str(pid) if pid else None,
        "checksum": checksum,
        "total": total,
        "lines": _price_lines(summary),
        "currency_conversion": conversion if conversion else None,
        "delivery": delivery,
        "address": addr_out,
        "payment": {
            "selected": selected_provider or None,
            "card": _card_label(selected) if selected_provider == "card" else None,
            "card_id": selected_card_id,
            "available": methods,
            "cards": cards,
        },
        "card_signals_required": bool(_get(raw, "checkout", "adyen_protect_signals_enabled") or raw.get("adyen_protect_signals_enabled")),
        "point_ref": ({"rate_uuid": point.get("rate_uuid"), "point_code": point.get("code"), "point_uuid": point.get("uuid")}
                      if point and point.get("rate_uuid") and point.get("code") and point.get("uuid") else None),
        "ready_to_pay": not problems,
        "problems": problems,
        "checkout_url": url,
    }


def _payment_choice(quote: dict, payment: str) -> Optional[dict]:
    """The ``payment_method`` component that selects ``payment`` (a card needs a saved, valid card)."""
    p = quote["payment"]
    natives = [m["payment_method"] for m in p["available"] if m["provider"] == payment]
    if not natives:
        return None
    if payment != "card":
        return {"card_id": None, "payment_method": natives[0]}
    card_id = p.get("card_id")
    if card_id is None:
        valid = [c for c in p["cards"] if not c["expired"] and c.get("id")]
        if len(valid) == 1:
            card_id = valid[0]["id"]
    if card_id is None:
        return None
    return {"card_id": card_id, "payment_method": natives[0]}


def _components(shipping: str, payment_choice: Optional[dict], point_ref: Optional[dict]) -> dict:
    comp: dict[str, Any] = {
        "additional_service": {}, "payment_method": {}, "shipping_address": {},
        "shipping_pickup_options": {}, "shipping_pickup_details": {},
    }
    if shipping in _SHIPPING_TYPES:
        comp["shipping_pickup_options"] = {"pickup_type": _SHIPPING_TYPES[shipping]}
    if shipping == "pickup" and point_ref:
        comp["shipping_pickup_details"] = dict(point_ref)
    if payment_choice:
        comp["payment_method"] = payment_choice
    return comp


# --- Vinted calls -----------------------------------------------------------


def _referer(v: Any, pid: str, tid: int) -> dict:
    return {"Referer": f"{v.web}/checkout?purchase_id={_q(pid)}&order_id={int(tid)}&order_type=transaction"}


def _build(v: Any, iid: int, tid: int) -> Response:
    return v.send("POST", "core", "/purchases/checkout/build",
                  {"purchase_items": [{"id": int(tid), "type": "transaction"}]},
                  headers={"Referer": f"{v.web}/items/{iid}"})


def _update(v: Any, pid: str, tid: int, components: dict) -> Response:
    return v.send("PUT", "core", f"/purchases/{_q(pid, safe='')}/checkout", {"components": components},
                  headers=_referer(v, pid, tid))


def _browser_info(v: Any) -> dict:
    return {"language": _cfg.locale(v.domain), "color_depth": 32, "java_enabled": False,
            "screen_height": 1080, "screen_width": 1920, "timezone_offset": -60}


def _check(r: Response, step: str) -> dict:
    if not r.ok or not isinstance(r.data, dict):
        raise CheckoutError(f"{step}: {r.error_reason or 'unexpected answer'} (nothing was paid)")
    return r.data


# --- public API -------------------------------------------------------------


def prepare(v: Any, item: str | int, *, shipping: str = "auto", payment: str = "card",
            ledger_path: Optional[str] = None) -> dict:
    """Build the checkout for ``item`` and return the quote plus a one-time confirmation code. Never pays."""
    v._require_login()
    if shipping not in ("auto", "home", "pickup"):
        raise ValueError("shipping must be auto, home or pickup")
    iid = _item_id(item)
    details = v.item(iid)
    if details.get("can_buy") is False or details.get("is_reserved") or details.get("is_hidden"):
        raise CheckoutError(f"item {iid} cannot be bought right now (sold, reserved or hidden)")
    conv = v.start_conversation(iid, seller_id=details.get("seller_id"))
    tid = conv.data.get("transaction_id") if conv.ok and isinstance(conv.data, dict) else None
    if not tid:
        raise CheckoutError(f"could not open the transaction for item {iid}: {conv.error_reason or 'no transaction id'}")
    tid = int(tid)

    ledger = load_ledger(ledger_path)
    blocking = _blocking_record(ledger, tid)
    if blocking:
        raise CheckoutError(
            f"a payment for this item was already started (purchase {blocking.get('purchase_id')}, status "
            f"{blocking.get('status')}). Check it in Vinted; no second payment will be started.")

    quote = parse_quote(_check(_build(v, iid, tid), "checkout build"), domain=v.domain, transaction_id=tid)
    pid = quote["purchase_id"]
    if not pid:
        raise CheckoutError("checkout build returned no purchase id (nothing was paid)")

    choice = _payment_choice(quote, payment)
    comp = _components(shipping, choice, quote["point_ref"] if shipping == "pickup" else None)
    quote = parse_quote(_check(_update(v, pid, tid, comp), "checkout update"), domain=v.domain, transaction_id=tid)
    # Switching to pickup can reveal Vinted's saved point, and a first update can
    # reveal the saved card: select exactly what this checkout offers, once more.
    again = False
    if shipping == "pickup" and not comp["shipping_pickup_details"] and quote["point_ref"]:
        comp["shipping_pickup_details"] = dict(quote["point_ref"])
        again = True
    if not choice:
        choice = _payment_choice(quote, payment)
        if choice:
            comp["payment_method"] = choice
            again = True
    if again:
        quote = parse_quote(_check(_update(v, pid, tid, comp), "checkout update"), domain=v.domain, transaction_id=tid)
    quote["purchase_id"] = quote["purchase_id"] or pid

    problems = list(quote["problems"])
    if payment == "card":
        if quote["payment"]["selected"] != "card" or quote["payment"]["card_id"] is None:
            problems.append("no saved, valid card is selected; only a saved card can be paid automatically")
        if quote["card_signals_required"]:
            problems.append("Vinted asks for device signals for this card payment; finish it in the Vinted checkout")
    else:
        problems.append(f"automatic payment supports a saved card only; finish {payment} in the Vinted checkout")

    for rec in ledger.values():  # an older unpaid quote for the same transaction is replaced
        if rec.get("transaction_id") == tid and rec.get("status") == "prepared":
            rec.update(status="superseded", reason="a newer checkout was prepared", updated_at=int(time.time()))

    code = None
    if not problems:
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    now = int(time.time())
    ledger[pid] = {
        "purchase_id": pid, "transaction_id": tid, "item_id": iid, "title": details.get("title"),
        "total": quote["total"], "payment": payment, "shipping": shipping, "components": comp,
        "code_hash": _code_hash(code) if code else None, "created_at": now, "updated_at": now,
        "status": "prepared" if code else "review_required",
        "reason": "waiting for the member's confirmation" if code else "; ".join(problems),
        "checkout_url": quote["checkout_url"],
    }
    _save_ledger(ledger, ledger_path)

    out = {k: quote[k] for k in ("purchase_id", "total", "lines", "currency_conversion", "delivery", "address",
                                 "checkout_url")}
    out.update(transaction_id=tid, item={"id": iid, "title": details.get("title")},
               payment={k: quote["payment"][k] for k in ("selected", "card", "available")},
               problems=problems, ready_to_pay=not problems)
    if code:
        out["confirmation_code"] = code
        out["expires_in_s"] = CONFIRM_TTL
        out["next_step"] = ("Nothing is paid yet. Read the total, delivery, address and card to the member and ask "
                            "for explicit confirmation of this purchase. Only then call checkout_pay with this "
                            "purchase_id, the confirmation_code and the confirmed total.")
    else:
        out["next_step"] = "Automatic payment is not possible for this checkout; the member can finish it at checkout_url."
    return out


def pay(v: Any, purchase_id: str, confirmation_code: str, confirmed_total: float | str, *,
        ledger_path: Optional[str] = None) -> dict:
    """Start the payment of a prepared checkout once, if the code and the confirmed total still match."""
    v._require_login()
    pid = str(purchase_id)
    ledger = load_ledger(ledger_path)
    rec = ledger.get(pid)
    if not rec:
        raise CheckoutError(f"unknown purchase {pid}: run checkout prepare first")
    if rec.get("status") != "prepared":
        raise CheckoutError(f"purchase {pid} is {rec.get('status')} ({rec.get('reason')}); no payment was started")
    if not rec.get("code_hash") or not hmac.compare_digest(rec["code_hash"], _code_hash(str(confirmation_code))):
        raise CheckoutError("wrong confirmation code; no payment was started")
    if time.time() - rec.get("created_at", 0) > CONFIRM_TTL:
        _set_status(ledger, pid, "expired", "the confirmation expired; prepare again", ledger_path)
        raise CheckoutError("the confirmation expired; prepare the checkout again (no payment was started)")
    try:
        confirmed = Decimal(str(confirmed_total)).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise CheckoutError("confirmed_total is not a number; no payment was started")
    if not rec.get("total") or Decimal(rec["total"]["amount"]) != confirmed:
        raise CheckoutError(f"the confirmed total {confirmed} differs from the prepared total "
                            f"{(rec.get('total') or {}).get('amount')}; no payment was started")
    tid = int(rec["transaction_id"])
    other = _blocking_record(ledger, tid)
    if other:
        raise CheckoutError(f"a payment for this transaction already exists (purchase {other.get('purchase_id')}, "
                            f"{other.get('status')}); no second payment")

    # Fresh quote with the same choices: the payment uses its checksum, and it must
    # still show exactly what the member confirmed.
    quote = parse_quote(_check(_update(v, pid, tid, rec["components"]), "checkout refresh"),
                        domain=v.domain, transaction_id=tid)
    changed = []
    if not quote["total"] or quote["total"] != rec["total"]:
        changed.append(f"total is now {quote['total']}")
    if not quote["ready_to_pay"]:
        changed += quote["problems"]
    if quote["payment"]["selected"] != "card" or quote["payment"]["card_id"] is None:
        changed.append("the saved card is no longer selected")
    if quote["card_signals_required"]:
        changed.append("Vinted asks for device signals; finish in the Vinted checkout")
    if changed:
        _set_status(ledger, pid, "review_required", "; ".join(changed), ledger_path)
        raise CheckoutError("the checkout changed since it was confirmed: " + "; ".join(changed)
                            + ". No payment was started; prepare it again.")

    # Make a 401 on the payment itself unlikely: it is not replayed.
    v.get("core", "/users/current")
    _set_status(ledger, pid, "payment_starting", "a payment request is being sent; do not start it again", ledger_path)
    body = {"checksum": quote["checksum"], "payment_options": {"browser_info": _browser_info(v)}}
    try:
        r = v.send("POST", "core", f"/purchases/{_q(pid, safe='')}/checkout/payment", body,
                   headers=_referer(v, pid, tid), retry_auth=False)
    except UnknownOutcomeError:
        rec = _set_status(ledger, pid, "payment_outcome_unknown",
                          "the connection failed while paying; Vinted may have taken the payment. Check in Vinted, "
                          "do not pay again.", ledger_path)
        return _result(rec)

    status, reason = classify_payment(r)
    extra: dict[str, Any] = {}
    if status == "card_authentication_required":
        url = quote["checkout_url"] or rec["checkout_url"]
        extra["checkout_url"] = url + ("&" if "?" in url else "?") + "after_payment_redirect=true"
    rec = _set_status(ledger, pid, status, reason, ledger_path, **extra)
    return _result(rec)


def classify_payment(r: Response) -> tuple[str, str]:
    """Map Vinted's answer to the payment request onto a status. Anything unclear is ``payment_outcome_unknown``."""
    if r.status in (401, 429) or r.blocked:
        return ("payment_refused", (f"Vinted refused the payment request (HTTP {r.status}) before processing it; "
                                    "nothing was paid. Prepare the checkout again to retry."))
    if not 200 <= r.status < 300 or not isinstance(r.data, dict):
        return ("payment_outcome_unknown", (f"Vinted answered HTTP {r.status}; the payment may be processing. "
                                            "Check in Vinted, do not pay again."))
    d = r.data
    if d.get("errors") or d.get("error"):
        return ("payment_outcome_unknown", "Vinted answered with errors; check the order in Vinted, do not pay again.")
    action = _get(d, "action", "type")
    if d.get("action") is not None:
        if action in _CARD_AUTH_ACTIONS:
            return ("card_authentication_required",
                    ("The payment was started and the bank asks for confirmation (3-D Secure). Approve it in the "
                     "bank app, or open checkout_url to finish. Do not pay again."))
        return ("payment_outcome_unknown", "Vinted asked for an unknown next step; check in Vinted, do not pay again.")
    st = _get(d, "payment", "status")
    if st == "success":
        return ("card_payment_confirmed", "Vinted reports the card payment succeeded. The order is in your purchases.")
    if st in ("pending", "preparing"):
        return ("card_payment_pending", "The card payment is processing. Check it in Vinted; do not pay again.")
    if st == "failure":
        return ("card_payment_failed", "Vinted reports the card payment failed; nothing was charged. No retry was made.")
    return ("payment_outcome_unknown", "Vinted's answer did not say how the payment went; check in Vinted, do not pay again.")


def _result(rec: dict) -> dict:
    return {k: rec.get(k) for k in ("purchase_id", "transaction_id", "item_id", "title", "total", "status", "reason",
                                    "checkout_url")}


def status(purchase_id: Optional[str] = None, *, ledger_path: Optional[str] = None, limit: int = 10) -> list[dict]:
    """Locally recorded checkouts, newest first (no codes, no payment details)."""
    recs = sorted(load_ledger(ledger_path).values(), key=lambda r: r.get("updated_at", 0), reverse=True)
    if purchase_id:
        recs = [r for r in recs if r.get("purchase_id") == str(purchase_id)]
    return [_result(r) | {"updated_at": r.get("updated_at")} for r in recs[:limit]]
