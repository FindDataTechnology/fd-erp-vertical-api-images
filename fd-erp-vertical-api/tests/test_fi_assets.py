"""固定资产端点（change: add-cn-asset-endpoints）。

判据（对应 spec `erp-cn-localization` 新增要求）：

1. 台账可查：清单 / 摘要 / 单项 / 计划 / 净值，不存在资产 404 而不返回零对象；
2. 预演只算不写：`proposed=false` / `preview=true`，提案条数不变；
3. 计提只出提案：确认门前 `1602` 无余额；
4. 同期间重复计提返回首次结果，不生成第二张提案；
5. 权限分档：只读账号调四个写端点全被拒；
6. 门槛只能人工登记；`agent` 被拒；`amount` 键缺失即拒（不许悄悄清闸）；
7. 低于门槛的资产被拒且 `treatment=expense`，台账未新增；
8. 工作量法缺产量被拒并点名资产，不用时间折旧顶替。

零替身：真库 + InProcess 内核 + 真实车道。
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

FI_RO = {"supply-chain.fi": ["finance.read"]}
FI_RW = {"supply-chain.fi": ["finance.read", "finance.write"]}
TENANT_HEADER = {"X-ERP-Tenant": "tnt-demo"}
BASE = "/erp/supply-chain/v1/fi"
PERIOD = "2026-02"
H = {**TENANT_HEADER, "X-ERP-Staff": "cfo@example"}
ASSET = {"assetId": "FA-1", "name": "数控机床", "cost": "12000",
         "acquiredOn": "2026-01-15", "usefulLifeMonths": 12, "salvage": "0",
         "expenseAccount": "5200", "category": "机器设备"}


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


def _pending(d) -> int:
    return len(d.kernel.list_proposals(status="pending")["data"])


def _register(client, **over) -> dict:
    payload = {**ASSET, **over}
    res = client.post(f"{BASE}/assets", headers=H, json=payload)
    assert res.status_code == 200, res.text
    return res.json()["data"]


def _balance(d, account: str) -> str:
    rows = d.kernel.query_balance(account_code=account) or {}
    items = rows.get("data") or rows.get("items") or []
    return items[0]["balance"] if items else "0"


# ---- 1) 台账 ----

def test_register_and_read_the_asset_book(rw):
    out = _register(rw)
    assert out["duplicate"] is False
    assert out["asset"]["assetId"] == "FA-1"
    assert out["asset"]["cost"] == "12000.00"
    # 当月增加的固定资产当月不提，次月起提（中国口径，车道里就是这个规则）
    assert out["asset"]["firstPeriod"] == "2026-02"
    assert out["asset"]["netBookValue"] == "12000.00"
    assert out["asset"]["remainingMonths"] == 12

    listed = rw.get(f"{BASE}/assets", headers=H).json()["data"]
    assert [a["assetId"] for a in listed] == ["FA-1"]

    one = rw.get(f"{BASE}/assets/FA-1", headers=H).json()["data"]
    assert one["name"] == "数控机床" and one["method"] == "straight-line"

    summary = rw.get(f"{BASE}/assets/summary", headers=H).json()["data"]
    assert summary["assetCount"] == 1
    assert summary["totalCost"] == "12000.00"
    assert summary["threshold"] is None                # 未登记门槛
    assert summary["policy"]["asset_ledger_owner"] == "platform"


def test_unknown_asset_is_404_not_a_zero_object(rw):
    res = rw.get(f"{BASE}/assets/NOPE", headers=H)
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"
    assert res.json()["error"]["detail"]["assetId"] == "NOPE"


def test_plan_lists_every_period_with_booked_flags(rw):
    _register(rw)
    rw.post(f"{BASE}/assets/depreciation/{PERIOD}", headers=H)
    plan = rw.get(f"{BASE}/assets/FA-1/plan", headers=H).json()["data"]
    assert plan["method"] == "straight-line"
    assert len(plan["periods"]) == 12
    assert plan["periods"][0]["period"] == "2026-02"
    first = next(p for p in plan["periods"] if p["period"] == PERIOD)
    assert first["booked"] is True and first["bookedAmount"] == "1000.00"
    assert plan["bookedAmount"] == "1000.00"


def test_book_value_can_be_read_as_of_a_period(rw):
    _register(rw)
    rw.post(f"{BASE}/assets/depreciation/{PERIOD}", headers=H)
    now = rw.get(f"{BASE}/assets/FA-1/book-value", headers=H).json()["data"]
    assert now["accumulated"] == "1000.00" and now["netBookValue"] == "11000.00"
    assert now["fullyDepreciated"] is False
    # asOf 回看：还没计提的期数不计入
    # asOf 回看：早于首个计提期间的期末，累计折旧仍是 0
    before = rw.get(f"{BASE}/assets/FA-1/book-value",
                    params={"asOf": "2026-01"}, headers=H).json()["data"]
    assert before["accumulated"] == "0.00" and before["netBookValue"] == "12000.00"


# ---- 2) 资本化门槛 ----

def test_threshold_blocks_low_value_assets(rw):
    rw.post(f"{BASE}/assets/threshold", headers=H, json={"amount": "5000"})
    res = rw.post(f"{BASE}/assets", headers=H, json={**ASSET, "assetId": "FA-CHEAP",
                                                    "cost": "3000"})
    assert res.status_code == 400
    err = res.json()["error"]
    assert err["detail"]["treatment"] == "expense"
    assert "门槛" in err["message"]
    # 台账未新增
    assert [a["assetId"] for a in rw.get(f"{BASE}/assets", headers=H).json()["data"]] == []


def test_threshold_must_be_given_explicitly_and_can_be_cleared(rw):
    # 少写 amount 键不许当成「取消门槛」
    res = rw.post(f"{BASE}/assets/threshold", headers=H, json={"currency": "CNY"})
    assert res.status_code == 400
    assert res.json()["error"]["detail"]["required"] == "amount"

    rw.post(f"{BASE}/assets/threshold", headers=H, json={"amount": "5000"})
    out = rw.post(f"{BASE}/assets/threshold", headers=H, json={"amount": None}).json()["data"]
    assert out["cleared"] is True and out["threshold"] is None
    # 门槛取消后低值资产可以登记
    _register(rw, assetId="FA-CHEAP", cost="3000")


def test_program_identity_cannot_set_the_threshold(rw):
    """引擎身份在 REST 层就被拒（比车道的「人工登记」更早一道）。

    车道自己的那一层判据在 `fd-erp-vertical-templates/tests/test_assets_lane.py`
    直接证；这里证的是「通过 API 也做不到」，且门槛未落下。
    """
    res = rw.post(f"{BASE}/assets/threshold",
                  headers={**TENANT_HEADER, "X-ERP-Staff": "asset-engine"},
                  json={"amount": "5000"})
    assert res.status_code == 400
    assert "人工" in res.json()["error"]["message"]
    assert rw.get(f"{BASE}/assets/summary", headers=H).json()["data"]["threshold"] is None


def test_threshold_rejects_unknown_fields(rw):
    res = rw.post(f"{BASE}/assets/threshold", headers=H,
                  json={"amount": "5000", "reason": "随便"})
    assert res.status_code == 400
    assert res.json()["error"]["detail"]["allowed"] == ["amount", "currency"]


# ---- 3) 折旧：预演 vs 真提交 ----

def test_preview_computes_without_writing(rw):
    _register(rw)
    d = rw.app.state.drivers
    before = _pending(d)
    res = rw.get(f"{BASE}/reports/depreciation-preview", params={"period": PERIOD},
                 headers=H)
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["preview"] is True and body["proposed"] is False
    assert body["total"] == "1000.00"
    assert body["proposal"] is None
    assert body["blockers"] == [] and body["canSubmit"] is True
    assert _pending(d) == before                       # 预演没有留下提案


def test_depreciate_only_proposes_until_confirmed(rw):
    _register(rw)
    d = rw.app.state.drivers
    res = rw.post(f"{BASE}/assets/depreciation/{PERIOD}", headers=H)
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["proposed"] is True and body["total"] == "1000.00"
    assert body["stableId"] == f"depr:{PERIOD}"
    assert Decimal(_balance(d, "1602")) == Decimal(0)   # 确认门前累计折旧为 0
    assert _pending(d) == 1

    prop = d.kernel.list_proposals(status="pending")["data"][0]
    d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")
    assert Decimal(_balance(d, "1602")) == Decimal("-1000")   # 贷方余额记负数


def test_second_depreciation_of_a_period_is_idempotent(rw):
    _register(rw)
    d = rw.app.state.drivers
    first = rw.post(f"{BASE}/assets/depreciation/{PERIOD}", headers=H).json()["data"]
    again = rw.post(f"{BASE}/assets/depreciation/{PERIOD}", headers=H).json()["data"]
    assert again["duplicate"] is True and again["proposed"] is False
    assert again["total"] == first["total"]
    assert _pending(d) == 1                            # 只有第一张提案


def test_preview_of_an_already_accrued_period_still_answers(rw):
    _register(rw)
    rw.post(f"{BASE}/assets/depreciation/{PERIOD}", headers=H)
    body = rw.get(f"{BASE}/reports/depreciation-preview", params={"period": PERIOD},
                  headers=H).json()["data"]
    assert body["total"] == "0.00"                     # 这期不会再提
    assert body["canSubmit"] is False
    assert any("已计提过" in b for b in body["blockers"]), body["blockers"]


def test_void_requires_a_reason(rw):
    _register(rw)
    rw.post(f"{BASE}/assets/depreciation/{PERIOD}", headers=H)
    res = rw.post(f"{BASE}/assets/depreciation/{PERIOD}/void", headers=H, json={})
    assert res.status_code == 400
    assert res.json()["error"]["detail"]["required"] == "reason"

    ok = rw.post(f"{BASE}/assets/depreciation/{PERIOD}/void", headers=H,
                 json={"reason": "资产卡片金额录错"}).json()["data"]
    assert ok["duplicate"] is False
    # 作废后重提要换稳定键
    again = rw.post(f"{BASE}/assets/depreciation/{PERIOD}", headers=H).json()["data"]
    assert again["stableId"] == f"depr:{PERIOD}#2"


def test_units_of_production_without_units_is_refused(rw):
    _register(rw, assetId="FA-U", method="units-of-production", unitsTotal="1000")
    res = rw.post(f"{BASE}/assets/depreciation/{PERIOD}", headers=H)
    assert res.status_code == 400
    err = res.json()["error"]
    assert err["detail"]["missing_units"] == ["FA-U"]
    assert "不猜产量" in err["message"]

    ok = rw.post(f"{BASE}/assets/depreciation/{PERIOD}", headers=H,
                 json={"units": {"FA-U": 100}}).json()["data"]
    assert ok["total"] == "1200.00"                    # 12000 / 1000 × 100


# ---- 4) 权限分档 ----

@pytest.mark.parametrize("path,payload", [
    (f"{BASE}/assets", ASSET),
    (f"{BASE}/assets/threshold", {"amount": "5000"}),
    (f"{BASE}/assets/depreciation/{PERIOD}", {}),
    (f"{BASE}/assets/depreciation/{PERIOD}/void", {"reason": "想撤"}),
])
def test_read_only_account_cannot_write(ro, path, payload):
    res = ro.post(path, headers=H, json=payload)
    assert res.status_code == 403, res.text
    assert res.json()["error"]["code"] == "GRANT_REQUIRED"


def test_read_only_can_read_every_get(ro):
    for path, params in [
        (f"{BASE}/assets", None),
        (f"{BASE}/assets/summary", None),
        (f"{BASE}/reports/depreciation-preview", {"period": PERIOD}),
    ]:
        res = ro.get(path, params=params, headers=H)
        assert res.status_code == 200, (path, res.text)


def test_staff_header_is_required(ro):
    res = ro.get(f"{BASE}/assets", headers=TENANT_HEADER)
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "AUTH_REQUIRED"
