"""上期留抵结转装配（change: add-cn-vat-credit-carry-forward 任务 2.6）。

判据：真库跨两个月的完整链条——

1. 10 月：销项 130 / 进项 260 → 应纳 0、结转 130、`close_vat` noop（不留结转分录）；
2. 11 月：销项 260、无进项 → `openingCredit` 130、应纳 130（不是 260）；
3. 附加税按 130 计提（城建 9.10），且与 5403 勾稽一致；
4. 11 月结转 130 后 `2221.01` 子树归零，12 月 `openingCredit` 为 0；
5. 申报口径（`vat_return`）的 payable / openingCredit 与计提口径（`position`）逐值相同。
"""
from __future__ import annotations

RATES = {"city_construction": "7", "education_surcharge": "3",
         "local_education_surcharge": "2", "income_tax": "25"}


def _confirm(d) -> None:
    for prop in d.kernel.list_proposals(status="pending")["data"]:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo")


def _cap(d, amount="1000") -> None:
    prop = d.kernel.submit_proposal(
        "cf:cap", [{"account_code": "1000", "direction": "debit",
                    "amount": amount, "currency": "CNY"},
                   {"account_code": "3000", "direction": "credit",
                    "amount": amount, "currency": "CNY"}], booked_on="2026-10-01")
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")


def test_credit_month_then_next_month_deducts_it(client_faces):
    d = client_faces.app.state.drivers
    _cap(d)

    # 10 月：销项 1000（1130 含税）→ 税 130；进项 2000（2260 含税）→ 税 260
    d.cn_tax.post_sale("CF-S10", "1130", rate="13", gross=True, booked_on="2026-10-10")
    d.cn_tax.post_purchase("CF-P10", "2260", rate="13", gross=True,
                           booked_on="2026-10-12")
    _confirm(d)

    oct_pos = d.cn_tax.position("2026-10")
    assert oct_pos["payable"] == "0.00"
    assert oct_pos["creditCarryForward"] == "130.00"
    oct_close = d.cn_tax.close_vat("2026-10")
    assert oct_close["noop"] is True
    _confirm(d)

    # 11 月：销项 2000（2260 含税）→ 税 260，无进项 → 应纳 130
    d.cn_tax.post_sale("CF-S11", "2260", rate="13", gross=True, booked_on="2026-11-10")
    _confirm(d)

    nov_pos = d.cn_tax.position("2026-11")
    assert nov_pos["openingCredit"] == "130.00"
    assert nov_pos["payable"] == "130.00"

    vat = d.cn_statements.vat_return("2026-11")
    assert vat["ledger"]["openingCredit"] == "130.00"
    assert vat["ledger"]["payable"] == nov_pos["payable"]

    d.cn_filing.register_rates(RATES, effective_from="2026-01", source="现行税法")
    sur = d.cn_filing.surcharges("2026-11")
    assert sur["base"] == "130.00"
    assert sur["items"][0]["amount"] == "9.10"       # 城建 7%
    _confirm(d)

    row = next(r for r in d.cn_filing.filing_summary("2026-11")["reconciliation"]
               if r["item"] == "surcharge")
    assert row["difference"] == "0.00"

    # 结转 70 后子树归零：12 月不再有留抵
    d.cn_tax.close_vat("2026-11")
    _confirm(d)
    dec_pos = d.cn_tax.position("2026-12")
    assert dec_pos["openingCredit"] == "0.00"
    assert dec_pos["payable"] == "0.00"
