"""预算控制车道装配（change: add-budget-control-lane 任务 3.1）。

判据：
1. `drivers.budget` 可用，三个预算口径键已转 `enforced=true`；
2. 登记 → 承诺 → 查询走通，且额度按承诺递减；
3. 实际值真的来自内核分录——先落一张真实 proposal 并确认，再看预算查询里的实际额
   （证明它是读内核、不是拿承诺顶替）。
"""
from __future__ import annotations

from decimal import Decimal


def test_drivers_expose_budget_and_flip_the_policy_keys(client_faces):
    d = client_faces.app.state.drivers
    assert d.budget is not None
    now = d.policy.current()
    for key in ("budget_dimension", "budget_period", "budget_control"):
        assert now["keys"][key]["enforced"] is True, key

    out = d.budget.set_budget("CC-1", "5200", "2026-11", "10000",
                              approved_by="owner")
    assert out["grain"] == "monthly"                    # 默认口径：cost-center + 月
    assert out["budget"]["dimension"] == "cost-center"

    c = d.budget.commit("CC-1", "5200", "3000", ref="PO-1",
                        occurred_on="2026-11-05")
    assert c["duplicate"] is False and c["remaining"] == "7000.000000"
    assert c["overBudget"] is False                     # 默认口径 warn-only、未超


def test_budget_actual_reads_the_kernel_not_the_commitments(client_faces):
    d = client_faces.app.state.drivers
    d.budget.set_budget("CC-1", "5200", "2026-11", "10000")
    d.budget.commit("CC-1", "5200", "3000", ref="PO-1", occurred_on="2026-11-05")

    before = d.budget.status("CC-1", "2026-11")
    assert before["rows"][0]["actual"] == "0.000000"    # 承诺不算实际

    # 内核里真落一笔 5200 借方（确认门之后），带本期口径维度 cost-center=CC-1
    prop = d.kernel.submit_proposal("exp:2026-11-001", [
        {"account_code": "5200", "direction": "debit", "amount": "1200",
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": "1200",
         "currency": "CNY"},
    ], dimensions={"cost_center": "CC-1"})
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="owner")

    # 另一笔同样科目但没标维度：不能算进 CC-1，必须单列
    other = d.kernel.submit_proposal("exp:2026-11-002", [
        {"account_code": "5200", "direction": "debit", "amount": "300",
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": "300",
         "currency": "CNY"},
    ])
    d.kernel.confirm_proposal(proposal_id=other["proposal_id"], confirmed_by="owner")

    after = d.budget.status("CC-1", "2026-11")
    row = after["rows"][0]
    assert row["actual"] == "1200.000000"               # 按维度汇总，来自内核分录
    assert row["unattributedActual"] == "300.000000"    # 未标注维度单列，不静默漏掉
    assert row["committed"] == "3000.000000"            # 承诺仍在，两者分列
    assert after["totals"]["actual"] == "1200.000000"
    assert "按同一维度汇总" in after["caliber_note"]


def test_budget_execution_table_on_real_ledger(client_faces):
    """整表执行：登记预算 → 承诺 → 真库落账 → 双执行率与预警。"""
    d = client_faces.app.state.drivers
    d.budget.set_budget("D-01", "5200", "2026-11", "10000", approved_by="cfo")
    d.budget.commit("D-01", "5200", "3000", ref="PO-EXEC-1",
                    occurred_on="2026-11-05")
    prop = d.kernel.submit_proposal("exec:exp", [
        {"account_code": "5200", "direction": "debit", "amount": "2500",
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": "2500",
         "currency": "CNY"}], dimensions={"cost_center": "D-01"},
        booked_on="2026-11-06")
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")

    out = d.budget.execution("2026-11")
    row = next(r for r in out["rows"]
               if (r["dimensionValue"], r["accountCode"]) == ("D-01", "5200"))
    assert row["budgetRegistered"] is True
    assert Decimal(row["committedRate"]) == Decimal("0.3")
    assert Decimal(row["actualRate"]) == Decimal("0.25")
    assert Decimal(row["remaining"]) == Decimal("7000")
    assert row["level"] == "ok" and out["alerts"] == []
    assert out["unbudgeted"] == []
