"""成本层与期末结转端点（change: add-fi-cost-endpoints）。

判据（对应 spec `erp-cn-localization` 新增要求）：

1. 重放：结存 + 每笔出库成本 + 缺口（成本未知为 `null`，不是 0）；
2. 出库定价：正常给单价与金额；取不到成本**拒**（绝不返回 0）；
3. 预演只算不写：`proposed=false` / `preview=true`，提案条数不变；
4. 结转只出提案：确认门前 5000/1200 无余额；
5. 成本未知时整期拒，detail 列出出库；同额重结幂等；
6. 权限分档；引擎身份不得设标准成本。

零替身：真库 + 真事件层 + 真实库存与成本车道（`inventory.receive/issue` → accepted 事件）。
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from fd_erp_vertical_api.pagesets import load_pageset_auth

FI_RO = {"supply-chain.fi": ["finance.read"]}
FI_RW = {"supply-chain.fi": ["finance.read", "finance.write"]}
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


def _approve_arrival_rule(settings) -> None:
    """到货过账规则（人工过门）：借 1200 存货 / 贷 2000 应付，金额取 payload.total_cost。

    没有这条规则，`project` 会报缺口、分录不落——那会让「存货 1000」这个断言失去意义。
    """
    from fd_erp_ledger_mcp import posting
    from fd_erp_ledger_mcp.db import connection
    with connection(settings) as conn:
        drafted = posting.submit_posting_rule(
            conn, tenant_id="tnt-demo", event_type="goods_received",
            rule={"lines": [
                {"account_code": "1200", "direction": "debit",
                 "amount_from": "payload.total_cost"},
                {"account_code": "2000", "direction": "credit",
                 "amount_from": "payload.total_cost"}]},
            actor="cfg-eng")
        posting.approve_posting_rule(conn, tenant_id="tnt-demo",
                                     event_type="goods_received",
                                     version=drafted["version"], approved_by="cfo")


def _accept(d, ref: str) -> None:
    """把库存单据的每一笔拟稿事实过确认门（唯一改变事件状态的入口）。"""
    for line in d.inventory.document(ref)["lines"]:
        d.kernel.decide_event(line["stable_id"], "accepted", note="仓管审核通过")


def _seed(client, *, cost="100", issue_qty="4") -> None:
    """入库 10 件 @100（带成本）→ 出库 N 件；每单过确认门**并投影**。

    投影不能省：数量面在投影时才更新，不投影则出库会被判「可用库存 0」。
    """
    d = client.app.state.drivers
    _approve_arrival_rule(client.app.state.settings)
    rc = d.inventory.receive(warehouse="WH-A", lines=[
        {"sku": "SKU-001", "qty": "10", "unit": "个",
         "cost": cost, "currency": "CNY"}])
    _accept(d, rc["ref"])
    d.inventory.project(rc["ref"])
    if issue_qty is not None:
        io = d.inventory.issue(warehouse="WH-A", lines=[
            {"sku": "SKU-001", "qty": issue_qty, "unit": "个"}])
        _accept(d, io["ref"])
        d.inventory.project(io["ref"])


def _pending(d) -> int:
    return len(d.kernel.list_proposals(status="pending")["data"])


def _balance(d, code: str) -> Decimal:
    rows = d.kernel.query_balance(account_code=code)["data"]
    return Decimal(rows[0]["balance"]) if rows else Decimal(0)


# ---- 1) 重放 ----

def test_replay_shows_layers_closing_and_issue_costs(rw):
    _seed(rw)
    res = rw.get(f"{BASE}/cost/replay", headers=H)
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["method"] == "weighted-average"
    assert body["provisionalBasis"] == "reject"

    closing = {(r["sku"], r["warehouse"]): r for r in body["closing"]}
    stock = closing[("SKU-001", "WH-A")]
    assert stock["qty"] == "6.000000"          # 10 − 4
    assert stock["value"] == "600.000000"
    assert stock["basis"] == "actual"

    assert len(body["issues"]) == 1
    assert body["issues"][0]["cost"] == "400.000000"   # 4 × 100
    assert body["gaps"] == []


def test_replay_reports_unknown_cost_as_a_gap_not_a_zero(rw):
    """成本未知的入库：结存 `basis=unknown`、出库成本 `null`、缺口清单点名。"""
    _seed(rw, cost=None, issue_qty="2")
    body = rw.get(f"{BASE}/cost/replay", headers=H).json()["data"]
    stock = {(r["sku"], r["warehouse"]): r for r in body["closing"]}[("SKU-001", "WH-A")]
    assert stock["value"] is None and stock["basis"] == "unknown"
    assert body["issues"][0]["cost"] is None            # 不是 "0.000000"
    assert body["gaps"] and body["gaps"][0]["reason"] == "cost_missing"


# ---- 2) 出库定价 ----

def test_issue_price_computes_unit_and_amount(rw):
    _seed(rw)
    body = rw.get(f"{BASE}/cost/issue-price",
                  params={"sku": "SKU-001", "qty": "2", "warehouse": "WH-A"},
                  headers=H).json()["data"]
    assert body["unitCost"] == "100.000000"
    assert body["amount"] == "200.000000"
    assert body["method"] == "weighted-average"
    assert body["layers"]                              # 消耗了哪几层


@pytest.mark.parametrize("params,needle,available", [
    ({"sku": "NOPE", "qty": "1", "warehouse": "WH-A"}, "没有成本层", None),
    # 入库 10 出库 4 ⇒ 可用 6；问 999 被拒时「可用」必须报 6（不是 0）
    ({"sku": "SKU-001", "qty": "999", "warehouse": "WH-A"}, "成本层不足", "6.000000"),
])
def test_issue_price_refuses_instead_of_pricing_zero(rw, params, needle, available):
    """取不到成本就拒：响应里 MUST NOT 出现 unitCost/amount——那才是「按 0 计价」。"""
    _seed(rw)
    res = rw.get(f"{BASE}/cost/issue-price", params=params, headers=H)
    assert res.status_code == 400, res.text
    err = res.json()["error"]
    assert needle in err["message"]
    assert "unitCost" not in (err.get("detail") or {})
    assert "amount" not in (err.get("detail") or {})
    if available is not None:
        assert err["detail"]["available"] == available


def test_issue_price_refuses_when_the_layer_cost_is_unknown(rw):
    _seed(rw, cost=None, issue_qty="2")
    # 期初 8 件成本未知；再出 1 件应被拒（不按 0 计价）
    res = rw.get(f"{BASE}/cost/issue-price",
                 params={"sku": "SKU-001", "qty": "1", "warehouse": "WH-A"},
                 headers=H)
    assert res.status_code == 400
    assert "成本未知" in res.json()["error"]["message"]


# ---- 3) 预演 ----

def test_cogs_preview_does_not_write(rw):
    _seed(rw)
    d = rw.app.state.drivers
    before = _pending(d)
    res = rw.get(f"{BASE}/reports/cogs-preview", params={"period": PERIOD},
                 headers=H)
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["preview"] is True and body["proposed"] is False
    assert body["total"] == "400.000000"
    assert body["issues"] == 1
    assert body["proposal"] is None
    assert body["blockers"] == [] and body["canSubmit"] is True
    assert _pending(d) == before


def test_cogs_preview_lists_blockers_when_cost_is_unknown(rw):
    _seed(rw, cost=None, issue_qty="2")
    body = rw.get(f"{BASE}/reports/cogs-preview", params={"period": PERIOD},
                  headers=H).json()["data"]
    assert body["canSubmit"] is False
    assert any("成本未知" in b for b in body["blockers"]), body["blockers"]
    assert body["total"] == "0.000000"


# ---- 4) 结转 ----

def test_cogs_posts_a_proposal_only(rw):
    _seed(rw)
    d = rw.app.state.drivers
    before = _pending(d)
    res = rw.post(f"{BASE}/cost/cogs/{PERIOD}", headers=H)
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["proposed"] is True
    assert body["total"] == "400.000000"
    assert body["stableId"] == f"cogs:{PERIOD}"
    # 确认门前：营业成本与存货都没变（存货仍是入库后的 1000）
    assert _balance(d, "5000") == Decimal(0)
    assert _balance(d, "1200") == Decimal(1000)
    assert _pending(d) == before + 1

    prop = d.kernel.list_proposals(status="pending")["data"][0]
    d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")
    assert _balance(d, "5000") == Decimal(400)
    assert _balance(d, "1200") == Decimal(600)


def test_cogs_refuses_the_whole_period_when_cost_is_unknown(rw):
    _seed(rw, cost=None, issue_qty="2")
    d = rw.app.state.drivers
    before = _pending(d)
    res = rw.post(f"{BASE}/cost/cogs/{PERIOD}", headers=H)
    assert res.status_code == 400
    err = res.json()["error"]
    assert "成本未知" in err["message"]
    assert err["detail"]["uncosted"]                    # 点名了哪几笔
    assert _pending(d) == before                        # 一张提案都没有


def test_cogs_is_idempotent_for_the_same_amount(rw):
    _seed(rw)
    d = rw.app.state.drivers
    rw.post(f"{BASE}/cost/cogs/{PERIOD}", headers=H)
    again = rw.post(f"{BASE}/cost/cogs/{PERIOD}", headers=H).json()["data"]
    assert again["duplicate"] is True and again["proposed"] is False
    assert again["total"] == "400.000000"
    assert _pending(d) == 1


# ---- 5) 标准成本 ----

def test_standard_cost_registration(rw):
    body = rw.post(f"{BASE}/cost/standard",
                   json={"sku": "SKU-001", "unitCost": "99.5"}, headers=H).json()["data"]
    assert body["sku"] == "SKU-001"
    assert body["unit_cost"] == "99.500000"
    assert body["note"].startswith("已登记")


def test_engine_identity_cannot_set_standard_cost(rw):
    """标准成本是会计估计：REST 层就把引擎身份拒掉，不靠额外配置。"""
    res = rw.post(f"{BASE}/cost/standard",
                  headers={**TENANT_HEADER, "X-ERP-Staff": "cost-engine"},
                  json={"sku": "SKU-001", "unitCost": "99.5"})
    assert res.status_code == 400
    assert "人工" in res.json()["error"]["message"]


def test_standard_cost_rejects_unknown_fields(rw):
    res = rw.post(f"{BASE}/cost/standard", headers=H,
                  json={"sku": "SKU-001", "unitCost": "99.5", "rate": "1"})
    assert res.status_code == 400
    assert res.json()["error"]["detail"]["allowed"] == ["sku", "unitCost", "currency"]


# ---- 6) 权限分档 ----

@pytest.mark.parametrize("path,payload", [
    (f"{BASE}/cost/cogs/{PERIOD}", None),
    (f"{BASE}/cost/standard", {"sku": "SKU-001", "unitCost": "1"}),
])
def test_read_only_cannot_write(ro, path, payload):
    res = ro.post(path, headers=H, json=payload) if payload is not None \
        else ro.post(path, headers=H)
    assert res.status_code == 403, res.text
    assert res.json()["error"]["code"] == "GRANT_REQUIRED"


def test_read_only_can_read_cost_views(ro):
    for path, params in [
        (f"{BASE}/cost/replay", None),
        (f"{BASE}/reports/cogs-preview", {"period": PERIOD}),
    ]:
        res = ro.get(path, params=params, headers=H)
        assert res.status_code == 200, (path, res.text)
