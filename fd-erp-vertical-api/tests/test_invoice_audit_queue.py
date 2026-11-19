"""开票审核队列（SE 面）API 测试（add-se-invoice-audit-queue 3.1-3.3）。

判据来自 spec delta `erp-invoice-audit-queue`：
  1. 列表只含本人名下客户；默认只看 pending；
  2. 拍板只经内核确认门：接受后客户侧状态随之变化；
  3. 已拍板再拍 → EVENT_STATE（409），不静默覆盖；
  4. 驳回必附 note；他人客户/不存在同一条 404；
  5. 状态不落本地副本（本服务零写入），内核不可达 → 501 + planned_lane。
"""
from __future__ import annotations

from .conftest import TENANT, _h, _h_se

SE = "/erp/supply-chain/v1/se"
UE = "/erp/supply-chain/v1/ue"
BASE = {"title": "示范客户一号（开票抬头）", "taxId": "91350100MA2Y0000XX",
        "orderId": "SO-AUDIT-0001"}


def _submit_ue(client, customer_id: str, **extra) -> str:
    """客户侧提交（走 UE 面，与业务员审核端解耦）。"""
    r = client.post(f"{UE}/invoice-requests", headers=_h(customer_id),
                    json=dict(BASE, **extra))
    assert r.status_code == 200, r.text
    return r.json()["data"]["requestId"]


def _decide(client, staff: str, request_id: str, decision: str, note: str | None = None):
    body = {"decision": decision}
    if note is not None:
        body["note"] = note
    return client.post(f"{SE}/invoice-requests/{request_id}/decide",
                       headers=_h_se(staff), json=body)


def test_se_list_shows_only_own_customers_pending_by_default(client_faces, seeded_faces):
    mine = _submit_ue(client_faces, seeded_faces["cust1"])    # 归 S-001
    theirs = _submit_ue(client_faces, seeded_faces["cust2"])  # 归 S-002

    r = client_faces.get(f"{SE}/invoice-requests", headers=_h_se("S-001"))
    assert r.status_code == 200, r.text
    ids = [row["requestId"] for row in r.json()["data"]]
    assert ids == [mine], ids
    assert theirs not in ids
    assert r.json()["data"][0]["customerId"] == seeded_faces["cust1"]

    # 另一位业务员看自己的
    other = client_faces.get(f"{SE}/invoice-requests", headers=_h_se("S-002")).json()
    assert [row["requestId"] for row in other["data"]] == [theirs]


def test_se_list_default_pending_and_all(client_faces, seeded_faces):
    first = _submit_ue(client_faces, seeded_faces["cust1"])
    second = _submit_ue(client_faces, seeded_faces["cust1"])
    assert _decide(client_faces, "S-001", first, "accepted").status_code == 200

    pending = client_faces.get(f"{SE}/invoice-requests", headers=_h_se("S-001")).json()
    assert [row["requestId"] for row in pending["data"]] == [second]

    everything = client_faces.get(f"{SE}/invoice-requests", headers=_h_se("S-001"),
                                  params={"status": "all"}).json()
    statuses = {row["requestId"]: row["status"] for row in everything["data"]}
    assert statuses == {first: "accepted", second: "pending"}


def test_accept_then_customer_sees_it(client_faces, seeded_faces):
    cust = seeded_faces["cust1"]
    req = _submit_ue(client_faces, cust)
    r = _decide(client_faces, "S-001", req, "accepted")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "accepted"

    # 客户侧查询随之变化（状态是内核投影，不是本服务存的）
    seen = client_faces.get(f"{UE}/invoice-requests", headers=_h(cust)).json()
    assert seen["data"][0]["status"] == "accepted"


def test_reject_requires_note_and_leaves_state(client_faces, seeded_faces):
    cust = seeded_faces["cust1"]
    req = _submit_ue(client_faces, cust)
    r = _decide(client_faces, "S-001", req, "rejected")
    assert r.status_code == 400
    assert "note" in r.json()["error"]["message"]

    # 没有 note 的驳回不得动状态
    listed = client_faces.get(f"{SE}/invoice-requests", headers=_h_se("S-001")).json()
    assert [row["requestId"] for row in listed["data"]] == [req]

    ok = _decide(client_faces, "S-001", req, "rejected", note="抬头与主档不一致")
    assert ok.status_code == 200
    seen = client_faces.get(f"{UE}/invoice-requests", headers=_h(cust)).json()
    assert seen["data"][0]["status"] == "rejected"


def test_double_decide_returns_event_state(client_faces, seeded_faces):
    req = _submit_ue(client_faces, seeded_faces["cust1"])
    assert _decide(client_faces, "S-001", req, "accepted").status_code == 200
    again = _decide(client_faces, "S-001", req, "rejected", note="改主意")
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "EVENT_STATE"

    seen = client_faces.get(f"{UE}/invoice-requests",
                            headers=_h(seeded_faces["cust1"])).json()
    assert seen["data"][0]["status"] == "accepted", "第二次拍板不得覆盖第一次"


def test_foreign_request_is_same_404_as_missing(client_faces, seeded_faces):
    theirs = _submit_ue(client_faces, seeded_faces["cust2"])   # 归 S-002
    r_foreign = _decide(client_faces, "S-001", theirs, "accepted")
    r_missing = _decide(client_faces, "S-001", "IR-C-NOT-EXIST-000001", "accepted")
    assert r_foreign.status_code == r_missing.status_code == 404
    e1 = r_foreign.json()["error"]["message"].replace(theirs, "<ref>")
    e2 = r_missing.json()["error"]["message"].replace("IR-C-NOT-EXIST-000001", "<ref>")
    assert e1 == e2
    assert not any(w in e1 for w in ("他人", "无权", "不属于"))


def test_kernel_down_degrades_to_501(client_faces, seeded_faces, monkeypatch):
    from fd_erp_vertical_api.errors import ApiError

    def _down(*a, **kw):
        raise ApiError("UPSTREAM_UNAVAILABLE", "内核不可达（测试注入）")

    monkeypatch.setattr(client_faces.app.state.kernel, "query_events", _down)
    r = client_faces.get(f"{SE}/invoice-requests", headers=_h_se("S-001"))
    assert r.status_code == 501, r.text
    assert r.json()["error"]["detail"]["planned_lane"]

    monkeypatch.setattr(client_faces.app.state.kernel, "decide_event", _down)
    r = client_faces.post(f"{SE}/invoice-requests/IR-X-000001/decide",
                          headers=_h_se("S-001"), json={"decision": "accepted"})
    assert r.status_code == 501, r.text
    assert r.json()["error"]["detail"]["planned_lane"]
