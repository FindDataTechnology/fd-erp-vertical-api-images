"""REST 面请求级结构化日志（establish-erp-independent-root 任务 3.4b）。

为什么要有：REST 面此前只有 Prometheus 计数（route/method/status/code 的**聚合**）。
出现 403 / 404 / 501 / 额度不足时，计数器只能说「有 3 个 403」，回答说不了
「哪条路由、哪次请求、业务码是什么、耗时多少」——而这正是客户报障时要的那一句。
指标答「有没有异常」，日志答「这一单为什么被拒」。

信封与内核同构（见 `fd-erp/shared/log-contract.json`）：`ts` / `event` / `tenant_hash`
一行一个 JSON，写 stdout（容器日志就是采集面，不新增落盘形态、不加日志轮转）。

四条纪律：

1. **只记路由模板，不记原始 URL**。id 进日志既膨胀又泄露，`route_template` 与指标
   共用同一个归一函数，两处口径不会分叉。
2. **租户明文永不落盘**：只留 12 位 sha256（可跨日志关联，不可回读）。被拒的跨租户
   请求记的是**请求方声称的**租户哈希——「有人拿别的租户戳过来」这件事本身要留痕。
3. **业务码与拒绝原因一起记**。`status` 只是 HTTP 层；`code` 才是可归因的那一维
   （封闭集，见 errors.ERROR_CATALOG），`hint` 是同一张表的默认修正建议。
   请求体与 `detail` 整体不落盘（可能含客户 id、金额、单号），只记 `detail` 的键名。
4. **本模块只依赖标准库**。打码逻辑要被 `fd-erp/tools/check_log_redaction.py` 用系统
   python 直接导入跑向量，不能把 prometheus/fastapi 拖进来。

日志量：默认 `errors` 模式只写 status ≥ 400，正常下单不产生日志行。
`ERP_ACCESS_LOG=all` 才逐请求全量（含 `/health` 探针与 `/metrics` 抓取，调试用，别常开）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

_LOG = logging.getLogger("fd_erp_vertical_api.access")

#: 事件名固定，便于按 `event=="http_request"` 一条 grep 捞全量访问日志
ACCESS_EVENT = "http_request"

#: 响应头：客户报障时把这串号给过来，就能定位到恰好那一行日志。
REQUEST_ID_HEADER = "X-Request-Id"

#: 访问日志开关。默认 errors：只有失败才写（正常流量零日志开销）。
#: 注意这**不是** Settings 字段：它是纯观测开关，改它不该动业务配置的形状。
ACCESS_LOG_ENV = "ERP_ACCESS_LOG"
ACCESS_MODES = ("errors", "all", "off")
_DEFAULT_MODE = "errors"
_warned_modes: set[str] = set()

#: 客户端送来的 request-id 只接受安全字符集（防换行注入伪造日志行）
_REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")

# ---- 打码（与内核 observability.redact 同语义，由跨仓闸钉住）----

#: 键名命中即打码（小写、去分隔符后子串匹配）
_SENSITIVE_KEY_HINTS = (
    "password", "passwd", "secret", "token", "credential", "api_key",
    "authorization", "database_url", "dsn", "conninfo", "private_key",
)
#: 值形状像连接串即打码（不依赖键名，防止 DSN 塞进 msg/detail）
_DSN_SHAPE = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s]+", re.IGNORECASE)
_SEP = re.compile(r"[^a-z0-9]+")


def _key_token(key: Any) -> str:
    """键名归一：只留小写字母数字。

    逐字子串匹配会漏掉分隔符变体——线索表写的是 `api_key`，而 `apiKey` /
    `api-key` 都不含 `api_key`，于是明码落盘（内核那边实测漏过）。
    """
    return _SEP.sub("", str(key).lower())


_HINT_TOKENS = tuple(_key_token(h) for h in _SENSITIVE_KEY_HINTS)


def tenant_hash(tenant_id: str | None) -> str | None:
    """租户明文一律不入日志；只留 12 位 sha256 前缀（可关联、不可回读）。"""
    if not tenant_id:
        return None
    return hashlib.sha256(str(tenant_id).encode("utf-8")).hexdigest()[:12]


def _redact_value(value: Any, *, key_hint: str = "") -> Any:
    if isinstance(value, dict):
        return redact(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(v, key_hint=key_hint) for v in value]
    if isinstance(value, str) and _DSN_SHAPE.search(value):
        return "[redacted-dsn]"
    return value


def redact(fields: Any) -> Any:
    """落盘前打码：凭据 / 连接串 / 租户明文。返回新对象，不改调用方对象。

    顶层非 dict（list / 标量）也走叶子打码——旧实现在这里抛 AttributeError，
    等于「打码失败」变成「记日志把请求打死」。
    """
    if not isinstance(fields, dict):
        return _redact_value(fields)
    out: dict[str, Any] = {}
    for key, value in fields.items():
        token = _key_token(key)
        if token == "tenantid":
            out["tenant_hash"] = tenant_hash(str(value))
            continue
        if any(hint in token for hint in _HINT_TOKENS):
            out[key] = "[redacted]"
            continue
        out[key] = _redact_value(value, key_hint=token)
    return out


# ---- 发射器 ----

class _StdoutHandler(logging.Handler):
    """始终写**当前** sys.stdout 的 handler。

    不用 logging.StreamHandler(sys.stdout)：它在构造时把流对象缓存下来。pytest 的
    capsys/capfd 每个用例换一次 sys.stdout，第二个用例起 handler 还拿着上一个用例
    的（可能已关闭的）流——日志就静默丢了（本地实测：第二个用例起全丢，而容器里
    看不出来，因为生产 stdout 不变）。每次 emit 现取，测试与生产同一行为。
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(self.format(record) + "\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 - 写日志失败不得影响业务
            self.handleError(record)


def _ensure_handler() -> None:
    """独立 handler + 独立 logger：不参与 uvicorn 的日志配置，也不冒泡重复输出。"""
    if any(getattr(h, "_erp_json", False) for h in _LOG.handlers):
        return
    handler = _StdoutHandler()
    handler._erp_json = True  # type: ignore[attr-defined]
    _LOG.addHandler(handler)
    _LOG.setLevel(logging.INFO)
    _LOG.propagate = False


def log_event(event: str, *, tenant_id: str | None = None,
              **fields: Any) -> dict[str, Any]:
    """写一行结构化 JSON 日志（stdout）；返回 payload 便于测试断言。"""
    _ensure_handler()
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": event,
    }
    th = tenant_hash(tenant_id)
    if th:
        payload["tenant_hash"] = th
    payload.update(redact(fields))
    _LOG.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return payload


def access_log_mode() -> str:
    """读 `ERP_ACCESS_LOG`；非法值回落默认并只警告一次（每请求刷警告会盖住真信号）。"""
    raw = (os.environ.get(ACCESS_LOG_ENV) or "").strip().lower()
    if not raw:
        return _DEFAULT_MODE
    if raw in ACCESS_MODES:
        return raw
    if raw not in _warned_modes:
        _warned_modes.add(raw)
        log_event("access_log_mode_invalid", configured=raw,
                  allowed=list(ACCESS_MODES), using=_DEFAULT_MODE)
    return _DEFAULT_MODE


def request_id(request) -> str:
    """沿用调用方的 `X-Request-Id`，没有就生成。

    入口网关（paas `erp-proxy.js`）只剥离 `X-ERP-*` 身份头，其它头原样转发、
    上游响应头也原样回传，所以这串号能从浏览器一路穿到本服务再穿回去。
    不合格的值（超长/带换行/怪字符）一律丢弃改用新值——否则日志可被注入假行。
    """
    incoming = (request.headers.get(REQUEST_ID_HEADER) or "").strip()
    if incoming and _REQUEST_ID_OK.match(incoming):
        return incoming
    return uuid.uuid4().hex[:16]


def access_log_middleware(app) -> None:
    """把访问日志挂到每条请求上（**最后注册 = 最外层**）。

    必须在租户闸之外：闸拒掉的 401/403 恰恰是最该看的信号，放内侧就永远记不到。
    这也意味着下游抛出的异常会先被租户闸兜成统一错误壳，本中间件看到的是
    **最终状态码**，而不是异常。
    """
    @app.middleware("http")
    async def _access_log_mw(request, call_next):
        from .observability import route_template  # 延迟导入：本模块保持纯 stdlib

        rid = request_id(request)
        request.state.request_id = rid
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            # 走到这里说明异常发生在闸之外——如实记 500 再上抛，不吞业务异常
            _emit(request, rid, 500, time.perf_counter() - started, route_template,
                  error_code="INTERNAL")
            raise
        dur = time.perf_counter() - started
        _emit(request, rid, status, dur, route_template)
        try:
            response.headers[REQUEST_ID_HEADER] = rid
        except Exception:  # noqa: BLE001 - 头写失败不该改变响应
            pass
        return response


def _emit(request, rid: str, status: int, dur: float, route_template,
          error_code: str | None = None) -> None:
    """按模式判定是否落一行；任何异常都吞掉——观测绝不能影响业务响应。"""
    try:
        mode = access_log_mode()
        if mode == "off":
            return
        state = getattr(request, "state", None)
        code = error_code or getattr(state, "error_code", None)
        if mode == "errors" and status < 400 and not code:
            return

        path = request.url.path
        fields: dict[str, Any] = {
            "request_id": rid,
            "method": request.method,
            "route": route_template(path),
            "status": status,
            "dur_ms": round(max(dur, 0.0) * 1000, 3),
        }
        if code:
            fields["code"] = code
        hint = getattr(state, "error_hint", None)
        if hint:
            fields["hint"] = hint
        detail_keys = getattr(state, "error_detail_keys", None)
        if detail_keys:
            fields["detail_keys"] = list(detail_keys)
        grant = getattr(state, "erp_grant", None)
        if grant:
            fields["grant"] = grant

        # 租户：跨租户被拒时记「请求方声称的那个租户」的哈希——这类请求本身要留痕，
        # 但明文一律不落盘。没带头的请求（探针等）记本进程绑定租户，便于按租户聚合。
        claimed = (request.headers.get("X-ERP-Tenant") or "").strip()
        bound = getattr(getattr(request.app.state, "settings", None), "tenant_id", None)
        log_event(ACCESS_EVENT, tenant_id=claimed or bound, **fields)
    except Exception:  # noqa: BLE001 - 日志失败不得影响响应
        pass


__all__ = [
    "ACCESS_EVENT", "ACCESS_LOG_ENV", "ACCESS_MODES", "REQUEST_ID_HEADER",
    "access_log_middleware", "access_log_mode", "log_event", "redact",
    "request_id", "tenant_hash",
]
