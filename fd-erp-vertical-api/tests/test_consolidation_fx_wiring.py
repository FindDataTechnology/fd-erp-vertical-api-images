"""合并 × 外币折算装配（change: add-fx-translation 任务 4.2）。

判据：真库里一个 USD 主体 + 一个 CNY 主体，汇率人工登记后，
①口径 none 时合并拒（不按 1:1 混入）；②置为 closing-rate 后折算并入、合并仍平；
③折算差额进权益、不落任何分录；④内部往来按折算后金额抵销。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.fx import seed_fx_facts
from fd_erp_vertical_templates.errors import TemplateError


def _seed_fx_fact(settings, *, rate: str = "7.0", as_of: str = "2026-11-10",
                  source: str = "银行回单") -> None:
    """外币分录的汇率必须在 `fx_facts` 有登记（无出处不入账），先种汇率事实。"""
    with connection(settings) as conn:
        seed_fx_facts(conn, [{"quote_currency": "USD", "rate": rate,
                              "as_of": as_of, "source": source}],
                      settings=settings)


def _post(d, stable_id: str, lines: list[dict], entity: str,
          booked_on: str = "2026-11-10") -> None:
    """外币行必须带可溯汇率（内核闸）——交易日汇率记在行上，`amount_base` 由内核折算。"""
    filled = []
    for line in lines:
        cur = str(line.get("currency") or "CNY")
        if cur == "CNY":
            filled.append(line)
            continue
        filled.append({**line, "fx_rate": "7.00", "fx_as_of": booked_on,
                       "fx_source": "银行回单"})
    prop = d.kernel.submit_proposal(stable_id, filled,
                                    dimensions={"entity": entity},
                                    booked_on=booked_on)
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")


def _seed(d) -> None:
    """SUB（USD）：现金 900 + 应收 100 = 预收 400 + 实收 300 + 本期利润 300（500−200）。"""
    _post(d, "fx:sub", [
        {"account_code": "1000", "direction": "debit", "amount": "900",
         "currency": "USD"},
        {"account_code": "1100", "direction": "debit", "amount": "100",
         "currency": "USD"},
        {"account_code": "5200", "direction": "debit", "amount": "200",
         "currency": "USD"},
        {"account_code": "2100", "direction": "credit", "amount": "400",
         "currency": "USD"},
        {"account_code": "3000", "direction": "credit", "amount": "300",
         "currency": "USD"},
        {"account_code": "4000", "direction": "credit", "amount": "500",
         "currency": "USD"}], entity="SUB")
    # PARENT（CNY）：现金 1000 = 内部应付 700 + 实收 300
    _post(d, "fx:par", [
        {"account_code": "1000", "direction": "debit", "amount": "1000",
         "currency": "CNY"},
        {"account_code": "2000", "direction": "credit", "amount": "700",
         "currency": "CNY"},
        {"account_code": "3000", "direction": "credit", "amount": "300",
         "currency": "CNY"}], entity="PARENT")


def test_foreign_entity_is_refused_until_the_caliber_is_set(client_faces):
    d = client_faces.app.state.drivers
    d.consolidation.register_entity("SUB", "海外子公司", functional_currency="USD",
                                    registered_by="cfo")
    d.consolidation.register_entity("PARENT", "母公司", functional_currency="CNY",
                                    registered_by="cfo")
    _seed_fx_fact(client_faces.app.state.settings)
    _seed(d)
    with pytest.raises(TemplateError) as ei:
        d.consolidation.report("2026-11")
    assert ei.value.code == "NOT_WIRED"
    assert "fx_translation" in ei.value.detail["fix"]


def test_closing_rate_caliber_translates_and_still_balances(client_faces):
    d = client_faces.app.state.drivers
    d.consolidation.register_entity("SUB", "海外子公司", functional_currency="USD",
                                    registered_by="cfo")
    d.consolidation.register_entity("PARENT", "母公司", functional_currency="CNY",
                                    registered_by="cfo")
    _seed_fx_fact(client_faces.app.state.settings)
    _seed(d)
    req = d.policy.propose({"fx_translation": "closing-rate"}, requested_by="owner")
    d.policy.confirm(req["requestId"], decided_by="owner")
    d.fx_revaluation.register_rates("2026-11", {"USD": "7.20"},
                                    source="PBOC 2026-11-30", registered_by="cfo")
    d.fx_revaluation.register_rates("2026-11", {"USD": "7.10"},
                                    source="PBOC 日均 2026-11", kind="average",
                                    registered_by="cfo")

    pending_before = len(d.kernel.list_proposals(status="pending")["data"])
    out = d.consolidation.report("2026-11")
    assert out["fxTranslation"] == "closing-rate"
    assert out["balanced"] is True, out.get("error")

    sub = next(c for c in out["entities"] if c["entity"] == "SUB")
    assert sub["assets"] == "7200.00"                  # 1000 USD × 7.20
    assert sub["equity"] == "2160.00"                  # 300 USD × 7.20（发生期）
    assert sub["translationDifference"] == "30.00"      # 权益/损益用不同汇率的差
    assert out["combined"]["translationDifference"] == "30.00"
    # 抵销按折算后金额：应收 720 vs 内部应付 700 → 抵 700，差 20 单列
    assert out["elimination"]["computed"]["assets"] == "700.00"
    assert out["elimination"]["unmatched"][0]["amount"] == "20.00"
    # 折算与合并都不落分录
    assert len(d.kernel.list_proposals(status="pending")["data"]) == pending_before
    assert (Decimal(out["combined"]["assets"])
            == Decimal(out["combined"]["liabilities"])
            + Decimal(out["combined"]["equity"])
            + Decimal(out["combined"]["translationDifference"])
            + Decimal(out["combined"]["retained"]))
