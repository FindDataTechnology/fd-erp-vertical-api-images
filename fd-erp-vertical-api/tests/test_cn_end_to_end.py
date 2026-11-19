"""中国财务链端到端验收（一次走完：期初 → 发票 → 过账 → 销售 → 报表 → 申报 → 结转）。

为什么要有这一个文件：单条车道的用例都绿，不代表**链子**是通的。这仓踩过太多次
「符号只有测试引用＝没上线」「代码在、通路没接」——每一段各自自洽，接起来断在中间。
所以这里不新增任何能力，只按业务真实顺序把七段串起来，用**跨段不变量**判：

1. 期初入账后，资产负债表仍平，且期初余额进入对应科目；
2. 发票登记 + 认证后过账、销售计提，都经确认门成为事实；
3. 三张法定报表同时成立：资产负债表平、利润表有数、现金流量表两条自证都过；
4. 两条跨账对账同时归零：账上进项 == 台账可抵；账面口径 == 申报口径；
5. **结转前后不变的不变量**：净利润、资产总计、期末现金在结转前后逐值相同
   （结转只把损益搬进权益，不改变「这期赚了多少」和「账上还有多少钱」）；
6. 结转后仍出得来完整报表：未结转损益归零、利润表不因结转分录归零。

零替身：真库 + InProcess 内核 + 真实车道。
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

#: 财务任职名单：第四道闸（按人授权）要用它，否则 fi 端点一律 403。
STAFF_ROSTER = json.dumps({"read": ["cfo@example"],
                            "write": ["cfo@example"],
                            "confirm": ["cfo@example"]})

TENANT = "tnt-demo"
PERIOD = "2026-11"
RATES = {"city_construction": "7", "education_surcharge": "3",
         "local_education_surcharge": "2", "income_tax": "25"}
H = {"X-ERP-Tenant": TENANT}


@pytest.fixture()
def env(reset_db, tmp_path, dsn):
    import json

    from fastapi.testclient import TestClient

    from fd_erp_vertical_api.app import create_app
    from fd_erp_vertical_api.cart import CartStore
    from fd_erp_vertical_api.config import Settings
    from fd_erp_vertical_api.kernel_client import InProcessKernelClient
    from fd_erp_vertical_api.pagesets import load_pageset_auth

    payload = json.dumps({"supply-chain.fi": ["finance.read", "finance.write",
                                             "finance.confirm"]})
    settings = Settings(tenant_id=TENANT, database_url=dsn, base_currency="CNY",
                        data_dir=str(tmp_path / "data"), pageset_auth_json=payload,
                        finance_staff_json=STAFF_ROSTER)
    kernel = InProcessKernelClient(settings)
    app = create_app(settings, kernel,
                     pageset_auth=load_pageset_auth(inline_json=payload),
                     cart_store=CartStore())
    client = TestClient(app)
    client.app.state.drivers.cn_filing.register_rates(
        RATES, effective_from="2026-01", source="端到端验收：现行税法")
    return client.app.state.drivers


def _confirm_all(d) -> int:
    """过确认门：把所有待确认提案确认为事实（验收里我们扮演财务）。"""
    pending = d.kernel.list_proposals(status="pending")["data"]
    for prop in pending:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")
    return len(pending)


def _line(rows, key):
    return next(r for r in rows if r["key"] == key)


def test_the_whole_cn_finance_chain_holds_together(env):
    d = env
    # ---- 1) 建账期初：现金 5000 / 实收资本 5000（只在空账上可录） ----
    opening = d.opening.register(
        [{"account_code": "1000", "direction": "debit", "amount": "5000"},
         {"account_code": "3000", "direction": "credit", "amount": "5000"}],
        as_of="2026-10-31")
    assert opening.get("duplicate") is not True, opening
    assert _confirm_all(d) == 1

    bs = d.cn_statements.balance_sheet("2026-10")
    assert bs["balanced"] is True, bs.get("error")
    assert _line(bs["assets"], "cash")["amount"] == "5000.00"


    # ---- 2) 采购进项票：登记 → 认证 → 过账（含税 1130，进项 130） ----
    d.cn_invoices.register("purchase", "vat-special", "E2E-P1", net="1000",
                           rate="13", issued_on="2026-11-05")
    d.cn_invoices.certify("E2E-P1", certified_on="2026-11-06", by="cfo@example")
    d.cn_invoices.purchase("inv:E2E-P1", "E2E-P1", "1130",
                           booked_on="2026-11-12", submitted_by="cfo@example")
    # ---- 销售：含税 2260（收入 2000 + 销项 260） ----
    d.cn_tax.post_sale("E2E-S1", "2260", rate="13", gross=True,
                       booked_on="2026-11-15", submitted_by="cfo@example")
    confirmed = _confirm_all(d)
    assert confirmed >= 2, confirmed          # 过账 + 销售各一张提案

    # ---- 3) 三张法定报表同时成立 ----
    bs = d.cn_statements.balance_sheet(PERIOD)
    inc = d.cn_statements.income_statement(PERIOD)
    flow = d.cn_statements.cash_flow(PERIOD)
    assert bs["balanced"] is True, bs.get("error")
    assert bs["complete"] is True, bs.get("unmapped")
    # 现金 5000（期初）+ 应收 2260（销售挂应收，不走现金）= 7260；成本 1000 进损益
    assert bs["totals"]["assets"] == "7260.00"
    assert inc["totals"]["netProfit"] == "1000.00"    # 收入 2000 − 成本 1000
    assert flow["consistent"] is True, flow.get("note") or flow.get("error")
    assert flow["sumCheck"]["ok"] is True
    assert flow["balanceCheck"]["ok"] is True
    assert flow["openingCash"] == "5000.00"           # 期初现金在 11 月 1 日之前
    assert flow["closingCash"] == "5000.00"           # 本期两笔都不动现金（挂应收/应付）
    assert flow["netIncrease"] == "0.00"

    # ---- 4) 两条跨账对账同时归零 ----
    inv_recon = d.cn_invoices.reconcile(PERIOD)
    assert inv_recon["consistent"] is True, inv_recon
    assert inv_recon["ledgerInputVat"] == inv_recon["invoiceInputVat"] == "130.00"

    d.cn_filing.register_vat_return(
        PERIOD, source="电子税局 2026-11 申报表", sales="2000",
        outputVat="260", inputVat="130", inputTransferOut="0", openingCredit="0")
    vat = d.cn_filing.vat_reconciliation(PERIOD)
    assert vat["provided"] is True
    assert vat["consistent"] is True, vat["reconciliation"]
    assert vat["declared"]["payable"] == "130.00"

    # ---- 5) 预演不写账 ----
    pending_before = len(d.kernel.list_proposals(status="pending")["data"])
    surcharge = d.cn_filing.surcharges(PERIOD, preview=True)
    prepay = d.cn_filing.income_tax_prepay(PERIOD, preview=True)
    assert surcharge["proposed"] is False and prepay["proposed"] is False
    assert len(d.kernel.list_proposals(status="pending")["data"]) == pending_before
    assert surcharge["base"] == "130.00"              # 应纳增值税
    assert prepay["cumulativeProfit"] == "1000.00"

    # ---- 6) 结转前后：净利润/资产总计/期末现金逐值不变 ----
    before = (inc["totals"]["netProfit"], bs["totals"]["assets"],
              flow["closingCash"], flow["netIncrease"])
    closing = d.closing.transfer(PERIOD, submitted_by="cfo@example")
    assert closing.get("noop") is not True, closing
    _confirm_all(d)

    inc2 = d.cn_statements.income_statement(PERIOD)
    bs2 = d.cn_statements.balance_sheet(PERIOD)
    flow2 = d.cn_statements.cash_flow(PERIOD)
    after = (inc2["totals"]["netProfit"], bs2["totals"]["assets"],
             flow2["closingCash"], flow2["netIncrease"])
    assert after == before, f"结转改变了不变量：{before} → {after}"

    # ---- 7) 结转后仍出得来完整报表 ----
    assert bs2["balanced"] is True
    assert _line(bs2["equity"], "__unclosed__")["amount"] == "0.00"
    assert _line(inc2["lines"], "revenue")["amount"] == "2000.00"   # 不因结转归零
    assert inc2["cumulative"]["totals"]["netProfit"] == "1000.00"
