"""内核事件层错误码在 REST 面的透出（change establishment-erp-event-kernel 5.2）。

为什么要有这一组：`adapt_exception` 的行为是「码不在 CATALOG 就塌成 INTERNAL 500」。
内核事件层新增 5 个码后，若 api 侧不登记，调用方拿到的会是 500「服务内部错误」，
把「事件已定型、请走冲正」「该类型缺过账规则、已挂待补」这类**可执行的信息**
全部丢掉——这类失败以前在别的仓上真出现过（失败文案被改写＝分类器不认）。

本文件不碰数据库：只验错误码映射语义。
"""
from __future__ import annotations

from fd_erp_vertical_api.errors import (ERROR_CATALOG, ApiError, adapt_exception,
                                        http_status_for)
from fd_erp_vertical_api.kernel_client import _kernel_result

EVENT_CODES = ("EVENT_IMMUTABLE", "EVENT_UNKNOWN_TYPE", "EVENT_STATE",
               "OBJECT_CONFLICT", "POSTING_RULE_MISSING")


class _KernelError(Exception):
    """仿内核 ErpError：带 code / message / hint / detail。"""

    def __init__(self, code, message, hint="", detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.detail = detail or {}


def test_event_codes_are_registered():
    for code in EVENT_CODES:
        assert code in ERROR_CATALOG, f"{code} 未在 api 封闭集登记"


def test_registered_event_codes_pass_through_not_collapsed_to_internal():
    """核心断言：5 个码都必须原样透出；塌成 INTERNAL 就是回归。"""
    for code in EVENT_CODES:
        out = adapt_exception(_KernelError(code, f"{code} happened", hint="按提示处理"))
        assert isinstance(out, ApiError)
        assert out.code == code, f"{code} 被改写成 {out.code}"
        assert out.hint == "按提示处理"  # 内核侧 hint 优先，不被默认 hint 覆盖


def test_http_statuses_are_distinguishable():
    """not_wired（缺规则）与 invalid_argument（状态/入参）不能同一个状态码。"""
    assert http_status_for("POSTING_RULE_MISSING") == 501
    assert http_status_for("EVENT_STATE") == 409
    assert http_status_for("EVENT_UNKNOWN_TYPE") == 400
    assert http_status_for("EVENT_IMMUTABLE") == 409
    assert http_status_for("OBJECT_CONFLICT") == 409


def test_posting_rule_missing_is_not_reported_as_upstream_failure():
    """缺过账规则是「没接线」，不是「上游挂了」——重试无意义，修法是补规则。"""
    meta = ERROR_CATALOG["POSTING_RULE_MISSING"]
    assert meta.semantic_class == "not_wired"
    assert meta.retryable is False


def test_unknown_kernel_code_still_collapses_to_internal():
    """回归护栏：未登记码仍塌成 INTERNAL —— 别把这个兜底也弄丢。"""
    out = adapt_exception(_KernelError("SOME_NEW_KERNEL_CODE", "boom"))
    assert out.code == "INTERNAL"


def test_recon_envelope_passes_registered_codes_and_keeps_unknown_visible():
    # 已登记：原样透出
    try:
        _kernel_result({"ok": False, "error": {"code": "POSTING_RULE_MISSING",
                                              "message": "缺规则", "detail": {}}}, "run")
        raise AssertionError("应当抛 ApiError")
    except ApiError as exc:
        assert exc.code == "POSTING_RULE_MISSING"

    # 未登记：降级为 UPSTREAM_UNAVAILABLE，但 kernel_code 必须留在 detail 里，
    # 否则排查时会以为"内核没这个码"
    try:
        _kernel_result({"ok": False, "error": {"code": "BRAND_NEW_CODE",
                                              "message": "x", "detail": {}}}, "run")
        raise AssertionError("应当抛 ApiError")
    except ApiError as exc:
        assert exc.code == "UPSTREAM_UNAVAILABLE"
        assert exc.detail.get("kernel_code") == "BRAND_NEW_CODE"
