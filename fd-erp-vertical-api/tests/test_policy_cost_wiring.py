"""口径册与成本计价的装配（change: add-valuation-policy-book 任务 4.1）。

判据：
1. `drivers.policy` 可读且 12 个口径键齐备、默认值都标 default；
2. `drivers.cost` 可用，结账报告带口径戳且与册内当前口径一致（不是各写一份默认值）；
3. 改口径（走确认车道）后，结账报告的口径戳跟着变——证明两者确实是同一条链。
"""
from __future__ import annotations

from fd_erp_vertical_templates.policy import KEYS


def test_drivers_expose_policy_and_cost(client_faces):
    d = client_faces.app.state.drivers
    now = d.policy.current()
    assert set(now["keys"]) == set(KEYS)
    assert now["counts"]["default"] == len(KEYS)
    assert now["values"]["cost_method"] == "weighted-average"
    assert now["keys"]["cost_method"]["enforced"] is True
    # 折旧引擎随 add-fixed-assets-lane 交付 → 该口径已真生效（不再是「仅登记」）
    assert now["keys"]["depreciation_method"]["enforced"] is True


def test_close_report_uses_the_same_policy_book(client_faces):
    d = client_faces.app.state.drivers
    before = d.cost.close()
    assert before["policy"]["cost_method"] == d.policy.current()["values"]["cost_method"]

    prop = d.policy.propose({"cost_method": "fifo"})
    d.policy.confirm(prop["requestId"], decided_by="owner")
    after = d.cost.close()
    assert after["policy"]["cost_method"] == "fifo"
    assert "fifo" in after["caliber_note"]
