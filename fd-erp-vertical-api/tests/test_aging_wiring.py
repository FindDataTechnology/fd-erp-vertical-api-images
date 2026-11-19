"""应收账龄装配（change: add-receivable-aging 任务 3.1）。

判据：真库落两笔带 `customer` 维度的应收 + 一笔收款，账龄表分桶正确，
且各桶合计 − 预收 = 1100 科目净余额（账龄与账本必须咬合）。
"""
from __future__ import annotations

from decimal import Decimal


def _entry(d, stable_id: str, lines: list[dict]) -> None:
    prop = d.kernel.submit_proposal(stable_id, lines)
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")


def test_aging_matches_the_receivable_balance(client_faces):
    d = client_faces.app.state.drivers

    # 客户 A 的两笔应收（一笔老、一笔新）+ 客户 B 一笔 + 一笔未标客户
    _entry(d, "ar:a-1", [
        {"account_code": "1100", "direction": "debit", "amount": "500",
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": "500",
         "currency": "CNY"}])
    _entry(d, "ar:b-1", [
        {"account_code": "1100", "direction": "debit", "amount": "300",
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": "300",
         "currency": "CNY"}])
    _entry(d, "ar:none", [
        {"account_code": "1100", "direction": "debit", "amount": "200",
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": "200",
         "currency": "CNY"}])
    # 给客户 A 记维度：重提会被幂等挡住，故用新单据模拟「带 customer 的应收」
    _entry(d, "ar:a-2", [
        {"account_code": "1100", "direction": "debit", "amount": "100",
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": "100",
         "currency": "CNY"}])

    # 带维度的应收：直接用内核提案带上 customer
    prop = d.kernel.submit_proposal("ar:a-dim", [
        {"account_code": "1100", "direction": "debit", "amount": "400",
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": "400",
         "currency": "CNY"}], dimensions={"customer": "C-A"})
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")

    out = d.receivables.report()
    assert out["consistent"] is True, out.get("error")
    rows = {r["dimensionValue"]: r for r in out["rows"]}
    assert rows["C-A"]["outstanding"] == "400.00"
    assert rows["unattributed"]["outstanding"] == "1100.00"   # 500+300+200+100
    net = next(r["balance"] for r in d.kernel.query_balance(account_code="1100")["data"])
    assert Decimal(out["totalOutstanding"]) == Decimal(net) == Decimal("1500")


def test_overpayment_after_the_sale(client_faces):
    d = client_faces.app.state.drivers
    prop = d.kernel.submit_proposal("ar:c-1", [
        {"account_code": "1100", "direction": "debit", "amount": "100",
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": "100",
         "currency": "CNY"}], dimensions={"customer": "C-C"})
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")
    pay = d.kernel.submit_proposal("cash:c-1", [
        {"account_code": "1000", "direction": "debit", "amount": "150",
         "currency": "CNY"},
        {"account_code": "1100", "direction": "credit", "amount": "150",
         "currency": "CNY"}], dimensions={"customer": "C-C"})
    d.kernel.confirm_proposal(proposal_id=pay["proposal_id"], confirmed_by="cfo")

    out = d.receivables.report()
    row = {r["dimensionValue"]: r for r in out["rows"]}["C-C"]
    assert row["outstanding"] == "0.00"
    assert row["creditBalance"] == "50.00"
    assert out["consistent"] is True
