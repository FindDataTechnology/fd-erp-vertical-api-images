"""准则选型装配（change: add-cn-chart-selection-wiring 任务 3.1 / 3.2）。

判据：
1. 默认（未配置）用小企业准则，`chart` 回显 `cn-small-business`；
2. `ERP_CN_CHART=accounts-large.json` 的服务出会企报表（`cn-enterprise`、complete、
   balanced），且**同一批分录**的净利润与资产总计与小企业服务逐值相等；
3. 非法准则文件名在装配阶段即拒（列出可选值），不静默退回小企业。

真库：两个服务共用同一内核与同一租户，所以「两套报表金额相等」是可验的恒等式。
"""
from __future__ import annotations

import dataclasses

import pytest

from fd_erp_vertical_api.config import Settings
from fd_erp_vertical_api.drivers import _cn_pack, build_drivers
from fd_erp_vertical_templates.errors import TemplateError

RATES = {"city_construction": "7", "education_surcharge": "3",
         "local_education_surcharge": "2", "income_tax": "25"}


def _confirm(d) -> None:
    for prop in d.kernel.list_proposals(status="pending")["data"]:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo")


def test_default_chart_is_the_small_business_standard():
    assert _cn_pack(Settings()).chart == "accounts-small.json"


def test_an_unknown_chart_is_refused_at_assembly_time():
    """配置拼错必须在装配阶段炸：静默退回小企业＝给客户出一张错准则的表。"""
    with pytest.raises(TemplateError) as ei:
        _cn_pack(Settings(cn_chart="accounts-big.json"))
    assert ei.value.code == "INVALID_ARGUMENT"
    assert ei.value.detail["allowed"] == ["accounts-small.json",
                                          "accounts-large.json"]
    assert ei.value.detail["env"] == "ERP_CN_CHART"


def test_the_configured_chart_drives_the_reports(client_faces, dsn):
    base = client_faces.app.state.settings
    kernel = client_faces.app.state.kernel
    small = build_drivers(dataclasses.replace(base, cn_chart="accounts-small.json"),
                          kernel)
    large = build_drivers(dataclasses.replace(base, cn_chart="accounts-large.json"),
                          kernel)

    # 一笔含税赊销 + 一笔含税采购：两边看同一份账
    pos = small.kernel.submit_proposal(
        "cs:cap", [{"account_code": "1000", "direction": "debit", "amount": "1000",
                    "currency": "CNY"},
                   {"account_code": "3000", "direction": "credit", "amount": "1000",
                    "currency": "CNY"}], booked_on="2026-11-01")
    small.kernel.confirm_proposal(proposal_id=pos["proposal_id"], confirmed_by="cfo")
    small.cn_tax.post_sale("CS-S1", "1130", rate="13", gross=True,
                           booked_on="2026-11-05")
    small.cn_tax.post_purchase("CS-P1", "565", rate="13", gross=True,
                               booked_on="2026-11-06")
    _confirm(small)

    s_inc = small.cn_statements.income_statement("2026-11")
    l_inc = large.cn_statements.income_statement("2026-11")
    s_bs = small.cn_statements.balance_sheet("2026-11")
    l_bs = large.cn_statements.balance_sheet("2026-11")

    assert s_inc["chart"] == "cn-small-business"
    assert l_inc["chart"] == "cn-enterprise"
    assert l_bs["chart"] == "cn-enterprise"
    assert l_bs["complete"] is True and l_bs["balanced"] is True
    assert l_inc["complete"] is True
    # 准则差异只在呈现：净利润与资产总计必须逐值相等
    assert l_inc["totals"]["netProfit"] == s_inc["totals"]["netProfit"]
    assert l_bs["totals"]["assets"] == s_bs["totals"]["assets"]
    assert l_bs["totals"]["liabilities"] == s_bs["totals"]["liabilities"]


def test_rates_and_filing_still_work_on_the_large_chart(client_faces, dsn):
    """大表下附加税车道照常工作（VAT 明细科目两套都有）。"""
    base = client_faces.app.state.settings
    d = build_drivers(dataclasses.replace(base, cn_chart="accounts-large.json"),
                      client_faces.app.state.kernel)
    pos = d.kernel.submit_proposal(
        "cs2:cap", [{"account_code": "1000", "direction": "debit", "amount": "500",
                     "currency": "CNY"},
                    {"account_code": "3000", "direction": "credit", "amount": "500",
                     "currency": "CNY"}], booked_on="2026-11-01")
    d.kernel.confirm_proposal(proposal_id=pos["proposal_id"], confirmed_by="cfo")
    d.cn_tax.post_sale("CS2-S1", "113", rate="13", gross=True,
                       booked_on="2026-11-10")
    _confirm(d)
    d.cn_filing.register_rates(RATES, effective_from="2026-01", source="税法")
    out = d.cn_filing.surcharges("2026-11")
    assert out["base"] == "13.00" and out["total"] == "1.56"   # 13 × (7+3+2)%
    assert d.cn_statements.balance_sheet("2026-11")["chart"] == "cn-enterprise"
