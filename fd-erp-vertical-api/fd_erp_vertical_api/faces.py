"""SE 业务员台（15）/ SP 供应商（13）/ CP 承运商（12）三页面集 REST 面。

契约真源：fd-erp-vertical-templates/templates/supply-chain/pagesets/
{se-sales,sp-supplier,cp-carrier}/api-contract.md（同源纪律：全部端点与 MCP
工具面同源数据，本模块零直连取数 —— 业务读写一律经模板层驱动 + KernelClient）。

闸序（同 UE：app.ue_context 先例）：
1. 租户闸（全局中间件）；
2. 页面集闸：supply-chain.se / .sp / .cp 须在租户授权配置内（fail-closed 403）；
3. grant 闸：端点所需权限码（契约权限列）须已授权（403）；工作台端点按契约
   仅要求「页面集授权」（无独立 grant）；
4. 身份/数据域闸（网关 PAT 落地前的演示形态，沿 X-ERP-* 头形态）：
   - SE  X-ERP-Staff   业务员工号；归属关系 = 客户主数据 payload.sales_rep
     （主数据关系）；代客下单越权 403 DATA_SCOPE_DENIED；读面越域 404 不泄露；
   - SP  X-ERP-Supplier 供应商档案（实体 id 或编码），数据域 = 本租户推送给
     本供应商档案的采购单及其回流；越域 404 不泄露存在性；
   - CP  X-ERP-Carrier  承运商标识；运单域 = carrier 字段命中的在途运单
     （承运商档案不在主数据三档，如实以运单留痕为域）。

数据域与车道映射（如实，不伪造）：
- SE 代客下单复用 orchestration.accept_order（同一取价/信用链路），业务员身份
  以 requested_by 留痕于订单事件档（= 服务端日志，JSONL append-only）；
- SE 预警处置走 reports.ThresholdEvaluator 处置任务确认车道（与 owner 席位
  同一确认门实现，拒绝留痕不落账）；
- SP 接单/发货反馈写采购编排同一状态机（同 ref 事件档）；发货反馈自动生成
  到货预告（record_arrival_notice）；
- CP 承运回执/月台/提货/POD 写交付编排运单同一事件档；异常签收进处置车道；
- 契约有、编排层无车道的端点如实 501 NOT_IMPLEMENTED + planned_lane（沿 UE
  既有 5 个 501 先例）：CRM 线索/拜访、对账单推送（SE）、SP 对账/发票/结算、
  CP 可约月台时段/运费单。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from fastapi import Depends, Request

from .drivers import Drivers, stock_on_hand
from .envelope import list_shell, ok_shell, parse_page_params
from .errors import ApiError, invoice_degraded
from .errors import ApiError
from .errors import adapt_exception
from .kernel_client import proposal_rows
from .pagesets import (PROGRAM_IDENTITIES, FinanceStaff, PagesetAuth,
                       load_finance_staff, require_finance_staff,
                       require_grant)

from fd_erp_vertical_templates.errors import TemplateError

PAGESET_SE = "supply-chain.se"
PAGESET_SP = "supply-chain.sp"
PAGESET_CP = "supply-chain.cp"
#: 内部页集（只在服务端守门，不进 /pagesets 载荷）
PAGESET_FI = "supply-chain.fi"
#: 仓管作业的内部页集（同上，不进载荷）——change: add-inventory-operations-lane 任务 3.1
PAGESET_WH = "supply-chain.wh"

STAFF_HEADER = "X-ERP-Staff"
SUPPLIER_HEADER = "X-ERP-Supplier"
CARRIER_HEADER = "X-ERP-Carrier"

ORDER_STATUS_NOTE = ("pending=待确认（确认车道）；confirmed=已确认移交履约；"
                     "cancelled=已取消（信用占用经内核回冲）")


def _not_implemented(message: str, planned_lane: str) -> ApiError:
    return ApiError("NOT_IMPLEMENTED", message,
                    detail={"planned_lane": planned_lane})


# --------------------------------------------------------------------------
# 共用 read 面辅助（编排层事件档同源回放；app.py _owned_order 同款先例）
# --------------------------------------------------------------------------

def _order_states(d: Drivers, customer_ids: set[str]) -> list[dict[str, Any]]:
    """订单当前态（proposed→confirmed/cancelled 合并），限客户集合（数据域）。"""
    seen: dict[str, dict[str, Any]] = {}
    for ev in d.orders._events():  # 编排层事件档（同源 read 面）
        if ev.get("record") != "order":
            continue
        ref = ev.get("ref")
        payload = ev.get("payload") or {}
        if ev["event"] == "proposed":
            if str(payload.get("customer_id") or "") in customer_ids:
                seen[ref] = {"ref": ref, "event": "proposed",
                             "status": ev.get("status"), "payload": dict(payload),
                             "requested_by": ev.get("requested_by")}
        elif ref in seen and ev["event"] in ("confirmed", "cancelled"):
            merged = dict(seen[ref]["payload"])
            merged.update(payload)
            seen[ref] = {"ref": ref, "event": ev["event"],
                         "status": ev.get("status"), "payload": merged,
                         "requested_by": seen[ref]["requested_by"]}
    return list(seen.values())


def _order_row(item: dict[str, Any]) -> dict[str, Any]:
    payload = item["payload"]
    return {
        "orderId": item["ref"], "status": item["status"], "phase": item["event"],
        "status_note": ORDER_STATUS_NOTE,
        "day": payload.get("day"), "amount_total": payload.get("amount_total"),
        "currency": payload.get("currency"),
        "customer_id": payload.get("customer_id"),
        "requested_by": item.get("requested_by"),
    }


def _order_lines(body_lines: Any) -> list[dict[str, Any]]:
    """代客下单行请求解析（同 UE 下单行形态）。"""
    if not isinstance(body_lines, list) or not body_lines:
        raise ApiError("INVALID_ARGUMENT", "lines 须为非空数组")
    lines: list[dict[str, Any]] = []
    for row in body_lines:
        row = row if isinstance(row, dict) else {}
        code = str(row.get("productId") or row.get("product_code") or "").strip()
        if not code:
            raise ApiError("INVALID_ARGUMENT", "行请求缺 productId")
        qty = row.get("qty")
        try:
            if qty is None or float(qty) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiError("INVALID_ARGUMENT", "数量 qty 须为正数",
                           detail={"qty": qty}) from exc
        entry: dict[str, Any] = {"product_code": code, "qty": qty}
        if row.get("unit"):
            entry["unit"] = str(row["unit"])
        if row.get("note"):
            entry["note"] = str(row["note"])
        lines.append(entry)
    return lines


def _fmt_amount(v: Any) -> str | None:
    try:
        return f"{float(v):.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# SE 业务员台（supply-chain.se）
# --------------------------------------------------------------------------

def _owned_customers(d: Drivers, staff: str) -> list[dict[str, Any]]:
    """名下客户（归属关系 = 客户主数据 payload.sales_rep，主数据关系）。"""
    out = [snap for snap in d.master.list_entities(kind="customer")
           if str(snap["payload"].get("sales_rep") or "").strip() == staff]
    out.sort(key=lambda s: str(s["payload"].get("code") or ""))
    return out


def _credit_or_none(kernel, customer_id: str) -> dict[str, Any] | None:
    """信用额度位（kernel query_credit）；未登记信用额度的客户如实 None。

    内核对 credit_registry 无记录的客户报 NOT_FOUND —— read 面聚合时如实
    缺省（None + 注记），不让单个客户缺额度拖垮整个列表；其余错误原样上抛。
    """
    try:
        return kernel.query_credit(customer_id) or None
    except Exception as exc:  # noqa: BLE001 仅吞「未登记」语义，其余上抛
        if getattr(exc, "code", None) == "NOT_FOUND":
            return None
        raise


def _statement_summary(service, customer_id: str) -> dict[str, Any]:
    rows = service.list_statements(customer_id)
    open_count = sum(1 for r in rows if r["status"] in ("open", "confirming",
                                                        "disputed_pending"))
    latest = rows[-1]["period"] if rows else None
    return {"count": len(rows), "open_count": open_count, "latest_period": latest}


def register_se_face(app, settings, d: Drivers, kernel,
                     statements, auth: PagesetAuth) -> None:
    """SE 业务员台 17 端点（实现 16 + 如实 501 1：仅对账单推送）。

    两条开票审核端点（`GET/POST .../se/invoice-requests`，change:
    add-se-invoice-audit-queue）复用 `alert.handle`：本 grant 在 SE 面守的是
    「对一条 pending 的客户请求作处置」，开票审核是同一动作类型。未新开
    `invoice.audit` 是为了不把 paas registry、本仓 KNOWN_GRANTS 与两处对拍
    用例同时改一遍（既有先例：契约外对账代理复用 `statement.confirm`）。
    """

    def se_route(grant: str | None):
        def _dep(request: Request) -> dict[str, Any]:
            request.state.erp_grant = grant  # 访问日志：被 GRANT 拒时能指到哪一条
            if grant is None:  # 工作台：契约权限列=页面集授权（无独立 grant）
                if auth.grants_of(PAGESET_SE) is None:
                    raise ApiError("PAGESET_FORBIDDEN",
                                   f"当前租户未授权页面集：{PAGESET_SE}",
                                   detail={"pageset_key": PAGESET_SE})
            else:
                require_grant(auth, PAGESET_SE, grant)
            staff = request.headers.get(STAFF_HEADER, "").strip()
            if not staff:
                raise ApiError("AUTH_REQUIRED",
                               f"缺少业务员身份头 {STAFF_HEADER}"
                               "（SE 身份=业务员工号；底座 PAT 网关落地前演示形态）")
            return {"staff": staff}
        return _dep

    def _owned_or_404(customer_id: str, staff: str) -> dict[str, Any]:
        """名下客户或 404（越域/未知一律 404，不泄露存在性）。"""
        snap = d.master.get_entity(customer_id)
        if snap is None or snap["kind"] != "customer" or \
                str(snap["payload"].get("sales_rep") or "").strip() != staff:
            raise ApiError("NOT_FOUND", "客户档案不存在",
                           detail={"customerId": customer_id})
        return snap

    # ---- 1) 工作台（权限=页面集授权）----

    @app.get("/erp/supply-chain/v1/se/workspace")
    async def se_workspace(ctx: dict = Depends(se_route(None))):
        owned = _owned_customers(d, ctx["staff"])
        owned_ids = {s["entity_id"] for s in owned}
        orders = _order_states(d, owned_ids)
        pending_orders = sum(1 for o in orders if o["status"] == "pending")
        stms = [_statement_summary(statements, s["entity_id"]) for s in owned]
        open_statements = sum(s["open_count"] for s in stms)
        alerts = d.disposal.pending_tasks()
        # 今日拜访与 /se/visits 同一真源（crm.today_visit_count），不另算一遍——
        # 工作台是业务员每天第一眼看的数，与记录不同源时两个数都会失去可信度。
        today = datetime.now(timezone.utc).date().isoformat()
        return ok_shell({
            "staff": ctx["staff"],
            "owned_customer_count": len(owned),
            "pending_orders": pending_orders,
            "open_statements": open_statements,
            "pending_alerts": len(alerts),
            "today_visit_plan": d.crm.today_visit_count(ctx["staff"], today),
            "notes": {
                "crm": "今日拜访 = 本人今天已登记的拜访条数（与 /se/visits 同源）；"
                       "此数反映已登记的实际拜访，计划排程另属一条待立的车道",
                "alerts": "预警数 = 处置任务确认车道 pending 派单数"
                          "（reports 阈值→处置任务闭环的移动呈现）",
                "orders": "待办 = 名下客户订单确认车道待决数",
            },
        })

    # ---- 2) CRM 域 ----

    @app.get("/erp/supply-chain/v1/se/customers")
    async def se_customers(request: Request,
                           ctx: dict = Depends(se_route("crm.read"))):
        q = request.query_params
        keyword = (q.get("keyword") or "").strip().lower()
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        rows: list[dict[str, Any]] = []
        for snap in _owned_customers(d, ctx["staff"]):
            payload = snap["payload"]
            if keyword and keyword not in str(payload.get("name") or "").lower() \
                    and keyword not in str(payload.get("code") or "").lower():
                continue
            credit = _credit_or_none(kernel, snap["entity_id"])
            summary = _statement_summary(statements, snap["entity_id"])
            rows.append({
                "customerId": snap["entity_id"],
                "code": payload.get("code"), "name": payload.get("name"),
                "state": snap.get("state"),
                "sales_rep": payload.get("sales_rep"),
                "credit_occupied": credit.get("reserved") if credit else None,
                "credit_available": credit.get("available") if credit else None,
                "ar_balance": credit.get("ar_balance") if credit else None,
                "open_statement_count": summary["open_count"],
                "credit_source": "kernel query_credit（账本同源）" if credit
                else "未登记信用额度（内核 credit_registry 无记录，如实缺省）",
            })
        return list_shell(rows, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/se/customers/{customer_id}")
    async def se_customer_360(customer_id: str,
                              ctx: dict = Depends(se_route("crm.read"))):
        snap = _owned_or_404(customer_id, ctx["staff"])
        credit = _credit_or_none(kernel, snap["entity_id"]) or {}
        orders = _order_states(d, {snap["entity_id"]})
        orders.sort(key=lambda i: str((i["payload"] or {}).get("day") or ""),
                    reverse=True)
        return ok_shell({
            "customerId": snap["entity_id"],
            "profile": dict(snap["payload"]),
            "state": snap.get("state"),
            "credit": {
                "limit": credit.get("limit"), "ar_balance": credit.get("ar_balance"),
                "reserved": credit.get("reserved"),
                "available": credit.get("available"),
                "source_note": "额度占用与账本 query_balance 同源"
                               "（kernel credit_position，账本计算）" if credit
                else "未登记信用额度（内核 credit_registry 无记录，如实缺省）",
            },
            "recent_orders": [_order_row(o) for o in orders[:5]],
            "order_count": len(orders),
            "statements": _statement_summary(statements, snap["entity_id"]),
        })

    @app.get("/erp/supply-chain/v1/se/customers/{customer_id}/orders")
    async def se_customer_orders(request: Request, customer_id: str,
                                 ctx: dict = Depends(se_route("crm.read"))):
        require_grant(auth, PAGESET_SE, "order.write.proxy")  # 契约双 grant
        _owned_or_404(customer_id, ctx["staff"])
        q = request.query_params
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        rows = [_order_row(o) for o in _order_states(d, {customer_id})]
        rows.sort(key=lambda r: (str(r.get("day") or ""), r["orderId"]),
                  reverse=True)
        return list_shell(rows, offset=offset, limit=limit)

    # CRM 记录边界（add-crm-leads-visits design D5）：线索/拜访是**编排流程记录**，
    # 不是事件层事实——事件层对 lead 系名字有黑名单（无载体即拒），而那说的是
    # 「可计量的销售线索事实」，与「业务员今天记了一条线索」不是一件事。

    @app.get("/erp/supply-chain/v1/se/leads")
    async def se_list_leads(request: Request,
                            ctx: dict = Depends(se_route("crm.read"))):
        """本人线索池（按创建时刻倒序）。"""
        q = request.query_params
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        out = d.crm.leads_of(
            ctx["staff"],
            status=(q.get("status") or "").strip() or None,
            offset=offset, limit=limit)
        return list_shell(out["rows"], offset=offset, limit=limit)

    @app.post("/erp/supply-chain/v1/se/leads")
    async def se_create_lead(body: dict[str, Any],
                             ctx: dict = Depends(se_route("crm.visit.write"))):
        """建线索：名称必填；状态只能是 new（推进没有车道，不假装有）。"""
        return ok_shell(d.crm.create_lead(ctx["staff"], body))

    @app.post("/erp/supply-chain/v1/se/visits")
    async def se_create_visit(body: dict[str, Any],
                              ctx: dict = Depends(se_route("crm.visit.write"))):
        """拜访登记：纪要必填，客户或线索至少一个。

        `location` 只作为调用方给的引用原样保存——设备侧定位/影像采集要隐私声明
        过审，本端点不采集、不推断、不补默认值（design D4）。
        """
        customer_id = str(body.get("customerId") or body.get("customer_id")
                          or "").strip() or None
        lead_ref = str(body.get("leadRef") or body.get("lead_ref")
                       or "").strip() or None
        location = body.get("location")
        return ok_shell(d.crm.record_visit(
            ctx["staff"], customer_id=customer_id, lead_ref=lead_ref,
            note=str(body.get("note") or ""),
            location=None if location is None else str(location)))

    @app.get("/erp/supply-chain/v1/se/visits")
    async def se_list_visits(request: Request,
                             ctx: dict = Depends(se_route("crm.visit.write"))):
        """本人拜访记录（期间过滤，含端点）。"""
        q = request.query_params
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        out = d.crm.visits_of(
            ctx["staff"],
            date_from=(q.get("from") or "").strip() or None,
            date_to=(q.get("to") or "").strip() or None,
            offset=offset, limit=limit)
        return list_shell(out["rows"], offset=offset, limit=limit)

    # ---- 2b) 开票审核队列（grant: alert.handle）----
    #
    # 为什么用 alert.handle：这个 grant 守的是「对一条 pending 的客户请求作处置」——
    # 开票审核与预警处置在权限语义上是同一件事（accept/reject 一条待办）。新开
    # `invoice.audit` 要同时改 paas registry、本仓 KNOWN_GRANTS 与两处对拍用例，
    # 而收益只是名字更对字（既有先例：契约外对账代理复用 statement.confirm）。
    #
    # 边界：只审**本人名下客户**的申请；状态只经内核确认门写（本服务不存副本）；
    # 票据本体不在这里——本端点只把「申请」从 pending 推进到 accepted/rejected。

    @app.get("/erp/supply-chain/v1/se/invoice-requests")
    async def se_list_invoice_requests(request: Request,
                                       ctx: dict = Depends(se_route("alert.handle"))):
        """本人客户的开票申请：默认只看 pending，`status=all` 看全部。"""
        q = request.query_params
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        want = (q.get("status") or "").strip().lower()
        # 默认只看待审：审核队列的第一眼应当是「还需要我做什么」。
        # 只有显式 status=all 才放开全部状态。
        status = None if want == "all" else (want or "pending")
        ids = {snap["entity_id"] for snap in _owned_customers(d, ctx["staff"])}
        try:
            out = d.invoices.requests_for_customers(
                ids, status=status,
                date_from=(q.get("from") or "").strip() or None,
                date_to=(q.get("to") or "").strip() or None)
        except ApiError as exc:
            raise invoice_degraded(exc) from exc
        return list_shell(out["rows"], offset=offset, limit=limit,
                          extra={k: v for k, v in out.items() if k != "rows"})

    @app.post("/erp/supply-chain/v1/se/invoice-requests/{request_id}/decide")
    async def se_decide_invoice_request(request_id: str, body: dict[str, Any],
                                        ctx: dict = Depends(se_route("alert.handle"))):
        """审核一条开票申请：accepted / rejected（驳回必附 note），只经内核确认门。"""
        decision = str(body.get("decision") or "").strip().lower()
        if decision not in ("accepted", "rejected"):
            raise ApiError("INVALID_ARGUMENT",
                           "decision 须为 accepted 或 rejected",
                           detail={"decision": body.get("decision")})
        note = str(body.get("note") or "").strip()
        if decision == "rejected" and not note:
            raise ApiError("INVALID_ARGUMENT",
                           "驳回必须写明原因（note）——客户与运营据此处置",
                           detail={"decision": decision})

        # 归属闸：该申请必须属于本人名下客户，越域与不存在同一条 404。
        ids = {snap["entity_id"] for snap in _owned_customers(d, ctx["staff"])}
        try:
            mine = d.invoices.requests_for_customers(ids)
        except ApiError as exc:
            raise invoice_degraded(exc) from exc
        hit = next((r for r in mine["rows"] if r.get("requestId") == request_id), None)
        if hit is None:
            raise ApiError("NOT_FOUND", f"开票申请不存在：{request_id}",
                           detail={"requestId": request_id})

        try:
            out = kernel.decide_event(request_id, decision, note=note,
                                      lane="draft", actor="human")
        except ApiError as exc:
            raise invoice_degraded(exc) from exc
        return ok_shell({
            "requestId": request_id,
            "status": (out or {}).get("status") or decision,
            "customerId": hit.get("customerId"),
            "decided_at": (out or {}).get("decided_at"),
            "note": note or None,
        })

    # ---- 3) 代客下单域（确认门 + 代理身份留痕）----

    @app.post("/erp/supply-chain/v1/se/orders/proxy")
    async def se_order_proxy(body: dict[str, Any],
                             ctx: dict = Depends(se_route("order.write.proxy"))):
        """代客下单：与 POST /ue/orders 同一取价引擎与信用校验链路（同源）。

        业务员-客户归属校验（主数据 sales_rep）越权 403；代理身份三处留痕：
        请求（X-ERP-Staff）→ 服务端日志（编排 JSONL append-only）→ 业务单据
        （订单事件 requested_by）。额度不足 fail-closed 可解释拒绝。
        """
        customer_id = str(body.get("customerId") or "").strip()
        if not customer_id:
            raise ApiError("INVALID_ARGUMENT", "缺 customerId")
        snap = d.master.get_entity(customer_id)
        if snap is None or snap["kind"] != "customer":
            raise ApiError("NOT_FOUND", "客户档案不存在",
                           detail={"customerId": customer_id})
        if str(snap["payload"].get("sales_rep") or "").strip() != ctx["staff"]:
            raise ApiError("DATA_SCOPE_DENIED",
                           "代客下单越权：客户不在本人名下（业务员-客户归属"
                           "主数据校验失败；「代客」不改变数据域）",
                           detail={"customerId": customer_id,
                                   "staff": ctx["staff"]})
        requested_by = f"se:{ctx['staff']}"
        result = d.orders.accept_order(
            customer_id, _order_lines(body.get("lines")),
            date.today().isoformat(), requested_by=requested_by)
        if not result.get("accepted"):
            raise ApiError("CREDIT_EXCEEDED",
                           "订单金额超出可用信用额度（fail-closed 拒单）",
                           detail=result.get("explanation") or {})
        result["proxy"] = {
            "staff": ctx["staff"], "customer_id": customer_id,
            "requested_by": requested_by,
            "note": "代理身份已留痕（订单事件 requested_by = 编排层服务端日志"
                    " append-only）；数据域仍限本人名下客户",
        }
        result["shipping_receipt"] = body.get("shipping") or {}
        result["note_receipt"] = body.get("note") or ""
        return ok_shell(result)

    @app.get("/erp/supply-chain/v1/se/orders")
    async def se_orders(request: Request,
                        ctx: dict = Depends(se_route("order.write.proxy"))):
        q = request.query_params
        customer_id = (q.get("customerId") or "").strip()
        status = (q.get("status") or "").strip() or None
        date_from = (q.get("from") or "").strip() or None
        date_to = (q.get("to") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        owned_ids = {s["entity_id"] for s in _owned_customers(d, ctx["staff"])}
        if customer_id:
            _owned_or_404(customer_id, ctx["staff"])  # 越域 404
            owned_ids &= {customer_id}
        rows = []
        for item in _order_states(d, owned_ids):
            payload = item["payload"]
            day = str(payload.get("day") or "")
            if date_from and day < date_from:
                continue
            if date_to and day > date_to:
                continue
            if status and item["status"] != status:
                continue
            rows.append(_order_row(item))
        rows.sort(key=lambda r: (str(r.get("day") or ""), r["orderId"]),
                  reverse=True)
        return list_shell(rows, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/se/orders/{order_id}")
    async def se_order_detail(order_id: str,
                              ctx: dict = Depends(se_route("order.write.proxy"))):
        owned_ids = {s["entity_id"] for s in _owned_customers(d, ctx["staff"])}
        hits = [o for o in _order_states(d, owned_ids) if o["ref"] == order_id]
        if not hits:
            raise ApiError("NOT_FOUND", f"订单不存在：{order_id}",
                           detail={"orderId": order_id})
        item = hits[0]
        payload = item["payload"]
        return ok_shell({
            "orderId": item["ref"], "status": item["status"],
            "phase": item["event"], "status_note": ORDER_STATUS_NOTE,
            "day": payload.get("day"), "amount_total": payload.get("amount_total"),
            "currency": payload.get("currency"), "lines": payload.get("lines") or [],
            "credit_reservation": payload.get("credit_reservation"),
            "requested_by": item.get("requested_by"),
            "request_id": payload.get("request_id"),
        })

    # ---- 4) 预警处置域（拍板席位移动呈现；确认门与 owner 同源）----

    def _disposal_rows(status: str | None, severity: str | None) -> list[dict[str, Any]]:
        tasks = d.disposal.list_tasks()  # 处置车道事件档（同源 read 面）
        if status and status != "all":
            tasks = [t for t in tasks if t.get("status") == status]
        elif not status:
            tasks = [t for t in tasks if t.get("status") == "pending"]
        rows = []
        for t in tasks:
            payload = t.get("payload") or {}
            if severity and str(payload.get("severity") or "") != severity:
                continue
            rows.append({
                "alertId": t.get("request_id"), "status": t.get("status"),
                "metric": payload.get("metric"), "value": payload.get("value"),
                "threshold": payload.get("threshold"),
                "severity": payload.get("severity"),
                "title": payload.get("title"),
                "assignee": payload.get("assignee"),
                "task_template": payload.get("task_template"),
                "note": "阈值预警→处置任务闭环（tasks 7.2）的移动呈现；"
                        "拍板与 owner 席位同一确认门实现",
            })
        rows.sort(key=lambda r: str(r["alertId"]))
        return rows

    @app.get("/erp/supply-chain/v1/se/alerts")
    async def se_alerts(request: Request,
                        ctx: dict = Depends(se_route("alert.handle"))):
        q = request.query_params
        status = (q.get("status") or "").strip() or None
        severity = (q.get("severity") or q.get("level") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        return list_shell(_disposal_rows(status, severity), offset=offset,
                          limit=limit)

    @app.post("/erp/supply-chain/v1/se/alerts/{alert_id}/handle")
    async def se_alert_handle(alert_id: str, body: dict[str, Any],
                              ctx: dict = Depends(se_route("alert.handle"))):
        """处置回执：accept→确认门拍板生效；reject→拒绝留痕不落账。

        与 owner 席位同一确认门实现（reports 处置任务 confirm/reject 车道）；
        超时/失败不假报成功：回执如实回显车道结果。
        """
        decision = str(body.get("decision") or "").strip()
        if decision not in ("accept", "reject"):
            raise ApiError("INVALID_ARGUMENT", "decision 须为 accept / reject",
                           detail={"decision": decision})
        note = str(body.get("note") or "").strip()
        decided_by = f"se:{ctx['staff']}"
        if decision == "accept":
            out = d.disposal.confirm_disposal_task(alert_id, decided_by=decided_by)
            out["handled_by"] = decided_by
            return ok_shell(out)
        out = d.disposal.reject_disposal_task(alert_id, note, decided_by=decided_by)
        out["handled_by"] = decided_by
        return ok_shell(out)

    # ---- 5) 对账协助域 ----

    @app.get("/erp/supply-chain/v1/se/statements")
    async def se_statements(request: Request,
                            ctx: dict = Depends(se_route(
                                "statement.confirm.proxy"))):
        q = request.query_params
        status = (q.get("status") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        rows: list[dict[str, Any]] = []
        for snap in _owned_customers(d, ctx["staff"]):
            for st in statements.list_statements(snap["entity_id"], status=status):
                rows.append(st)
        rows.sort(key=lambda r: (str(r.get("period") or ""), r["statement_id"]))
        return list_shell(rows, offset=offset, limit=limit)

    @app.post("/erp/supply-chain/v1/se/statements/{statement_id}/push")
    async def se_statement_push(statement_id: str, body: dict[str, Any],
                                ctx: dict = Depends(se_route(
                                    "statement.confirm.proxy"))):
        raise _not_implemented(
            "对账单推送后端能力未就绪：端内消息通道归底座通知面（模板层无消息"
            "车道）；推送须留痕，禁止无车道假实现。客户确认仍走 UE 端确认门，"
            "SE 端不可代拍",
            "planned_lane: 底座端内消息通道（notify 车道）接入后实施推送留痕；"
            "客户确认门=UE /ue/statements/{id}/confirm（内核 proposal 车道）")

    # 契约 §6：AI 助手入口无独立端点（复用底座 chat/WS 面）。


# --------------------------------------------------------------------------
# SP 供应商（supply-chain.sp）
# --------------------------------------------------------------------------

def _resolve_supplier(d: Drivers, ref: str) -> dict[str, Any] | None:
    """供应商身份解析：实体 id 优先，其次编码（越域/未知由调用方 404）。"""
    snap = d.master.get_entity(ref)
    if snap is not None and snap["kind"] == "supplier":
        return snap
    hit = d.master.find_by_code("supplier", ref)
    if hit is not None and hit["kind"] == "supplier":
        return hit
    return None


def _po_states(d: Drivers, supplier_id: str | None = None) -> list[dict[str, Any]]:
    """采购单当前态（proposed→confirmed→pushed→supplier_accepted/rejected）。

    supplier_id 给定时只返回「已推送给该供应商」的采购单（数据域：本租户推送
    给本供应商档案的数据；未推送提案对供应商不可见）。
    """
    pos: dict[str, dict[str, Any]] = {}
    for ev in d.procurement._events():  # 采购编排事件档（同源 read 面）
        if ev.get("record") != "purchase_order":
            continue
        ref = ev.get("ref")
        payload = ev.get("payload") or {}
        if ev["event"] == "proposed":
            pos[ref] = {"ref": ref, "event": "proposed",
                        "status": ev.get("status"), "payload": dict(payload),
                        "pushed": False, "push_channel": None,
                        "supplier_response": None}
            continue
        item = pos.get(ref)
        if item is None:
            continue
        item["event"] = ev.get("event")
        item["status"] = ev.get("status")
        if ev["event"] == "pushed":
            item["pushed"] = True
            item["push_channel"] = payload.get("channel")
        if ev["event"] in ("supplier_accepted", "supplier_rejected"):
            item["supplier_response"] = dict(payload)
    out = []
    for item in pos.values():
        if supplier_id is not None:
            if not item["pushed"]:
                continue  # 未推送采购单对供应商不可见（数据域）
            if str(item["payload"].get("supplier_id") or "") != supplier_id:
                continue
        out.append(item)
    return out


def _po_visible(po_states: list[dict[str, Any]], po_ref: str) -> dict[str, Any]:
    hit = next((p for p in po_states if p["ref"] == po_ref), None)
    if hit is None:
        raise ApiError("NOT_FOUND", f"采购订单不存在：{po_ref}",
                       detail={"poId": po_ref})
    return hit


def _arrival_records(d: Drivers, po_refs: set[str]) -> list[dict[str, Any]]:
    return [e for e in d.procurement._events()
            if e.get("record") == "arrival_notice"
            and str((e.get("payload") or {}).get("po_ref") or "") in po_refs]


def _inbound_tasks(d: Drivers, arrival_refs: set[str]) -> list[dict[str, Any]]:
    return [e for e in d.procurement._events()
            if e.get("record") == "inbound_task"
            and str((e.get("payload") or {}).get("arrival_ref") or "") in arrival_refs]


def _po_feedbacks(d: Drivers, po_ref: str) -> list[dict[str, Any]]:
    return d.procurement.delivery_feedbacks(po_ref)


def register_sp_face(app, settings, d: Drivers, kernel,
                     auth: PagesetAuth) -> None:
    """SP 供应商 13 端点（实现 7 + 如实 501 6）。资金红线：本端零支付操作面。"""

    def sp_route(grant: str | None):
        def _dep(request: Request) -> dict[str, Any]:
            request.state.erp_grant = grant  # 访问日志：被 GRANT 拒时能指到哪一条
            if grant is None:  # 工作台：契约权限列=页面集授权（无独立 grant）
                if auth.grants_of(PAGESET_SP) is None:
                    raise ApiError("PAGESET_FORBIDDEN",
                                   f"当前租户未授权页面集：{PAGESET_SP}",
                                   detail={"pageset_key": PAGESET_SP})
            else:
                require_grant(auth, PAGESET_SP, grant)
            ref = request.headers.get(SUPPLIER_HEADER, "").strip()
            if not ref:
                raise ApiError("AUTH_REQUIRED",
                               f"缺少供应商身份头 {SUPPLIER_HEADER}"
                               "（SP 身份=供应商档案 id/编码；网关 PAT 落地前演示形态）")
            snap = _resolve_supplier(d, ref)
            if snap is None:
                raise ApiError("NOT_FOUND", "供应商档案不存在",
                               detail={"supplier": ref})
            return {"supplier_id": snap["entity_id"], "supplier": snap,
                    "supplier_code": snap["payload"].get("code")}
        return _dep

    def _my_pos(supplier_id: str) -> list[dict[str, Any]]:
        return _po_states(d, supplier_id)

    # ---- 1) 工作台（权限=页面集授权）----

    @app.get("/erp/supply-chain/v1/sp/workspace")
    async def sp_workspace(ctx: dict = Depends(sp_route(None))):
        supplier_id = ctx["supplier_id"]
        pos = _my_pos(supplier_id)
        po_refs = {p["ref"] for p in pos}
        arrivals = _arrival_records(d, po_refs)
        fb_po = {f["payload"].get("po_ref") for f in
                 [e for e in d.procurement._events()
                  if e.get("record") == "delivery_feedback"]}
        pending_accept = sum(1 for p in pos if p["status"] == "pushed"
                             and p["supplier_response"] is None)
        pending_ship = sum(1 for p in pos
                           if p["status"] in ("pushed", "accepted")
                           and p["ref"] not in fb_po)
        arrivals_open = sum(1 for a in arrivals if a.get("status") == "open")
        return ok_shell({
            "supplier_id": supplier_id, "supplier_code": ctx["supplier_code"],
            "po_count": len(pos),
            "po_pending_accept": pending_accept,
            "po_pending_ship": pending_ship,
            "arrival_notices_open": arrivals_open,
            "statements_pending": 0, "invoices_pending_verify": 0,
            "notes": {
                "statements": "对账域后端能力未就绪（501 如实）：应付对账单与"
                              "金蝶往来面同源（任务 3.1 挂账），计数暂不可得",
                "invoices": "发票域后端能力未就绪（501 如实）：上传/查验车道"
                            "未接入，计数暂不可得",
                "settlements": "结算查询与金蝶往来面同源（挂账）；本端零支付"
                               "操作面（红线 D4）",
            },
        })

    # ---- 2) 接单域（确认门）----

    @app.get("/erp/supply-chain/v1/sp/purchase-orders")
    async def sp_purchase_orders(request: Request,
                                 ctx: dict = Depends(sp_route("po.read"))):
        q = request.query_params
        status = (q.get("status") or "").strip() or None
        period = (q.get("period") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        rows = []
        for p in _my_pos(ctx["supplier_id"]):
            if status and p["status"] != status:
                continue
            payload = p["payload"]
            response = p["supplier_response"]
            rows.append({
                "poId": p["ref"], "status": p["status"], "event": p["event"],
                "supplier_id": payload.get("supplier_id"),
                "aggregate_ref": payload.get("aggregate_ref"),
                "lines": payload.get("lines") or [],
                "push_channel": p["push_channel"],
                "supplier_response": response,
                "status_note": "pushed=已推送待接单；accepted=已接单（到货预告"
                               "编排可驱动）；rejected=已拒单（终态，拒绝留痕）",
            })
        rows.sort(key=lambda r: r["poId"])
        extra = {"source": "采购编排事件档（同源 read 面）；数据域=推送给我方"
                           "供应商档案的采购单"}
        if period:
            extra["period_note"] = ("采购编排采购单无单据日期字段，期间过滤暂不可用"
                                    "（挂账：编排层补单据日期后生效）")
        return list_shell(rows, offset=offset, limit=limit, extra=extra)

    @app.get("/erp/supply-chain/v1/sp/purchase-orders/{po_id}")
    async def sp_po_detail(po_id: str, ctx: dict = Depends(sp_route("po.read"))):
        po = _po_visible(_my_pos(ctx["supplier_id"]), po_id)
        payload = po["payload"]
        arrivals = _arrival_records(d, {po_id})
        arrival_refs = {a["ref"] for a in arrivals}
        return ok_shell({
            "poId": po_id, "status": po["status"], "event": po["event"],
            "supplier_id": payload.get("supplier_id"),
            "aggregate_ref": payload.get("aggregate_ref"),
            "lines": payload.get("lines") or [],
            "push_channel": po["push_channel"],
            "supplier_response": po["supplier_response"],
            "delivery_feedbacks": [
                {"ref": f["ref"], "event": f["event"], "status": f["status"],
                 "payload": dict(f.get("payload") or {})}
                for f in _po_feedbacks(d, po_id)],
            "arrival_notices": [
                {"ref": a["ref"], "status": a["status"],
                 "payload": dict(a.get("payload") or {})} for a in arrivals],
            "inbound_tasks": [
                {"ref": t["ref"], "status": t["status"],
                 "payload": dict(t.get("payload") or {})}
                for t in _inbound_tasks(d, arrival_refs)],
            "field_note": "交期/收货仓：采购编排提案不携带交期与收货仓字段"
                          "（如实缺省，待编排层补字段）",
        })

    @app.post("/erp/supply-chain/v1/sp/purchase-orders/{po_id}/accept")
    async def sp_po_accept(po_id: str, body: dict[str, Any],
                           ctx: dict = Depends(sp_route("po.accept"))):
        """接单回执：确认门语义；写入采购单同一状态机（不建第二状态机）。

        rejected 须附 rejectedNote（可解释拒绝纪律）；回执驱动到货预告编排。
        """
        po = _po_visible(_my_pos(ctx["supplier_id"]), po_id)  # 越域 404
        decision = str(body.get("decision") or "").strip()
        if decision not in ("accepted", "rejected"):
            raise ApiError("INVALID_ARGUMENT",
                           "decision 须为 accepted / rejected",
                           detail={"decision": decision})
        responded_by = f"sp:{ctx['supplier_code'] or ctx['supplier_id']}"
        out = d.procurement.record_supplier_response(
            po_id, decision, note=str(body.get("rejectedNote") or ""),
            responded_by=responded_by)
        out["po_status_before"] = po["status"]
        return ok_shell(out)

    # ---- 3) 发货反馈域 ----

    @app.post("/erp/supply-chain/v1/sp/purchase-orders/{po_id}/delivery-feedback")
    async def sp_delivery_feedback(po_id: str, body: dict[str, Any],
                                   ctx: dict = Depends(sp_route(
                                       "delivery.feedback"))):
        """发货反馈回执：行级明细 + 物流商/单号/凭证影像引用；自动生成到货
        预告（与入库任务联动，编排面同源）。凭证影像仅存引用（中台文件服务
        上传契约另行接入）。"""
        _po_visible(_my_pos(ctx["supplier_id"]), po_id)  # 越域 404
        lines = body.get("lines")
        if not isinstance(lines, list) or not lines:
            raise ApiError("INVALID_ARGUMENT", "发货反馈 lines 须为非空数组")
        responded_by = f"sp:{ctx['supplier_code'] or ctx['supplier_id']}"
        out = d.procurement.record_delivery_feedback(
            po_id, lines,
            carrier=str(body.get("carrier") or ""),
            waybill_no=str(body.get("waybillNo") or ""),
            documents=[str(x) for x in (body.get("documents") or [])],
            expected_day=str(body.get("expectedDay") or "") or None,
            responded_by=responded_by)
        return ok_shell(out)

    @app.get("/erp/supply-chain/v1/sp/arrival-notices")
    async def sp_arrival_notices(request: Request,
                                 ctx: dict = Depends(sp_route(
                                     "delivery.feedback"))):
        q = request.query_params
        status = (q.get("status") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        po_refs = {p["ref"] for p in _my_pos(ctx["supplier_id"])}
        rows = []
        for a in _arrival_records(d, po_refs):
            if status and a.get("status") != status:
                continue
            payload = dict(a.get("payload") or {})
            rows.append({"ref": a["ref"], "status": a["status"],
                         "event": a["event"], **payload})
        rows.sort(key=lambda r: r["ref"])
        return list_shell(rows, offset=offset, limit=limit)

    @app.post("/erp/supply-chain/v1/sp/arrival-notices")
    async def sp_arrival_notice_create(body: dict[str, Any],
                                       ctx: dict = Depends(sp_route(
                                           "delivery.feedback"))):
        """到货预告补录（发货反馈已自动生成，本端点承载补录）。"""
        po_ref = str(body.get("poRef") or body.get("po_ref") or "").strip()
        if not po_ref:
            raise ApiError("INVALID_ARGUMENT", "缺 poRef（订单引用）")
        _po_visible(_my_pos(ctx["supplier_id"]), po_ref)  # 越域 404
        lines = body.get("lines")
        if not isinstance(lines, list) or not lines:
            raise ApiError("INVALID_ARGUMENT", "到货预告 lines 须为非空数组")
        responded_by = f"sp:{ctx['supplier_code'] or ctx['supplier_id']}"
        out = d.procurement.record_arrival_notice(
            po_ref, lines, expected_day=str(body.get("expectedDay") or "") or None,
            requested_by=responded_by)
        return ok_shell(out)

    @app.get("/erp/supply-chain/v1/sp/inventory")
    async def sp_inventory(request: Request,
                           ctx: dict = Depends(sp_route("po.read"))):
        """库存数量面只读（establish-erp-event-kernel 7.2）。

        读的是**对象投影余额**（事件派生，带水位与重算校验），不是 WMS 快照那份
        外部库位视图——两者是不同事实，不互相冒充。

        越域闸：只允许查本供应商供的商品。到货预告不带库位时库存记在未知库位维度，
        所以 `warehouse`/`lot` 缺省按 `__unknown__` 查（与写入侧同一个值），
        否则「刚收的货查不到」会被误读成「没收到」。
        """
        q = request.query_params
        sku = (q.get("sku") or "").strip()
        if not sku:
            raise ApiError("INVALID_ARGUMENT", "缺 sku（商品编码）")
        product = d.master.find_by_code("product", sku)
        if product is None or (product.get("payload") or {}).get("supplier_id") \
                != ctx["supplier_id"]:
            # 不是本供应商供的货一律 404：库存是跨供应商同表的，不能靠"查到了"推断可见性。
            raise ApiError("NOT_FOUND", "该商品不在本供应商供货范围",
                           detail={"sku": sku})
        warehouse = (q.get("warehouse") or "").strip() or None
        lot = (q.get("lot") or "").strip() or None
        return ok_shell(stock_on_hand(d, sku, warehouse=warehouse, lot=lot))

    # ---- 4) 对账域 —— 后端能力未就绪，如实 501 ----

    _SP_STATEMENT_501 = (
        "应付对账单后端能力未就绪：契约数据源=金蝶 provider 往来面拉取 + 账本"
        "内核计算（api-contract §4）；金蝶 live 接通属任务 3.1 挂账，采购编排"
        "单据无金额无日期，无法如实派生对账单 —— 禁止假实现")

    @app.get("/erp/supply-chain/v1/sp/statements")
    async def sp_statements(ctx: dict = Depends(sp_route("statement.read"))):
        raise _not_implemented(_SP_STATEMENT_501,
                               "planned_lane: 金蝶 provider 往来面拉取"
                               "（任务 3.1）→ 账本内核计算应付 → 单据级对账单"
                               "派生 + 确认门（内核自平核对 proposal，同 UE 契约）")

    @app.get("/erp/supply-chain/v1/sp/statements/{statement_id}")
    async def sp_statement_detail(statement_id: str,
                                  ctx: dict = Depends(sp_route("statement.read"))):
        raise _not_implemented(_SP_STATEMENT_501, "planned_lane: 同 GET /sp/statements")

    @app.post("/erp/supply-chain/v1/sp/statements/{statement_id}/confirm")
    async def sp_statement_confirm(statement_id: str, body: dict[str, Any],
                                   ctx: dict = Depends(sp_route("statement.read"))):
        raise _not_implemented(_SP_STATEMENT_501,
                               "planned_lane: 金额同源到位后确认门=内核自平核对"
                               " proposal（超时/失败不假报成功）；异议进处置车道")

    # ---- 5) 发票域 —— 后端能力未就绪，如实 501 ----

    _SP_INVOICE_501 = (
        "发票上传后端能力未就绪：上传件中台文件服务与发票查验/勾兑车道均未接入，"
        "金蝶连接器回写为 contract-only（docs/connector-onboarding-supply-chain.md"
        " §1.2）；禁止假实现")

    @app.post("/erp/supply-chain/v1/sp/invoices")
    async def sp_invoice_upload(body: dict[str, Any],
                                ctx: dict = Depends(sp_route("invoice.upload"))):
        raise _not_implemented(
            _SP_INVOICE_501,
            "planned_lane: 中台文件服务上传契约 + 发票查验/勾兑车道 + 金蝶连接器"
            "回写（contract-only）")

    @app.get("/erp/supply-chain/v1/sp/invoices")
    async def sp_invoice_list(ctx: dict = Depends(sp_route("invoice.upload"))):
        raise _not_implemented(_SP_INVOICE_501,
                               "planned_lane: 同 POST /sp/invoices（上传历史与"
                               "核验进度 read 面）")

    # ---- 6) 结算查询域（只读；零支付操作面）—— 未就绪，如实 501 ----

    @app.get("/erp/supply-chain/v1/sp/settlements")
    async def sp_settlements(ctx: dict = Depends(sp_route("statement.read"))):
        raise _not_implemented(
            "结算进度查询后端能力未就绪：结算与金蝶往来面同源（任务 3.1 挂账）；"
            "本端零支付操作面（红线 D4：对外付款走金蝶/持牌通道）不变",
            "planned_lane: 金蝶付款/结算回写 read 面（只读，无任何支付/账户操作）")

    # 契约 §7：AI 助手入口无独立端点（复用底座 chat/WS 面）。


# --------------------------------------------------------------------------
# CP 承运商（supply-chain.cp）
# --------------------------------------------------------------------------

def _waybill_states(d: Drivers, carrier: str | None = None) -> list[dict[str, Any]]:
    """运单当前态（交付编排事件档同源回放；carrier 给定时按运单域过滤）。"""
    waybills: dict[str, dict[str, Any]] = {}
    for ev in d.fulfillment._events():  # 交付编排事件档（同源 read 面）
        if ev.get("record") != "waybill":
            continue
        ref = ev.get("ref")
        payload = ev.get("payload") or {}
        if ev["event"] == "created":
            if carrier is not None and str(payload.get("carrier") or "") != carrier:
                continue
            waybills[ref] = {"ref": ref, "event": ev["event"],
                             "status": ev.get("status"), "payload": dict(payload),
                             "carrier_response": None}
            continue
        item = waybills.get(ref)
        if item is None:
            continue
        item["event"] = ev.get("event")
        item["status"] = ev.get("status")
        if ev["event"] in ("carrier_accepted", "carrier_rejected"):
            item["carrier_response"] = dict(payload)
    return list(waybills.values())


def _waybill_by_ref(d: Drivers, waybill_id: str,
                    carrier: str | None) -> dict[str, Any]:
    """运单域解析：实体 ref 或运单号（waybill_no）均可；越域/未知 404。"""
    states = _waybill_states(d, carrier)
    hit = next((w for w in states if w["ref"] == waybill_id), None)
    if hit is None:
        hit = next((w for w in states
                    if str(w["payload"].get("waybill_no") or "") == waybill_id),
                   None)
    if hit is None:
        raise ApiError("NOT_FOUND", f"运单不存在：{waybill_id}",
                       detail={"waybillId": waybill_id})
    return hit


def _plan_of(d: Drivers, plan_ref: str) -> dict[str, Any] | None:
    hit = next((e for e in d.fulfillment._events()
                if e.get("record") == "fulfillment_plan"
                and e.get("ref") == plan_ref and e.get("event") == "created"), None)
    return dict(hit) if hit else None


def register_cp_face(app, settings, d: Drivers, kernel,
                     auth: PagesetAuth) -> None:
    """CP 承运商 12 端点（实现 8 + 如实 501 4）。计费同源：CP 端只读确认，
    不改计费口径；采集纪律：定位/影像为可选采集（隐私声明过审后启用）。"""

    def cp_route(grant: str | None):
        def _dep(request: Request) -> dict[str, Any]:
            request.state.erp_grant = grant  # 访问日志：被 GRANT 拒时能指到哪一条
            if grant is None:  # 工作台：契约权限列=页面集授权（无独立 grant）
                if auth.grants_of(PAGESET_CP) is None:
                    raise ApiError("PAGESET_FORBIDDEN",
                                   f"当前租户未授权页面集：{PAGESET_CP}",
                                   detail={"pageset_key": PAGESET_CP})
            else:
                require_grant(auth, PAGESET_CP, grant)
            carrier = request.headers.get(CARRIER_HEADER, "").strip()
            if not carrier:
                raise ApiError("AUTH_REQUIRED",
                               f"缺少承运商身份头 {CARRIER_HEADER}"
                               "（CP 身份=承运商标识；网关 PAT 落地前演示形态）")
            return {"carrier": carrier}
        return _dep

    # ---- 1) 工作台（权限=页面集授权）----

    @app.get("/erp/supply-chain/v1/cp/workspace")
    async def cp_workspace(ctx: dict = Depends(cp_route(None))):
        carrier = ctx["carrier"]
        waybills = _waybill_states(d, carrier)
        receipts: list[dict[str, Any]] = []
        for w in waybills:  # 运单关联回执（月台/提货/POD，同源 read 面）
            receipts.extend(d.fulfillment.carrier_receipts(w["ref"]))
        open_wb = [w for w in waybills if w["status"] == "open"]
        pending_accept = [w for w in open_wb if w["carrier_response"] is None]
        dock_active = sum(1 for r in receipts
                          if r["record"] == "dock_reservation"
                          and r["status"] == "reserved")
        pod_exception = sum(1 for r in receipts
                            if r["record"] == "pod" and r["event"] == "reported"
                            and str((r["payload"] or {}).get("exception_note")
                                    or "").strip())
        return ok_shell({
            "carrier": carrier,
            "waybills_total": len(waybills),
            "waybills_open": len(open_wb),
            "waybills_pending_accept": len(pending_accept),
            "dock_reservations_active": dock_active,
            "pod_exceptions": pod_exception,
            "notes": {
                "dock_slots": "可约时段目录 501 如实（月台资源主数据归仓库作业"
                              "编排，未建模）",
                "freight": "运费单 read 面 501 如实（计费 read 工具面待扩展）",
                "tasks": "今日任务=有效月台预约数（编排层无日期字段，如实不按日"
                         "过滤）",
            },
        })

    # ---- 2) 运单域（确认门）----

    @app.get("/erp/supply-chain/v1/cp/waybills")
    async def cp_waybills(request: Request,
                          ctx: dict = Depends(cp_route("waybill.read"))):
        q = request.query_params
        status = (q.get("status") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        rows = []
        for w in _waybill_states(d, ctx["carrier"]):
            if status and w["status"] != status:
                continue
            payload = w["payload"]
            plan = _plan_of(d, str(payload.get("plan_ref") or ""))
            rows.append({
                "waybillId": w["ref"], "waybill_no": payload.get("waybill_no"),
                "carrier": payload.get("carrier"), "status": w["status"],
                "event": w["event"], "plan_ref": payload.get("plan_ref"),
                "order_ref": (plan or {}).get("payload", {}).get("order_ref"),
                "carrier_response": w["carrier_response"],
                "status_note": "open=在途流转（可月台/提货/签收）；"
                               "rejected=已拒单（终态，改派由租户侧重下运单）",
            })
        rows.sort(key=lambda r: r["waybillId"])
        return list_shell(rows, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/cp/waybills/{waybill_id}")
    async def cp_waybill_detail(waybill_id: str,
                                ctx: dict = Depends(cp_route("waybill.read"))):
        w = _waybill_by_ref(d, waybill_id, ctx["carrier"])
        payload = w["payload"]
        plan = _plan_of(d, str(payload.get("plan_ref") or ""))
        track = None
        try:
            track = d.fulfillment.track_waybill(
                str(payload.get("waybill_no") or ""))["events"]
        except Exception:
            track = None  # 轨迹不可得如实置空，不伪造
        return ok_shell({
            "waybillId": w["ref"], "waybill_no": payload.get("waybill_no"),
            "carrier": payload.get("carrier"), "status": w["status"],
            "plan_ref": payload.get("plan_ref"),
            "order_ref": (plan or {}).get("payload", {}).get("order_ref"),
            "customer_id": (plan or {}).get("payload", {}).get("customer_id"),
            "lines": (plan or {}).get("payload", {}).get("lines") or [],
            "carrier_response": w["carrier_response"],
            "track_events": track,
            "logistics_adapter": d.fulfillment._logistics.describe(),  # 适配器描述（同源 read 面）
            "note": "运单明细（货/仓/窗期/收发信息）：编排层运单记录不携带窗期"
                    "与收发货人明细（如实缺省，待编排层补字段）",
        })

    @app.post("/erp/supply-chain/v1/cp/waybills/{waybill_id}/accept")
    async def cp_waybill_accept(waybill_id: str, body: dict[str, Any],
                                ctx: dict = Depends(cp_route("waybill.accept"))):
        """承运回执：确认门语义；拒绝回显原因；回执驱动交付编排改派（改派由
        租户侧重下运单承载，本端拒绝即终态留痕）。"""
        _waybill_by_ref(d, waybill_id, ctx["carrier"])  # 越域 404
        decision = str(body.get("decision") or "").strip()
        if decision not in ("accepted", "rejected"):
            raise ApiError("INVALID_ARGUMENT",
                           "decision 须为 accepted / rejected",
                           detail={"decision": decision})
        responded_by = f"cp:{ctx['carrier']}"
        out = d.fulfillment.record_carrier_response(
            waybill_id, decision, note=str(body.get("rejectedNote") or ""),
            responded_by=responded_by)
        return ok_shell(out)

    # ---- 3) 月台预约域 ----

    @app.get("/erp/supply-chain/v1/cp/dock-slots")
    async def cp_dock_slots(ctx: dict = Depends(cp_route("dock.reserve"))):
        raise _not_implemented(
            "可约月台时段后端能力未就绪：月台资源主数据与可约时段计算归仓库"
            "作业编排（编排层未建模月台资源）；预约回执本身已实现"
            "（POST /cp/waybills/{id}/dock-reservation，slotId 为仓库侧不透明"
            "引用）；禁止伪造时段目录",
            "planned_lane: 仓库作业编排月台资源建模（时段目录 + 占用计算）")

    @app.post("/erp/supply-chain/v1/cp/waybills/{waybill_id}/dock-reservation")
    async def cp_dock_reserve(waybill_id: str, body: dict[str, Any],
                              ctx: dict = Depends(cp_route("dock.reserve"))):
        """预约回执（action=reserve / cancel，改约=再预约新时段）；与仓库作业
        编排联动（slotId 为仓库侧不透明引用）。"""
        _waybill_by_ref(d, waybill_id, ctx["carrier"])  # 越域 404
        action = str(body.get("action") or "reserve").strip()
        responded_by = f"cp:{ctx['carrier']}"
        if action == "cancel":
            reservation_id = str(body.get("reservationId") or "").strip()
            if not reservation_id:
                raise ApiError("INVALID_ARGUMENT", "取消预约须带 reservationId")
            owned = any(r["ref"] == reservation_id
                        for r in d.fulfillment.carrier_receipts(waybill_id)
                        if r["record"] == "dock_reservation")
            if not owned:
                raise ApiError("NOT_FOUND",
                               f"月台预约不存在或不属于该运单：{reservation_id}",
                               detail={"reservationId": reservation_id})
            return ok_shell(d.fulfillment.cancel_dock_reservation(
                reservation_id, responded_by=responded_by))
        slot_id = str(body.get("slotId") or "").strip()
        if not slot_id:
            raise ApiError("INVALID_ARGUMENT", "预约须带 slotId")
        return ok_shell(d.fulfillment.record_dock_reservation(
            waybill_id, slot_id,
            warehouse=str(body.get("warehouse") or ""),
            slot_day=str(body.get("slotDay") or ""),
            responded_by=responded_by))

    # ---- 4) 提货域 ----

    @app.post("/erp/supply-chain/v1/cp/waybills/{waybill_id}/pickup-checkin")
    async def cp_pickup_checkin(waybill_id: str, body: dict[str, Any],
                                ctx: dict = Depends(cp_route("pickup.checkin"))):
        """打卡回执：回执驱动在途状态。扫码提货复用底座 bind-scan 同一扫码
        实现（解析目标=作业码）。定位/影像为可选采集（隐私声明过审后启用）。"""
        _waybill_by_ref(d, waybill_id, ctx["carrier"])  # 越域 404
        location = body.get("location") if isinstance(body.get("location"), dict) \
            else None
        image_refs = [str(i) for i in (body.get("imageRefs") or [])]
        responded_by = f"cp:{ctx['carrier']}"
        return ok_shell(d.fulfillment.record_pickup_checkin(
            waybill_id, location=location, image_refs=image_refs,
            scan_code=str(body.get("scanCode") or ""),
            responded_by=responded_by))

    # ---- 5) 签收域 ----

    @app.post("/erp/supply-chain/v1/cp/waybills/{waybill_id}/pod")
    async def cp_pod_report(waybill_id: str, body: dict[str, Any],
                            ctx: dict = Depends(cp_route("pod.report"))):
        """POD 回执：驱动运单结单与计费事件（计费规则引擎同源，本端零落账）；
        异常签收进处置车道（ThresholdEvaluator 确认门派单留痕）。"""
        _waybill_by_ref(d, waybill_id, ctx["carrier"])  # 越域 404
        documents = [str(x) for x in (body.get("documents") or [])]
        exception_note = str(body.get("exceptionNote") or "").strip()
        responded_by = f"cp:{ctx['carrier']}"
        out = d.fulfillment.record_carrier_pod(
            waybill_id, documents=documents, exception_note=exception_note,
            responded_by=responded_by)
        if out["exception"]:
            alert = {"triggered": True, "metric": "waybill_pod_exception",
                     "value": out["waybill_no"], "severity": "high",
                     "disposal_task": "异常签收处置：核对异常说明、回执客户并留痕"}
            task = d.disposal.raise_disposal_task(
                alert, title=f"运单异常签收：{out['waybill_no']}"
                             f"（{exception_note[:80]}）",
                requested_by=responded_by)
            out["disposal_request_id"] = task.get("request_id")
            out["disposal_status"] = task.get("status")
        out["settlement_note"] = ("签收触发收入确认经内核 proposal 车道（账务面"
                                  "驱动）；运单结单/计费事件由计费规则引擎"
                                  "事件驱动，本端不落结单态（如实挂账）")
        return ok_shell(out)

    @app.get("/erp/supply-chain/v1/cp/waybills/{waybill_id}/events")
    async def cp_waybill_events(waybill_id: str,
                                ctx: dict = Depends(cp_route("waybill.read"))):
        """运单事件流/轨迹：交付编排事件档 + 只读适配器（与客户侧全单物流
        视图同源，零平行取数）。"""
        w = _waybill_by_ref(d, waybill_id, ctx["carrier"])
        events: list[dict[str, Any]] = []
        for ev in d.fulfillment._events():
            if ev.get("ref") != w["ref"]:
                continue
            events.append({"record": ev.get("record"), "ref": ev.get("ref"),
                           "event": ev.get("event"), "status": ev.get("status"),
                           "payload": dict(ev.get("payload") or {}),
                           "requested_by": ev.get("requested_by"),
                           "note": ev.get("note")})
        events.extend(d.fulfillment.carrier_receipts(w["ref"]))
        track = None
        try:
            track = d.fulfillment.track_waybill(
                str(w["payload"].get("waybill_no") or ""))["events"]
        except Exception:
            track = None  # 轨迹不可得如实置空，不伪造
        return ok_shell({
            "waybillId": w["ref"], "waybill_no": w["payload"].get("waybill_no"),
            "status": w["status"], "events": events,
            "track_events": track,
            "caliber_note": "与客户侧全单物流视图同源（交付编排事件档 + 只读"
                            "适配器回流，零平行取数）",
        })

    # ---- 6) 运费对账域（读面=内核费用行；确认=内核自平核对 proposal）----

    @app.get("/erp/supply-chain/v1/cp/freight-statements")
    async def cp_freight_statements(request: Request,
                                    ctx: dict = Depends(cp_route(
                                        "freight.statement.read"))):
        """运费单列表：按（承运商, 期间）归集内核费用行。

        归集只认**费用行 refs 里带承运商**的行（`list_fees` 每行带 `refs`，即计费时
        传入的 `document_refs`）。没有承运商维度的费用不进运费单——宁可空，也不编。
        越域不出现在列表里：查询恒带调用方承运商身份。
        """
        q = request.query_params
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        period = (q.get("period") or "").strip() or None
        out = d.freight.statements(carrier=ctx["carrier"], period=period)
        return list_shell(out["rows"], offset=offset, limit=limit, extra={
            "note": out["note"],
            "truncated": out["truncated"],
            "caliber_note": "运费单=内核费用行按（承运商,期间）归集，红冲行按负额"
                            "自然抵消；单据号 FS-<carrier>-<period>",
            "source_note": "费用行来自内核计费（费用行 refs 带承运商才可归集）；"
                           "本端只读归集，不另存一份运费单状态",
        })

    @app.get("/erp/supply-chain/v1/cp/freight-statements/{statement_id}")
    async def cp_freight_detail(statement_id: str,
                                ctx: dict = Depends(cp_route(
                                    "freight.statement.read"))):
        """运费单明细：按运单聚合，每行带费用溯源（费用项/规则版本/单据引用）。

        越域按 **404**（不是 403）：非本承运商的单号不确认其存在。
        """
        detail = d.freight.statement(statement_id)  # 不存在 → NOT_FOUND(404)
        if str(detail.get("carrier") or "") != ctx["carrier"]:
            raise ApiError("NOT_FOUND", f"运费单不存在：{statement_id}",
                           detail={"statementId": statement_id})
        return ok_shell(detail)

    @app.post("/erp/supply-chain/v1/cp/freight-statements/{statement_id}/confirm")
    async def cp_freight_confirm(statement_id: str, body: dict[str, Any],
                                 ctx: dict = Depends(cp_route(
                                     "freight.statement.read"))):
        """确认/异议一张运费单：落库形态是**内核自平核对 proposal**（借贷同额、净额
        为零，不动任何余额），由确认门拍板 —— 本端不落任何运费单状态。

        - 驳回必须写原因（`note`）；
        - 重复确认/改判 → 409 WORKFLOW_STATE（如实冲突，不静默覆盖）；
        - 超时/失败不假报成功。
        """
        detail = d.freight.statement(statement_id)
        if str(detail.get("carrier") or "") != ctx["carrier"]:
            raise ApiError("NOT_FOUND", f"运费单不存在：{statement_id}",
                           detail={"statementId": statement_id})
        out = d.freight.confirm(
            statement_id, str(body.get("decision") or ""),
            note=(str(body.get("note") or "").strip() or None),
            confirmed_by=f"cp:{ctx['carrier']}")
        out["decision_note"] = ("确认结论经内核自平核对 proposal 车道（净额为零，"
                                "不动余额），由确认门拍板；驳回进异议处置")
        return ok_shell(out)

    # 契约 §7：AI 助手入口无独立端点（复用底座 chat/WS 面）。


# ---- FI 财务后台（内部页集 supply-chain.fi：不进 /pagesets 载荷）----

def register_fi_face(app, settings, d: Drivers, auth: PagesetAuth,
                     staff: FinanceStaff | None = None) -> None:
    """财务后台（48 个端点）。

    为什么是与四个业务页集**并列的一面**，而不是它们的子集：公司自己的资产负债表 /
    利润表 / 现金流量表不属于任何供应链外部角色。放进客户订货小程序既是放错位置，
    也是一处暴露面——而对外页集 key 由客户端硬编码校验，多吐一个会整包作废。
    所以这里是 `INTERNAL_PAGESET_KEYS`：服务端能授权、能守门、能审计，载荷里不出现。

    三道闸的顺序**不能换**：先页面集（这个部署允不允许财务面，未授权时连「有没有名单」
    都不该泄露）→ 再 grant（这个端点要哪一档）→ 最后按人（`ERP_FINANCE_STAFF`）。

    第三道是 2026-11-14 补的（change: add-fi-staff-authorization）。为什么必须有它：
    `require_grant` 判的是**部署**，不认识调用者；而 fi 面没有任何数据范围（财务就是看
    全公司的账），所以少了这一道，「把 supply-chain.fi 写进授权配置」就等于把财务后台
    发给所有已登录用户——留痕标签还会让他们看起来像有授权的经办人。

    端点本身都是**投影**：不写账、不建表、不缓存，直接调各车道。
    报表自己的对照列与自证字段随之一起返回，不在这一层重算任何数——重算就是第二份口径。
    """
    policy = staff or load_finance_staff(
        inline_json=getattr(settings, "finance_staff_json", None),
        file_path=getattr(settings, "finance_staff_file", None))

    def fi_route(grant: str):
        def _dep(request: Request) -> dict[str, Any]:
            request.state.erp_grant = grant
            require_grant(auth, PAGESET_FI, grant)
            who = request.headers.get(STAFF_HEADER, "").strip()
            if not who:
                raise ApiError(
                    "AUTH_REQUIRED",
                    f"缺少财务身份头 {STAFF_HEADER}"
                    "（FI 身份=财务员工号；租户头是常量，不能单独当凭据）")
            # 程序身份先拒（不靠名单）：这是「机器不得代表财务」那条纪律的服务面实现，
            # 也让归档规格里「机器不得关账 → 参数错误」继续成立（而不是被名单的 403 顶掉）。
            if who.lower() in PROGRAM_IDENTITIES:
                raise ApiError(
                    "INVALID_ARGUMENT",
                    f"须人工经办：{who} 不得代表财务操作"
                    "（REST 面只接人工；自动化需服务账号机制，不复用人的名单）",
                    detail={"provided": who,
                            "program_identities": sorted(PROGRAM_IDENTITIES)})
            require_finance_staff(policy, grant, who)
            return {"staff": who}
        return _dep

    def _period(request: Request) -> Any:
        return request.query_params.get("period")

    @app.get("/erp/supply-chain/v1/fi/reports/balance-sheet")
    async def fi_balance_sheet(request: Request,
                               ctx: dict = Depends(fi_route("finance.read"))):
        """资产负债表（期末 + 年初）。"""
        return ok_shell(d.cn_statements.balance_sheet(_period(request)))

    @app.get("/erp/supply-chain/v1/fi/reports/income-statement")
    async def fi_income_statement(request: Request,
                                  ctx: dict = Depends(fi_route("finance.read"))):
        """利润表（本期 + 上年同期 + 本年累计）。"""
        return ok_shell(d.cn_statements.income_statement(_period(request)))

    @app.get("/erp/supply-chain/v1/fi/reports/cash-flow")
    async def fi_cash_flow(request: Request,
                           ctx: dict = Depends(fi_route("finance.read"))):
        """现金流量表（直接法；本期 + 上年同期 + 本年累计，含两条自证脚注）。"""
        return ok_shell(d.cn_statements.cash_flow(_period(request)))

    # ---- 月结三件：期间 / 期初 / 结转 ----

    @app.get("/erp/supply-chain/v1/fi/periods")
    async def fi_periods(ctx: dict = Depends(fi_route("finance.read"))):
        """期间清单：关账记录 ∪ **有分录的期间**（`hasEntries`/`vouchers`/`lines`）。

        只列「关过账的期间」会让有业务没关账的月份整月消失——那正是要关的那一个月。
        """
        return ok_shell(d.periods.overview(d.books.period_activity()))

    @app.get("/erp/supply-chain/v1/fi/periods/{period}")
    async def fi_period(period: str,
                        ctx: dict = Depends(fi_route("finance.read"))):
        """单期间状态：是否关账、关账人/时间/备注。"""
        return ok_shell(d.periods.status(period))

    @app.get("/erp/supply-chain/v1/fi/opening")
    async def fi_opening(ctx: dict = Depends(fi_route("finance.read"))):
        """建账期初：状态 + 预演（还不知道要录什么时先看它）。"""
        return ok_shell({"status": d.opening.status(), "preview": d.opening.preview()})

    @app.post("/erp/supply-chain/v1/fi/opening")
    async def fi_register_opening(request: Request,
                                  ctx: dict = Depends(fi_route("finance.write"))):
        """登记期初余额单（只出待确认提案）。

        只在空账上可录：车道强制，事后插入会改写全部历史报表；确需在有分录入账时补录，
        必须显式 `allowWithEntries` + 理由（受权偏离要留痕）。
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        unknown = sorted(set(body) - {"lines", "asOf", "allowWithEntries", "reason"})
        if unknown:
            raise ApiError("INVALID_ARGUMENT", f"未知字段：{'、'.join(unknown)}",
                           detail={"allowed": ["lines", "asOf", "allowWithEntries",
                                               "reason"]})
        return ok_shell(d.opening.register(
            body.get("lines") or [], as_of=body.get("asOf"),
            allow_with_entries=bool(body.get("allowWithEntries")),
            reason=body.get("reason"), submitted_by=ctx["staff"]))

    @app.get("/erp/supply-chain/v1/fi/reports/closing-preview")
    async def fi_closing_preview(request: Request,
                                 ctx: dict = Depends(fi_route("finance.read"))):
        """期末结转预演：结哪些科目、净额多少、搬进权益多少（不写账）。"""
        return ok_shell(d.closing.preview(_period(request)))

    @app.post("/erp/supply-chain/v1/fi/closing/{period}/transfer")
    async def fi_closing_transfer(period: str,
                                  ctx: dict = Depends(fi_route("finance.write"))):
        """提交结转提案（不代确认）：同期间幂等，已关账期间拒绝穿入。"""
        return ok_shell(d.closing.transfer(period, submitted_by=ctx["staff"]))

    @app.post("/erp/supply-chain/v1/fi/periods/{period}/close")
    async def fi_close_period(period: str, request: Request,
                              ctx: dict = Depends(fi_route("finance.confirm"))):
        """关账：挡住该期间后续落账。

        权限用 `finance.confirm` 而不是 write——关账是时间轴上的不可逆动作，
        与「登记一张票」不是同一类事。顺序关账由内核判，这里只转发。
        """
        who = _human(ctx["staff"])
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        return ok_shell(d.periods.close(period, actor=who, note=body.get("note")))

    @app.post("/erp/supply-chain/v1/fi/periods/{period}/reopen")
    async def fi_reopen_period(period: str, request: Request,
                               ctx: dict = Depends(fi_route("finance.confirm"))):
        """重开已关期间。`reason` 必填——放开一个已封的期间必须留下为什么。"""
        who = _human(ctx["staff"])
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise ApiError("INVALID_ARGUMENT", "重开必须带理由（reason）",
                           detail={"required": "reason"})
        return ok_shell(d.periods.reopen(period, actor=who, reason=reason))

    @app.get("/erp/supply-chain/v1/fi/periods/{period}/report")
    async def fi_period_report(period: str,
                               ctx: dict = Depends(fi_route("finance.read"))):
        """关账快照复算：这一期封账那一刻的数与现在重算的数。"""
        return ok_shell(d.periods.report(period))

    # ---- 提案收件箱与拍板（确认门缺的那一侧）----

    def _human(role: str) -> str:
        """拍板/认证的经办人：取财务身份头，机器不得自证。"""
        who = role.strip()
        if not who or who.lower() in PROGRAM_IDENTITIES:
            raise ApiError(
                "INVALID_ARGUMENT",
                f"须人工拍板：{who or '（空）'} 不得代替人确认提案"
                "（确认是这一笔分录成为事实的唯一入口）",
                detail={"provided": who})
        return who

    @app.get("/erp/supply-chain/v1/fi/proposals")
    async def fi_proposals(request: Request,
                           ctx: dict = Depends(fi_route("finance.read"))):
        """提案收件箱：默认全部状态，`status` 可收窄到 pending/confirmed/rejected。"""
        status = request.query_params.get("status")
        return ok_shell(proposal_rows(d.kernel.list_proposals(status=status)))

    @app.get("/erp/supply-chain/v1/fi/proposals/{proposal_id}")
    async def fi_proposal(request: Request, proposal_id: int,
                          ctx: dict = Depends(fi_route("finance.read"))):
        rows = proposal_rows(d.kernel.list_proposals())
        hit = next((r for r in rows if int(r.get("id") or 0) == proposal_id), None)
        if hit is None:
            raise ApiError("NOT_FOUND", "提案不存在",
                           detail={"proposal_id": proposal_id})
        return ok_shell(hit)

    @app.post("/erp/supply-chain/v1/fi/proposals/{proposal_id}/confirm")
    async def fi_confirm(request: Request, proposal_id: int,
                         ctx: dict = Depends(fi_route("finance.confirm"))):
        """确认提案 → 它成为账本事实。这是唯一入口，所以权限独立、且必须人工。"""
        return ok_shell(d.kernel.confirm_proposal(
            proposal_id=proposal_id, confirmed_by=_human(ctx["staff"])))

    @app.post("/erp/supply-chain/v1/fi/proposals/{proposal_id}/reject")
    async def fi_reject(request: Request, proposal_id: int,
                        ctx: dict = Depends(fi_route("finance.confirm"))):
        """退回提案。`reason` 必填——不带理由的退回等于没退回，提出方不知道要改什么。"""
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise ApiError("INVALID_ARGUMENT", "退回必须带理由（reason）",
                           detail={"required": "reason"})
        return ok_shell(d.kernel.reject_proposal(
            proposal_id=proposal_id, reason=reason,
            rejected_by=_human(ctx["staff"])))

    # ---- 发票台账（录入与查询；抵扣判断在台账里，这一层只投影）----

    @app.get("/erp/supply-chain/v1/fi/invoices")
    async def fi_invoices(request: Request,
                          ctx: dict = Depends(fi_route("finance.read"))):
        """列出票据；`direction` 可收窄到 sale / purchase。"""
        direction = request.query_params.get("direction")
        return ok_shell(d.cn_invoices.invoices(direction=direction))

    @app.get("/erp/supply-chain/v1/fi/invoices/deduction")
    async def fi_invoice_deduction(request: Request,
                                   ctx: dict = Depends(fi_route("finance.read"))):
        """本期进项三档：可抵 / 待认证 / 不可抵（税额合计 + 逐票）。"""
        return ok_shell(d.cn_invoices.deduction(_period(request)))

    @app.get("/erp/supply-chain/v1/fi/invoices/reconciliation")
    async def fi_invoice_reconciliation(request: Request,
                                        ctx: dict = Depends(fi_route("finance.read"))):
        """账上 2221.01.02 借方发生额 vs 台账可抵税额。

        差额通常是手工直接记进项科目的金额——它不在台账里，无法证明可抵。
        """
        return ok_shell(d.cn_invoices.reconcile(_period(request)))

    @app.get("/erp/supply-chain/v1/fi/reports/invoice-summary")
    async def fi_invoice_summary(request: Request,
                                 ctx: dict = Depends(fi_route("finance.read"))):
        """台账口径的期间汇总。"""
        return ok_shell(d.cn_invoices.filing_summary(_period(request)))

    @app.get("/erp/supply-chain/v1/fi/invoices/{number}")
    async def fi_invoice(request: Request, number: str,
                         ctx: dict = Depends(fi_route("finance.read"))):
        row = d.cn_invoices.invoice(number)
        if row is None:
            raise ApiError("NOT_FOUND", "发票不存在", detail={"number": number})
        return ok_shell(row)

    @app.post("/erp/supply-chain/v1/fi/invoices")
    async def fi_register_invoice(request: Request,
                                  ctx: dict = Depends(fi_route("finance.write"))):
        """登记一张发票。

        票种与税额由台账校验：票种是封闭集，`tax` 与「金额 × 税率」不符即拒——
        票面与台账不符是最难查的错误，宁可拒也不记账。
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        unknown = sorted(set(body) - {"direction", "kind", "number", "net", "rate",
                                      "tax", "counterparty", "issuedOn"})
        if unknown:
            # 不把请求体原样透传：驼峰键与台账的 snake_case 形参一撞就是 500，
            # 而 500 会把「字段名写错」伪装成「服务坏了」。名单之外一律点名拒绝。
            raise ApiError("INVALID_ARGUMENT", f"未知字段：{'、'.join(unknown)}",
                           detail={"allowed": ["direction", "kind", "number", "net",
                                               "rate", "tax", "counterparty",
                                               "issuedOn"]})
        return ok_shell(d.cn_invoices.register(
            body.get("direction"), body.get("kind"), body.get("number"),
            body.get("net"), rate=body.get("rate", "13"), tax=body.get("tax"),
            counterparty=body.get("counterparty"),
            issued_on=body.get("issuedOn"), submitted_by=ctx["staff"]))

    @app.post("/erp/supply-chain/v1/fi/invoices/{number}/certify")
    async def fi_certify_invoice(request: Request, number: str,
                                 ctx: dict = Depends(fi_route("finance.write"))):
        """记录勾选认证（人工事实）。

        经办人取财务身份头，**不许 system/agent**：认证是人工事实，系统不得自证抵扣。
        """
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        return ok_shell(d.cn_invoices.certify(
            number, certified_on=body.get("certifiedOn"), by=ctx["staff"]))

    @app.post("/erp/supply-chain/v1/fi/invoices/red")
    async def fi_register_red(request: Request,
                              ctx: dict = Depends(fi_route("finance.write"))):
        """登记红字票冲减蓝字原票。红字票是独立票据（自己的号码与日期），原票不动。"""
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        # 红字票的票种与税率**由原票决定**（台账从 redOf 那张票推税额），
        # 所以这里不收 rate/tax——收进来只会让人以为能改，实际会被忽略。
        unknown = sorted(set(body) - {"direction", "kind", "number", "net", "redOf",
                                      "reason", "issuedOn"})
        if unknown:
            raise ApiError("INVALID_ARGUMENT", f"未知字段：{'、'.join(unknown)}",
                           detail={"allowed": ["direction", "kind", "number", "net",
                                               "redOf", "reason", "issuedOn"]})
        return ok_shell(d.cn_invoices.register_red(
            body.get("direction"), body.get("kind"), body.get("number"),
            body.get("net"), red_of=body.get("redOf"),
            reason=body.get("reason"), issued_on=body.get("issuedOn"),
            submitted_by=ctx["staff"]))

    @app.post("/erp/supply-chain/v1/fi/invoices/{number}/post")
    async def fi_post_invoice(request: Request, number: str,
                              ctx: dict = Depends(fi_route("finance.write"))):
        """用已登记的进项票过账：只提待确认提案，不直接写分录。

        按台账判抵扣（不可抵的税进成本）——判断在台账里，这里只发起。
        """
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        unknown = sorted(set(body) - {"ref", "amount", "costAccount", "settleAccount",
                                      "gross", "bookedOn"})
        if unknown:
            raise ApiError("INVALID_ARGUMENT", f"未知字段：{'、'.join(unknown)}",
                           detail={"allowed": ["ref", "amount", "costAccount",
                                               "settleAccount", "gross", "bookedOn"]})
        ref = str(body.get("ref") or f"invoice:{number}").strip()
        return ok_shell(d.cn_invoices.purchase(
            ref, number, body.get("amount"),
            cost_account=str(body.get("costAccount") or "5000"),
            settle_account=str(body.get("settleAccount") or "2000"),
            gross=bool(body.get("gross", True)), booked_on=body.get("bookedOn"),
            submitted_by=ctx["staff"]))

    @app.post("/erp/supply-chain/v1/fi/invoices/{number}/post-red")
    async def fi_post_red(request: Request, number: str,
                          ctx: dict = Depends(fi_route("finance.write"))):
        """红字销售票过账（与现销逐行反向），同样只提提案。"""
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        unknown = sorted(set(body) - {"ref", "amount", "revenueAccount",
                                      "settleAccount", "bookedOn"})
        if unknown:
            raise ApiError("INVALID_ARGUMENT", f"未知字段：{'、'.join(unknown)}",
                           detail={"allowed": ["ref", "amount", "revenueAccount",
                                               "settleAccount", "bookedOn"]})
        ref = str(body.get("ref") or f"red:{number}").strip()
        return ok_shell(d.cn_invoices.post_red(
            ref, number, body.get("amount"),
            revenue_account=str(body.get("revenueAccount") or "4000"),
            settle_account=str(body.get("settleAccount") or "1100"),
            booked_on=body.get("bookedOn"), submitted_by=ctx["staff"]))

    @app.get("/erp/supply-chain/v1/fi/reports/surcharges")
    async def fi_surcharges(request: Request,
                            ctx: dict = Depends(fi_route("finance.read"))):
        """附加税预演：只算不写。

        走 `preview=True`——读接口绝不在账上留提案。「这期要缴多少」是看一眼的事，
        看一眼就生成两笔待确认提案，是谁点的、点两次会不会重复，事后都说不清。
        """
        return ok_shell(d.cn_filing.surcharges(_period(request), preview=True))

    @app.get("/erp/supply-chain/v1/fi/reports/income-tax-prepay")
    async def fi_income_tax_prepay(request: Request,
                                   ctx: dict = Depends(fi_route("finance.read"))):
        """所得税预缴预演：只算不写（与附加税同一纪律）。"""
        return ok_shell(d.cn_filing.income_tax_prepay(_period(request), preview=True))

    @app.get("/erp/supply-chain/v1/fi/reports/filing-summary")
    async def fi_filing_summary(request: Request,
                                ctx: dict = Depends(fi_route("finance.read"))):
        """三税一张表：增值税 + 附加税 + 所得税预缴，并与账面逐项对比。"""
        return ok_shell(d.cn_filing.filing_summary(_period(request)))

    @app.post("/erp/supply-chain/v1/fi/filings/vat-return")
    async def fi_register_vat_return(request: Request,
                                     ctx: dict = Depends(fi_route("finance.write"))):
        """登记某期间申报数（电子税局实际报出去的数）。

        写动作，权限码独立于 `finance.read`：能看申报表不等于能改申报数。
        `source` 由申报车道强制必填——申报是外部事实，无出处无法复核。
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        period = body.pop("period", None)
        return ok_shell(d.cn_filing.register_vat_return(period, **body))

    @app.get("/erp/supply-chain/v1/fi/reports/vat-return")
    async def fi_vat_return(request: Request,
                            ctx: dict = Depends(fi_route("finance.read"))):
        """增值税申报口径 vs 账面口径 + 进项三档。

        走申报车道的 `vat_reconciliation`，而不是直接调报表：**申报数**要从车道里
        已登记的拿、**进项三档**要把发票台账递进去。这两样都是「调用方忘了传就静默
        少半张表」的形态（报表照样返回、`provided=false`、`inputTiers` 干脆没有），
        所以供给方在车道里定死，端点不需要记得。

        未登记申报数时仍按账面视同申报，`provided=false` 且口径说明写明「不能当作已核对」。
        """
        return ok_shell(d.cn_filing.vat_reconciliation(_period(request)))

    # ---- 账龄与坏账准备（催收与期末减值；同一套 FIFO 账龄实现）----

    def _aging_kwargs(request: Request) -> dict[str, Any]:
        """`asOf` / `dimension` / `buckets` 的解析：只做形状转换，不在这一层判口径。

        `buckets` 用逗号分隔（`30,60,90`）——账龄分界是客户习惯不同的东西，
        写死三档会让某些客户看到的表永远不是他们习惯的切法。
        """
        out: dict[str, Any] = {}
        as_of = (request.query_params.get("asOf") or "").strip()
        if as_of:
            out["as_of"] = as_of
        dimension = (request.query_params.get("dimension") or "").strip()
        if dimension:
            out["dimension"] = dimension
        raw = (request.query_params.get("buckets") or "").strip()
        if raw:
            try:
                out["buckets"] = tuple(int(p) for p in raw.split(",") if p.strip())
            except ValueError:
                raise ApiError("INVALID_ARGUMENT",
                               f"buckets 须为逗号分隔的正整数：{raw!r}",
                               detail={"example": "30,60,90"}) from None
        return out

    @app.get("/erp/supply-chain/v1/fi/receivables/aging")
    async def fi_receivable_aging(request: Request,
                                  ctx: dict = Depends(fi_route("finance.read"))):
        """应收账龄：按往来对象 × 账龄桶，FIFO 核销。

        原样带出车道自己的自证（`consistent` 与不平时的 `error`）——**不在这里调桶凑平**。
        收多了的对象单列 `advanceReceipt`，不摊给任何桶。
        """
        return ok_shell(d.receivables.report(**_aging_kwargs(request)))

    @app.get("/erp/supply-chain/v1/fi/payables/aging")
    async def fi_payable_aging(request: Request,
                               ctx: dict = Depends(fi_route("finance.read"))):
        """应付账龄：与应收共用同一份实现，只是开放方向相反。"""
        return ok_shell(d.payables.report(**_aging_kwargs(request)))

    @app.get("/erp/supply-chain/v1/fi/bad-debts/schedule")
    async def fi_bad_debt_schedule(ctx: dict = Depends(fi_route("finance.read"))):
        """当前生效的分档费率（含 `registered=false`：还没人登记过）。"""
        return ok_shell(d.bad_debts.schedule())

    @app.post("/erp/supply-chain/v1/fi/bad-debts/schedule")
    async def fi_set_bad_debt_schedule(request: Request,
                                       ctx: dict = Depends(fi_route("finance.write"))):
        """登记分档费率（人工 only，追加式修订）。

        经办人取财务身份头，`system`/`agent` 由车道拒——费率是**会计估计**，
        要写进报表附注对审计解释，机器不得自设。
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        unknown = sorted(set(body) - {"bands"})
        if unknown:
            raise ApiError("INVALID_ARGUMENT", f"未知字段：{'、'.join(unknown)}",
                           detail={"allowed": ["bands"]})
        return ok_shell(d.bad_debts.set_schedule(body.get("bands") or [],
                                                 registered_by=ctx["staff"]))

    @app.get("/erp/supply-chain/v1/fi/bad-debts/status")
    async def fi_bad_debt_status(request: Request,
                                 ctx: dict = Depends(fi_route("finance.read"))):
        """准备余额 / 应收净额 / 覆盖率 / 分档表（`asOf` 缺省取今天）。"""
        as_of = (request.query_params.get("asOf") or "").strip() or None
        return ok_shell(d.bad_debts.status(as_of=as_of))

    @app.get("/erp/supply-chain/v1/fi/reports/bad-debt-preview")
    async def fi_bad_debt_preview(request: Request,
                                  ctx: dict = Depends(fi_route("finance.read"))):
        """计提预演：只算不写。

        与附加税/所得税预演同一纪律——看一眼就生成一笔待确认提案，是谁点的、
        点两次会不会重复，事后都说不清。真计提会被什么挡住（跨期调整 / 须先作废 /
        noop）以 `blockers` 报出，而不是抛错。
        """
        period = _period(request)
        as_of = (request.query_params.get("asOf") or "").strip() or None
        return ok_shell(d.bad_debts.provision(period, as_of=as_of, preview=True))
    @app.post("/erp/supply-chain/v1/fi/bad-debts/{period}/accrue")
    async def fi_accrue_bad_debt(period: str,
                                 ctx: dict = Depends(fi_route("finance.write"))):
        """提交某期间计提（只出提案，仍要过确认门）。"""
        return ok_shell(d.bad_debts.provision(period, submitted_by=ctx["staff"]))

    @app.post("/erp/supply-chain/v1/fi/bad-debts/{period}/void")
    async def fi_void_bad_debt(period: str, request: Request,
                               ctx: dict = Depends(fi_route("finance.write"))):
        """作废某期尚未确认的计提。`reason` 必填——不带理由的重算等于偷改估计。"""
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise ApiError("INVALID_ARGUMENT", "作废必须带理由（reason）",
                           detail={"required": "reason"})
        return ok_shell(d.bad_debts.void(period, reason=reason,
                                         voided_by=ctx["staff"]))

    # ---- 固定资产台账与折旧（本轮：台账 + 门槛 + 月度折旧）----

    @app.get("/erp/supply-chain/v1/fi/assets")
    async def fi_assets(ctx: dict = Depends(fi_route("finance.read"))):
        """固定资产清单：每项原值 / 残值 / 已提 / 净值 / 剩余月数。"""
        return ok_shell(d.assets.assets())

    @app.get("/erp/supply-chain/v1/fi/assets/summary")
    async def fi_asset_summary(ctx: dict = Depends(fi_route("finance.read"))):
        """台账摘要：规模 + 资本化门槛 + 折旧方法与台账归属口径。

        门槛放在这里一起返回，而不是单开一个 GET：**没有门槛**（`None`）与
        「门槛是 0」是两件事，摘要里一眼能分辨，单独一个 GET 只会多一次往返。
        """
        return ok_shell(d.assets.summary())

    @app.post("/erp/supply-chain/v1/fi/assets/threshold")
    async def fi_set_asset_threshold(request: Request,
                                     ctx: dict = Depends(fi_route("finance.write"))):
        """登记资本化门槛（人工 only）。

        `amount` 键**必须显式出现**：`null` 才表示「取消门槛」。少写一个键就把门槛
        悄悄清掉，会让此后所有低值支出都被资本化，而没有人会注意到闸没了。
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        unknown = sorted(set(body) - {"amount", "currency"})
        if unknown:
            raise ApiError("INVALID_ARGUMENT", f"未知字段：{'、'.join(unknown)}",
                           detail={"allowed": ["amount", "currency"]})
        if "amount" not in body:
            raise ApiError("INVALID_ARGUMENT",
                           "必须显式给出 amount（null 表示取消门槛）",
                           detail={"required": "amount", "example": {"amount": "5000"}})
        return ok_shell(d.assets.set_threshold(
            body.get("amount"), currency=body.get("currency") or "CNY",
            approved_by=ctx["staff"]))

    @app.get("/erp/supply-chain/v1/fi/reports/depreciation-preview")
    async def fi_depreciation_preview(request: Request,
                                      ctx: dict = Depends(fi_route("finance.read"))):
        """折旧预演：只算不写。

        已计提过的期间照样给数（可提额为 0 并说明原因），不拿「已计提」把人挡在门外。
        """
        return ok_shell(d.assets.depreciate(_period(request), preview=True))

    @app.post("/erp/supply-chain/v1/fi/assets/depreciation/{period}")
    async def fi_depreciate(period: str, request: Request,
                            ctx: dict = Depends(fi_route("finance.write"))):
        """提交某期间折旧（只出提案，仍要过确认门）。

        `units` 只有工作量法的资产需要；缺了由车道点名拒，不猜产量。
        """
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        unknown = sorted(set(body) - {"units"})
        if unknown:
            raise ApiError("INVALID_ARGUMENT", f"未知字段：{'、'.join(unknown)}",
                           detail={"allowed": ["units"]})
        return ok_shell(d.assets.depreciate(period, units=body.get("units") or {},
                                            submitted_by=ctx["staff"]))

    @app.post("/erp/supply-chain/v1/fi/assets/depreciation/{period}/void")
    async def fi_void_depreciation(period: str, request: Request,
                                   ctx: dict = Depends(fi_route("finance.write"))):
        """作废某期折旧。`reason` 必填——退回理由就是审计线索。"""
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise ApiError("INVALID_ARGUMENT", "作废计提须说明原因（reason）",
                           detail={"required": "reason"})
        return ok_shell(d.assets.void(period, reason=reason,
                                      voided_by=ctx["staff"]))

    @app.post("/erp/supply-chain/v1/fi/assets")
    async def fi_register_asset(request: Request,
                                ctx: dict = Depends(fi_route("finance.write"))):
        """登记一项固定资产（受资本化门槛约束；同标识幂等）。

        门槛在这里是**闸**：低于门槛直接拒并指明应费用化——把「多小就不该进固定资产」
        这条政策判断留在车道里，而不是让登记的人自己记得。
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        allowed = ["assetId", "name", "cost", "acquiredOn", "usefulLifeMonths",
                   "salvage", "currency", "category", "expenseAccount", "method",
                   "unitsTotal"]
        unknown = sorted(set(body) - set(allowed))
        if unknown:
            raise ApiError("INVALID_ARGUMENT", f"未知字段：{'、'.join(unknown)}",
                           detail={"allowed": allowed})
        return ok_shell(d.assets.register(
            body.get("assetId"), body.get("name"),
            cost=body.get("cost"), acquired_on=body.get("acquiredOn"),
            useful_life_months=body.get("usefulLifeMonths"),
            salvage=body.get("salvage", 0),
            currency=body.get("currency") or "CNY",
            category=body.get("category"),
            expense_account=body.get("expenseAccount") or "5200",
            method=body.get("method"), units_total=body.get("unitsTotal"),
            registered_by=ctx["staff"]))

    @app.get("/erp/supply-chain/v1/fi/assets/{asset_id}")
    async def fi_asset(asset_id: str,
                       ctx: dict = Depends(fi_route("finance.read"))):
        """单项资产（含累计折旧与净值）。不存在 → `NOT_FOUND`，不返回零对象。"""
        row = next((r for r in d.assets.assets() if r["assetId"] == asset_id), None)
        if row is None:
            raise ApiError("NOT_FOUND", "资产不存在", detail={"assetId": asset_id})
        return ok_shell(row)

    @app.get("/erp/supply-chain/v1/fi/assets/{asset_id}/plan")
    async def fi_asset_plan(asset_id: str,
                            ctx: dict = Depends(fi_route("finance.read"))):
        """完整折旧计划（每期金额 / 累计 / 期末净值 + 是否已计提）。"""
        return ok_shell(d.assets.plan(asset_id))

    @app.get("/erp/supply-chain/v1/fi/assets/{asset_id}/book-value")
    async def fi_asset_book_value(request: Request, asset_id: str,
                                  ctx: dict = Depends(fi_route("finance.read"))):
        """账面净值；`asOf`（YYYY-MM 或 YYYY-MM-DD）可回看某一期末。"""
        as_of = (request.query_params.get("asOf") or "").strip() or None
        return ok_shell(d.assets.book_value(asset_id, as_of=as_of))

    # ---- 月结清单（一串有顺序的动作，不是 48 个按钮）----

    @app.get("/erp/supply-chain/v1/fi/month-end/{period}")
    async def fi_month_end(period: str,
                           ctx: dict = Depends(fi_route("finance.read"))):
        """月结清单：逐项判据 + 期间内待确认提案 + 下一步该由谁做（只读）。"""
        return ok_shell(d.month_end.checklist(period))

    @app.post("/erp/supply-chain/v1/fi/month-end/{period}/run")
    async def fi_month_end_run(period: str,
                               ctx: dict = Depends(fi_route("finance.write"))):
        """按顺序准备（结转成本 → 折旧 → 减值 → 结转损益），只出待确认提案。

        **不代任何人过门**，也不带 `partial` 逃生阀：成本未知时整期拒是设计——
        结转一个偏小的营业成本会让毛利虚高，而报表上看不出来。
        """
        return ok_shell(d.month_end.run(period, actor=ctx["staff"]))

    @app.post("/erp/supply-chain/v1/fi/month-end/{period}/close")
    async def fi_month_end_close(period: str,
                                 ctx: dict = Depends(fi_route("finance.confirm"))):
        """准备并关账（`finance.confirm`）：关账是时间轴上的不可逆动作。

        前置没满足就关不上：各步必须都成功，且期间内**没有任何待确认提案**——
        有待办时关账会让「还没人看过的一笔分录」变成既成事实。
        """
        who = _human(ctx["staff"])
        return ok_shell(d.month_end.run(period, actor=who, close=True))

    # ---- 三本账（账 → 凭证；与三张报表同期间口径）----

    @app.get("/erp/supply-chain/v1/fi/books/balances")
    async def fi_book_balances(request: Request,
                               ctx: dict = Depends(fi_route("finance.read"))):
        """科目余额表（三栏）。不平时如实带 `error`——不调行凑平。"""
        return ok_shell(d.books.account_balances(_period(request)))

    @app.get("/erp/supply-chain/v1/fi/books/ledger/{account_code}")
    async def fi_subsidiary_ledger(request: Request, account_code: str,
                                   ctx: dict = Depends(fi_route("finance.read"))):
        """明细账：逐笔流水 + 逐行余额。

        `limit` 只截**显示行**，合计与期末仍按全量算——否则「最后一行的余额」会骗人。
        """
        raw = (request.query_params.get("limit") or "").strip()
        try:
            limit = int(raw) if raw else 0
        except ValueError:
            raise ApiError("INVALID_ARGUMENT", f"limit 须为整数：{raw!r}") from None
        if limit < 0:
            raise ApiError("INVALID_ARGUMENT", f"limit 不能为负：{limit}")
        return ok_shell(d.books.subsidiary_ledger(
            account_code, _period(request), limit=limit))

    @app.get("/erp/supply-chain/v1/fi/books/journal")
    async def fi_journal(request: Request,
                         ctx: dict = Depends(fi_route("finance.read"))):
        """序时账：按凭证汇总，逐凭证一行。

        `txnId` 只能从明细账行里捡到时，「先知道要查哪个科目」成了前提；序时账把这一段
        期间发生了哪些业务按凭证摊开，是查账的起点。`limit` 只截显示行。
        """
        raw = (request.query_params.get("limit") or "").strip()
        try:
            limit = int(raw) if raw else 0
        except ValueError:
            raise ApiError("INVALID_ARGUMENT", f"limit 须为整数：{raw!r}") from None
        if limit < 0:
            raise ApiError("INVALID_ARGUMENT", f"limit 不能为负：{limit}")
        return ok_shell(d.books.journal(_period(request), limit=limit))

    @app.get("/erp/supply-chain/v1/fi/books/voucher/{txn_id}")
    async def fi_voucher(txn_id: str,
                         ctx: dict = Depends(fi_route("finance.read"))):
        """单张凭证：全部借贷行 + 合计 + 平衡断言 + `isClosing`。

        这是「账」与「表」之间唯一不可替代的一步：报表说这一期费用 12 万、明细账说某天
        有一笔 3000 挂着某个 `txnId`，而那 3000 借的是费用还是预付——只有凭证能答。
        """
        return ok_shell(d.books.voucher(txn_id))

    @app.get("/erp/supply-chain/v1/fi/books/party-statement")
    async def fi_party_statement(request: Request,
                                 ctx: dict = Depends(fi_route("finance.read"))):
        """往来对账单：某客户/供应商的跨科目明细，指定 `accounts` 时对回总账。

        归属只认显式维度（`dimensions[dimension]`）——靠科目名或摘要文字猜归属，
        迟早把两家的往来记到一家头上。
        """
        params = request.query_params
        raw_accounts = (params.get("accounts") or "").strip()
        accounts = [a.strip() for a in raw_accounts.split(",") if a.strip()]
        return ok_shell(d.books.party_statement(
            params.get("dimension"), params.get("value"), _period(request),
            accounts=accounts or None))

    # ---- 成本层与期末结转（月结清单第一步的料）----

    @app.get("/erp/supply-chain/v1/fi/cost/replay")
    async def fi_cost_replay(ctx: dict = Depends(fi_route("finance.read"))):
        """成本层重放：方法/暂估口径 + 结存 + 每笔出库成本 + 缺口 + 逐层明细。

        缺口（`gaps`）是重点：它是「成本未知」的事实清单，不是错误——有缺口时
        结存会标 `basis=unknown`，而出库成本会是 `null` 而不是 0。
        """
        return ok_shell(d.cost.replay())

    @app.get("/erp/supply-chain/v1/fi/cost/issue-price")
    async def fi_cost_issue_price(request: Request,
                                  ctx: dict = Depends(fi_route("finance.read"))):
        """这一次出库按当前口径值多少（只读，不记账）。

        取不到成本就拒——**不返回 0**：一次被算成 0 的出库成本会永久抬高毛利，
        而报表上看不出来。
        """
        p = request.query_params
        return ok_shell(d.cost.issue_cost(
            p.get("sku"), p.get("qty"), warehouse=p.get("warehouse"),
            lot=p.get("lot")))

    @app.get("/erp/supply-chain/v1/fi/reports/cogs-preview")
    async def fi_cogs_preview(request: Request,
                              ctx: dict = Depends(fi_route("finance.read"))):
        """结转预演：只算不写。真结转会被什么挡住（成本未知/无已计价/金额变化/已结转过）
        以 `blockers` 报出，而不是抛错。"""
        return ok_shell(d.cost.post_cogs(_period(request), preview=True))

    @app.post("/erp/supply-chain/v1/fi/cost/cogs/{period}")
    async def fi_post_cogs(period: str,
                           ctx: dict = Depends(fi_route("finance.write"))):
        """提交本期出库成本结转（只出提案：借 5000 / 贷 1200）。

        不开放 `partial`：只结已计价的会让营业成本偏小、毛利虚高，而报表上「看起来合理」。
        要结转就先把成本补齐。
        """
        return ok_shell(d.cost.post_cogs(period, submitted_by=ctx["staff"]))

    @app.post("/erp/supply-chain/v1/fi/cost/standard")
    async def fi_set_standard_cost(request: Request,
                                   ctx: dict = Depends(fi_route("finance.write"))):
        """登记标准成本（人工 only）。标准成本是会计估计，程序不得自设。"""
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        unknown = sorted(set(body) - {"sku", "unitCost", "currency"})
        if unknown:
            raise ApiError("INVALID_ARGUMENT", f"未知字段：{'、'.join(unknown)}",
                           detail={"allowed": ["sku", "unitCost", "currency"]})
        return ok_shell(d.cost.set_standard_cost(
            body.get("sku"), body.get("unitCost"),
            currency=body.get("currency"), approved_by=ctx["staff"]))

    # ---- 补充资料（间接法）与外币期末调汇 ----
    # change: add-fi-indirect-and-fx-endpoints。
    # 两条能力在模板层都已经交付（`cn_statements.cash_flow_indirect`、
    # `FxRevaluation` 六个方法），但此前 REST 面**一个入口都没有**——覆盖矩阵标
    # `delivered`、对外也宣称「期末调汇已交付」，客户却调不到。这类缝没有任何东西会红，
    # 所以这一节的意义不只是「多七条路由」，是把「已交付」与「能调到」接上。

    @app.get("/erp/supply-chain/v1/fi/reports/cash-flow-indirect")
    async def fi_cash_flow_indirect(request: Request,
                                    ctx: dict = Depends(fi_route("finance.read"))):
        """补充资料（间接法）：净利润 → 加回非现金 → 营运资本变动 → 经营净额。

        与 `/fi/reports/cash-flow` 的**本准则**直接法互证；对不上就报差额，不调平。
        """
        return ok_shell(d.cn_statements.cash_flow_indirect(_period(request)))

    @app.get("/erp/supply-chain/v1/fi/fx/rates")
    async def fi_fx_rates(request: Request,
                          ctx: dict = Depends(fi_route("finance.read"))):
        """该期间生效的汇率（`kind`：`closing` 期末 / `average` 期间平均）。"""
        kind = (request.query_params.get("kind") or "").strip() or "closing"
        return ok_shell(d.fx_revaluation.rates(_period(request), kind=kind))

    @app.post("/erp/supply-chain/v1/fi/fx/rates")
    async def fi_register_fx_rates(request: Request,
                                   ctx: dict = Depends(fi_route("finance.write"))):
        """登记某期间的汇率（**人工 only**，`source` 必填）。

        汇率是**外部事实**：无出处就无法复核，所以 `source` 是必填项而不是可选备注；
        程序身份由这里拒掉——与坏账费率、资本化门槛同一条纪律（外部事实不得由系统自证）。
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        unknown = sorted(set(body) - {"period", "rates", "source", "kind"})
        if unknown:
            raise ApiError("INVALID_ARGUMENT", f"未知字段：{'、'.join(unknown)}",
                           detail={"allowed": ["period", "rates", "source", "kind"]})
        return ok_shell(d.fx_revaluation.register_rates(
            body.get("period"), body.get("rates") or {},
            source=body.get("source"), kind=body.get("kind") or "closing",
            registered_by=_human(ctx["staff"])))

    @app.get("/erp/supply-chain/v1/fi/fx/status/{period}")
    async def fi_fx_status(period: str,
                           ctx: dict = Depends(fi_route("finance.read"))):
        """本期调汇状态（`not_revalued`/`pending`/`posted`/`rejected`）。

        状态只看提案；**是否还需调汇看 preflight 的差额**——汇率被修订时，状态会把它盖住。
        """
        return ok_shell(d.fx_revaluation.status(period))

    @app.get("/erp/supply-chain/v1/fi/reports/fx-revaluation-preview")
    async def fi_fx_preview(request: Request,
                            ctx: dict = Depends(fi_route("finance.read"))):
        """调汇预演：只算不写（逐币种逐科目：外币净额 / 账上本位币 / 重估后 / 差额）。"""
        return ok_shell(d.fx_revaluation.preview(_period(request)))

    @app.post("/erp/supply-chain/v1/fi/fx/revaluation/{period}")
    async def fi_fx_revalue(period: str,
                            ctx: dict = Depends(fi_route("finance.write"))):
        """出调汇提案（科目 ↔ `6603`，借贷必平）。**不直接落账**：过确认门才成为事实。"""
        return ok_shell(d.fx_revaluation.revalue(period,
                                                 submitted_by=_human(ctx["staff"])))


# ---- WH 仓管作业（内部页集 supply-chain.wh：不进 /pagesets 载荷）----
#
# change: add-inventory-operations-lane 任务 3.1/3.2。为什么走**内部**页集而不是新开
# 一个对外页集，见 `pagesets.INTERNAL_PAGESET_KEYS` 的注释：对外页集 key 与 grant 是
# 封闭集、镜像在 paas 客户端注册表里，多吐一个不认识的 key 会让客户端整体 fail-closed，
# 连带四个业务页集一起打掉；而仓管作业又不属于 UE/SE/SP/CP 任何一个角色。
#
# 三条口径（决定这一面怎么写，别按「像别的面那样」改）：
#
# 1. **提交只落拟稿事实**：`receive/issue/transfer/stocktake` 走内核拟稿车道落 pending，
#    不投影、不过账。投影与过账只发生在 `POST /wh/documents/{ref}/confirm`，而那是独立
#    权限档（`inventory.confirm`）——「能开一张单」与「能让这批货成为账面事实」是两件事，
#    后者动的是对象余额。
# 2. **闸序不能换**：页集 → grant → 身份头。与 fi 的差异是**不做人员名单**：仓管有数据
#    范围（仓库来自请求体/查询串），越权由仓库维度的合理缺省兜住；但身份头仍必填，
#    它要进 `requested_by` 留痕，共用 `X-ERP-Staff` 会让留痕指向错误的席位。
# 3. **不回写外部 WMS**：本面只记录我方收下的作业事实；外部库存台账仍归 WMS（边界写在
#    `erp-vertical-supply-chain` 的「仓配与交付编排（执行在外部）」那条需求里）。

#: 仓管经办人身份头（与 `X-ERP-Staff` 分开，见上面第 2 条）
WAREHOUSE_HEADER = "X-ERP-Warehouse"


def register_wh_face(app, settings, d: Drivers, auth: PagesetAuth) -> None:
    """库存作业面：入库 / 出库 / 调拨 / 盘点的提交与查询（11 个端点）。

    行级状态（GET 单张）读**内核 draft 车道的当前状态**，不拿提交时的快照充数：
    拍板之后事件状态会变，而文档档里那一行不会跟着变——用快照报「待确认」就是在报告
    一件已经变了的事。读不到内核时如实标 `unknown` 并在响应里点名，不猜。
    """

    def wh_route(grant: str):
        def _dep(request: Request) -> dict[str, Any]:
            request.state.erp_grant = grant
            require_grant(auth, PAGESET_WH, grant)
            who = request.headers.get(WAREHOUSE_HEADER, "").strip()
            if not who:
                raise ApiError(
                    "AUTH_REQUIRED",
                    f"缺少仓管身份头 {WAREHOUSE_HEADER}"
                    "（WH 身份=仓管员工号；租户头是常量，不能单独当凭据）")
            # 程序身份先拒（不靠名单）：REST 面只接人工经办。将来要接自动化，
            # 得先有服务账号机制（独立凭据 + 独立审计），而不是把引擎名写进人的头。
            if who.lower() in PROGRAM_IDENTITIES:
                raise ApiError(
                    "INVALID_ARGUMENT",
                    f"须人工经办：{who} 不得代表仓管操作"
                    "（REST 面只接人工；自动化需服务账号机制，不复用人的身份头）",
                    detail={"provided": who,
                            "program_identities": sorted(PROGRAM_IDENTITIES)})
            return {"operator": who}
        return _dep

    def _lane(fn, *a, **kw):
        """车道调用：模板层错误归一成 ApiError（保留 code/message/detail）。"""
        try:
            return fn(*a, **kw)
        except TemplateError as exc:
            raise adapt_exception(exc) from exc

    def _payload(body: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        return {k: body[k] for k in keys if body.get(k) not in (None, "")}

    def _list(request: Request, kind: str) -> dict[str, Any]:
        q = request.query_params
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        out = _lane(d.inventory.documents, kind=kind,
                    status=(q.get("status") or "").strip() or None)
        return list_shell(out["rows"], offset=offset, limit=limit,
                          extra={"total": out["total"], "kind": kind,
                                 "caliber_note": (
                                     "单据来自本板块作业档；数量面余额以 "
                                     "query_object_balance 为唯一出处"
                                     "（不是外部 WMS 实时库存）")})

    # ---- 1) 入库单（goods_received；可不挂采购单）----

    @app.post("/erp/supply-chain/v1/wh/receipts")
    async def wh_create_receipt(body: dict[str, Any],
                                ctx: dict = Depends(wh_route("inventory.write"))):
        """提交入库单：逐行落 `goods_received` **拟稿**事实（pending，不投影）。"""
        return ok_shell(_lane(
            d.inventory.receive,
            warehouse=str(body.get("warehouse") or ""),
            lines=body.get("lines") or body.get("items") or [],
            ref=body.get("ref") or None,
            occurred_at=body.get("occurredAt") or body.get("occurred_at") or None,
            requested_by=ctx["operator"],
            note=body.get("note") or None))

    @app.get("/erp/supply-chain/v1/wh/receipts")
    async def wh_list_receipts(request: Request,
                               ctx: dict = Depends(wh_route("inventory.read"))):
        """入库单列表（按发生时间倒序口径由车道给，本层只分页，不重排）。"""
        return _list(request, "receipt")

    # ---- 2) 出库单（goods_issued；先整单核量，不足一行都不写）----

    @app.post("/erp/supply-chain/v1/wh/issues")
    async def wh_create_issue(body: dict[str, Any],
                              ctx: dict = Depends(wh_route("inventory.write"))):
        """提交出库单：按（sku, 仓库, 批次）核可用量，不足**整单拒绝**并点名行号。"""
        return ok_shell(_lane(
            d.inventory.issue,
            warehouse=str(body.get("warehouse") or ""),
            lines=body.get("lines") or body.get("items") or [],
            ref=body.get("ref") or None,
            occurred_at=body.get("occurredAt") or body.get("occurred_at") or None,
            requested_by=ctx["operator"],
            note=body.get("note") or None))

    @app.get("/erp/supply-chain/v1/wh/issues")
    async def wh_list_issues(request: Request,
                             ctx: dict = Depends(wh_route("inventory.read"))):
        return _list(request, "issue")

    # ---- 3) 调拨单（源仓 goods_issued + 目的仓 goods_received 两笔事实）----

    @app.post("/erp/supply-chain/v1/wh/transfers")
    async def wh_create_transfer(body: dict[str, Any],
                                 ctx: dict = Depends(wh_route("inventory.write"))):
        """提交调拨单：同单两笔事实（数量守恒）；同仓调拨按参数错误拒。"""
        return ok_shell(_lane(
            d.inventory.transfer,
            from_warehouse=str(body.get("fromWarehouse")
                               or body.get("from_warehouse") or ""),
            to_warehouse=str(body.get("toWarehouse")
                             or body.get("to_warehouse") or ""),
            lines=body.get("lines") or body.get("items") or [],
            ref=body.get("ref") or None,
            occurred_at=body.get("occurredAt") or body.get("occurred_at") or None,
            requested_by=ctx["operator"],
            note=body.get("note") or None))

    @app.get("/erp/supply-chain/v1/wh/transfers")
    async def wh_list_transfers(request: Request,
                                ctx: dict = Depends(wh_route("inventory.read"))):
        return _list(request, "transfer")

    # ---- 4) 盘点单（只对差异行出 goods_adjusted；账面数读本板块数量面）----

    @app.post("/erp/supply-chain/v1/wh/stocktakes")
    async def wh_create_stocktake(body: dict[str, Any],
                                  ctx: dict = Depends(wh_route("inventory.write"))):
        """提交盘点单：盘盈/盘亏各出一笔事实，**无差异不出事实**。"""
        return ok_shell(_lane(
            d.inventory.stocktake,
            warehouse=str(body.get("warehouse") or ""),
            counts=body.get("counts") or body.get("lines") or [],
            ref=body.get("ref") or None,
            occurred_at=body.get("occurredAt") or body.get("occurred_at") or None,
            requested_by=ctx["operator"],
            note=body.get("note") or None))

    @app.get("/erp/supply-chain/v1/wh/stocktakes")
    async def wh_list_stocktakes(request: Request,
                                 ctx: dict = Depends(wh_route("inventory.read"))):
        return _list(request, "stocktake")

    # ---- 5) 单张详情：逐行状态 + 调拨的「半边已确认」----

    #: 内核 `query_events` 真正用的返回键是 **`items`**（`{items, completeness}`）。
    #: `events`/`data`/`rows` 是历史包法与替身用过的写法，一并认：**一个键名认错，
    #: 整面就会把「已确认」读成「不存在」**——这类错不报错、只报假状态，而单据本身
    #: 读得到（200），现场看起来像「内核里没有这些事实」。同一条坑在
    #: `fd_erp_vertical_templates/cost.py` 记过一次（只认 `events`/`data` ⇒ 对着真内核
    #: 永远看不见任何事件，而单测替身恰好返回 `events`，假绿了整整一轮）。
    _EVENT_ROW_KEYS = ("items", "events", "data", "rows")

    def _event_rows(out: Any) -> list[dict[str, Any]]:
        """从内核查询返回里取行（宽容读键，只认列表形态，不猜标量）。"""
        if isinstance(out, list):
            return [r for r in out if isinstance(r, dict)]
        if not isinstance(out, dict):
            return []
        for key in _EVENT_ROW_KEYS:
            rows = out.get(key)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
        return []

    def _status_of(doc_lines: list[dict[str, Any]]) -> tuple[dict[str, str], str | None]:
        """按 `stable_id` 问内核 draft 车道的**当前**状态。

        返回 (stable_id → status, 读不到时的原因)。读不到就整批标 `unknown`：
        一条「不知道」比一条猜出来的「accepted」安全——后者会让人以为货已经入账。
        """
        wanted = {str(x.get("stable_id") or "") for x in doc_lines}
        by_event_type: dict[str, set[str]] = {}
        for x in doc_lines:
            et = str(x.get("event_type") or "")
            if et:
                by_event_type.setdefault(et, set()).add(str(x.get("stable_id") or ""))
        found: dict[str, str] = {}
        try:
            for et in sorted(by_event_type):
                out = d.kernel.query_events(event_type=et, lane="draft", limit=500)
                for row in _event_rows(out):
                    sid = str(row.get("stable_id") or "")
                    if sid in wanted:
                        found[sid] = str(row.get("status") or "")
        except Exception as exc:  # noqa: BLE001 — 内核不可达/协议变动都归「读不到」
            return {}, f"{type(exc).__name__}: {exc}"
        return found, None

    @app.get("/erp/supply-chain/v1/wh/documents/{ref}")
    async def wh_document(ref: str, ctx: dict = Depends(wh_route("inventory.read"))):
        """单张单据：逐行**当前**状态 + 按侧汇总（调拨要能看见「只有一边确认了」）。"""
        doc = _lane(d.inventory.document, ref)
        lines = doc.get("lines") or []
        status, why = _status_of(lines)
        rows: list[dict[str, Any]] = []
        sides: dict[str, dict[str, Any]] = {}
        for item in lines:
            sid = str(item.get("stable_id") or "")
            st = status.get(sid) or ("unknown" if why else "not_found")
            row = {**item,
                   "event_status_submitted": item.get("event_status"),
                   "event_status": st}
            rows.append(row)
            side = str(item.get("side") or "")
            if not side:
                continue
            bucket = sides.setdefault(side, {"lines": 0, "pending": 0, "accepted": 0,
                                             "rejected": 0, "unknown": 0,
                                             "statuses": []})
            bucket["lines"] += 1
            if st in ("pending", "accepted", "rejected"):
                bucket[st] += 1
            else:
                bucket["unknown"] += 1
            if st not in bucket["statuses"]:
                bucket["statuses"].append(st)
        for bucket in sides.values():
            bucket["statuses"] = sorted(bucket["statuses"])
            bucket["settled"] = bucket["pending"] == 0 and bucket["unknown"] == 0
        # 半边已确认：至少一侧全部定案、而另一侧仍有待确认
        half = (len(sides) > 1
                and any(b["settled"] for b in sides.values())
                and any(not b["settled"] for b in sides.values()))
        out = {**doc, "lines": rows, "sides": sides, "halfConfirmed": half}
        if why:
            out["status_source"] = (f"内核 draft 车道读不到（{why}）——逐行状态标 unknown，"
                                    "不要据此判断是否已确认")
        else:
            out["status_source"] = "内核 draft 车道当前状态（不是提交时的快照）"
        return ok_shell(out)

    # ---- 6) 拍板：过确认门 + 投影 + 过账（独立权限档）----

    @app.post("/erp/supply-chain/v1/wh/documents/{ref}/confirm")
    async def wh_confirm(ref: str, body: dict[str, Any],
                         ctx: dict = Depends(wh_route("inventory.confirm"))):
        """逐行过确认门，再统一投影与过账。

        **逐行**拍板、**逐行**报错：某一行被内核拒（例如数量与维度不符）时其余行照常
        推进，响应里那一行标 `ok: false` 并带内核码——不留「整单都成功」的假象。
        投影只认 accepted 的事实，所以顺序必须是「先全部拍板、再 project」。

        可只拍**一侧**（`side=out|in`）或**一行**（`line=N`）：调拨是两笔事实，某侧失败
        时另一侧不该被重放（`project()` 的 `line`/`side` 正是为此存在）。**筛过的时候
        不 finalize**——只看半边就宣布整单 `confirmed` 会造出一张「已确认」却仍有
        pending 行的单据。

        **`actor` 只能是 `human`**：内核确认门不接受自报身份（`KernelProxy.decide_event`
        会直接拒掉别的 lane/actor），经办人身份留在本层——响应里的 `operator` 与访问日志，
        不写进内核事件。这不是遗漏，是「机器不得代替人拍板」那条纪律的实现面。
        """
        doc = _lane(d.inventory.document, ref)
        if str(doc.get("status")) == "rejected":
            raise ApiError("WORKFLOW_STATE",
                           f"单据已被驳回（{ref}），不可再确认",
                           detail={"ref": ref, "status": doc.get("status")})
        note = str(body.get("note") or "").strip() or None
        want_side = str(body.get("side") or "").strip() or None
        want_line = body.get("line")
        if want_line is not None:
            try:
                want_line = int(want_line)
            except (TypeError, ValueError) as exc:
                raise ApiError("INVALID_ARGUMENT",
                               f"line 必须为整数：{body.get('line')}") from exc
        narrowed = bool(want_side or want_line is not None)
        decided: list[dict[str, Any]] = []
        for item in doc.get("lines") or []:
            if want_side and str(item.get("side") or "") != want_side:
                continue
            if want_line is not None and int(item.get("line") or 0) != want_line:
                continue
            sid = str(item.get("stable_id") or "")
            base = {"line": item.get("line"), "side": item.get("side") or "",
                    "stable_id": sid}
            if str(item.get("event_status")) == "accepted":
                # 幂等：已 accepted 的行走同一条路（decide 是幂等的，这里只是省一次往返）
                decided.append({**base, "ok": True, "status": "accepted",
                                "note": "提交时即 accepted（内核直通）"})
                continue
            try:
                out = d.kernel.decide_event(sid, "accepted", note=note,
                                            lane="draft", actor="human")
            except ApiError as exc:
                decided.append({**base, "ok": False, "code": exc.code,
                                "message": exc.message})
                continue
            decided.append({**base, "ok": True,
                            "status": (out or {}).get("status") or "accepted",
                            "decided_at": (out or {}).get("decided_at")})
        projected = _lane(d.inventory.project, ref, line=want_line,
                          side=want_side, settled_by=ctx["operator"],
                          finalize=not narrowed)
        rejected_lines = [d0 for d0 in decided if not d0.get("ok")]
        return ok_shell({
            "ref": ref,
            "operator": ctx["operator"],
            "decided": decided,
            "decidedOk": len(decided) - len(rejected_lines),
            "decidedFailed": len(rejected_lines),
            "scope": {"side": want_side, "line": want_line,
                      "narrowed": narrowed,
                      "finalized": not narrowed},
            "project": projected,
            "note": ("先逐行过确认门、再投影：被拒的行不会拉平其余行；"
                     "缺过账规则的行在 project.lines[] 里是 posted=false + "
                     "posting_reason，不当成已入账"
                     + ("；本次只拍了筛选出来的那些行，整单状态不推进"
                        if narrowed else "")),
        })

    @app.post("/erp/supply-chain/v1/wh/documents/{ref}/reject")
    async def wh_reject(ref: str, body: dict[str, Any],
                        ctx: dict = Depends(wh_route("inventory.confirm"))):
        """驳回单据：内核侧逐行 rejected，本地单据状态转 rejected（原因必填）。"""
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise ApiError("INVALID_ARGUMENT",
                           "驳回必须写明原因（reason）——后续要据此查为什么没入账")
        doc = _lane(d.inventory.document, ref)
        rejected: list[dict[str, Any]] = []
        for item in doc.get("lines") or []:
            sid = str(item.get("stable_id") or "")
            base = {"line": item.get("line"), "side": item.get("side") or "",
                    "stable_id": sid}
            try:
                # 同上：内核确认门不接受自报经办人，actor 固定 human
                out = d.kernel.decide_event(sid, "rejected", note=reason,
                                            lane="draft", actor="human")
            except ApiError as exc:
                rejected.append({**base, "ok": False, "code": exc.code,
                                 "message": exc.message})
                continue
            rejected.append({**base, "ok": True,
                             "status": (out or {}).get("status") or "rejected"})
        local = _lane(d.inventory.reject, ref, reason=reason,
                      decided_by=ctx["operator"])
        failed = [r for r in rejected if not r.get("ok")]
        return ok_shell({"ref": ref, "operator": ctx["operator"],
                         "rejected": rejected,
                         "rejectedOk": len(rejected) - len(failed),
                         "rejectedFailed": len(failed),
                         "document": local,
                         "note": ("内核侧 rejected 与本地单据状态是两件事："
                                  "本地拒绝不改事件，事件由确认门拒绝")})

