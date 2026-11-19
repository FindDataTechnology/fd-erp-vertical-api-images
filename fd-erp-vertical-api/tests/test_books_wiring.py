"""账簿装配（change: add-ledger-books 任务 3.1）。

判据：真库落赊销/收现/付现 → ①科目余额表三栏自洽且借贷合计相等、②期末列与试算平衡表逐科目
相等、③明细账逐行余额闭合且期末 == **内核余额**（真账对账，不是自己跟自己比）、
④上月分录进本月期初。
"""
from __future__ import annotations

from decimal import Decimal


def _post(d, stable_id: str, lines: list[dict], **kw) -> dict:
    prop = d.kernel.submit_proposal(stable_id, lines, **kw)
    return d.kernel.confirm_proposal(proposal_id=prop["proposal_id"],
                                     confirmed_by="cfo")


def _seed(d) -> None:
    _post(d, "bk:oct-sale", [
        {"account_code": "1100", "direction": "debit", "amount": "1000",
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": "1000",
         "currency": "CNY"}], booked_on="2026-10-20")
    _post(d, "bk:nov-sale", [
        {"account_code": "1100", "direction": "debit", "amount": "10000",
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": "10000",
         "currency": "CNY"}], booked_on="2026-11-05")
    _post(d, "bk:nov-collect", [
        {"account_code": "1000", "direction": "debit", "amount": "4000",
         "currency": "CNY"},
        {"account_code": "1100", "direction": "credit", "amount": "4000",
         "currency": "CNY"}], booked_on="2026-11-08")
    _post(d, "bk:nov-exp", [
        {"account_code": "5200", "direction": "debit", "amount": "1200",
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": "1200",
         "currency": "CNY"}], booked_on="2026-11-09")


def test_books_agree_with_the_kernel_and_the_statements(client_faces):
    d = client_faces.app.state.drivers
    _seed(d)

    bal = d.books.account_balances("2026-11")
    rows = {r["accountCode"]: r for r in bal["rows"]}
    assert rows["1100"]["opening"] == "1000.00"      # 10 月赊销进了期初
    assert rows["1100"]["debit"] == "10000.00"
    assert rows["1100"]["credit"] == "4000.00"
    assert rows["1100"]["closing"] == "7000.00"
    assert rows["1000"]["closing"] == "2800.00"      # 收现 4000 − 付现 1200
    assert bal["totals"]["debit"] == bal["totals"]["credit"]
    assert bal["balanced"] is True
    for row in bal["rows"]:
        assert Decimal(row["opening"]) + Decimal(row["debit"]) \
            - Decimal(row["credit"]) == Decimal(row["closing"])

    # ② 两条独立取数路径：科目余额表期末 == 试算平衡表余额
    tb = d.statements.trial_balance("2026-11")
    assert {r["accountCode"]: r["closing"] for r in bal["rows"]} == \
        {r["accountCode"]: r["balance"] for r in tb["rows"]}

    # ③ 明细账：逐行余额闭合，且期末 == 内核余额（真账对账）
    sub = d.books.subsidiary_ledger("1100", "2026-11")
    assert sub["opening"] == "1000.00"
    assert [r["balance"] for r in sub["rows"]] == ["11000.00", "7000.00"]
    assert [r["txnId"] for r in sub["rows"]]
    assert sub["closingBalance"] == "7000.00"
    kernel_balance = {r["account_code"]: r["balance"]
                      for r in d.kernel.query_balance()["data"]}
    assert Decimal(sub["closingBalance"]) == Decimal(kernel_balance["1100"])


def test_subsidiary_covers_all_time_when_no_period_is_given(client_faces):
    d = client_faces.app.state.drivers
    _seed(d)
    sub = d.books.subsidiary_ledger("1100")
    assert sub["opening"] == "0.00"
    assert len(sub["rows"]) == 3                     # 10 月一笔 + 11 月两笔
    assert sub["closingBalance"] == "7000.00"


def test_empty_account_is_reported_not_faked(client_faces):
    d = client_faces.app.state.drivers
    _seed(d)
    sub = d.books.subsidiary_ledger("1601", "2026-11")
    assert sub["rows"] == [] and "无发生额" in sub["note"]
    assert sub["closingBalance"] == "0.00"
