"""履约编排触发端点（add-erp-fulfillment-trigger）。

锁四件事：
  1. 已确认订单 → 200 且计划落档（此前 REST 面没有任何入口）；
  2. 未确认订单 → 409 WORKFLOW_STATE（不伪造计划）；
  3. 重复调用幂等（plan_fulfillment 每次新 ref，重复会堆重复指令）；
  4. 越域订单 → 404（不泄露存在性）。
"""
from __future__ import annotations

from datetime import date

from .conftest import TENANT, _h


def _accept(client, cust: str) -> dict:
    d = client.app.state.drivers
    return d.orders.accept_order(
        cust, [{"product_code": "SKU-001", "qty": "5"}], date.today().isoformat())


def test_pending_order_is_refused(client, seeded):
    out = _accept(client, seeded["cust1"])
    resp = client.post(
        f"/erp/supply-chain/v1/ue/orders/{out['order_ref']}/fulfillment",
        headers=_h(seeded["cust1"]))
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "WORKFLOW_STATE"
    # 不伪造计划
    d = client.app.state.drivers
    plans = [e for e in d.fulfillment._events()
             if e.get("record") == "fulfillment_plan"]
    assert plans == []


def test_confirmed_order_generates_plan_and_is_idempotent(client, seeded):
    out = _accept(client, seeded["cust1"])
    client.app.state.drivers.orders.confirm_order(out["request_id"])
    url = f"/erp/supply-chain/v1/ue/orders/{out['order_ref']}/fulfillment"

    first = client.post(url, headers=_h(seeded["cust1"]))
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["ok"] is True
    plans = body["data"]["plans"]
    assert len(plans) == 1
    plan_ref = plans[0]["plan_ref"]
    assert "idempotent" not in body["data"] or body["data"]["idempotent"] is not True

    second = client.post(url, headers=_h(seeded["cust1"]))
    assert second.status_code == 200
    again = second.json()["data"]
    assert again["idempotent"] is True
    assert [p["plan_ref"] for p in again["plans"]] == [plan_ref]
    # 计划未重复落档
    d = client.app.state.drivers
    made = [e for e in d.fulfillment._events()
            if e.get("record") == "fulfillment_plan"]
    assert len(made) == 1


def test_cross_customer_order_is_invisible(client, seeded):
    out = _accept(client, seeded["cust1"])
    client.app.state.drivers.orders.confirm_order(out["request_id"])
    resp = client.post(
        f"/erp/supply-chain/v1/ue/orders/{out['order_ref']}/fulfillment",
        headers=_h(seeded["cust2"]))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
