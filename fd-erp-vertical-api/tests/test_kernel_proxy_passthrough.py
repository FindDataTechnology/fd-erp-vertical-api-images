"""`KernelProxy` 的透传方法也必须归一（2026-11-15 生产实测两条假象的回归钉）。

`KernelProxy` 只手写覆盖了 10 个方法（对账 2 / 事件层 2 / decide / 期间 5），其余靠
`__getattr__` **裸透传**。而内核 `@safe_tool` 是把异常**返回**成错误信封
`{"ok": false, "error": {...}}`（不抛），所以裸透传在 MCP 形态下等于：

- `confirm_proposal(proposal_id=999999999)` → 返回信封；`proposal_rows()` 读真键 `data`
  取不到 → `[]` ⇒ 页面写「没有提案」（静默空）；
- `query_credit("未登记客户")` → 返回信封；消费点取 `available`/`limit` 得到 None。

生产红证（pod 内实跑，2026-11-15）：
    passthrough confirm_proposal -> {"ok": false, "error": {"code": "NOT_FOUND", …}}
    proposal_rows(...) -> [] | len = 0

本文件钉住修好后的语义：透传的每个可调用对象都过 `_raise_kernel_error` +
`_kernel_result`——成功原样返回、失败变 `ApiError`（带内核业务码），且新增工具
天然被覆盖（不需要谁记得手写一层）。
"""
from __future__ import annotations

import pytest

from fd_erp_vertical_api.errors import ApiError
from fd_erp_vertical_api.kernel_client import KernelProxy, proposal_rows


class _Inner:
    """假内层：`list_proposals` / `query_credit` 都不在 KernelProxy 的手写覆盖里。"""

    def __init__(self, result):
        self._result = result
        self.calls: list[tuple[str, dict]] = []

    def list_proposals(self, **kwargs):
        self.calls.append(("list_proposals", kwargs))
        return self._result

    def query_credit(self, customer_id, **kwargs):
        self.calls.append(("query_credit", {"customer_id": customer_id, **kwargs}))
        return self._result

    # 非可调用属性：透传要原样给出（别把数据包装成函数）
    version = "kernel-v1"


def _envelope(code="NOT_FOUND", message="proposal 不存在"):
    return {"ok": False,
            "error": {"code": code, "message": message,
                      "hint": "对象不存在", "retryable": False, "detail": {}},
            "rows_returned": 0}


def test_passthrough_success_is_returned_unchanged():
    out = {"ok": True, "data": [{"id": 1}], "rows_returned": 1}
    proxy = KernelProxy(_Inner(out))
    assert proxy.list_proposals(status="pending") is out


def test_passthrough_error_envelope_becomes_api_error():
    """核心回归：信封必须变异常，否则 `proposal_rows` 会读成空。"""
    proxy = KernelProxy(_Inner(_envelope()))
    with pytest.raises(ApiError) as excinfo:
        proxy.list_proposals()
    assert excinfo.value.code == "NOT_FOUND"
    assert "proposal 不存在" in str(excinfo.value)
    # 反命题：修好之后调用方拿不到信封，`proposal_rows` 永远不会把它读成 []
    assert not issubclass(ApiError, dict)


def test_passthrough_error_envelope_for_other_method():
    """不是特例：同一个 `__getattr__` 覆盖所有透传方法（新工具天然受益）。"""
    proxy = KernelProxy(_Inner(_envelope("NOT_FOUND", "该客户未登记信用额度")))
    with pytest.raises(ApiError) as excinfo:
        proxy.query_credit("SMOKE-CUSTOMER-NOPE")
    assert excinfo.value.code == "NOT_FOUND"


def test_proposal_rows_never_sees_an_envelope():
    """回归的判据写成行为：拿到信封就抛，拿不到才是行。"""
    proxy = KernelProxy(_Inner(_envelope()))
    with pytest.raises(ApiError):
        proposal_rows(proxy.list_proposals())


def test_unregistered_code_maps_to_upstream_unavailable():
    proxy = KernelProxy(_Inner(_envelope("SOMETHING_NEW", "内核新码")))
    with pytest.raises(ApiError) as excinfo:
        proxy.list_proposals()
    assert excinfo.value.code == "UPSTREAM_UNAVAILABLE"
    assert excinfo.value.detail.get("kernel_code") == "SOMETHING_NEW"


def test_passthrough_transport_failure_is_mapped():
    """连接类失败 → UPSTREAM_UNAVAILABLE（与业务错误分开，前者可重试/降级）。"""
    class _Raising:
        def list_proposals(self, **kwargs):
            raise ConnectionError("内核不可达")

    with pytest.raises(ApiError) as excinfo:
        KernelProxy(_Raising()).list_proposals()
    assert excinfo.value.code == "UPSTREAM_UNAVAILABLE"


def test_passthrough_unknown_exception_propagates():
    """未知异常**原样抛出**（→ INTERNAL 500），别伪装成「内核不可用」掩盖缺陷。"""
    class _Raising:
        def list_proposals(self, **kwargs):
            raise RuntimeError("代码缺陷，不是传输问题")

    with pytest.raises(RuntimeError):
        KernelProxy(_Raising()).list_proposals()


def test_non_callable_attribute_passes_through():
    assert KernelProxy(_Inner({})).version == "kernel-v1"


def test_private_attributes_mirror_the_inner():
    """单下划线私有属性照常镜像内层。

    代理的契约是「与内层同形」：测试种子数据（`kernel._s` / `kernel._tenant`，直连库配
    规则）与运维脚本都在用这条。挡掉它不会更安全，只会让每个新用例都得记得穿一层
    `._inner`——**那个约定没人会记得**，代价是一个接一个的 setup AttributeError。
    """
    class _WithPrivates:
        _s = "settings-obj"
        _tenant = "t-demo"

    proxy = KernelProxy(_WithPrivates())
    assert proxy._s == "settings-obj"
    assert proxy._tenant == "t-demo"


def test_inner_itself_is_not_proxied():
    """`_inner` 不能接管：`__init__` 里它尚未赋值时接管会无限递归。"""
    proxy = KernelProxy(_Inner({}))
    assert proxy._inner.__class__ is _Inner


def test_dunder_names_are_not_proxied():
    """dunder 是解释器协议钩子（copy/pickle 探 `__getstate__`），接管会把内层协议搬过来。"""
    proxy = KernelProxy(_Inner({}))
    with pytest.raises(AttributeError):
        proxy.__deepcopy__  # noqa: B018


def test_missing_private_name_raises_attribute_error():
    proxy = KernelProxy(_Inner({}))
    with pytest.raises(AttributeError):
        proxy._no_such_private  # noqa: B018
