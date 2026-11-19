"""应收减值装配（change: add-bad-debt-provision 任务 3.2）。

判据：真库落应收（带客户维度）→ 人工登记费率 → 计提出待确认 proposal → 确认落账 →
报表按净额（1100 − 1231）且平衡 → 再跑计提 noop；未实现方法拒且不立项。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_vertical_api.errors import ApiError


def _post(d, stable_id: str, lines: list[dict], **kw) -> None:
    prop = d.kernel.submit_proposal(stable_id, lines, **kw)
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")


def _ar(d, stable_id: str, amount: str, customer: str, booked_on: str) -> None:
    _post(d, stable_id, [
        {"account_code": "1100", "direction": "debit", "amount": amount,
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": amount,
         "currency": "CNY"}], dimensions={"customer": customer}, booked_on=booked_on)


BANDS = [{"maxDays": 30, "rate": "0.005"}, {"maxDays": None, "rate": "0.3"}]


def test_provision_posts_increment_and_statements_show_net(client_faces):
    d = client_faces.app.state.drivers
    _ar(d, "bd:ar-1", "10000", "C-A", "2026-11-05")
    _ar(d, "bd:ar-2", "2000", "C-B", "2026-06-01")     # 5 个多月前 → 最后一档

    d.bad_debts.set_schedule(BANDS, registered_by="cfo")
    out = d.bad_debts.provision("2026-11")
    assert out["target"] == "650.00", out
    prop = out["proposal"]
    assert prop["booked_on"] == "2026-11-30"

    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")

    # 账本：6602 借 650 / 1231 贷 650
    assert next(r["balance"] for r in d.kernel.query_balance(account_code="6602")["data"]) \
        == "650.000000"
    assert next(r["balance"] for r in d.kernel.query_balance(account_code="1231")["data"]) \
        == "-650.000000"

    # 报表按净额：资产 = 应收 − 准备
    bs = d.statements.balance_sheet("2026-11")
    assert bs["balanced"] is True, bs.get("error")
    assert Decimal(bs["assets"]["total"]) == Decimal("11350")

    # 再跑：应计提 650 = 账上 650 → noop
    again = d.bad_debts.provision("2026-11")
    assert again["noop"] is True and again["delta"] == "0.00"

    st = d.bad_debts.status(as_of="2026-11-30")
    assert st["allowance"] == "650.00"
    assert st["netReceivable"] == "11350.00"


def test_unimplemented_method_is_refused_without_a_proposal(client_faces):
    d = client_faces.app.state.drivers
    _ar(d, "bd:ar-3", "1000", "C-C", "2026-11-05")
    d.bad_debts.set_schedule(BANDS, registered_by="cfo")

    req = d.policy.propose({"bad_debt_method": "direct-writeoff"}, requested_by="owner")
    d.policy.confirm(req["requestId"], decided_by="owner")
    try:
        with pytest.raises(Exception) as ei:
            d.bad_debts.provision("2026-11")
        assert getattr(ei.value, "code", None) == "NOT_WIRED"
        # 零写入：没有任何 6602 分录
        assert not d.kernel.query_balance(account_code="6602")["data"] or \
            next(r["balance"] for r in d.kernel.query_balance(account_code="6602")["data"]) \
            == "0.000000"
    finally:
        back = d.policy.propose({"bad_debt_method": "aging-rate"}, requested_by="owner")
        d.policy.confirm(back["requestId"], decided_by="owner")
