"""月结三件端点（change: add-cn-month-end-endpoints）。

判据（对应 spec `erp-cn-localization` 新增要求）：

1. **关账权限独立**：只有 write 的账号关账被拒，且期间未变；
2. **机器不得关账**：`X-ERP-Staff: agent` 被拒；
3. **重开必须带理由**：空 reason 被拒、期间仍关着；
4. **关账后落账被挡**：同期间提交结转被拒（期间已关）；
5. **期初只在空账可录**：空账可登记（只出提案），有分录时被拒且账本未改；
6. **结转两段**：预演不写账；提交只出提案，确认后才入账。
"""
from __future__ import annotations

import json

import pytest

#: 财务任职名单：第四道闸（按人授权）要用它，否则 fi 端点一律 403。
STAFF_ROSTER = json.dumps({"read": ["cfo@example"],
                            "write": ["cfo@example"],
                            "confirm": ["cfo@example"]})

from fd_erp_vertical_api.pagesets import load_pageset_auth

FI_RW = {"supply-chain.fi": ["finance.read", "finance.write"]}
FI_RWC = {"supply-chain.fi": ["finance.read", "finance.write", "finance.confirm"]}
TENANT_HEADER = {"X-ERP-Tenant": "tnt-demo"}
STAFF_HEADER = {"X-ERP-Staff": "cfo@example"}
H = {**TENANT_HEADER, **STAFF_HEADER}
BASE = "/erp/supply-chain/v1/fi"
PERIOD = "2026-11"


def _app(tmp_path, dsn, grants):
    from fastapi.testclient import TestClient

    from fd_erp_vertical_api.app import create_app
    from fd_erp_vertical_api.cart import CartStore
    from fd_erp_vertical_api.config import Settings
    from fd_erp_vertical_api.kernel_client import InProcessKernelClient

    payload = json.dumps(grants)
    settings = Settings(tenant_id="tnt-demo", database_url=dsn,
                        base_currency="CNY", data_dir=str(tmp_path / "data"),
                        pageset_auth_json=payload,
                        finance_staff_json=STAFF_ROSTER)
    kernel = InProcessKernelClient(settings)
    app = create_app(settings, kernel,
                     pageset_auth=load_pageset_auth(inline_json=payload),
                     cart_store=CartStore())
    return TestClient(app)


@pytest.fixture()
def rwc(reset_db, tmp_path, dsn):
    return _app(tmp_path, dsn, FI_RWC)


@pytest.fixture()
def rw(reset_db, tmp_path, dsn):
    return _app(tmp_path, dsn, FI_RW)


def _post_sale(client) -> None:
    """一笔含税销售（收入 1000 + 销项 130），供结转与关账用例使用。"""
    d = client.app.state.drivers
    d.cn_tax.post_sale("ME-S1", "1130", rate="13", gross=True,
                       booked_on="2026-11-10", submitted_by="cfo@example")
    for prop in d.kernel.list_proposals(status="pending")["data"]:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")


# ---- 1) 关账权限独立 ----

def test_a_write_only_account_cannot_close_a_period(rw):
    res = rw.post(f"{BASE}/periods/{PERIOD}/close", headers=H, json={})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "GRANT_REQUIRED"
    status = rw.get(f"{BASE}/periods/{PERIOD}", headers=H).json()["data"]
    assert not status.get("closed"), status


# ---- 2) 机器不得关账 ----

def test_the_machine_cannot_close_a_period(rwc):
    res = rwc.post(f"{BASE}/periods/{PERIOD}/close",
                   headers={**TENANT_HEADER, "X-ERP-Staff": "agent"}, json={})
    assert res.status_code == 400, res.json()
    assert "人工" in res.json()["error"]["message"]


# ---- 3) 重开必须带理由 ----

def test_reopening_without_a_reason_is_refused(rwc):
    assert rwc.post(f"{BASE}/periods/{PERIOD}/close",
                    headers=H, json={"note": "月结"}).status_code == 200
    res = rwc.post(f"{BASE}/periods/{PERIOD}/reopen", headers=H,
                   json={"reason": "   "})
    assert res.status_code == 400
    assert rwc.get(f"{BASE}/periods/{PERIOD}", headers=H).json()["data"]["closed"]


def test_closing_then_reopening_with_a_reason_works(rwc):
    rwc.post(f"{BASE}/periods/{PERIOD}/close", headers=H, json={"note": "月结"})
    assert rwc.get(f"{BASE}/periods/{PERIOD}", headers=H).json()["data"]["closed"]
    ok = rwc.post(f"{BASE}/periods/{PERIOD}/reopen", headers=H,
                  json={"reason": "漏记一张票，需补账"})
    assert ok.status_code == 200, ok.json()
    assert not rwc.get(f"{BASE}/periods/{PERIOD}", headers=H).json()["data"]["closed"]


# ---- 4) 关账后落账被挡 ----

def test_a_closed_period_refuses_a_late_transfer(rwc):
    _post_sale(rwc)
    assert rwc.post(f"{BASE}/periods/{PERIOD}/close", headers=H,
                    json={"note": "月结"}).status_code == 200
    res = rwc.post(f"{BASE}/closing/{PERIOD}/transfer", headers=H)
    assert res.status_code == 400, res.json()
    assert "关账" in res.json()["error"]["message"]


# ---- 5) 期初只在空账可录 ----

OPENING_LINES = [{"account_code": "1000", "direction": "debit", "amount": "5000"},
                 {"account_code": "3000", "direction": "credit", "amount": "5000"}]


def test_opening_is_accepted_on_an_empty_ledger_and_only_proposes(rwc):
    d = rwc.app.state.drivers
    res = rwc.post(f"{BASE}/opening", headers=H,
                   json={"lines": OPENING_LINES, "asOf": "2026-10-31"})
    assert res.status_code == 200, res.json()
    assert len(_pending(d)) == 1                  # 只出提案，不直接写账
    d.kernel.confirm_proposal(proposal_id=_pending(d)[0]["id"],
                             confirmed_by="cfo@example")
    bs = d.cn_statements.balance_sheet("2026-10")
    assert bs["balanced"] is True
    cash = next(l for l in bs["assets"] if l["key"] == "cash")
    assert cash["amount"] == "5000.00"


def test_registering_the_opening_twice_is_idempotent_not_an_error(rwc):
    """已建账后再提交：车道按「同一 stable_id 幂等」返回 duplicate，而不是报错。

    幂等优先于空账校验——这是对的：重复点一次按钮不该失败，也不该写两遍。
    """
    rwc.post(f"{BASE}/opening", headers=H,
             json={"lines": OPENING_LINES, "asOf": "2026-10-31"})
    again = rwc.post(f"{BASE}/opening", headers=H,
                     json={"lines": OPENING_LINES, "asOf": "2026-10-31"})
    assert again.status_code == 200, again.json()
    assert again.json()["data"]["duplicate"] is True


def test_opening_is_refused_when_the_ledger_already_has_entries(rwc):
    """账上已有分录、又没有期初：事后补录会改写全部历史报表，必须拒。"""
    _post_sale(rwc)                               # 账上先有分录
    res = rwc.post(f"{BASE}/opening", headers=H,
                   json={"lines": OPENING_LINES, "asOf": "2025-12-31"})
    assert res.status_code == 400, res.json()
    assert "空账" in res.json()["error"]["message"]


def _pending(d) -> list[dict]:
    return d.kernel.list_proposals(status="pending")["data"]


# ---- 6) 结转两段 ----

def test_closing_preview_does_not_write_and_transfer_only_proposes(rwc):
    _post_sale(rwc)
    d = rwc.app.state.drivers
    before = len(_pending(d))

    preview = rwc.get(f"{BASE}/reports/closing-preview?period={PERIOD}", headers=H)
    assert preview.status_code == 200, preview.json()
    assert len(_pending(d)) == before             # 预演不写账
    assert preview.json()["data"]["netProfit"] == "1000.00"

    transfer = rwc.post(f"{BASE}/closing/{PERIOD}/transfer", headers=H)
    assert transfer.status_code == 200, transfer.json()
    rows = _pending(d)
    assert len(rows) == 1                          # 只出提案，没直接写分录

    inc_before = d.cn_statements.income_statement(PERIOD)["totals"]["netProfit"]
    assert inc_before == "1000.00"
    d.kernel.confirm_proposal(proposal_id=rows[0]["id"], confirmed_by="cfo@example")

    # 结转后利润表不归零、未结转损益归零
    inc = d.cn_statements.income_statement(PERIOD)
    bs = d.cn_statements.balance_sheet(PERIOD)
    assert inc["totals"]["netProfit"] == "1000.00"
    unclosed = next(l for l in bs["equity"] if l["key"] == "__unclosed__")
    assert unclosed["amount"] == "0.00"
