"""生产工单装配（change: add-manufacturing-orders 任务 3.1）。

判据：真库 BOM → 工单 → 领料 → 过门 → 完工 → 过门 → ①账上 1401 的工单维度净额 == 台账在制品、
②全部完工后在制品归零、③资产负债表把在制品算进资产且仍平。
"""
from __future__ import annotations

from decimal import Decimal

BOM = [{"sku": "RM-1", "qty": "2", "unit": "个"},
       {"sku": "RM-2", "qty": "1", "unit": "个"}]


def _confirm(d) -> None:
    for prop in d.kernel.list_proposals(status="pending")["data"]:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo")


def test_work_order_closes_the_wip_loop(client_faces):
    d = client_faces.app.state.drivers
    d.manufacturing.register_bom("FG-1", "2", BOM, registered_by="owner")
    d.manufacturing.order("FG-1", "2", ref="WO-1")
    issue = d.manufacturing.issue("WO-1", [
        {"sku": "RM-1", "qty": "4", "unit_cost": "30"},
        {"sku": "RM-2", "qty": "2", "unit_cost": "20"}])      # 120 + 40 = 160
    assert issue["total"] == "160.00"
    _confirm(d)

    status = d.manufacturing.status("WO-1")
    assert status["issuedCost"] == "160.00"
    assert status["wipBalance"] == status["wipOnLedger"] == "160.00"
    assert status["consistent"] is True

    done = d.manufacturing.complete("WO-1", "2")             # 单位成本 80
    assert done["unitCost"] == "80.00" and done["total"] == "160.00"
    _confirm(d)

    final = d.manufacturing.status("WO-1")
    assert final["wipBalance"] == final["wipOnLedger"] == "0.00"
    assert final["consistent"] is True
    assert final["completedPercent"] == "100.000000"

    # 在制品已转出，成品入库 160：资产负债表把在制品算进资产且仍平
    bs = d.statements.balance_sheet("2026-11")
    assert bs["balanced"] is True, bs.get("error")
    rows = {r["accountCode"]: r
            for r in d.books.account_balances("2026-11")["rows"]}
    assert Decimal(rows["1401"]["closing"]) == Decimal("0")
    # 原材料与成品共用 1200：领料出 160、完工入 160 ⇒ 期末净额回到 0。
    # 这不是漏记——生产的实质是「存货构成变了、总额没变」，两条腿都要在簿上。
    assert Decimal(rows["1200"]["debit"]) == Decimal("160")
    assert Decimal(rows["1200"]["credit"]) == Decimal("160")
    assert Decimal(rows["1200"]["closing"]) == Decimal("0")


def test_issue_without_unit_cost_is_refused_on_the_real_kernel(client_faces):
    d = client_faces.app.state.drivers
    d.manufacturing.register_bom("FG-2", "1", [{"sku": "RM-1", "qty": "1"}],
                                 registered_by="owner")
    d.manufacturing.order("FG-2", "1", ref="WO-2")
    try:
        d.manufacturing.issue("WO-2", [{"sku": "RM-1", "qty": "1"}])
    except Exception as exc:                       # noqa: BLE001
        assert "成本" in str(exc)
    else:                                          # pragma: no cover
        raise AssertionError("缺单位成本必须拒绝")
