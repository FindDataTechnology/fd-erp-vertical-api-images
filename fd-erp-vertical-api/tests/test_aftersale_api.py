"""售后域 API 测试（add-aftersale-lane 2.2-2.3）。

判据来自 spec delta `erp-aftersale-lane`：
  1. 自有订单的退货申请被受理并可读回（同一行/类型/凭证引用/提交时刻）；
  2. 他人订单与不存在的订单返回**同一条** 404，响应体不泄露存在性；
  3. 行或数量对不上 → 拒并点名，且不产生记录；
  4. 列表按状态/期间过滤，只返回自己的；
  5. 受理**不动事件层**：事件条数不变（申请不是入库事实）。

越域一律 404 是契约 §0 的通用约定（不泄露存在性），这里对售后单号同样成立。
"""
from __future__ import annotations

from .conftest import TENANT, _h

UE = "/erp/supply-chain/v1/ue"


def _order(client, customer_id: str, qty: int = 4) -> str:
    """给某客户下一单（10 元/个 × qty）并确认，返回订单引用。"""
    r = client.post(f"{UE}/orders", headers=_h(customer_id),
                    json={"lines": [{"productId": "SKU-001", "qty": qty,
                                     "unit": "个"}],
                          "shipping": {"receiver": "张三", "phone": "138****0000",
                                       "address": "杭州市××路 1 号"}})
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["accepted"] is True, body
    d = client.app.state.drivers
    confirmations = [e for e in d.orders._events()
                     if e.get("record") == "order" and e.get("ref") == body["order_ref"]]
    d.orders.confirm_order(confirmations[0]["request_id"], decided_by="owner")
    return body["order_ref"]


def _events_count(client) -> int:
    from fd_erp_ledger_mcp.db import connection as kconn
    with kconn(client._settings) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM business_events WHERE tenant_id = %s",
            (TENANT,)).fetchone()
        return row[0] if not isinstance(row, dict) else row["n"]


def test_aftersale_request_roundtrip(client, seeded):
    cust1 = seeded["cust1"]
    order_ref = _order(client, cust1)
    before = _events_count(client)

    r = client.post(f"{UE}/aftersale-requests", headers=_h(cust1),
                    json={"orderId": order_ref,
                          "lines": [{"product": "SKU-001", "qty": 2, "unit": "个"}],
                          "kind": "return",
                          "evidence": ["obj:proof-1"]})
    assert r.status_code == 200, r.text
    out = r.json()["data"]
    assert out["requestId"].startswith("AS-")
    assert out["status"] == "submitted"
    assert "等待商家处置" in out["status_note"]

    # 受理不动事件层：申请不是入库事实
    assert _events_count(client) == before

    r = client.get(f"{UE}/aftersale-requests/{out['requestId']}", headers=_h(cust1))
    assert r.status_code == 200
    detail = r.json()["data"]
    assert detail["orderId"] == order_ref and detail["kind"] == "return"
    assert detail["lines"][0]["qty"] == "2"
    assert detail["evidence"] == ["obj:proof-1"]
    assert detail["timeline"][0]["status"] == "submitted"


def test_aftersale_foreign_order_is_same_404_as_missing(client, seeded):
    cust1, cust2 = seeded["cust1"], seeded["cust2"]
    order_ref = _order(client, cust1)
    payload = {"lines": [{"product": "SKU-001", "qty": 1}], "kind": "return"}

    r_foreign = client.post(f"{UE}/aftersale-requests", headers=_h(cust2),
                            json={"orderId": order_ref, **payload})
    r_missing = client.post(f"{UE}/aftersale-requests", headers=_h(cust1),
                            json={"orderId": "SO-NOT-EXIST", **payload})
    assert r_foreign.status_code == r_missing.status_code == 404
    # 除调用方自己给的 ref 外，两条错误没有可区分信息
    e1 = r_foreign.json()["error"]["message"].replace(order_ref, "<ref>")
    e2 = r_missing.json()["error"]["message"].replace("SO-NOT-EXIST", "<ref>")
    assert e1 == e2
    assert not any(w in e1 for w in ("他人", "无权", "已存在", "不属于"))

    # 越域请求既不落记录，也不在对方列表里
    r = client.get(f"{UE}/aftersale-requests", headers=_h(cust1))
    assert r.json()["rows_returned"] == 0


def test_aftersale_line_and_qty_validation(client, seeded):
    cust1 = seeded["cust1"]
    order_ref = _order(client, cust1, qty=3)

    r = client.post(f"{UE}/aftersale-requests", headers=_h(cust1),
                    json={"orderId": order_ref,
                          "lines": [{"product": "SKU-999", "qty": 1}],
                          "kind": "return"})
    assert r.status_code == 400 and "不在订单" in r.json()["error"]["message"]

    r = client.post(f"{UE}/aftersale-requests", headers=_h(cust1),
                    json={"orderId": order_ref,
                          "lines": [{"product": "SKU-001", "qty": 4}],
                          "kind": "return"})
    assert r.status_code == 400 and "超过下单量" in r.json()["error"]["message"]

    r = client.post(f"{UE}/aftersale-requests", headers=_h(cust1),
                    json={"orderId": order_ref,
                          "lines": [{"product": "SKU-001", "qty": 1}],
                          "kind": "refund"})
    assert r.status_code == 400 and "return" in r.json()["error"]["message"]

    # 缺 orderId / lines 也必须是 400（形态校验在最前）
    assert client.post(f"{UE}/aftersale-requests", headers=_h(cust1),
                       json={"lines": [{"product": "SKU-001", "qty": 1}],
                             "kind": "return"}).status_code == 400
    assert client.post(f"{UE}/aftersale-requests", headers=_h(cust1),
                       json={"orderId": order_ref, "lines": [], "kind": "return"}
                       ).status_code == 400

    r = client.get(f"{UE}/aftersale-requests", headers=_h(cust1))
    assert r.json()["rows_returned"] == 0


def test_aftersale_list_is_scoped_and_filterable(client, seeded):
    cust1, cust2 = seeded["cust1"], seeded["cust2"]
    o1 = _order(client, cust1, qty=2)
    o2 = _order(client, cust2, qty=2)

    for cid, ref in ((cust1, o1), (cust2, o2)):
        r = client.post(f"{UE}/aftersale-requests", headers=_h(cid),
                        json={"orderId": ref,
                              "lines": [{"product": "SKU-001", "qty": 1}],
                              "kind": "exchange"})
        assert r.status_code == 200, r.text

    mine = client.get(f"{UE}/aftersale-requests", headers=_h(cust1)).json()
    assert mine["rows_returned"] == 1
    row = mine["data"][0]
    assert row["orderId"] == o1 and row["kind"] == "exchange"

    theirs = client.get(f"{UE}/aftersale-requests", headers=_h(cust2)).json()
    assert theirs["rows_returned"] == 1 and theirs["data"][0]["orderId"] == o2

    day = row["submitted_at"][:10]
    assert client.get(f"{UE}/aftersale-requests", headers=_h(cust1),
                      params={"from": day, "to": day}).json()["rows_returned"] == 1
    assert client.get(f"{UE}/aftersale-requests", headers=_h(cust1),
                      params={"status": "completed"}).json()["rows_returned"] == 0
    assert client.get(f"{UE}/aftersale-requests", headers=_h(cust1),
                      params={"from": "2099-01-01"}).json()["rows_returned"] == 0


def test_aftersale_detail_of_other_customers_request_is_404(client, seeded):
    cust1, cust2 = seeded["cust1"], seeded["cust2"]
    o2 = _order(client, cust2, qty=2)
    made = client.post(f"{UE}/aftersale-requests", headers=_h(cust2),
                       json={"orderId": o2,
                             "lines": [{"product": "SKU-001", "qty": 1}],
                             "kind": "supplement"}).json()["data"]

    r = client.get(f"{UE}/aftersale-requests/{made['requestId']}",
                   headers=_h(cust1))
    assert r.status_code == 404
    assert "不存在" in r.json()["error"]["message"]
