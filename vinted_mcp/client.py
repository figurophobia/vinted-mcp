"""Vinted API client (the web app's internal API, no browser).

Two API hosts are involved:

- **core** ``https://www.vinted.<tld>/api/v2``: users, wardrobes, favourites,
  uploads, items, transactions/offers, orders.
- **edge** ``https://api.vinted.<tld>``: catalogue search, filters, search
  suggestions and saved searches, messaging, notifications, wallet.

Authentication is the browser session: the ``access_token_web`` /
``refresh_token_web`` cookies (plus the anti-bot cookies). A ``401`` refreshes
the access token through ``/web/api/auth/refresh`` and replays the request.

Checkout / payment is deliberately not implemented.
"""

from __future__ import annotations

import mimetypes
import os
import uuid
from typing import Any, Callable, Iterable, Optional

from curl_cffi import CurlMime

from . import config as _cfg
from .http import ApiError, Response, Transport, TransportError
from .ids import item_id as _item_id
from .ids import user_id as _user_id
from .models import Item, item_details_from_page, item_from_api


class Vinted:
    """Typed wrapper around Vinted's web API."""

    def __init__(
        self,
        cookies: Optional[dict[str, str]] = None,
        *,
        domain: Optional[str] = None,
        persist_fn: Optional[Callable[["Vinted"], None]] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        self.domain = domain or _cfg.DOMAIN
        self.web = _cfg.web_base(self.domain)
        self.core = _cfg.core_base(self.domain)
        self.edge = _cfg.edge_base(self.domain)
        self.http = transport or Transport(
            cookies,
            domain=self.domain,
            locale=_cfg.locale(self.domain),
            csrf_token=_cfg.CSRF_TOKEN,
            impersonate=_cfg.IMPERSONATE,
        )
        self.http.on_unauthorized = self.refresh
        self.persist_fn = persist_fn
        self._me: Optional[dict] = None
        self.last_refresh_rejected = False

    # -- session -----------------------------------------------------------

    @property
    def logged_in(self) -> bool:
        return bool(self.http.cookie("access_token_web") or self.http.cookie("refresh_token_web"))

    def refresh(self) -> bool:
        """Mint a new ``access_token_web`` from ``refresh_token_web``. Returns success.

        ``last_refresh_rejected`` tells apart "Vinted rejected the token" (400/401)
        from transient failures (anti-bot 403, 5xx, network), where retrying
        with another token would be wrong.
        """
        self.last_refresh_rejected = False
        if not self.http.cookie("refresh_token_web"):
            return False
        try:
            r = self.http.request("POST", f"{self.web}/web/api/auth/refresh", retry_auth=False)
        except TransportError:
            return False
        if r.status >= 300:
            self.last_refresh_rejected = r.status in (400, 401)
            return False
        for name, value in r.set_cookies.items():
            if name in _cfg.AUTH_COOKIES and value:
                self.http.set_cookie(name, value)
        self._persist()
        return True

    def _persist(self) -> None:
        if self.persist_fn:
            try:
                self.persist_fn(self)
            except OSError:
                pass

    def cookies(self) -> dict[str, str]:
        return self.http.cookies()

    def _require_login(self) -> None:
        if not self.logged_in:
            raise ApiError("this operation needs a session: run `vinted login --browser chromium` (or --cookies-file)")

    # -- raw access --------------------------------------------------------

    def get(self, base: str, path: str, params: Optional[dict] = None) -> Response:
        return self.http.request("GET", self._url(base, path), params=params)

    def send(self, method: str, base: str, path: str, body: Any = None, **kw: Any) -> Response:
        r = self.http.request(method, self._url(base, path), json_body=body, **kw)
        return r

    def _url(self, base: str, path: str) -> str:
        if path.startswith("http"):
            return path
        root = {"core": self.core, "edge": self.edge, "web": self.web}.get(base, base)
        return root + (path if path.startswith("/") else "/" + path)

    # -- account -----------------------------------------------------------

    def me(self, fresh: bool = False) -> dict:
        """The signed-in user (``/users/current``)."""
        self._require_login()
        if self._me is None or fresh:
            r = self.get("core", "/users/current").raise_for_error()
            self._me = r.data.get("user") or {}
        return self._me

    def my_id(self) -> int:
        return int(self.me()["id"])

    # -- catalogue / search ------------------------------------------------

    def search(
        self,
        text: str = "",
        *,
        catalog_ids: Optional[Iterable[int]] = None,
        brand_ids: Optional[Iterable[int]] = None,
        conditions: Optional[Iterable[str | int]] = None,
        size_ids: Optional[Iterable[int]] = None,
        color_ids: Optional[Iterable[int]] = None,
        material_ids: Optional[Iterable[int]] = None,
        attributes: Optional[dict[str, Iterable[int]]] = None,
        price_from: Optional[float] = None,
        price_to: Optional[float] = None,
        currency: str = "EUR",
        order: str = "relevance",
        page: int = 1,
        per_page: int = 24,
    ) -> Response:
        """Raw catalogue search (``svc-catalogue/items``).

        Filters other than text/price/order go in ``attribute_ids[<code>]``:
        ``catalog``, ``brand``, ``status`` (condition), ``size``, ``color``,
        ``material``, ``video_game_platform``... (codes from :meth:`filters`).
        """
        if order not in _cfg.ORDERS:
            raise ValueError(f"order must be one of {', '.join(_cfg.ORDERS)}")
        params: dict[str, Any] = {
            "search_text": text,
            "page": page,
            "per_page": per_page,
            "order": order,
            "price_from": price_from,
            "price_to": price_to,
            "currency": currency if (price_from is not None or price_to is not None) else None,
        }
        attrs: dict[str, Any] = dict(attributes or {})
        if catalog_ids:
            attrs["catalog"] = catalog_ids
        if brand_ids:
            attrs["brand"] = brand_ids
        if conditions:
            attrs["status"] = [condition_id(c) for c in conditions]
        if size_ids:
            attrs["size"] = size_ids
        if color_ids:
            attrs["color"] = color_ids
        if material_ids:
            attrs["material"] = material_ids
        for code, ids in attrs.items():
            ids = [ids] if isinstance(ids, (int, str)) else list(ids)
            if ids:
                params[f"attribute_ids[{code}]"] = ",".join(str(i) for i in ids)
        return self.get("edge", "/svc-catalogue/items", params)

    def search_items(self, text: str = "", *, pages: int = 1, per_page: int = 24, **filters: Any) -> list[Item]:
        """Search and map results to :class:`Item`, following up to ``pages`` pages."""
        out: list[Item] = []
        for page in range(1, max(1, pages) + 1):
            r = self.search(text, page=page, per_page=per_page, **filters).raise_for_error()
            items = r.data.get("items") or []
            out.extend(item_from_api(i, self.web) for i in items)
            pag = r.data.get("pagination") or {}
            if not items or page >= int(pag.get("total_pages") or page):
                break
        return out

    def filters(self, text: str = "", catalog_ids: Optional[Iterable[int]] = None) -> Response:
        """Available search filters (codes and options) for a query/category."""
        params: dict[str, Any] = {"search_text": text}
        if catalog_ids:
            params["attribute_ids[catalog]"] = ",".join(str(c) for c in catalog_ids)
        return self.get("edge", "/svc-filters/filters", params)

    def filter_options(self, code: str, text: str = "", catalog_ids: Optional[Iterable[int]] = None,
                       search: str = "") -> Response:
        """Options of one filter (e.g. ``brand``), optionally searching inside it."""
        params: dict[str, Any] = {"search_text": text, "filter_code": code}
        if catalog_ids:
            params["attribute_ids[catalog]"] = ",".join(str(c) for c in catalog_ids)
        if search:
            params.update(filter_search_code=code, filter_search_text=search)
            return self.get("edge", "/svc-filters/filters/search", params)
        return self.get("edge", "/svc-filters/filters/facets", params)

    def suggestions(self, text: str) -> Response:
        """Search-box autocomplete."""
        return self.get("edge", "/search-bar/v2/suggestions", {"search_text": text})

    def categories(self) -> list[dict]:
        """Flat category list ``[{id, title, path, leaf}]`` from the upload tree."""
        r = self.get("core", "/item_upload/catalogs").raise_for_error()
        flat: list[dict] = []

        def walk(nodes: list, trail: list[str]) -> None:
            for n in nodes:
                children = n.get("catalogs") or []
                path = trail + [n.get("title", "")]
                flat.append({"id": n["id"], "title": n.get("title"), "path": " > ".join(path), "leaf": not children})
                walk(children, path)

        walk(r.data.get("catalogs") or [], [])
        return flat

    def brands(self, keyword: str, category_id: Optional[int] = None) -> Response:
        """Brand lookup for uploads (``id``/``title``)."""
        return self.get("core", "/item_upload/brands", {"keyword": keyword, "category_id": category_id})

    def colors(self) -> Response:
        return self.get("core", "/item_upload/colors")

    def upload_attributes(self, catalog_id: int) -> Response:
        """Attributes a category asks for when listing (code, required, options with ids)."""
        return self.send("POST", "core", "/item_upload/attributes",
                         {"attributes": [{"code": "category", "value": [int(catalog_id)]}]})

    def find_categories(self, name: str) -> Response:
        """Category ids whose *name* matches (``data.catalog_ids``), as the upload form's category search."""
        return self.get("core", "/item_upload/catalogs/search", {"keyword": name})

    def suggest_categories(self, text: str, max_depth: int = 6) -> list[dict]:
        """Rank categories for an item description by where similar listings are.

        Drills down the catalogue facets following the busiest branch and
        returns the last level ``[{id, title, item_count}]``, best first.
        """
        level: list[dict] = []
        cid: Optional[str] = None
        for _ in range(max_depth):
            params: dict[str, Any] = {"search_text": text}
            if cid:
                params["attribute_ids[catalog]"] = cid
            r = self.get("edge", "/svc-filters/faceted_categories", params).raise_for_error()
            cats = [{"id": int(c["id"]), "title": c.get("title"), "item_count": c.get("item_count")}
                    for c in r.data.get("categories") or []]
            if not cats:
                break
            level = cats
            cid = str(cats[0]["id"])
        return level

    def package_sizes(self, catalog_id: int) -> Response:
        """Shipping package sizes allowed for a category (``id`` is the ``package_size_id``)."""
        return self.get("edge", f"/shipping-estimation/external/catalogs/{int(catalog_id)}/package_sizes",
                        {"upload_session_id": str(uuid.uuid4())})

    # -- items -------------------------------------------------------------

    def item(self, ref: str | int) -> dict:
        """Item details parsed from the item page (title, price, attributes, seller...)."""
        iid = _item_id(ref)
        r = self.http.request("GET", f"{self.web}/items/{iid}", headers={"Accept": "text/html,application/xhtml+xml"})
        if r.status == 404:
            raise ApiError(f"item {iid} not found (deleted, sold or under review)")
        if r.status >= 300 or not isinstance(r.data, str):
            raise ApiError(r.error_reason or f"HTTP {r.status}")
        details = item_details_from_page(r.data, iid)
        ship = self.shipping_details(iid)
        if ship.ok:
            details["shipping"] = shipping_summary(ship.data.get("shipping_details") or {})
        return details

    def shipping_details(self, ref: str | int) -> Response:
        """The "Envío desde X €" block: cheapest shipping price, free shipping, pickup-only, discounts."""
        return self.get("core", f"/items/{_item_id(ref)}/shipping_details")

    def similar_items(self, ref: str | int, source: str = "similar_items") -> Response:
        """Items related to a listing: ``similar_items`` or ``other_user_items`` (same seller)."""
        return self.get("edge", f"/item-details/more-items/{_item_id(ref)}", {"content_source": source, "screen": "item"})

    # -- users -------------------------------------------------------------

    def user(self, ref: str | int) -> dict:
        r = self.get("core", f"/users/{_user_id(ref)}").raise_for_error()
        return r.data.get("user") or {}

    def user_items(self, ref: str | int, *, page: int = 1, per_page: int = 20, order: str = "relevance") -> Response:
        """A member's wardrobe (their listings). For yourself it includes hidden/reserved ones."""
        return self.get("core", f"/wardrobe/{_user_id(ref)}/items", {"page": page, "per_page": per_page, "order": order})

    def user_feedback(self, ref: str | int, *, page: int = 1, per_page: int = 20) -> Response:
        return self.get("core", "/user_feedbacks", {"user_id": _user_id(ref), "page": page, "per_page": per_page})

    def feedback_summary(self, ref: str | int) -> Response:
        return self.get("core", "/user_feedbacks/summary", {"user_id": _user_id(ref)})

    def followers(self, ref: str | int, *, page: int = 1) -> Response:
        return self.get("core", f"/users/{_user_id(ref)}/followers", {"page": page})

    def toggle_follow(self, ref: str | int) -> Response:
        """Follow/unfollow a member (the API only toggles). ``data.is_followed`` is the new state."""
        self._require_login()
        return self.send("POST", "core", "/followed_users/toggle", {"user_id": _user_id(ref)})

    # -- favourites ----------------------------------------------------------

    def favourites(self, *, page: int = 1, per_page: int = 20) -> Response:
        self._require_login()
        return self.get("core", f"/users/{self.my_id()}/items/favourites", {"page": page, "per_page": per_page})

    def is_favourite(self, ref: str | int, max_pages: int = 10) -> bool:
        iid = _item_id(ref)
        for page in range(1, max_pages + 1):
            r = self.favourites(page=page, per_page=96).raise_for_error()
            items = r.data.get("items") or []
            if any(int(i.get("id", 0)) == iid for i in items):
                return True
            pag = r.data.get("pagination") or {}
            if not items or page >= int(pag.get("total_pages") or page):
                return False
        return False

    def toggle_favourite(self, ref: str | int) -> Response:
        self._require_login()
        return self.send("POST", "core", "/user_favourites/toggle", {"type": "item", "user_favourites": [_item_id(ref)]})

    def set_favorite(self, ref: str | int, favourite: bool) -> Response:
        """Idempotent favourite: only toggles when the current state differs."""
        if self.is_favourite(ref) == favourite:
            return Response(200, {"code": 0, "unchanged": True, "is_favourite": favourite})
        r = self.toggle_favourite(ref)
        if r.ok and isinstance(r.data, dict):
            r.data.setdefault("is_favourite", favourite)
        return r

    # -- messaging -----------------------------------------------------------

    def inbox(self, *, page: int = 1, per_page: int = 20) -> Response:
        self._require_login()
        return self.get("edge", "/messaging/main/inbox", {"page": page, "per_page": per_page})

    def conversation(self, conversation_id: str) -> Response:
        self._require_login()
        return self.get("edge", f"/messaging/main/conversations/{conversation_id}")

    def unread_messages(self) -> Response:
        self._require_login()
        return self.get("edge", "/messaging/main/users/unread_count")

    def start_conversation(self, ref: str | int, seller_id: Optional[int] = None) -> Response:
        """Open (or reuse) the buyer->seller conversation for an item.

        ``data`` carries ``conversation_id`` and ``transaction_id`` (needed for offers).
        """
        self._require_login()
        iid = _item_id(ref)
        if seller_id is None:
            details = self.item(iid)
            seller_id = details.get("seller_id")
            if not seller_id:
                raise ApiError(f"could not find the seller of item {iid}")
        return self.send("POST", "edge", "/messaging/main/inquiries", {"item_ids": [str(iid)], "receiver_id": str(seller_id)})

    def reply(self, conversation_id: str, text: str = "", photo_temp_uuids: Optional[list[str]] = None,
              photo_paths: Optional[list[str]] = None) -> Response:
        """Send a message (text and/or photos) in an existing conversation."""
        self._require_login()
        uuids = list(photo_temp_uuids or [])
        for p in photo_paths or []:
            r = self._upload_photo_of_type(p, "user_msg")
            if not r.ok:
                return r
            uuids.append(r.data["temp_uuid"])
        photo_temp_uuids = uuids
        if not text.strip() and not photo_temp_uuids:
            raise ValueError("empty message")
        return self.send("POST", "edge", f"/messaging/main/conversations/{conversation_id}/replies", {
            "content": text or None,
            "is_personal_data_sharing_check_skipped": False,
            "photo_temp_uuids": photo_temp_uuids or None,
        })

    def message_item(self, ref: str | int, text: str, photo_paths: Optional[list[str]] = None) -> Response:
        """Message the seller of an item (creates the conversation when needed)."""
        conv = self.start_conversation(ref)
        if not conv.ok:
            return conv
        r = self.reply(str(conv.data["conversation_id"]), text, photo_paths=photo_paths)
        if isinstance(r.data, dict):
            r.data.setdefault("conversation_id", conv.data.get("conversation_id"))
            r.data.setdefault("transaction_id", conv.data.get("transaction_id"))
        return r

    def mark_conversation_read(self, conversation_id: str) -> Response:
        self._require_login()
        return self.send("POST", "edge", f"/messaging/main/conversations/{conversation_id}/mark_as_read")

    def delete_message(self, conversation_id: str, message_id: str) -> Response:
        """Delete one of your own messages.

        ``message_id`` is the numeric ``messages[].data.entity.id`` from
        :meth:`conversation` (the message UUID answers ``invalid message ID``).
        """
        self._require_login()
        return self.send("DELETE", "edge", f"/messaging/main/conversations/{conversation_id}/replies/{message_id}")

    def delete_conversation(self, conversation_id: str) -> Response:
        self._require_login()
        return self.send("DELETE", "edge", f"/messaging/main/conversations/{conversation_id}")

    # -- interested buyers --------------------------------------------------

    def item_likers(self, ref: Optional[str | int] = None, max_pages: int = 5) -> list[dict]:
        """Who favourited your listings, rebuilt from "X ha marcado como favorito..." notifications.

        The API has no "who favourited my item" endpoint; Vinted notifies the
        seller (``entry_type`` 20) with a link carrying ``item_id`` and
        ``user_id``. Returns ``[{item_id, user_id, login, at}]``, newest first,
        one entry per (item, user). Older likes are lost if the notification
        history no longer has them.
        """
        from urllib.parse import parse_qs, urlparse

        wanted = _item_id(ref) if ref is not None else None
        seen: set = set()
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            r = self.notifications(page=page, per_page=50).raise_for_error()
            notes = r.data.get("notifications") or []
            for n in notes:
                if n.get("entry_type") != _LIKE_NOTIFICATION:
                    continue
                q = parse_qs(urlparse(n.get("link") or "").query)
                try:
                    iid = int((q.get("item_id") or [n.get("subject_id")])[0])
                    uid = int(q["user_id"][0])
                except (KeyError, TypeError, ValueError):
                    continue
                if (wanted and iid != wanted) or (iid, uid) in seen:
                    continue
                seen.add((iid, uid))
                body = n.get("body") or ""
                out.append({"item_id": iid, "user_id": uid, "login": body.split(" ", 1)[0] if body else None,
                            "at": n.get("updated_at"), "text": body})
            if len(notes) < 50:
                break
        return out

    def offer_to_liker(self, ref: str | int, user: str | int, price: Optional[float] = None,
                       text: Optional[str] = None, currency: str = "EUR") -> Response:
        """Seller: message someone interested in your item and optionally propose a price.

        Opens (or reuses) the conversation about ``ref`` with ``user``, sends
        ``text`` and, with ``price``, a seller offer they can buy at.
        """
        self._require_login()
        conv = self.start_conversation(ref, seller_id=_user_id(user))
        if not conv.ok:
            return conv
        cid, tid = str(conv.data["conversation_id"]), conv.data.get("transaction_id")
        out: dict[str, Any] = {"conversation_id": cid, "transaction_id": tid}
        if text:
            m = self.reply(cid, text)
            if not m.ok:
                return m
            out["message_id"] = (m.data.get("data") or {}).get("id")
        if price is not None:
            if not tid:
                raise ApiError("this conversation has no transaction; cannot attach an offer")
            o = self.seller_offer(int(tid), price, currency)
            if not o.ok:
                o.data = {"error": o.error_reason, **out}
                return o
            out["offer"] = o.data.get("offer")
        return Response(200, {"code": 0, **out})

    def message_likers(
        self,
        text: str,
        *,
        ref: Optional[str | int] = None,
        price: Optional[float] = None,
        discount_percent: Optional[float] = None,
        dry_run: bool = False,
    ) -> list[dict]:
        """Message everyone who favourited your active listings (or one listing), optionally with an offer.

        ``text`` may use ``{login}``, ``{title}``, ``{price}`` (the offered or
        listed price) and ``{listed_price}``. ``price`` fixes the offer;
        ``discount_percent`` derives it from each listing's price. Likes on
        listings that are no longer active are skipped. With ``dry_run`` nothing
        is sent and the planned messages are returned. One failure does not
        stop the rest; every recipient gets its own ``ok``/``error``.
        """
        if price is not None and discount_percent is not None:
            raise ValueError("pass price or discount_percent, not both")
        if discount_percent is not None and not 0 < discount_percent < 100:
            raise ValueError("discount_percent must be between 0 and 100")
        r = self.user_items(self.my_id(), per_page=96).raise_for_error()
        active = {
            int(i["id"]): i for i in r.data.get("items") or []
            if not (i.get("is_draft") or i.get("is_closed") or i.get("is_hidden") or i.get("item_alert_type"))
        }
        plan: list[dict] = []
        for like in self.item_likers(ref):
            it = active.get(like["item_id"])
            if not it:
                continue
            pm = it.get("price")
            listed = float(pm.get("amount") if isinstance(pm, dict) else pm)
            offer = price if price is not None else (
                round(listed * (1 - discount_percent / 100), 2) if discount_percent is not None else None)
            shown = offer if offer is not None else listed
            body = text.format(login=like.get("login") or "", title=it.get("title") or "",
                               price=f"{shown:g}", listed_price=f"{listed:g}")
            plan.append({"item_id": like["item_id"], "title": it.get("title"), "user_id": like["user_id"],
                         "login": like.get("login"), "listed_price": listed, "offer_price": offer, "message": body})
        if dry_run:
            return plan
        for p in plan:
            try:
                res = self.offer_to_liker(p["item_id"], p["user_id"], p["offer_price"], p["message"])
                p["ok"] = res.ok
                if res.ok:
                    p["conversation_id"] = res.data.get("conversation_id")
                else:
                    p["error"] = res.error_reason
            except ApiError as e:
                p["ok"], p["error"] = False, str(e)
        return plan

    # -- notifications / wallet ----------------------------------------------

    def notifications(self, *, page: int = 1, per_page: int = 20) -> Response:
        self._require_login()
        return self.get("edge", "/inbox-notifications/v1/notifications", {"page": page, "per_page": per_page})

    def unread_notifications(self) -> Response:
        self._require_login()
        return self.get("edge", "/inbox-notifications/v1/notifications/unread_count")

    def wallet_balance(self) -> Response:
        self._require_login()
        return self.get("edge", "/ledger/wallet/balance")

    def wallet_history(self) -> Response:
        self._require_login()
        return self.get("edge", "/wallet/history/overview")

    # -- offers / transactions ---------------------------------------------

    def transaction(self, transaction_id: int) -> Response:
        self._require_login()
        return self.get("core", f"/transactions/{int(transaction_id)}")

    def offer_constraints(self, ref: str | int, price: float, seller_id: Optional[int] = None,
                      currency: str = "EUR") -> Response:
        """Offer suggestions/limits for an item at a given listed price."""
        self._require_login()
        iid = _item_id(ref)
        if seller_id is None:
            seller_id = self.item(iid).get("seller_id")
        return self.send("POST", "core", "/offers/request_options", {
            "price": {"amount": _amount(price), "currency_code": currency},
            "item_ids": [iid],
            "seller_id": int(seller_id) if seller_id else None,
        })

    def send_offer(self, ref: str | int, price: float, currency: str = "EUR",
                   transaction_id: Optional[int] = None) -> Response:
        """Buyer: send a price offer on an item. Does not buy or pay."""
        self._require_login()
        if transaction_id is None:
            conv = self.start_conversation(ref)
            if not conv.ok:
                return conv
            transaction_id = conv.data.get("transaction_id")
            if not transaction_id:
                raise ApiError("the conversation has no transaction; offers are not available for this item")
        r = self.send("POST", "core", f"/transactions/{int(transaction_id)}/offer_requests",
                      {"offer_request": {"price": _amount(price), "currency": currency}})
        if isinstance(r.data, dict):
            r.data.setdefault("transaction_id", int(transaction_id))
        return r

    def respond_offer(self, transaction_id: int, offer_request_id: int, accept: bool) -> Response:
        """Seller: accept or decline a buyer's offer request."""
        self._require_login()
        action = "accept" if accept else "reject"
        return self.send("PUT", "core", f"/transactions/{int(transaction_id)}/offer_requests/{int(offer_request_id)}/{action}")

    def seller_offer(self, transaction_id: int, price: float, currency: str = "EUR") -> Response:
        """Seller: propose a price to the buyer of a conversation (counter-offer)."""
        self._require_login()
        return self.send("POST", "core", f"/transactions/{int(transaction_id)}/offers",
                         {"offer": {"price": _amount(price), "currency": currency}})

    # -- orders ---------------------------------------------------------------

    def orders(self, kind: str = "purchased", status: str = "all", *, page: int = 1, per_page: int = 20) -> Response:
        """Your purchases (``kind="purchased"``) or sales (``"sold"``). ``status``: all|in_progress|completed|canceled."""
        self._require_login()
        if kind not in ("purchased", "sold"):
            raise ValueError("kind must be 'purchased' or 'sold'")
        return self.get("core", "/my_orders", {"type": kind, "status": status, "page": page, "per_page": per_page})

    # -- saved searches ----------------------------------------------------

    def saved_searches(self) -> Response:
        """Recent and saved searches (``type`` is ``type_recent`` or ``type_subscribed``)."""
        self._require_login()
        return self.get("edge", f"/search-bar/v2/users/{self.my_id()}/previous_searches")

    def save_search(self, search_id: int, saved: bool = True) -> Response:
        """Subscribe (alert) to / unsubscribe from a previous search."""
        self._require_login()
        verb = "save" if saved else "unsave"
        return self.send("PUT", "edge", f"/search-bar/v2/users/{self.my_id()}/previous_searches/{int(search_id)}/{verb}")

    def create_search(self, text: str, *, catalog_id: Optional[int] = None, price_from: Optional[float] = None,
                      price_to: Optional[float] = None, currency: str = "EUR",
                      filters: Optional[dict[str, Iterable[int]]] = None) -> Response:
        """Record a search in the account history (what the web does when you search)."""
        self._require_login()
        body: dict[str, Any] = {"search_text": text}
        if catalog_id is not None:
            body["catalog_id"] = int(catalog_id)
        if price_from is not None:
            body["price_from"] = {"amount": _amount(price_from), "currency_code": currency}
        if price_to is not None:
            body["price_to"] = {"amount": _amount(price_to), "currency_code": currency}
        if filters:
            body["selected_filters"] = [{"code": k, "ids": [str(i) for i in v]} for k, v in filters.items()]
        return self.send("POST", "edge", f"/search-bar/v2/users/{self.my_id()}/previous_searches", body)

    # -- listing management -------------------------------------------------

    def upload_photo(self, path: str, temp_uuid: str) -> Response:
        """Upload one photo for a listing being created (``photo[type]=item``)."""
        self._require_login()
        if not os.path.isfile(path):
            raise ApiError(f"photo not found: {path}")
        mp = CurlMime()
        mp.addpart(name="photo[type]", data=b"item")
        mp.addpart(name="photo[temp_uuid]", data=temp_uuid.encode())
        ctype = mimetypes.guess_type(path)[0] or "image/jpeg"
        mp.addpart(name="photo[file]", content_type=ctype, filename=os.path.basename(path), local_path=path)
        try:
            return self.http.request("POST", f"{self.core}/photos", multipart=mp)
        finally:
            mp.close()

    def item_draft(self, ref: str | int) -> Response:
        """The editable representation of one of your items (what the edit form loads)."""
        self._require_login()
        return self.get("core", f"/item_upload/items/{_item_id(ref)}")

    def build_item(
        self,
        *,
        title: str,
        description: str,
        price: float,
        catalog_id: int,
        condition: str | int,
        photo_ids: list[int],
        temp_uuid: str,
        brand_id: Optional[int] = None,
        brand: Optional[str] = None,
        size_id: Optional[int] = None,
        color_ids: Optional[list[int]] = None,
        package_size_id: Optional[int] = None,
        attributes: Optional[dict[str, list[int]]] = None,
        isbn: Optional[str] = None,
        currency: str = "EUR",
        item_id: Optional[int] = None,
    ) -> dict:
        """The ``item`` object the upload form posts."""
        item_attributes = [{"code": k, "ids": [int(i) for i in v]} for k, v in (attributes or {}).items() if k != "condition"]
        item_attributes.append({"code": "condition", "ids": [condition_id(condition)]})
        item: dict[str, Any] = {
            "id": item_id,
            "currency": currency,
            "temp_uuid": temp_uuid,
            "title": title,
            "description": description,
            "brand_id": brand_id,
            "brand": brand,
            "catalog_id": int(catalog_id),
            "isbn": isbn,
            "ai_photo": False,
            "price": _amount(price),
            "package_size_id": package_size_id,
            "shipment_prices": {"domestic": None, "international": None},
            "color_ids": list(color_ids or []),
            "assigned_photos": [
                {"id": int(pid), "orientation": 0, "ai_detected": False, "digital_source_type": [], "c2pa_read_error": "other"}
                for pid in photo_ids
            ],
            "measurement_length": None,
            "measurement_width": None,
            "item_attributes": item_attributes,
            "manufacturer": None,
            "manufacturer_labelling": None,
        }
        if size_id is not None:
            item["size_id"] = int(size_id)
        return item

    def publish(self, *, photo_paths: Optional[list[str]] = None, draft: bool = False,
                photo_ids: Optional[list[int]] = None, upload_session_id: Optional[str] = None,
                **fields: Any) -> Response:
        """Upload photos and create the listing (or a draft with ``draft=True``).

        Photos already uploaded (e.g. by :func:`automation.listing_from_photos`)
        can be reused with ``photo_ids`` + their ``upload_session_id``.

        ``fields`` are :meth:`build_item` keyword arguments (minus photo ids /
        temp uuid). If the photo uploads succeed but the final POST fails the
        returned response carries ``data["photo_ids"]`` so nothing is lost.
        """
        self._require_login()
        if photo_ids:
            if not upload_session_id:
                raise ValueError("photo_ids need the upload_session_id they were uploaded with")
            photo_paths = []
        elif not photo_paths:
            raise ValueError("at least one photo is required")
        if len(photo_paths) > 20 or len(photo_ids or []) > 20:
            raise ValueError("Vinted accepts at most 20 photos")
        temp_uuid = upload_session_id or str(uuid.uuid4())
        photo_ids = [int(i) for i in photo_ids or []]
        for p in photo_paths:
            r = self.upload_photo(p, temp_uuid)
            if not r.ok:
                r.data = {"error": r.error_reason, "uploaded_photo_ids": photo_ids, "failed_photo": p}
                return r
            photo_ids.append(int(r.data["id"]))
        item = self.build_item(photo_ids=photo_ids, temp_uuid=temp_uuid, **fields)
        if draft:
            r = self.send("POST", "core", "/item_upload/drafts",
                          {"draft": item, "parcel": None, "upload_session_id": temp_uuid})
        else:
            r = self.send("POST", "core", "/item_upload/items",
                          {"item": item, "push_up": False, "parcel": None, "upload_session_id": temp_uuid},
                          headers={"X-Upload-Form": "true"})
        if isinstance(r.data, dict):
            r.data.setdefault("photo_ids", photo_ids)
        return r

    def edit_item(self, ref: str | int, **changes: Any) -> Response:
        """Change only the given fields of one of your listings.

        Accepted keys: ``title``, ``description``, ``price``, ``catalog_id``,
        ``condition``, ``brand_id``/``brand``, ``size_id``, ``color_ids``,
        ``package_size_id``, ``attributes`` (merged by code), ``isbn``,
        ``photo_paths`` (replaces the photos).
        """
        self._require_login()
        unknown = set(changes) - _EDITABLE
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
        iid = _item_id(ref)
        cur = self.item_draft(iid).raise_for_error().data.get("item") or {}
        temp_uuid = str(uuid.uuid4())
        photo_ids = [int(p["id"]) for p in cur.get("photos") or [] if p.get("id")]
        if changes.get("photo_paths"):
            photo_ids = []
            for p in changes.pop("photo_paths"):
                r = self.upload_photo(p, temp_uuid)
                if not r.ok:
                    return r
                photo_ids.append(int(r.data["id"]))
        changes.pop("photo_paths", None)
        item = self._merged_item(iid, cur, changes, temp_uuid, photo_ids)
        return self.send("PUT", "core", f"/item_upload/items/{iid}",
                         {"item": item, "push_up": False, "parcel": None, "upload_session_id": temp_uuid},
                         headers={"X-Upload-Form": "true"})

    def _merged_item(self, iid: int, cur: dict, changes: dict, temp_uuid: str, photo_ids: list[int]) -> dict:
        """The upload body for ``cur`` (an ``item_upload`` item) with ``changes`` applied."""
        changes = dict(changes)
        attrs = {a["code"]: a.get("ids") or [] for a in cur.get("item_attributes") or [] if a.get("code")}
        attrs.update(changes.pop("attributes", None) or {})
        condition = changes.pop("condition", None) or (attrs.pop("condition", [None]) or [None])[0] or cur.get("status_id")
        attrs.pop("condition", None)
        price = changes.pop("price", None)
        if price is None:
            pm = cur.get("price")
            price = pm.get("amount") if isinstance(pm, dict) else pm
        return self.build_item(
            title=changes.pop("title", cur.get("title")),
            description=changes.pop("description", cur.get("description")),
            price=float(price),
            catalog_id=changes.pop("catalog_id", cur.get("catalog_id")),
            condition=condition,
            photo_ids=photo_ids,
            temp_uuid=temp_uuid,
            brand_id=changes.pop("brand_id", cur.get("brand_id")),
            brand=changes.pop("brand", (cur.get("brand_dto") or {}).get("title") or cur.get("brand_title")),
            size_id=changes.pop("size_id", cur.get("size_id")),
            color_ids=changes.pop("color_ids", [c for c in (cur.get("color1_id"), cur.get("color2_id")) if c] or cur.get("color_ids")),
            package_size_id=changes.pop("package_size_id", cur.get("package_size_id")),
            attributes=attrs,
            isbn=changes.pop("isbn", cur.get("isbn")),
            currency=cur.get("currency") or "EUR",
            item_id=iid,
        )

    def delete_item(self, ref: str | int) -> Response:
        """Permanently delete one of your listings."""
        self._require_login()
        return self.send("POST", "core", f"/items/{_item_id(ref)}/delete")

    def set_hidden(self, ref: str | int, hidden: bool) -> Response:
        """Hide (pause) or show one of your listings."""
        self._require_login()
        return self.send("PUT", "core", f"/items/{_item_id(ref)}/is_hidden", {"is_hidden": bool(hidden)})

    def reserve(self, transaction_id: int, reserved: bool = True) -> Response:
        """Reserve an item for the buyer of a conversation (by its transaction id)."""
        self._require_login()
        return self.send("PUT", "core", f"/transactions/{int(transaction_id)}/reservation", {"reserved": bool(reserved)})

    def unreserve(self, ref: str | int) -> Response:
        self._require_login()
        return self.send("DELETE", "core", f"/items/{_item_id(ref)}/reservation")

    def drafts(self) -> list[dict]:
        """Your drafts (wardrobe entries flagged ``is_draft``)."""
        r = self.user_items(self.my_id(), per_page=96).raise_for_error()
        return [i for i in r.data.get("items") or [] if i.get("is_draft")]

    def publish_draft(self, draft_id: int, **changes: Any) -> Response:
        """Publish an existing draft (``POST /item_upload/drafts/{id}/completion``), optionally changing fields first.

        Accepts the same keys as :meth:`edit_item` (minus ``photo_paths``).
        """
        self._require_login()
        unknown = set(changes) - (_EDITABLE - {"photo_paths"})
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
        did = int(draft_id)
        cur = self.item_draft(did).raise_for_error().data.get("item") or {}
        temp_uuid = str(uuid.uuid4())
        draft = self._merged_item(did, cur, changes, temp_uuid,
                                  [int(p["id"]) for p in cur.get("photos") or [] if p.get("id")])
        return self.send("POST", "core", f"/item_upload/drafts/{did}/completion",
                         {"draft": draft, "push_up": False, "parcel": None, "upload_session_id": temp_uuid})

    def delete_draft(self, draft_id: int) -> Response:
        self._require_login()
        return self.send("DELETE", "core", f"/item_upload/drafts/{int(draft_id)}")

    def bundle_discounts(self) -> Response:
        """Bundle discount settings: ``{enabled, tiers: [{discount_value, min_items}]}``."""
        self._require_login()
        return self.get("edge", "/messaging/bundles/discounts/settings")

    def set_bundle_discounts(self, enabled: bool, tiers: Optional[dict[int, float]] = None) -> Response:
        """Set "X% off when buying N or more" tiers, e.g. ``{2: 5, 3: 10}`` (min items -> percent)."""
        self._require_login()
        body_tiers = []
        for min_items, pct in sorted((tiers or {}).items()):
            if not 0 < float(pct) < 100 or int(min_items) < 2:
                raise ValueError("tiers are {min_items >= 2: percent between 0 and 100}")
            body_tiers.append({"discount_value": f"{float(pct) / 100:.2f}", "min_items": int(min_items)})
        if enabled and not body_tiers:
            raise ValueError("enabling bundle discounts needs at least one tier")
        return self.send("PUT", "edge", "/messaging/bundles/discounts/settings", {"enabled": bool(enabled), "tiers": body_tiers})

    def _profile_body(self, me: dict, **overrides: Any) -> dict:
        """The full ``PUT /users/current`` body: current values with ``overrides`` applied."""
        settings = me.get("user_settings") or {}
        user = {
            "real_name": me.get("real_name"),
            "city_id": me.get("city_id"),
            "country_id": me.get("country_id"),
            "gender": me.get("gender"),
            "birthday": me.get("birthday"),
            "text_about": me.get("about"),
            "login": me.get("login"),
            "photo_temp_uuid": None,
            "user_settings": {
                "is_location_public": settings.get("is_location_public", me.get("is_location_public")),
                "on_holiday": settings.get("on_holiday", me.get("is_on_holiday")),
                "international_trading_enabled": settings.get("international_trading_enabled",
                                                              me.get("international_trading_enabled")),
            },
        }
        for k, v in overrides.items():
            if k in user["user_settings"]:
                user["user_settings"][k] = v
            else:
                user[k] = v
        return {"user": user}

    def set_holiday(self, on: bool) -> Response:
        """Holiday mode: hides your whole closet (``on=True``) or brings it back.

        Turning it on goes through the profile update (``PUT /users/current``),
        which rewrites the whole profile, so the current values are sent back
        unchanged. Turning it off is ``DELETE /users/{id}/holiday``.
        """
        self._require_login()
        me = self.me(fresh=True)
        if not on:
            return self.send("DELETE", "core", f"/users/{me['id']}/holiday")
        return self.send("PUT", "core", "/users/current", self._profile_body(me, on_holiday=True))

    def cities(self, search: str, country_id: Optional[int] = None) -> Response:
        """City lookup for the profile (``data.cities: [{id, title}]``); defaults to your country."""
        cid = country_id or self.me().get("country_id")
        return self.get("core", f"/countries/{int(cid)}/cities", {"search_text": search})

    def update_profile(
        self,
        *,
        about: Optional[str] = None,
        city: Optional[str] = None,
        city_id: Optional[int] = None,
        show_city: Optional[bool] = None,
        photo_path: Optional[str] = None,
        real_name: Optional[str] = None,
    ) -> Response:
        """Change your bio (``about``), city (by name or id), city visibility, real name or profile photo.

        Only the given fields change; the rest of the profile is sent back as is.
        """
        self._require_login()
        me = self.me(fresh=True)
        overrides: dict[str, Any] = {}
        if about is not None:
            overrides["text_about"] = about
        if real_name is not None:
            overrides["real_name"] = real_name
        if show_city is not None:
            overrides["is_location_public"] = bool(show_city)
        if city is not None and city_id is None:
            found = (self.cities(city).raise_for_error().data or {}).get("cities") or []
            exact = [c for c in found if (c.get("title") or "").lower() == city.lower()]
            if not (exact or found):
                raise ApiError(f"no city matches {city!r}")
            city_id = (exact or found)[0]["id"]
        if city_id is not None:
            overrides["city_id"] = int(city_id)
        if photo_path:
            r = self._upload_photo_of_type(photo_path, "user")
            if not r.ok:
                return r
            overrides["photo_temp_uuid"] = r.data.get("temp_uuid")
        if not overrides:
            raise ValueError("nothing to change")
        r = self.send("PUT", "core", "/users/current", self._profile_body(me, **overrides))
        if r.ok:
            self._me = None
        return r

    def _upload_photo_of_type(self, path: str, photo_type: str) -> Response:
        """``POST /photos`` with ``photo[type]`` (``user`` = avatar, ``user_msg`` = chat photo); returns ``temp_uuid``."""
        if not os.path.isfile(path):
            raise ApiError(f"photo not found: {path}")
        mp = CurlMime()
        mp.addpart(name="photo[type]", data=photo_type.encode())
        mp.addpart(name="photo[file]", content_type=mimetypes.guess_type(path)[0] or "image/jpeg",
                   filename=os.path.basename(path), local_path=path)
        try:
            return self.http.request("POST", f"{self.core}/photos", multipart=mp)
        finally:
            mp.close()

    def block_user(self, ref: str | int, blocked: bool = True) -> Response:
        """Block (or with ``blocked=False`` unblock) a member: they cannot message you or buy from you."""
        self._require_login()
        uid = _user_id(ref)
        if blocked:
            return self.send("POST", "core", f"/users/{self.my_id()}/user_hates", {"hated_user_id": uid})
        return self.http.request("DELETE", f"{self.core}/users/{self.my_id()}/user_hates", params={"hated_user_id": uid})

    def item_stats(self, ref: str | int) -> dict:
        """Views and favourites of one of your listings, who liked it, and daily bump stats when it was bumped."""
        iid = _item_id(ref)
        r = self.user_items(self.my_id(), per_page=96).raise_for_error()
        it = next((i for i in r.data.get("items") or [] if int(i["id"]) == iid), None)
        if it is None:
            raise ApiError(f"item {iid} is not one of your listings")
        out: dict[str, Any] = {
            "item_id": iid,
            "title": it.get("title"),
            "views": it.get("view_count"),
            "favourites": it.get("favourite_count"),
            "likers": [{k: l[k] for k in ("user_id", "login", "at")} for l in self.item_likers(iid)],
        }
        b = self.get("edge", f"/vas/items/{iid}/bumps/stats", {"timezone": "Europe/Madrid"})
        if b.ok:
            out["bump_stats"] = b.data
        return out


#: Notification ``entry_type`` for "<user> favourited your item".
_LIKE_NOTIFICATION = 20


_EDITABLE = frozenset({
    "title", "description", "price", "catalog_id", "condition", "brand_id", "brand", "size_id", "color_ids",
    "package_size_id", "attributes", "isbn", "photo_paths",
})


def shipping_summary(sd: dict) -> dict:
    """Flatten ``shipping_details`` to ``{from_price, currency, free, pickup_only, multiple_options, discount}``."""
    price = sd.get("price") or {}
    out = {
        "from_price": str(price.get("amount")) if price.get("amount") is not None else None,
        "currency": price.get("currency_code"),
        "free": sd.get("free_shipping"),
        "pickup_only": sd.get("pickup_only"),
        "multiple_options": sd.get("multiple_shipping_options_available"),
        "discount": sd.get("discount"),
    }
    return {k: v for k, v in out.items() if v is not None}


def condition_id(c: str | int) -> int:
    """``"good"`` / ``3`` / ``"3"`` -> 3."""
    if isinstance(c, int):
        return c
    s = str(c).strip().lower()
    if s.isdigit():
        return int(s)
    if s in _cfg.CONDITIONS:
        return _cfg.CONDITIONS[s]
    raise ValueError(f"unknown condition {c!r}; use one of {', '.join(_cfg.CONDITIONS)}")


def _amount(price: float | str) -> str:
    v = float(price)
    if v <= 0:
        raise ValueError("price must be positive")
    return f"{v:.2f}".rstrip("0").rstrip(".")
