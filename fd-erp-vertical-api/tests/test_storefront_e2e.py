"""店面全链 e2e：匿名逛店 → 下单 → 商家到账 → 履约 → 跨店隔离（真库）。

与 `test_e2e_chain.py` 同风格：真库、零旁路、TestClient，不 mock 业务层。

**为什么还要这条**：`test_storefront_public.py` 是逐端点的行为测试（读面口径、
限流、幂等），它验证「每个端点对」；这一条验证「这些端点串起来能开一家店」——
顾客从打开店面到商家发货，中间没有断点。两者不能互相替代。

店面这条路与 UE 订货台的**结构差异**必须在这里被证明，而不是靠读代码相信：
- 进门不要身份（匿名），出门要商家身份（到账登记）
- 订单走 `settlement_mode=prepaid`，**不占信用额度**
- 到账是 `retail_payment_received` 事实，**不产生分录**
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def shop(reset_db, tmp_path, dsn):
    """店面客户端：零售渠道已配，主数据与零售价种子就位。"""
    from tests.conftest import _make_app

    client = _make_app(tmp_path, dsn, pageset_json=None)
    client._settings.retail_channel = "retail-online"
    master = client.app.state.drivers.master
    pricing = client.app.state.drivers.pricing

    def product(code, name, base="10"):
        req = master.create_entity("product", {
            "code": code, "name": name, "category": "饮料", "base_price": base,
            "units": [{"unit": "件", "kind": "base", "rate": "1"}]})
        master.confirm_request(req["request_id"])

    product("SKU-A", "矿泉水")
    product("SKU-B", "苏打水")
    product("SKU-C", "下架品")
    pool = master.create_entity("product_pool", {
        "name": "零售池", "visibility": "authorized", "org_path": "/零售",
        "product_codes": ["SKU-A", "SKU-B", "SKU-C"]})
    master.confirm_request(pool["request_id"])
    master.confirm_request(
        master.authorize_pool(pool["entity_id"], channels=["retail-online"])["request_id"])
    snap = master.find_by_code("product", "SKU-C")
    master.confirm_request(master.set_lifecycle(snap["entity_id"], "stop_selling")["request_id"])
    for code, price in (("SKU-A", "12.50"), ("SKU-B", "8.00"), ("SKU-C", "99.00")):
        pricing.confirm_rule(pricing.add_rule({
            "layer": "retail",
            "scope": {"product_code": code, "channel_id": "retail-online"},
            "price": price, "valid_from": "2026-01-01", "valid_to": "2099-12-31",
            "rate_source": "retail/price-list/e2e/v1"})["request_id"])
    return client, master


SHIP = {"name": "收货甲", "phone": "13800000001", "address": "示例地址"}
TENANT = "api-e2e"


def test_full_storefront_chain(shop):
    """一条链路走到底：逛店 → 下单 → 到账 → 履约。"""
    client, _master = shop

    # ── 1. 匿名逛店：只看到可售的两件（下架的 SKU-C 不出现）──
    browse = client.get(f"/shop/{TENANT}/products", headers={})
    assert browse.status_code == 200
    ids = [p["productId"] for p in browse.json()["data"]]
    assert ids == ["SKU-A", "SKU-B"], ids
    prices = {p["productId"]: p["unit_price"] for p in browse.json()["data"]}
    assert float(prices["SKU-A"]) == 12.5
    # 零售价不是商品主档 base_price（10）
    assert float(prices["SKU-A"]) != 10.0

    # ── 2. 匿名下单（两件 SKU-A，预付）──
    created = client.post(f"/shop/{TENANT}/orders",
                          json={"lines": [{"productId": "SKU-A", "qty": 2}],
                                "shipTo": SHIP},
                          headers={"Idempotency-Key": "e2e-1"})
    assert created.status_code == 200, created.text
    order = created.json()["data"]
    ref = order["order_ref"]
    assert order["settlement_mode"] == "prepaid"
    assert order["payment_state"] == "awaiting_payment"
    assert float(order["amount_total"]) == 25.0

    # ── 2b. 预付单不占信用额度 ──
    orders_driver = client.app.state.drivers.orders
    evs = orders_driver._events()
    order_ev = [e for e in evs if e.get("ref") == ref and e.get("record") == "order"][-1]
    assert "credit_reservation" not in (order_ev.get("payload") or {}), \
        "预付单不该有信用占用"

    # ── 3. 未到账不可确认（确认会放货）──
    from fd_erp_vertical_templates.errors import TemplateError
    req_id = order_ev["request_id"]
    with pytest.raises(TemplateError) as ei:
        orders_driver.confirm_order(req_id)
    assert "尚未到账" in ei.value.message

    # ── 4. 买家侧查单：看不到付款能力，只有状态 ──
    got = client.get(f"/shop/{TENANT}/orders/{ref}", headers={})
    assert got.status_code == 200
    assert got.json()["data"]["payment_state"] == "awaiting_payment"
    assert "proof_ref" not in got.text and "bank" not in got.text.lower()

    # ── 5. 买家不能自己登记到账 ──
    anon_pay = client.post(f"/shop/{TENANT}/orders/{ref}/payment",
                           json={"amount": "25", "proofRef": "x"}, headers={})
    assert anon_pay.status_code == 401

    # ── 6. 商家登记到账（钱进商家账户，平台只记事实）──
    paid = client.post(f"/shop/{TENANT}/orders/{ref}/payment",
                       json={"amount": "25", "proofRef": "bank-e2e-1"},
                       headers={"X-ERP-Tenant": TENANT})
    assert paid.status_code == 200, paid.text
    assert paid.json()["data"]["requires_posting"] is False

    # ── 6b. 幂等：重复登记不新增事实 ──
    dup = client.post(f"/shop/{TENANT}/orders/{ref}/payment",
                      json={"amount": "25", "proofRef": "bank-e2e-1"},
                      headers={"X-ERP-Tenant": TENANT})
    assert dup.json()["data"]["duplicate"] is True
    payment_count = sum(1 for e in orders_driver._events()
                        if e.get("record") == "payment_received"
                        and e.get("order_ref") == ref)
    assert payment_count == 1

    # ── 7. 到账后确认 → 进入履约 ──
    confirmed = orders_driver.confirm_order(req_id)
    assert confirmed["status"] == "confirmed"
    assert confirmed["handoff_ref"].startswith("FH-")
    final = client.get(f"/shop/{TENANT}/orders/{ref}", headers={}).json()["data"]
    assert final["status"] == "confirmed" and final["payment_state"] == "received"

    # ── 8. 履约移交记录落在同一事件档（不是第二套订单）──
    handoffs = [e for e in orders_driver._events()
                if e.get("record") == "fulfillment_handoff"
                and (e.get("payload") or {}).get("order_ref") == ref]
    assert len(handoffs) == 1


def test_idempotency_key_replays_whole_order(shop):
    """同一个 Idempotency-Key 重放，返回同一单，不产生第二单。"""
    client, _ = shop
    h = {"Idempotency-Key": "e2e-replay"}
    body = {"lines": [{"productId": "SKU-B", "qty": 1}], "shipTo": SHIP}
    a = client.post(f"/shop/{TENANT}/orders", json=body, headers=h).json()["data"]
    b = client.post(f"/shop/{TENANT}/orders", json=body, headers=h).json()["data"]
    assert a["order_ref"] == b["order_ref"]
    assert b.get("duplicate") is True


def test_b2b_order_invisible_via_storefront(shop):
    """B2B 订单号经店面查不到——店面只承认自己的零售单。"""
    client, _ = shop
    r = client.get(f"/shop/{TENANT}/orders/SO-NOT-A-RETAIL-ORDER", headers={})
    assert r.status_code == 404
    assert "存在" not in r.text or "不存在" in r.text


def test_cross_tenant_storefront_is_not_found(shop):
    client, _ = shop
    r = client.get("/shop/someone-else/products", headers={})
    assert r.status_code == 404
    # 不回显请求里的租户名
    assert "someone-else" not in r.text


def test_delisted_product_cannot_be_ordered(shop):
    """下架商品在列表里看不见，直接拿编码下单也必须被拒。"""
    client, _ = shop
    r = client.post(f"/shop/{TENANT}/orders",
                    json={"lines": [{"productId": "SKU-C", "qty": 1}], "shipTo": SHIP},
                    headers={})
    assert r.status_code >= 400
