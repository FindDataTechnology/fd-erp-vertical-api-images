"""期末损益结转装配（change: add-profit-closing 任务 4.1）。

判据：真库落收入与费用 → `preview` 给出本期净损益 → `transfer` 出待确认提案 → 过门落账 →
①利润表逐项不变（结转不进损益口径）、②资产负债表仍平且未结转损益归零、③3100 等于本期净损益、
④重复 `transfer` 不重复过账、⑤已关账期间被拒。
"""
from __future__ import annotations

from decimal import Decimal

import pytest


def _post(d, stable_id: str, lines: list[dict], **kw) -> dict:
    prop = d.kernel.submit_proposal(stable_id, lines, **kw)
    return d.kernel.confirm_proposal(proposal_id=prop["proposal_id"],
                                     confirmed_by="cfo")


def _november(d) -> None:
    _post(d, "close:sale", [
        {"account_code": "1100", "direction": "debit", "amount": "10000",
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": "10000",
         "currency": "CNY"}], booked_on="2026-11-05")
    _post(d, "close:exp", [
        {"account_code": "5200", "direction": "debit", "amount": "2500",
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": "2500",
         "currency": "CNY"}], booked_on="2026-11-06")


def test_transfer_closes_profit_into_retained_and_reports_stay_right(client_faces):
    d = client_faces.app.state.drivers
    _november(d)

    pre = d.closing.preview("2026-11")
    assert pre["netProfit"] == "7500.00"
    assert pre["retainedDirection"] == "credit"
    assert pre["status"]["state"] == "not_closed"

    out = d.closing.transfer("2026-11")
    assert out["stableId"] == "close:2026-11"
    assert out["bookedOn"] == "2026-11-30"
    d.kernel.confirm_proposal(proposal_id=out["proposal"]["proposal_id"],
                              confirmed_by="cfo")
    assert d.closing.status("2026-11")["state"] == "posted"

    # ① 结转不进损益口径：已结转期间的利润表还是原数
    inc = d.statements.income_statement("2026-11")
    assert inc["revenue"] == "10000.00"
    assert inc["otherOpex"] == "2500.00"
    assert inc["operatingProfit"] == "7500.00"

    # ②③ 资产负债表仍平，未结转损益归零，3100 = 本期净损益
    bs = d.statements.balance_sheet("2026-11")
    assert bs["balanced"] is True, bs.get("error")
    assert bs["unclosedProfit"] == "0.00"
    equity = {line["accountCode"]: line["amount"] for line in bs["equity"]["lines"]}
    assert equity["3100"] == "7500.00"
    assert Decimal(bs["assets"]["total"]) == Decimal("7500")

    # ④ 幂等：不重复过账
    again = d.closing.transfer("2026-11")
    assert again["duplicate"] is True and again["posted"] is True
    pending = d.kernel.list_proposals(status="pending")["data"]
    assert [p for p in pending if p["stable_id"].startswith("close:2026-11")] == []
    balances = {r["account_code"]: r["balance"] for r in d.kernel.query_balance()["data"]}
    assert balances["4000"] == "0.000000"        # 收入已结平
    assert balances["3100"] == "-7500.000000"    # 贷方 7500


def test_transfer_is_refused_on_a_closed_period(client_faces):
    d = client_faces.app.state.drivers
    _post(d, "close:oct", [
        {"account_code": "5200", "direction": "debit", "amount": "100",
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": "100",
         "currency": "CNY"}], booked_on="2026-10-05")
    d.periods.close("2026-10", actor="cfo")

    with pytest.raises(Exception) as ei:
        d.closing.transfer("2026-10")
    assert "已关账" in str(ei.value)


def test_no_profit_activity_is_noop(client_faces):
    d = client_faces.app.state.drivers
    _post(d, "close:cash-only", [
        {"account_code": "1000", "direction": "debit", "amount": "500",
         "currency": "CNY"},
        {"account_code": "3000", "direction": "credit", "amount": "500",
         "currency": "CNY"}], booked_on="2026-11-07")

    out = d.closing.transfer("2026-11")
    assert out["noop"] is True and out["proposal"] is None
    assert d.closing.status("2026-11")["state"] == "not_closed"
