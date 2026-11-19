"""`decide_event` 在 api 侧的接线（2026-11-15 生产 500 的回归钉）。

**事故**：`POST /se/invoice-requests/{id}/decide` 每单必 500。红证取自生产 pod：

    kernel proxy type: KernelProxy | inner: McpKernelClient
    has decide_event on inner? False
    raised: AttributeError -> 'McpKernelClient' object has no attribute 'decide_event'

三层叠在一起才让它藏住：

1. 传输面（模板仓 `McpKernelClient`）没有 `decide_event`——api 的 MCP 内层就是它；
2. `KernelProxy.decide_event` 还多传了 `lane`/`actor`，而内核工具签名**不收**这两个
   参数（`tools.py` 内部硬编码 draft/human）——就算补了转发也是
   `unexpected_keyword_argument`；
3. `faces.py` 那段只兜 `ApiError`，所以 AttributeError/TypeError 直接裸 500。

本文件用**真对象**接头（`KernelProxy` 包真 `McpKernelClient`，只把 `_call` 换成
记录器，不碰网络），钉三件事：真转发且参数逐字；`lane`/`actor` 非默认显式拒绝；
内核拒绝原样透出。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_api.errors import ApiError
from fd_erp_vertical_api.kernel_client import KernelProxy


def _proxy(monkeypatch):
    from fd_erp_vertical_templates.kernel_client import McpKernelClient

    calls: list[tuple[str, dict]] = []

    def fake_call(self, tool, args):
        calls.append((tool, dict(args)))
        return {"ok": True, "data": {"stable_id": args.get("stable_id"),
                                     "status": args.get("decision")}}

    monkeypatch.setattr(McpKernelClient, "_call", fake_call)
    return KernelProxy(McpKernelClient("http://kernel/mcp", "acme")), calls


def test_decide_event_is_forwarded_with_kernel_signature(monkeypatch) -> None:
    """真转发到内核工具，参数只带内核签名有的三个（生产 500 的直接回归钉）。"""
    proxy, calls = _proxy(monkeypatch)
    out = proxy.decide_event("IR-C-1-000001", "accepted", note="审核通过")

    assert out["ok"] is True
    assert calls == [("decide_event", {
        "stable_id": "IR-C-1-000001", "decision": "accepted", "note": "审核通过",
    })], calls
    _, args = calls[-1]
    assert "lane" not in args and "actor" not in args, (
        f"lane/actor 被传给内层：{args} —— 内核工具签名不收这两个参数"
        "（unexpected_keyword_argument → 500）")


def test_decide_event_note_optional(monkeypatch) -> None:
    """不给 note 时内层按默认值走；api 不再补空串以外的形状。"""
    proxy, calls = _proxy(monkeypatch)
    proxy.decide_event("IR-1", "rejected", note="")
    _, args = calls[-1]
    assert args == {"stable_id": "IR-1", "decision": "rejected", "note": ""}


@pytest.mark.parametrize("kwargs", [
    {"lane": "confirmed"},
    {"actor": "cfo-001"},
    {"lane": "published", "actor": "cfo-001"},
])
def test_non_default_lane_or_actor_is_refused(monkeypatch, kwargs) -> None:
    """不许静默丢弃：内核按「人工 draft 拍板」留痕，收下别人再记 human 就是写假话。"""
    proxy, calls = _proxy(monkeypatch)
    with pytest.raises(ApiError) as excinfo:
        proxy.decide_event("IR-1", "accepted", **kwargs)
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert calls == [], f"拒绝时不应打到内核：{calls}"


def test_kernel_rejection_surfaces_as_api_error(monkeypatch) -> None:
    """内核错误信封 `ok=false` 必须翻成 ApiError（否则 faces 只兜 ApiError ⇒ 500）。"""
    from fd_erp_vertical_templates.kernel_client import McpKernelClient

    def reject(self, tool, args):
        return {"ok": False, "error": {"code": "NOT_FOUND",
                                      "message": "拟稿事件不存在：IR-404"}}

    monkeypatch.setattr(McpKernelClient, "_call", reject)
    proxy = KernelProxy(McpKernelClient("http://kernel/mcp", "acme"))
    with pytest.raises(ApiError) as excinfo:
        proxy.decide_event("IR-404", "accepted", note="")
    assert excinfo.value.code == "NOT_FOUND"
    assert "IR-404" in str(excinfo.value)
