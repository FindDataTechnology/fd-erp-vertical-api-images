"""月结清单装配（change: add-month-end-checklist 任务 2.1）。

判据：真库落应收与出库成本 → 登记坏账费率 → `run` 三步准备（只出待确认）→ 有待办不关账 →
人工过门 → 再 `run` 出结转损益（第四步）→ 过门 → `run(close=True)` 关账成功 →
`periods.report` 复算一致。

关账前必须多过一道门：结转的基数只认已落账分录，坏账计提没落账时结转会少一笔费用，
所以它被推迟（`deferred`），而不是拿未落账的数据算出来。
"""
from __future__ import annotations

import pytest


def _post(d, stable_id: str, lines: list[dict], **kw) -> dict:
    prop = d.kernel.submit_proposal(stable_id, lines, **kw)
    return d.kernel.confirm_proposal(proposal_id=prop["proposal_id"],
                                     confirmed_by="cfo")


def _sale(d, stable_id: str, amount: str) -> None:
    _post(d, stable_id, [
        {"account_code": "1100", "direction": "debit", "amount": amount,
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": amount,
         "currency": "CNY"}], dimensions={"customer": "C-A"}, booked_on="2026-11-05")


def test_month_end_prepares_requires_confirmation_then_closes(client_faces):
    d = client_faces.app.state.drivers
    _sale(d, "me:ar-1", "10000")
    d.bad_debts.set_schedule(
        [{"maxDays": 30, "rate": "0.005"}, {"maxDays": None, "rate": "0.3"}],
        registered_by="cfo")

    pre = d.month_end.precheck("2026-11")
    assert pre["readyToRun"] is True, pre["blockers"]

    out = d.month_end.run("2026-11", actor="cfo")
    steps = {s["step"]: s for s in out["steps"]}
    assert steps["cogs"]["state"] == "prepared"          # 本期无出库 → 0 元结转
    assert steps["depreciation"]["state"] == "prepared"  # 无资产 → 不生成提案
    assert steps["bad_debt"]["state"] == "prepared"      # 650 元计提
    assert out["closed"] is False
    assert len(out["pending"]) >= 1
    assert "过确认门" in out["nextActions"][0]

    # 有待办时强行关账：不关
    again = d.month_end.run("2026-11", close=True)
    assert again["closed"] is False

    # 人工过门（list_proposals 的行键是 id，不是 proposal_id）
    for prop in d.kernel.list_proposals(status="pending")["data"]:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo")

    # 费用类提案落账后，第四步结转损益才出得来（它只认已落账分录）
    after = d.month_end.run("2026-11", close=True)
    closing_step = {s["step"]: s for s in after["steps"]}["closing"]
    assert closing_step["state"] == "prepared", closing_step
    assert after["closed"] is False            # 结转提案本身还得过门

    for prop in d.kernel.list_proposals(status="pending")["data"]:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo")

    final = d.month_end.run("2026-11", close=True)
    assert final["closed"] is True, final.get("closeError")
    assert final["blocked"] is False
    report = d.periods.report("2026-11")
    assert report["verified"] is True and report["diffs"] == []

    # 关账后报表口径：利润表仍是原数（结转不进损益口径），资产负债表未结转损益归零
    inc = d.statements.income_statement("2026-11")
    assert inc["revenue"] == "10000.00"
    bs = d.statements.balance_sheet("2026-11")
    assert bs["balanced"] is True, bs.get("error")
    assert bs["unclosedProfit"] == "0.00"
    assert d.closing.status("2026-11")["state"] == "posted"


def test_closed_period_is_refused(client_faces):
    d = client_faces.app.state.drivers
    _post(d, "me:oct", [
        {"account_code": "5200", "direction": "debit", "amount": "100",
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": "100",
         "currency": "CNY"}], booked_on="2026-10-05")
    d.bad_debts.set_schedule([{"maxDays": None, "rate": "0.0"}], registered_by="cfo")
    d.periods.close("2026-10", actor="cfo")

    with pytest.raises(Exception) as ei:
        d.month_end.run("2026-10")
    assert "已关账" in str(ei.value)
