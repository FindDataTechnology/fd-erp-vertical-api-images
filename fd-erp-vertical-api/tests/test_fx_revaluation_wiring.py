"""期末调汇装配（change: add-fx-revaluation 任务 3.1）。

判据：真库落 USD 应收（原币 + 本位币 + 汇率出处）→ 登记期末汇率 → 调汇出待确认提案 →
过门落账 → ①利润表单列汇兑损益 200、②资产负债表仍平、③重跑差额为零（不重复调汇）。
"""
from __future__ import annotations

from decimal import Decimal

from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.fx import seed_fx_facts


def _seed_rate(settings, *, rate: str, as_of: str = "2026-11-05",
               source: str = "pboc-initial") -> None:
    """内核要求分录汇率在 fx_facts 有登记（无出处不入账），所以先种汇率事实。"""
    with connection(settings) as conn:
        seed_fx_facts(conn, [{"quote_currency": "USD", "rate": rate,
                              "as_of": as_of, "source": source}],
                      settings=settings)

USD_RECEIVABLE = [
    {"account_code": "1100", "direction": "debit", "amount": "1000",
     "currency": "USD", "fx_rate": "7.0", "fx_as_of": "2026-11-05",
     "fx_source": "pboc-initial"},
    {"account_code": "4000", "direction": "credit", "amount": "7000",
     "currency": "CNY"},
]


def test_revaluation_posts_fx_loss_and_is_not_repeated(client_faces):
    d = client_faces.app.state.drivers
    _seed_rate(client_faces.app.state.settings, rate="7.0")
    prop = d.kernel.submit_proposal("fx:sale", USD_RECEIVABLE, booked_on="2026-11-05")
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")

    d.fx_revaluation.register_rates("2026-11", {"USD": "7.2"}, source="pboc-eom")
    pre = d.fx_revaluation.preview("2026-11")
    assert pre["missingRates"] == []
    assert {a["accountCode"]: a["difference"] for a in pre["adjustments"]} == {
        "1100": "200.00"}

    out = d.fx_revaluation.revalue("2026-11")
    assert out["stableId"] == "fx-reval:2026-11"
    d.kernel.confirm_proposal(proposal_id=out["proposal"]["proposal_id"],
                              confirmed_by="cfo")

    inc = d.statements.income_statement("2026-11")
    assert inc["fxGainLoss"] == "-200.00"     # 收益（应收升值）记贷方
    bs = d.statements.balance_sheet("2026-11")
    assert bs["balanced"] is True, bs.get("error")

    # 调汇业务日期是期间末日（可能是未来日期），所以按期间取余额，不用「截至此刻」的
    # query_balance——那会把期末分录挡在外面，看起来像没调。
    balances = {r["accountCode"]: r["closing"]
                for r in d.books.account_balances("2026-11")["rows"]}
    assert Decimal(balances["1100"]) == Decimal("7200")
    assert Decimal(balances["6603"]) == Decimal("-200")

    # ③ 重跑差额为零：不重复调汇
    again = d.fx_revaluation.revalue("2026-11")
    assert again["noop"] is True and again["proposal"] is None
    assert all(a["difference"] == "0.00" for a in again["adjustments"])


def test_missing_rate_blocks_revaluation(client_faces):
    d = client_faces.app.state.drivers
    _seed_rate(client_faces.app.state.settings, rate="7.0")
    prop = d.kernel.submit_proposal("fx:sale", USD_RECEIVABLE, booked_on="2026-11-05")
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")

    d.fx_revaluation.register_rates("2026-11", {"EUR": "7.9"}, source="pboc-eom")
    try:
        d.fx_revaluation.revalue("2026-11")
    except Exception as exc:                      # noqa: BLE001
        assert "USD" in str(exc)
    else:                                         # pragma: no cover
        raise AssertionError("缺汇率必须拒绝")
