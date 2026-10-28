"""``vinted`` command line interface over :mod:`vinted_mcp`.

Every command prints JSON (``--pretty`` for indentation) except ``search``,
``my-items`` and ``favs`` which print a compact table unless ``--json``.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Optional

from .. import config as _cfg
from ..client import Vinted
from ..http import ApiError, Response
from ..models import Item, item_from_api
from ..session import SessionError, build_client, import_browser, import_cookie_text, login_wizard, session_info

_PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic")


def _out(args: argparse.Namespace, value: Any) -> None:
    if isinstance(value, Response):
        value = {"ok": value.ok, "status": value.status, "data": value.data, **({"error": value.error_reason} if not value.ok else {})}
    print(json.dumps(value, ensure_ascii=False, indent=2 if args.pretty else None, default=str))


def _table(items: list[Item]) -> None:
    for i in items:
        flags = ",".join(k.replace("is_", "") for k in i.status) if i.status else ""
        price = f"{i.price} {i.currency or ''}".strip() if i.price else "-"
        print(f"{i.id:>12}  {price:>12}  {(i.condition or '')[:14]:14}  {(i.brand or '')[:14]:14}  {i.title[:60]}"
              + (f"  [{flags}]" if flags else ""))


def _items(args: argparse.Namespace, items: list[Item]) -> None:
    if args.json:
        _out(args, [i.to_dict() for i in items])
    else:
        _table(items)


def _ints(s: Optional[str]) -> Optional[list[int]]:
    return [int(x) for x in s.split(",") if x.strip()] if s else None


def _attrs(pairs: Optional[list[str]]) -> Optional[dict[str, list[int]]]:
    if not pairs:
        return None
    out: dict[str, list[int]] = {}
    for p in pairs:
        code, _, ids = p.partition("=")
        out[code.strip()] = [int(x) for x in ids.split(",") if x.strip()]
    return out


def _photos(paths: list[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            out += sorted(f for f in glob.glob(os.path.join(p, "*")) if f.lower().endswith(_PHOTO_EXT))
        else:
            out.append(p)
    return out


def _client(args: argparse.Namespace, required: bool = True) -> Vinted:
    try:
        return build_client(required=required, path=args.session)
    except SessionError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1)


# --- commands -----------------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> None:
    path = args.session or _cfg.SESSION_PATH
    if args.wizard:
        s = login_wizard(args.domain, path)
    elif args.cookies_file:
        text = sys.stdin.read() if args.cookies_file == "-" else open(args.cookies_file, encoding="utf-8").read()
        s = import_cookie_text(text, args.domain, path)
    else:
        s = import_browser(args.browser, args.domain, args.profile, path)
    v = build_client(path=path)
    # Never refresh imported tokens here: if the browser already rotated them, using the old
    # refresh token makes Vinted revoke the session for both the browser and us.
    v.http.on_unauthorized = None
    r = v.get("core", "/users/current")
    if r.status == 401:
        raise SessionError("the imported access token is expired. Open vinted in that browser (so it refreshes "
                           "its session) and run login again right away, or use --wizard for a separate session.")
    me = r.raise_for_error().data.get("user") or {}
    _out(args, {"ok": True, "login": me.get("login"), "id": me.get("id"), "domain": s["domain"], "source": s["source"], "session": path})


def cmd_session(args: argparse.Namespace) -> None:
    _out(args, session_info(args.session))


def cmd_whoami(args: argparse.Namespace) -> None:
    _out(args, _client(args).me(fresh=True))


def cmd_search(args: argparse.Namespace) -> None:
    v = _client(args, required=False)
    items = v.search_items(
        args.text, pages=args.pages, per_page=args.per_page, catalog_ids=_ints(args.catalog),
        brand_ids=_ints(args.brand), conditions=args.condition.split(",") if args.condition else None,
        size_ids=_ints(args.size), color_ids=_ints(args.color), attributes=_attrs(args.attr),
        price_from=args.min_price, price_to=args.max_price, order=args.order,
    )
    _items(args, items)


def cmd_filters(args: argparse.Namespace) -> None:
    v = _client(args, required=False)
    if args.code:
        _out(args, v.filter_options(args.code, args.text, _ints(args.catalog), args.find or ""))
    else:
        _out(args, v.filters(args.text, _ints(args.catalog)))


def cmd_categories(args: argparse.Namespace) -> None:
    v = _client(args, required=False)
    if args.suggest:
        _out(args, v.suggest_categories(args.suggest))
        return
    cats = v.categories()
    if args.query:
        cats = [c for c in cats if args.query.lower() in c["path"].lower()]
    _out(args, cats)


def cmd_listing_options(args: argparse.Namespace) -> None:
    v = _client(args, required=False)
    _out(args, {"attributes": v.upload_attributes(args.catalog_id).data,
                "package_sizes": v.package_sizes(args.catalog_id).data})


def cmd_brand(args: argparse.Namespace) -> None:
    _out(args, _client(args, required=False).brands(args.keyword, args.catalog))


def cmd_show(args: argparse.Namespace) -> None:
    _out(args, _client(args, required=False).item(args.item))


def cmd_user(args: argparse.Namespace) -> None:
    v = _client(args, required=False)
    if args.items:
        r = v.user_items(args.user, per_page=args.per_page).raise_for_error()
        _items(args, [item_from_api(i, v.web) for i in r.data.get("items") or []])
    elif args.feedback:
        _out(args, v.user_feedback(args.user))
    else:
        _out(args, v.user(args.user))


def cmd_my_items(args: argparse.Namespace) -> None:
    v = _client(args)
    r = v.user_items(v.my_id(), per_page=args.per_page).raise_for_error()
    _items(args, [item_from_api(i, v.web) for i in r.data.get("items") or []])


def cmd_favs(args: argparse.Namespace) -> None:
    v = _client(args)
    r = v.favourites(per_page=args.per_page).raise_for_error()
    _items(args, [item_from_api(i, v.web) for i in r.data.get("items") or []])


def cmd_fav(args: argparse.Namespace) -> None:
    _out(args, _client(args).set_favourite(args.item, not args.remove))


def cmd_follow(args: argparse.Namespace) -> None:
    _out(args, _client(args).toggle_follow(args.user))


def cmd_inbox(args: argparse.Namespace) -> None:
    _out(args, _client(args).inbox(page=args.page, per_page=args.per_page))


def cmd_conversation(args: argparse.Namespace) -> None:
    v = _client(args)
    if args.delete_message:
        _out(args, v.delete_message(args.conversation_id, args.delete_message))
    elif args.delete:
        _out(args, v.delete_conversation(args.conversation_id))
    elif args.read:
        _out(args, v.mark_conversation_read(args.conversation_id))
    else:
        _out(args, v.conversation(args.conversation_id))


def cmd_message(args: argparse.Namespace) -> None:
    v = _client(args)
    photos = _photos(args.photo) if args.photo else None
    if args.conversation:
        _out(args, v.reply(args.conversation, args.text, photo_paths=photos))
    elif args.item:
        _out(args, v.message_item(args.item, args.text, photo_paths=photos))
    else:
        raise SystemExit("error: pass --conversation <id> or --item <id>")


def cmd_offer(args: argparse.Namespace) -> None:
    v = _client(args)
    if args.offer_cmd == "send":
        _out(args, v.make_offer(args.item, args.price, transaction_id=args.transaction))
    elif args.offer_cmd == "options":
        _out(args, v.offer_options(args.item, args.price))
    elif args.offer_cmd in ("accept", "decline"):
        _out(args, v.respond_offer(args.transaction, args.offer_request, args.offer_cmd == "accept"))
    elif args.offer_cmd == "counter":
        _out(args, v.seller_offer(args.transaction, args.price))


def cmd_likers(args: argparse.Namespace) -> None:
    v = _client(args)
    if args.all:
        if not args.text:
            raise SystemExit("error: --all needs --text")
        _out(args, v.message_likers(args.text, ref=args.item, price=args.offer, discount_percent=args.discount,
                                    dry_run=not args.send))
        return
    if args.offer is not None or args.text:
        if not (args.item and args.user):
            raise SystemExit("error: --item and --user are needed to contact a liker")
        _out(args, v.offer_to_liker(args.item, args.user, args.offer, args.text))
    else:
        _out(args, v.item_likers(args.item))


def cmd_orders(args: argparse.Namespace) -> None:
    _out(args, _client(args).orders(args.kind, args.status, page=args.page))


def cmd_wallet(args: argparse.Namespace) -> None:
    v = _client(args)
    _out(args, {"balance": v.wallet_balance().data, "history": v.wallet_history().data})


def cmd_notifications(args: argparse.Namespace) -> None:
    _out(args, _client(args).notifications(per_page=args.per_page))


def cmd_searches(args: argparse.Namespace) -> None:
    v = _client(args)
    if args.save is not None:
        _out(args, v.save_search(args.save, True))
    elif args.unsave is not None:
        _out(args, v.save_search(args.unsave, False))
    else:
        _out(args, v.saved_searches())


def cmd_publish(args: argparse.Namespace) -> None:
    v = _client(args)
    photos = _photos(args.photos)
    if not photos:
        raise SystemExit("error: no photos found")
    description = args.description
    if description and description.startswith("@"):
        description = open(description[1:], encoding="utf-8").read()
    _out(args, v.publish(
        photo_paths=photos[:20], draft=args.draft, title=args.title, description=description or args.title,
        price=args.price, catalog_id=args.catalog, condition=args.condition, brand_id=args.brand_id,
        brand=args.brand, size_id=args.size, color_ids=_ints(args.colors), package_size_id=args.package_size,
        attributes=_attrs(args.attr),
    ))


def cmd_edit(args: argparse.Namespace) -> None:
    changes: dict[str, Any] = {k: getattr(args, k) for k in ("title", "price", "condition", "package_size") if getattr(args, k) is not None}
    if "package_size" in changes:
        changes["package_size_id"] = changes.pop("package_size")
    if args.description is not None:
        changes["description"] = open(args.description[1:], encoding="utf-8").read() if args.description.startswith("@") else args.description
    if args.attr:
        changes["attributes"] = _attrs(args.attr)
    if args.photos:
        changes["photo_paths"] = _photos(args.photos)
    if not changes:
        raise SystemExit("error: nothing to change")
    _out(args, _client(args).edit_item(args.item, **changes))


def cmd_hide(args: argparse.Namespace) -> None:
    _out(args, _client(args).set_hidden(args.item, not args.show))


def cmd_delete(args: argparse.Namespace) -> None:
    v = _client(args)
    _out(args, v.delete_draft(int(args.item)) if args.draft else v.delete_item(args.item))


def cmd_reserve(args: argparse.Namespace) -> None:
    v = _client(args)
    if args.undo:
        _out(args, v.unreserve(args.item) if args.item else v.reserve(args.transaction, False))
    else:
        _out(args, v.reserve(args.transaction, True))


def cmd_profile(args: argparse.Namespace) -> None:
    v = _client(args)
    if args.find_city:
        _out(args, v.cities(args.find_city))
        return
    show = None if args.show_city is None else args.show_city == "yes"
    _out(args, v.update_profile(about=args.about, city=args.city, show_city=show, photo_path=args.photo,
                                real_name=args.real_name))


def cmd_block(args: argparse.Namespace) -> None:
    _out(args, _client(args).block_user(args.user, not args.undo))


def cmd_stats(args: argparse.Namespace) -> None:
    _out(args, _client(args).item_stats(args.item))


def cmd_holiday(args: argparse.Namespace) -> None:
    _out(args, _client(args).set_holiday(args.state == "on"))


def cmd_bundle(args: argparse.Namespace) -> None:
    v = _client(args)
    if args.off:
        _out(args, v.set_bundle_discounts(False))
    elif args.tier:
        tiers = {int(k): float(p) for k, _, p in (t.partition("=") for t in args.tier)}
        _out(args, v.set_bundle_discounts(True, tiers))
    else:
        _out(args, v.bundle_discounts())


def cmd_publish_draft(args: argparse.Namespace) -> None:
    changes = {k: getattr(args, k) for k in ("price", "title") if getattr(args, k) is not None}
    _out(args, _client(args).publish_draft(args.draft_id, **changes))


def cmd_suggest_listing(args: argparse.Namespace) -> None:
    from ..automation import listing_from_photos

    _out(args, listing_from_photos(_client(args), _photos(args.photos), title=args.title, brand=args.brand,
                                   condition=args.condition, catalog_id=args.catalog))


def cmd_negotiate(args: argparse.Namespace) -> None:
    from ..automation import negotiate

    _out(args, negotiate(_client(args), args.conversation_id, floor=args.floor, steps=args.steps,
                         dry_run=not args.send))


def cmd_relist(args: argparse.Namespace) -> None:
    from ..automation import relist

    changes = {k: getattr(args, k) for k in ("price", "title") if getattr(args, k) is not None}
    _out(args, relist(_client(args), args.item, photo_paths=_photos(args.photos) if args.photos else None,
                      dry_run=not args.yes, **changes))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="vinted", description="Vinted from the terminal (no checkout).")
    ap.add_argument("--session", help="session JSON file (default ~/.config/vinted-mcp/session.json)")
    ap.add_argument("--pretty", action="store_true", help="indent JSON output")
    ap.add_argument("--version", action="version", version=f"%(prog)s {_cfg.VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.set_defaults(fn=fn)
        return p

    p = add("login", cmd_login, "import a logged-in browser session")
    p.add_argument("--browser", default="chromium", help="chromium|chrome|brave|edge|vivaldi|firefox|wizard")
    p.add_argument("--profile", help="browser profile directory name (e.g. 'Profile 1')")
    p.add_argument("--cookies-file", help="Cookie header / JSON export / cookies.txt ('-' for stdin)")
    p.add_argument("--wizard", action="store_true", help="log in in a dedicated browser window (independent session)")
    p.add_argument("--domain", help=f"vinted country domain (default {_cfg.DOMAIN})")
    add("session", cmd_session, "show the stored session (offline)")
    add("whoami", cmd_whoami, "show the signed-in member")

    p = add("search", cmd_search, "search listings")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--catalog", help="category ids, comma separated")
    p.add_argument("--brand", help="brand ids")
    p.add_argument("--condition", help=f"comma separated: {','.join(_cfg.CONDITIONS)}")
    p.add_argument("--size", help="size ids")
    p.add_argument("--color", help="color ids")
    p.add_argument("--attr", action="append", help="extra filter code=id,id (e.g. video_game_platform=1265)")
    p.add_argument("--min-price", type=float)
    p.add_argument("--max-price", type=float)
    p.add_argument("--order", default="relevance", choices=_cfg.ORDERS)
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--per-page", type=int, default=24)
    p.add_argument("--json", action="store_true")

    p = add("filters", cmd_filters, "list search filters or one filter's options")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--catalog")
    p.add_argument("--code", help="filter code, e.g. brand")
    p.add_argument("--find", help="search inside the filter options")

    p = add("categories", cmd_categories, "category tree (flat)")
    p.add_argument("query", nargs="?", help="substring of the category path")
    p.add_argument("--suggest", help="rank categories for an item description")

    p = add("listing-options", cmd_listing_options, "attributes and package sizes for a category")
    p.add_argument("catalog_id", type=int)

    p = add("brand", cmd_brand, "look up brand ids")
    p.add_argument("keyword")
    p.add_argument("--catalog", type=int)

    p = add("show", cmd_show, "item details")
    p.add_argument("item")

    p = add("user", cmd_user, "member profile / items / feedback")
    p.add_argument("user")
    p.add_argument("--items", action="store_true")
    p.add_argument("--feedback", action="store_true")
    p.add_argument("--per-page", type=int, default=40)
    p.add_argument("--json", action="store_true")

    p = add("my-items", cmd_my_items, "your listings")
    p.add_argument("--per-page", type=int, default=40)
    p.add_argument("--json", action="store_true")

    p = add("favs", cmd_favs, "your favourites")
    p.add_argument("--per-page", type=int, default=40)
    p.add_argument("--json", action="store_true")

    p = add("fav", cmd_fav, "favourite an item (idempotent)")
    p.add_argument("item")
    p.add_argument("--remove", action="store_true")

    p = add("follow", cmd_follow, "toggle following a member")
    p.add_argument("user")

    p = add("inbox", cmd_inbox, "conversations")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--per-page", type=int, default=20)

    p = add("conversation", cmd_conversation, "one conversation with messages (--read / --delete)")
    p.add_argument("conversation_id")
    p.add_argument("--read", action="store_true", help="mark it as read")
    p.add_argument("--delete", action="store_true", help="delete it from the inbox")
    p.add_argument("--delete-message", metavar="MESSAGE_ID", help="delete one of your messages (numeric entity id)")

    p = add("message", cmd_message, "send a message (text and/or --photo)")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--photo", action="append", help="image to attach (repeatable)")
    p.add_argument("--conversation")
    p.add_argument("--item", help="message the seller of this item")

    p = add("offer", cmd_offer, "offers: send|options|accept|decline|counter")
    osub = p.add_subparsers(dest="offer_cmd", required=True)
    o = osub.add_parser("send")
    o.add_argument("item")
    o.add_argument("--price", type=float, required=True)
    o.add_argument("--transaction", type=int)
    o = osub.add_parser("options")
    o.add_argument("item")
    o.add_argument("--price", type=float, required=True, help="the item's listed price")
    for name in ("accept", "decline"):
        o = osub.add_parser(name)
        o.add_argument("transaction", type=int)
        o.add_argument("offer_request", type=int)
    o = osub.add_parser("counter")
    o.add_argument("transaction", type=int)
    o.add_argument("--price", type=float, required=True)

    p = add("likers", cmd_likers, "who favourited your listings; with --user/--offer/--text, contact one")
    p.add_argument("--item")
    p.add_argument("--user")
    p.add_argument("--offer", type=float, help="price to propose")
    p.add_argument("--text", help="message to send ({login} {title} {price} {listed_price} with --all)")
    p.add_argument("--all", action="store_true", help="message every liker of your active listings (preview unless --send)")
    p.add_argument("--discount", type=float, help="with --all: offer this %% off the listed price")
    p.add_argument("--send", action="store_true", help="with --all: actually send")

    p = add("orders", cmd_orders, "purchases or sales")
    p.add_argument("kind", nargs="?", default="purchased", choices=("purchased", "sold"))
    p.add_argument("--status", default="all", choices=("all", "in_progress", "completed", "canceled"))
    p.add_argument("--page", type=int, default=1)

    add("wallet", cmd_wallet, "balance and history")

    p = add("notifications", cmd_notifications, "account notifications")
    p.add_argument("--per-page", type=int, default=20)

    p = add("searches", cmd_searches, "recent/saved searches; --save/--unsave <id>")
    p.add_argument("--save", type=int)
    p.add_argument("--unsave", type=int)

    p = add("publish", cmd_publish, "publish a listing (or --draft)")
    p.add_argument("photos", nargs="+", help="photo files or a directory")
    p.add_argument("--title", required=True)
    p.add_argument("--description", help="text or @file")
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--catalog", type=int, required=True, help="leaf category id")
    p.add_argument("--condition", required=True, choices=list(_cfg.CONDITIONS))
    p.add_argument("--brand-id", type=int)
    p.add_argument("--brand")
    p.add_argument("--size", type=int)
    p.add_argument("--colors")
    p.add_argument("--package-size", type=int)
    p.add_argument("--attr", action="append", help="code=id,id (see listing-options)")
    p.add_argument("--draft", action="store_true")

    p = add("edit", cmd_edit, "edit fields of a listing")
    p.add_argument("item")
    p.add_argument("--title")
    p.add_argument("--description")
    p.add_argument("--price", type=float)
    p.add_argument("--condition", choices=list(_cfg.CONDITIONS))
    p.add_argument("--package-size", type=int)
    p.add_argument("--attr", action="append")
    p.add_argument("--photos", nargs="+")

    p = add("profile", cmd_profile, "edit your profile (bio, city, photo) or --find-city")
    p.add_argument("--about", help="bio text")
    p.add_argument("--city", help="city name")
    p.add_argument("--show-city", choices=("yes", "no"))
    p.add_argument("--photo", help="new profile photo")
    p.add_argument("--real-name")
    p.add_argument("--find-city", metavar="NAME")

    p = add("block", cmd_block, "block a member (--undo to unblock)")
    p.add_argument("user")
    p.add_argument("--undo", action="store_true")

    p = add("stats", cmd_stats, "views, favourites and likers of one of your listings")
    p.add_argument("item")

    p = add("holiday", cmd_holiday, "holiday mode on/off")
    p.add_argument("state", choices=("on", "off"))

    p = add("bundle", cmd_bundle, "bundle discounts: show, --tier 2=5 --tier 3=10, or --off")
    p.add_argument("--tier", action="append", help="min_items=percent")
    p.add_argument("--off", action="store_true")

    p = add("publish-draft", cmd_publish_draft, "publish one of your drafts")
    p.add_argument("draft_id", type=int)
    p.add_argument("--price", type=float)
    p.add_argument("--title")

    p = add("suggest-listing", cmd_suggest_listing, "upload photos and propose category, attributes and price")
    p.add_argument("photos", nargs="+")
    p.add_argument("--title")
    p.add_argument("--brand")
    p.add_argument("--condition", choices=list(_cfg.CONDITIONS))
    p.add_argument("--catalog", type=int)

    p = add("negotiate", cmd_negotiate, "plan (or --send) the next seller move in a conversation")
    p.add_argument("conversation_id")
    p.add_argument("--floor", type=float, required=True)
    p.add_argument("--steps", type=int, default=3)
    p.add_argument("--send", action="store_true")

    p = add("relist", cmd_relist, "delete and republish a listing (preview unless --yes)")
    p.add_argument("item")
    p.add_argument("--photos", nargs="+")
    p.add_argument("--price", type=float)
    p.add_argument("--title")
    p.add_argument("--yes", action="store_true")

    p = add("hide", cmd_hide, "hide a listing (--show to unhide)")
    p.add_argument("item")
    p.add_argument("--show", action="store_true")

    p = add("delete", cmd_delete, "delete a listing (or --draft)")
    p.add_argument("item")
    p.add_argument("--draft", action="store_true")

    p = add("reserve", cmd_reserve, "reserve for a buyer's transaction (--undo to unreserve)")
    p.add_argument("--transaction", type=int)
    p.add_argument("--item")
    p.add_argument("--undo", action="store_true")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (ApiError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
