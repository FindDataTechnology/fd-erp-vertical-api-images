"""发票台账装配（change: add-cn-invoice-register 任务 4.1）。

判据：真库里三张票走三条路——普票（不可抵）、未认证专票（不可抵，进待认证）、已认证专票
（可抵）——过门后①账上进项只等于已认证专票那 13；②申报口径三档各 13；③台账与账面勾稽一致；
④同一张票不能抵两次。
"""
from __future__ import annotations

from decimal import Decimal


def _confirm(d) -> None:
    for prop in d.kernel.list_proposals(status="pending")["data"]:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo")


def _cap(d, amount="1000") -> None:
    prop = d.kernel.submit_proposal(
        "inv:cap", [{"account_code": "1000", "direction": "debit",
                     "amount": amount, "currency": "CNY"},
                    {"account_code": "3000", "direction": "credit",
                     "amount": amount, "currency": "CNY"}], booked_on="2026-11-01")
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")


def test_only_certified_special_invoices_reach_the_ledger(client_faces):
    d = client_faces.app.state.drivers
    _cap(d)
    # 三张进项票，各 113 含税（13%）
    d.cn_invoices.register("purchase", "vat-general", "P-1", "100", rate="13",
                           issued_on="2026-11-05")
    d.cn_invoices.register("purchase", "vat-special", "S-1", "100", rate="13",
                           issued_on="2026-11-05")
    d.cn_invoices.register("purchase", "vat-special", "S-2", "100", rate="13",
                           issued_on="2026-11-05")
    d.cn_invoices.certify("S-2", certified_on="2026-11-06", by="cfo")

    # 三张都过账：只有 S-2 的税进进项，另两张的税进成本
    d.cn_invoices.purchase("B-1", "P-1", "113", booked_on="2026-11-07")
    d.cn_invoices.purchase("B-2", "S-1", "113", booked_on="2026-11-07")
    d.cn_invoices.purchase("B-3", "S-2", "113", booked_on="2026-11-07")
    _confirm(d)

    vat = d.cn_statements.vat_return("2026-11", invoices=d.cn_invoices)
    assert vat["ledger"]["inputVat"] == "13.00"          # 只有已认证的专票
    assert vat["inputTiers"] == {"deductible": "13.00",
                                 "pendingCertification": "13.00",
                                 "nonDeductible": "13.00"}
    assert vat["invoiceReconcile"]["consistent"] is True

    # 成本科目：不可抵的两张按含税 113 进，可抵那张按净额 100 进 → 326
    bs = d.cn_statements.income_statement("2026-11")
    cost = next(r for r in bs["lines"] if r["key"] == "cost_of_sales")
    assert Decimal(cost["amount"]) == Decimal("326")

    # 同一张票不能抵两次
    try:
        d.cn_invoices.purchase("B-4", "S-2", "113", booked_on="2026-11-08")
    except Exception as exc:                          # noqa: BLE001
        assert "不能抵两次" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("同一张进项票重复抵扣必须拒绝")


def test_sales_matrix_splits_by_rate_and_kind(client_faces):
    d = client_faces.app.state.drivers
    _cap(d)
    d.cn_invoices.register("sale", "vat-special", "A-1", "100", rate="13",
                           issued_on="2026-11-05", counterparty="C-A")
    d.cn_invoices.register("sale", "vat-general", "A-2", "200", rate="13",
                           issued_on="2026-11-06", counterparty="C-B")
    d.cn_invoices.register("sale", "e-special", "A-3", "100", rate="6",
                           issued_on="2026-11-07", counterparty="C-C")

    out = d.cn_invoices.filing_summary("2026-11")
    rows = {(r["rate"], r["invoiceKind"]): (r["sales"], r["tax"])
            for r in out["salesByRateAndKind"]}
    assert rows[("13", "vat-special")] == ("100.00", "13.00")
    assert rows[("13", "vat-general")] == ("200.00", "26.00")
    assert rows[("6", "e-special")] == ("100.00", "6.00")
