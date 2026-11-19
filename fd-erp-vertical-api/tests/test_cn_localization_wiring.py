"""中国本地化装配（change: add-cn-localization-pack 任务 5.1）。

判据：真库里走一遍完整链条——
含税销售 → 含税采购 → 两条 proposal 过门 → ①资产负债表按小企业准则项目出且自平、
②利润表把税从收入里拆出、③增值税申报口径与账面一致、④月末转出未交增值税且可重复不重复。
"""
from __future__ import annotations

from decimal import Decimal

def _confirm(d) -> None:
    for prop in d.kernel.list_proposals(status="pending")["data"]:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo")


def test_taxed_month_closes_the_cn_books(client_faces):
    d = client_faces.app.state.drivers
    # 资本投入 1000：让资产负债表有权益侧
    cap = d.kernel.submit_proposal(
        "cn:cap", [{"account_code": "1000", "direction": "debit", "amount": "1000",
                    "currency": "CNY"},
                   {"account_code": "3000", "direction": "credit", "amount": "1000",
                    "currency": "CNY"}], booked_on="2026-11-01")
    d.kernel.confirm_proposal(proposal_id=cap["proposal_id"], confirmed_by="cfo")

    # 含税销售 113（13%）：收入 100 + 销项 13
    sale = d.cn_tax.post_sale("S-1", "113", rate="13", gross=True,
                              booked_on="2026-11-10", customer="C-A")
    assert sale["net"] == "100.00" and sale["tax"] == "13.00"
    # 含税采购 226（13%）：成本 200 + 进项 26
    buy = d.cn_tax.post_purchase("P-1", "226", rate="13", gross=True,
                                 booked_on="2026-11-12")
    assert buy["net"] == "200.00" and buy["tax"] == "26.00"
    _confirm(d)

    bs = d.cn_statements.balance_sheet("2026-11")
    assert bs["balanced"] is True, bs.get("error")
    assert bs["complete"] is True
    # 资产 = 现金 1000 + 应收 113 = 1113；负债 = 应付 226；应交税费 = 13 − 26 = −13（留抵）
    assert bs["totals"]["assets"] == "1113.00"
    taxes = next(r for r in bs["liabilities"] if r["key"] == "taxes_payable")
    assert taxes["amount"] == "-13.00"

    inc = d.cn_statements.income_statement("2026-11")
    line = {r["key"]: r["amount"] for r in inc["lines"]}
    assert line["revenue"] == "100.00"          # 不是 113：税被拆出去了
    assert line["cost_of_sales"] == "200.00"
    assert inc["totals"]["netProfit"] == "-100.00"

    vat = d.cn_statements.vat_return("2026-11")
    # 应纳下限为 0：留抵是「结转下期」而不是负应纳（change: add-cn-vat-credit-carry-forward）
    assert vat["ledger"] == {"sales": "100.00", "outputVat": "13.00",
                             "inputVat": "26.00", "inputTransferOut": "0.00",
                             "openingCredit": "0.00", "payable": "0.00"}
    # 进项大于销项：留抵，不结转；重复调用仍 noop
    closed = d.cn_tax.close_vat("2026-11")
    assert closed["noop"] is True
    assert closed["creditCarryForward"] == "13.00"
    assert d.cn_tax.close_vat("2026-11")["noop"] is True


def test_payable_month_transfers_and_is_idempotent(client_faces):
    d = client_faces.app.state.drivers
    cap = d.kernel.submit_proposal(
        "cn2:cap", [{"account_code": "1000", "direction": "debit", "amount": "500",
                     "currency": "CNY"},
                    {"account_code": "3000", "direction": "credit", "amount": "500",
                     "currency": "CNY"}], booked_on="2026-11-01")
    d.kernel.confirm_proposal(proposal_id=cap["proposal_id"], confirmed_by="cfo")

    d.cn_tax.post_sale("S-9", "113", rate="13", gross=True, booked_on="2026-11-05")
    _confirm(d)

    first = d.cn_tax.close_vat("2026-11")
    assert first["noop"] is False and first["amount"] == "13.00"
    _confirm(d)
    again = d.cn_tax.close_vat("2026-11")
    assert again["noop"] is True or again["duplicate"] is True

    bs = d.cn_statements.balance_sheet("2026-11")
    assert bs["balanced"] is True, bs.get("error")
    taxes = next(r for r in bs["liabilities"] if r["key"] == "taxes_payable")
    # 转出后：2221.01 归零，2221.02 挂 13（都在 2221 子树里，合计仍是 13）
    assert Decimal(taxes["amount"]) == Decimal("13")
