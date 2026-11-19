"""往来对账单装配（change: add-party-statement 任务 2.1）。

判据：真库落客户赊销与回款 → 对账单期末 == 该科目总账净额（`consistent=true`），
逐行余额按科目走，分科目小计自洽。
"""
from __future__ import annotations

from decimal import Decimal


def _post(d, stable_id: str, lines: list[dict], booked_on: str) -> None:
    prop = d.kernel.submit_proposal(stable_id, lines, booked_on=booked_on)
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")


def test_party_statement_ties_to_the_ledger(client_faces):
    d = client_faces.app.state.drivers
    dims = {"customer": "C-A"}
    prop = d.kernel.submit_proposal("ps:sale", [
        {"account_code": "1100", "direction": "debit", "amount": "10000",
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": "10000",
         "currency": "CNY"}], dimensions=dims, booked_on="2026-11-05")
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")
    prop = d.kernel.submit_proposal("ps:pay", [
        {"account_code": "1000", "direction": "debit", "amount": "4000",
         "currency": "CNY"},
        {"account_code": "1100", "direction": "credit", "amount": "4000",
         "currency": "CNY"}], dimensions=dims, booked_on="2026-11-08")
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")

    out = d.books.party_statement("customer", "C-A", "2026-11",
                                  accounts=["1100", "4000", "1000"])
    subs = {s["accountCode"]: s for s in out["subtotals"]}
    assert subs["1100"]["closing"] == "6000.00"
    assert subs["4000"]["closing"] == "-10000.00"
    assert [r["balance"] for r in out["rows"]] == [
        "10000.00", "-10000.00", "4000.00", "6000.00"]
    assert out["consistent"] is True and out["reconciled"] is True

    # 对回总账：应收期末 == 科目余额表里 1100 的期末
    rows = {r["accountCode"]: r["closing"]
            for r in d.books.account_balances("2026-11")["rows"]}
    assert Decimal(rows["1100"]) == Decimal(subs["1100"]["closing"])


def test_party_statement_reports_an_unknown_party_as_empty(client_faces):
    d = client_faces.app.state.drivers
    out = d.books.party_statement("customer", "C-NONE", "2026-11")
    assert out["rows"] == [] and out["closingBalance"] == "0.00"
    assert "无发生额" in out["note"]
