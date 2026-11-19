"""进项转出与红字发票装配（change: add-cn-red-letter-and-input-transfer-out 任务 4.1）。

两条判据都盯「应纳增值税这个数」——附加税与所得税挂在它上面：

1. **转出上抬应纳，附加税跟着上抬**：销项 130 / 进项 65 → 应纳 65 → 附加税 7.80；
   转出 13 后应纳 78 → 附加税 9.36。先转出再计提附加税，勾稽必须一致。
2. **红字冲减销项净额**：蓝票 100（税 13）+ 红票 30（税 -3.90）过账后，
   账面销项 9.10，申报矩阵同一条「税率 × 票种」行净额 70 / 9.10。
"""
from __future__ import annotations

RATES = {"city_construction": "7", "education_surcharge": "3",
         "local_education_surcharge": "2", "income_tax": "25"}


def _confirm(d) -> None:
    for prop in d.kernel.list_proposals(status="pending")["data"]:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo")


def _cap(d, amount="1000") -> None:
    prop = d.kernel.submit_proposal(
        "rl:cap", [{"account_code": "1000", "direction": "debit",
                    "amount": amount, "currency": "CNY"},
                   {"account_code": "3000", "direction": "credit",
                    "amount": amount, "currency": "CNY"}], booked_on="2026-11-01")
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")


def test_transfer_out_raises_payable_and_surcharges_follow(client_faces):
    d = client_faces.app.state.drivers
    _cap(d)
    d.cn_tax.post_sale("RL-S1", "1130", rate="13", gross=True, booked_on="2026-11-05")
    d.cn_tax.post_purchase("RL-P1", "565", rate="13", gross=True,
                           booked_on="2026-11-06")
    _confirm(d)

    before = d.cn_tax.position("2026-11")
    assert before["payable"] == "65.00" and before["inputTransferOut"] == "0.00"

    # 13 的进项用于集体福利：转出
    out = d.cn_tax.transfer_input_out("2026-11", reason="collective-welfare",
                                      amount="13", ref="W-1")
    assert out["remainingAfter"] == "52.00"
    _confirm(d)

    after = d.cn_tax.position("2026-11")
    assert after["inputTransferOut"] == "13.00"
    assert after["payable"] == "78.00"          # 65 + 13

    d.cn_filing.register_rates(RATES, effective_from="2026-01", source="现行税法")
    sur = d.cn_filing.surcharges("2026-11")
    assert sur["base"] == "78.00"
    assert sur["total"] == "9.36"               # 5.46 + 2.34 + 1.56
    _confirm(d)

    summary = d.cn_filing.filing_summary("2026-11")
    assert summary["surcharges"]["base"] == "78.00"
    row = next(r for r in summary["reconciliation"] if r["item"] == "surcharge")
    # 只验附加税这一项：所得税本用例没计提，整表 consistent 本就该是 False
    assert row["computed"] == row["ledger"] == "9.36" and row["difference"] == "0.00"


def test_red_letter_nets_down_output_vat(client_faces):
    d = client_faces.app.state.drivers
    _cap(d)
    d.cn_invoices.register("sale", "vat-special", "BLUE-1", "100", rate="13",
                           issued_on="2026-11-05", counterparty="C-A")
    # 蓝票过账借应收：先确认资本与应收都在，红票才有东西可冲
    blue = d.kernel.submit_proposal(
        "rl:blue", [{"account_code": "1100", "direction": "debit", "amount": "113",
                     "currency": "CNY"},
                    {"account_code": "4000", "direction": "credit", "amount": "100",
                     "currency": "CNY"},
                    {"account_code": "2221.01.01", "direction": "credit",
                     "amount": "13", "currency": "CNY"}], booked_on="2026-11-05")
    d.kernel.confirm_proposal(proposal_id=blue["proposal_id"], confirmed_by="cfo")

    red = d.cn_invoices.register_red("sale", "vat-special", "RED-1", "30",
                                     red_of="BLUE-1", reason="退货",
                                     issued_on="2026-11-20")
    assert red["remainingAfter"] == "70.00"
    d.cn_invoices.post_red("RL-R1", "RED-1", "33.90", booked_on="2026-11-20")
    _confirm(d)

    vat = d.cn_statements.vat_return("2026-11")
    assert vat["ledger"]["outputVat"] == "9.10"        # 13 − 3.90
    assert vat["ledger"]["sales"] == "70.00"           # 100 − 30
    assert vat["consistent"] is True

    summary = d.cn_invoices.filing_summary("2026-11")
    row = next(r for r in summary["salesByRateAndKind"]
               if r["invoiceKind"] == "vat-special")
    assert (row["sales"], row["tax"]) == ("70.00", "9.10")
    assert summary["redLetters"]["tax"] == "-3.90"
