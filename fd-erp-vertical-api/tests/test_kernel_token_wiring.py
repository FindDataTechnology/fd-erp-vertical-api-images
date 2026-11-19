"""`ERP_KERNEL_TOKEN` 的接线（2026-11-15 生产事故的回归钉，api 侧）。

内核 `fd-erp_ledger_mcp/auth.py` 在 `MCP_BEARER_TOKEN` 非空时给数据面 `/mcp` 挂
bearer 门。门开了之后，本进程若没配钥匙，`fastmcp.Client` 一个 `Authorization`
都不带 ⇒ 401 被统一壳兜成 `500 INTERNAL: MCPError`。当天实测：`/health` 200、
`/pagesets` 200、`/fi/*` 与所有碰账本的端点 500，端到端静默约 38 小时。

本文件只钉**配置到装配这一段**（api 的责任边界）：`Settings.from_env()` 读
`ERP_KERNEL_TOKEN`，`build_kernel_client()` 把它交给 `McpKernelClient`。
「token 真的变成 `Authorization: Bearer`」那半在 templates 仓
`tests/test_kernel_client_bearer.py` 里验，两仓各管自己那一段。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_api import kernel_client as kc
from fd_erp_vertical_api.config import Settings

TOKEN = "kernel-bearer-token-xyz"


def test_settings_reads_kernel_token(monkeypatch) -> None:
    monkeypatch.setenv("ERP_KERNEL_URL", "http://kernel/mcp")
    monkeypatch.setenv("ERP_KERNEL_TOKEN", TOKEN)
    monkeypatch.setenv("ERP_KERNEL_TRANSPORT", "mcp")
    settings = Settings.from_env()
    assert settings.kernel_token == TOKEN
    # 凭据纪律：repr 里不得出现明文（与 database_url/kernel_url 同档）
    assert TOKEN not in repr(settings)


def test_settings_kernel_token_absent_is_none(monkeypatch) -> None:
    """空串/未设 = 没配（部署里 `ERP_KERNEL_TOKEN=` 的形态），不得变成空钥匙。"""
    monkeypatch.delenv("ERP_KERNEL_TOKEN", raising=False)
    assert Settings.from_env().kernel_token is None
    monkeypatch.setenv("ERP_KERNEL_TOKEN", "")
    assert Settings.from_env().kernel_token is None


def test_build_passes_token_to_mcp_client(monkeypatch) -> None:
    """装配层：mcp transport 下 token 必须传到 `McpKernelClient`。"""
    seen: dict = {}

    class _FakeMcp:
        def __init__(self, url, tenant_id, token=None):
            seen.update(url=url, tenant_id=tenant_id, token=token)

    import fd_erp_vertical_templates.kernel_client as tpl_kc

    monkeypatch.setattr(tpl_kc, "McpKernelClient", _FakeMcp)
    settings = Settings(tenant_id="acme", kernel_transport="mcp",
                        kernel_url="http://kernel/mcp", kernel_token=TOKEN)
    kc.build_kernel_client(settings)

    assert seen["token"] == TOKEN
    assert seen["url"] == "http://kernel/mcp"
    assert seen["tenant_id"] == "acme"


def test_build_without_token_still_works(monkeypatch) -> None:
    """反命题：没配钥匙时装配不得失败（内核没开门 / 同进程形态都要照常）。"""
    seen: dict = {}

    class _FakeMcp:
        def __init__(self, url, tenant_id, token=None):
            seen.update(token=token)

    import fd_erp_vertical_templates.kernel_client as tpl_kc

    monkeypatch.setattr(tpl_kc, "McpKernelClient", _FakeMcp)
    settings = Settings(tenant_id="acme", kernel_transport="mcp",
                        kernel_url="http://kernel/mcp")
    kc.build_kernel_client(settings)
    assert seen["token"] is None
