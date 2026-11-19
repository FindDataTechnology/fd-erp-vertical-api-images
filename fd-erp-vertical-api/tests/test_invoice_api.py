"""开票申请域 API 测试（add-invoice-request-lane 3.2-3.4）。

判据来自 spec delta `erp-invoice-request-lane`：
  1. 受理落**拟稿车道**（pending），不是「写入即事实」；
  2. 状态取内核投影：确认门拍板后客户侧看到 accepted / rejected；
  3. 只看到自己的申请；
  4. 调用方给的 amount 不落载荷；不承诺开票；
  5. 内核不可达 → 501 + planned_lane（不返回空列表冒充「没有申请」）。
"""
from __future__ import annotations

from .conftest import TENANT, _h

UE = "/erp/supply-chain/v1/ue"
BASE = {"title": "示范客户一号（开票抬头）", "taxId": "91350100MA2Y0000XX",
        "orderId": "SO-DEMO-0001"}


def _kernel_events(client, lane: str = "draft") -> list[dict]:
    from fd_erp_ledger_mcp.db import connection as kconn
    with kconn(client._settings) as conn:
        rows = conn.execute(
            "SELECT stable_id, event_type, lane, status, payload "
            "FROM business_events WHERE tenant_id = %s AND lane = %s "
            "ORDER BY id", (TENANT, lane)).fetchall()
    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
        else:
            out.append({"stable_id": row[0], "event_type": row[1], "lane": row[2],
                        "status": row[3], "payload": row[4]})
    return out


def _decide(client, stable_id: str, decision: str) -> None:
    """走内核确认门（运营/agent 面的真实入口），不绕过它。"""
    from fd_erp_ledger_mcp import events as kernel_events
    from fd_erp_ledger_mcp.db import connection as kconn
    with kconn(client._settings) as conn:
        kernel_events.decide_event(conn, tenant_id=TENANT, stable_id=stable_id,
                                   lane="draft", decision=decision,
                                   actor="human", note="测试拍板")


def test_request_lands_in_draft_lane_as_pending(client, seeded):
    cust = seeded["cust1"]
    r = client.post(f"{UE}/invoice-requests", headers=_h(cust), json=BASE)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["requestId"].startswith(f"IR-{cust}-")
    assert data["status"] == "pending"
    assert "审核" in data["note"] and "代开票" in data["note"]

    rows = _kernel_events(client)
    assert len(rows) == 1
    ev = rows[0]
    assert ev["event_type"] == "invoice_requested" and ev["status"] == "pending"
    assert ev["stable_id"] == data["requestId"]
    payload = ev["payload"] if isinstance(ev["payload"], dict) else {}
    assert payload["customer_id"] == cust
    assert payload["order_id"] == "SO-DEMO-0001"
    assert "amount" not in payload


def test_list_shows_pending_then_decided_status(client, seeded):
    cust = seeded["cust1"]
    req = client.post(f"{UE}/invoice-requests", headers=_h(cust),
                      json=BASE).json()["data"]["requestId"]

    listed = client.get(f"{UE}/invoice-requests", headers=_h(cust)).json()
    assert listed["rows_returned"] == 1 and listed["data"][0]["status"] == "pending"

    _decide(client, req, "accepted")
    after = client.get(f"{UE}/invoice-requests", headers=_h(cust)).json()
    assert after["data"][0]["status"] == "accepted"

    # 状态是真投影：过滤按状态生效，且本服务没有写过任何状态
    assert client.get(f"{UE}/invoice-requests", headers=_h(cust),
                      params={"status": "accepted"}).json()["rows_returned"] == 1
    assert client.get(f"{UE}/invoice-requests", headers=_h(cust),
                      params={"status": "pending"}).json()["rows_returned"] == 0


def test_requests_are_scoped_to_customer(client, seeded):
    a, b = seeded["cust1"], seeded["cust2"]
    mine = client.post(f"{UE}/invoice-requests", headers=_h(a),
                       json=BASE).json()["data"]["requestId"]
    theirs = client.post(f"{UE}/invoice-requests", headers=_h(b),
                         json=dict(BASE, title="另一家")).json()["data"]["requestId"]
    assert [r["requestId"] for r in
            client.get(f"{UE}/invoice-requests", headers=_h(a)).json()["data"]] == [mine]
    assert [r["requestId"] for r in
            client.get(f"{UE}/invoice-requests", headers=_h(b)).json()["data"]] == [theirs]


def test_validation_rejects_and_writes_nothing(client, seeded):
    cust = seeded["cust1"]
    cases = [
        ({"taxId": "91350100MA2Y0000XX", "orderId": "SO-1"}, "抬头"),
        ({"title": "抬头", "orderId": "SO-1"}, "税号"),
        ({"title": "抬头", "taxId": "91350100MA2Y0000XX"}, "对账单或订单"),
    ]
    for body, needle in cases:
        r = client.post(f"{UE}/invoice-requests", headers=_h(cust), json=body)
        assert r.status_code == 400, (body, r.text)
        assert needle in r.json()["error"]["message"]
    assert _kernel_events(client) == [], "校验失败不得写内核"


def test_caller_amount_is_not_persisted(client, seeded):
    cust = seeded["cust1"]
    client.post(f"{UE}/invoice-requests", headers=_h(cust),
                json=dict(BASE, amount="88888.00"))
    payload = _kernel_events(client)[0]["payload"]
    payload = payload if isinstance(payload, dict) else {}
    assert "amount" not in payload


def test_kernel_down_degrades_to_501_not_empty_list(client, seeded, monkeypatch):
    """内核不可达：不返回空列表冒充「没有申请」，而是 501 + planned_lane。"""
    from fd_erp_vertical_api.errors import ApiError

    def _down(*a, **kw):
        raise ApiError("UPSTREAM_UNAVAILABLE", "内核不可达（测试注入）")

    monkeypatch.setattr(client.app.state.kernel, "query_events", _down)
    monkeypatch.setattr(client.app.state.kernel, "submit_event", _down)
    r = client.get(f"{UE}/invoice-requests", headers=_h(seeded["cust1"]))
    assert r.status_code == 501, r.text
    assert r.json()["error"]["detail"]["planned_lane"]
    r = client.post(f"{UE}/invoice-requests", headers=_h(seeded["cust1"]), json=BASE)
    assert r.status_code == 501, r.text
    assert r.json()["error"]["detail"]["planned_lane"]
