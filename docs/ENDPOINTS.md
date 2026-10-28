# Vinted web API — endpoint research

Captured on 2026-10-28 from vinted.es (Chromium + web bundle, Next.js/turbopack build).
`core` = `https://www.vinted.es/api/v2`, `edge` = `https://api.vinted.es`, `web` = `https://www.vinted.es`.
✅ = exercised live by this project, 📦 = taken from the bundle, not exercised.

## How requests work

**Which host.** Older REST resources (users, wardrobes, favourites, uploads, items, transactions, orders) live
on `core`; newer services (catalogue search, filters, search bar, messaging, notifications, wallet, pricing,
shipping estimation, item-details) on `edge`. Calling a path on the wrong host gives an HTML 404 page. Item
details have no JSON endpoint and are parsed from the `web` item page.

**Transport.** Vinted sits behind Cloudflare + DataDome: plain Python HTTP clients get a 403 HTML page, so
requests go through `curl_cffi` impersonating Chrome's TLS/HTTP2 fingerprint (`vinted_mcp/http.py`). A 403
with an HTML body is the anti-bot layer (`Response.blocked`), not the API.

**Headers on every call** (copied from the web's axios interceptors):

| Header | Value |
|---|---|
| `X-CSRF-Token` | `75f6c9fa-dc8e-4e52-a000-e09dd4084b3e` — constant baked into the JS bundle (`VINTED_CSRF_TOKEN` overrides) |
| `X-Anon-Id` | value of the `anon_id` cookie |
| `Locale` | `es-ES` (from the domain) |
| `Accept` | `application/json, text/plain, */*, image/webp` |
| `Origin` / `Referer` | `https://www.vinted.<tld>` |
| `X-Upload-Form: true` | only on `POST/PUT /item_upload/items` |

The web also sends `SIID` (a per-tab session id) and `Priority`; they are optional.

**Auth.** Cookies only: `access_token_web` (JWT, ~1 h; `sub` = user id — anonymous visitors also get one,
without `sub`) and `refresh_token_web` (~7 d, rotated on every refresh). Anti-bot cookies kept with them:
`datadome`, `cf_clearance`, `__cf_bm`, plus `_vinted_fr_session`, `anon_id`, `v_*`.

**Refresh / recovery.** `POST web/web/api/auth/refresh` (cookies only, no body) returns new tokens as
Set-Cookie. Replaying an already-rotated refresh token makes Vinted revoke the whole session, which bites when
the library and the browser it was imported from share a session. On a `401` the client therefore (1) re-reads
the source browser's cookies and adopts a newer valid access token without refreshing, (2) else refreshes with
its own token, (3) else retries with the browser's rotated refresh token
(`vinted_mcp/session.py: recover_session`). `vinted login --wizard` gives the library its own session instead.

**Responses.** JSON. `core` reports business errors as HTTP 200/4xx with `{"code": <non-zero>, "message",
"message_code", "errors": [{field, value}]}` (e.g. `99 validation_error`, `104 not_found`, `106
access_denied`); `edge` uses `{"code": "INVALID_INPUT"|"FORBIDDEN"|…, "message"}`. `Response.ok` is false for
any non-zero `code`.

**Retries and pacing.** Only `GET`s are retried (network errors, 429/502/503/504, honouring `Retry-After` up to
30 s). Writes are sent once; a write that dies mid-flight raises `UnknownOutcomeError` because Vinted may have
applied it. Requests are spaced by `VINTED_RATE_PACE` (0.6 s).

**Photo upload.** `POST core/photos` as `multipart/form-data` with `photo[type]=item`,
`photo[temp_uuid]=<upload session uuid>`, `photo[file]=<jpeg/png>`; returns `{id}`. All photos of one listing
share the same uuid, which is then sent as `item.temp_uuid` and `upload_session_id`. The same endpoint
takes `photo[type]=user` (profile photo → `temp_uuid` → `user.photo_temp_uuid`) and `photo[type]=user_msg`
(chat photo → `temp_uuid` → `photo_temp_uuids` of a reply), without `photo[temp_uuid]`.

## Auth

| ✅ | `POST web/web/api/auth/refresh` | new `access_token_web`/`refresh_token_web` via Set-Cookie |
|---|---|---|
| 📦 | `POST web/web/api/auth/oauth` | `{client_id:"web", scope:"user", grant_type:"password"|"assertion"|"refresh_token", ...}` |

## Account / users (core)

| ✅ | `GET /users/current` | signed-in user |
|---|---|---|
| ✅ | `GET /users/{id}` | profile |
| ✅ | `GET /wardrobe/{id}/items?page&per_page&order` | listings (own: incl. hidden, drafts, `item_alert_type`) |
| ✅ | `GET /user_feedbacks?user_id&page&per_page`, `GET /user_feedbacks/summary?user_id` | reviews |
| ✅ | `GET /users/{id}/followers?page` | followers |
| ✅ | `POST /followed_users/toggle {user_id}` | → `{is_followed}` |
| ✅ | `GET /users/{me}/items/favourites?page&per_page` | favourites |
| ✅ | `POST /user_favourites/toggle {type:"item", user_favourites:[id]}` | toggle |
| ✅ | `GET /my_orders?type=purchased|sold&status=all&page&per_page` | orders |
| ✅ | `GET /transactions/{id}` | deal: item, buyer, seller, `offer_id`, shipment |
| ✅ | `PUT /users/current {user: {real_name, city_id, country_id, gender, birthday, text_about, login, photo_temp_uuid, user_settings: {is_location_public, on_holiday, international_trading_enabled}}}` | profile update; holiday mode on = `on_holiday: true` (send every field back, it rewrites the profile) |
| ✅ | `DELETE /users/{id}/holiday` | holiday mode off |
| ✅ | `PUT /users/current` (same body) | profile edit: `text_about` (bio), `city_id`, `real_name`, `user_settings.is_location_public`, `photo_temp_uuid` (avatar) |
| ✅ | `GET /countries/{country_id}/cities?search_text` | city ids for the profile |
| ✅ | `POST /users/{me}/user_hates {hated_user_id}` / `DELETE /users/{me}/user_hates?hated_user_id=` | block / unblock (`users/{id}.is_hated`) |

## Catalogue (edge)

| ✅ | `GET /svc-catalogue/items?search_text&page&per_page&order&price_from&price_to&currency&attribute_ids[catalog]=…&attribute_ids[status]=…&attribute_ids[brand]=…` | search |
|---|---|---|
| ✅ | `GET /svc-filters/filters?search_text&attribute_ids[...]` | filter list |
| ✅ | `GET /svc-filters/filters/facets?filter_code=…` / `/svc-filters/filters/search?filter_search_code&filter_search_text` | filter options |
| ✅ | `GET /svc-filters/faceted_categories?search_text&attribute_ids[catalog]` | category counts (drill down) |
| ✅ | `GET /search-bar/v2/suggestions?search_text` | autocomplete |
| ✅ | `GET /search-bar/v2/users/{me}/previous_searches` | recent/saved searches |
| 📦 | `PUT /search-bar/v2/users/{me}/previous_searches/{id}/save|unsave`, `POST .../previous_searches` | saved-search alerts |
| ✅ | `GET /item-details/more-items/{id}?content_source=similar_items|other_user_items&screen=item` | related |
| ✅ | `GET core/items/{id}/shipping_details` | "Envío desde": `price`, `free_shipping`, `pickup_only`, `multiple_shipping_options_available`, `discount` |
| ✅ | `GET edge/pricing/public/api/v1/items/{id}/services` | buyer protection fee breakdown |
| ✅ | `GET web/items/{id}` (HTML) | item details in `self.__next_f` flight data, `plugins` array + ld+json |

Order values: `relevance`, `newest_first`, `price_low_to_high`, `price_high_to_low`.
Status (condition) ids: 6 new with tags, 1 new without tags, 2 very good, 3 good, 4 satisfactory.

## Messaging (edge)

| ✅ | `GET /messaging/main/inbox?page&per_page` | conversations (per_page ignored, 20) |
|---|---|---|
| ✅ | `GET /messaging/main/conversations/{uuid}` | messages, `data.transaction_id`, `current_user_side` |
| ✅ | `GET /messaging/main/users/unread_count` | |
| ✅ | `POST /messaging/main/inquiries {item_ids:["id"], receiver_id:"id"}` | → `{conversation_id, transaction_id}` (strings!) |
| ✅ | `POST /messaging/main/conversations/{uuid}/replies {content|null, is_personal_data_sharing_check_skipped:false, photo_temp_uuids:[…]|null}` | send text and/or photos |
| ✅ | `POST /messaging/main/conversations/{uuid}/mark_as_read` | mark read |
| ✅ | `DELETE /messaging/main/conversations/{uuid}/replies/{entity_id}` | delete own message; `entity_id` = numeric `messages[].data.entity.id` (the message uuid → 400 `invalid message ID`) |
| ✅ | `DELETE /messaging/main/conversations/{uuid}` | delete conversation (only `is_deletable`) |
| ✅ | `GET /inbox-notifications/v1/notifications?page&per_page` | notifications (`entry_type` 20 = someone favourited your item, 300 = a favourite sold) |
| ✅ | `GET /inbox-notifications/v1/notifications/unread_count` | `{count}` |
| ✅ | `GET /ledger/wallet/balance`, `GET /wallet/history/overview` | wallet |
| ✅ | `GET /messaging/bundles/discounts/settings` | bundle discounts `{enabled, tiers:[{discount_value, min_items}]}` |
| ✅ | `PUT /messaging/bundles/discounts/settings {enabled, tiers:[{discount_value:"0.05", min_items:2}, …]}` | set them; Vinted keeps tiers for 2, 3 and 5 items and fills missing ones |
| ✅ | `GET edge/vas/items/{id}/bumps/stats?timezone` | daily views/impressions, only for bumped items (404 otherwise) |

Who favourited your items: no endpoint. Notifications with `entry_type` 20 ("X ha marcado como favorito tu
artículo") carry `link=…messaging?item_id=…&user_id=…`; the seller can then `POST /messaging/main/inquiries`
with `receiver_id` = that user to open the conversation and `POST /transactions/{tid}/offers` to offer a price.

Message `data.entity_type`: `message`, `offer_request_message` (buyer offer: `offer_request_id`, `status`
10 pending / 30 rejected, `transaction_id`), `offer_message` (seller offer).

## Offers (core)

| ✅ | `POST /offers/request_options {price:{amount,currency_code}, item_ids:[id], seller_id}` | min/max price, suggestions |
|---|---|---|
| ✅ | `POST /transactions/{tid}/offer_requests {offer_request:{price:"35", currency:"EUR"}}` | buyer offer |
| ✅ | `PUT /transactions/{tid}/offer_requests/{oid}/accept|reject` | seller answers (status 20 accepted / 30 rejected) |
| ✅ | `POST /transactions/{tid}/offers {offer:{price, currency}}` | seller offer / counter |
| ✅ | `PUT /transactions/{tid}/reservation {reserved}`, `DELETE /items/{id}/reservation` | reserve for that buyer (5 days) / unreserve |
| ⛔ | `POST /purchases/checkout/build`, `PUT /purchases/{id}/checkout` | checkout — deliberately not implemented |

## Listings (core unless noted)

| ✅ | `GET /item_upload/catalogs` | category tree |
|---|---|---|
| ✅ | `GET /item_upload/catalogs/search?keyword` | categories by *name* |
| ✅ | `POST /item_upload/attributes {attributes:[{code:"category", value:[id]}]}` | attributes for a category |
| ✅ | `GET /item_upload/brands?keyword&category_id`, `GET /item_upload/colors` | |
| ✅ | `GET edge/shipping-estimation/external/catalogs/{cid}/package_sizes?upload_session_id` | package sizes |
| ✅ | `POST /photos` multipart `photo[type]=item`, `photo[temp_uuid]=<session uuid>`, `photo[file]` | → `{id}` |
| ✅ | `POST /item_upload/items {item, push_up:false, parcel:null, upload_session_id}` + `X-Upload-Form: true` | publish |
| ✅ | `POST /item_upload/drafts {draft, parcel, upload_session_id}` / `DELETE /item_upload/drafts/{id}` | drafts |
| ✅ | `GET /item_upload/items/{id}` | editable state (404 while under review) |
| ✅ | `PUT /item_upload/items/{id}` (same body, `item.id` set, photos = item photo ids, or new `/photos` ids to replace them) | edit |
| ✅ | `PUT /items/{id}/is_hidden {is_hidden}` | hide/show (400 while under review) |
| ✅ | `POST /items/{id}/delete` | delete (403 `access_denied` while under review) |
| ✅ | `POST /item_upload/drafts/{id}/completion {draft, push_up:false, parcel:null, upload_session_id}` | publish a draft (draft = same shape as `item`, `id` set) |
| ✅ | `POST /item_upload/suggestions/categories {image_metadata:[{image_id:<photo.high_resolution.id>, orientation:"0"}], upload_session_id}` | categories guessed from photos → `{categories, prefill_categories}` |
| ✅ | `POST /item_upload/suggestions/attributes {title, description, category_id, suggested_category_id, image_metadata, upload_session_id, attributes}` | brand/colour/model suggestions |
| ✅ | `POST edge/upload-form-suggestions/item_price_suggestions {category_id, brand_id, condition_id, model_metadata, image_metadata, attributes}` | price recommendation min/mid/max. Numeric ids here, string ids inside `attributes` |
| ✅ | `POST edge/upload-form-suggestions/item_upload/items/similar_sold_items {…same, price_from, price_to, currency_code, upload_session_id}` | comparable sold items |
| 📦 | `POST /vas/bumps/orders` | paid bump — not implemented (payment) |

`item` body:

```json
{"id": null, "currency": "EUR", "temp_uuid": "<uuid>", "title": "…", "description": "…",
 "brand_id": 77600, "brand": "Nintendo", "catalog_id": 3025, "isbn": null, "ai_photo": false,
 "price": "450", "package_size_id": 8, "shipment_prices": {"domestic": null, "international": null},
 "color_ids": [], "assigned_photos": [{"id": 1, "orientation": 0, "ai_detected": false,
 "digital_source_type": [], "c2pa_read_error": "other"}], "measurement_length": null,
 "measurement_width": null, "item_attributes": [{"code": "video_game_platform", "ids": [1265]},
 {"code": "condition", "ids": [3]}], "manufacturer": null, "manufacturer_labelling": null}
```
