"""合并报表装配（change: add-consolidation-engine 任务 3.1）。

判据：真库落两个主体（`entity` 维度）+ 内部往来 → 工作底稿按主体分列并抵销 →
抵销落成一笔待确认分录 → 确认后底稿把该分录归入抵销列、合并结果仍平。
"""
from __future__ import annotations

from decimal import Decimal

import pytest


def _post(d, stable_id: str, lines: list[dict], entity: str | None = None,
          source_ref: str | None = None, booked_on: str | None = None) -> dict:
    prop = d.kernel.submit_proposal(
        stable_id, lines, source_ref=source_ref,
        dimensions={"entity": entity} if entity else None, booked_on=booked_on)
    return d.kernel.confirm_proposal(proposal_id=prop["proposal_id"],
                                     confirmed_by="cfo")


def _sale(d, stable_id: str, amount: str, entity: str, *, ar: str = "1100") -> None:
    _post(d, stable_id, [
        {"account_code": ar, "direction": "debit", "amount": amount,
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": amount,
         "currency": "CNY"}], entity=entity, booked_on="2026-11-10")


def _purchase(d, stable_id: str, amount: str, entity: str, *, ap: str = "2000") -> None:
    _post(d, stable_id, [
        {"account_code": "1200", "direction": "debit", "amount": amount,
         "currency": "CNY"},
        {"account_code": ap, "direction": "credit", "amount": amount,
         "currency": "CNY"}], entity=entity, booked_on="2026-11-10")


def test_worksheet_eliminates_and_stays_balanced(client_faces):
    d = client_faces.app.state.drivers
    d.consolidation.register_entity("E-A", "甲公司", ownership="1.0",
                                   registered_by="cfo")
    d.consolidation.register_entity("E-B", "乙公司", ownership="0.8",
                                   registered_by="cfo")

    # A 对集团外销售 1000（应收），B 向 A 采购 800（应付 + 存货）——内部往来两侧不等
    _sale(d, "grp:a-sale", "1000", "E-A")
    _purchase(d, "grp:b-buy", "800", "E-B")

    out = d.consolidation.report("2026-11")
    cols = {c["entity"]: c for c in out["entities"]}
    assert set(cols) == {"E-A", "E-B"}
    assert cols["E-A"]["assets"] == "1000.00"
    assert cols["E-B"]["assets"] == "800.00"
    # 抵销取较小者 800，差额 200 单列
    assert out["elimination"]["computed"]["assets"] == "800.00"
    assert out["elimination"]["unmatched"][0]["amount"] == "200.00"
    assert out["balanced"] is True, out.get("error")
    assert Decimal(out["combined"]["assets"]) == Decimal("1000")   # 1000+800−800

    # 落抵销：待确认 → 确认 → 底稿把该分录归入抵销列
    elim = d.consolidation.propose_eliminations("2026-11")
    assert elim["amount"] == "800.00"
    assert elim["proposal"]["booked_on"] == "2026-11-30"
    d.kernel.confirm_proposal(proposal_id=elim["proposal"]["proposal_id"],
                              confirmed_by="cfo")

    after = d.consolidation.report("2026-11")
    assert after["elimination"]["posted"]["assets"] == "-800.00"
    # 已落账抵销后不再重复抵销同一笔：两侧余额被抵销分录压平，计算列必须归零
    # （不归零就会出现「第二次合并又抵一次」，资产越并越小而报表照样平）。
    assert after["elimination"]["computed"]["assets"] == "0.00"
    # 合并资产回到「抵销一次」的正确值：1000 应收（差额 200 仍在 unmatched，不抹平）
    assert Decimal(after["combined"]["assets"]) == Decimal("1000")
    assert after["balanced"] is True, after.get("error")


def test_foreign_currency_entity_is_not_wired(client_faces):
    d = client_faces.app.state.drivers
    d.consolidation.register_entity("E-U", "海外子公司",
                                    functional_currency="USD",
                                    registered_by="cfo")
    # 空主体不拒：没有分录就没有可折算的东西，折算口径无从谈起。
    assert d.consolidation.report("2026-11")["balanced"] is True
    _sale(d, "grp:u-sale", "1000", "E-U")
    with pytest.raises(Exception) as ei:
        d.consolidation.report("2026-11")
    assert getattr(ei.value, "code", None) == "NOT_WIRED"
    assert "fx_translation" in ei.value.detail["fix"]
