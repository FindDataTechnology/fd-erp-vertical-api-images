"""发票台账端点（change: add-cn-invoice-endpoints）。

判据（对应 spec `erp-cn-localization` 新增要求）：

1. **三档由台账决定**：已认证专票→可抵、未认证专票→待认证、普票→不可抵；
2. **登记校验票面**：税额与「金额 × 税率」不符即拒（票面与台账不符最难查）；
3. **认证不许系统自证**：`by=system/agent` 被拒，且台账未变；
4. **读写分档**：只读账号登记被拒且确实没落台账；
5. **账上进项 vs 台账可抵的差额被点出**。
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
FI_RO = {"supply-chain.fi": ["finance.read"]}
TENANT_HEADER = {"X-ERP-Tenant": "tnt-demo"}
STAFF_HEADER = {"X-ERP-Staff": "cfo@example"}
BASE = "/erp/supply-chain/v1/fi"


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
def rw(reset_db, tmp_path, dsn):
    return _app(tmp_path, dsn, FI_RW)


@pytest.fixture()
def ro(reset_db, tmp_path, dsn):
    return _app(tmp_path, dsn, FI_RO)


def _register(client, **body):
    return client.post(f"{BASE}/invoices", headers={**TENANT_HEADER, **STAFF_HEADER},
                       json=body)


# ---- 1) 三档由台账决定 ----

def test_the_three_tiers_come_from_the_register(rw):
    assert _register(rw, direction="purchase", kind="vat-special", number="A-1",
                     net="1000", rate="13", issuedOn="2026-11-05").status_code == 200
    assert rw.post(f"{BASE}/invoices/A-1/certify",
                   headers={**TENANT_HEADER, **STAFF_HEADER},
                   json={"certifiedOn": "2026-11-06"}).status_code == 200
    _register(rw, direction="purchase", kind="vat-special", number="A-2",
              net="2000", rate="13", issuedOn="2026-11-06")
    _register(rw, direction="purchase", kind="vat-general", number="A-3",
              net="500", rate="13", issuedOn="2026-11-07")

    got = rw.get(f"{BASE}/invoices/deduction?period=2026-11",
                 headers={**TENANT_HEADER, **STAFF_HEADER})
    assert got.status_code == 200
    tiers = got.json()["data"]["tiers"]
    assert Decimal(tiers["deductible"]["tax"]) == Decimal("130.00")
    assert Decimal(tiers["pendingCertification"]["tax"]) == Decimal("260.00")
    assert Decimal(tiers["nonDeductible"]["tax"]) == Decimal("65.00")


def test_the_list_and_one_invoice_are_readable(rw):
    _register(rw, direction="sale", kind="vat-special", number="S-1",
              net="10000", rate="13", issuedOn="2026-11-05")
    listed = rw.get(f"{BASE}/invoices?direction=sale",
                    headers={**TENANT_HEADER, **STAFF_HEADER})
    assert listed.status_code == 200
    numbers = [r["number"] for r in listed.json()["data"]]
    assert numbers == ["S-1"]
    one = rw.get(f"{BASE}/invoices/S-1", headers={**TENANT_HEADER, **STAFF_HEADER})
    assert one.status_code == 200
    assert one.json()["data"]["number"] == "S-1"
    missing = rw.get(f"{BASE}/invoices/NOPE", headers={**TENANT_HEADER, **STAFF_HEADER})
    assert missing.status_code == 404


# ---- 2) 登记校验票面 ----

def test_a_tax_that_does_not_match_the_rate_is_refused(rw):
    res = _register(rw, direction="purchase", kind="vat-special", number="B-1",
                    net="1000", rate="13", tax="100", issuedOn="2026-11-05")
    assert res.status_code == 400, res.json()
    listed = rw.get(f"{BASE}/invoices", headers={**TENANT_HEADER, **STAFF_HEADER})
    assert "B-1" not in [r["number"] for r in listed.json()["data"]]


def test_an_unknown_invoice_kind_is_refused(rw):
    res = _register(rw, direction="purchase", kind="no-such-kind", number="B-2",
                    net="1000", rate="13", issuedOn="2026-11-05")
    assert res.status_code == 400


# ---- 3) 认证不许系统自证 ----

def test_certification_by_the_system_is_refused(rw):
    _register(rw, direction="purchase", kind="vat-special", number="C-1",
              net="1000", rate="13", issuedOn="2026-11-05")
    # 身份头由请求方给，这里模拟被误配成系统账号的情形
    res = rw.post(f"{BASE}/invoices/C-1/certify",
                  headers={**TENANT_HEADER, "X-ERP-Staff": "system"}, json={})
    assert res.status_code == 400, res.json()
    tiers = rw.get(f"{BASE}/invoices/deduction?period=2026-11",
                   headers={**TENANT_HEADER, **STAFF_HEADER}).json()["data"]["tiers"]
    assert Decimal(tiers["pendingCertification"]["tax"]) == Decimal("130.00")
    assert Decimal(tiers["deductible"]["tax"]) == Decimal("0.00")


def test_certification_is_idempotent_and_says_so(rw):
    _register(rw, direction="purchase", kind="vat-special", number="C-2",
              net="1000", rate="13", issuedOn="2026-11-05")
    first = rw.post(f"{BASE}/invoices/C-2/certify",
                    headers={**TENANT_HEADER, **STAFF_HEADER}, json={})
    assert first.status_code == 200 and first.json()["data"]["duplicate"] is False
    again = rw.post(f"{BASE}/invoices/C-2/certify",
                    headers={**TENANT_HEADER, **STAFF_HEADER}, json={})
    assert again.status_code == 200 and again.json()["data"]["duplicate"] is True


# ---- 4) 读写分档 ----

def test_a_read_only_account_cannot_register_an_invoice(ro):
    res = _register(ro, direction="purchase", kind="vat-special", number="D-1",
                    net="1000", rate="13", issuedOn="2026-11-05")
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "GRANT_REQUIRED"
    listed = ro.get(f"{BASE}/invoices", headers={**TENANT_HEADER, **STAFF_HEADER})
    assert "D-1" not in [r["number"] for r in listed.json()["data"]]


# ---- 5) 账上进项 vs 台账可抵 ----

def test_the_gap_between_the_ledger_and_the_register_is_named(rw):
    d = rw.app.state.drivers
    prop = d.kernel.submit_proposal(
        "inv:input",
        [{"account_code": "1200", "direction": "debit", "amount": "565",
          "currency": "CNY"},
         {"account_code": "2221.01.02", "direction": "debit", "amount": "65",
          "currency": "CNY"},
         {"account_code": "1000", "direction": "credit", "amount": "630",
          "currency": "CNY"}],
        booked_on="2026-11-12")
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="cfo")
    got = rw.get(f"{BASE}/invoices/reconciliation?period=2026-11",
                 headers={**TENANT_HEADER, **STAFF_HEADER})
    assert got.status_code == 200
    data = got.json()["data"]
    assert data["ledgerInputVat"] == "65.00"
    assert data["invoiceInputVat"] == "0.00"
    assert data["difference"] == "65.00"
    assert data["consistent"] is False
    assert "未经台账" in data["warning"]
