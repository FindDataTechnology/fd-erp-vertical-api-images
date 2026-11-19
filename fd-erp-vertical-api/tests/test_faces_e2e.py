"""SE/SP/CP 三页面集 e2e：身份闸 → 全链（SE 代客下单含信用/归属越权；SP 接单
→发货反馈→到货预告；CP 运单→月台→提货→签收）→ 501 如实 → 分页/统一壳。
真库 erp_test_b，零旁路（全部经模板驱动 + KernelClient）。"""
from __future__ import annotations

from decimal import Decimal as D

from .conftest import TENANT, _h, _h_cp, _h_se, _h_sp


def _push_po_to_supplier(client, supplier_id: str) -> str:
    """租户侧铺底：需求→汇总→采购单提案→确认→推送（推送留痕=SP 可见域）。"""
    d = client.app.state.drivers
    demand = d.procurement.submit_demand(
        "manual", [{"product_code": "SKU-001", "qty": "50", "unit": "个"}])
    agg = d.procurement.aggregate_demand([demand["ref"]])
    po = d.procurement.propose_purchase_order(agg["ref"], supplier_id)
    d.procurement.confirm_purchase_order(po["request_id"])
    d.procurement.record_push(po["ref"], channel="supplier-portal")
    return po["ref"]


def _make_waybill_for_carrier(client, cust1: str, carrier: str,
                              waybill_no: str) -> str:
    """租户侧铺底：订单→确认→履约编排→运单登记（承运商回流可见域）。"""
    from datetime import date
    d = client.app.state.drivers
    order = d.orders.accept_order(
        cust1, [{"product_code": "SKU-001", "qty": "10"}],
        date.today().isoformat())
    assert order["accepted"] is True
    confirmed = d.orders.confirm_order(order["request_id"])
    plan = d.fulfillment.plan_fulfillment(
        confirmed["handoff_ref"], order["order_ref"], cust1,
        [{"product_code": "SKU-001", "qty": "10"}])
    wb = d.fulfillment.record_waybill(plan["ref"], waybill_no, carrier)
    return wb["ref"]


# ==========================================================================
# SE 业务员台
# ==========================================================================

def test_se_full_chain(client_faces, seeded_faces):
    cust1, cust2 = seeded_faces["cust1"], seeded_faces["cust2"]

    # ---- 工作台（页面集授权闸 + 聚合卡）----
    r = client_faces.get("/erp/supply-chain/v1/se/workspace", headers=_h_se())
    assert r.status_code == 200
    ws = r.json()["data"]
    assert ws["staff"] == "S-001" and ws["owned_customer_count"] == 1
    assert ws["pending_orders"] == 0 and ws["pending_alerts"] == 0
    # 今日拜访 = 与 /se/visits 同源的已登记条数（CRM 车道已落地，不再是「未上线」）；
    # 这里刚开库还没登记拜访，所以是 0，但口径说明不再指向 501。
    assert ws["today_visit_plan"] == 0
    assert "501" not in ws["notes"]["crm"]
    assert "同源" in ws["notes"]["crm"]

    # 缺业务员身份头 → 401
    r = client_faces.get("/erp/supply-chain/v1/se/workspace",
                         headers={"X-ERP-Tenant": TENANT})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_REQUIRED"

    # ---- 客户列表（名下 1 个；关键词过滤；信用占用同源）----
    r = client_faces.get("/erp/supply-chain/v1/se/customers", headers=_h_se())
    assert r.status_code == 200 and r.json()["rows_returned"] == 1
    row = r.json()["data"][0]
    assert row["customerId"] == cust1 and row["sales_rep"] == "S-001"
    assert D(row["credit_available"]) == D("5000")
    r = client_faces.get("/erp/supply-chain/v1/se/customers",
                         headers=_h_se(), params={"keyword": "二号"})
    assert r.json()["rows_returned"] == 0  # C-002 是 S-002 名下

    # ---- 代客下单：归属越权 403（S-001 不得替 S-002 名下客户下单）----
    r = client_faces.post("/erp/supply-chain/v1/se/orders/proxy", headers=_h_se(),
                          json={"customerId": cust2,
                                "lines": [{"productId": "SKU-001", "qty": 1}]})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "DATA_SCOPE_DENIED"

    # 未建档客户 → 404（不泄露）
    r = client_faces.post("/erp/supply-chain/v1/se/orders/proxy", headers=_h_se(),
                          json={"customerId": "nope",
                                "lines": [{"productId": "SKU-001", "qty": 1}]})
    assert r.status_code == 404

    # ---- 代客下单全链（同一取价/信用链路 + 代理身份三处留痕）----
    r = client_faces.post("/erp/supply-chain/v1/se/orders/proxy", headers=_h_se(),
                          json={"customerId": cust1,
                                "lines": [{"productId": "SKU-001", "qty": 10,
                                           "unit": "个", "note": "加急"}],
                                "shipping": {"receiver": "张三",
                                             "address": "杭州市××路 1 号"},
                                "note": "代客录入"})
    assert r.status_code == 200, r.text
    order = r.json()["data"]
    assert order["accepted"] is True and order["amount_total"] == "100"
    assert order["proxy"]["staff"] == "S-001"
    assert order["proxy"]["requested_by"] == "se:S-001"
    assert order["credit_reservation"]["amount"] == "100"
    order_ref = order["order_ref"]

    # 信用占用真落内核（query_credit 同源）
    credit = client_faces.app.state.kernel.query_credit(cust1)
    assert D(credit["reserved"]) == D("100") and D(credit["available"]) == D("4900")

    # 额度不足 → 422 CREDIT_EXCEEDED（可解释拒绝，与 UE 同一链路）
    r = client_faces.post("/erp/supply-chain/v1/se/orders/proxy", headers=_h_se(),
                          json={"customerId": cust1,
                                "lines": [{"productId": "SKU-001", "qty": 1000}]})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "CREDIT_EXCEEDED" and err["detail"]["shortfall"]

    # ---- 名下订单列表/详情（requested_by 留痕可读）----
    r = client_faces.get("/erp/supply-chain/v1/se/orders", headers=_h_se())
    assert r.json()["rows_returned"] == 1
    assert r.json()["data"][0]["orderId"] == order_ref
    assert r.json()["data"][0]["requested_by"] == "se:S-001"

    r = client_faces.get(f"/erp/supply-chain/v1/se/orders/{order_ref}",
                         headers=_h_se())
    assert r.status_code == 200
    assert r.json()["data"]["lines"][0]["product"] == "SKU-001"  # 取价行（quote 形态）
    assert r.json()["data"]["requested_by"] == "se:S-001"

    # 客户域过滤 + 越域客户订单 404
    r = client_faces.get(f"/erp/supply-chain/v1/se/customers/{cust1}/orders",
                         headers=_h_se())
    assert r.json()["rows_returned"] == 1
    r = client_faces.get(f"/erp/supply-chain/v1/se/customers/{cust2}/orders",
                         headers=_h_se())
    assert r.status_code == 404  # 越域 404，不泄露存在性
    r = client_faces.get(f"/erp/supply-chain/v1/se/customers/{cust2}",
                         headers=_h_se())
    assert r.status_code == 404  # 客户 360 同口径

    # ---- 客户 360（档案/近单/对账摘要/额度占用，query_balance 同源）----
    r = client_faces.get(f"/erp/supply-chain/v1/se/customers/{cust1}",
                         headers=_h_se())
    c360 = r.json()["data"]
    assert c360["profile"]["sales_rep"] == "S-001"
    assert c360["order_count"] == 1 and len(c360["recent_orders"]) == 1
    assert D(c360["credit"]["reserved"]) == D("100")
    assert "query_balance 同源" in c360["credit"]["source_note"]
    assert c360["statements"]["count"] == 1

    # ---- 对账协助：名下客户对账单总览 ----
    r = client_faces.get("/erp/supply-chain/v1/se/statements", headers=_h_se())
    assert r.json()["rows_returned"] == 1
    st = r.json()["data"][0]
    assert st["customer_id"] == cust1 and st["amount_total"] == "100"
    sid = st["statement_id"]

    # 推送 → 501（端内消息通道归底座，SE 端不可代拍）
    r = client_faces.post(f"/erp/supply-chain/v1/se/statements/{sid}/push",
                          headers=_h_se(), json={"channel": "in-app"})
    assert r.status_code == 501
    assert r.json()["error"]["code"] == "NOT_IMPLEMENTED"
    assert r.json()["error"]["detail"]["planned_lane"]

    # ---- 预警处置：处置任务车道（与 owner 席位同一确认门）----
    d = client_faces.app.state.drivers
    task = d.disposal.raise_disposal_task(
        {"triggered": True, "metric": "ar_overdue", "value": "7",
         "severity": "high", "disposal_task": "逾期应收处置"},
        title="逾期应收处置：C-001")
    r = client_faces.get("/erp/supply-chain/v1/se/alerts", headers=_h_se())
    assert r.json()["rows_returned"] == 1
    alert = r.json()["data"][0]
    assert alert["alertId"] == task["request_id"]
    assert alert["metric"] == "ar_overdue" and alert["severity"] == "high"

    r = client_faces.get("/erp/supply-chain/v1/se/workspace", headers=_h_se())
    assert r.json()["data"]["pending_alerts"] == 1

    # 拍板 accept → 确认门生效（decided_by 留业务员身份）
    r = client_faces.post(f"/erp/supply-chain/v1/se/alerts/{task['request_id']}/handle",
                          headers=_h_se(), json={"decision": "accept"})
    assert r.status_code == 200
    assert r.json()["data"]["decision"] == "confirmed"
    assert r.json()["data"]["handled_by"] == "se:S-001"

    # 拒绝留痕不落账
    task2 = d.disposal.raise_disposal_task(
        {"triggered": True, "metric": "stock_stale", "value": "9",
         "severity": "medium", "disposal_task": "库存积压处置"}, title="积压处置")
    r = client_faces.post(f"/erp/supply-chain/v1/se/alerts/{task2['request_id']}/handle",
                          headers=_h_se(), json={"decision": "reject",
                                                 "note": "已排促销，暂不处置"})
    assert r.status_code == 200
    assert r.json()["data"]["decision"] == "rejected"
    r = client_faces.get("/erp/supply-chain/v1/se/alerts",
                         headers=_h_se(), params={"status": "rejected"})
    assert r.json()["rows_returned"] == 1

    # 非法 decision → 400；未知 alert → 404
    r = client_faces.post(f"/erp/supply-chain/v1/se/alerts/{task2['request_id']}/handle",
                          headers=_h_se(), json={"decision": "maybe"})
    assert r.status_code == 400
    r = client_faces.post("/erp/supply-chain/v1/se/alerts/NOPE/handle",
                          headers=_h_se(), json={"decision": "accept"})
    assert r.status_code == 404

    # ---- CRM 线索/拜访：车道已落地（add-crm-leads-visits），四条都不再是 501。
    # 归属隔离/纪要必填/location 可选等行为判据在 tests/test_crm_api.py，
    # 这里只钉住「从如实 501 变成真实现」这个交接点，防止有人把端点退回占位。
    for method, path, body in (
            ("GET", "/erp/supply-chain/v1/se/leads", None),
            ("POST", "/erp/supply-chain/v1/se/leads", {"name": "e2e 线索"}),
            ("GET", "/erp/supply-chain/v1/se/visits", None),
            ("POST", "/erp/supply-chain/v1/se/visits",
             {"customerId": seeded_faces["cust1"], "note": "e2e 拜访"})):
        r = (client_faces.get(path, headers=_h_se()) if method == "GET"
             else client_faces.post(path, headers=_h_se(), json=body))
        assert r.status_code == 200, (method, path, r.text)
        assert "planned_lane" not in r.text


def test_se_grant_gate_and_fail_closed(reset_db, tmp_path, dsn, seeded_faces):
    """grant 闸 / 页面集 fail-closed（同数据目录另建受限授权应用）。"""
    import json as _json

    from .conftest import FULL_GRANTS_4, _make_app
    limited = _json.loads(_json.dumps(FULL_GRANTS_4))
    limited["supply-chain.se"] = ["crm.read", "order.write.proxy"]
    c = _make_app(tmp_path, dsn, pageset_json=_json.dumps(limited))

    # grant 闸：页面集已授权但缺 alert.handle → GRANT_REQUIRED
    r = c.get("/erp/supply-chain/v1/se/alerts", headers=_h_se())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "GRANT_REQUIRED"
    # 已授权域不受影响
    r = c.get("/erp/supply-chain/v1/se/customers", headers=_h_se())
    assert r.status_code == 200

    # fail-closed：整集未授权 → PAGESET_FORBIDDEN
    c2 = _make_app(tmp_path, dsn, pageset_json=_json.dumps(
        {"supply-chain.ue": ["catalog.read"]}))
    r = c2.get("/erp/supply-chain/v1/se/customers", headers=_h_se())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "PAGESET_FORBIDDEN"
    r = c2.get("/erp/supply-chain/v1/sp/purchase-orders", headers=_h_sp("SUP-001"))
    assert r.status_code == 403
    r = c2.get("/erp/supply-chain/v1/cp/waybills", headers=_h_cp("CARRIER-A"))
    assert r.status_code == 403


# ==========================================================================
# SP 供应商
# ==========================================================================

def test_sp_full_chain(client_faces, seeded_faces):
    supplier = seeded_faces["supplier"]
    supplier_b = seeded_faces["supplier_b"]

    # 未推送的采购单对供应商不可见（数据域）
    r = client_faces.get("/erp/supply-chain/v1/sp/purchase-orders",
                         headers=_h_sp("SUP-001"))
    assert r.json()["rows_returned"] == 0

    po_ref = _push_po_to_supplier(client_faces, supplier)

    # ---- 工作台（推送后待接单 1）----
    r = client_faces.get("/erp/supply-chain/v1/sp/workspace",
                         headers=_h_sp("SUP-001"))
    assert r.status_code == 200
    ws = r.json()["data"]
    assert ws["po_count"] == 1 and ws["po_pending_accept"] == 1
    assert ws["po_pending_ship"] == 1 and ws["arrival_notices_open"] == 0

    # ---- 采购单列表/明细 ----
    r = client_faces.get("/erp/supply-chain/v1/sp/purchase-orders",
                         headers=_h_sp("SUP-001"))
    assert r.status_code == 200 and r.json()["rows_returned"] == 1
    row = r.json()["data"][0]
    assert row["poId"] == po_ref and row["status"] == "pushed"
    assert row["push_channel"] == "supplier-portal"
    assert row["lines"][0]["product_code"] == "SKU-001"

    r = client_faces.get(f"/erp/supply-chain/v1/sp/purchase-orders/{po_ref}",
                         headers=_h_sp("SUP-001"))
    assert r.status_code == 200
    detail = r.json()["data"]
    assert detail["supplier_response"] is None
    assert "field_note" in detail  # 交期/收货仓字段如实缺省注记

    # ---- 越权：另一供应商看不到也动不了（404，不泄露存在性）----
    r = client_faces.get("/erp/supply-chain/v1/sp/purchase-orders",
                         headers=_h_sp("SUP-002"))
    assert r.json()["rows_returned"] == 0
    r = client_faces.post(
        f"/erp/supply-chain/v1/sp/purchase-orders/{po_ref}/accept",
        headers=_h_sp("SUP-002"), json={"decision": "accepted"})
    assert r.status_code == 404

    # 未知供应商 → 404；缺身份头 → 401
    r = client_faces.get("/erp/supply-chain/v1/sp/purchase-orders",
                         headers=_h_sp("SUP-404"))
    assert r.status_code == 404
    r = client_faces.get("/erp/supply-chain/v1/sp/purchase-orders",
                         headers={"X-ERP-Tenant": TENANT})
    assert r.status_code == 401

    # ---- 接单回执（确认门语义；同一状态机）----
    r = client_faces.post(
        f"/erp/supply-chain/v1/sp/purchase-orders/{po_ref}/accept",
        headers=_h_sp("SUP-001"), json={"decision": "accepted"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["decision"] == "accepted"
    assert r.json()["data"]["responded_by"] == "sp:SUP-001"

    # 幂等裁决保护：重复接单 → 409 WORKFLOW_STATE（不建第二状态机）
    r = client_faces.post(
        f"/erp/supply-chain/v1/sp/purchase-orders/{po_ref}/accept",
        headers=_h_sp("SUP-001"), json={"decision": "rejected",
                                        "rejectedNote": "反悔"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "WORKFLOW_STATE"

    # 拒单须附 note（可解释拒绝纪律）——用第二张采购单验证
    po2 = _push_po_to_supplier(client_faces, supplier)
    r = client_faces.post(
        f"/erp/supply-chain/v1/sp/purchase-orders/{po2}/accept",
        headers=_h_sp("SUP-001"), json={"decision": "rejected",
                                        "rejectedNote": ""})
    assert r.status_code == 400
    r = client_faces.post(
        f"/erp/supply-chain/v1/sp/purchase-orders/{po2}/accept",
        headers=_h_sp("SUP-001"), json={"decision": "rejected",
                                        "rejectedNote": "产能不足，协商交期"})
    assert r.status_code == 200
    assert r.json()["data"]["decision"] == "rejected"
    # 拒单终态：不可再发货反馈/到货预告
    r = client_faces.post(
        f"/erp/supply-chain/v1/sp/purchase-orders/{po2}/delivery-feedback",
        headers=_h_sp("SUP-001"),
        json={"lines": [{"product_code": "SKU-001", "qty": "50"}]})
    assert r.status_code == 409

    # ---- 发货反馈（自动生成到货预告，与入库任务联动）----
    r = client_faces.post(
        f"/erp/supply-chain/v1/sp/purchase-orders/{po_ref}/delivery-feedback",
        headers=_h_sp("SUP-001"),
        json={"lines": [{"product_code": "SKU-001", "qty": "50", "unit": "个"}],
              "carrier": "CARRIER-A", "waybillNo": "WB-SP-001",
              "documents": ["img-ref-001"],
              "expectedDay": "2026-11-10"})
    assert r.status_code == 200, r.text
    fb = r.json()["data"]
    assert fb["po_ref"] == po_ref and fb["arrival_ref"]
    assert fb["lines"][0]["qty"] == "50"

    # 列表可见发货反馈 + 自动到货预告
    r = client_faces.get(f"/erp/supply-chain/v1/sp/purchase-orders/{po_ref}",
                         headers=_h_sp("SUP-001"))
    detail = r.json()["data"]
    assert len(detail["delivery_feedbacks"]) == 1
    assert len(detail["arrival_notices"]) == 1
    assert detail["arrival_notices"][0]["payload"]["expected_day"] == "2026-11-10"

    # ---- 到货预告列表 + 补录 ----
    r = client_faces.get("/erp/supply-chain/v1/sp/arrival-notices",
                         headers=_h_sp("SUP-001"))
    assert r.json()["rows_returned"] == 1
    r = client_faces.post("/erp/supply-chain/v1/sp/arrival-notices",
                          headers=_h_sp("SUP-001"),
                          json={"poRef": po_ref, "expectedDay": "2026-11-12",
                                "lines": [{"product_code": "SKU-001",
                                           "qty": "10"}]})
    assert r.status_code == 200
    manual_an = r.json()["data"]["ref"]
    r = client_faces.get("/erp/supply-chain/v1/sp/arrival-notices",
                         headers=_h_sp("SUP-001"), params={"status": "open"})
    assert r.json()["rows_returned"] == 2
    # 补录越权：别家供应商的采购单 404
    r = client_faces.post("/erp/supply-chain/v1/sp/arrival-notices",
                          headers=_h_sp("SUP-002"),
                          json={"poRef": po_ref,
                                "lines": [{"product_code": "SKU-001", "qty": "1"}]})
    assert r.status_code == 404

    # 入库任务联动（租户侧到货预告→入库任务，明细可见）
    d = client_faces.app.state.drivers
    d.procurement.create_inbound_task(manual_an, "WH-华东")
    r = client_faces.get(f"/erp/supply-chain/v1/sp/purchase-orders/{po_ref}",
                         headers=_h_sp("SUP-001"))
    assert len(r.json()["data"]["inbound_tasks"]) == 1

    # 工作台计数推进
    r = client_faces.get("/erp/supply-chain/v1/sp/workspace",
                         headers=_h_sp("SUP-001"))
    ws = r.json()["data"]
    assert ws["po_pending_accept"] == 0 and ws["arrival_notices_open"] == 2

    # ---- 对账/发票/结算：契约里有、后端无车道 → 如实 501 ----
    for method, path, payload in (
            ("GET", "/erp/supply-chain/v1/sp/statements", None),
            ("GET", "/erp/supply-chain/v1/sp/statements/ST-X", None),
            ("POST", "/erp/supply-chain/v1/sp/statements/ST-X/confirm",
             {"decision": "confirmed"}),
            ("POST", "/erp/supply-chain/v1/sp/invoices",
             {"statementId": "ST-X"}),
            ("GET", "/erp/supply-chain/v1/sp/invoices", None),
            ("GET", "/erp/supply-chain/v1/sp/settlements", None)):
        r = (client_faces.get(path, headers=_h_sp("SUP-001")) if method == "GET"
             else client_faces.post(path, headers=_h_sp("SUP-001"),
                                    json=payload))
        assert r.status_code == 501, (method, path, r.status_code)
        err = r.json()["error"]
        assert err["code"] == "NOT_IMPLEMENTED"
        assert err["detail"]["planned_lane"]


# ==========================================================================
# CP 承运商
# ==========================================================================

def test_cp_full_chain(client_faces, seeded_faces):
    cust1 = seeded_faces["cust1"]
    wb1 = _make_waybill_for_carrier(client_faces, cust1, "CARRIER-A", "WB-CP-001")
    wb2 = _make_waybill_for_carrier(client_faces, cust1, "CARRIER-A", "WB-CP-002")

    # ---- 运单列表（carrier 数据域）----
    r = client_faces.get("/erp/supply-chain/v1/cp/waybills",
                         headers=_h_cp("CARRIER-A"))
    assert r.status_code == 200 and r.json()["rows_returned"] == 2
    row = r.json()["data"][0]
    assert row["carrier"] == "CARRIER-A" and row["status"] == "open"
    assert row["order_ref"] and row["plan_ref"]

    # 越权：CARRIER-B 看不到也动不了（404，不泄露存在性）
    r = client_faces.get("/erp/supply-chain/v1/cp/waybills",
                         headers=_h_cp("CARRIER-B"))
    assert r.json()["rows_returned"] == 0
    r = client_faces.get(f"/erp/supply-chain/v1/cp/waybills/{wb1}",
                         headers=_h_cp("CARRIER-B"))
    assert r.status_code == 404
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb1}/accept",
                          headers=_h_cp("CARRIER-B"),
                          json={"decision": "accepted"})
    assert r.status_code == 404

    # 缺身份头 → 401
    r = client_faces.get("/erp/supply-chain/v1/cp/waybills",
                         headers={"X-ERP-Tenant": TENANT})
    assert r.status_code == 401

    # ---- 运单明细 + 事件流（轨迹不可得如实置空）----
    r = client_faces.get(f"/erp/supply-chain/v1/cp/waybills/{wb1}",
                         headers=_h_cp("CARRIER-A"))
    assert r.status_code == 200
    detail = r.json()["data"]
    assert detail["waybill_no"] == "WB-CP-001"
    assert detail["carrier_response"] is None
    assert detail["track_events"] is None  # 合成适配器无轨迹 → 如实 null
    assert detail["logistics_adapter"]["vendor"]

    # ---- 承运回执 ----
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb1}/accept",
                          headers=_h_cp("CARRIER-A"),
                          json={"decision": "accepted"})
    assert r.status_code == 200
    assert r.json()["data"]["decision"] == "accepted"
    # 重复回执 → 409
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb1}/accept",
                          headers=_h_cp("CARRIER-A"),
                          json={"decision": "rejected", "rejectedNote": "改口"})
    assert r.status_code == 409
    # 拒单须附 note
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb2}/accept",
                          headers=_h_cp("CARRIER-A"), json={"decision": "rejected"})
    assert r.status_code == 400
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb2}/accept",
                          headers=_h_cp("CARRIER-A"),
                          json={"decision": "rejected", "rejectedNote": "运力不足"})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "rejected"
    # 拒单终态：不可再月台/提货/签收
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb2}/dock-reservation",
                          headers=_h_cp("CARRIER-A"), json={"slotId": "D-1"})
    assert r.status_code == 409

    # ---- 工作台计数 ----
    r = client_faces.get("/erp/supply-chain/v1/cp/workspace",
                         headers=_h_cp("CARRIER-A"))
    ws = r.json()["data"]
    assert ws["waybills_total"] == 2 and ws["waybills_open"] == 1
    assert ws["waybills_pending_accept"] == 0

    # ---- 月台预约（预留/改约/取消，与仓库作业编排联动）----
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb1}/dock-reservation",
                          headers=_h_cp("CARRIER-A"),
                          json={"slotId": "DOCK-01", "warehouse": "WH-华东",
                                "slotDay": "2026-11-08"})
    assert r.status_code == 200
    dr1 = r.json()["data"]["ref"]
    # 改约 = 再预约新时段
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb1}/dock-reservation",
                          headers=_h_cp("CARRIER-A"), json={"slotId": "DOCK-02"})
    assert r.status_code == 200
    # 取消须带 reservationId 且属本运单
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb1}/dock-reservation",
                          headers=_h_cp("CARRIER-A"),
                          json={"action": "cancel", "reservationId": "DR-NOPE"})
    assert r.status_code == 404
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb1}/dock-reservation",
                          headers=_h_cp("CARRIER-A"),
                          json={"action": "cancel", "reservationId": dr1})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "cancelled"

    # ---- 提货打卡（可选采集；驱动在途状态）----
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb1}/pickup-checkin",
                          headers=_h_cp("CARRIER-A"),
                          json={"scanCode": "JOB-001",
                                "location": {"lat": "31.0", "lng": "121.0"},
                                "imageRefs": ["img-ref-002"]})
    assert r.status_code == 200
    assert r.json()["data"]["waybill_no"] == "WB-CP-001"

    # ---- 正常 POD（驱动结单/计费事件，本端零落账）----
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb1}/pod",
                          headers=_h_cp("CARRIER-A"),
                          json={"documents": ["pod-img-001"]})
    assert r.status_code == 200
    pod = r.json()["data"]
    assert pod["exception"] is False and pod["waybill_no"] == "WB-CP-001"
    assert "disposal_request_id" not in pod
    assert "计费" in pod["settlement_note"]

    # ---- 异常 POD → 处置车道（确认门派单留痕）----
    r = client_faces.post(f"/erp/supply-chain/v1/cp/waybills/{wb1}/pod",
                          headers=_h_cp("CARRIER-A"),
                          json={"exceptionNote": "外包装破损，客户拒收两箱"})
    assert r.status_code == 200
    pod2 = r.json()["data"]
    assert pod2["exception"] is True
    assert pod2["disposal_status"] == "pending"
    pending = client_faces.app.state.drivers.disposal.pending_tasks()
    assert any(t["payload"]["metric"] == "waybill_pod_exception"
               for t in pending)

    # ---- 事件流（与客户侧全单物流视图同源）----
    r = client_faces.get(f"/erp/supply-chain/v1/cp/waybills/{wb1}/events",
                         headers=_h_cp("CARRIER-A"))
    assert r.status_code == 200
    body = r.json()["data"]
    records = [e["record"] for e in body["events"]]
    assert "waybill" in records and "dock_reservation" in records
    assert "pickup_checkin" in records and "pod" in records
    assert "全单物流视图同源" in body["caliber_note"]

    # 工作台异常单计数
    r = client_faces.get("/erp/supply-chain/v1/cp/workspace",
                         headers=_h_cp("CARRIER-A"))
    assert r.json()["data"]["pod_exceptions"] == 1

    # ---- 月台可约时段：契约里有、后端无车道 → 如实 501 ----
    r = client_faces.get("/erp/supply-chain/v1/cp/dock-slots",
                         headers=_h_cp("CARRIER-A"))
    assert r.status_code == 501
    err = r.json()["error"]
    assert err["code"] == "NOT_IMPLEMENTED"
    assert err["detail"]["planned_lane"]

    # ---- 运费对账域：已实现（读面=内核费用行归集；本期无带承运商的费用行 → 空单+说明）
    r = client_faces.get("/erp/supply-chain/v1/cp/freight-statements",
                         headers=_h_cp("CARRIER-A"))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0 and body["rows_returned"] == 0
    assert "没有带承运商" in body["note"]  # 空表也必须说明为什么空
    # 不存在的运费单 → 404（不编造）
    r = client_faces.get("/erp/supply-chain/v1/cp/freight-statements/FS-1",
                         headers=_h_cp("CARRIER-A"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
    r = client_faces.post(
        "/erp/supply-chain/v1/cp/freight-statements/FS-1/confirm",
        headers=_h_cp("CARRIER-A"), json={"decision": "accepted"})
    assert r.status_code == 404


# ==========================================================================
# 分页 / 统一壳 / 校验（三面同壳纪律）
# ==========================================================================

def test_faces_pagination_and_envelope(client_faces, seeded_faces):
    d = client_faces.app.state.drivers
    for i in range(3):
        req = d.master.create_entity("customer", {
            "code": f"C-P{i}", "name": f"分页客户 {i}", "sales_rep": "S-001"})
        d.master.confirm_request(req["request_id"])

    r = client_faces.get("/erp/supply-chain/v1/se/customers", headers=_h_se(),
                         params={"limit": 2, "offset": 0})
    body = r.json()
    assert body["ok"] is True and body["rows_returned"] == 2
    assert body["total"] == 4 and body["truncated"] is True
    assert body["continuation"] == {"method": "offset", "next_offset": 2}
    r = client_faces.get("/erp/supply-chain/v1/se/customers", headers=_h_se(),
                         params={"limit": 99999})
    assert r.json()["limit"] == 500  # 体积上限不放开（内核 result.py 语义）
    r = client_faces.get("/erp/supply-chain/v1/se/customers", headers=_h_se(),
                         params={"offset": -1})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENT"

    # SP 列表统一壳 + 分页字段
    _push_po_to_supplier(client_faces, seeded_faces["supplier"])
    r = client_faces.get("/erp/supply-chain/v1/sp/purchase-orders",
                         headers=_h_sp("SUP-001"), params={"limit": 1})
    body = r.json()
    assert body["ok"] is True and body["rows_returned"] == 1
    assert body["total"] == 1 and body["truncated"] is False

    # 非对象 JSON 体 → 400 封闭码
    r = client_faces.post("/erp/supply-chain/v1/se/orders/proxy",
                          headers=_h_se(), json=[1, 2])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENT"

    # 错误壳四件齐（faces 路由同壳）
    r = client_faces.get("/erp/supply-chain/v1/cp/waybills/WB-NOPE",
                         headers=_h_cp("CARRIER-A"))
    assert r.status_code == 404
    err = r.json()["error"]
    assert set(err) >= {"code", "message", "hint", "detail"}
    assert r.json()["ok"] is False and r.json()["rows_returned"] == 0


def test_faces_tenant_gate_and_pagesets_listing(client_faces, seeded_faces):
    # 跨租户一律 403 TENANT_DENIED（全局租户闸对三面生效）
    r = client_faces.get("/erp/supply-chain/v1/se/workspace",
                         headers=_h_se(tenant="intruder"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "TENANT_DENIED"
    r = client_faces.get("/erp/supply-chain/v1/sp/purchase-orders",
                         headers=_h_sp("SUP-001", tenant="intruder"))
    assert r.status_code == 403
    r = client_faces.get("/erp/supply-chain/v1/cp/waybills",
                         headers=_h_cp("CARRIER-A", tenant="intruder"))
    assert r.status_code == 403

    # pagesets 端点输出四页面集（key 白名单与 paas KNOWN_KEYS 对齐）
    r = client_faces.get("/erp/supply-chain/v1/pagesets", headers=_h("x"))
    assert r.status_code == 200
    keys = [p["key"] for p in r.json()["data"]["pagesets"]]
    assert keys == ["supply-chain.ue", "supply-chain.se",
                    "supply-chain.sp", "supply-chain.cp"]
