"""CRM 域 API 测试（add-crm-leads-visits 2.2-2.6）。

判据来自 spec delta `erp-crm-lane`：
  1. 线索池与拜访记录只看本人；他人客户/线索与「不存在」同一条 404，不泄露存在性；
  2. 拜访纪要必填，主体（客户或线索）至少一个；
  3. location 可选：不给就不出现位置字段，服务端不补默认值；
  4. 工作台「今日拜访」与 /se/visits 同源——今天登记两条，工作台就是 2；
  5. CRM 记录不动事件层（事件条数不变）。
"""
from __future__ import annotations

from .conftest import TENANT, _h_se

SE = "/erp/supply-chain/v1/se"
S1, S2 = "S-001", "S-002"


def _events_count(client) -> int:
    from fd_erp_ledger_mcp.db import connection as kconn
    with kconn(client.app.state.settings) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM business_events WHERE tenant_id = %s",
            (TENANT,)).fetchone()
        return row[0] if not isinstance(row, dict) else row["n"]


def test_lead_roundtrip_and_pool_scoping(client_faces, seeded_faces):
    before = _events_count(client_faces)
    r = client_faces.post(f"{SE}/leads", headers=_h_se(S1),
                          json={"name": "某连锁超市", "phone": "13800000000",
                                "company": "某某商贸", "source": "展会"})
    assert r.status_code == 200, r.text
    lead = r.json()["data"]
    assert lead["leadId"].startswith("LD-S001-") and lead["status"] == "new"

    mine = client_faces.get(f"{SE}/leads", headers=_h_se(S1)).json()
    assert mine["rows_returned"] == 1
    assert mine["data"][0]["name"] == "某连锁超市"

    # 另一位业务员看不到
    theirs = client_faces.get(f"{SE}/leads", headers=_h_se(S2)).json()
    assert theirs["rows_returned"] == 0

    # 状态过滤：只有 new
    assert client_faces.get(f"{SE}/leads", headers=_h_se(S1),
                            params={"status": "converted"}
                            ).json()["rows_returned"] == 0

    # CRM 记录不动事件层
    assert _events_count(client_faces) == before


def test_lead_rejects_missing_name_and_advanced_status(client_faces, seeded_faces):
    r = client_faces.post(f"{SE}/leads", headers=_h_se(S1), json={"phone": "1"})
    assert r.status_code == 400 and "name" in r.json()["error"]["message"]
    r = client_faces.post(f"{SE}/leads", headers=_h_se(S1),
                          json={"name": "x", "status": "following"})
    assert r.status_code == 400
    assert client_faces.get(f"{SE}/leads", headers=_h_se(S1)
                            ).json()["rows_returned"] == 0


def test_visit_requires_note_and_subject(client_faces, seeded_faces):
    cust = seeded_faces["cust1"]
    r = client_faces.post(f"{SE}/visits", headers=_h_se(S1),
                          json={"customerId": cust, "note": "   "})
    assert r.status_code == 400 and "纪要" in r.json()["error"]["message"]
    r = client_faces.post(f"{SE}/visits", headers=_h_se(S1), json={"note": "谈得挺好"})
    assert r.status_code == 400 and "客户或线索" in r.json()["error"]["message"]
    assert client_faces.get(f"{SE}/visits", headers=_h_se(S1)
                            ).json()["rows_returned"] == 0


def test_visit_foreign_customer_is_404_indistinguishable(client_faces, seeded_faces):
    # seeded_faces 里 cust1 归 S-001、cust2 归 S-002
    r_foreign = client_faces.post(f"{SE}/visits", headers=_h_se(S1),
                                  json={"customerId": seeded_faces["cust2"],
                                        "note": "跑错门了"})
    r_missing = client_faces.post(f"{SE}/visits", headers=_h_se(S1),
                                  json={"customerId": "C-NOT-EXIST", "note": "不存在"})
    assert r_foreign.status_code == r_missing.status_code == 404
    e1 = r_foreign.json()["error"]["message"].replace(seeded_faces["cust2"], "<ref>")
    e2 = r_missing.json()["error"]["message"].replace("C-NOT-EXIST", "<ref>")
    assert e1 == e2
    assert not any(w in e1 for w in ("他人", "无权", "不属于", "已存在"))


def test_visit_location_optional_and_verbatim(client_faces, seeded_faces):
    cust = seeded_faces["cust1"]
    r = client_faces.post(f"{SE}/visits", headers=_h_se(S1),
                          json={"customerId": cust, "note": "送样"})
    assert r.status_code == 200
    assert "location" not in r.json()["data"]

    r = client_faces.post(f"{SE}/visits", headers=_h_se(S1),
                          json={"customerId": cust, "note": "现场签约",
                                "location": "geo:30.2741,120.1551"})
    assert r.json()["data"]["location"] == "geo:30.2741,120.1551"

    rows = client_faces.get(f"{SE}/visits", headers=_h_se(S1)).json()["data"]
    assert len(rows) == 2
    with_loc = [x for x in rows if x.get("location")]
    assert len(with_loc) == 1
    assert with_loc[0]["location"] == "geo:30.2741,120.1551"
    assert all(x["location"] is None for x in rows if not x.get("location"))


def test_visit_period_filter_and_scoping(client_faces, seeded_faces):
    client_faces.post(f"{SE}/visits", headers=_h_se(S1),
                      json={"customerId": seeded_faces["cust1"], "note": "甲的拜访"})
    client_faces.post(f"{SE}/visits", headers=_h_se(S2),
                      json={"customerId": seeded_faces["cust2"], "note": "乙的拜访"})
    mine = client_faces.get(f"{SE}/visits", headers=_h_se(S1)).json()
    assert mine["rows_returned"] == 1 and mine["data"][0]["note"] == "甲的拜访"

    day = mine["data"][0]["visited_at"][:10]
    assert client_faces.get(f"{SE}/visits", headers=_h_se(S1),
                            params={"from": day, "to": day}
                            ).json()["rows_returned"] == 1
    assert client_faces.get(f"{SE}/visits", headers=_h_se(S1),
                            params={"from": "2099-01-01"}
                            ).json()["rows_returned"] == 0


def test_workspace_today_visits_comes_from_records(client_faces, seeded_faces):
    """工作台今日拜访数与 /se/visits 同源（design D5）。"""
    before = client_faces.get(f"{SE}/workspace", headers=_h_se(S1)).json()["data"]
    assert before["today_visit_plan"] == 0
    assert "CRM" not in before["notes"]["crm"].split("；")[0] or         "未上线" not in before["notes"]["crm"], (
        "拜访已上线，notes 不得再声称 CRM 车道未上线")

    for note in ("上午送样", "下午签约"):
        client_faces.post(f"{SE}/visits", headers=_h_se(S1),
                          json={"customerId": seeded_faces["cust1"], "note": note})

    after = client_faces.get(f"{SE}/workspace", headers=_h_se(S1)).json()["data"]
    assert after["today_visit_plan"] == 2
    # 另一位业务员的数字不受影响
    other = client_faces.get(f"{SE}/workspace", headers=_h_se(S2)).json()["data"]
    assert other["today_visit_plan"] == 0
