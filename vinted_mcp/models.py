"""Compact, JSON-friendly views of Vinted payloads."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class Item:
    """A listing as returned by search, wardrobes and favourites."""

    id: int
    title: str
    price: Optional[str] = None
    currency: Optional[str] = None
    total_price: Optional[str] = None
    brand: Optional[str] = None
    condition: Optional[str] = None
    size: Optional[str] = None
    url: Optional[str] = None
    photo: Optional[str] = None
    seller_id: Optional[int] = None
    seller_login: Optional[str] = None
    favourite_count: Optional[int] = None
    view_count: Optional[int] = None
    status: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, {}, "")}


def _money(m: Any) -> tuple[Optional[str], Optional[str]]:
    if isinstance(m, dict):
        return m.get("amount"), m.get("currency_code")
    if m is None:
        return None, None
    return str(m), None


def _photo_url(it: dict) -> Optional[str]:
    p = it.get("photo") or (it.get("photos") or [None])[0]
    if not isinstance(p, dict):
        return None
    return p.get("url") or p.get("full_size_url")


def item_from_api(it: dict, web_base: str = "") -> Item:
    """Map a catalogue / wardrobe / favourites item dict to :class:`Item`."""
    price, cur = _money(it.get("price"))
    if cur is None:
        cur = it.get("currency")
    total, _ = _money(it.get("total_item_price"))
    box = it.get("item_box") or {}
    user = it.get("user") or {}
    url = it.get("url") or it.get("path")
    if url and url.startswith("/"):
        url = web_base + url
    status_flags = {
        k: it[k]
        for k in ("is_reserved", "is_hidden", "is_closed", "is_draft", "is_processing", "item_closing_action",
                  "item_alert_type")
        if it.get(k) not in (None, False)
    }
    cond = it.get("status")
    if not isinstance(cond, str):
        cond = None
    second = box.get("second_line") or ""
    size = it.get("size_title") or it.get("size")
    if not cond and second:
        # "M · Muy bueno" or just "Muy bueno"
        parts = [p.strip() for p in second.split("·")]
        cond = parts[-1]
        if len(parts) > 1 and not size:
            size = parts[0]
    return Item(
        id=int(it.get("id") or it.get("item_id")),
        title=it.get("title") or "",
        price=price,
        currency=cur,
        total_price=total,
        # In search results item_box.first_line is the brand; in wardrobes it is a "N views" counter.
        brand=it.get("brand_title") or (it.get("brand") if isinstance(it.get("brand"), str) else None)
        or (box.get("first_line") if it.get("content_source") == "search" else None),
        condition=cond,
        size=size if isinstance(size, str) else None,
        url=url,
        photo=_photo_url(it),
        seller_id=user.get("id"),
        seller_login=user.get("login"),
        favourite_count=it.get("favourite_count"),
        view_count=it.get("view_count"),
        status=status_flags,
    )


# --- item page (RSC payload) ------------------------------------------------

_PUSH = re.compile(r"self\.__next_f\.push\(\[1,(\".*?\")\]\)</script>", re.S)


def rsc_payload(html: str) -> str:
    """Concatenate the Next.js flight chunks embedded in a page."""
    out = []
    for m in _PUSH.finditer(html):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return "".join(out)


def extract_plugins(html: str) -> list[dict]:
    """Return the item page's ``plugins`` list (summary, attributes, description, seller...)."""
    rsc = rsc_payload(html)
    dec = json.JSONDecoder()
    best: list[dict] = []
    for m in re.finditer(r'\{"plugins":\s*\[', rsc):
        i = m.start()
        try:
            obj, _ = dec.raw_decode(rsc, i)
        except ValueError:
            continue
        plugins = obj.get("plugins") or []
        if len(plugins) > len(best):
            best = plugins
    return best


def extract_ld_json(html: str) -> dict:
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def item_details_from_page(html: str, item_id: int) -> dict:
    """Flatten an item page into a details dict (title, price, attributes, seller...)."""
    plugins = extract_plugins(html)
    ld = extract_ld_json(html)
    out: dict[str, Any] = {"id": item_id}
    offers = ld.get("offers") or {}
    if ld:
        out.update({
            "title": ld.get("name"),
            "description": ld.get("description"),
            "price": str(offers.get("price")) if offers.get("price") is not None else None,
            "currency": offers.get("priceCurrency"),
            "availability": offers.get("availability"),
            "url": offers.get("url"),
            "brand": (ld.get("brand") or {}).get("name"),
            "category": ld.get("category"),
        })
    attrs: dict[str, Any] = {}
    for p in plugins:
        name = p.get("name")
        data = p.get("data") or {}
        if name == "attributes":
            for a in data.get("attributes") or []:
                d = a.get("data") or {}
                attrs[a.get("code") or d.get("title")] = d.get("value")
        elif name == "description":
            out["description"] = data.get("description") or out.get("description")
            if data.get("translated_description"):
                out["translated_description"] = data["translated_description"]
        elif name == "user_info_header":
            out["seller"] = {
                k: data.get(k)
                for k in ("name", "feedback_count", "feedback_reputation", "business", "badges", "last_seen", "location")
                if data.get(k) not in (None, [], "")
            }
        elif name in ("ask_seller", "buy", "make_offer"):
            for k in ("can_buy", "instant_buy", "is_hidden", "is_reserved", "seller_id", "reservation", "offer_price"):
                if k in data and k not in out:
                    out[k] = data[k]
            if data.get("photos") and "photos" not in out:
                out["photos"] = [
                    ph.get("url") or ph.get("full_size_url") or next(
                        (t.get("url") for t in ph.get("thumbnails") or [] if t.get("url")), None)
                    for ph in data["photos"]
                ]
        elif name == "high_demand_signal_info" and data.get("title"):
            out["signal"] = data["title"]
        elif name == "summary":
            out["summary"] = [
                " ".join(str(e.get("value", "")) for e in line.get("elements", []) if e.get("value"))
                for line in data.get("lines") or []
            ]
    if attrs:
        out["attributes"] = attrs
    out["plugins_seen"] = [p.get("name") for p in plugins]
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


# --- offers -----------------------------------------------------------------

#: ``offer_request`` status codes seen in conversations.
OFFER_STATUS = {10: "pending", 20: "accepted", 30: "rejected", 40: "expired", 50: "canceled"}


def offer_events(conversation: dict) -> list[dict]:
    """Offer history of a conversation: buyer requests and seller offers, oldest first.

    ``kind`` is ``"buyer_offer"`` (an ``offer_request``; the seller answers it
    with ``respond_offer``) or ``"seller_offer"`` (a price the seller proposed;
    for the buyer, accepting it means checking out, which is done by hand).
    """
    events: list[dict] = []
    for m in conversation.get("messages") or []:
        data = m.get("data") or {}
        ent = data.get("entity") or {}
        etype = data.get("entity_type")
        if etype not in ("offer_request_message", "offer_message"):
            continue
        price = ent.get("price") or {}
        ev = {
            "kind": "buyer_offer" if etype == "offer_request_message" else "seller_offer",
            "id": ent.get("offer_request_id") or data.get("id"),
            "price": price.get("amount"),
            "currency": price.get("currency_code") or ent.get("currency"),
            "original_price": (ent.get("original_price") or {}).get("amount"),
            "by_user_id": ent.get("user_id"),
            "current": ent.get("current"),
            "created_at": data.get("created_at_ts"),
            "transaction_id": ent.get("transaction_id"),
        }
        if etype == "offer_request_message":
            ev["status"] = OFFER_STATUS.get(ent.get("status"), ent.get("status"))
            ev["status_title"] = ent.get("status_title")
        events.append({k: v for k, v in ev.items() if v is not None})
    return events
