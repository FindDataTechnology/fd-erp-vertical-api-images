"""`GET /metrics`：免租户闸、计数生效、自身不计数（optimize-erp-solution 6.1 收尾）。

为什么这组用例必须存在：
1. 看板/抓取器没有业务身份，走租户闸必然 401 —— 而 API 的 /metrics 曾经根本不存在，
   看板那一格一直显示 `read failed: HTTP 401`（2026-11-10 实测）。
2. 计数的中间件**必须注册在其他 http 中间件之后**（Starlette 后注册者在外层）。
   放早了会被租户闸包在内侧，被闸拒掉的请求统计不到——而「被拒」是最该看的信号。
   这条没有任何编译期保护，只能靠断言钉住。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FINDDATA = REPO.parent
for p in (str(REPO), str(FINDDATA / "fd-erp-vertical-templates"),
          str(FINDDATA / "fd-erp-ledger-mcp")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fd_erp_vertical_api.app import create_app  # noqa: E402
from fd_erp_vertical_api.config import Settings  # noqa: E402
from fd_erp_vertical_api.observability import (reset_for_tests,  # noqa: E402
                                               route_template)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    reset_for_tests()
    settings = Settings(tenant_id="t1", database_url="postgresql://x/y",
                        base_currency="CNY")
    with TestClient(create_app(settings)) as c:
        yield c
    reset_for_tests()


def test_metrics_readable_without_tenant_identity(client):
    """无任何业务身份头也能读——否则集群内抓取器永远 401。"""
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "erp_api_requests_total" in r.text
    assert "erp_api_request_seconds" in r.text


def test_metrics_is_exempt_but_business_routes_are_not(client):
    """豁免是精确的：/metrics 放行，业务端点仍被租户闸拦（闸没被拆）。"""
    assert client.get("/metrics").status_code == 200
    r = client.get("/erp/supply-chain/v1/pagesets")
    assert r.status_code in (401, 403)


def test_rejected_requests_are_counted(client):
    """被闸拒掉的请求必须计到——中间件注册顺序一错就统计不到。"""
    client.get("/erp/supply-chain/v1/pagesets")   # 预期 401（缺 X-ERP-Tenant）
    text = client.get("/metrics").text
    assert ('status="401"' in text) or ('status="403"' in text), \
        "被拒请求未计入 erp_api_requests_total —— 检查 instrument_middleware 是否最后注册"


def test_metrics_does_not_count_itself(client):
    """/metrics 自身不计数，否则每抓一次就自我放大。"""
    client.get("/metrics")
    assert 'route="/metrics"' not in client.get("/metrics").text


@pytest.mark.parametrize("raw,want", [
    ("/erp/supply-chain/v1/ue/orders/abc123def456",
     "/erp/supply-chain/v1/ue/orders/{id}"),
    ("/erp/supply-chain/v1/ue/orders",
     "/erp/supply-chain/v1/ue/orders"),
    ("/erp/supply-chain/v1/cp/waybills/WB-DEMO-0001/accept",
     "/erp/supply-chain/v1/cp/waybills/{id}/accept"),
    ("/erp/supply-chain/v1/ue/orders/42",
     "/erp/supply-chain/v1/ue/orders/{id}"),
])
def test_route_template_collapses_ids(raw, want):
    """id 必须归一到 {id}，否则标签基数会被真实单据号打爆。"""
    assert route_template(raw) == want


def test_route_template_keeps_query_off_label():
    assert route_template("/erp/supply-chain/v1/ue/orders?page=2") == \
        "/erp/supply-chain/v1/ue/orders"
