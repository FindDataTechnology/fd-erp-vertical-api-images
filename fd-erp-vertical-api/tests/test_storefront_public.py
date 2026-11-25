"""店面公开读面测试（change establish-ai-native-ecommerce-stack 0.3）。

判据（对应 spec `ecommerce-storefront` 的三条 requirement）：
1. 匿名可读：不带任何身份头时 200；
2. 只出零售渠道商品：下架 / 无零售价 / 非该渠道可见的商品一律不出现；
3. 不泄露内部口径：响应不含 entity_id / supplier_id / 成本价 / B2B 客户专属价。

**本测试不连数据库**：店面的两个读面只走主数据（JSONL 事件档）与取价规则簿，
不碰内核表。因此它能在任何机器上跑（含 CI），与 API 套件里那批需要
`erp_test_b` 的用例不同——那些在本机没有隧道时是 skip/error，会掩盖回归。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from fd_erp_vertical_api.app import create_app
from fd_erp_vertical_api.cart import CartStore
from fd_erp_vertical_api.config import Settings

TENANT = "api-e2e"
CHANNEL = "retail-online"
RETAIL_DAY = {"valid_from": "2026-01-01", "valid_to": "2099-12-31"}


def _retail_rule(*, product="*", channel="*", price="99.00"):
    return {"layer": "retail",
            "scope": {"product_code": product, "channel_id": channel},
            "price": price, **RETAIL_DAY,
            "rate_source": "retail/price-list/v1"}


@pytest.fixture()
def shop(tmp_path):
    """店面客户端 + 主数据种子。返回 (client, master, pricing)。"""
    settings = Settings(tenant_id=TENANT, data_dir=str(tmp_path / "data"),
                        retail_channel=CHANNEL)
    client = TestClient(create_app(settings, cart_store=CartStore()))
    master = client.app.state.drivers.master
    pricing = client.app.state.drivers.pricing

    def product(code, name):
        req = master.create_entity("product", {
            "code": code, "name": name, "category": "饮料", "base_price": "10",
            "units": [{"unit": "件", "kind": "base", "rate": "1"},
                      {"unit": "箱", "kind": "sales", "rate": "12"}]})
        master.confirm_request(req["request_id"])

    product("SKU-001", "零售可售商品")
    product("SKU-002", "无零售价商品")
    product("SKU-003", "已下架商品")
    product("SKU-004", "非零售渠道商品")
    pool = master.create_entity("product_pool", {
        "name": "零售渠道池", "visibility": "authorized", "org_path": "/零售",
        "product_codes": ["SKU-001", "SKU-002", "SKU-003"]})
    master.confirm_request(pool["request_id"])
    master.confirm_request(
        master.authorize_pool(pool["entity_id"], channels=[CHANNEL])["request_id"])
    snap = master.find_by_code("product", "SKU-003")
    master.confirm_request(
        master.set_lifecycle(snap["entity_id"], "stop_selling")["request_id"])
    for code in ("SKU-001", "SKU-003", "SKU-004"):   # SKU-002 故意无零售价
        pricing.confirm_rule(pricing.add_rule(_retail_rule(product=code))["request_id"])
    return client, master, pricing, settings


def _get(client, path):
    return client.get(path, headers={})   # 显式空头：证明匿名可读


def test_anonymous_read_without_any_identity(shop):
    client, *_ = shop
    r = _get(client, f"/shop/{TENANT}/products")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert [c["productId"] for c in body["data"]] == ["SKU-001"]


def test_no_retail_price_is_not_sellable_and_not_backfilled(shop):
    """SKU-002 在渠道池内、有 base_price，但没有零售价 → 必须不出现。"""
    client, *_ = shop
    body = _get(client, f"/shop/{TENANT}/products").json()
    assert "SKU-002" not in [c["productId"] for c in body["data"]]


def test_delisted_and_out_of_channel_excluded(shop):
    client, *_ = shop
    ids = [c["productId"] for c in _get(client, f"/shop/{TENANT}/products").json()["data"]]
    assert "SKU-003" not in ids   # 下架
    assert "SKU-004" not in ids   # 不在零售渠道池


def test_detail_404_for_non_sellable(shop):
    client, *_ = shop
    for code in ("SKU-002", "SKU-003", "SKU-004"):
        assert _get(client, f"/shop/{TENANT}/products/{code}").status_code == 404, code


def test_detail_ok_and_no_internal_caliber(shop):
    client, *_ = shop
    r = _get(client, f"/shop/{TENANT}/products/SKU-001")
    assert r.status_code == 200, r.text
    card = r.json()["data"]
    assert float(card["unit_price"]) == 99.0
    assert card["price_source"] == "retail/price-list/v1"
    blob = json.dumps(card, ensure_ascii=False)
    for forbidden in ("entity_id", "supplier_id", "base_price", "cost",
                      "list_price", "customer_price"):
        assert forbidden not in blob, forbidden


def test_cross_tenant_is_404(shop):
    client, *_ = shop
    assert _get(client, "/shop/intruder-tenant/products").status_code == 404
    assert _get(client, "/shop/intruder-tenant/products/SKU-001").status_code == 404


def test_store_closed_when_retail_channel_unset(shop):
    """零售渠道未配置 = 店面关闭：空集合，**不回落成全量商品可见**。"""
    client, _master, _pricing, settings = shop
    client.app.state.drivers  # 闭包读 settings 同一对象
    object.__setattr__(settings, "retail_channel", None)  # frozen dataclass
    body = _get(client, f"/shop/{TENANT}/products").json()
    assert body["data"] == [] and body["channel"] is None
    assert _get(client, f"/shop/{TENANT}/products/SKU-001").status_code == 404


def test_shop_paths_are_an_explicit_list_not_a_prefix(shop):
    """身份闸只对登记过的店面路径让行；`/shop/` 下的其他路径仍要租户头。"""
    from fd_erp_vertical_api.app import SHOP_RE
    assert SHOP_RE.match("/shop/api-e2e/products")
    assert SHOP_RE.match("/shop/api-e2e/products/SKU-001")
    assert SHOP_RE.match("/shop/api-e2e/orders")
    assert SHOP_RE.match("/shop/api-e2e/orders/SO-1")
    assert not SHOP_RE.match("/shop/api-e2e/anything-else")   # 未登记 → 仍过租户闸
    assert not SHOP_RE.match("/shop/api-e2e/orders/SO-1/refund")  # 未登记的子路径同理


# ---- 0.5 缓存与枚举面 ----

def test_cache_headers_on_public_reads(shop):
    client, *_ = shop
    r = _get(client, f"/shop/{TENANT}/products")
    assert "public" in r.headers["cache-control"]
    assert r.headers["etag"].startswith('W/"shop-')
    d = _get(client, f"/shop/{TENANT}/products/SKU-001")
    assert "public" in d.headers["cache-control"]
    assert "etag" in d.headers


def test_conditional_get_returns_304(shop):
    client, *_ = shop
    first = _get(client, f"/shop/{TENANT}/products")
    etag = first.headers["etag"]
    second = client.get(f"/shop/{TENANT}/products",
                        headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""


def test_etag_changes_with_filter(shop):
    """不同过滤参数必须拿不同 ETag——否则客户端会拿 A 的 304 当 B 的结果。"""
    client, *_ = shop
    base = _get(client, f"/shop/{TENANT}/products").headers["etag"]
    filtered = _get(client, f"/shop/{TENANT}/products?keyword=可售").headers["etag"]
    assert base != filtered


def test_no_id_enumeration_beyond_public_catalog(shop):
    """不提供「按 id 探测他人商品」的接口：任何不可售/不存在/越域的 id 都是同一个
    404 且响应体不含该 id 之外的任何信息（不泄露存在性）。"""
    client, *_ = shop
    codes = ["SKU-002", "SKU-003", "SKU-004", "DOES-NOT-EXIST", "SKU-001-typo"]
    bodies = []
    for code in codes:
        r = _get(client, f"/shop/{TENANT}/products/{code}")
        assert r.status_code == 404, code
        bodies.append(json.dumps(r.json()["error"]["code"]))
    # 全部同一个错误码，无法从错误形态区分「不存在」与「存在但不可售」
    assert set(bodies) == {'"NOT_FOUND"'}


def test_cross_tenant_body_does_not_echo_tenant(shop):
    client, *_ = shop
    r = _get(client, "/shop/intruder-tenant/products/SKU-001")
    assert r.status_code == 404
    # 不回显请求里的租户标识——越域探测拿不到「这个租户存在/不存在」的区分
    assert "intruder-tenant" not in json.dumps(r.json(), ensure_ascii=False)


def test_rate_limit_returns_429_with_retryable_code(shop, monkeypatch):
    """匿名面按来源地址限流：超窗返回 429 + 可退避的错误码。"""
    from fd_erp_vertical_api import storefront
    client, *_ = shop
    monkeypatch.setattr(storefront, "_RATE_MAX_PER_WINDOW", 3)
    storefront._RATE_BUCKETS.clear()
    codes = []
    for _ in range(5):
        r = _get(client, f"/shop/{TENANT}/products")
        codes.append(r.status_code)
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]
    err = _get(client, f"/shop/{TENANT}/products").json()["error"]
    assert err["code"] == "RATE_LIMITED_PUBLIC"
    assert "退避" in err["hint"]


def test_rate_limit_is_per_source_not_global(shop, monkeypatch):
    """甲地址打满不该连累乙地址——否则一个爬虫就能把整站锁死。"""
    from fd_erp_vertical_api import storefront
    client, *_ = shop
    monkeypatch.setattr(storefront, "_RATE_MAX_PER_WINDOW", 2)
    storefront._RATE_BUCKETS.clear()
    for _ in range(3):
        _get(client, f"/shop/{TENANT}/products")   # 第 3 次被限流（来源=testclient）
    assert "testclient" in storefront._RATE_BUCKETS
    storefront._rate_check("other-host")           # 另一来源不受影响
    assert len(storefront._RATE_BUCKETS["other-host"]) == 1


# ---- 零售下单 / 到账（change establish-ai-native-ecommerce-stack 0.7/0.9/0.11/0.12）----

SHIP = {"name": "李四", "phone": "13900000000", "address": "示例省示例市"}


def test_anonymous_order_roundtrip(shop):
    """0.7：匿名下单 → 凭单号查回，且状态为待付款。"""
    client, *_ = shop
    r = client.post(f"/shop/{TENANT}/orders",
                    json={"lines": [{"productId": "SKU-001", "qty": 2}],
                          "shipTo": SHIP}, headers={})
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["settlement_mode"] == "prepaid"
    assert body["payment_state"] == "awaiting_payment"
    assert float(body["amount_total"]) == 198.0     # 99 × 2

    got = client.get(f"/shop/{TENANT}/orders/{body['order_ref']}", headers={})
    assert got.status_code == 200
    assert got.json()["data"]["status"] == "pending"


def test_order_missing_shipping_is_rejected_with_field(shop):
    """0.9：缺收货信息被拒并指名缺项。"""
    client, *_ = shop
    r = client.post(f"/shop/{TENANT}/orders",
                    json={"lines": [{"productId": "SKU-001", "qty": 1}]}, headers={})
    assert r.status_code == 400
    assert r.json()["error"]["detail"]["missing"] == "shipTo"

    bad = client.post(f"/shop/{TENANT}/orders",
                      json={"lines": [{"productId": "SKU-001", "qty": 1}],
                            "shipTo": {"name": "李四"}}, headers={})
    assert bad.status_code == 400
    assert "phone" in json.dumps(bad.json()["error"])


def test_order_unbuyable_product_rejected(shop):
    """无零售价 / 下架商品不能下单（与读面同一判据）。"""
    client, *_ = shop
    for code in ("SKU-002", "SKU-003"):
        r = client.post(f"/shop/{TENANT}/orders",
                        json={"lines": [{"productId": code, "qty": 1}],
                              "shipTo": SHIP}, headers={})
        assert r.status_code >= 400, (code, r.status_code)


def test_order_idempotency_key_replays_first_result(shop):
    client, *_ = shop
    h = {"Idempotency-Key": "k-1"}
    first = client.post(f"/shop/{TENANT}/orders",
                        json={"lines": [{"productId": "SKU-001", "qty": 1}],
                              "shipTo": SHIP}, headers=h)
    second = client.post(f"/shop/{TENANT}/orders",
                         json={"lines": [{"productId": "SKU-001", "qty": 1}],
                               "shipTo": SHIP}, headers=h)
    assert first.json()["data"]["order_ref"] == second.json()["data"]["order_ref"]
    assert second.json()["data"]["duplicate"] is True


def test_b2b_order_not_visible_via_storefront(shop):
    """B2B 订单号经店面查询一律 404（不泄露存在性）。"""
    client, *_ = shop
    r = client.get(f"/shop/{TENANT}/orders/SO-DOESNOTEXIST", headers={})
    assert r.status_code == 404


def test_payment_requires_merchant_identity(shop):
    """到账登记要商家身份：匿名请求不可达。"""
    client, *_ = shop
    r = client.post("/shop/api-e2e/orders/SO-1/payment",
                    json={"amount": "99", "proofRef": "x"}, headers={})
    assert r.status_code == 401


def test_payment_then_confirm_gate(shop):
    """0.11/0.12：登记到账后可确认；重复登记幂等。"""
    client, *_ = shop
    created = client.post(f"/shop/{TENANT}/orders",
                          json={"lines": [{"productId": "SKU-001", "qty": 1}],
                                "shipTo": SHIP}, headers={}).json()["data"]
    ref = created["order_ref"]
    orders = client.app.state.drivers.orders
    # 未到账确认被拦
    from fd_erp_vertical_templates.errors import TemplateError
    import pytest as _pytest
    req_id = orders._latest(ref)["request_id"]
    with _pytest.raises(TemplateError) as ei:
        orders.confirm_order(req_id)
    assert "尚未到账" in ei.value.message

    h = {"X-ERP-Tenant": TENANT}
    ok = client.post(f"/shop/{TENANT}/orders/{ref}/payment",
                     json={"amount": "99", "proofRef": "bank-1"}, headers=h)
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["requires_posting"] is False
    dup = client.post(f"/shop/{TENANT}/orders/{ref}/payment",
                      json={"amount": "99", "proofRef": "bank-1"}, headers=h)
    assert dup.json()["data"]["duplicate"] is True

    assert orders.confirm_order(req_id)["status"] == "confirmed"
    got = client.get(f"/shop/{TENANT}/orders/{ref}", headers={}).json()["data"]
    assert got["payment_state"] == "received"
    assert got["status"] == "confirmed"


def test_budget_exhausted_maps_to_422_not_500(shop):
    """预算不足必须是可解释的 422，不能是 500 INTERNAL。

    2026-11-25 线上实撞：模板层抛 BUDGET_EXHAUSTED，但 API 的 adapt_exception
    不认识这个码，兜成 500「服务内部错误」——调用方看不出是「额度不够」。
    """
    client, *_ = shop
    body = {"lines": [{"productId": "SKU-001", "qty": 10}], "shipTo": SHIP}
    r = client.post(f"/shop/{TENANT}/orders", json=body,
                    headers={"Idempotency-Key": "budget-1",
                             "X-ERP-Grant-Jti": "jti-budget",
                             "X-ERP-Grant-Budget": "1"})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "BUDGET_EXHAUSTED"
    assert err["detail"]["order_total"]
    assert err["detail"]["remaining"]
