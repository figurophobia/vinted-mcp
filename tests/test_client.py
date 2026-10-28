from __future__ import annotations

import pytest

from vinted_mcp.client import Vinted, condition_id
from vinted_mcp.http import ApiError, Response

from .conftest import FakeTransport, ok


def test_search_uses_edge_host_and_attribute_ids(client: Vinted, fake: FakeTransport) -> None:
    fake.routes[("GET", "/svc-catalogue/items")] = ok({"items": [], "pagination": {"total_pages": 1}})
    client.search_items("3ds", catalog_ids=[3025], conditions=["good", "very_good"], brand_ids=[77600],
                        attributes={"video_game_platform": [1265]}, price_to=80, order="price_low_to_high")
    call = fake.calls[0]
    assert call["url"] == "https://api.vinted.es/svc-catalogue/items"
    p = call["params"]
    assert p["attribute_ids[catalog]"] == "3025"
    assert p["attribute_ids[status]"] == "3,2"
    assert p["attribute_ids[brand]"] == "77600"
    assert p["attribute_ids[video_game_platform]"] == "1265"
    assert p["price_to"] == 80 and p["currency"] == "EUR"
    assert p["order"] == "price_low_to_high"


def test_search_rejects_unknown_order(client: Vinted) -> None:
    with pytest.raises(ValueError):
        client.search("x", order="cheapest")


def test_search_items_follows_pages(client: Vinted, fake: FakeTransport) -> None:
    def page(call):
        n = call["params"]["page"]
        return ok({"items": [{"id": n, "title": f"t{n}", "price": {"amount": "1.0", "currency_code": "EUR"}}],
                   "pagination": {"total_pages": 2}})

    fake.routes[("GET", "/svc-catalogue/items")] = page
    items = client.search_items("x", pages=5)
    assert [i.id for i in items] == [1, 2]
    assert items[0].price == "1.0" and items[0].currency == "EUR"


def test_condition_ids() -> None:
    assert condition_id("new_with_tags") == 6
    assert condition_id("good") == 3
    assert condition_id("4") == 4
    with pytest.raises(ValueError):
        condition_id("mint")


def test_publish_uploads_photos_then_posts_item(client: Vinted, fake: FakeTransport, tmp_path) -> None:
    photos = []
    for n in range(2):
        f = tmp_path / f"p{n}.jpg"
        f.write_bytes(b"\xff\xd8\xff")
        photos.append(str(f))
    counter = iter([101, 102])
    fake.routes[("POST", "/api/v2/photos")] = lambda call: ok({"id": next(counter), "code": 0})
    fake.routes[("POST", "/api/v2/item_upload/items")] = ok({"item": {"id": 999}, "code": 0})
    r = client.publish(photo_paths=photos, title="3DS", description="d", price=450, catalog_id=3025,
                       condition="good", brand_id=77600, brand="Nintendo", package_size_id=8,
                       attributes={"video_game_platform": [1265]})
    assert r.ok and r.data["item"]["id"] == 999 and r.data["photo_ids"] == [101, 102]
    post = fake.calls[-1]
    assert post["headers"] == {"X-Upload-Form": "true"}
    body = post["json"]
    item = body["item"]
    assert body["upload_session_id"] == item["temp_uuid"]
    assert item["price"] == "450" and item["catalog_id"] == 3025 and item["id"] is None
    assert [p["id"] for p in item["assigned_photos"]] == [101, 102]
    assert {"code": "condition", "ids": [3]} in item["item_attributes"]
    assert {"code": "video_game_platform", "ids": [1265]} in item["item_attributes"]


def test_publish_draft_endpoint(client: Vinted, fake: FakeTransport, tmp_path) -> None:
    f = tmp_path / "p.jpg"
    f.write_bytes(b"x")
    fake.routes[("POST", "/api/v2/photos")] = ok({"id": 1, "code": 0})
    fake.routes[("POST", "/api/v2/item_upload/drafts")] = ok({"draft": {"id": 5}, "code": 0})
    r = client.publish(photo_paths=[str(f)], draft=True, title="t", description="d", price=1, catalog_id=1,
                       condition="good")
    assert r.ok and "draft" in fake.calls[-1]["json"]


def test_publish_keeps_uploaded_photo_ids_on_failure(client: Vinted, fake: FakeTransport, tmp_path) -> None:
    files = []
    for n in range(2):
        f = tmp_path / f"{n}.jpg"
        f.write_bytes(b"x")
        files.append(str(f))
    answers = iter([ok({"id": 7, "code": 0}), Response(500, {"message": "boom"})])
    fake.routes[("POST", "/api/v2/photos")] = lambda call: next(answers)
    r = client.publish(photo_paths=files, title="t", description="d", price=1, catalog_id=1, condition="good")
    assert not r.ok and r.data["uploaded_photo_ids"] == [7]


def test_edit_item_merges_current_state(client: Vinted, fake: FakeTransport) -> None:
    fake.routes[("GET", "/api/v2/item_upload/items/55")] = ok({"code": 0, "item": {
        "id": 55, "title": "old", "description": "desc", "catalog_id": 3025, "brand_id": 77600,
        "brand_dto": {"title": "Nintendo"}, "price": {"amount": "450.0", "currency_code": "EUR"},
        "package_size_id": 8, "currency": "EUR", "photos": [{"id": 11}, {"id": 12}],
        "item_attributes": [{"code": "video_game_platform", "ids": [1265]}, {"code": "condition", "ids": [3]}],
    }})
    fake.routes[("PUT", "/api/v2/item_upload/items/55")] = ok({"item": {"id": 55}, "code": 0})
    r = client.edit_item(55, price=465, title="new")
    assert r.ok
    item = fake.calls[-1]["json"]["item"]
    assert item["id"] == 55 and item["title"] == "new" and item["price"] == "465"
    assert item["description"] == "desc" and item["brand"] == "Nintendo"
    assert [p["id"] for p in item["assigned_photos"]] == [11, 12]
    assert {"code": "condition", "ids": [3]} in item["item_attributes"]
    assert {"code": "video_game_platform", "ids": [1265]} in item["item_attributes"]


def test_edit_item_rejects_unknown_fields(client: Vinted, fake: FakeTransport) -> None:
    fake.routes[("GET", "/api/v2/item_upload/items/1")] = ok({"code": 0, "item": {"id": 1, "price": "2", "item_attributes": []}})
    with pytest.raises(ValueError):
        client.edit_item(1, colour="red", condition="good")


def test_message_item_creates_inquiry_with_string_ids(client: Vinted, fake: FakeTransport) -> None:
    fake.routes[("POST", "/messaging/main/inquiries")] = ok({"conversation_id": "c1", "transaction_id": "77"}, 201)
    fake.routes[("POST", "/messaging/main/conversations/c1/replies")] = ok({"id": "m1"}, 201)
    client.start_conversation(1234567891, seller_id=555000111)
    assert fake.calls[-1]["json"] == {"item_ids": ["1234567891"], "receiver_id": "555000111"}
    assert fake.calls[-1]["url"].startswith("https://api.vinted.es/")
    r = client.reply("c1", "hola")
    assert r.ok and fake.calls[-1]["json"]["content"] == "hola"


def test_make_offer_opens_conversation_for_transaction(client: Vinted, fake: FakeTransport) -> None:
    page = ('<script>self.__next_f.push([1,"{\\"plugins\\":[{\\"name\\":\\"ask_seller\\",'
            '\\"data\\":{\\"seller_id\\":\\"555000111\\"}}]}"])</script>')
    fake.routes[("GET", "/items/9")] = Response(200, page)
    fake.routes[("POST", "/messaging/main/inquiries")] = ok({"conversation_id": "c", "transaction_id": "77"})
    fake.routes[("POST", "/api/v2/transactions/77/offer_requests")] = ok({"offer_request": {"id": 1}, "code": 0})
    r = client.make_offer(9, 35)
    inquiry = next(c for c in fake.calls if c["url"].endswith("/messaging/main/inquiries"))
    assert inquiry["json"] == {"item_ids": ["9"], "receiver_id": "555000111"}
    assert r.ok and fake.calls[-1]["json"] == {"offer_request": {"price": "35", "currency": "EUR"}}
    assert r.data["transaction_id"] == 77


def test_respond_offer_paths(client: Vinted, fake: FakeTransport) -> None:
    fake.routes[("PUT", "/accept")] = ok({"code": 0})
    fake.routes[("PUT", "/reject")] = ok({"code": 0})
    client.respond_offer(1, 2, True)
    client.respond_offer(1, 2, False)
    assert fake.calls[0]["url"].endswith("/transactions/1/offer_requests/2/accept")
    assert fake.calls[1]["url"].endswith("/transactions/1/offer_requests/2/reject")


def test_set_favourite_is_idempotent(client: Vinted, fake: FakeTransport) -> None:
    fake.routes[("GET", "/users/42/items/favourites")] = ok({"items": [{"id": 5}], "pagination": {"total_pages": 1}})
    r = client.set_favourite(5, True)
    assert r.data["unchanged"] is True
    assert not any(c["url"].endswith("/user_favourites/toggle") for c in fake.calls)
    fake.routes[("POST", "/user_favourites/toggle")] = ok({"code": 0})
    r = client.set_favourite(5, False)
    assert fake.calls[-1]["json"] == {"type": "item", "user_favourites": [5]}


def test_business_error_code_is_not_ok() -> None:
    r = Response(200, {"code": 99, "message": "Se han encontrado errores", "message_code": "validation_error"})
    assert not r.ok and "validation_error" in r.error_reason
    with pytest.raises(ApiError):
        r.raise_for_error()


def test_antibot_block_is_reported() -> None:
    r = Response(403, "<html>blocked</html>")
    assert r.blocked and "anti-bot" in r.error_reason


def test_refresh_normalizes_auth_cookies(client: Vinted, fake: FakeTransport) -> None:
    fake.routes[("POST", "/web/api/auth/refresh")] = Response(200, {}, set_cookies={"access_token_web": "new"})
    assert client.refresh() is True
    assert fake.cookie("access_token_web") == "new"


def test_writes_need_login(fake: FakeTransport) -> None:
    fake._cookies = {}
    v = Vinted(domain="es", transport=fake)
    with pytest.raises(ApiError):
        v.delete_item(1)


def test_categories_flatten() -> None:
    fake = FakeTransport({("GET", "/item_upload/catalogs"): ok({"code": 0, "catalogs": [
        {"id": 1, "title": "Electrónica", "catalogs": [{"id": 3025, "title": "Consolas", "catalogs": []}]}]})})
    v = Vinted(domain="es", transport=fake)
    cats = v.categories()
    assert cats == [
        {"id": 1, "title": "Electrónica", "path": "Electrónica", "leaf": False},
        {"id": 3025, "title": "Consolas", "path": "Electrónica > Consolas", "leaf": True},
    ]


def test_item_likers_from_notifications(client: Vinted, fake: FakeTransport) -> None:
    fake.routes[("GET", "/inbox-notifications/v1/notifications")] = ok({"code": 0, "notifications": [
        {"entry_type": 20, "body": 'buyer1 ha marcado como favorito tu artículo "3DS".', "subject_id": 7,
         "link": "vintedfr://messaging?item_id=7&user_id=555000111&portal=fr", "updated_at": "t2"},
        {"entry_type": 20, "body": "buyer1 ha marcado...", "link": "vintedfr://messaging?item_id=7&user_id=555000111"},
        {"entry_type": 300, "body": "Tu artículo favorito X se ha vendido."},
    ]})
    likers = client.item_likers()
    assert likers == [{"item_id": 7, "user_id": 555000111, "login": "buyer1", "at": "t2",
                       "text": 'buyer1 ha marcado como favorito tu artículo "3DS".'}]
    assert client.item_likers(8) == []


def test_offer_to_liker(client: Vinted, fake: FakeTransport) -> None:
    fake.routes[("POST", "/messaging/main/inquiries")] = ok({"conversation_id": "c", "transaction_id": "77"})
    fake.routes[("POST", "/messaging/main/conversations/c/replies")] = ok({"data": {"id": "m"}}, 201)
    fake.routes[("POST", "/api/v2/transactions/77/offers")] = ok({"offer": {"id": 9}, "code": 0})
    r = client.offer_to_liker(7, 555000111, 410, "¡Te la dejo en 410!")
    assert r.ok and r.data["offer"] == {"id": 9} and r.data["message_id"] == "m"
    assert fake.calls[0]["json"] == {"item_ids": ["7"], "receiver_id": "555000111"}
    assert fake.calls[-1]["json"] == {"offer": {"price": "410", "currency": "EUR"}}


def test_delete_message_path(client: Vinted, fake: FakeTransport) -> None:
    fake.routes[("DELETE", "/messaging/main/conversations/c/replies/73496224709")] = ok({"code": 0})
    assert client.delete_message("c", "73496224709").ok
    assert fake.calls[-1]["url"] == "https://api.vinted.es/messaging/main/conversations/c/replies/73496224709"


def _likers_setup(fake: FakeTransport) -> None:
    fake.routes[("GET", "/wardrobe/42/items")] = ok({"code": 0, "items": [
        {"id": 7, "title": "3DS XL", "price": {"amount": "500.0", "currency_code": "EUR"}},
        {"id": 8, "title": "Oculta", "price": {"amount": "100.0"}, "is_hidden": True},
    ]})
    fake.routes[("GET", "/inbox-notifications/v1/notifications")] = ok({"code": 0, "notifications": [
        {"entry_type": 20, "body": "ana ha marcado...", "link": "x://m?item_id=7&user_id=1"},
        {"entry_type": 20, "body": "luis ha marcado...", "link": "x://m?item_id=7&user_id=2"},
        {"entry_type": 20, "body": "eva ha marcado...", "link": "x://m?item_id=8&user_id=3"},
        {"entry_type": 20, "body": "old ha marcado...", "link": "x://m?item_id=99&user_id=4"},
    ]})


def test_message_likers_dry_run_plans_offers(client: Vinted, fake: FakeTransport) -> None:
    _likers_setup(fake)
    plan = client.message_likers("Hola {login}, {title} para ti por {price} € (antes {listed_price})",
                                 discount_percent=10, dry_run=True)
    assert [(p["login"], p["offer_price"]) for p in plan] == [("ana", 450.0), ("luis", 450.0)]
    assert plan[0]["message"] == "Hola ana, 3DS XL para ti por 450 € (antes 500)"
    assert not any(c["method"] == "POST" for c in fake.calls)


def test_message_likers_sends_and_reports_each(client: Vinted, fake: FakeTransport) -> None:
    _likers_setup(fake)
    answers = iter([ok({"conversation_id": "c1", "transaction_id": "71"}), Response(403, {"message": "nope"})])
    fake.routes[("POST", "/messaging/main/inquiries")] = lambda call: next(answers)
    fake.routes[("POST", "/messaging/main/conversations/c1/replies")] = ok({"data": {"id": "m"}}, 201)
    fake.routes[("POST", "/api/v2/transactions/71/offers")] = ok({"offer": {"id": 5}, "code": 0})
    plan = client.message_likers("hola {login}", price=480)
    assert [p["ok"] for p in plan] == [True, False]
    assert plan[0]["conversation_id"] == "c1" and "nope" in plan[1]["error"]


def test_item_includes_shipping(client: Vinted, fake: FakeTransport) -> None:
    fake.routes[("GET", "/items/9")] = Response(200, "<html></html>")
    fake.routes[("GET", "/items/9/shipping_details")] = ok({"code": 0, "shipping_details": {
        "pickup_only": False, "multiple_shipping_options_available": True, "free_shipping": False,
        "discount": None, "price": {"amount": 3.85, "currency_code": "EUR"}}})
    d = client.item(9)
    assert d["shipping"] == {"from_price": "3.85", "currency": "EUR", "free": False, "pickup_only": False,
                             "multiple_options": True}
