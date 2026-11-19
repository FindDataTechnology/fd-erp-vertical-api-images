"""封闭错误码目录 + 统一错误对象（语义复用内核 error_codes 封闭集，新码进封闭集）。

规则（沿 fd-erp-ledger-mcp/fd_erp_ledger_mcp/error_codes.py）：
- code 来自封闭集合，新增码必须在 CATALOG 登记；
- 至少四类语义可观测地区分：not_wired / not_found / upstream / invalid_argument；
- 失败响应绝不只给自然语言，一律机器可读 code + 人话 message + hint + detail。

本包新增码（REST 面专属）：
- AUTH_REQUIRED      请求缺租户/客户身份上下文（壹座底座 PAT 在网关侧落地前，
                     测试/演示以身份头承载，见 README）
- PAGESET_FORBIDDEN  租户未授权所请求页面集（fail-closed：不可见即拒绝）
- GRANT_REQUIRED     页面集已授权但缺该端点所需 grant
- STAFF_NOT_AUTHORIZED 财务后台按人授权：调用者不在 ERP_FINANCE_STAFF 名单内
- NOT_IMPLEMENTED    契约端点存在、后端能力未就绪（如实 501，禁止假实现）
"""
from __future__ import annotations

from typing import Any


class ErrorCodeMeta:
    """错误码元数据：语义类 + 可否重试 + 默认修正建议 + HTTP 状态映射。"""

    __slots__ = ("code", "semantic_class", "retryable", "default_hint", "http_status")

    def __init__(self, code: str, semantic_class: str, retryable: bool,
                 default_hint: str, http_status: int):
        self.code = code
        self.semantic_class = semantic_class
        self.retryable = retryable
        self.default_hint = default_hint
        self.http_status = http_status


# 内核复用码（同名同义同 hint 方向，语义与 fd_erp_ledger_mcp.error_codes 对齐）
# + 本包新码。新码必须登记于此 —— 封闭集合，不允许临时造码。
ERROR_CATALOG: dict[str, ErrorCodeMeta] = {
    # ---- 四类基础语义（内核同款）----
    "NOT_WIRED": ErrorCodeMeta(
        "NOT_WIRED", "not_wired", False,
        "该能力未接线/未实现，重试无意义；请申请开通对应能力。", 501),
    "NOT_FOUND": ErrorCodeMeta(
        "NOT_FOUND", "not_found", False,
        "对象不存在，属正常空结果；请核对标识后重试。", 404),
    "UPSTREAM_UNAVAILABLE": ErrorCodeMeta(
        "UPSTREAM_UNAVAILABLE", "upstream", True,
        "上游（数据库/外部系统）暂时不可用，可稍后重试。", 503),
    "INVALID_ARGUMENT": ErrorCodeMeta(
        "INVALID_ARGUMENT", "invalid_argument", False,
        "入参非法，请按提示修正参数后重试。", 400),
    # ---- 内核域复用码 ----
    "TENANT_DENIED": ErrorCodeMeta(
        "TENANT_DENIED", "invalid_argument", False,
        "跨租户访问被拒绝并已留审计痕；请只查询本租户数据。", 403),
    "CREDIT_EXCEEDED": ErrorCodeMeta(
        "CREDIT_EXCEEDED", "invalid_argument", False,
        "信用额度不足；detail 含额度明细（上限/应收余额/已占用/本次请求/可用）。", 422),
    # 与 CREDIT_EXCEEDED 严格区分（2026-11-08 产上首现）：未登记额度是**运维前置
    # 动作缺失**，不是「钱不够」，也不是调用方参数错——此前被兜成
    # INVALID_ARGUMENT「信用额度 limit 非法：None」，误导排查方向。
    "CREDIT_NOT_REGISTERED": ErrorCodeMeta(
        "CREDIT_NOT_REGISTERED", "invalid_argument", False,
        "该客户未在内核登记信用额度，无法受理订单；请运维登记额度（set_credit_limit）后重试。",
        409),
    # ---- 期间关账（change: add-period-close）----
    # 业务拒绝：期间已关账，业务日期落在该期的分录进不去。不是「服务炸了」，重试也无用——
    # 要么先重开期间（带理由），要么在开放期间做冲正。
    "PERIOD_CLOSED": ErrorCodeMeta(
        "PERIOD_CLOSED", "invalid_argument", False,
        "期间已关账，业务日期落在该期间的分录被拒；要改该期数字须先重开期间（带理由），"
        "或在开放期间做冲正。", 409),
    # ---- 内核事件层复用码（change establish-erp-event-kernel）----
    # semantic_class / retryable / default_hint 必须与
    # fd_erp_ledger_mcp.error_codes.CATALOG 逐字一致（check_semantics_contract 的
    # shared 规则按交集比对这三项）。
    # 未登记的后果不是"少一条码"，而是 adapt_exception 里 `code not in ERROR_CATALOG`
    # 把内核语义**塌成 INTERNAL 500**——调用方就无法区分"事件已定型、请走冲正"
    # 与"服务炸了"。
    "EVENT_IMMUTABLE": ErrorCodeMeta(
        "EVENT_IMMUTABLE", "invalid_argument", False,
        "事件已定型或字段不可改写；更正须提交冲正事件，定型前修订须走 revision 通道"
        "（保留旧值、修订来源与时点）。", 409),
    "EVENT_UNKNOWN_TYPE": ErrorCodeMeta(
        "EVENT_UNKNOWN_TYPE", "invalid_argument", False,
        "事件类型未在注册表登记（封闭集）；请先登记类型与载荷 schema，"
        "不要以未定义语义的事实写入。", 400),
    "EVENT_STATE": ErrorCodeMeta(
        "EVENT_STATE", "invalid_argument", False,
        "事件状态不允许该迁移（仅 pending→accepted/rejected、accepted→reversed）。", 409),
    "OBJECT_CONFLICT": ErrorCodeMeta(
        "OBJECT_CONFLICT", "invalid_argument", False,
        "对象余额是投影产物、不可直写；或同一对象维度下出现单位冲突（kg 与 pcs 不可相加）。",
        409),
    "POSTING_RULE_MISSING": ErrorCodeMeta(
        "POSTING_RULE_MISSING", "not_wired", False,
        "该事件类型没有命中的过账规则，故未落账（不猜科目）；detail 点名缺失的规则类型"
        "与所需科目，事件留在待补清单。", 501),
    "WORKFLOW_STATE": ErrorCodeMeta(
        "WORKFLOW_STATE", "invalid_argument", False,
        "编排工作流状态不允许该操作。", 409),
    "MASTER_DATA_STATE": ErrorCodeMeta(
        "MASTER_DATA_STATE", "invalid_argument", False,
        "主数据状态非法（如已停售商品不可下单）。", 409),
    "INTERNAL": ErrorCodeMeta(
        "INTERNAL", "upstream", True,
        "服务内部错误，可重试；若持续请联系运维。", 500),
    # ---- REST 面新码（REST 服务面专属，封闭集登记）----
    "AUTH_REQUIRED": ErrorCodeMeta(
        "AUTH_REQUIRED", "invalid_argument", False,
        "请求缺少身份上下文：请携带租户与客户身份（底座登录态落地后由网关注入）。", 401),
    "PAGESET_FORBIDDEN": ErrorCodeMeta(
        "PAGESET_FORBIDDEN", "invalid_argument", False,
        "当前租户未授权该页面集（fail-closed：未授权即不可见、不可调）。", 403),
    "GRANT_REQUIRED": ErrorCodeMeta(
        "GRANT_REQUIRED", "invalid_argument", False,
        "页面集已授权但当前账号缺该端点所需权限码；请联系管理员开通。", 403),
    "NOT_IMPLEMENTED": ErrorCodeMeta(
        "NOT_IMPLEMENTED", "not_wired", False,
        "契约端点已定义、后端能力未就绪（如实 501，无假实现）；详见 README 未实现清单。", 501),
    "SHARE_EXECUTION_DENIED": ErrorCodeMeta(
        "SHARE_EXECUTION_DENIED", "invalid_argument", False,
        "试图输出资金执行面（红线），拒绝。", 403),
    "DATA_SCOPE_DENIED": ErrorCodeMeta(
        "DATA_SCOPE_DENIED", "invalid_argument", False,
        "越权操作非名下/非本域数据（业务员-客户归属、代理身份越权校验失败）。",
        403),
    "STAFF_NOT_AUTHORIZED": ErrorCodeMeta(
        "STAFF_NOT_AUTHORIZED", "invalid_argument", False,
        "调用者不在财务任职名单内（或名单未配置）——财务后台按人授权，需管理员登记。",
        403),
}

#: 模板层错误码 → HTTP 映射补遗（TemplateError 独有码；语义同 TemplateError.CODES）
_TEMPLATE_CODE_HTTP: dict[str, int] = {
    "MAPPING_INVALID": 400,
    "MANIFEST_INVALID": 400,
    "BOUNDARY_VIOLATION": 500,
}


class ApiError(Exception):
    """REST 面错误：code 取自 ERROR_CATALOG 封闭集 + message + detail。"""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        meta = ERROR_CATALOG.get(code)
        if meta is None:
            raise ValueError(f"ApiError code 不在封闭集合: {code}")
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.hint = meta.default_hint
        self.http_status = meta.http_status
        self.detail = detail or {}

    def to_error(self) -> dict[str, Any]:
        """统一响应壳 error 段：{code, message, hint, detail}。"""
        return {"code": self.code, "message": self.message,
                "hint": self.hint, "detail": self.detail}


def http_status_for(code: str) -> int:
    """任意来源错误码 → HTTP 状态（未知码一律 500，不外溢）。"""
    meta = ERROR_CATALOG.get(code)
    if meta is not None:
        return meta.http_status
    return _TEMPLATE_CODE_HTTP.get(code, 500)


#: 开票/事件层在内核不可达时的挂账车道说明（放这里是因为 app.py 与 faces.py 都要用：
#: 同一个 helper 在两处各写一遍，迟早变成两份文案、两个判据）。
INVOICE_KERNEL_LANE = ("内核事件层（拟稿车道 submit_event + 确认门 decide_event）："
                       "开票申请走 draft 车道落 pending，审核由确认门拍板")


def invoice_degraded(exc: "ApiError") -> "ApiError":
    """内核事件层不可用 → 501 + planned_lane（与对账代理同口径）。

    **必须在 except 分支里调用**：顶层 raise 会被 `scripts/planned_surfaces.py`
    识别成「规划中端点」重新挂回 501 清单，而这条车道其实已实现、只是内核此刻不可达。
    """
    if exc.code in ("NOT_WIRED", "UPSTREAM_UNAVAILABLE"):
        return ApiError("NOT_IMPLEMENTED",
                        f"内核事件层当前不可用（{exc.code}）：{exc.message}",
                        detail={"planned_lane": INVOICE_KERNEL_LANE,
                                "kernel_code": exc.code})
    return exc


def adapt_exception(exc: Exception) -> ApiError:
    """把模板层/内核错误归一为 ApiError（保留 code/message/detail，语义不丢）。

    - 内核 ErpError / 模板 TemplateError：都有 .code/.message（detail 取属性或 {}）；
    - 已是 ApiError：原样返回；
    - 其他异常：包 INTERNAL（不向外泄露堆栈细节）。
    """
    if isinstance(exc, ApiError):
        return exc
    code = getattr(exc, "code", None)
    message = str(getattr(exc, "message", None) or exc)
    detail = getattr(exc, "detail", None)
    if isinstance(code, str) and (code in ERROR_CATALOG
                                  or code in _TEMPLATE_CODE_HTTP):
        out = ApiError(code, message, detail if isinstance(detail, dict) else None)
        # 保留模板/内核侧已定的 hint（若有）
        hint = getattr(exc, "hint", None)
        if isinstance(hint, str) and hint:
            out.hint = hint
        return out
    return ApiError("INTERNAL", f"服务内部错误：{type(exc).__name__}")
