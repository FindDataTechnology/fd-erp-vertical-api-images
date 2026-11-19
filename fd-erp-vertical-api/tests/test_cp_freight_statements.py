"""CP 运费对账域（change: add-carrier-freight-dimension 任务 4.1）。

判据：
1. 列表按调用方承运商归集（越域不出现在列表里），空表也说明为什么空；
2. 明细按运单聚合且带费用溯源；非本承运商的单号一律 404（不确认其存在）；
3. 确认走内核自平核对 proposal（净额为零）；确认门落定后再拍 → 409；
4. 驳回必须写原因（400）。
"""
from __future__ import annotations

import pytest

from .conftest import TENANT, _h_cp

DAY = "2026-11-08"
CP = "/erp/supply-chain/v1/cp"


def _rule(kernel, *, fee_item="freight", kind="delivery"):
    """配一条 active 运费规则（走内核计费包；管理面未开规则端点，测试直连内核包）。"""
    import fd_erp_ledger_mcp.billing as billing
    import fd_erp_ledger_mcp.db as db
    with db.connection(kernel._s) as conn:
        rule = billing.submit_billing_rule(
            conn, settings=kernel._s, tenant_id=kernel._tenant,
            config={"fee_item": fee_item, "event_kind": kind, "mode": "unit",
                    "unit_price": "20.00", "measure": "quantity",
                    "fee_type": "expense", "rule_key": fee_item,
                    "rate_source": "价目表/运费-2026-001"},
            source_ref="价目表/运费-2026-001")
        approved = billing.approve_billing_rule(
            conn, settings=kernel._s, tenant_id=kernel._tenant,
            rule_id=rule["rule_id"], version=rule["version"])
    assert approved["status"] == "active"


@pytest.fixture()
def billed(client_faces):
    """两条运单计费：CARRIER-A 一单、CARRIER-B 一单（各自 2 件 × 20.00）。"""
    d = client_faces.app.state.drivers
    _rule(d.kernel)
    d.freight.bill_delivery(waybill="WB-F-A1", carrier="CARRIER-A",
                            occurred_at=f"{DAY}T09:00:00",
                            measures={"quantity": 2})
    d.freight.bill_delivery(waybill="WB-F-B1", carrier="CARRIER-B",
                            occurred_at=f"{DAY}T10:00:00",
                            measures={"quantity": 2})
    return d


def test_list_is_scoped_to_calling_carrier(client_faces, billed):
    r = client_faces.get(f"{CP}/freight-statements", headers=_h_cp("CARRIER-A"))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["rows_returned"] == 1
    row = body["data"][0]
    assert row["statementId"] == f"FS-CARRIER-A-{DAY}"
    assert row["carrier"] == "CARRIER-A"
    assert row["waybillCount"] == 1 and row["feeCount"] == 1
    assert row["total"] == "40.000000"          # 20.00 × 2
    assert "只统计带承运商" in body["note"]
    # 越域：另一承运商看不到这张单
    r2 = client_faces.get(f"{CP}/freight-statements", headers=_h_cp("CARRIER-B"))
    ids = [x["statementId"] for x in r2.json()["data"]]
    assert ids == [f"FS-CARRIER-B-{DAY}"]


def test_empty_period_explains_itself(client_faces, billed):
    r = client_faces.get(f"{CP}/freight-statements", headers=_h_cp("CARRIER-Z"),
                         params={"period": DAY})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert "没有带承运商" in body["note"]
    assert body["data"] == []


def test_detail_aggregates_by_waybill_with_provenance(client_faces, billed):
    r = client_faces.get(f"{CP}/freight-statements/FS-CARRIER-A-{DAY}",
                         headers=_h_cp("CARRIER-A"))
    assert r.status_code == 200
    detail = r.json()["data"]
    assert detail["waybillCount"] == 1 and detail["total"] == "40.000000"
    line = detail["lines"][0]
    assert line["waybill"] == "WB-F-A1" and line["total"] == "40.000000"
    fee = line["fees"][0]
    assert fee["feeItem"] == "freight"
    assert fee["ruleId"] and fee["ruleVersion"]       # 规则版本溯源
    assert fee["refs"]["carrier"] == "CARRIER-A"      # 单据引用在明细里
    assert fee["refs"]["waybill"] == "WB-F-A1"


def test_cross_carrier_detail_is_404_not_403(client_faces, billed):
    """越域按 404：不向他方确认单号是否存在。"""
    r = client_faces.get(f"{CP}/freight-statements/FS-CARRIER-A-{DAY}",
                         headers=_h_cp("CARRIER-B"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_confirm_records_self_balancing_proposal_then_conflict(client_faces, billed):
    d = billed
    sid = f"FS-CARRIER-A-{DAY}"
    r = client_faces.post(f"{CP}/freight-statements/{sid}/confirm",
                          headers=_h_cp("CARRIER-A"),
                          json={"decision": "accepted", "note": "核对一致"})
    assert r.status_code == 200
    out = r.json()["data"]
    assert out["status"] == "pending" and out["total"] == "40.000000"
    assert out["confirmedBy"] == "cp:CARRIER-A"

    # 自平：提案借贷同额（净额为零，不动任何余额），科目在机器车道白名单内
    detail = d.kernel.list_proposals(status="pending")["data"]
    hit = next(p for p in detail if p["id"] == out["proposalId"])
    assert hit["lane_target"] == "diff:freight_statement_confirmed"
    assert "statement:" + sid in hit["source_ref"]

    # 确认门落定后改判 → 409（如实冲突，不静默覆盖）
    d.kernel.confirm_proposal(proposal_id=out["proposalId"], confirmed_by="owner")
    r = client_faces.post(f"{CP}/freight-statements/{sid}/confirm",
                          headers=_h_cp("CARRIER-A"),
                          json={"decision": "rejected", "note": "改主意"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "WORKFLOW_STATE"


def test_reject_requires_note(client_faces, billed):
    sid = f"FS-CARRIER-A-{DAY}"
    r = client_faces.post(f"{CP}/freight-statements/{sid}/confirm",
                          headers=_h_cp("CARRIER-A"),
                          json={"decision": "rejected"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_confirm_cross_carrier_is_404(client_faces, billed):
    r = client_faces.post(f"{CP}/freight-statements/FS-CARRIER-A-{DAY}/confirm",
                          headers=_h_cp("CARRIER-B"),
                          json={"decision": "accepted"})
    assert r.status_code == 404
