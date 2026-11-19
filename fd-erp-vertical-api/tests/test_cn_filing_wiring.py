"""附加税与所得税预缴装配（change: add-cn-surcharge-and-income-tax 任务 3.1）。

判据：真库里两条依赖各走一遍——

1. **附加税随增值税**：销售 1130（销项 130）+ 采购 565（进项 65）→ 应纳 65 →
   附加税 4.55 / 1.95 / 1.30；留抵月份不再产生附加税（基数是「应纳」不是销售额）。
2. **所得税跨月补差**：11 月累计利润 400 → 预缴 100；12 月累计 800 → 只补 100，
   不是再缴 200；进项税额不可抵的上游（普票）不改变这两条判据。
"""
from __future__ import annotations

from decimal import Decimal

RATES = {"city_construction": "7", "education_surcharge": "3",
         "local_education_surcharge": "2", "income_tax": "25"}


def _confirm(d) -> None:
    for prop in d.kernel.list_proposals(status="pending")["data"]:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo")


def _cap(d, amount="1000", ref="cnf:cap") -> None:
    prop = d.kernel.submit_proposal(
        ref, [{"account_code": "1000", "direction": "debit",
               "amount": amount, "currency": "CNY"},
              {"account_code": "3000", "direction": "credit",
               "amount": amount, "currency": "CNY"}], booked_on="2026-11-01")
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")


def _rates(d) -> None:
    d.cn_filing.register_rates(RATES, effective_from="2026-01",
                               source="现行税法：市区 7%、教育费附加 3%、地方 2%")


def test_surcharges_follow_payable_vat(client_faces):
    d = client_faces.app.state.drivers
    _cap(d)
    d.cn_tax.post_sale("F-S1", "1130", rate="13", gross=True, booked_on="2026-11-10")
    d.cn_tax.post_purchase("F-P1", "565", rate="13", gross=True,
                           booked_on="2026-11-12")
    _confirm(d)
    _rates(d)

    out = d.cn_filing.surcharges("2026-11")
    assert out["base"] == "65.00"             # 应纳 = 130 − 65，不是销项、更不是销售额
    assert [i["amount"] for i in out["items"]] == ["4.55", "1.95", "1.30"]
    assert out["total"] == "7.80"

    _confirm(d)
    summary = d.cn_filing.filing_summary("2026-11")
    assert summary["surcharges"]["total"] == "7.80"
    surcharge_row = next(r for r in summary["reconciliation"] if r["item"] == "surcharge")
    assert surcharge_row["difference"] == "0.00"

    # 账面真落了 5403 与三个应交税费科目 → 资产负债表仍平（0019 四个科目进损益口径的回归判据）
    bs = d.cn_statements.balance_sheet("2026-11")
    assert bs["balanced"] is True, bs.get("error")


def test_a_credit_carry_forward_month_has_no_surcharge(client_faces):
    d = client_faces.app.state.drivers
    _cap(d)
    d.cn_tax.post_sale("F-S2", "113", rate="13", gross=True, booked_on="2026-11-10")
    d.cn_tax.post_purchase("F-P2", "226", rate="13", gross=True,
                           booked_on="2026-11-12")
    _confirm(d)
    _rates(d)
    out = d.cn_filing.surcharges("2026-11")
    assert out["noop"] is True and out["total"] == "0.00"


def test_income_tax_tops_up_across_months(client_faces):
    d = client_faces.app.state.drivers
    _cap(d)
    _rates(d)

    def earn(ref: str, amount: str, on: str) -> None:
        prop = d.kernel.submit_proposal(
            ref, [{"account_code": "1000", "direction": "debit", "amount": amount,
                   "currency": "CNY"},
                  {"account_code": "4000", "direction": "credit", "amount": amount,
                   "currency": "CNY"}], booked_on=on)
        d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")

    earn("F-R11", "400", "2026-11-10")
    nov = d.cn_filing.income_tax_prepay("2026-11")
    assert nov["cumulativeProfit"] == "400.00"
    assert nov["target"] == "100.00" and nov["due"] == "100.00"
    _confirm(d)

    earn("F-R12", "400", "2026-12-10")
    dec = d.cn_filing.income_tax_prepay("2026-12")
    assert dec["cumulativeProfit"] == "800.00"
    assert dec["target"] == "200.00"
    assert dec["alreadyPrepaid"] == "100.00"
    assert dec["due"] == "100.00"              # 补差，不是再缴 200
    _confirm(d)

    # 过门后账上的应交所得税：11 月 100 + 12 月 100 = 200（贷方）
    rows = d.books.account_balances("2026-12")["rows"]
    row = next(r for r in rows if r["accountCode"] == "2221.11")
    assert Decimal(row["closing"]) == Decimal("-200")
    # 勾稽：12 月本期应计提 = 200 − 100（本期之前）= 100，与 5801 本期发生额一致；
    # 已计提后「剩余应缴」为 0——两个数是两件事，混在一起勾稽就永远勾不上
    summary = d.cn_filing.filing_summary("2026-12")
    assert summary["consistent"] is True, summary["reconciliation"]
    assert Decimal(summary["incomeTax"]["accrued"]) == Decimal("100")
    assert Decimal(summary["incomeTax"]["due"]) == Decimal("0")
