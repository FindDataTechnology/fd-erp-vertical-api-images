"""建账期初装配（change: add-opening-balances 任务 2.1）。

判据：空账录期初 → 过门落账 → ①`preview` 与账簿期初列逐科目相等、②明细账期初跟着变、
③再录返回同一提案（不重复过账）、④已有分录时录期初被拒。
"""
from __future__ import annotations

import pytest

LINES = [
    {"account_code": "1100", "direction": "debit", "amount": "3000"},
    {"account_code": "1200", "direction": "debit", "amount": "2000"},
    {"account_code": "2000", "direction": "credit", "amount": "2000"},
    {"account_code": "3000", "direction": "credit", "amount": "3000"},
]


def test_opening_lands_in_the_books_and_is_idempotent(client_faces):
    d = client_faces.app.state.drivers
    out = d.opening.register(LINES, as_of="2026-01-01")
    assert out["stableId"].startswith("opening:")
    assert out["debit"] == out["credit"] == "5000.00"

    d.kernel.confirm_proposal(proposal_id=out["proposal"]["proposal_id"],
                              confirmed_by="cfo")
    assert d.opening.status()["state"] == "posted"

    pre = d.opening.preview()
    assert {r["accountCode"]: r["balance"] for r in pre["rows"]} == {
        "1100": "3000.00", "1200": "2000.00",
        "2000": "-2000.00", "3000": "-3000.00"}

    # ① 账簿期初列 == preview（期初到底录进去没有，只认这一条）
    bal = d.books.account_balances("2026-02")
    assert {r["accountCode"]: r["opening"] for r in bal["rows"]} == \
        {r["accountCode"]: r["balance"] for r in pre["rows"]}

    # ② 明细账期初也看得到
    assert d.books.subsidiary_ledger("1100", "2026-02")["opening"] == "3000.00"

    # ③ 再录不重复过账
    again = d.opening.register(LINES, as_of="2026-01-01")
    assert again["duplicate"] is True
    assert len(d.kernel.query_entries()["entries"]) == 4


def test_opening_is_refused_once_the_ledger_has_entries(client_faces):
    d = client_faces.app.state.drivers
    prop = d.kernel.submit_proposal("post:first", [
        {"account_code": "5200", "direction": "debit", "amount": "100",
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": "100",
         "currency": "CNY"}], booked_on="2026-11-05")
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")

    with pytest.raises(Exception) as ei:
        d.opening.register(LINES, as_of="2026-01-01")
    assert "空账" in str(ei.value)

    # 显式受权 + 理由才放行，且理由回显
    out = d.opening.register(LINES, as_of="2026-01-01",
                             allow_with_entries=True, reason="历史迁移")
    assert out["allowWithEntries"] is True and out["reason"] == "历史迁移"
