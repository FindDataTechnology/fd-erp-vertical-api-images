"""财务后台按人授权（change: add-fi-staff-authorization）。

判据（对应 spec `erp-cn-localization` 新增要求）：

1. 人员闸在页面集与 grant 之后：先 `PAGESET_FORBIDDEN`、再 `GRANT_REQUIRED`、最后
   `STAFF_NOT_AUTHORIZED`——顺序换掉会泄露「这个部署有没有配名单」；
2. 名单未配置 ⇒ **一个都不许**（不是全放行）；
3. 名单已配置但调用者不在册 ⇒ 403 并点名是哪一档；
4. 单调闭包：`confirm` ⊇ `write` ⊇ `read`（CFO 只登记一档就能干三档的事，
   反过来不行）；
5. 程序身份（system/agent/各引擎）在 REST 层一律拒，**不靠名单放行**；
6. 被拒请求零副作用。
"""
from __future__ import annotations

import json

import pytest

from fd_erp_vertical_api.pagesets import FinanceStaff, load_finance_staff

TENANT_HEADER = {"X-ERP-Tenant": "tnt-demo"}
BASE = "/erp/supply-chain/v1/fi"
PERIOD = "2026-11"
ALL_FI_GRANTS = ["finance.read", "finance.write", "finance.confirm"]


def _app(tmp_path, dsn, *, pagesets=None, roster=None):
    from fastapi.testclient import TestClient

    from fd_erp_vertical_api.app import create_app
    from fd_erp_vertical_api.cart import CartStore
    from fd_erp_vertical_api.config import Settings
    from fd_erp_vertical_api.kernel_client import InProcessKernelClient
    from fd_erp_vertical_api.pagesets import load_pageset_auth

    # 不用 `grants or ALL` —— 空列表会被当成「没传」，而空列表正是要测的形态之一
    payload = json.dumps(pagesets if pagesets is not None
                         else {"supply-chain.fi": ALL_FI_GRANTS})
    settings = Settings(
        tenant_id="tnt-demo", database_url=dsn, base_currency="CNY",
        data_dir=str(tmp_path / "data"), pageset_auth_json=payload,
        finance_staff_json=None if roster is None else json.dumps(roster))
    kernel = InProcessKernelClient(settings)
    app = create_app(settings, kernel,
                     pageset_auth=load_pageset_auth(inline_json=payload),
                     cart_store=CartStore())
    return TestClient(app)


def _h(who: str) -> dict:
    return {**TENANT_HEADER, "X-ERP-Staff": who}


# ---- 0) 装载与判定的纯逻辑（不起服务）----

def test_roster_accepts_short_and_full_tier_keys():
    short = load_finance_staff(inline_json=json.dumps({"confirm": ["cfo@example"]}))
    full = load_finance_staff(inline_json=json.dumps(
        {"finance.confirm": ["cfo@example"]}))
    assert short.by_grant == full.by_grant
    assert short.configured is True


def test_roster_is_monotone_strongest_tier_covers_the_rest():
    roster = FinanceStaff({"finance.confirm": frozenset({"cfo@example"})})
    assert roster.allows("finance.read", "cfo@example")
    assert roster.allows("finance.write", "cfo@example")
    assert roster.allows("finance.confirm", "cfo@example")

    weak = FinanceStaff({"finance.read": frozenset({"clerk@example"})})
    assert weak.allows("finance.read", "clerk@example")
    assert not weak.allows("finance.write", "clerk@example")
    assert not weak.allows("finance.confirm", "clerk@example")


def test_roster_matching_is_trimmed_and_case_insensitive():
    roster = FinanceStaff({"finance.write": frozenset({"cfo@example"})})
    assert roster.allows("finance.write", "  CFO@Example  ")
    assert not roster.allows("finance.write", "")
    assert not roster.allows("finance.read", "")          # 空身份永不放行


def test_roster_garbage_and_empty_mean_nobody():
    for bad in (None, "", "{not json", "[]", json.dumps({"read": []}),
                json.dumps({"unknown": ["cfo@example"]})):
        roster = load_finance_staff(inline_json=bad)
        assert roster.configured is False, bad
        assert not roster.allows("finance.read", "cfo@example"), bad


# ---- 1) 未在名单内 ----

def test_unlisted_person_is_refused_and_nothing_changes(reset_db, tmp_path, dsn):
    client = _app(tmp_path, dsn, roster={"confirm": ["cfo@example"]})
    d = client.app.state.drivers
    before = len(d.kernel.list_proposals()["data"])

    res = client.post(f"{BASE}/bad-debts/{PERIOD}/accrue", headers=_h("intern@example"))
    assert res.status_code == 403, res.text
    err = res.json()["error"]
    assert err["code"] == "STAFF_NOT_AUTHORIZED"
    assert err["detail"]["configured"] is True
    assert err["detail"]["grant"] == "finance.write"
    assert "intern@example" in err["message"]
    assert len(d.kernel.list_proposals()["data"]) == before   # 零副作用


def test_close_period_by_unlisted_person_leaves_it_open(reset_db, tmp_path, dsn):
    client = _app(tmp_path, dsn, roster={"confirm": ["cfo@example"]})
    res = client.post(f"{BASE}/periods/{PERIOD}/close",
                      headers=_h("intern@example"), json={})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "STAFF_NOT_AUTHORIZED"
    status = client.get(f"{BASE}/periods/{PERIOD}", headers=_h("cfo@example"))
    assert status.json()["data"].get("closed") is not True


# ---- 2) 名单未配置：一个都不许 ----

def test_missing_roster_closes_the_whole_backoffice(reset_db, tmp_path, dsn):
    client = _app(tmp_path, dsn, roster=None)
    for path, method in [(f"{BASE}/reports/balance-sheet", "get"),
                         (f"{BASE}/assets", "get"),
                         (f"{BASE}/reports/bad-debt-preview", "get")]:
        res = getattr(client, method)(path, headers=_h("cfo@example"),
                                      params={"period": PERIOD})
        assert res.status_code == 403, (path, res.text)
        err = res.json()["error"]
        assert err["code"] == "STAFF_NOT_AUTHORIZED"
        assert err["detail"]["configured"] is False
        assert "ERP_FINANCE_STAFF" in err["message"]


# ---- 3) 单调闭包 ----

def test_only_confirm_can_do_all_three(reset_db, tmp_path, dsn):
    client = _app(tmp_path, dsn, roster={"confirm": ["cfo@example"]})
    h = _h("cfo@example")
    assert client.get(f"{BASE}/reports/balance-sheet", params={"period": PERIOD},
                      headers=h).status_code == 200
    assert client.post(f"{BASE}/bad-debts/schedule", headers=h,
                       json={"bands": [{"maxDays": None, "rate": "0.05"}]}
                       ).status_code == 200
    assert client.post(f"{BASE}/periods/{PERIOD}/close", headers=h,
                       json={}).status_code == 200


def test_only_read_cannot_write_or_confirm(reset_db, tmp_path, dsn):
    client = _app(tmp_path, dsn, roster={"read": ["clerk@example"]})
    h = _h("clerk@example")
    assert client.get(f"{BASE}/reports/balance-sheet", params={"period": PERIOD},
                      headers=h).status_code == 200
    for path, payload in [(f"{BASE}/bad-debts/{PERIOD}/accrue", None),
                          (f"{BASE}/periods/{PERIOD}/close", {})]:
        res = client.post(path, headers=h, json=payload)
        assert res.status_code == 403, (path, res.text)
        assert res.json()["error"]["code"] == "STAFF_NOT_AUTHORIZED"


# ---- 4) 判定顺序 ----

def test_absent_pageset_wins_over_staff_gate(reset_db, tmp_path, dsn):
    """页面集压根没授权时返回 PAGESET_FORBIDDEN——不泄露「这个部署有没有配名单」。"""
    client = _app(tmp_path, dsn, pagesets={"supply-chain.ue": ["catalog.read"]},
                  roster={"confirm": ["cfo@example"]})
    res = client.get(f"{BASE}/reports/balance-sheet", params={"period": PERIOD},
                     headers=_h("intern@example"))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "PAGESET_FORBIDDEN"


def test_empty_grant_list_wins_over_staff_gate(reset_db, tmp_path, dsn):
    """页集授权了但一档都没开：GRANT_REQUIRED 先答（先部署后人员）。"""
    client = _app(tmp_path, dsn, pagesets={"supply-chain.fi": []},
                  roster={"confirm": ["cfo@example"]})
    res = client.get(f"{BASE}/reports/balance-sheet", params={"period": PERIOD},
                     headers=_h("cfo@example"))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "GRANT_REQUIRED"


def test_grant_gate_still_wins_over_staff_gate(reset_db, tmp_path, dsn):
    """有 read 没 confirm：关账报 GRANT_REQUIRED（而不是人员的 403）。"""
    client = _app(tmp_path, dsn, pagesets={"supply-chain.fi": ["finance.read"]},
                  roster={"confirm": ["cfo@example"]})
    res = client.post(f"{BASE}/periods/{PERIOD}/close", headers=_h("cfo@example"),
                      json={})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "GRANT_REQUIRED"


# ---- 5) 程序身份不靠名单放行 ----

@pytest.mark.parametrize("who", ["system", "agent", "asset-engine", "bad-debt-engine"])
def test_program_identities_are_refused_even_if_someone_lists_them(
        reset_db, tmp_path, dsn, who):
    """即使有人把引擎名写进名单，REST 层也拒——「谁是程序」不能靠配置决定。"""
    client = _app(tmp_path, dsn, roster={"read": [who], "write": [who],
                                         "confirm": [who]})
    res = client.post(f"{BASE}/periods/{PERIOD}/close", headers=_h(who), json={})
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert "人工" in res.json()["error"]["message"]


def test_missing_staff_header_is_still_auth_required(reset_db, tmp_path, dsn):
    client = _app(tmp_path, dsn, roster={"confirm": ["cfo@example"]})
    res = client.get(f"{BASE}/reports/balance-sheet", params={"period": PERIOD},
                     headers=TENANT_HEADER)
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "AUTH_REQUIRED"
