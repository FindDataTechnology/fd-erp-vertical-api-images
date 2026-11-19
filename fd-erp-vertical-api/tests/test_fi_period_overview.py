"""期间清单与错误分档（change: fix-fi-surface-audit）。

这一轮不是加端点，是把 61 个端点走一遍抓到的两处**语义错位**钉住：

1. `GET /fi/periods` 必须包含「有分录但没关账」的期间——只列关账记录会让要关的那个月消失；
2. 缺税率报 **400**（配置缺失），不是 501（能力缺失）。
"""
from __future__ import annotations

import json

import pytest

from fd_erp_vertical_api.pagesets import load_pageset_auth

FI_RWC = {"supply-chain.fi": ["finance.read", "finance.write", "finance.confirm"]}
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

    payload = json.dumps(FI_RWC)
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


def _confirm_all(d) -> int:
    pending = d.kernel.list_proposals(status="pending")["data"]
    for prop in pending:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")
    return len(pending)


def _seed_sale(client, ref="OV-S1") -> None:
    d = client.app.state.drivers
    d.cn_tax.post_sale(ref, "2260", rate="13", gross=True, customer="C-A",
                       booked_on="2026-11-05", submitted_by="cfo@example")
    _confirm_all(d)


# ---- 1) 期间清单包含有分录的期间 ----

def test_period_list_is_empty_on_a_blank_ledger(client):
    assert client.get(f"{BASE}/periods", headers=H).json()["data"] == []


def test_period_with_entries_but_never_closed_shows_up(client):
    """这条是审计抓到的那一处：此前账本里有整月业务，`GET /fi/periods` 返回 []。"""
    _seed_sale(client)
    rows = client.get(f"{BASE}/periods", headers=H).json()["data"]
    assert [r["period"] for r in rows] == [PERIOD]
    row = rows[0]
    assert row["closed"] is False
    assert row["hasEntries"] is True
    assert row["vouchers"] == 1          # 一笔含税销售 = 一张凭证
    assert row["lines"] == 3             # 应收 / 收入 / 销项
    assert row["lastBookedOn"] == "2026-11-05"


def test_closed_period_keeps_actor_and_stays_in_the_list(client):
    _seed_sale(client)
    client.post(f"{BASE}/periods/{PERIOD}/close", headers=H, json={})
    rows = client.get(f"{BASE}/periods", headers=H).json()["data"]
    row = next(r for r in rows if r["period"] == PERIOD)
    assert row["closed"] is True
    assert row["actor"] == "cfo@example"
    assert row["hasEntries"] is True     # 关账之后仍能看见它有过业务
    assert row["vouchers"] == 1


def test_two_months_are_sorted_and_both_visible(client):
    d = client.app.state.drivers
    _seed_sale(client, "OV-S1")
    d.cn_tax.post_sale("OV-S2", "1130", rate="13", gross=True, customer="C-A",
                       booked_on="2026-12-03", submitted_by="cfo@example")
    _confirm_all(d)
    rows = client.get(f"{BASE}/periods", headers=H).json()["data"]
    assert [r["period"] for r in rows] == ["2026-11", "2026-12"]
    assert all(r["hasEntries"] is True for r in rows)


# ---- 2) 配置缺失报 400，不是 501 ----

@pytest.mark.parametrize("path", [f"{BASE}/reports/surcharges",
                                  f"{BASE}/reports/income-tax-prepay"])
def test_missing_tax_rates_is_400_not_501(client, path):
    """能力是好的，缺的是这个租户的登记；501 会被监控当服务降级、被使用者当没做。"""
    res = client.get(path, params={"period": PERIOD}, headers=H)
    assert res.status_code == 400, res.text
    err = res.json()["error"]
    assert err["code"] == "INVALID_ARGUMENT"
    assert "register_rates" in err["message"]
    assert err["detail"]["missing"]                      # 点名缺哪几项


def test_rates_registered_then_preview_works(client):
    d = client.app.state.drivers
    d.cn_filing.register_rates({"city_construction": "7", "education_surcharge": "3",
                               "local_education_surcharge": "2", "income_tax": "25"},
                               effective_from="2026-01", source="现行税法")
    res = client.get(f"{BASE}/reports/surcharges", params={"period": PERIOD}, headers=H)
    assert res.status_code == 200, res.text
    assert res.json()["data"]["proposed"] is False


# ---- 3) 两种传输形态的返回形状必须归一 ----

class _ListKernel:
    """inprocess 形态：直接回 list。"""

    def list_periods(self):
        return [{"period": "2026-11", "closed": True, "actor": "cfo",
                 "decidedAt": "2026-11-14"}]


class _McpKernel:
    """mcp 形态：回 {ok, data, rows_returned}。"""

    def list_periods(self):
        return {"ok": True, "rows_returned": 1,
                "data": [{"period": "2026-11", "closed": True, "actor": "cfo",
                          "decidedAt": "2026-11-14"}]}


@pytest.mark.parametrize("kernel", [_ListKernel(), _McpKernel()])
def test_period_list_shape_is_normalised_across_transports(kernel):
    """两个传输形态返回**不同形状**，归一后必须一样。

    否则同一端点在测试环境返回数组、在生产返回嵌套对象，而两边都「有数据」，
    谁也看不出错——同族于 cost 车道 `items`/`events`/`data` 的键名教训。
    """
    from fd_erp_vertical_api.periods import PeriodBook

    rows = PeriodBook(kernel).list()
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["period"] == "2026-11" and rows[0]["closed"] is True
    # overview 同样吃这两种形态
    merged = PeriodBook(kernel).overview([])
    assert merged[0]["hasEntries"] is False and merged[0]["vouchers"] == 0
