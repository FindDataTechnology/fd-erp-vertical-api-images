"""KernelClient 装配：生产走 MCP 工具面，同进程走 InProcess 同形客户端。

- InProcessKernelClient：与 fd-erp-vertical-templates/tests/conftest.py 的同形
  客户端同构 —— 委托 fd_erp_ledger_mcp 内核模块函数（惰性导入，签名同形）。
  服务进程内直连内核模块是模板测试同构形态；生产形态 ERP_KERNEL_TRANSPORT=mcp
  时改用 templates.kernel_client.McpKernelClient（fastmcp 工具面）。
- 两种形态对模板层都只暴露 KernelClient 协议，不给 SQL/DB —— 零旁路纪律不变形。

fail-closed：内核侧信用/计费工具未上线时如实报 NOT_WIRED（不伪造成功）。
对账两个方法（run_reconciliation / get_recon_report）是**长任务代理**：提交立刻拿
task_id，报告靠轮询；内核不可达时抛 UPSTREAM_UNAVAILABLE，由 REST 面按 spec 降级
成 501 + planned_lane（不用 500、不伪造结果）。
"""
from __future__ import annotations

from typing import Any

from .config import Settings
from .errors import ERROR_CATALOG, ApiError


class InProcessKernelClient:
    """内核工具面同进程实现（fd_erp_ledger_mcp 模块委托，签名与协议同形）。

    内核惰性导入：模块导入期不触碰任何 DB 依赖；仅调用时刻解析内核包。
    """

    def __init__(self, settings: Settings):
        self._s = settings
        self._tenant = settings.tenant_id

    # ---- 内核模块惰性解析 ----

    def _kernel_mod(self, name: str):
        """惰性解析内核子模块：导入期零 DB 依赖，仅调用时刻触碰内核包。"""
        try:
            import importlib
            return importlib.import_module(f"fd_erp_ledger_mcp.{name}")
        except ImportError as exc:
            raise ApiError("NOT_WIRED",
                           "内核包 fd_erp_ledger_mcp 不可用（InProcess 形态需可导入内核）",
                           detail={"module": name}) from exc

    def _connection(self):
        db = self._kernel_mod("db")
        return db.connection(self._s)

    # ---- 8 核心工具（同形签名）----

    def submit_proposal(self, stable_id, lines, source_ref=None, kind="journal",
                        reversal_of=None, lane_fleet=None, lane_post=None,
                        lane_title=None, lane_target=None, tenant_id=None,
                        dimensions=None, submitted_by="agent", booked_on=None):
        proposals = self._kernel_mod("proposals")
        with self._connection() as conn:
            return proposals.submit_proposal(
                conn, settings=self._s, tenant_id=self._tenant,
                stable_id=stable_id, lines=lines, source_ref=source_ref,
                kind=kind, reversal_of=reversal_of, lane_fleet=lane_fleet,
                lane_post=lane_post, lane_title=lane_title,
                lane_target=lane_target, dimensions=dimensions,
                submitted_by=submitted_by, booked_on=booked_on)

    def list_proposals(self, status=None, tenant_id=None, offset=0, limit=100):
        proposals = self._kernel_mod("proposals")
        with self._connection() as conn:
            rows = proposals.list_proposals(conn, self._tenant, status)
            # **信封必须与真生产者（MCP 工具面）逐键同形**（2026-11-15 事故的第三层）。
            # 内核 `result.paginate()` 给的是 `{data,total,offset,limit,rows_returned,
            # truncated}`，而本替身原先返回 `{"proposals": …}` —— 形状不同的替身会让
            # 「api 按错的键名读」在所有单测里都绿，只有打到真内核才 500。改信封为目标
            # 契约，任何再按 `proposals` 读的代码会当场在单测里红。
            window = rows[offset:offset + limit] if limit else rows[offset:]
            return {"ok": True, "data": window, "total": len(rows),
                    "offset": offset, "limit": limit,
                    "rows_returned": len(window),
                    "truncated": offset + len(window) < len(rows)}

    def confirm_proposal(self, proposal_id=None, stable_id=None, tenant_id=None,
                         confirmed_by="human"):
        proposals = self._kernel_mod("proposals")
        with self._connection() as conn:
            return proposals.confirm_proposal(
                conn, settings=self._s, tenant_id=self._tenant,
                proposal_id=proposal_id, stable_id=stable_id,
                confirmed_by=confirmed_by)

    def reject_proposal(self, proposal_id=None, stable_id=None, reason=None,
                        tenant_id=None, rejected_by="human"):
        proposals = self._kernel_mod("proposals")
        with self._connection() as conn:
            return proposals.reject_proposal(
                conn, tenant_id=self._tenant, proposal_id=proposal_id,
                stable_id=stable_id, reason=reason, rejected_by=rejected_by)

    def query_balance(self, account_code=None, tenant_id=None):
        ledger = self._kernel_mod("ledger")
        with self._connection() as conn:
            data = ledger.compute_balance(conn, self._tenant, account_code,
                                          settings=self._s)
            return {"data": data, "rows_returned": len(data)}

    def query_entries(self, account_code=None, txn_id=None, tenant_id=None,
                      dimensions=None, offset=0, limit=100):
        ledger = self._kernel_mod("ledger")
        with self._connection() as conn:
            rows = ledger.query_entries(conn, self._tenant,
                                        account_code=account_code, txn_id=txn_id,
                                        dimensions=dimensions)
            return {"entries": rows[offset:offset + limit],
                    "rows_returned": len(rows)}

    # ---- 对账长任务（design D13：提交立即返回 task_id，报告靠轮询）----
    #
    # 通道结论（任务 6.6）：内核对这个工具的设计本来就是「立即返回 task_id +
    # get_recon_report 轮询」（fd_erp_ledger_mcp/recon/engine.py：写 queued 行后启动
    # daemon 线程，dt 内完成）。也就是说通道本身能承载长任务——承载方式是任务化，
    # 不是同步等待。因此这里**原样代理**：REST 面不做「等服务端跑完再回」的同步
    # 包装（那等于把内核已解决的长任务超时问题搬到 REST 面，design D13 已否掉）。
    #
    # 错误归一（404 / 501 降级）不在本类做：MCP 形态返回的是错误信封、InProcess 形态
    # 抛 ErpError，两形态分叉会养出「测试绿、产上 500」的问题（本仓已被同类分叉咬过
    # 两次）。统一收口见模块级 KernelProxy。

    def _recon_tenant(self, tenant_id: str | None) -> str:
        """查询闸同内核工具面：跨租户请求拒绝并留痕（tenancy.resolve_tenant）。"""
        tenancy = self._kernel_mod("tenancy")
        with self._connection() as conn:
            return tenancy.resolve_tenant(conn, self._s, tenant_id or self._tenant)

    def run_reconciliation(self, left_source, right_source, retry_task_id=None,
                           tenant_id=None):
        """提交两源对账（只读滩头）：返回 {"task_id", "status": "queued", ...}。

        内核在后台线程跑，落库态可轮询（服务重启后仍可查）；幂等/重试语义由内核
        retry_task_id 承担（仅失败条目重试）。
        """
        engine = self._kernel_mod("recon.engine")
        tenant = self._recon_tenant(tenant_id)
        return engine.enqueue(self._s, tenant, left_source, right_source, retry_task_id)

    def get_recon_report(self, task_id, tenant_id=None):
        """按 task_id 查状态/进度/差异（四类差异 + 双侧引用），任务不存在 404。"""
        engine = self._kernel_mod("recon.engine")
        tenant = self._recon_tenant(tenant_id)
        return engine.get_report(self._s, tenant, task_id)

    # ---- 期间关账（add-period-close）----
    #
    # 关账是内核概念（期间台账、落账闸都在内核库里），模板层不另包一层——那层只会是
    # 纯转发。本类走内核模块直调（同进程形态）。
    #
    # 2026-11-15 订正：这里原写着「生产 MCP 形态尚未开这几个工具」，**那是错的**——
    # 内核工具面 33-37 早就全在（`close_period`/`reopen_period`/`period_status`/
    # `period_report`/`list_periods`），只是模板层的 `McpKernelClient` 一个都没转发，
    # 于是 MCP 形态下 `/fi/periods` 回 `501 NOT_WIRED`。同进程形态一直是好的，
    # 所以单测全绿、只有真机现形。已在 templates 补齐五个转发方法（见
    # `tests/test_kernel_client_period_tools.py`）；`KernelProxy` 的缺位兜底仍保留
    # （接更旧的内核镜像时如实报未接线，而不是伪造成功）。

    def _periods_mod(self):
        return self._kernel_mod("periods")

    def close_period(self, period, actor, note=None):
        mod = self._periods_mod()
        with self._connection() as conn:
            return mod.close(conn, settings=self._s, tenant_id=self._tenant,
                             period=period, actor=actor, note=note)

    def reopen_period(self, period, actor, reason):
        mod = self._periods_mod()
        with self._connection() as conn:
            return mod.reopen(conn, settings=self._s, tenant_id=self._tenant,
                              period=period, actor=actor, reason=reason)

    def period_status(self, period):
        mod = self._periods_mod()
        with self._connection() as conn:
            return mod.status(conn, self._tenant, period)

    def period_report(self, period):
        mod = self._periods_mod()
        with self._connection() as conn:
            return mod.report(conn, settings=self._s, tenant_id=self._tenant,
                              period=period)

    def list_periods(self):
        mod = self._periods_mod()
        with self._connection() as conn:
            return mod.list_periods(conn, self._tenant)

    # ---- 信用额度扩展（占用/释放/回冲由账本记账）----

    def _credit_mod(self):
        try:
            import fd_erp_ledger_mcp.credit as credit  # 内核侧信用记账模块
        except ImportError as exc:
            raise ApiError("NOT_WIRED",
                           "内核信用工具未上线（fail-closed，不伪造成功）") from exc
        return credit

    def reserve_credit(self, customer_id, stable_id, amount, currency,
                       source_ref=None, expires_at=None):
        mod = self._credit_mod()
        with self._connection() as conn:
            return mod.reserve_credit(
                conn, settings=self._s, tenant_id=self._tenant,
                customer_id=customer_id, stable_id=stable_id, amount=amount,
                currency=currency, source_ref=source_ref, expires_at=expires_at)

    def release_credit(self, customer_id, stable_id, released_by="system", reason=None):
        mod = self._credit_mod()
        with self._connection() as conn:
            return mod.release_credit(conn, settings=self._s, tenant_id=self._tenant,
                                      customer_id=customer_id, stable_id=stable_id,
                                      released_by=released_by, reason=reason)

    def query_credit(self, customer_id):
        mod = self._credit_mod()
        with self._connection() as conn:
            # MCP 工具面 query_credit = credit.credit_position（同形语义）
            return mod.credit_position(conn, settings=self._s,
                                       tenant_id=self._tenant,
                                       customer_id=customer_id)

    # ---- 事件层扩展（establish-erp-event-kernel 7.1）----
    #
    # 与模板层的 KernelClient 协议同名同形：到货预告这类外部原始事实经这里进内核，
    # 数量面与分录都由内核派生——REST 面不直连库表、不算金额、不猜科目。

    def _events_mod(self):
        try:
            import fd_erp_ledger_mcp.events as events
        except ImportError as exc:
            raise ApiError("NOT_WIRED",
                           "内核事件工具未上线（fail-closed，不伪造事实）",
                           detail={"module": "fd_erp_ledger_mcp.events"}) from exc
        return events

    def ingest_event(self, event_type, stable_id, occurred_at, payload,
                     source_ref=None, provenance=None):
        mod = self._events_mod()
        with self._connection() as conn:
            return mod.append_event(
                conn, settings=self._s, tenant_id=self._tenant,
                event_type=event_type, stable_id=stable_id,
                occurred_at=occurred_at, payload=payload, lane="ingest",
                source_ref=source_ref, provenance=provenance)

    # ---- 拟稿车道与事件查询（add-invoice-request-lane）----
    #
    # ingest 是「外部系统报来的事实，写入即接受」；submit 是「我方替调用方拟的稿，
    # 落 pending 等确认门拍板」。开票申请必须走后者：用 ingest 等于自动通过审核。

    def submit_event(self, event_type, stable_id, occurred_at, payload,
                     source_ref=None):
        mod = self._events_mod()
        with self._connection() as conn:
            return mod.append_event(
                conn, settings=self._s, tenant_id=self._tenant,
                event_type=event_type, stable_id=stable_id,
                occurred_at=occurred_at, payload=payload, lane="draft",
                source_ref=source_ref, actor="agent")

    def query_events(self, event_type=None, lane=None, status=None,
                     limit=100, after_event_id=0):
        mod = self._events_mod()
        with self._connection() as conn:
            return mod.query_events(
                conn, tenant_id=self._tenant, event_type=event_type, lane=lane,
                status=status, limit=limit, after_event_id=after_event_id)

    def decide_event(self, stable_id, decision, note=None, lane="draft",
                     actor="human"):
        """确认门：draft 车道事件 accepted / rejected（唯一改变事件状态的人工入口）。

        **只由 API 层的拍板端点调用**：模板编排层不代任何一方过门
        （`shared/kernel-client-contract.json` 对模板仓的 note 原文）。
        """
        mod = self._events_mod()
        with self._connection() as conn:
            return mod.decide_event(
                conn, tenant_id=self._tenant, stable_id=stable_id, lane=lane,
                decision=decision, note=note or "", actor=actor)

    def project_event(self, event_id, object_type, object_key, quantity_delta,
                      unit, dimension=None):
        mod = self._events_mod()
        with self._connection() as conn:
            return mod.project_object_event(
                conn, tenant_id=self._tenant, event_id=event_id,
                object_type=object_type, object_key=object_key,
                quantity_delta=quantity_delta, unit=unit, dimension=dimension)

    def query_object_balance(self, object_type, object_key, dimension=None):
        mod = self._events_mod()
        with self._connection() as conn:
            return mod.query_object_balance(
                conn, tenant_id=self._tenant, object_type=object_type,
                object_key=object_key, dimension=dimension)

    def post_event(self, event_id):
        try:
            import fd_erp_ledger_mcp.posting as posting
        except ImportError as exc:
            raise ApiError("NOT_WIRED",
                           "内核过账工具未上线（fail-closed，不伪造分录）",
                           detail={"module": "fd_erp_ledger_mcp.posting"}) from exc
        with self._connection() as conn:
            return posting.post_event(conn, settings=self._s,
                                      tenant_id=self._tenant, event_id=event_id)

    # ---- 计费（change: add-carrier-freight-dimension 4.1）----
    #
    # 运费单归集读的是内核费用行（费用行 `refs` = 计费时传入的 document_refs，
    # 承运商维度就落在那里）。内核这三个工具早已在 MCP 工具面上，但 InProcess
    # 形态此前没桥接——不补这一层，运费单车道在进程内直接 AttributeError。

    def _billing_mod(self):
        try:
            import fd_erp_ledger_mcp.billing as billing
        except ImportError as exc:
            raise ApiError("NOT_WIRED",
                           "内核计费工具未上线（fail-closed，不编运费单）",
                           detail={"module": "fd_erp_ledger_mcp.billing"}) from exc
        return billing

    def bill_event(self, event, source_ref=None):
        mod = self._billing_mod()
        with self._connection() as conn:
            return mod.bill_event(conn, settings=self._s, tenant_id=self._tenant,
                                  event=event, source_ref=source_ref)

    def list_fees(self, offset=0, limit=100):
        """与 MCP 工具面同形：`paginate(...)`（data/total/truncated）。"""
        mod = self._billing_mod()
        from fd_erp_ledger_mcp.result import paginate
        with self._connection() as conn:
            rows = mod.list_fees(conn, self._tenant)
            return paginate(rows, offset=offset, limit=limit)

    def get_fee_detail(self, fee_id):
        mod = self._billing_mod()
        with self._connection() as conn:
            return mod.get_fee_detail(conn, settings=self._s,
                                      tenant_id=self._tenant, fee_id=fee_id)


def _is_transport_failure(exc: Exception) -> bool:
    """传输层失败（内核包缺失 / DB 连接不可达 / MCP 连接异常），与业务错误区分。"""
    if isinstance(exc, (ImportError, ConnectionError, TimeoutError)):
        return True
    mod = (type(exc).__module__ or "").split(".")[0]
    if mod.startswith(("psycopg", "psycopg2")):
        return type(exc).__name__ in ("OperationalError", "InterfaceError")
    # MCP 形态：fastmcp/httpx 的连接类异常（内核进程没起、URL 打不通、超时）
    if mod in ("httpx", "httpcore", "anyio", "fastmcp", "mcp", "sse", "h11"):
        return True
    if "connect" in type(exc).__name__.lower() or "timeout" in type(exc).__name__.lower():
        return True
    # fastmcp 的连接失败被包成 builtins.RuntimeError("Client failed to connect: …")，
    # 类型上完全看不出来（实测：内核进程停掉后就是这个形态）；只能按消息判。
    msg = str(exc).lower()
    return any(sig in msg for sig in (
        "failed to connect", "connection refused", "connection attempts failed",
        "connection error", "connection reset", "timed out", "timeout"))


def _raise_kernel_error(exc: Exception, action: str):
    """内核异常 → REST 面封闭错误码（本函数必定抛出）。

    - RECON_TASK_NOT_FOUND（内核专属码）→ NOT_FOUND 404，不落成 500；
    - 其余封闭集码原样透出（NOT_WIRED / INVALID_ARGUMENT / TENANT_DENIED …）；
    - 传输层失败 → UPSTREAM_UNAVAILABLE（REST 层按 spec 降级为 501 + planned_lane）；
    - 其它未知异常原样抛（→ INTERNAL 500），绝不当成「内核不可用」掩盖缺陷。
    """
    if isinstance(exc, ApiError):
        raise exc
    code = getattr(exc, "code", None)
    message = str(getattr(exc, "message", None) or exc)
    detail = getattr(exc, "detail", None)
    detail = detail if isinstance(detail, dict) else None
    if code == "RECON_TASK_NOT_FOUND":
        raise ApiError("NOT_FOUND", message, detail=detail) from exc
    if isinstance(code, str) and code in ERROR_CATALOG:
        raise ApiError(code, message, detail=detail) from exc
    if _is_transport_failure(exc):
        raise ApiError("UPSTREAM_UNAVAILABLE",
                       f"内核不可达（{type(exc).__name__}）",
                       detail={"kernel": "erp-ledger", "action": action}) from exc
    raise exc


def proposal_rows(out: Any) -> list:
    """从内核 `list_proposals` 的信封里取行。

    **真生产者的键是 `data`**（内核 `result.paginate()` 的契约：`{data,total,offset,
    limit,rows_returned,truncated}`），不是 `proposals`。2026-11-15 实跑才暴露：api 原先
    按 `["proposals"]` 硬取 ⇒ `KeyError` → 500；`statements.py` 用 `.get("proposals")`
    则长期**静默空**（对账单状态永远解析不到，看起来像「没有提案」）。两者在用
    `InProcessKernelClient` 的单测里都绿，因为替身自己造了 `{"proposals": …}` —— 替身与
    真生产者形状不同，是最容易长期藏住的一类。

    这里同时接受 `proposals`：滚动期间旧内核镜像（若有）仍在跑时不至于 500。
    """
    if not isinstance(out, dict):
        return []
    rows = out.get("data")
    if rows is None:
        rows = out.get("proposals")
    return list(rows or [])


def _kernel_result(out: Any, action: str) -> Any:
    """MCP 工具面失败是「错误信封」而非异常：统一翻成 ApiError（否则会当数据返回）。"""
    if isinstance(out, dict) and out.get("ok") is False:
        err = out.get("error") or {}
        code = str(err.get("code") or "INTERNAL")
        message = str(err.get("message") or f"内核工具报错（{action}）")
        detail = err.get("detail") if isinstance(err.get("detail"), dict) else {}
        if code == "RECON_TASK_NOT_FOUND":
            raise ApiError("NOT_FOUND", message, detail=detail)
        if code in ERROR_CATALOG:
            raise ApiError(code, message, detail=detail)
        raise ApiError("UPSTREAM_UNAVAILABLE",
                       f"内核对账工具返回未登记错误码：{code}",
                       detail={**detail, "kernel_code": code, "action": action})
    return out


class KernelProxy:
    """内核调用在**两种 transport** 上的统一收口（一层，不各写一遍）。

    覆盖：对账两方法（run_reconciliation / get_recon_report）与事件层两方法
    （submit_event / query_events，add-invoice-request-lane）。

    为什么必须有：InProcess 形态委托内核模块 → 失败**抛** ErpError；MCP 形态经
    fastmcp 调工具面 → 内核 `@safe_tool` 把失败包成 `{"ok": false, "error": …}`
    **返回**。只在一侧做归一，另一侧就会把错误信封当成功数据吐给客户，或把
    「任务不存在」变成 200 / 把「内核不可达」变成 500 —— 产上实测踩到过（见
    docs/optimize-erp-solution 6.6 记录）。故两种形态都过这层。

    未覆盖的方法原样透传（信用/计费/账本工具面行为不变）。
    """

    def __init__(self, inner):
        self._inner = inner

    def run_reconciliation(self, left_source, right_source, retry_task_id=None,
                           tenant_id=None):
        try:
            out = self._inner.run_reconciliation(
                left_source, right_source, retry_task_id=retry_task_id,
                tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001 见 _raise_kernel_error
            _raise_kernel_error(exc, "run_reconciliation")
        return _kernel_result(out, "run_reconciliation")

    def get_recon_report(self, task_id, tenant_id=None):
        try:
            out = self._inner.get_recon_report(task_id, tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001 见 _raise_kernel_error
            _raise_kernel_error(exc, "get_recon_report")
        return _kernel_result(out, "get_recon_report")

    # ---- 事件层两方法（add-invoice-request-lane）----
    #
    # 这两条同样必须收口：MCP 形态下 `submit_event` 失败会以错误信封**返回**，
    # 不收口就会把「内核拒收」当成功（开票申请回 pending 却什么也没写），
    # 而 `query_events` 失败会被读成空列表（「这个客户没有申请」）——正是
    # spec 明令禁止的两种假象。

    def submit_event(self, event_type, stable_id, occurred_at, payload,
                     source_ref=None):
        try:
            out = self._inner.submit_event(
                event_type, stable_id, occurred_at, payload,
                source_ref=source_ref)
        except Exception as exc:  # noqa: BLE001 见 _raise_kernel_error
            _raise_kernel_error(exc, "submit_event")
        return _kernel_result(out, "submit_event")

    def query_events(self, event_type=None, lane=None, status=None,
                     limit=100, after_event_id=0):
        try:
            out = self._inner.query_events(
                event_type=event_type, lane=lane, status=status, limit=limit,
                after_event_id=after_event_id)
        except Exception as exc:  # noqa: BLE001 见 _raise_kernel_error
            _raise_kernel_error(exc, "query_events")
        return _kernel_result(out, "query_events")

    def decide_event(self, stable_id, decision, note=None, lane="draft",
                     actor="human"):
        """拍板：同样必须收口——MCP 形态下内核拒绝会以错误信封**返回**。

        **内核 MCP 工具的确认门签名里没有 `lane`/`actor`**（`tools.py`：
        `decide_event(stable_id, decision, note="")`，内部硬编码
        `lane="draft", actor="human"`；2026-11-15 实读）。所以这里不再把这两个
        参数传下去：传下去在 MCP 形态是 `unexpected_keyword_argument` → **500**
        （生产实历：`/se/invoice-requests/{id}/decide` 每单必 500），在同进程
        形态却是「收下了」——同一次调用两种传输给出相反结局，正是本轮要消灭的
        分叉。

        但也不能静默丢弃：调用方要求别的车道/经办时**显式拒绝**。内核工具面按
        「人工确认」留痕，这里收下一个 `actor="cfo-001"` 再悄悄记成 human，
        就是往审计台账里写假话。
        """
        if lane != "draft" or actor != "human":
            raise ApiError(
                "INVALID_ARGUMENT",
                "确认门只接受 lane=draft / actor=human"
                "（内核 MCP 工具按人工拍板留痕，不接受自报身份）",
                detail={"lane": lane, "actor": actor})
        try:
            out = self._inner.decide_event(stable_id, decision, note=note)
        except Exception as exc:  # noqa: BLE001 见 _raise_kernel_error
            _raise_kernel_error(exc, "decide_event")
        return _kernel_result(out, "decide_event")

    # ---- 期间关账（add-period-close）：同样收口 ----
    #
    # 失败语义两条必须分清：期间已关账（PERIOD_CLOSED）是**业务拒绝**，让人先重开或改冲正；
    # 「传输形态没有这个方法」是**未接线**（NOT_WIRED）。混成一个码，调用方会把
    # 「内核还没开这个工具」当成「期间关了」，然后白等或白重试。

    def _period_call(self, name, action, *args, **kwargs):
        fn = getattr(self._inner, name, None)
        if fn is None:
            raise ApiError("NOT_WIRED",
                           f"当前传输形态未开期间关账工具（{name}）",
                           detail={"action": action, "method": name})
        try:
            out = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 见 _raise_kernel_error
            _raise_kernel_error(exc, action)
        return _kernel_result(out, action)

    def close_period(self, period, actor, note=None):
        return self._period_call("close_period", "close_period",
                                 period, actor, note=note)

    def reopen_period(self, period, actor, reason):
        return self._period_call("reopen_period", "reopen_period",
                                 period, actor, reason)

    def period_status(self, period):
        return self._period_call("period_status", "period_status", period)

    def period_report(self, period):
        return self._period_call("period_report", "period_report", period)

    def list_periods(self):
        return self._period_call("list_periods", "list_periods")

    def __getattr__(self, name):
        """其余工具面方法：**仍然归一**，不裸透传。

        2026-11-15 生产实测的两条假象都出自「裸透传 `getattr(self._inner, name)`」：

        1. `confirm_proposal(proposal_id=999999999)` 的返回值原样是内核错误信封
           `{"ok": false, "error": {"code": "NOT_FOUND", …}}`——`proposal_rows()`
           读真键 `data` 取不到，返回 `[]` ⇒ 页面显示「没有提案」（静默空）。
        2. `query_credit("未登记客户")` 同样返回信封，消费点取 `available`/`limit`
           得到 None ⇒ 空额度或 500。

        这正是本类存在的理由（见类 docstring：「不收口就会把『内核拒收』当成功」），
        而裸透传把这层保护留给了「有没有人记得手写覆盖」——手写了 10 个，剩下 6 个
        真被调用的方法（list_proposals / query_credit / submit_proposal /
        reject_proposal / query_object_balance / confirm_proposal）一直在裸奔。

        现在改成**结构式**：`__getattr__` 返回的每个可调用对象都过同一对
        `_raise_kernel_error` + `_kernel_result`。手写覆盖的方法保持原样（语义相同，
        只是各自带了更具体的 action 名），而**新增工具天然被覆盖**——不需要谁记得。

        下划线开头的处理分两种，理由不同：

        - `_inner` 与 dunder **不接管**：`__init__` 里 `_inner` 尚未赋值时若接管会无限
          递归；dunder 则被解释器当协议钩子（`copy`/`pickle` 探 `__getstate__`、
          `__deepcopy__`），接管等于把内层的协议行为搬到代理身上。
        - 其余单下划线私有属性**照常镜像内层**（`proxy._s` / `proxy._tenant`）：代理
          的契约就是「与内层同形」，测试种子数据、运维脚本都在用这条。挡掉它只会让
          每个新写的用例都得记得穿一层 `._inner`，而那个约定没人会记得。
        """
        if name == "_inner" or name.startswith("__"):
            raise AttributeError(name)
        attr = getattr(object.__getattribute__(self, "_inner"), name)
        if not callable(attr):
            return attr

        def _normalized(*args, **kwargs):
            try:
                out = attr(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 见 _raise_kernel_error
                _raise_kernel_error(exc, name)
            return _kernel_result(out, name)

        return _normalized


def ensure_kernel_proxy(client):
    """幂等包装：已经是 KernelProxy 就原样返回。"""
    return client if isinstance(client, KernelProxy) else KernelProxy(client)


def build_kernel_client(settings: Settings):
    """按 Settings.kernel_transport 装配 KernelClient（生产 MCP / 同进程 InProcess）。"""
    if settings.kernel_transport == "mcp":
        if not settings.kernel_url:
            raise ApiError("INVALID_ARGUMENT",
                           "ERP_KERNEL_TRANSPORT=mcp 需设置 ERP_KERNEL_URL")
        from fd_erp_vertical_templates.kernel_client import McpKernelClient
        # 内核可在数据面 `/mcp` 上挂 bearer 门（`MCP_BEARER_TOKEN` 非空即开）。2026-11-15
        # 事故：门开了而本进程没配 `ERP_KERNEL_TOKEN` ⇒ 每个碰账本的端点 401 被统一壳
        # 兜成 `500 INTERNAL: MCPError`，而 `/health`、`/pagesets` 照常 200 ⇒「服务健康」
        # 与「取不到账本数据」同时成立，静默约 38 小时。这里把钥匙透传下去；为空时
        # McpKernelClient 一个头都不带（内核没开门的环境行为不变）。
        return ensure_kernel_proxy(
            McpKernelClient(settings.kernel_url, settings.tenant_id,
                            token=getattr(settings, "kernel_token", None)))
    return ensure_kernel_proxy(InProcessKernelClient(settings))
