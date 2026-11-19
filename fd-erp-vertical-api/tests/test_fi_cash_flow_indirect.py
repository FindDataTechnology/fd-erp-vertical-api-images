"""补充资料（间接法）的 REST 面（change: add-fi-indirect-and-fx-endpoints）。

判据（对应 spec `erp-cash-flow` 新增要求）：

1. `/fi/reports/cash-flow-indirect` 与本页集**直接法**互证（`directOperating` 取本准则现金表，
   不取通用引擎那版——同一个面上不能有两个经营活动净额）；
2. 起点是净利润（`totals.netProfit`，程序可读），不是从 `lines` 的中文行名里捞；
3. 只读端点：不写账、不生成提案；
4. 落不进归位表的科目让 `consistent` 为假并点名 `unclassified`。

零替身：真库 + InProcess 内核。
"""
from __future__ import annotations

import json

import pytest

from fd_erp_vertical_api.pagesets import load_pageset_auth

FI_RO = {"supply-chain.fi": ["finance.read"]}
TENANT_HEADER = {"X-ERP-Tenant": "tnt-demo"}
BASE = "/erp/supply-chain/v1/fi"
PERIOD = "2026-11"
H = {**TENANT_HEADER, "X-ERP-Staff": "cfo@example"}
STAFF_ROSTER = json.dumps({"read": ["cfo@example"],
                           "write": ["cfo@example"],
                           "confirm": ["cfo@example"]})


def _app(tmp_path, dsn):
    from fastapi.testclient import TestClient

    from fd_erp_vertical_api.app import create_app
    from fd_erp_vertical_api.cart import CartStore
    from fd_erp_vertical_api.config import Settings
    from fd_erp_vertical_api.kernel_client import InProcessKernelClient

    payload = json.dumps(FI_RO)
    settings = Settings(tenant_id="tnt-demo", database_url=dsn,
                        base_currency="CNY", data_dir=str(tmp_path / "data"),
                        pageset_auth_json=payload, finance_staff_json=STAFF_ROSTER)
    kernel = InProcessKernelClient(settings)
    app = create_app(settings, kernel,
                     pageset_auth=load_pageset_auth(inline_json=payload),
                     cart_store=CartStore())
    return TestClient(app)


@pytest.fixture()
def client(reset_db, tmp_path, dsn):
    return _app(tmp_path, dsn)


def _post(client, ref, lines, when="2026-11-10") -> None:
    """过确认门落账（真库）：现销 / 付现费用 / 计提未缴附加税都靠这个造。"""
    kernel = client.app.state.kernel
    kernel.submit_proposal(ref, lines, source_ref=ref, kind="journal",
                           lane_title=ref, submitted_by="cfo@example",
                           booked_on=when)
    for prop in kernel.list_proposals(status="pending")["data"]:
        kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")


def _cash_month(client) -> None:
    """现销 1130（净 1000 + 销项 130）+ 付现管理费用 200 + 计提未缴附加税 30。"""
    _post(client, "cf:cash-sale", [
        {"account_code": "1000", "direction": "debit", "amount": "1130"},
        {"account_code": "4000", "direction": "credit", "amount": "1000"},
        {"account_code": "2221.01.01", "direction": "credit", "amount": "130"},
    ], when="2026-11-05")
    _post(client, "cf:cash-exp", [
        {"account_code": "5200", "direction": "debit", "amount": "200"},
        {"account_code": "1000", "direction": "credit", "amount": "200"},
    ], when="2026-11-08")
    _post(client, "cf:surcharge", [
        {"account_code": "5403", "direction": "debit", "amount": "30"},
        {"account_code": "2221.12", "direction": "credit", "amount": "30"},
    ], when="2026-11-09")


def _pending(d) -> int:
    return len(d.kernel.list_proposals(status="pending")["data"])


# ---- 1) 两法互证 ----

def test_indirect_reconciles_with_the_direct_method(client):
    _cash_month(client)
    body = client.get(f"{BASE}/reports/cash-flow-indirect",
                      params={"period": PERIOD}, headers=H).json()["data"]
    assert body["period"] == PERIOD
    assert body["netProfit"] == "770.00"            # 1000 − 200 − 30
    assert body["indirectOperating"] == "930.00"    # 1130 − 200
    assert body["directOperating"] == "930.00"
    assert body["difference"] == "0.00"
    assert body["consistent"] is True
    assert body["chart"] == "cn-small-business"


def test_direct_operating_matches_the_direct_statement_on_the_same_face(client):
    """同一个面上不能有两个经营活动净额：对账基准必须是本准则的直接法。"""
    _cash_month(client)
    indirect = client.get(f"{BASE}/reports/cash-flow-indirect",
                          params={"period": PERIOD}, headers=H).json()["data"]
    direct = client.get(f"{BASE}/reports/cash-flow",
                        params={"period": PERIOD}, headers=H).json()["data"]
    assert indirect["directOperating"] == direct["categories"]["operating"]["net"]


def test_net_profit_is_program_readable(client):
    """净利润取 `totals.netProfit`，不是从 `lines` 的中文行名里捞。"""
    _cash_month(client)
    isr = client.get(f"{BASE}/reports/income-statement",
                     params={"period": PERIOD}, headers=H).json()["data"]
    assert isr["totals"]["netProfit"] == "770.00"
    net_line = next(l for l in isr["lines"] if "净利润" in str(l.get("label")))
    assert net_line["amount"] == isr["totals"]["netProfit"]


# ---- 2) 只读 ----

def test_indirect_is_read_only(client):
    _cash_month(client)
    d = client.app.state.drivers
    before = (_pending(d), len(d.kernel.query_entries()["entries"]))
    client.get(f"{BASE}/reports/cash-flow-indirect", params={"period": PERIOD}, headers=H)
    assert (_pending(d), len(d.kernel.query_entries()["entries"])) == before


def test_working_capital_rows_are_listed(client):
    _cash_month(client)
    body = client.get(f"{BASE}/reports/cash-flow-indirect",
                      params={"period": PERIOD}, headers=H).json()["data"]
    codes = {r["accountCode"] for r in body["workingCapital"]}
    assert {"1100", "2000", "2221.12"} <= codes
    tax = next(r for r in body["workingCapital"] if r["accountCode"] == "2221.12")
    assert tax["change"] == "-30.00"                # 借 − 贷：计提未缴是负债增加
    assert body["workingCapitalTotal"] == "-160.00"  # 销项 130 + 附加税 30


# ---- 3) 对不上要如实报 ----

def test_unmapped_account_is_named_and_not_smoothed_over(client):
    _cash_month(client)
    _post(client, "cf:weird", [
        {"account_code": "1000", "direction": "debit", "amount": "88"},
        {"account_code": "9999", "direction": "credit", "amount": "88"},
    ], when="2026-11-12")
    body = client.get(f"{BASE}/reports/cash-flow-indirect",
                      params={"period": PERIOD}, headers=H).json()["data"]
    assert body["consistent"] is False
    assert body["unclassified"] == ["9999"]
    assert "未归位" in body["note"]


def test_missing_period_is_400(client):
    res = client.get(f"{BASE}/reports/cash-flow-indirect", headers=H)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "INVALID_ARGUMENT"
