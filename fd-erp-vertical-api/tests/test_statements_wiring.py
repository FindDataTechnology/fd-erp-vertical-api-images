"""三表装配（change: add-financial-statements 任务 2.1）。

判据：用月度闭环那批真分录查三表——资产负债表 `balanced=true`，且三表数字与账本余额咬合。
这是「报表能不能当账用」的判据：报表若自成一派（自己算一遍余额），这一条就会红。
"""
from __future__ import annotations

from decimal import Decimal

from .conftest import TENANT


def _approve_arrival_rule(settings) -> None:
    from fd_erp_ledger_mcp import posting
    from fd_erp_ledger_mcp.db import connection
    with connection(settings) as conn:
        drafted = posting.submit_posting_rule(
            conn, tenant_id=TENANT, event_type="goods_received",
            rule={"lines": [
                {"account_code": "1200", "direction": "debit",
                 "amount_from": "payload.total_cost"},
                {"account_code": "2000", "direction": "credit",
                 "amount_from": "payload.total_cost"}]},
            actor="cfg-eng")
        posting.approve_posting_rule(conn, tenant_id=TENANT,
                                     event_type="goods_received",
                                     version=drafted["version"], approved_by="cfo")


def _seed_one_month(d) -> None:
    """到货 1000 → 出库 400 结转 → 费用 1000（带维度）→ 折旧 1000。"""
    settings = d.kernel._s if hasattr(d.kernel, "_s") else None
    rc = d.inventory.receive(warehouse="WH-A", lines=[
        {"sku": "SKU-001", "qty": "10", "unit": "个", "cost": "100"}])
    for line in d.inventory.document(rc["ref"])["lines"]:
        d.kernel.decide_event(line["stable_id"], "accepted", note="ok")
    d.inventory.project(rc["ref"])

    io = d.inventory.issue(warehouse="WH-A", lines=[
        {"sku": "SKU-001", "qty": "4", "unit": "个"}])
    for line in d.inventory.document(io["ref"])["lines"]:
        d.kernel.decide_event(line["stable_id"], "accepted", note="ok")
    d.inventory.project(io["ref"])

    cogs = d.cost.post_cogs("2026-11")
    d.kernel.confirm_proposal(proposal_id=cogs["proposal"]["proposal_id"],
                              confirmed_by="cfo")

    exp = d.kernel.submit_proposal("exp:stmt-1", [
        {"account_code": "5200", "direction": "debit", "amount": "1000",
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": "1000",
         "currency": "CNY"}], dimensions={"cost_center": "CC-1"})
    d.kernel.confirm_proposal(proposal_id=exp["proposal_id"], confirmed_by="cfo")

    d.assets.register("FA-01", "包装机", cost="12000", acquired_on="2026-01-15",
                      useful_life_months=12, expense_account="5200")
    dep = d.assets.depreciate("2026-11")
    d.kernel.confirm_proposal(proposal_id=dep["proposal"]["proposal_id"],
                              confirmed_by="cfo")
    return settings


def test_statements_balance_on_real_entries(client_faces):
    d = client_faces.app.state.drivers
    _approve_arrival_rule(client_faces.app.state.settings)
    _seed_one_month(d)

    tb = d.statements.trial_balance("2026-11")
    assert tb["balanced"] is True
    assert tb["totals"]["debit"] == tb["totals"]["credit"]

    inc = d.statements.income_statement("2026-11")
    assert inc["costOfSales"] == "400.00"
    assert inc["otherOpex"] == "2000.00"      # 费用 1000 + 折旧 1000
    assert inc["operatingProfit"] == "-2400.00"

    bs = d.statements.balance_sheet("2026-11")
    assert bs["balanced"] is True, bs.get("error")
    # 与账本余额咬合：1200 = 600、1602 = −1000、5200 = 2000
    balances = {r["account_code"]: r["balance"]
                for r in d.kernel.query_balance()["data"]}
    assert balances["1200"] == "600.000000"
    assert balances["1602"] == "-1000.000000"
    assert Decimal(bs["assets"]["total"]) == Decimal("600") + Decimal("-1000") \
        + Decimal(balances["1000"])
    assert bs["unclosedProfit"] == inc["operatingProfit"]

    dim = d.statements.expenses_by_dimension("2026-11", "cost_center")
    rows = {r["dimensionValue"]: r["amount"] for r in dim["rows"]}
    assert rows["CC-1"] == "1000.00"
    # 未标注维度的费用有两笔：成本结转 400（5000）与折旧 1000（5200）
    assert rows["unattributed"] == "1400.00"
    assert dim["total"] == "2400.00"


def test_cash_flow_self_proves_on_real_entries(client_faces):
    """四表装配：真库落费用付现 / 销售收现 / 购置固定资产，三类净额之和 == 期末 − 期初。"""
    d = client_faces.app.state.drivers
    _approve_arrival_rule(client_faces.app.state.settings)
    _seed_one_month(d)          # 费用 1000 付现；到货/结转/折旧都不过现金

    for stable_id, lines in (
        ("cf:sale", [{"account_code": "1000", "direction": "debit", "amount": "3000",
                      "currency": "CNY"},
                     {"account_code": "4000", "direction": "credit", "amount": "3000",
                      "currency": "CNY"}]),
        ("cf:fa", [{"account_code": "1601", "direction": "debit", "amount": "500",
                    "currency": "CNY"},
                   {"account_code": "1000", "direction": "credit", "amount": "500",
                    "currency": "CNY"}]),
    ):
        prop = d.kernel.submit_proposal(stable_id, lines, booked_on="2026-11-10")
        d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")

    cf = d.statements.cash_flow("2026-11")
    assert cf["consistent"] is True, cf.get("error")
    assert Decimal(cf["categorizedNet"]) == Decimal(cf["netIncrease"])
    assert cf["categories"]["operating"]["net"] == "2000.00"    # 收现 3000 − 费用 1000
    assert cf["categories"]["investing"]["net"] == "-500.00"
    assert cf["categories"]["financing"]["lines"] == []
    assert cf["openingCash"] == "0.00"
    assert cf["closingCash"] == cf["netIncrease"] == "1500.00"
    assert cf["internal"] == [] and "unclassified" not in cf


def test_indirect_method_reconciles_with_direct_on_real_entries(client_faces):
    """补充资料：真库落现销 / 赊销 / 付现费用 / 折旧，间接法必须与直接法相等。"""
    d = client_faces.app.state.drivers
    _approve_arrival_rule(client_faces.app.state.settings)
    _seed_one_month(d)          # 费用 1000 付现 + 折旧 1000 + 到货/结转（非现金）

    for stable_id, lines in (
        ("cf:ind-sale", [{"account_code": "1000", "direction": "debit", "amount": "3000",
                          "currency": "CNY"},
                         {"account_code": "4000", "direction": "credit", "amount": "3000",
                          "currency": "CNY"}]),
        ("cf:ind-credit", [{"account_code": "1100", "direction": "debit", "amount": "2000",
                            "currency": "CNY"},
                           {"account_code": "4000", "direction": "credit", "amount": "2000",
                            "currency": "CNY"}]),
    ):
        prop = d.kernel.submit_proposal(stable_id, lines, booked_on="2026-11-10")
        d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")

    ind = d.statements.cash_flow_indirect("2026-11")
    assert ind["consistent"] is True, ind.get("error")
    assert ind["indirectOperating"] == ind["directOperating"]
    assert Decimal(ind["difference"]) == 0
    # 净利润 5000 − 成本 400 − 费用 1000 − 折旧 1000 = 2600
    assert ind["netProfit"] == "2600.00"
    assert {r["line"]: r["amount"] for r in ind["adjustments"]}["固定资产折旧"] == "1000.00"
    assert {r["accountCode"]: r["change"]
            for r in ind["workingCapital"]}["1100"] == "2000.00"
    # 营运资本：应收 +2000、存货 +600（到货 1000 − 结转 400）、应付 −1000 → 占用 1600
    assert {r["accountCode"]: r["change"]
            for r in ind["workingCapital"]}["1200"] == "600.00"
    assert ind["workingCapitalTotal"] == "1600.00"
    assert ind["indirectOperating"] == "2000.00"    # 2600 + 1000 − 1600
