"""内核信封的形状契约：替身不得与真生产者分叉（2026-11-15 事故的第三层回归钉）。

**事故形态**：api 的 `InProcessKernelClient.list_proposals` 返回 `{"proposals": …}`，
而真生产者（内核 MCP 工具面）走 `fd_erp_ledger_mcp.result.paginate()`，返回
`{"data", "total", "offset", "limit", "truncated"}`（截断时再加 `continuation`）。

后果两层，方向相反但都静默：
- `faces.py` 用 `["proposals"]` 硬取 ⇒ 打到真内核时 `KeyError` → 500；
- `statements.py` 用 `.get("proposals")` ⇒ **永远拿到空列表**，对账单状态解析不到，
  看起来像「这个客户没有提案」。

**为什么单测一直是绿的**：所有用例都用替身（InProcess），而替身自己造了那个错的键。
这正是「替身与真生产者形状不同」这一类最难发现的地方——它不会让任何断言变红，
只会让断言验证一件真实世界里不存在的事。

所以本文件的判据不是「函数返回什么」，而是**「替身返回的键集与真生产者一致」**，
并把真生产者的键集写成常量（取自 `result.py:paginate`，2026-11-15 实测在役内核
`list_proposals` 的返回 `['data','limit','offset','ok','rows_returned','total','truncated']`
——`ok`/`rows_returned` 由工具壳的 `stamp()` 加）。真生产者将来改信封，这条会红，
提醒同步替身与消费者，而不是等到上产后 500。
"""
from __future__ import annotations

from fd_erp_vertical_api.kernel_client import InProcessKernelClient, proposal_rows

#: 真生产者 `paginate()` 的键（`result.py`）。`ok`/`rows_returned` 由工具壳 `stamp()` 补。
REAL_ENVELOPE_KEYS = {"data", "total", "offset", "limit", "truncated",
                      "ok", "rows_returned"}

#: 旧替身造的、真生产者**没有**的键。留一条反命题钉住它不许回来。
LEGACY_FAKE_KEY = "proposals"


class _FakeKernelModule:
    """替身的最小内核模块：`list_proposals(conn, tenant, status)` 返回两行。"""

    @staticmethod
    def list_proposals(conn, tenant, status):
        return [{"stable_id": "statement-confirm|a", "status": "confirmed"},
                {"stable_id": "statement-confirm|b", "status": "proposed"}]


class _NoConn:
    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


def _client(monkeypatch) -> InProcessKernelClient:
    client = InProcessKernelClient.__new__(InProcessKernelClient)
    client._tenant = "acme"
    client._s = None
    monkeypatch.setattr(client, "_kernel_mod", lambda name: _FakeKernelModule)
    monkeypatch.setattr(client, "_connection", lambda: _NoConn())
    return client


def test_list_proposals_envelope_matches_real_producer(monkeypatch) -> None:
    """替身的键集必须等于真生产者的键集（多一个少一个都红）。"""
    out = _client(monkeypatch).list_proposals()
    assert set(out) == REAL_ENVELOPE_KEYS, (
        f"替身信封与真生产者分叉：多出 {set(out) - REAL_ENVELOPE_KEYS}、"
        f"少了 {REAL_ENVELOPE_KEYS - set(out)}。真生产者见 "
        "fd_erp_ledger_mcp/result.py:paginate + 工具壳 stamp()。")
    assert LEGACY_FAKE_KEY not in out, (
        "旧替身那个 `proposals` 键回来了——真生产者没有它，"
        "按它读的代码在上产时会是 KeyError 或永远空。")


def test_proposal_rows_reads_real_envelope() -> None:
    """消费者从 `data` 取行；旧键当兼容保留（滚动期旧内核）。"""
    rows = [{"stable_id": "statement-confirm|a", "status": "confirmed"}]
    assert proposal_rows({"ok": True, "data": rows}) == rows
    # 兼容：只在没有 data 时才看旧键（不能反过来——真内核永远先命中 data）
    assert proposal_rows({"proposals": rows}) == rows
    assert proposal_rows({}) == []
    assert proposal_rows(None) == []


def test_double_and_reader_agree_on_paging(monkeypatch) -> None:
    """分页语义也要一致：`limit` 生效、`truncated` 如实体现在 `data` 的长度上。"""
    client = _client(monkeypatch)
    out = client.list_proposals(limit=1)
    assert len(out["data"]) == 1
    assert out["total"] == 2
    assert out["truncated"] is True
    assert len(proposal_rows(out)) == 1
