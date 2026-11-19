"""承载探针（`/health` / `/healthz`）契约。

背景（serve-erp-vertical-api 1.1）：单副本 Deployment 的 readiness/liveness
探针**不带任何业务身份**（没有 `X-ERP-Tenant`）。此前租户闸拦一切路径，探针
恒 401 → Pod 永不 Ready。本组用例锁死两件事：

1. 探针免租户闸，且只回服务名与内核承载形态（不回显租户、DSN 或任何业务数据）；
2. 业务端点**没有**被顺手放行——无租户头依旧 401 `AUTH_REQUIRED`。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from fd_erp_vertical_api.app import create_app
from fd_erp_vertical_api.cart import CartStore
from fd_erp_vertical_api.config import Settings
from fd_erp_vertical_api.pagesets import load_pageset_auth

TENANT = "probe-tenant"
GRANTS = '{"supply-chain.ue": ["catalog.read"]}'


class _NullKernel:
    """探针不碰内核；这个替身只满足装配形状（任何调用都是测试失败信号）。"""

    def __getattr__(self, name):  # pragma: no cover - 出现即说明探针越界
        raise AssertionError(f"探针路径不该调用内核：{name}")


def _client(tmp_path) -> TestClient:
    settings = Settings(
        tenant_id=TENANT,
        database_url="postgresql://unused/unused",
        base_currency="CNY",
        data_dir=str(tmp_path / "data"),
        pageset_auth_json=GRANTS,
    )
    app = create_app(settings, _NullKernel(),
                     pageset_auth=load_pageset_auth(inline_json=GRANTS),
                     cart_store=CartStore())
    return TestClient(app)


def test_health_is_reachable_without_tenant_header(tmp_path):
    res = _client(tmp_path).get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body == {"ok": True, "service": "fd-erp-vertical-api",
                    "kernel_transport": "inprocess"}


def test_healthz_alias(tmp_path):
    assert _client(tmp_path).get("/healthz").status_code == 200


def test_health_leaks_no_tenant_or_dsn(tmp_path):
    text = _client(tmp_path).get("/health").text.lower()
    for forbidden in (TENANT, "postgres", "password", "dsn", "unused"):
        assert forbidden.lower() not in text


def test_business_routes_still_gated(tmp_path):
    """探针豁免必须精准：业务端点无租户头仍 401。"""
    res = _client(tmp_path).get("/erp/supply-chain/v1/pagesets")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "AUTH_REQUIRED"
