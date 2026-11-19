"""报表对比装配（change: add-report-comparison 任务 2.1）。

判据：真库两个月各落现销与费用 → 对比给出差额与变动率、本期列与报表逐行相等、
首月对比不拿 0 当基准。
"""
from __future__ import annotations

from decimal import Decimal


def _post(d, stable_id: str, lines: list[dict], booked_on: str) -> None:
    prop = d.kernel.submit_proposal(stable_id, lines, booked_on=booked_on)
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")


def _sale_and_expense(d, period: str, sale: str, exp: str) -> None:
    _post(d, f"cmp:{period}:sale", [
        {"account_code": "1000", "direction": "debit", "amount": sale,
         "currency": "CNY"},
        {"account_code": "4000", "direction": "credit", "amount": sale,
         "currency": "CNY"}], f"{period}-05")
    _post(d, f"cmp:{period}:exp", [
        {"account_code": "5200", "direction": "debit", "amount": exp,
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": exp,
         "currency": "CNY"}], f"{period}-06")


def test_comparison_matches_the_statements_and_uses_the_previous_month(client_faces):
    d = client_faces.app.state.drivers
    _sale_and_expense(d, "2026-10", "1000", "400")
    _sale_and_expense(d, "2026-11", "3000", "500")

    out = d.comparison.compare("2026-11")
    assert out["against"] == "previous" and out["baselinePeriod"] == "2026-10"
    inc = out["statements"]["incomeStatement"]
    assert inc["baselineAvailable"] is True
    rows = {r["key"]: r for r in inc["rows"]}
    assert rows["营业收入"]["change"] == "2000.00"
    assert rows["营业收入"]["ratio"] == "200.00%"

    # 本期列与直接查报表逐行相等
    report = d.statements.income_statement("2026-11")
    for line in report["lines"]:
        assert rows[line["line"]]["current"] == line["amount"]

    cash = {r["key"]: r for r in out["statements"]["cashFlow"]["rows"]}
    assert cash["operating"]["current"] == d.statements.cash_flow("2026-11")[
        "categories"]["operating"]["net"]


def test_first_month_does_not_treat_zero_as_a_baseline(client_faces):
    d = client_faces.app.state.drivers
    _sale_and_expense(d, "2026-11", "800", "100")

    out = d.comparison.compare("2026-11")
    inc = out["statements"]["incomeStatement"]
    assert inc["baselineAvailable"] is False
    assert all(r["baseline"] is None and r["ratio"] is None for r in inc["rows"])
    assert Decimal(inc["rows"][0]["current"]) > 0


def test_last_year_basis(client_faces):
    d = client_faces.app.state.drivers
    _sale_and_expense(d, "2025-11", "500", "100")
    _sale_and_expense(d, "2026-11", "900", "100")

    out = d.comparison.compare("2026-11", "last-year")
    assert out["baselinePeriod"] == "2025-11"
    rows = {r["key"]: r for r in out["statements"]["incomeStatement"]["rows"]}
    assert rows["营业收入"]["change"] == "400.00"
    assert rows["营业收入"]["ratio"] == "80.00%"


def test_ytd_accumulates_against_the_same_months_last_year(client_faces):
    d = client_faces.app.state.drivers
    _sale_and_expense(d, "2025-01", "1000", "200")
    _sale_and_expense(d, "2026-01", "2000", "400")
    _sale_and_expense(d, "2026-02", "2500", "500")

    out = d.comparison.compare("2026-02", "ytd")
    assert out["periodRange"] == ["2026-01", "2026-02"]
    assert out["baselineRange"] == ["2025-01", "2025-02"]
    rows = {r["key"]: r for r in out["statements"]["incomeStatement"]["rows"]}
    assert rows["营业收入"]["current"] == "4500.00"      # 1+2 月
    assert rows["营业收入"]["baseline"] == "1000.00"     # 去年只有 1 月有数
    assert rows["营业收入"]["change"] == "3500.00"
    assert out["statements"]["balanceSheet"]["notApplicable"] is True

    cash = {r["key"]: r for r in out["statements"]["cashFlow"]["rows"]}
    assert cash["operating"]["current"] == "3600.00"     # (2000-400)+(2500-500)
    assert out["statements"]["cashFlow"]["consistent"] is True
