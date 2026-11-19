"""账龄与坏账准备端点（change: add-cn-aging-endpoints）。

判据（对应 spec `erp-cn-localization` 新增要求）：

1. **账龄可查且自证在场**：按对象 × 桶，`各桶合计 − 多收/多付 == 科目净额`；
2. **预演只算不写**：`proposed=false` / `preview=true`，提案条数不变；
3. **计提只出提案**：确认门前账本事实不变；
4. **权限分档**：只读账号调写端点全被拒且无副作用；
5. **机器不得设费率**；**作废必须带理由**；
6. **未登记费率时计提被拒**（不猜默认费率）。

零替身：真库 + InProcess 内核 + 真实车道。
"""
from __future__ import annotations

import json

import pytest

#: 财务任职名单：第四道闸（按人授权）要用它，否则 fi 端点一律 403。
STAFF_ROSTER = json.dumps({"read": ["cfo@example"],
                            "write": ["cfo@example"],
                            "confirm": ["cfo@example"]})

from fd_erp_vertical_api.pagesets import load_pageset_auth

FI_RO = {"supply-chain.fi": ["finance.read"]}
FI_RW = {"supply-chain.fi": ["finance.read", "finance.write"]}
TENANT_HEADER = {"X-ERP-Tenant": "tnt-demo"}
BASE = "/erp/supply-chain/v1/fi"
PERIOD = "2026-11"
H = {**TENANT_HEADER, "X-ERP-Staff": "cfo@example"}
BANDS = [{"maxDays": 30, "rate": "0.005"}, {"maxDays": 60, "rate": "0.05"},
         {"maxDays": 90, "rate": "0.2"}, {"maxDays": None, "rate": "0.3"}]


def _app(tmp_path, dsn, grants, staff="cfo@example"):
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


def _confirm_all(d) -> int:
    pending = d.kernel.list_proposals(status="pending")["data"]
    for prop in pending:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")
    return len(pending)


def _seed_receivables(client) -> None:
    """两笔销售：C-A 含税 1130（11-10）、C-B 含税 2260（10-05）。"""
    d = client.app.state.drivers
    d.cn_tax.post_sale("AG-S1", "1130", rate="13", gross=True, customer="C-A",
                       booked_on="2026-11-10", submitted_by="cfo@example")
    d.cn_tax.post_sale("AG-S2", "2260", rate="13", gross=True, customer="C-B",
                       booked_on="2026-10-05", submitted_by="cfo@example")
    _confirm_all(d)


def _pending(d) -> int:
    return len(d.kernel.list_proposals(status="pending")["data"])


def _rows_by_customer(report):
    return {r["dimensionValue"]: r for r in report["rows"]}


# ---- 1) 账龄 ----

def test_receivable_aging_splits_by_customer_and_self_proves(rw):
    _seed_receivables(rw)
    res = rw.get(f"{BASE}/receivables/aging",
                 params={"asOf": "2026-11-30"}, headers=H)
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    # 自证：桶合计 − 多收 == 科目净额
    assert body["consistent"] is True, body.get("error")
    assert body["account"] == "1100" and body["side"] == "debit"
    assert body["totalOutstanding"] == "3390.00"      # 1130 + 2260
    assert body["accountNetBalance"] == "3390.00"

    rows = _rows_by_customer(body)
    assert set(rows) == {"C-A", "C-B"}
    # 11-10 到 11-30 = 20 天 → 0-30 桶；10-05 到 11-30 = 56 天 → 31-60 桶
    assert rows["C-A"]["buckets"][0]["amount"] == "1130.00"
    assert rows["C-A"]["buckets"][1]["amount"] == "0.00"
    assert rows["C-B"]["buckets"][0]["amount"] == "0.00"
    assert rows["C-B"]["buckets"][1]["amount"] == "2260.00"


def test_aging_buckets_are_configurable(rw):
    _seed_receivables(rw)
    res = rw.get(f"{BASE}/receivables/aging",
                 params={"asOf": "2026-11-30", "buckets": "7,30"}, headers=H)
    body = res.json()["data"]
    assert body["bucketLabels"] == ["0-7天", "8-30天", "31天以上"]
    assert body["consistent"] is True
    rows = _rows_by_customer(body)
    # C-A 20 天 → 8-30 档；C-B 56 天 → 31天以上 档
    assert rows["C-A"]["buckets"][1]["amount"] == "1130.00"
    assert rows["C-B"]["buckets"][2]["amount"] == "2260.00"


def test_payable_aging_is_the_mirror_side(rw):
    d = rw.app.state.drivers
    d.cn_tax.post_purchase("AG-P1", "1130", rate="13", gross=True,
                           booked_on="2026-11-08", submitted_by="cfo@example")
    _confirm_all(d)
    res = rw.get(f"{BASE}/payables/aging", params={"asOf": "2026-11-30"}, headers=H)
    body = res.json()["data"]
    assert body["side"] == "credit" and body["account"] == "2000"
    assert body["totalOutstanding"] == "1130.00"
    assert body["consistent"] is True


def test_aging_rejects_unsorted_buckets(rw):
    res = rw.get(f"{BASE}/receivables/aging", params={"buckets": "60,30"}, headers=H)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "INVALID_ARGUMENT"


# ---- 2) 费率登记 ----

def test_schedule_starts_unregistered_then_can_be_set(rw):
    before = rw.get(f"{BASE}/bad-debts/schedule", headers=H).json()["data"]
    assert before["registered"] is False and before["bands"] == []

    out = rw.post(f"{BASE}/bad-debts/schedule", headers=H,
                  json={"bands": BANDS}).json()["data"]
    assert out["version"] == 1 and out["registeredBy"] == "cfo@example"
    after = rw.get(f"{BASE}/bad-debts/schedule", headers=H).json()["data"]
    assert after["registered"] is True and len(after["bands"]) == 4


def test_program_identity_cannot_set_the_schedule(rw):
    """费率是会计估计，只能人工拍板——身份头写 agent 一律拒。"""
    res = rw.post(f"{BASE}/bad-debts/schedule",
                  headers={**TENANT_HEADER, "X-ERP-Staff": "agent"},
                  json={"bands": BANDS})
    assert res.status_code == 400
    assert "人工" in res.json()["error"]["message"]
    assert rw.get(f"{BASE}/bad-debts/schedule", headers=H).json()["data"]["bands"] == []


def test_schedule_rejects_unknown_fields(rw):
    res = rw.post(f"{BASE}/bad-debts/schedule", headers=H,
                  json={"bands": BANDS, "rate": "0.1"})
    assert res.status_code == 400
    assert res.json()["error"]["detail"]["allowed"] == ["bands"]


# ---- 3) 计提：预演 vs 真提交 ----

def test_preview_computes_without_writing(rw):
    _seed_receivables(rw)
    d = rw.app.state.drivers
    d.bad_debts.set_schedule(BANDS, registered_by="cfo@example")
    before = _pending(d)

    res = rw.get(f"{BASE}/reports/bad-debt-preview", params={"period": PERIOD},
                 headers=H)
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["preview"] is True and body["proposed"] is False
    # C-A 1130 在 0-30 档（0.5%）= 5.65；C-B 2260 在 31-60 档（5%）= 113
    assert body["target"] == "118.65"
    assert body["existing"] == "0.00" and body["delta"] == "118.65"
    assert body["proposal"] is None and body["stableId"] is None
    assert body["blockers"] == [] and body["canSubmit"] is True
    assert _pending(d) == before                 # 预演没有留下任何提案


def test_accrue_only_proposes_until_confirmed(rw):
    _seed_receivables(rw)
    d = rw.app.state.drivers
    d.bad_debts.set_schedule(BANDS, registered_by="cfo@example")
    entries_before = len(d.kernel.query_entries(account_code="1231")["entries"])

    res = rw.post(f"{BASE}/bad-debts/{PERIOD}/accrue", headers=H)
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["proposed"] is True and body["noop"] is False
    assert body["stableId"] == f"bad-debt:{PERIOD}"
    assert body["delta"] == "118.65"
    # 确认门前：准备科目一笔分录都没有
    assert len(d.kernel.query_entries(account_code="1231")["entries"]) == entries_before
    assert _pending(d) == 1

    _confirm_all(d)
    assert d.bad_debts.status(as_of="2026-11-30")["allowance"] == "118.65"


def test_accrue_requires_a_registered_schedule(rw):
    _seed_receivables(rw)
    res = rw.post(f"{BASE}/bad-debts/{PERIOD}/accrue", headers=H)
    assert res.status_code == 400
    assert "分档费率" in res.json()["error"]["message"]


def test_second_accrue_is_idempotent(rw):
    _seed_receivables(rw)
    d = rw.app.state.drivers
    d.bad_debts.set_schedule(BANDS, registered_by="cfo@example")
    first = rw.post(f"{BASE}/bad-debts/{PERIOD}/accrue", headers=H).json()["data"]
    again = rw.post(f"{BASE}/bad-debts/{PERIOD}/accrue", headers=H).json()["data"]
    assert again["noop"] is True and again["duplicate"] is True
    assert again["stableId"] == first["stableId"]
    assert _pending(d) == 1


# ---- 4) 作废 ----

def test_void_requires_a_reason(rw):
    _seed_receivables(rw)
    d = rw.app.state.drivers
    d.bad_debts.set_schedule(BANDS, registered_by="cfo@example")
    rw.post(f"{BASE}/bad-debts/{PERIOD}/accrue", headers=H)

    res = rw.post(f"{BASE}/bad-debts/{PERIOD}/void", headers=H, json={"reason": " "})
    assert res.status_code == 400
    assert res.json()["error"]["detail"]["required"] == "reason"

    ok = rw.post(f"{BASE}/bad-debts/{PERIOD}/void", headers=H,
                 json={"reason": "账龄口径调整后重算"}).json()["data"]
    assert ok["voided"] is True
    # 作废后重提要换稳定键（内核按 stable_id 幂等）
    again = rw.post(f"{BASE}/bad-debts/{PERIOD}/accrue", headers=H).json()["data"]
    assert again["stableId"] == f"bad-debt:{PERIOD}#2"


# ---- 5) 权限分档 ----

@pytest.mark.parametrize("path,payload", [
    (f"{BASE}/bad-debts/schedule", {"bands": BANDS}),
    (f"{BASE}/bad-debts/{PERIOD}/accrue", None),
    (f"{BASE}/bad-debts/{PERIOD}/void", {"reason": "想撤"}),
])
def test_read_only_account_cannot_write(ro, path, payload):
    res = ro.post(path, headers=H, json=payload) if payload is not None \
        else ro.post(path, headers=H)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "GRANT_REQUIRED"


def test_read_only_can_still_read_every_get(ro):
    # 费率由财务（写权限）事先登记过；只读账号只查不设
    ro.app.state.drivers.bad_debts.set_schedule(BANDS, registered_by="cfo@example")
    for path, params in [
        (f"{BASE}/receivables/aging", {"asOf": "2026-11-30"}),
        (f"{BASE}/payables/aging", {"asOf": "2026-11-30"}),
        (f"{BASE}/bad-debts/schedule", None),
        (f"{BASE}/bad-debts/status", {"asOf": "2026-11-30"}),
        (f"{BASE}/reports/bad-debt-preview", {"period": PERIOD}),
    ]:
        res = ro.get(path, params=params, headers=H)
        assert res.status_code == 200, (path, res.text)


def test_preview_refuses_when_no_schedule_is_registered(ro):
    """未登记费率时连预演也不给数——不猜默认费率，也就没有「大概多少」可报。"""
    res = ro.get(f"{BASE}/reports/bad-debt-preview", params={"period": PERIOD},
                 headers=H)
    assert res.status_code == 400
    assert "分档费率" in res.json()["error"]["message"]


def test_staff_header_is_required(ro):
    res = ro.get(f"{BASE}/receivables/aging", headers=TENANT_HEADER)
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "AUTH_REQUIRED"
