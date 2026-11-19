"""全链 e2e：pagesets 授权 → 商品 → 进货单 → 下单（信用占用）→ 查单 →
履约 → 对账单（确认门/异议车道）。真库 erp_test_b，零旁路（全部经模板驱动）。"""
from __future__ import annotations

from .conftest import TENANT, _h


def test_full_chain(client, seeded):
    cust1, cust2 = seeded["cust1"], seeded["cust2"]

    # ---- 0) pageset 授权端点（paas P0 客户端固定形状）----
    r = client.get("/erp/supply-chain/v1/pagesets", headers=_h(cust1))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "rows_returned" in body
    pagesets = body["data"]["pagesets"]
    assert pagesets == [{"key": "supply-chain.ue",
                         "grants": ["aftersale.write", "ai.chat", "catalog.read",
                                    "invoice.write", "order.write",
                                    "statement.confirm"]}]

    # ---- 1) 商品域 ----
    r = client.get("/erp/supply-chain/v1/ue/products", headers=_h(cust1))
    assert r.status_code == 200
    assert r.json()["rows_returned"] == 1
    card = r.json()["data"][0]
    assert card["productId"] == "SKU-001" and card["title"] == "示范商品 A"
    assert card["list_price"] == "10" and card["images"]

    r = client.get("/erp/supply-chain/v1/ue/products",
                   headers=_h(cust1), params={"keyword": "不存在"})
    assert r.json()["rows_returned"] == 0

    r = client.get("/erp/supply-chain/v1/ue/product-categories", headers=_h(cust1))
    cats = r.json()["data"]
    assert cats and cats[0]["categoryId"] == "饮料/水" and cats[0]["product_count"] == 1

    r = client.get("/erp/supply-chain/v1/ue/products/SKU-001", headers=_h(cust1))
    assert r.status_code == 200
    detail = r.json()["data"]
    assert detail["price"]["base_price"] == "10"
    assert detail["price"]["rule_chain"] == []  # 无规则命中回落基础价
    assert detail["stock"]["visible"] is True   # WMS 快照文件注入
    assert detail["stock"]["qty_available"] == "66"
    # 不可见商品：未入池商品一律 404（不泄露存在性）
    client.app.state.drivers.master.create_entity(
        "product", {"code": "SKU-HIDDEN", "name": "池外商品", "base_price": "5",
                    "units": [{"unit": "个", "kind": "base", "rate": "1"}]})
    r = client.get("/erp/supply-chain/v1/ue/products/SKU-HIDDEN", headers=_h(cust1))
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"

    # ---- 2) 进货单 ----
    r = client.post("/erp/supply-chain/v1/ue/cart/lines", headers=_h(cust1),
                    json={"productId": "SKU-001", "qty": 2})
    assert r.status_code == 200
    line = r.json()["data"]
    assert line["quote"]["line_total"] == "20"  # 2 × 10，当次取价快照
    line_id = line["line_id"]

    r = client.get("/erp/supply-chain/v1/ue/cart", headers=_h(cust1))
    assert r.json()["data"]["line_count"] == 1

    r = client.post("/erp/supply-chain/v1/ue/cart/price-preview", headers=_h(cust1),
                    json={"lines": [{"productId": "SKU-001", "qty": 1000}]})
    preview = r.json()["data"]
    assert preview["amount_total"] == "10000"
    assert preview["credit_preview"]["sufficient"] is False  # 额度 5000 < 10000
    from decimal import Decimal as D
    assert D(preview["credit_preview"]["available"]) == D("5000")

    r = client.delete(f"/erp/supply-chain/v1/ue/cart/lines/{line_id}",
                      headers=_h(cust1))
    assert r.json()["data"]["line_count"] == 0

    # ---- 3) 下单（信用校验 → 取价 → reserve_credit → 订单提案）----
    r = client.post("/erp/supply-chain/v1/ue/orders", headers=_h(cust1),
                    json={"lines": [{"productId": "SKU-001", "qty": 10, "unit": "个",
                                     "note": " urgent"}],
                          "shipping": {"receiver": "张三", "phone": "138****0000",
                                       "address": "杭州市××路 1 号"},
                          "note": "尽快发货"})
    assert r.status_code == 200, r.text
    order = r.json()["data"]
    assert order["accepted"] is True and order["status"] == "pending"
    assert order["amount_total"] == "100"
    assert order["credit_reservation"]["amount"] == "100"
    assert order["shipping_receipt"]["receiver"] == "张三"  # 受理回执回显
    order_ref = order["order_ref"]

    # 信用占用确实落内核（账本记账）
    credit = client.app.state.kernel.query_credit(cust1)
    from decimal import Decimal as D
    assert D(credit["reserved"]) == D("100") and D(credit["available"]) == D("4900")

    # ---- 4) 查单域 ----
    r = client.get("/erp/supply-chain/v1/ue/orders", headers=_h(cust1))
    assert r.json()["rows_returned"] == 1
    assert r.json()["data"][0]["orderId"] == order_ref
    assert r.json()["data"][0]["status"] == "pending"

    r = client.get(f"/erp/supply-chain/v1/ue/orders/{order_ref}", headers=_h(cust2))
    assert r.status_code == 404  # 越域一律 404，不泄露存在性

    r = client.get(f"/erp/supply-chain/v1/ue/orders/{order_ref}", headers=_h(cust1))
    detail = r.json()["data"]
    assert detail["lines"][0]["rule_chain"] == []
    assert detail["credit_reservation"]["stable_id"]

    # 履约：未确认订单无履约计划 → 404（如实）
    r = client.get(f"/erp/supply-chain/v1/ue/orders/{order_ref}/fulfillment",
                   headers=_h(cust1))
    assert r.status_code == 404

    # 确认车道确认订单 → 履约移交 → 履约编排可见
    req_id = order["request_id"]
    confirmed = client.app.state.drivers.orders.confirm_order(req_id)
    assert confirmed["status"] == "confirmed"
    # 已确认未编排：如实中间态 handoff_pending（不伪造计划）
    r = client.get(f"/erp/supply-chain/v1/ue/orders/{order_ref}/fulfillment",
                   headers=_h(cust1))
    assert r.status_code == 200
    view = r.json()["data"]
    assert view["handoff_pending"] is True and view["plans"] == []
    # 交付编排消费移交记录生成计划（编排层驱动，路径直发/仓内）
    planned = client.app.state.drivers.fulfillment.plan_fulfillment(
        confirmed["handoff_ref"], order_ref, cust1,
        [{"product_code": "SKU-001", "qty": "10"}])
    assert planned["ref"]
    r = client.get(f"/erp/supply-chain/v1/ue/orders/{order_ref}/fulfillment",
                   headers=_h(cust1))
    assert r.status_code == 200
    view = r.json()["data"]
    assert view["order_ref"] == order_ref and view["plans"]
    assert all("lines" in p for p in view["plans"])

    # ---- 5) 对账单：确认门 ----
    r = client.get("/erp/supply-chain/v1/ue/statements", headers=_h(cust1))
    assert r.json()["rows_returned"] == 1
    st = r.json()["data"][0]
    assert st["status"] == "open" and st["customer_id"] == cust1
    assert st["amount_total"] == "100" and st["order_count"] == 1
    sid = st["statement_id"]

    r = client.get(f"/erp/supply-chain/v1/ue/statements/{sid}", headers=_h(cust1))
    assert r.status_code == 200

    # cust2 无对账单（数据域隔离）
    r = client.get("/erp/supply-chain/v1/ue/statements", headers=_h(cust2))
    assert r.json()["rows_returned"] == 0

    # 确认 → 内核确认门 pending（不假报成功）
    r = client.post(f"/erp/supply-chain/v1/ue/statements/{sid}/confirm",
                    headers=_h(cust1), json={"decision": "confirmed"})
    assert r.status_code == 200
    confirm = r.json()["data"]
    assert confirm["proposal_status"] == "pending" and confirm["duplicate"] is False
    # 幂等：重复确认回放既有 pending，不重推
    r = client.post(f"/erp/supply-chain/v1/ue/statements/{sid}/confirm",
                    headers=_h(cust1), json={"decision": "confirmed"})
    assert r.json()["data"]["duplicate"] is True
    assert r.json()["data"]["proposal_status"] == "pending"

    r = client.get("/erp/supply-chain/v1/ue/statements", headers=_h(cust1))
    assert r.json()["data"][0]["status"] == "confirming"

    # 确认门拍板（owner 侧，经内核）→ 状态 confirmed
    stable = confirm["proposal_id"]
    client.app.state.kernel.confirm_proposal(proposal_id=stable, confirmed_by="owner")
    r = client.get("/erp/supply-chain/v1/ue/statements", headers=_h(cust1))
    assert r.json()["data"][0]["status"] == "confirmed"

    # ---- 6) 对账单：异议 → 内部处置车道 ----
    # cust2 也下一单，得到自己的 open 对账单
    r = client.post("/erp/supply-chain/v1/ue/orders", headers=_h(cust2),
                    json={"lines": [{"productId": "SKU-001", "qty": 1}]})
    assert r.json()["data"]["accepted"] is True
    r = client.get("/erp/supply-chain/v1/ue/statements", headers=_h(cust2))
    sid2 = r.json()["data"][0]["statement_id"]
    r = client.post(f"/erp/supply-chain/v1/ue/statements/{sid2}/confirm",
                    headers=_h(cust2),
                    json={"decision": "disputed", "disputeNote": "第三行金额有异议"})
    assert r.status_code == 200
    disp = r.json()["data"]
    assert disp["disputed"] is True and disp["disposal_status"] == "pending"
    pending = client.app.state.drivers.disposal.pending_tasks()
    assert len(pending) == 1 and pending[0]["payload"]["metric"] == "statement_disputed"

    # 缺 disputeNote → INVALID_ARGUMENT（可解释拒绝纪律）
    r = client.post(f"/erp/supply-chain/v1/ue/statements/{sid2}/confirm",
                    headers=_h(cust2), json={"decision": "disputed", "disputeNote": ""})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_ARGUMENT"

    # ---- 7) 额度不足 fail-closed 拒单（CREDIT_EXCEEDED + 占用明细）----
    r = client.post("/erp/supply-chain/v1/ue/orders", headers=_h(cust1),
                    json={"lines": [{"productId": "SKU-001", "qty": 1000}]})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "CREDIT_EXCEEDED"
    from decimal import Decimal as D
    assert D(err["detail"]["available"]) == D("4900") and err["detail"]["shortfall"]
