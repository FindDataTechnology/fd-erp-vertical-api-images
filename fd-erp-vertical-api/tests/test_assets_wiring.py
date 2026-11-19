"""固定资产车道装配（change: add-fixed-assets-lane 任务 4.1）。

判据：
1. `drivers.assets` 可用，能登记并查计划；口径两键已转 `enforced=true`；
2. 期末计提真的打到内核：pending proposal、借贷同额、科目是 5200/1602；
3. **过门后能真落账**——这是本单的核心：内核补了 1601/1602 才谈得上折旧，
   确认后 1602 贷方余额等于已提折旧（否则「计划出得来、账落不下」又回来了）。
"""
from __future__ import annotations


def test_drivers_expose_assets_and_flip_the_policy_keys(client_faces):
    d = client_faces.app.state.drivers
    assert d.assets is not None
    now = d.policy.current()
    assert now["keys"]["depreciation_method"]["enforced"] is True
    assert now["keys"]["asset_ledger_owner"]["enforced"] is True

    reg = d.assets.register("FA-M1", "注塑机", cost="12000",
                            acquired_on="2026-01-15", useful_life_months=12)
    assert reg["duplicate"] is False
    plan = d.assets.plan("FA-M1")
    assert len(plan["periods"]) == 12
    assert plan["periods"][0]["amount"] == "1000.00"
    assert plan["periods"][0]["period"] == "2026-02"


def test_depreciation_posts_to_1602_after_the_confirmation_gate(client_faces):
    d = client_faces.app.state.drivers
    d.assets.register("FA-M1", "注塑机", cost="12000",
                      acquired_on="2026-01-15", useful_life_months=12)

    out = d.assets.depreciate("2026-02")
    assert out["proposal"]["status"] == "pending"      # 只到确认门
    assert out["total"] == "1000.00"
    assert [(a["assetId"], a["amount"], a["expenseAccount"])
            for a in out["assets"]] == [("FA-M1", "1000.00", "5200")]

    pending = d.kernel.list_proposals(status="pending")["data"]
    hit = next(p for p in pending if p["stable_id"] == "depr:2026-02")
    assert hit["status"] == "pending"                  # 没跳过确认门

    # 过门：科目在内核账上真实存在才确认得动——证明迁移 0012 已随种子落地
    d.kernel.confirm_proposal(proposal_id=hit["id"], confirmed_by="owner")
    bal = d.kernel.query_balance(account_code="1602")["data"][0]
    assert bal["credit_total"].startswith("1000")
    assert bal["balance"].startswith("-1000")          # 累计折旧是贷方余额
    debits = d.kernel.query_entries(account_code="5200")["entries"]
    assert sum(float(e["amount"]) for e in debits) == 1000.0
    assert d.assets.book_value("FA-M1")["accumulated"] == "1000.00"
