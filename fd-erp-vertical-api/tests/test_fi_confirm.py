"""提案收件箱与拍板（change: add-cn-fi-confirm-gate）。

判据（对应 spec `erp-cn-localization` 新增要求）：

1. **拍板权限独立**：只有 `finance.write` 的账号确认/退回被拒，且提案仍 pending；
2. **机器不得拍板**：`X-ERP-Staff: system` 被拒；
3. **退回必须带理由**：空 reason 被拒，提案仍 pending；
4. **全链路**：登记 → 认证 → 过账 → 确认 → 提案 confirmed、账上有分录、账 vs 台账差额归零；
5. **红字票**：蓝票与红字票并存，红字为负，原票未改。
"""
from __future__ import annotations

import json
from decimal import Decimal

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
BASE = "/erp/supply-chain/v1/fi"
H = {**TENANT_HEADER, **STAFF_HEADER}


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
    client = _app(tmp_path, dsn, FI_RWC)
    client.app.state.drivers.cn_filing.register_rates(
        {"city_construction": "7", "education_surcharge": "3",
         "local_education_surcharge": "2", "income_tax": "25"},
        effective_from="2026-01", source="测试：现行税法")
    return client


@pytest.fixture()
def rw(reset_db, tmp_path, dsn):
    return _app(tmp_path, dsn, FI_RW)


def _pending(client) -> list[dict]:
    res = client.get(f"{BASE}/proposals?status=pending", headers=H)
    assert res.status_code == 200, res.json()
    return res.json()["data"]


def _register_and_post(client, number="P-1", net="1000", rate="13") -> int:
    """登记一张已认证专票并过账，返回提案 id。"""
    reg = client.post(f"{BASE}/invoices", headers=H,
                      json={"direction": "purchase", "kind": "vat-special",
                            "number": number, "net": net, "rate": rate,
                            "issuedOn": "2026-11-05"})
    assert reg.status_code == 200, reg.json()
    cert = client.post(f"{BASE}/invoices/{number}/certify", headers=H,
                       json={"certifiedOn": "2026-11-06"})
    assert cert.status_code == 200, cert.json()
    posted = client.post(f"{BASE}/invoices/{number}/post", headers=H,
                         json={"amount": "1130", "bookedOn": "2026-11-12"})
    assert posted.status_code == 200, posted.json()
    rows = _pending(client)
    assert len(rows) == 1, rows
    return int(rows[0]["id"])


# ---- 1) 拍板权限独立 ----

def test_a_write_only_account_cannot_confirm(rw):
    prop = rw.app.state.drivers.kernel.submit_proposal(
        "seed:1",
        [{"account_code": "1200", "direction": "debit", "amount": "10",
          "currency": "CNY"},
         {"account_code": "2000", "direction": "credit", "amount": "10",
          "currency": "CNY"}], booked_on="2026-11-10")
    res = rw.post(f"{BASE}/proposals/{prop['proposal_id']}/confirm", headers=H)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "GRANT_REQUIRED"
    still = rw.get(f"{BASE}/proposals?status=pending", headers=H).json()["data"]
    assert any(int(r["id"]) == int(prop["proposal_id"]) for r in still)


# ---- 2) 机器不得拍板 ----

def test_the_system_cannot_confirm_its_own_proposal(rwc):
    pid = _register_and_post(rwc)
    res = rwc.post(f"{BASE}/proposals/{pid}/confirm",
                   headers={**TENANT_HEADER, "X-ERP-Staff": "system"})
    assert res.status_code == 400, res.json()
    assert any(int(r["id"]) == pid for r in _pending(rwc))


# ---- 3) 退回必须带理由 ----

def test_a_rejection_without_a_reason_is_refused(rwc):
    pid = _register_and_post(rwc, number="P-2")
    res = rwc.post(f"{BASE}/proposals/{pid}/reject", headers=H, json={"reason": "  "})
    assert res.status_code == 400, res.json()
    assert any(int(r["id"]) == pid for r in _pending(rwc))


# ---- 4) 全链路：登记 → 认证 → 过账 → 确认 ----

def test_the_invoice_reaches_the_ledger_end_to_end(rwc):
    pid = _register_and_post(rwc, number="P-3", net="1000", rate="13")
    before = rwc.get(f"{BASE}/invoices/reconciliation?period=2026-11",
                     headers=H).json()["data"]
    assert before["ledgerInputVat"] == "0.00"
    assert Decimal(before["difference"]) == Decimal("-130.00")   # 台账有、账上无

    res = rwc.post(f"{BASE}/proposals/{pid}/confirm", headers=H)
    assert res.status_code == 200, res.json()

    assert _pending(rwc) == []
    after = rwc.get(f"{BASE}/invoices/reconciliation?period=2026-11",
                    headers=H).json()["data"]
    assert after["ledgerInputVat"] == "130.00"
    assert after["consistent"] is True, after
    confirmed = rwc.get(f"{BASE}/proposals?status=confirmed", headers=H).json()["data"]
    assert any(int(r["id"]) == pid for r in confirmed)


def test_a_rejected_proposal_leaves_no_entry(rwc):
    pid = _register_and_post(rwc, number="P-4")
    res = rwc.post(f"{BASE}/proposals/{pid}/reject", headers=H,
                   json={"reason": "票面不清，重新上传"})
    assert res.status_code == 200, res.json()
    recon = rwc.get(f"{BASE}/invoices/reconciliation?period=2026-11",
                    headers=H).json()["data"]
    assert recon["ledgerInputVat"] == "0.00"        # 退回 → 账上没进项


# ---- 5) 红字票 ----

def test_a_red_letter_coexists_with_the_blue_one(rwc):
    blue = rwc.post(f"{BASE}/invoices", headers=H,
                    json={"direction": "sale", "kind": "vat-special",
                          "number": "S-1", "net": "10000", "rate": "13",
                          "issuedOn": "2026-11-05"})
    assert blue.status_code == 200, blue.json()
    red = rwc.post(f"{BASE}/invoices/red", headers=H,
                   json={"direction": "sale", "kind": "vat-special",
                         "number": "S-1R", "net": "2000",
                         "redOf": "S-1", "reason": "客户退货",
                         "issuedOn": "2026-11-20"})
    assert red.status_code == 200, red.json()
    rows = {r["number"]: r for r in
            rwc.get(f"{BASE}/invoices?direction=sale", headers=H).json()["data"]}
    assert set(rows) == {"S-1", "S-1R"}
    assert Decimal(rows["S-1R"]["net"]) < 0
    assert Decimal(rows["S-1"]["net"]) == Decimal("10000")   # 原票未改
