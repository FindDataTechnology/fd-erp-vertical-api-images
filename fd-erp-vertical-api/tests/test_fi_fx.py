"""期末外币调汇的 REST 面（change: add-fi-indirect-and-fx-endpoints）。

判据（对应 spec `erp-fx-revaluation` 新增要求）：

1. 应交付而此前**一个入口都没有**：登记汇率 / 看生效汇率 / 预演 / 出提案 / 状态；
2. 汇率是外部事实：`source` 必填、**程序身份不得登记**（`system`/`agent`/各引擎）；
3. 预演只算不写；出提案不落账（确认门前账本不变）；
4. 缺汇率即拒并给修法，**不回落到历史汇率**；
5. 无外币余额 noop；已有待确认提案不重复提交；过门后重跑差额为零；
6. 已关账期间拒绝；只读账号不得登记/调汇。

零替身：真库 + InProcess 内核 + 真实 `FxRevaluation` 车道；外币分录的汇率事实
经 `seed_fx_facts` 落 `fx_facts`（内核只接受有出处的汇率）。
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
STAFF_ROSTER = json.dumps({"read": ["cfo@example"],
                           "write": ["cfo@example"],
                           "confirm": ["cfo@example"]})


def _app(tmp_path, dsn, grants):
    from fastapi.testclient import TestClient

    from fd_erp_vertical_api.app import create_app
    from fd_erp_vertical_api.cart import CartStore
    from fd_erp_vertical_api.config import Settings
    from fd_erp_vertical_api.kernel_client import InProcessKernelClient

    payload = json.dumps(grants)
    settings = Settings(tenant_id="tnt-demo", database_url=dsn,
                        base_currency="CNY", data_dir=str(tmp_path / "data"),
                        pageset_auth_json=payload, finance_staff_json=STAFF_ROSTER)
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


@pytest.fixture()
def rwc(reset_db, tmp_path, dsn):
    return _app(tmp_path, dsn, FI_RWC)


# ---- 种子：一笔 USD 1000 的应收（按 7.00 折成 7000）----

def _seed_usd_receivable(client, *, base_rate="7.00", when="2026-11-05",
                         foreign="1000") -> None:
    from fd_erp_ledger_mcp.db import connection
    from fd_erp_ledger_mcp.fx import seed_fx_facts

    settings = client.app.state.settings
    kernel = client.app.state.kernel
    with connection(settings) as conn:
        seed_fx_facts(conn, [{"quote_currency": "USD", "rate": base_rate,
                              "as_of": when, "source": "ecb-exr"}],
                      settings=settings)
    kernel.submit_proposal(
        "fx-seed:ar", [
            {"account_code": "1100", "direction": "debit", "amount": foreign,
             "currency": "USD", "fx_rate": base_rate, "fx_as_of": when,
             "fx_source": "ecb-exr"},
            {"account_code": "4000", "direction": "credit", "amount": foreign,
             "currency": "USD", "fx_rate": base_rate, "fx_as_of": when,
             "fx_source": "ecb-exr"},
        ], source_ref="fx-seed:ar", kind="journal", lane_title="出口销售（USD）",
        submitted_by="cfo@example", booked_on=when)
    for prop in kernel.list_proposals(status="pending")["data"]:
        kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")


def _register(client, rate="7.20", *, source="中国银行 2026-11-30 中间价",
              period=PERIOD, staff="cfo@example", currency="USD"):
    return client.post(f"{BASE}/fx/rates",
                       json={"period": period, "rates": {currency: rate},
                             "source": source},
                       headers={**TENANT_HEADER, "X-ERP-Staff": staff})


def _pending(d) -> int:
    return len(d.kernel.list_proposals(status="pending")["data"])


def _balance(d, code: str):
    from decimal import Decimal
    rows = d.kernel.query_balance(account_code=code)["data"]
    return Decimal(rows[0]["balance"]) if rows else Decimal(0)


# ---- 1) 汇率登记 ----

def test_rates_are_empty_before_registration(rw):
    body = rw.get(f"{BASE}/fx/rates", params={"period": PERIOD}, headers=H).json()["data"]
    assert body["period"] == PERIOD and body["kind"] == "closing"
    assert body["rates"] == {} and body["revisions"] == 0


def test_register_requires_a_source(rw):
    res = _register(rw, source="")
    assert res.status_code == 400, res.text
    assert "source" in res.json()["error"]["message"]


def test_base_currency_cannot_be_registered(rw):
    res = _register(rw, currency="CNY")
    assert res.status_code == 400
    assert "本位币" in res.json()["error"]["message"]


@pytest.mark.parametrize("staff", ["system", "agent", "fx-engine", "fx-revaluation"])
def test_program_identity_cannot_register_rates(rw, staff):
    """汇率是外部事实，与坏账费率、资本化门槛同一条纪律：机器不得自证。"""
    res = _register(rw, staff=staff)
    assert res.status_code == 400, res.text
    assert "人工" in res.json()["error"]["message"]


def test_revisions_keep_the_latest_and_count_them(rw):
    _register(rw, "7.20", source="中行 11-30")
    second = _register(rw, "7.30", source="外管局 11-30").json()["data"]
    assert second["registered"] == {"USD": "7.30"}
    assert second["rates"]["USD"]["rate"] == "7.30"
    assert second["rates"]["USD"]["source"] == "外管局 11-30"
    body = rw.get(f"{BASE}/fx/rates", params={"period": PERIOD}, headers=H).json()["data"]
    assert body["revisions"] == 2
    assert body["rates"]["USD"]["registeredBy"] == "cfo@example"


def test_average_rates_are_a_separate_lane(rw):
    """期间平均汇率不拿期末冒充：两口径各存一份，互不覆盖。"""
    _register(rw, "7.20")
    rw.post(f"{BASE}/fx/rates",
            json={"period": PERIOD, "rates": {"USD": "7.10"},
                  "source": "期内均值", "kind": "average"}, headers=H)
    closing = rw.get(f"{BASE}/fx/rates", params={"period": PERIOD}, headers=H).json()["data"]
    average = rw.get(f"{BASE}/fx/rates",
                     params={"period": PERIOD, "kind": "average"}, headers=H).json()["data"]
    assert closing["rates"]["USD"]["rate"] == "7.20"
    assert average["rates"]["USD"]["rate"] == "7.10"


def test_unknown_field_is_rejected(rw):
    res = rw.post(f"{BASE}/fx/rates",
                  json={"period": PERIOD, "rates": {"USD": "7.2"},
                        "source": "x", "note": "y"}, headers=H)
    assert res.status_code == 400
    assert res.json()["error"]["detail"]["allowed"] == ["period", "rates", "source", "kind"]


# ---- 2) 预演 ----

def test_preview_covers_monetary_items_only_and_writes_nothing(rw):
    _seed_usd_receivable(rw)
    _register(rw)
    d = rw.app.state.drivers
    before = _pending(d)
    body = rw.get(f"{BASE}/reports/fx-revaluation-preview",
                  params={"period": PERIOD}, headers=H).json()["data"]
    assert body["period"] == PERIOD
    row = next(r for r in body["rows"] if r["accountCode"] == "1100")
    assert row["foreignBalance"] == "1000.00"
    assert row["bookedBase"] == "7000.00"
    assert row["rate"] == "7.20"
    assert row["restatedBase"] == "7200.00"
    assert row["originalDifference"] == "200.00"
    adjust = next(r for r in body["adjustments"] if r["accountCode"] == "1100")
    assert adjust["difference"] == "200.00"
    assert body["totalDifference"] == "200.00"
    assert body["missingRates"] == []
    assert _pending(d) == before                       # 预演不写


def test_preview_names_missing_rates_without_guessing(rw):
    _seed_usd_receivable(rw)
    body = rw.get(f"{BASE}/reports/fx-revaluation-preview",
                  params={"period": PERIOD}, headers=H).json()["data"]
    assert body["missingRates"] == ["USD"]
    assert "未登记" in body["note"]
    row = next(r for r in body["rows"] if r["accountCode"] == "1100")
    assert row["rate"] is None and row["restatedBase"] is None


# ---- 3) 出提案 ----

def test_revalue_posts_a_paired_proposal_not_a_posting(rw):
    _seed_usd_receivable(rw)
    _register(rw)
    d = rw.app.state.drivers
    before = _pending(d)
    res = rw.post(f"{BASE}/fx/revaluation/{PERIOD}", headers=H)
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["noop"] is False
    assert body["stableId"] == f"fx-reval:{PERIOD}"
    assert body["totalDifference"] == "200.00"
    codes = sorted(l["account_code"] for l in body["lines"])
    assert codes == ["1100", "6603"]                   # 科目 ↔ 汇兑损益成对
    debit = sum(float(l["amount"]) for l in body["lines"] if l["direction"] == "debit")
    credit = sum(float(l["amount"]) for l in body["lines"] if l["direction"] == "credit")
    assert debit == credit == 200.0
    # 确认门前账本不变（1100 仍是 7000，6603 仍是 0）
    assert _balance(d, "1100") == 7000
    assert _balance(d, "6603") == 0
    assert _pending(d) == before + 1


def test_revalue_refuses_without_a_rate_and_names_the_fix(rw):
    _seed_usd_receivable(rw)
    d = rw.app.state.drivers
    before = _pending(d)
    res = rw.post(f"{BASE}/fx/revaluation/{PERIOD}", headers=H)
    assert res.status_code == 400, res.text
    err = res.json()["error"]
    assert "USD" in err["message"]
    assert err["detail"]["missing"] == ["USD"]
    assert "register_rates" in err["detail"]["fix"]
    assert _pending(d) == before


def test_second_call_while_pending_is_a_duplicate(rw):
    _seed_usd_receivable(rw)
    _register(rw)
    d = rw.app.state.drivers
    rw.post(f"{BASE}/fx/revaluation/{PERIOD}", headers=H)
    again = rw.post(f"{BASE}/fx/revaluation/{PERIOD}", headers=H).json()["data"]
    assert again["duplicate"] is True and again["pending"] is True
    assert again["noop"] is True
    assert _pending(d) == 1


def test_after_confirming_the_rerun_is_a_noop_with_zero_difference(rw):
    """调汇落账后账上已按期末汇率计价 ⇒ 再算差额为零。用差额判，不用状态拦。"""
    _seed_usd_receivable(rw)
    _register(rw)
    d = rw.app.state.drivers
    rw.post(f"{BASE}/fx/revaluation/{PERIOD}", headers=H)
    pending = d.kernel.list_proposals(status="pending")["data"]
    for prop in pending:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")
    body = rw.post(f"{BASE}/fx/revaluation/{PERIOD}", headers=H).json()["data"]
    assert body["noop"] is True and body.get("duplicate") is not True
    assert body["proposal"] is None
    preview = rw.get(f"{BASE}/reports/fx-revaluation-preview",
                     params={"period": PERIOD}, headers=H).json()["data"]
    assert preview["totalDifference"] == "0.00"


def test_no_foreign_balance_is_noop(rw):
    body = rw.post(f"{BASE}/fx/revaluation/{PERIOD}", headers=H).json()["data"]
    assert body["noop"] is True
    assert body["proposal"] is None


def test_status_follows_the_proposal_not_a_flag(rw):
    assert rw.get(f"{BASE}/fx/status/{PERIOD}", headers=H).json()["data"]["state"] == \
        "not_revalued"
    _seed_usd_receivable(rw)
    _register(rw)
    rw.post(f"{BASE}/fx/revaluation/{PERIOD}", headers=H)
    assert rw.get(f"{BASE}/fx/status/{PERIOD}", headers=H).json()["data"]["state"] == "pending"
    d = rw.app.state.drivers
    for prop in d.kernel.list_proposals(status="pending")["data"]:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")
    body = rw.get(f"{BASE}/fx/status/{PERIOD}", headers=H).json()["data"]
    assert body["state"] == "posted" and body["attempts"] == 1


# ---- 4) 期间与权限 ----

def test_closed_period_refuses_revaluation(rwc):
    _seed_usd_receivable(rwc)
    _register(rwc)
    assert rwc.post(f"{BASE}/periods/{PERIOD}/close", headers=H, json={}).status_code == 200
    res = rwc.post(f"{BASE}/fx/revaluation/{PERIOD}", headers=H)
    assert res.status_code == 400, res.text
    assert "已关账" in res.json()["error"]["message"]


@pytest.mark.parametrize("path", ["/fx/rates", f"/fx/revaluation/{PERIOD}"])
def test_read_only_cannot_write(ro, path):
    if path == "/fx/rates":
        res = ro.post(f"{BASE}{path}", json={"period": PERIOD, "rates": {"USD": "7.2"},
                                             "source": "x"}, headers=H)
    else:
        res = ro.post(f"{BASE}{path}", headers=H)
    assert res.status_code == 403, res.text
    assert res.json()["error"]["code"] == "GRANT_REQUIRED"


def test_read_only_can_read_the_fx_views(ro):
    for path, params in [(f"{BASE}/fx/rates", {"period": PERIOD}),
                         (f"{BASE}/reports/fx-revaluation-preview", {"period": PERIOD}),
                         (f"{BASE}/fx/status/{PERIOD}", None)]:
        res = ro.get(path, params=params, headers=H)
        assert res.status_code == 200, (path, res.text)
