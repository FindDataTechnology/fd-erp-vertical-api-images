"""对账入口代理（optimize-erp-solution 6.6）：提交即返回 task_id，报告靠轮询。

- 常态必须是「能跑」：submit 202 + task_id → 轮询自己的报告端点 → done + 差异；
  NOT_WIRED 不得作为常态返回（本文件第一条用例即是对它的回归）。
- 内核不可达：501 + planned_lane（spec 明示），不是 500、不伪造结果。
- 源声明白名单：只放 inline（内核的 api/csv 适配器会服务端拉 URL / 读本地文件，
  不能透传给客户面）。
"""
from __future__ import annotations

import time

from fd_erp_vertical_api.errors import ApiError

from .conftest import _h

RECON_PATH = "/erp/supply-chain/v1/ue/reconciliations"


def _rec(sid, amount, status="posted"):
    return {"stable_id": sid, "amount": amount, "currency": "CNY", "status": status}


def _body(left, right):
    return {"left_source": {"type": "inline", "records": left},
            "right_source": {"type": "inline", "records": right}}


def _poll(client, headers, task_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        r = client.get(f"{RECON_PATH}/{task_id}", headers=headers)
        assert r.status_code == 200, r.text
        last = r.json()["data"]
        if last["status"] in ("done", "failed", "interrupted"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"对账任务未在 {timeout}s 内收敛：{last}")


def test_recon_submit_returns_task_id_then_report(client, seeded):
    """经纵向 API 发起两源对账 → 返回任务标识 → 可查报告与差异（NOT_WIRED 消失）。"""
    headers = _h(seeded["cust1"])
    r = client.post(RECON_PATH, headers=headers,
                    json=_body([_rec("a", "100"), _rec("b", 200)],
                               [_rec("a", 100), _rec("b", 250)]))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["ok"] is True
    task = body["data"]
    assert task["task_id"], task
    assert task["status"] == "queued"
    assert task["poll_with"] == "get_recon_report"
    assert task["poll_url"].endswith(task["task_id"])

    report = _poll(client, headers, task["task_id"])
    assert report["status"] == "done"
    assert report["processed"] == report["total"] == 2
    assert report["result"]["summary"]["by_type"]["amount_mismatch"] == 1
    # 只读滩头：对客户外部系统零写入
    assert report["result"]["summary"]["external_writes"] == 0
    assert report["result"]["diffs"][0]["stable_id"] == "b"


def test_recon_report_not_found_is_404(client, seeded):
    """未知任务如实 404 NOT_FOUND（不是 500，也不是 501）。"""
    r = client.get(f"{RECON_PATH}/no-such-task", headers=_h(seeded["cust1"]))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_recon_requires_statement_grant(client_no_statement_grant, seeded):
    """缺 statement.confirm 时 403 GRANT_REQUIRED（不越权跑对账）。"""
    r = client_no_statement_grant.post(RECON_PATH, headers=_h(seeded["cust1"]),
                                       json=_body([_rec("a", 1)], [_rec("a", 1)]))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "GRANT_REQUIRED"


def test_recon_kernel_unavailable_degrades_to_501(client, seeded):
    """内核不可达 → 501 + planned_lane（spec 场景「内核不可用时如实降级」）。"""
    kernel = client.app.state.kernel

    def _down(*args, **kwargs):
        raise ApiError("UPSTREAM_UNAVAILABLE", "内核对账引擎不可达（OperationalError）",
                       detail={"kernel": "recon"})

    kernel.run_reconciliation = _down
    kernel.get_recon_report = _down
    r = client.post(RECON_PATH, headers=_h(seeded["cust1"]),
                    json=_body([_rec("a", 1)], [_rec("a", 1)]))
    assert r.status_code == 501, r.text
    err = r.json()["error"]
    assert err["code"] == "NOT_IMPLEMENTED"
    assert err["detail"]["planned_lane"]
    assert err["detail"]["kernel_code"] == "UPSTREAM_UNAVAILABLE"
    # 查报告同样降级
    r = client.get(f"{RECON_PATH}/any-task", headers=_h(seeded["cust1"]))
    assert r.status_code == 501
    assert r.json()["error"]["detail"]["planned_lane"]


def test_recon_rejects_unsafe_source_types(client, seeded):
    """api/csv 源被拒（服务端拉外部 URL / 读本地文件 = SSRF 与文件读取面）。"""
    headers = _h(seeded["cust1"])
    for spec in ({"type": "api", "url": "http://169.254.169.254/latest/meta-data"},
                 {"type": "csv", "path": "file:/etc/passwd"}):
        r = client.post(RECON_PATH, headers=headers,
                        json={"left_source": spec,
                              "right_source": {"type": "inline", "records": []}})
        assert r.status_code == 400, (spec, r.status_code)
        err = r.json()["error"]
        assert err["code"] == "INVALID_ARGUMENT"
        assert err["detail"]["allowed"] == ["inline"]
        assert err["detail"]["planned_lane"]


def test_recon_source_record_cap(client, seeded):
    """单侧记录上限：超限如实 400，不静默截断。"""
    from fd_erp_vertical_api.app import RECON_MAX_RECORDS
    records = [_rec(f"x{i}", 1) for i in range(RECON_MAX_RECORDS + 1)]
    r = client.post(RECON_PATH, headers=_h(seeded["cust1"]),
                    json={"left_source": {"type": "inline", "records": records},
                          "right_source": {"type": "inline", "records": []}})
    assert r.status_code == 400
    assert r.json()["error"]["detail"]["limit"] == RECON_MAX_RECORDS


# ---- 两形态一致（MCP 错误信封 vs InProcess 异常）：这层不做就会「测试绿、产上错」----

class _EnvelopeKernel:
    """模拟 MCP 工具面：内核 @safe_tool 把失败包成信封**返回**，不抛异常。"""

    def __init__(self, envelope):
        self._envelope = envelope
        self.calls = 0

    def run_reconciliation(self, left_source, right_source, retry_task_id=None,
                           tenant_id=None):
        self.calls += 1
        return self._envelope

    def get_recon_report(self, task_id, tenant_id=None):
        self.calls += 1
        return self._envelope


def test_recon_proxy_translates_mcp_error_envelope():
    """MCP 信封里的 RECON_TASK_NOT_FOUND → NOT_FOUND 404（不是 200 数据、不是 500）。"""
    from fd_erp_vertical_api.errors import ApiError
    from fd_erp_vertical_api.kernel_client import KernelProxy

    proxy = KernelProxy(_EnvelopeKernel({
        "ok": False, "rows_returned": 0,
        "error": {"code": "RECON_TASK_NOT_FOUND", "message": "对账任务不存在：x",
                  "detail": {"task_id": "x"}}}))
    try:
        proxy.get_recon_report("x")
    except ApiError as exc:
        assert exc.code == "NOT_FOUND" and exc.http_status == 404
        assert exc.detail == {"task_id": "x"}
    else:
        raise AssertionError("MCP 错误信封被当成成功数据返回了")


def test_recon_proxy_translates_unknown_tool_envelope():
    """内核没有该工具（未知码）→ UPSTREAM_UNAVAILABLE（REST 再降级 501），不落 500。"""
    from fd_erp_vertical_api.errors import ApiError
    from fd_erp_vertical_api.kernel_client import KernelProxy

    proxy = KernelProxy(_EnvelopeKernel({
        "ok": False,
        "error": {"code": "UNKNOWN_TOOL", "message": "tool not found"}}))
    try:
        proxy.run_reconciliation([], [])
    except ApiError as exc:
        assert exc.code == "UPSTREAM_UNAVAILABLE"
        assert exc.detail["kernel_code"] == "UNKNOWN_TOOL"
    else:
        raise AssertionError("未知工具错误信封未翻译")


def test_recon_proxy_passthrough_other_methods():
    """未覆盖的工具面方法原样透传（不改信用/计费/账本行为）。"""
    class _Inner:
        def query_balance(self, **kwargs):
            return {"data": [{"account_code": "1100", "balance": "1"}]}

    from fd_erp_vertical_api.kernel_client import KernelProxy
    assert KernelProxy(_Inner()).query_balance()["data"][0]["balance"] == "1"


def test_recon_proxy_classifies_wrapped_connection_error():
    """fastmcp 把连接失败包成 builtins.RuntimeError —— 也须归成 UPSTREAM_UNAVAILABLE。

    实测形态（内核进程停掉）：RuntimeError("Client failed to connect: All connection
    attempts failed")，类型上完全看不出来，只能按消息判；不判就会落成 500 INTERNAL，
    违反 spec「内核不可用 → 501」。
    """
    from fd_erp_vertical_api.errors import ApiError
    from fd_erp_vertical_api.kernel_client import KernelProxy

    class _Down:
        def run_reconciliation(self, *args, **kwargs):
            raise RuntimeError("Client failed to connect: All connection attempts failed")

    try:
        KernelProxy(_Down()).run_reconciliation([], [])
    except ApiError as exc:
        assert exc.code == "UPSTREAM_UNAVAILABLE"
    else:
        raise AssertionError("连接失败未归类")


def test_recon_proxy_keeps_real_bug_visible():
    """真正的实现缺陷不能被当成「内核不可用」（否则 501 掩盖 500 类 bug）。"""
    from fd_erp_vertical_api.kernel_client import KernelProxy

    class _Buggy:
        def run_reconciliation(self, *args, **kwargs):
            raise TypeError("unexpected keyword argument")

    try:
        KernelProxy(_Buggy()).run_reconciliation([], [])
    except TypeError as exc:
        assert "unexpected keyword" in str(exc)
    else:
        raise AssertionError("实现缺陷被吞成内核不可用")
