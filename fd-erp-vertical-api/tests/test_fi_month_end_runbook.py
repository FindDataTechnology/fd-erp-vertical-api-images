"""月结清单端点（change: add-cn-month-end-runbook）。

判据（对应 spec `erp-cn-localization` 新增要求）：

1. 清单只读：提案条数与期间状态不变；清单要指出「缺什么」与「下一步谁做」；
2. 准备只出提案，确认门前账本不变；
3. 结转损益在前序提案未落账时 `deferred` 并列出挡住它的提案；
4. 只读账号不得 run/close，且期间未变；
5. 无待办时关账成功；已关账期间重跑被拒；
6. 顺序与前置判断不在这层重写（只转发 `MonthEndChecklist`）。

零替身：真库 + InProcess 内核 + 真实车道。
"""
from __future__ import annotations

import json

import pytest

from fd_erp_vertical_api.pagesets import load_pageset_auth

FI_RO = {"supply-chain.fi": ["finance.read"]}
FI_RW = {"supply-chain.fi": ["finance.read", "finance.write"]}
FI_RWC = {"supply-chain.fi": ["finance.read", "finance.write", "finance.confirm"]}
TENANT_HEADER = {"X-ERP-Tenant": "tnt-demo"}
BASE = "/erp/supply-chain/v1/fi"
PERIOD = "2026-11"
H = {**TENANT_HEADER, "X-ERP-Staff": "cfo@example"}
BANDS = [{"maxDays": 30, "rate": "0.005"}, {"maxDays": 60, "rate": "0.05"},
         {"maxDays": 90, "rate": "0.2"}, {"maxDays": None, "rate": "0.3"}]
STAFF_ROSTER = json.dumps({"read": ["cfo@example"],
                           "write": ["cfo@example"],
                           "confirm": ["cfo@example"]})
ROSTER = {"read": ["cfo@example"], "write": ["cfo@example"], "confirm": ["cfo@example"]}


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


@pytest.fixture()
def ro_fi(reset_db, tmp_path, dsn):
    return _app(tmp_path, dsn, FI_RO)


def _pending(d) -> int:
    return len(d.kernel.list_proposals(status="pending")["data"])


def _item(cl, key):
    return next(i for i in cl["precheck"]["items"] if i["item"] == key)


def _confirm_all(d) -> int:
    pending = d.kernel.list_proposals(status="pending")["data"]
    for prop in pending:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")
    return len(pending)


def _seed_sale(client) -> None:
    """一笔已落账的含税销售（收入 2000 + 销项 260，挂应收）——月结要有业务可结。"""
    d = client.app.state.drivers
    d.cn_tax.post_sale("ME-S1", "2260", rate="13", gross=True, customer="C-A",
                       booked_on="2026-11-15", submitted_by="cfo@example")
    _confirm_all(d)


# ---- 1) 清单只读，且指出缺什么 ----

def test_checklist_is_read_only_and_names_blockers(rwc):
    d = rwc.app.state.drivers
    before = _pending(d)
    res = rwc.get(f"{BASE}/month-end/{PERIOD}", headers=H)
    assert res.status_code == 200, res.text
    cl = res.json()["data"]
    assert cl["period"] == PERIOD
    assert _pending(d) == before                        # 只读

    # 费率没登记 → 是 blocker，并给出处置方向
    bad = _item(cl, "bad_debt_schedule")
    assert bad["state"] == "blocker"
    assert "set_schedule" in (bad["fix"] or "")
    assert cl["precheck"]["readyToRun"] is False
    # 未关账 + 无待办
    assert cl["precheck"]["closed"] is False
    assert _item(cl, "pending_proposals")["state"] == "ok"
    # 下一步谁做
    assert cl["nextOwners"]


def test_checklist_reports_the_period_as_closed(rwc):
    rwc.post(f"{BASE}/periods/{PERIOD}/close", headers=H, json={})
    cl = rwc.get(f"{BASE}/month-end/{PERIOD}", headers=H).json()["data"]
    assert cl["precheck"]["closed"] is True
    assert _item(cl, "period_status")["state"] == "blocker"
    assert cl["precheck"]["readyToRun"] is False


# ---- 2) 准备只出提案 ----

def test_run_prepares_proposals_without_confirming(rwc):
    """一笔销售挂应收 → 减值计提出提案 → 结转被推迟（前序提案还没落账）。"""
    _seed_sale(rwc)
    d = rwc.app.state.drivers
    d.bad_debts.set_schedule(BANDS, registered_by="cfo@example")
    before = _pending(d)

    res = rwc.post(f"{BASE}/month-end/{PERIOD}/run", headers=H)
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    steps = {s["step"]: s for s in body["steps"]}
    assert set(steps) >= {"cogs", "depreciation", "bad_debt", "closing"}
    assert steps["bad_debt"]["state"] == "prepared"      # 2260 × 0.5% = 11.30
    # 结转被推迟：前序提案还没落账
    assert steps["closing"]["state"] == "deferred"
    assert steps["closing"]["blockedBy"]
    assert body["closed"] is False
    assert _pending(d) > before                         # 只出提案，不落账
    assert any("确认门" in a for a in body["nextActions"])


def test_the_real_loop_is_run_confirm_run_confirm_close(rwc):
    """真实月结是**三趟两过门**，不是一次点完：

    跑 → 出减值提案；过门；跑 → 出结转提案（基数这时才含那笔费用）；过门；
    跑并关账 → 各步幂等 noop 且期间内零待办，才关得上。

    这条也是「关账不代确认」的证明：`close` 第一次调用会带着未落账的结转提案返回
    `closed=false` 并说明还需确认哪几张。
    """
    _seed_sale(rwc)
    d = rwc.app.state.drivers
    d.bad_debts.set_schedule(BANDS, registered_by="cfo@example")

    # 第一趟：减值提案
    first = rwc.post(f"{BASE}/month-end/{PERIOD}/run", headers=H).json()["data"]
    assert {s["step"]: s["state"] for s in first["steps"]}["bad_debt"] == "prepared"
    assert _confirm_all(d) == 1

    # 第二趟由 close 带出来：结转提案这一趟才出现，且关不上
    second = rwc.post(f"{BASE}/month-end/{PERIOD}/close", headers=H).json()["data"]
    assert second["closed"] is False
    closing = {s["step"]: s for s in second["steps"]}["closing"]
    assert closing["state"] == "prepared"
    # 2000 − 11.30：结转基数**含**了刚确认的那笔减值——这正是「等前序落账再结转」的意义
    assert closing["amount"] == "1988.70"
    assert any("确认门" in a for a in second["nextActions"])
    assert rwc.get(f"{BASE}/periods/{PERIOD}", headers=H).json()["data"]["closed"] is not True

    # 过门后再关：各步幂等 noop，零待办 → 关得上
    assert _confirm_all(d) == 1
    third = rwc.post(f"{BASE}/month-end/{PERIOD}/close", headers=H).json()["data"]
    assert third["closed"] is True, third
    assert third["blocked"] is False
    assert rwc.get(f"{BASE}/periods/{PERIOD}", headers=H).json()["data"]["closed"] is True
    # 结转后未结转损益归零
    bs = rwc.get(f"{BASE}/reports/balance-sheet", params={"period": PERIOD},
                 headers=H).json()["data"]
    unclosed = next(r for r in bs["equity"] if r["key"] == "__unclosed__")
    assert unclosed["amount"] == "0.00"


# ---- 3) 权限分档 ----

def test_write_only_cannot_close(rw):
    res = rw.post(f"{BASE}/month-end/{PERIOD}/close", headers=H)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "GRANT_REQUIRED"
    status = rw.get(f"{BASE}/periods/{PERIOD}", headers=H).json()["data"]
    assert status.get("closed") is not True


def test_read_only_cannot_run(ro_fi):
    res = ro_fi.post(f"{BASE}/month-end/{PERIOD}/run", headers=H)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "GRANT_REQUIRED"


# ---- 4) 已关账期间 ----

def test_closed_period_cannot_be_rerun(rwc):
    rwc.post(f"{BASE}/periods/{PERIOD}/close", headers=H, json={})
    res = rwc.post(f"{BASE}/month-end/{PERIOD}/run", headers=H)
    assert res.status_code == 400
    assert "已关账" in res.json()["error"]["message"]
    assert res.json()["error"]["detail"]["closed"] is True
