"""应付账龄装配（change: add-payable-aging 任务 2.1）。

判据：真库落两笔带 `customer` 维度的应付 + 一笔付款，账龄表分桶正确，
且各桶合计 − 预付 = 2000 科目贷方净额（账龄与账本必须咬合）。
应付侧与应收侧读的是同一个引擎，只反转开放方向。
"""
from __future__ import annotations

from decimal import Decimal


def _entry(d, stable_id: str, lines: list[dict], dimensions: dict | None = None) -> None:
    prop = d.kernel.submit_proposal(stable_id, lines, dimensions=dimensions or {})
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")


def _payable(amount: str, **kw) -> list[dict]:
    return [
        {"account_code": "2000", "direction": "credit", "amount": amount,
         "currency": "CNY"},
        {"account_code": "1200", "direction": "debit", "amount": amount,
         "currency": "CNY"}]


def _payment(amount: str) -> list[dict]:
    return [
        {"account_code": "2000", "direction": "debit", "amount": amount,
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": amount,
         "currency": "CNY"}]


def test_payable_aging_matches_the_payable_balance(client_faces):
    d = client_faces.app.state.drivers
    _entry(d, "ap:s-1", _payable("500"), {"customer": "S-A"})
    _entry(d, "ap:s-2", _payable("300"), {"customer": "S-A"})
    _entry(d, "ap:none", _payable("200"))

    out = d.payables.report()
    assert out["side"] == "credit"
    assert out["account"] == "2000"
    assert out["consistent"] is True, out.get("error")
    rows = {r["dimensionValue"]: r for r in out["rows"]}
    assert rows["S-A"]["outstanding"] == "800.00"
    assert rows["unattributed"]["outstanding"] == "200.00"
    net = next(r["balance"] for r in d.kernel.query_balance(account_code="2000")["data"])
    # 内核侧 2000 的余额符号与我们「开放方向为正」相反，取绝对值比
    assert Decimal(out["totalOutstanding"]) == abs(Decimal(net)) == Decimal("1000")


def test_payment_clears_oldest_payable_and_overpayment_is_prepaid(client_faces):
    d = client_faces.app.state.drivers
    _entry(d, "ap:t-1", _payable("100"), {"customer": "S-T"})
    _entry(d, "ap:t-pay", _payment("150"))

    # 付款未标供应商 → 落到 unattributed，不会替 S-T 冲账
    out = d.payables.report()
    rows = {r["dimensionValue"]: r for r in out["rows"]}
    assert rows["S-T"]["outstanding"] == "100.00"
    assert rows["unattributed"]["debitBalance"] == "150.00"
    assert out["consistent"] is True

    # 标了供应商的多付 → 该对象报预付，不出负账龄
    _entry(d, "ap:u-1", _payable("200"), {"customer": "S-U"})
    _entry(d, "ap:u-pay", _payment("350"), {"customer": "S-U"})
    out = d.payables.report()
    row = {r["dimensionValue"]: r for r in out["rows"]}["S-U"]
    assert row["outstanding"] == "0.00"
    assert row["debitBalance"] == "150.00"     # 350 − 200
    assert out["consistent"] is True
