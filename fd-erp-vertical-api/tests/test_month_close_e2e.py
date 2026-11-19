"""月度闭环 e2e：四条引擎车道在同一个月里跑通并互相对账。

这一条**不是单元测试的重复**：四条车道各自有自己的用例，但「一个月到底能不能结出来」
只能靠把它们放进同一个月、同一批事实里看。它盯的是跨车道的接缝：

    到货（库存车道，带成本）
      → 过账规则 → 借 1200 存货 / 贷 2000 应付
    出库（库存车道）
      → 成本计价（成本引擎）→ 出库成本
      → 成本结转（成本引擎 post_cogs）→ 借 5000 营业成本 / 贷 1200 存货
    费用 proposal（带 cost_center 维度）→ 借 5200 / 贷 1000
      → 预算车道：承诺 vs 实际（按维度）
    资产登记 → 折旧计提 → 借 5200 / 贷 1602
    开票申请 → 过门 → 回执登记（号码唯一、金额核对）

期末四处必须自洽：1200 余额 = 到货成本 − 结转成本；5000 = 结转金额；
1602 = 已提折旧；预算的 actual 只含带该维度的分录，不带维度的单列。
"""
from __future__ import annotations

from decimal import Decimal

from .conftest import TENANT


def _approve_arrival_rule(settings) -> None:
    """到货过账规则（人工过门）：借 1200 存货 / 贷 2000 应付，金额取 payload.total_cost。

    用 total_cost 而不是 cost：`cost` 是单位成本（计价按它建层），规则要的是行金额。
    """
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


def _accept(d, ref: str) -> None:
    """把库存单据的每一笔拟稿事实过确认门（唯一改变事件状态的入口）。"""
    for line in d.inventory.document(ref)["lines"]:
        d.kernel.decide_event(line["stable_id"], "accepted", note="仓管审核通过")


def _balance(d, code: str) -> Decimal:
    rows = d.kernel.query_balance(account_code=code)["data"]
    return Decimal(rows[0]["balance"]) if rows else Decimal(0)


def test_one_month_closes_across_all_lanes(client_faces):
    d = client_faces.app.state.drivers
    settings = client_faces.app.state.settings
    _approve_arrival_rule(settings)

    # ---- 1) 到货 10 件 @100：事件 → 数量面 → 分录（借 1200 / 贷 2000）----
    rc = d.inventory.receive(warehouse="WH-A", lines=[
        {"sku": "SKU-001", "qty": "10", "unit": "个", "cost": "100",
         "currency": "CNY"}])
    _accept(d, rc["ref"])
    projected = d.inventory.project(rc["ref"])
    assert projected["ok"] is True
    assert all(item.get("posted") is not False for item in projected["lines"])
    assert _balance(d, "1200") == Decimal("1000")

    # ---- 2) 出库 4 件：事件 + 数量面 + 成本计价 ----
    io = d.inventory.issue(warehouse="WH-A", lines=[
        {"sku": "SKU-001", "qty": "4", "unit": "个"}])
    _accept(d, io["ref"])
    d.inventory.project(io["ref"])
    unit_cost = d.cost.issue_cost("SKU-001", "4", warehouse="WH-A")
    assert unit_cost["amount"] == "400.000000"

    # ---- 3) 成本结转：借 5000 / 贷 1200（待确认 → 过门）----
    cogs = d.cost.post_cogs("2026-11")
    assert cogs["total"] == "400.000000"
    assert cogs["proposal"]["status"] == "pending"
    d.kernel.confirm_proposal(proposal_id=cogs["proposal"]["proposal_id"],
                              confirmed_by="cfo")
    assert _balance(d, "5000") == Decimal("400")      # 营业成本
    assert _balance(d, "1200") == Decimal("600")      # 存货 1000 − 400

    # ---- 4) 费用 1000（带 cost_center 维度）+ 预算承诺 ----
    exp = d.kernel.submit_proposal("exp:2026-11-001", [
        {"account_code": "5200", "direction": "debit", "amount": "1000",
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": "1000",
         "currency": "CNY"},
    ], dimensions={"cost_center": "CC-1"})
    d.kernel.confirm_proposal(proposal_id=exp["proposal_id"], confirmed_by="cfo")

    d.budget.set_budget("CC-1", "5200", "2026-11", "1500", approved_by="owner")
    committed = d.budget.commit("CC-1", "5200", "1200", ref="EXP-1",
                                occurred_on="2026-11-05")
    assert committed["overBudget"] is False

    # ---- 5) 固定资产：登记 + 本月折旧（借 5200 / 贷 1602）----
    d.assets.register("FA-01", "包装机", cost="12000", acquired_on="2026-01-15",
                      useful_life_months=12, expense_account="5200")
    dep = d.assets.depreciate("2026-11")
    assert dep["total"] == "1000.00"
    d.kernel.confirm_proposal(proposal_id=dep["proposal"]["proposal_id"],
                              confirmed_by="cfo")
    assert _balance(d, "1602") == Decimal("-1000")    # 累计折旧（贷方）

    # ---- 6) 开票申请 → 过门 → 回执 ----
    req = d.invoices.submit_request("C-001", {
        "title": "示范客户一号", "taxId": "91310000MA1K3XYZ12",
        "statementId": "ST-2026-11"})
    d.kernel.decide_event(req["requestId"], "accepted", note="审核通过")
    receipt = d.invoice_receipts.record(req["requestId"], "NO-20261113",
                                        "2026-11-13", "400",
                                        expected_amount="400")
    assert receipt["match"] == "matched"

    # ---- 7) 期末对账 ----
    close = d.cost.close("2026-11")
    assert close["issues"]["cost"] == "400.000000"
    assert close["closing"][0]["qty"] == "6.000000"          # 10 − 4

    status = d.budget.status("CC-1", "2026-11")
    row = status["rows"][0]
    assert row["budget"] == "1500.000000"
    assert row["committed"] == "1200.000000"
    assert row["actual"] == "1000.000000"        # 只有带 CC-1 维度的那笔费用
    assert row["unattributedActual"] == "1000.000000"   # 折旧没标维度，单列
    assert row["remaining"] == "300.000000"

    assert d.assets.book_value("FA-01")["netBookValue"] == "11000.00"
    recons = d.invoice_receipts.reconciliation(["C-001"])
    assert recons["counts"]["issued"] == 1
    assert recons["counts"]["mismatched"] == 0

    # 借贷全局必平（分录层），且资产/成本/预算三处数字互相咬合
    entries = d.kernel.query_entries(limit=500)["entries"]
    debits = sum(Decimal(e["amount_base"]) for e in entries
                 if e["direction"] == "debit")
    credits = sum(Decimal(e["amount_base"]) for e in entries
                  if e["direction"] == "credit")
    assert debits == credits == Decimal("1000") + Decimal("400") \
        + Decimal("1000") + Decimal("1000")
    assert _balance(d, "5200") == Decimal("2000")     # 费用 1000 + 折旧 1000
