"""期间关账装配（change: add-period-close 任务 4.1）。

判据：真库落一笔 10 月业务 → 关 10 月 → 补录 10 月被拒（PERIOD_CLOSED，账本零变化）
→ 重开 → 补录成功 → 复算核对与快照一致。走的是 `drivers.periods`，不是直接调内核。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_api.errors import ApiError


def _expect_period_closed(exc_info) -> None:
    """拒落账的码必须仍然可辨（不可塌成 INTERNAL / UPSTREAM_UNAVAILABLE）。

    `drivers.periods.*` 走 KernelProxy 的收口（ApiError），而提交/确认分录是透传
    （内核 ErpError 原样上抛）——两条路径的异常类型不同，但**语义码必须相同**，
    否则调用方会把「期间关了」当成「库抖了」并白重试。
    """
    code = getattr(exc_info.value, "code", None)
    assert code == "PERIOD_CLOSED", (code, str(exc_info.value))
    assert "已关账" in str(exc_info.value)


def _post(d, stable_id: str, *, booked_on: str | None, amount: str = "100") -> dict:
    prop = d.kernel.submit_proposal(
        stable_id,
        [{"account_code": "5200", "direction": "debit", "amount": amount,
          "currency": "CNY"},
         {"account_code": "1000", "direction": "credit", "amount": amount,
          "currency": "CNY"}],
        booked_on=booked_on)
    return d.kernel.confirm_proposal(proposal_id=prop["proposal_id"],
                                     confirmed_by="cfo")


def _balance(d, code: str = "5200") -> str:
    return next(r["balance"] for r in d.kernel.query_balance(account_code=code)["data"])


def test_period_close_blocks_postings_and_report_verifies(client_faces):
    d = client_faces.app.state.drivers
    _post(d, "pc:oct-1", booked_on="2026-10-08", amount="100")

    closed = d.periods.close("2026-10", actor="cfo", note="月结")
    assert closed["closed"] is True
    assert closed["snapshot"]["lines"] == 2

    # 补录到已关期间：拒绝且账本零变化
    with pytest.raises(Exception) as ei:
        _post(d, "pc:oct-late", booked_on="2026-10-20", amount="50")
    _expect_period_closed(ei)
    assert _balance(d) == "100.000000"

    report = d.periods.report("2026-10")
    assert report["verified"] is True and report["diffs"] == []

    # 重开 → 补录成功 → 再关后复算仍然一致
    assert d.periods.reopen("2026-10", actor="cfo", reason="补录 10/20 单据")["closed"] is False
    _post(d, "pc:oct-late", booked_on="2026-10-20", amount="50")
    assert d.periods.close("2026-10", actor="cfo")["closed"] is True
    report = d.periods.report("2026-10")
    assert report["verified"] is True
    assert report["snapshot"]["debitTotal"] == "150.000000"
    assert [p["period"] for p in d.periods.list()] == ["2026-10"]


def test_reopen_requires_a_reason_and_a_closed_period(client_faces):
    d = client_faces.app.state.drivers
    _post(d, "pc:nov-1", booked_on="2026-11-03")
    with pytest.raises(ApiError) as ei:
        d.periods.reopen("2026-11", actor="cfo", reason="  ")
    assert "理由" in ei.value.message

    d.periods.close("2026-11", actor="cfo")
    with pytest.raises(ApiError) as ei:
        d.periods.reopen("2026-12", actor="cfo", reason="不存在")
    assert "未关账" in ei.value.message
