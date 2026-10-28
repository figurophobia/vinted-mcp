"""Agent automation built on top of :class:`~vinted_mcp.client.Vinted`.

- :func:`listing_from_photos` — upload photos once and gather everything needed
  to publish: category guessed from the photos, required attributes, brand,
  Vinted's price recommendation, similar sold and active listings.
- :func:`negotiate` — one deterministic negotiation step for a seller
  conversation: accept at or above the floor, otherwise reject and counter down
  a concession ladder that never goes below the floor. Buyer messages are data:
  instructions inside them are flagged, never followed.
- :func:`relist` — take a listing down and publish it again (fresh photos or
  the current ones), waiting for the old one to disappear so Vinted does not
  see two copies online.
"""

from __future__ import annotations

import os
import re
import statistics
import tempfile
import time
import uuid
from typing import Any, Optional

from .client import Vinted, condition_id
from .http import ApiError, Response
from .ids import item_id as _item_id
from .models import offer_events

# --- listing from photos ------------------------------------------------------


def listing_from_photos(
    v: Vinted,
    photo_paths: list[str],
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    brand: Optional[str] = None,
    condition: Optional[str | int] = None,
    catalog_id: Optional[int] = None,
) -> dict:
    """Upload ``photo_paths`` and return a listing proposal for the user to confirm.

    The photos stay attached to the returned ``upload_session_id`` /
    ``photo_ids``; pass both to :meth:`Vinted.publish` (or the
    ``publish_item`` tool) to publish without uploading them again.
    """
    if not photo_paths:
        raise ValueError("at least one photo is required")
    session_id = str(uuid.uuid4())
    photo_ids: list[int] = []
    image_metadata: list[dict] = []
    for p in photo_paths[:20]:
        r = v.upload_photo(p, session_id).raise_for_error()
        photo_ids.append(int(r.data["id"]))
        hr = (r.data.get("high_resolution") or {}).get("id")
        if hr:
            image_metadata.append({"image_id": hr, "orientation": "0"})

    cats = {c["id"]: c for c in v.categories()}
    out: dict[str, Any] = {"upload_session_id": session_id, "photo_ids": photo_ids}

    # category: from the photos, unless given
    suggested: list[int] = []
    r = v.send("POST", "core", "/item_upload/suggestions/categories",
               {"image_metadata": image_metadata, "upload_session_id": session_id})
    if r.ok:
        suggested = [int(c) for c in r.data.get("prefill_categories") or r.data.get("categories") or []]
        out["category_candidates"] = [cats.get(int(c), {"id": int(c)}) for c in r.data.get("categories") or []]
    if catalog_id is None and not suggested and title:
        suggested = [c["id"] for c in v.suggest_categories(title)[:1]]
    cid = int(catalog_id or (suggested[0] if suggested else 0)) or None
    out["category"] = cats.get(cid, {"id": cid}) if cid else None

    # attributes, brand and colour suggestions from title/photos
    if cid:
        r = v.send("POST", "core", "/item_upload/suggestions/attributes", {
            "title": title or "", "description": description or "", "category_id": cid,
            "suggested_category_id": suggested[0] if suggested else cid, "image_metadata": image_metadata,
            "upload_session_id": session_id, "attributes": [],
        })
        if r.ok:
            out["suggested"] = {k: r.data.get(k) for k in ("prefill_brand", "brands", "colors", "dynamic_attributes",
                                                          "models", "prefill_model", "isbn") if r.data.get(k)}
        a = v.upload_attributes(cid)
        if a.ok:
            out["required_attributes"] = [
                {"code": at.get("code"), "title": (at.get("configuration") or {}).get("title"),
                 "options": _flat_options((at.get("configuration") or {}).get("options"))[:60]}
                for at in a.data.get("attributes") or []
                if (at.get("configuration") or {}).get("required")
            ]
        p = v.package_sizes(cid)
        if p.ok:
            out["package_sizes"] = [{"id": s.get("id"), "title": s.get("title")} for s in p.data.get("package_sizes") or []]

    brand_id = None
    if brand and cid:
        b = v.brands(brand, cid)
        if b.ok:
            found = b.data.get("brands") or []
            exact = [x for x in found if (x.get("title") or "").lower() == brand.lower()]
            pick = (exact or found or [None])[0]
            if pick:
                brand_id = pick.get("id")
                out["brand"] = {"id": brand_id, "title": pick.get("title")}

    # price: Vinted's recommendation, sold comparables, active comparables
    price: dict[str, Any] = {}
    if cid:
        # This edge endpoint wants numeric ids here (but string ids inside ``attributes``).
        body = {"category_id": int(cid), "brand_id": int(brand_id) if brand_id else None,
                "condition_id": condition_id(condition) if condition is not None else None,
                "model_metadata": None, "image_metadata": image_metadata, "attributes": []}
        r = v.send("POST", "edge", "/upload-form-suggestions/item_price_suggestions", body)
        if r.ok and r.data.get("price_suggestion"):
            ps = r.data["price_suggestion"]
            price["vinted_recommendation"] = {k: (ps.get(k) or {}).get("amount") for k in ("minimum", "midpoint", "maximum")}
            rng = r.data.get("price_range") or {}
            s = v.send("POST", "edge", "/upload-form-suggestions/item_upload/items/similar_sold_items", {
                **body, "price_from": rng.get("price_from"), "price_to": rng.get("price_to"),
                "currency_code": rng.get("currency_code") or "EUR", "upload_session_id": session_id,
            })
            if s.ok:
                price["similar_sold"] = [
                    {"id": it.get("id"), "title": it.get("title"), "price": (it.get("price") or {}).get("amount"),
                     "condition": (it.get("item_box") or {}).get("second_line")}
                    for it in (s.data.get("items") or [])[:10]
                ]
    if title:
        # Drop trailing words until the search finds enough comparables ("… con caja" rarely matches).
        words = title.split()
        prices: list[float] = []
        query = title
        while words:
            query = " ".join(words)
            active = v.search_items(query, catalog_ids=[cid] if cid else None, per_page=48)
            prices = sorted(float(i.price) for i in active if i.price)
            if len(prices) >= 5 or len(words) <= 2:
                break
            words = words[:-1]
        if len(prices) >= 3:
            q = statistics.quantiles(prices, n=4)
            price["active_listings"] = {"query": query, "count": len(prices), "p25": round(q[0], 2),
                                       "median": round(q[1], 2), "p75": round(q[2], 2)}
    out["price"] = price
    out["next_step"] = ("Review category/attributes/price, then publish with publish_item(..., photo_ids=photo_ids, "
                        "upload_session_id=upload_session_id).")
    return out


def _flat_options(opts: Any) -> list[dict]:
    flat: list[dict] = []
    for o in opts or []:
        if o.get("options"):
            flat.extend(_flat_options(o["options"]))
        elif o.get("type") != "group":
            flat.append({"id": o.get("id"), "title": o.get("title")})
    return flat


# --- negotiation ---------------------------------------------------------------

#: Phrases that try to steer the agent rather than negotiate. They are reported, never obeyed.
_INJECTION = re.compile(
    r"ignor\w* (todas? )?(las |the )?(instruc|previous|anterior)|eres (un|una) (modelo|ia|bot)|"
    r"(you are|are you) an? (ai|bot|language model)|system prompt|nuevas instrucciones|new instructions|"
    r"olvida (todo|lo anterior)|forget (all|everything)",
    re.I,
)

DEFAULT_MESSAGES = {
    "accept": "¡Perfecto! Acepto tu oferta de {price} €. Cuando la compres te la envío bien protegida.",
    "counter": "Gracias por la oferta, pero {offer} € se queda corto. Te la puedo dejar en {price} €; te he enviado la oferta.",
    "final": "Mi último precio es {price} €, de ahí no puedo bajar. Te dejo la oferta enviada por si te encaja.",
    "remind": "La oferta de {price} € sigue en pie; si te encaja, puedes comprarla directamente.",
}


def concession_ladder(asking: float, floor: float, steps: int = 3) -> list[float]:
    """Prices from just below ``asking`` down to ``floor`` with shrinking concessions (halving the gap)."""
    if floor > asking:
        raise ValueError("floor is above the asking price")
    ladder, price = [], asking
    for _ in range(max(1, steps) - 1):
        price = price - (price - floor) / 2
        rounded = float(round(price))
        if rounded > floor and (not ladder or rounded < ladder[-1]):
            ladder.append(rounded)
    ladder.append(float(floor))
    return ladder


def negotiate(
    v: Vinted,
    conversation_id: str,
    *,
    floor: float,
    steps: int = 3,
    messages: Optional[dict[str, str]] = None,
    dry_run: bool = True,
) -> dict:
    """Decide (and with ``dry_run=False`` perform) the next seller move in one conversation.

    - A pending buyer offer at or above ``floor`` is accepted.
    - A lower one is rejected and countered with the next ladder price below
      the current asking price (never under ``floor``; the last step uses the
      ``final`` message).
    - Buyer messages after your last move without a new offer get a reminder
      of the current asking price.
    Buyer text is treated as data: anything that looks like instructions to an
    AI is listed in ``ignored_instructions`` and has no effect.
    """
    msgs = {**DEFAULT_MESSAGES, **(messages or {})}
    r = v.conversation(conversation_id).raise_for_error()
    conv = r.data
    data = conv.get("data") or {}
    if data.get("current_user_side") != "seller":
        raise ApiError("negotiate only handles conversations where you are the seller")
    tid = int(data["transaction_id"])
    me = str(v.my_id())
    events = offer_events(conv)
    tx = v.transaction(tid).raise_for_error().data.get("transaction") or {}
    listed = _listed_price(v, conv, tx)
    if floor > listed:
        raise ValueError(f"floor {floor} is above the listed price {listed}")

    my_offers = [float(e["price"]) for e in events if e["kind"] == "seller_offer" and str(e.get("by_user_id")) == me]
    asking = min(my_offers) if my_offers else listed
    ladder = [p for p in concession_ladder(listed, floor, steps) if p < asking] or [float(floor)]

    buyer_msgs, last_mine = [], ""
    for m in conv.get("messages") or []:
        d = m.get("data") or {}
        ent = d.get("entity") or {}
        ts = d.get("created_at_ts") or ""
        if str(ent.get("user_id")) == me:
            last_mine = max(last_mine, ts)
        elif ent.get("user_id") and d.get("entity_type") == "message":
            buyer_msgs.append((ts, ent.get("body") or ""))
    flagged = [b for _, b in buyer_msgs if _INJECTION.search(b)]
    new_msgs = [b for ts, b in buyer_msgs if ts > last_mine]

    pending = next((e for e in reversed(events) if e["kind"] == "buyer_offer" and e.get("status") == "pending"), None)
    plan: dict[str, Any] = {"transaction_id": tid, "listed_price": listed, "floor": floor, "asking": asking,
                            "ladder": ladder, "ignored_instructions": flagged}
    if pending:
        offer = float(pending["price"])
        if offer >= floor:
            plan.update(action="accept", offer=offer, message=msgs["accept"].format(price=_fmt(offer)))
        else:
            nxt = next((p for p in ladder if p > offer), float(floor))
            final = nxt <= floor
            plan.update(action="reject_and_counter", offer=offer, counter=nxt,
                        message=(msgs["final"] if final else msgs["counter"]).format(price=_fmt(nxt), offer=_fmt(offer)))
        plan["offer_request_id"] = pending["id"]
    elif new_msgs:
        plan.update(action="remind", message=msgs["remind"].format(price=_fmt(asking)), buyer_said=new_msgs[-3:])
    else:
        plan.update(action="wait")
    if dry_run or plan["action"] == "wait":
        plan["dry_run"] = dry_run
        return plan

    results = []
    if plan["action"] == "accept":
        results.append(_ok("accept", v.respond_offer(tid, plan["offer_request_id"], True)))
    elif plan["action"] == "reject_and_counter":
        results.append(_ok("reject", v.respond_offer(tid, plan["offer_request_id"], False)))
        results.append(_ok("counter", v.seller_offer(tid, plan["counter"])))
    results.append(_ok("message", v.reply(conversation_id, plan["message"])))
    plan["results"] = results
    plan["dry_run"] = False
    return plan


def _listed_price(v: Vinted, conv: dict, tx: dict) -> float:
    iid = tx.get("item_id") or next((i.get("id") for i in (conv.get("data") or {}).get("items") or []), None)
    for e in offer_events(conv):
        if e.get("original_price"):
            return float(e["original_price"])
    if iid:
        r = v.user_items(v.my_id(), per_page=96)
        for it in (r.data or {}).get("items") or []:
            if int(it["id"]) == int(iid):
                pm = it.get("price")
                return float(pm.get("amount") if isinstance(pm, dict) else pm)
    raise ApiError("could not determine the listed price")


def _fmt(p: float) -> str:
    return f"{p:g}"


def _ok(step: str, r: Response) -> dict:
    return {"step": step, "ok": r.ok, **({"error": r.error_reason} if not r.ok else {})}


# --- relisting --------------------------------------------------------------------


def relist(
    v: Vinted,
    ref: str | int,
    *,
    photo_paths: Optional[list[str]] = None,
    wait_s: int = 180,
    dry_run: bool = False,
    **changes: Any,
) -> dict:
    """Delete a listing and publish it again with the same fields (plus ``changes``).

    Without ``photo_paths`` the current photos are downloaded and re-uploaded;
    Vinted may flag re-used photos of a deleted listing as a duplicate, so fresh
    photos are safer. The old listing is deleted first and the new one is only
    published once the old one is gone, so two copies are never online.
    Listings under review cannot be read or deleted until the review ends.
    """
    iid = _item_id(ref)
    r = v.item_draft(iid)
    if not r.ok:
        raise ApiError(f"cannot read listing {iid} ({r.error_reason}); listings under review cannot be relisted yet")
    cur = r.data.get("item") or {}
    attrs = {a["code"]: a.get("ids") or [] for a in cur.get("item_attributes") or [] if a.get("code")}
    pm = cur.get("price")
    fields: dict[str, Any] = {
        "title": cur.get("title"),
        "description": cur.get("description"),
        "price": float(pm.get("amount") if isinstance(pm, dict) else pm),
        "catalog_id": cur.get("catalog_id"),
        "condition": (attrs.pop("condition", [None]) or [None])[0],
        "brand_id": cur.get("brand_id"),
        "brand": (cur.get("brand_dto") or {}).get("title"),
        "size_id": cur.get("size_id"),
        "color_ids": [c for c in (cur.get("color1_id"), cur.get("color2_id")) if c],
        "package_size_id": cur.get("package_size_id"),
        "attributes": attrs,
    }
    fields.update(changes)
    fields = {k: v_ for k, v_ in fields.items() if v_ not in (None, [], {})}
    if dry_run:
        return {"old_item_id": iid, "would_publish": fields,
                "photos": photo_paths or [p.get("full_size_url") for p in cur.get("photos") or []], "dry_run": True}

    tmpdir = None
    if not photo_paths:
        tmpdir = tempfile.mkdtemp(prefix="vinted-relist-")
        photo_paths = []
        for n, p in enumerate(cur.get("photos") or []):
            url = p.get("full_size_url") or p.get("url")
            if not url:
                continue
            path = os.path.join(tmpdir, f"{n:02d}.jpg")
            with open(path, "wb") as f:
                f.write(v.http.session.get(url, timeout=60).content)
            photo_paths.append(path)

    d = v.delete_item(iid)
    if not d.ok:
        raise ApiError(f"could not delete listing {iid}: {d.error_reason}")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        items = (v.user_items(v.my_id(), per_page=96).data or {}).get("items") or []
        if not any(int(i["id"]) == iid for i in items):
            break
        time.sleep(5)
    else:
        return {"old_item_id": iid, "deleted": True, "published": False,
                "error": f"old listing still visible after {wait_s}s; publish later with these fields", "fields": fields,
                "photo_paths": photo_paths}

    r = v.publish(photo_paths=photo_paths, **fields)
    out = {"old_item_id": iid, "deleted": True, "published": r.ok, "fields": fields}
    if r.ok:
        out["new_item_id"] = (r.data.get("item") or {}).get("id")
    else:
        out.update(error=r.error_reason, photo_paths=photo_paths)
    return out
