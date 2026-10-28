# AGENTS.md — driving vinted-mcp from an AI agent

MCP server + CLI + Python library for Vinted, **one codebase**: the MCP
server and the CLI both call `vinted_mcp.Vinted`, so a fix lands once.

- **[Flow A — buy & negotiate](#flow-a--buy--negotiate)**: search, ask, make offers.
- **[Flow B — sell](#flow-b--sell)**: publish, edit, hide, answer buyers and offers.

There is **no checkout/payment tool**: when a deal is agreed the user pays by hand.

## Interfaces

| | MCP server | CLI | Library |
|---|---|---|---|
| Entry | `vinted-mcp` / `python -m vinted_mcp.mcp_server` | `vinted` | `import vinted_mcp` |
| Writes | need `--allow-writes` | always available | always available |

Ad-hoc call: `v = vinted_mcp.build_client(); v.get("core"|"edge", path, params)` /
`v.send(method, "core"|"edge", path, body)`.

## Login / session

The tools never log in. Best: `vinted login --wizard` (dedicated window, independent session).
A session imported from a browser is kept in sync with it: on a 401 the client adopts the browser's newer
token, else refreshes, else retries with the browser's rotated refresh token (`session.recover_session`).
Import once with:
`vinted login --browser chromium` (or `chrome|brave|edge|vivaldi|firefox`,
`--profile`), or `vinted login --cookies-file cookies.txt` (Cookie header, JSON
export or Netscape format). Headless: set `VINTED_COOKIES="access_token_web=…; refresh_token_web=…"`.
Check offline with `vinted session` / `session_status`. Token refresh is automatic.

## Flow A — buy & negotiate

1. **Find the category / filters** — `suggest_category("nintendo 3ds")` → leaf ids
   (consoles `3025`); `search_filters(text, catalog_ids, filter_code="brand", filter_search="ninten")` → option ids.
2. **Search** — `search(text, catalog_ids=[3025], conditions=["good"], price_to=80, order="price_low_to_high")`.
   Extra filters by code: `attributes={"video_game_platform": [1265]}`.
3. **Inspect** — `get_item(id)` (description, photos, attributes, seller, `can_buy`, `is_reserved`,
   `shipping.from_price` = the "Envío desde" price; `search(..., include_shipping=true)` adds it to results),
   `get_user(seller_id, include_feedback=True)`, `similar_items(id)`.
4. **Ask** — `send_message(text, item=id)` creates the conversation; later `send_message(text, conversation_id=…)`.
5. **Offer** — `offer_options(item, listed_price)` gives `min_price`/suggestions; `make_offer(item, price)`
   (pass `transaction_id` from the conversation to skip re-opening it). Not a purchase.
6. **Negotiate** — `get_offer_status(conversation_id)`: `buyer_offer` events carry `status`
   (pending/accepted/rejected); a `seller_offer` is the seller's counter. Counter again with `make_offer`.
   There is no "accept" for a seller offer: on Vinted the buyer accepts it by pressing *Buy* with that
   offer (`/purchases/checkout/build`), i.e. the purchase, which the user does by hand.

## Flow B — sell

1. **Prepare** — `suggest_category(text)` → leaf `catalog_id`; `listing_options(catalog_id)` → required
   attributes (e.g. `video_game_platform`, `condition`) and `package_sizes` (`8` = 5 kg, `9` = 10 kg, `10` = 20 kg
   for consoles); `find_brand("nintendo", catalog_id)` → `brand_id`.
2. **Publish** — `publish_item(title, description, price, catalog_id, condition, photo_paths, brand_id, brand,
   package_size_id, attributes={"video_game_platform": [1265]})` (`draft=True` for a draft).
   New listings are briefly hidden while processed; check `my_items`.
3. **Edit** — `edit_item(item, price=…, title=…)` changes only what you pass. Vinted re-reviews edits:
   `status.item_alert_type = "delayed_publication"` (hidden, not editable/hideable) for a while.
4. **Lifecycle** — `set_item_hidden(item, true|false)`, `reserve_item(transaction_id=…)` /
   `reserve_item(item=…, reserved=false)`, `delete_item(item)`, `delete_draft(id)`.
5. **Buyers** — `list_inbox`, `get_conversation`, `send_message(text, conversation_id=…)`.
   Offers: `get_offer_status` → `respond_offer(transaction_id, offer_request_id, accept)` or
   `counter_offer(transaction_id, price)`.
6. **Interested buyers** — `list_item_likers()` (who favourited what; rebuilt from like notifications, the API has
   no endpoint for it), `offer_to_liker(item, user, price, message)` for one, or
   `message_all_likers(message, discount_percent=10)` for all of them (`dry_run=true` by default: review the plan,
   then call again with `dry_run=false`). Placeholders: `{login}`, `{title}`, `{price}`, `{listed_price}`.

## MCP tool reference

**Reads (25):** `session_status`, `whoami`, `search`, `search_filters`, `suggest`, `list_categories`,
`suggest_category`, `find_brand`, `listing_options`, `get_item`, `similar_items`, `get_user`, `my_items`,
`list_favourites`, `list_inbox`, `get_conversation`, `get_offer_status`, `list_item_likers`, `unread_counts`,
`list_notifications`, `list_orders`, `get_transaction`, `wallet`, `list_saved_searches`, `offer_options`.

**Writes (18, `--allow-writes`):** `set_favourite`, `toggle_follow`, `send_message`, `make_offer`,
`respond_offer`, `counter_offer`, `offer_to_liker`, `message_all_likers`, `mark_conversation_read`,
`delete_message`, `delete_conversation`, `publish_item`, `edit_item`, `set_item_hidden`, `reserve_item`,
`delete_item`, `delete_draft`, `save_search`.

## API quirks (handled by the library)

- **Two hosts.** `www.vinted.<tld>/api/v2` (core) vs `api.vinted.<tld>` (edge: search, filters, messaging,
  notifications, wallet). Wrong host = HTML 404.
- **Anti-bot.** Plain HTTP clients get 403 HTML; `curl_cffi` with a Chrome fingerprint passes. A 403 with an
  HTML body is reported as `blocked` — re-import the browser session.
- **CSRF.** Every call carries the constant `X-CSRF-Token` baked into the web bundle (`VINTED_CSRF_TOKEN`
  overrides it) plus `X-Anon-Id` (the `anon_id` cookie).
- **Search filters** go in `attribute_ids[<code>]=id,id`; `catalog_ids`/`status_ids` query params are ignored.
- **Edge is strict about types**: `item_ids`/`receiver_id` must be strings in `/messaging/main/inquiries`.
- **Business errors on 200**: core answers `{"code": 99, "message_code": "validation_error"}`; `Response.ok`
  treats any non-zero `code` as failure.
- **Duplicates.** Re-listing an item whose photos are already in the catalogue gets it deleted ("anuncios
  duplicados") and repeated cases can restrict publishing.
- **Item details** have no JSON endpoint: they come from the item page's Next.js flight data (`plugins`).

## File map

- `vinted_mcp/client.py` — API client. `vinted_mcp/http.py` — curl_cffi transport, pacing, retries, 401 refresh.
- `vinted_mcp/session.py` — browser cookie import (Chromium decryption, Firefox) and `build_client`.
- `vinted_mcp/models.py` — `Item`, item-page parsing, offer events. `vinted_mcp/mcp_server.py` — MCP tools.
- `vinted_mcp/cli/main.py` — `vinted` CLI. `tests/` — offline pytest suite. `docs/ENDPOINTS.md` — raw endpoints.
