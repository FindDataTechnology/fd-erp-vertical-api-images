"""期间关账在 api 侧的接线（2026-11-15 `501 NOT_WIRED` 的回归钉，跨仓半边）。

api 的 `KernelProxy._period_call` 是「按方法名转发，缺位才报 NOT_WIRED」。所以
「生产 MCP 形态能不能关账」**不由 api 决定**，取决于模板层 `McpKernelClient` 有没有
那五个方法。2026-11-15 之前五个都没有 ⇒ 生产 `/fi/periods` 501，而同进程形态
（单测、demo）全绿，单测因此看不见。

本文件用**真对象**接头（`KernelProxy` 包 `McpKernelClient`，只把内层的 `_call`
换成记录器，不碰网络），钉两件事：

1. 装上真模板客户端后，五个期间方法**不再** NOT_WIRED，且 tool 名与参数按内核签名；
2. 反命题：内层真缺方法时**仍**报 `NOT_WIRED` —— 兜底必须留着，否则接更旧的内核
   镜像时会把「未接线」伪装成成功。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_api.errors import ApiError
from fd_erp_vertical_api.kernel_client import KernelProxy


def _proxy_with_real_mcp_client(monkeypatch):
    """`KernelProxy(McpKernelClient)`：真类，只有 `_call` 被换掉。"""
    from fd_erp_vertical_templates.kernel_client import McpKernelClient

    calls: list[tuple[str, dict]] = []

    def fake_call(self, tool, args):
        calls.append((tool, args))
        return {"ok": True, "data": [], "rows_returned": 0}

    monkeypatch.setattr(McpKernelClient, "_call", fake_call)
    return KernelProxy(McpKernelClient("http://kernel/mcp", "acme")), calls


@pytest.mark.parametrize("method,kwargs,tool", [
    ("close_period", {"period": "2026-11", "actor": "cfo-001"}, "close_period"),
    ("reopen_period", {"period": "2026-11", "actor": "cfo-001",
                       "reason": "补记"}, "reopen_period"),
    ("period_status", {"period": "2026-11"}, "period_status"),
    ("period_report", {"period": "2026-11"}, "period_report"),
    ("list_periods", {}, "list_periods"),
])
def test_period_tools_pass_through_mcp_client(monkeypatch, method, kwargs, tool) -> None:
    """五个期间端点经 MCP 形态都要真转发（2026-11-15 之前是 501 NOT_WIRED）。"""
    proxy, calls = _proxy_with_real_mcp_client(monkeypatch)
    out = getattr(proxy, method)(**kwargs)

    assert out["ok"] is True, "返回值被再包一层或吞掉了"
    assert calls and calls[-1][0] == tool, f"没真转发到内核工具 {tool}：{calls}"


def test_missing_method_still_reports_not_wired(monkeypatch) -> None:
    """反命题：内层真缺方法 ⇒ 仍报 NOT_WIRED（不伪造成功）。

    这条不是为了保住旧缺陷，是为了保住**语义**：接更旧的内核镜像时「未接线」
    必须是可读的错误码，而不是 200 空数据。
    """

    class _NoPeriods:
        """只有账本基础方法，没有期间关账那一族。"""

        def query_balance(self, **kwargs):
            return {"ok": True, "data": [], "rows_returned": 0}

    proxy = KernelProxy(_NoPeriods())
    with pytest.raises(ApiError) as excinfo:
        proxy.list_periods()
    assert excinfo.value.code == "NOT_WIRED"
    assert "list_periods" in str(excinfo.value)
