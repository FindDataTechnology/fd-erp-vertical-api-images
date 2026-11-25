"""FastAPI 应用工厂：UE 订货台 19 + SE 业务员台 15 + SP 供应商 13 + CP 承运商
12 契约端点 + pageset 授权端点 + 2 条契约外对账支撑入口（共 63 路由）。

请求闸序（每请求）：
1. 租户闸：X-ERP-Tenant 必带且须等于本进程绑定租户（跨租户 TENANT_DENIED 403）；
2. 页面集闸：页面集 key（supply-chain.ue/se/sp/cp）须在租户授权配置内
   （fail-closed 403）；
3. grant 闸：端点所需权限码（契约权限列）须已授权（403）；
4. 身份/数据域闸：UE=X-ERP-Customer（本租户已建档客户）；SE=X-ERP-Staff
   （业务员工号，归属=客户主数据 sales_rep）；SP=X-ERP-Supplier（供应商档案）；
   CP=X-ERP-Carrier（运单 carrier 域）。越域/未知一律 404，不泄露存在性
   （例外：SE 代客下单归属越权 403 DATA_SCOPE_DENIED，契约明示）。

统一响应壳 {ok, data, error:{code,message,hint,detail}, rows_returned}；
错误码封闭集（errors.ERROR_CATALOG）；分页/体积上限沿内核 result.py 语义。
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from fd_erp_vertical_templates.errors import TemplateError

from .cart import CartStore
from .config import Settings
from .drivers import build_drivers, stock_visibility
from .envelope import error_shell, list_shell, ok_shell, parse_page_params
from .errors import (ApiError, adapt_exception, http_status_for,
                     invoice_degraded)
from .faces import (register_cp_face, register_fi_face, register_se_face,
                    register_sp_face, register_wh_face)
from .kernel_client import build_kernel_client, ensure_kernel_proxy
from .logs import access_log_middleware
from .observability import instrument_middleware
from .pagesets import PagesetAuth, load_pageset_auth, require_grant
from .statements import StatementService
from .storefront import register_storefront_face
from .trade import register_trade_face

PAGESET_UE = "supply-chain.ue"
TENANT_HEADER = "X-ERP-Tenant"
CUSTOMER_HEADER = "X-ERP-Customer"
# 探针路径：不走租户闸（见 tenant_gate 注释）
HEALTH_PATHS = ("/health", "/healthz")
#: 店面公开读面：匿名可读，故不走 `X-ERP-Tenant` 头闸（匿名访客不持任何头）。
#: 这是**显式登记**的例外清单，不是前缀白名单：每条路径带理由，新增公开路由必须
#: 在此登记（change: establish-ai-native-ecommerce-stack 0.3/0.4）。
#: 租户校验不因此消失——店面路由改用**路径参数** `{tenant}`，与本进程绑定租户比对，
#: 不符一律 404（与 UE 侧「不泄露存在性」同口径）。
SHOP_PATHS: dict[str, str] = {
    "/shop/{tenant}/products": "店面商品列表：匿名访客的公开读面",
    "/shop/{tenant}/products/{product_id}": "店面商品详情：匿名访客的公开读面",
    "/shop/{tenant}/orders": "零售下单：匿名访客提交预付单（不占信用）",
    "/shop/{tenant}/orders/{order_ref}": "零售订单查询：凭不透明单号读自己那一单",
    "/shop/{tenant}/orders/{order_ref}/payment": "商家侧到账登记（要商家身份，买家不可达）",
}


def _shop_regex(path: str) -> str:
    """把 `/shop/{tenant}/products/{id}` 编译成匹配一条路径段的正则片段。"""
    return "/".join("[^/]+" if seg.startswith("{") else re.escape(seg)
                    for seg in path.split("/"))


#: **精确**匹配店面公开路由，不用前缀放行——前缀会把未来任何 `/shop/` 下的业务端点
#: 一起豁免出身份闸，那正是「靠路径约定放行」这个坑。新增店面路由必须同时改
#: SHOP_PATHS 与契约表（`check_rest_surface` 守两者一致）。
SHOP_RE = re.compile("^(?:" + "|".join(_shop_regex(p) for p in SHOP_PATHS) + ")$")
# 指标路径：同样不走租户闸。理由与探针同源但不同：/metrics 是**进程级观测面**
# （请求数/耗时/错误码，见 observability.py），不含任何业务数据、不出现租户标识；
# 若过租户闸，集群内抓取器（无业务身份）会一律 401，指标永远无人可读。
METRICS_PATH = "/metrics"
#: 对账入口单侧记录上限：内核对齐在内存里做（单副本进程），防止一次请求打爆
RECON_MAX_RECORDS = 5000
#: 内核不可达时的规划车道（spec erp-operations-hardening：如实 501 + 车道，而非 500）
RECON_KERNEL_LANE = (
    "planned_lane: 内核 run_reconciliation 已接通并优先使用（两源 inline 代理）；"
    "内核不可达期间本入口如实 501，恢复后同一入口自动可用，调用方无需改"
    "（optimize-erp-solution 6.6）")


def _recon_source(side: str, spec: Any) -> dict[str, Any]:
    """对账源声明白名单：只放 inline。

    为什么拒 csv/api：内核适配器支持 `api`（服务端 GET 任意 URL）与 `csv`
    （`file:` 前缀读服务端文件）。那是内核内部工具面的能力，**不是**该 REST
    入口该有的能力——把它透传给客户等于开 SSRF 与本地文件读取。平台连接器
    （金蝶/客户 WMS）走连接器仓车道，也不经这里。
    """
    if not isinstance(spec, dict):
        raise ApiError("INVALID_ARGUMENT", f"{side} 须为对象（源声明）",
                       detail={"side": side})
    kind = str(spec.get("type") or "inline")
    if kind != "inline":
        raise ApiError(
            "INVALID_ARGUMENT", f"{side} 源类型 {kind} 不在本入口允许集",
            detail={"side": side, "type": kind, "allowed": ["inline"],
                    "planned_lane": "planned_lane: 平台连接器（金蝶/客户 WMS）"
                                    "与 CSV/API 源归连接器仓与模板仓车道；本入口"
                                    "只代理内联两源，不代客户在服务端拉外部地址"})
    records = spec.get("records")
    if not isinstance(records, list):
        raise ApiError("INVALID_ARGUMENT", f"{side}.records 须为数组",
                       detail={"side": side})
    if len(records) > RECON_MAX_RECORDS:
        raise ApiError("INVALID_ARGUMENT",
                       f"{side}.records 超过单次上限 {RECON_MAX_RECORDS}",
                       detail={"side": side, "records": len(records),
                               "limit": RECON_MAX_RECORDS})
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise ApiError("INVALID_ARGUMENT", f"{side}.records[{i}] 须为对象",
                           detail={"side": side, "index": i})
    return {"type": "inline", "records": records}


def _recon_degraded(exc: ApiError) -> ApiError:
    """内核不可用 → 501 + planned_lane（spec 明示；不用 500、不伪造结果）。

    NOT_WIRED（内核包/工具未上线）与 UPSTREAM_UNAVAILABLE（连接不可达）都归
    「能力当前不可用」；其余错误码（400/403/404/500）原样透出，保持语义可诊断。
    """
    if exc.code in ("NOT_WIRED", "UPSTREAM_UNAVAILABLE"):
        return ApiError("NOT_IMPLEMENTED",
                        f"内核对账引擎当前不可用（{exc.code}）：{exc.message}",
                        detail={"planned_lane": RECON_KERNEL_LANE,
                                "kernel_code": exc.code})
    return exc


def _err_response(exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=error_shell(exc))


def _note_error(request: Request, exc: ApiError) -> None:
    """把「为什么拒」记到 request.state 上，供访问日志读取（logs.py）。

    为什么不在日志中间件里解析响应体：`call_next` 拿到的是流式包装，body 不便读，
    且逐请求反序列化 JSON 只为取一个码不划算。在错误发生地顺手盖个戳最省。
    `detail` 只记**键名**——值里可能有客户 id、金额、单号，不该进日志。
    """
    request.state.error_code = exc.code
    request.state.error_hint = exc.hint
    request.state.error_detail_keys = sorted(exc.detail)


def _resolve_product_ref(drivers, product_ref: str) -> dict[str, Any] | None:
    """productId 兼容实体 id 与商品编码（先实体 id 后 code，越域 404）。"""
    snap = drivers.master.get_entity(product_ref)
    if snap is not None and snap["kind"] == "product":
        return snap
    return drivers.master.find_by_code("product", product_ref)


def _visible_product(drivers, product_ref: str, customer_id: str,
                     channel_id: str | None, org_node: str | None):
    """商品可见性：商品池授权过滤（可见可售集外的商品一律 404 不泄露存在性）。"""
    snap = _resolve_product_ref(drivers, product_ref)
    if snap is None:
        return None
    visible = drivers.master.visible_product_codes(
        customer_id=customer_id, channel_id=channel_id, org_node=org_node)
    code = str(snap["payload"].get("code") or "")
    if code not in visible or snap.get("state") != "active":
        return None
    return snap


def _request_price(drivers, customer_id: str, product_code: str, qty: Any,
                   unit: str | None) -> dict[str, Any]:
    """当次取价（pricing.resolve_price，规则链 provenance）。"""
    if unit:
        drivers.master.convert_qty(product_code, qty, unit, unit)
    from datetime import date
    return drivers.pricing.resolve_price(
        drivers.tenant_id, customer_id, product_code,
        date.today().isoformat(), qty)


def _line_request(body: dict[str, Any]) -> tuple[str, Any, str | None, str | None]:
    """cart/price-preview 行请求解析：productId+qty 必带。"""
    code = str(body.get("productId") or body.get("product_code") or "").strip()
    if not code:
        raise ApiError("INVALID_ARGUMENT", "行请求缺 productId")
    qty = body.get("qty")
    try:
        if qty is None or float(qty) <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ApiError("INVALID_ARGUMENT", "数量 qty 须为正数",
                       detail={"qty": qty}) from exc
    return code, qty, body.get("unit") or None, body.get("skuId") or None


def create_app(settings: Settings, kernel=None, *,
               pageset_auth: PagesetAuth | None = None,
               cart_store: CartStore | None = None) -> FastAPI:
    """应用工厂（测试可注入 kernel/pageset_auth；serve 形态全部按 Settings 装配）。"""
    app = FastAPI(title="fd-erp-vertical-api", version="0.1.0",
                  description="ERP 行业垂直 REST 服务面（UE 订货台）")
    app.state.settings = settings
    # 内核调用统一收口（两种 transport 的错误语义一致；见 kernel_client.KernelProxy）
    app.state.kernel = ensure_kernel_proxy(kernel or build_kernel_client(settings))
    app.state.pageset_auth = pageset_auth or load_pageset_auth(
        inline_json=settings.pageset_auth_json,
        file_path=settings.pageset_auth_file)
    app.state.drivers = build_drivers(settings, app.state.kernel)
    app.state.cart = cart_store or CartStore()
    app.state.statements = StatementService(app.state.drivers, app.state.kernel)

    # ---- 请求闸（中间件：租户闸全域生效；其余在依赖里按端点粒度生效）----

    @app.middleware("http")
    async def tenant_gate(request: Request, call_next):
        try:
            # 探针豁免：/health 是给 k8s readiness/liveness 用的，探针不带任何
            # 身份（没有 X-ERP-Tenant），若一并过租户闸，Pod 永远不会 Ready。
            # 该端点只回服务名与承载形态，不碰任何业务数据、不泄露租户标识。
            if request.url.path in HEALTH_PATHS or request.url.path == METRICS_PATH:
                return await call_next(request)
            # 店面公开面：匿名可读，不走租户头闸；租户改由路径参数校验（见 storefront.py）
            if SHOP_RE.match(request.url.path):
                return await call_next(request)
            requested = request.headers.get(TENANT_HEADER)
            if not requested:
                raise ApiError("AUTH_REQUIRED", f"缺少租户上下文头 {TENANT_HEADER}")
            if requested != settings.tenant_id:
                raise ApiError("TENANT_DENIED",
                               f"跨租户访问被拒绝：本进程仅服务租户 {settings.tenant_id}",
                               detail={"requested_tenant": requested,
                                       "cell_tenant": settings.tenant_id})
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 统一归一化为封闭错误码
            # 注意 try 必须罩住所有 return 分支：豁免路径（探针/店面）里抛出的
            # ApiError 也要走统一壳，否则未处理异常被 FastAPI 兜成 500 裸响应，
            # 客户端拿到的是「服务端错误」而不是「不可售/不存在」（0.3 实测）。
            api = adapt_exception(exc)
            _note_error(request, api)
            return _err_response(api)

    # 请求体/参数校验失败（FastAPI 默认 422）→ 统一壳 400 封闭码
    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        api = ApiError("INVALID_ARGUMENT",
                       "请求体/参数形态非法（须为 JSON 对象）",
                       detail={"errors": exc.errors()[:5]})
        _note_error(request, api)
        return _err_response(api)

    def ue_context(request: Request) -> dict[str, Any]:
        """UE 端点统一闸：页面集 → grant → 客户身份（数据域）。"""
        grant = request.scope.get("ue_grant")
        if grant is None:  # pragma: no cover - 路由声明保证
            raise ApiError("INTERNAL", "路由未声明所需 grant")
        require_grant(app.state.pageset_auth, PAGESET_UE, grant)
        customer_id = request.headers.get(CUSTOMER_HEADER, "").strip()
        if not customer_id:
            raise ApiError("AUTH_REQUIRED",
                           f"缺少客户身份头 {CUSTOMER_HEADER}（UE 身份=客户联系人所属客户档案）")
        snap = app.state.drivers.master.get_entity(customer_id)
        if snap is None or snap["kind"] != "customer":
            # 越域/未知身份一律 404，不泄露存在性（契约 0. 通用约定）
            raise ApiError("NOT_FOUND", "客户档案不存在",
                           detail={"customer_id": customer_id})
        return {"customer_id": customer_id, "customer": snap}

    def route(grant: str):
        """路由工厂：把该端点所需 grant 注入 scope，供 ue_context 消费。"""
        def _dep(request: Request) -> dict[str, Any]:
            request.scope["ue_grant"] = grant
            request.state.erp_grant = grant  # 访问日志用（被 GRANT 拒时能指到哪一条）
            return ue_context(request)
        return _dep

    # ---- 0) 页面集授权端点（形状已被 paas P0 客户端固定：store.ts 以
    #      res.json() 原始体过 parsePagesetAuth，要求顶层 payload.pagesets；
    #      故在统一壳外附顶层 pagesets 镜像字段，解析两侧都成立）----

    @app.get("/health")
    @app.get("/healthz")
    async def health():
        """承载探针：只报服务名与内核承载形态，不碰业务数据、不回显租户与 DSN。

        单副本 Deployment 的 readiness/liveness 都打这里；探针不能携带业务身份，
        故本路径在租户闸里被显式豁免（HEALTH_PATHS）。
        """
        return {"ok": True, "service": "fd-erp-vertical-api",
                "kernel_transport": settings.kernel_transport}

    @app.get(METRICS_PATH, include_in_schema=False)
    async def metrics():
        """运行指标：prometheus 文本，纯内存、零数据库往返（optimize-erp-solution 6.1）。

        暴露面纪律：只绑集群内地址、不挂公网 Ingress（清单侧约束）。指标不带租户明文
        ——本服务一进程一租户，租户是常量，进标签只会虚高基数。
        """
        from .observability import metrics_content_type, render_metrics
        from fastapi.responses import Response

        return Response(content=render_metrics(), media_type=metrics_content_type())

    @app.get("/erp/supply-chain/v1/pagesets")
    async def list_pagesets():
        pagesets = app.state.pageset_auth.wire_payload()["pagesets"]
        return {"ok": True, "data": {"pagesets": pagesets},
                "pagesets": pagesets, "rows_returned": len(pagesets)}

    # ---- 1) 商品域（grant: catalog.read）----

    @app.get("/erp/supply-chain/v1/ue/products")
    async def list_products(request: Request, ctx: dict = Depends(route("catalog.read"))):
        q = request.query_params
        keyword = (q.get("keyword") or "").strip().lower()
        category = (q.get("categoryId") or "").strip()
        sort = (q.get("sort") or "code").strip()
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        page = int(q["page"]) if q.get("page") else None  # 1-based 便捷参数
        if page is not None and page > 0:
            offset, limit = (page - 1) * limit, limit
        channel_id = (q.get("channelId") or "").strip() or None
        org_node = (q.get("orgNode") or "").strip() or None
        d = app.state.drivers
        visible = d.master.visible_product_codes(
            customer_id=ctx["customer_id"], channel_id=channel_id, org_node=org_node)
        cards: list[dict[str, Any]] = []
        for code in visible:
            snap = d.master.find_by_code("product", code)
            if snap is None:
                continue
            payload = snap["payload"]
            if keyword and keyword not in str(payload.get("name") or "").lower() \
                    and keyword not in code.lower():
                continue
            if category and str(payload.get("category") or "") != category:
                continue
            cards.append({
                "productId": code,
                "entity_id": snap["entity_id"],
                "title": payload.get("name"),
                "category": payload.get("category"),
                "units": payload.get("units") or [],
                "images": payload.get("images") or [],
                "list_price": payload.get("base_price"),
                "visibility_note": "商品池授权过滤后可见（masterdata.visible_product_codes）",
            })
        reverse = sort.startswith("-")
        cards.sort(key=lambda c: _sort_key(c, sort), reverse=reverse)
        return list_shell(cards, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/ue/product-categories")
    async def list_categories(ctx: dict = Depends(route("catalog.read"))):
        d = app.state.drivers
        visible = d.master.visible_product_codes(customer_id=ctx["customer_id"])
        tree: dict[str, dict[str, Any]] = {}
        for code in visible:
            snap = d.master.find_by_code("product", code)
            payload = snap["payload"] if snap else {}
            cat = str(payload.get("category") or "未分类")
            node = tree.setdefault(cat, {"categoryId": cat, "product_count": 0,
                                         "stop_selling_count": 0})
            node["product_count"] += 1
        # 禁售/限售标注：租户全量商品中 stop_selling 者按类计数（如实标注）
        for snap in d.master.list_entities(kind="product"):
            if snap.get("state") == "stop_selling":
                cat = str(snap["payload"].get("category") or "未分类")
                node = tree.setdefault(cat, {"categoryId": cat, "product_count": 0,
                                             "stop_selling_count": 0})
                node["stop_selling_count"] += 1
        out = [tree[k] for k in sorted(tree)]
        return list_shell(out, offset=0, limit=len(out) or 1)

    @app.get("/erp/supply-chain/v1/ue/products/{product_id}")
    async def product_detail(product_id: str,
                             ctx: dict = Depends(route("catalog.read"))):
        d = app.state.drivers
        snap = _visible_product(d, product_id, ctx["customer_id"], None, None)
        if snap is None:
            raise ApiError("NOT_FOUND", f"商品不存在或不可见：{product_id}",
                           detail={"productId": product_id})
        payload = snap["payload"]
        quote = _request_price(d, ctx["customer_id"], str(payload.get("code")),
                               1, None)
        return ok_shell({
            "productId": payload.get("code"),
            "entity_id": snap["entity_id"],
            "title": payload.get("name"),
            "category": payload.get("category"),
            "state": snap.get("state"),
            "units": payload.get("units") or [],
            "images": payload.get("images") or [],
            "supplier_id": payload.get("supplier_id"),
            "price": quote,  # 取价引擎结果：客户价/活动价/券 + 规则链出处与生效期
            "stock": stock_visibility(d, str(payload.get("code"))),
        })

    # ---- 2) 下单域（grant: catalog.read / order.write）----

    @app.get("/erp/supply-chain/v1/ue/cart")
    async def get_cart(ctx: dict = Depends(route("catalog.read"))):
        lines = app.state.cart.lines(settings.tenant_id, ctx["customer_id"])
        return ok_shell({"lines": lines, "line_count": len(lines)})

    @app.post("/erp/supply-chain/v1/ue/cart/lines")
    async def add_cart_line(body: dict[str, Any],
                            ctx: dict = Depends(route("catalog.read"))):
        code, qty, unit, sku_id = _line_request(body)
        d = app.state.drivers
        snap = _visible_product(d, code, ctx["customer_id"], None, None)
        if snap is None:
            raise ApiError("NOT_FOUND", f"商品不存在或不可见：{code}",
                           detail={"productId": code})
        quote = _request_price(d, ctx["customer_id"], str(snap["payload"].get("code")),
                               qty, unit)
        line = app.state.cart.add_line(settings.tenant_id, ctx["customer_id"], {
            "product": str(snap["payload"].get("code")),
            "qty": qty,
            "unit": unit or "",
            "skuId": sku_id,
            "title": snap["payload"].get("name"),
            "quote": quote,
        })
        return ok_shell(line)

    @app.delete("/erp/supply-chain/v1/ue/cart/lines/{line_id}")
    async def remove_cart_line(line_id: str,
                               ctx: dict = Depends(route("catalog.read"))):
        lines = app.state.cart.remove_line(settings.tenant_id,
                                           ctx["customer_id"], line_id)
        return ok_shell({"lines": lines, "line_count": len(lines)})

    @app.post("/erp/supply-chain/v1/ue/cart/price-preview")
    async def price_preview(body: dict[str, Any],
                            ctx: dict = Depends(route("catalog.read"))):
        """试算：行价/优惠/合计 + 信用额度预估占用；纯读，不占用额度。"""
        rows = body.get("lines")
        if not isinstance(rows, list) or not rows:
            raise ApiError("INVALID_ARGUMENT", "lines 须为非空数组")
        d = app.state.drivers
        priced: list[dict[str, Any]] = []
        total = 0.0
        for row in rows:
            code, qty, unit, _sku = _line_request(row if isinstance(row, dict) else {})
            snap = _visible_product(d, code, ctx["customer_id"], None, None)
            if snap is None:
                raise ApiError("NOT_FOUND", f"商品不存在或不可见：{code}",
                               detail={"productId": code})
            quote = _request_price(d, ctx["customer_id"],
                                   str(snap["payload"].get("code")), qty, unit)
            total += float(quote["line_total"])
            priced.append(quote)
        credit = app.state.kernel.query_credit(ctx["customer_id"]) or {}
        try:
            available = float(credit.get("available") or 0)
        except (TypeError, ValueError):
            available = 0.0
        return ok_shell({
            "lines": priced,
            "amount_total": f"{total:.6f}".rstrip("0").rstrip("."),
            "currency": settings.base_currency,
            "credit_preview": {
                "available": credit.get("available"),
                "requested": f"{total:.6f}".rstrip("0").rstrip("."),
                "sufficient": total <= available,
                "note": "纯读试算：不占用额度；实际占用在下单经内核 reserve_credit 记账",
            },
        })

    @app.post("/erp/supply-chain/v1/ue/orders")
    async def create_order(body: dict[str, Any],
                           ctx: dict = Depends(route("order.write"))):
        """下单：编排层 accept_order（信用校验→取价→reserve_credit→订单提案）。

        额度不足 fail-closed 拒单：CREDIT_EXCEEDED + 占用明细（可解释拒绝）。
        落账仍走内核确认门 —— REST 不直接落账。
        """
        rows = body.get("lines")
        if not isinstance(rows, list) or not rows:
            raise ApiError("INVALID_ARGUMENT", "lines 须为非空数组")
        from datetime import date
        lines = []
        for row in rows:
            code, qty, unit, _sku = _line_request(row if isinstance(row, dict) else {})
            entry: dict[str, Any] = {"product_code": code, "qty": qty}
            if unit:
                entry["unit"] = unit
            if isinstance(row, dict) and row.get("note"):
                entry["note"] = str(row["note"])
            lines.append(entry)
        result = app.state.drivers.orders.accept_order(
            ctx["customer_id"], lines, date.today().isoformat())
        if not result.get("accepted"):
            raise ApiError("CREDIT_EXCEEDED",
                           "订单金额超出可用信用额度（fail-closed 拒单）",
                           detail=result.get("explanation") or {})
        # 收货信息/备注：受理回执回显（编排层 accept_order 暂不承载收货字段 ——
        # 持久化挂账见 README；不伪造已保存）
        result["shipping_receipt"] = body.get("shipping") or {}
        result["note_receipt"] = body.get("note") or ""
        return ok_shell(result)

    # ---- 3) 查单域（grant: order.write）----

    def _owned_order(order_ref: str, customer_id: str) -> dict[str, Any]:
        """自有订单当前态（越域一律 404，不泄露存在性）。"""
        d = app.state.drivers
        hits = [e for e in d.orders._events()
                if e.get("record") == "order" and e.get("ref") == order_ref]
        if not hits:
            raise ApiError("NOT_FOUND", f"订单不存在：{order_ref}",
                           detail={"orderId": order_ref})
        first = hits[0]
        if (first.get("payload") or {}).get("customer_id") != customer_id:
            raise ApiError("NOT_FOUND", f"订单不存在：{order_ref}",
                           detail={"orderId": order_ref})
        latest = hits[-1]
        payload = dict(first.get("payload") or {})
        payload.update(latest.get("payload") or {})
        return {"order_ref": order_ref, "phase": latest.get("event"),
                "status": latest.get("status"), "payload": payload}

    @app.get("/erp/supply-chain/v1/ue/orders")
    async def list_orders(request: Request,
                          ctx: dict = Depends(route("order.write"))):
        q = request.query_params
        status = (q.get("status") or "").strip() or None
        date_from = (q.get("from") or "").strip() or None
        date_to = (q.get("to") or "").strip() or None
        keyword = (q.get("keyword") or "").strip().lower()
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        d = app.state.drivers
        seen: dict[str, dict[str, Any]] = {}
        for ev in d.orders._events():
            if ev.get("record") != "order":
                continue
            ref = ev.get("ref")
            payload = ev.get("payload") or {}
            if ev["event"] == "proposed":
                if payload.get("customer_id") == ctx["customer_id"]:
                    seen[ref] = {"ref": ref, "event": "proposed",
                                 "status": ev.get("status"), "payload": dict(payload)}
            elif ref in seen and ev["event"] in ("confirmed", "cancelled"):
                merged = dict(seen[ref]["payload"])
                merged.update(payload)
                seen[ref] = {"ref": ref, "event": ev["event"],
                             "status": ev.get("status"), "payload": merged}
        rows: list[dict[str, Any]] = []
        for item in seen.values():
            payload = item["payload"]
            day = str(payload.get("day") or "")
            if date_from and day < date_from:
                continue
            if date_to and day > date_to:
                continue
            if status and item["status"] != status:
                continue
            if keyword and keyword not in item["ref"].lower() and not any(
                    keyword in str(ln.get("product") or "").lower()
                    for ln in payload.get("lines") or []):
                continue
            rows.append({
                "orderId": item["ref"],
                "status": item["status"],
                "phase": item["event"],
                "status_note": "pending=待确认（确认车道）；confirmed=已确认移交履约；"
                               "cancelled=已取消（信用占用经内核回冲）",
                "day": payload.get("day"),
                "amount_total": payload.get("amount_total"),
                "currency": payload.get("currency"),
                "customer_id": payload.get("customer_id"),
            })
        rows.sort(key=lambda r: (str(r.get("day") or ""), r["orderId"]), reverse=True)
        return list_shell(rows, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/ue/orders/{order_id}")
    async def order_detail(order_id: str,
                           ctx: dict = Depends(route("order.write"))):
        item = _owned_order(order_id, ctx["customer_id"])
        payload = item["payload"]
        return ok_shell({
            "orderId": item["order_ref"],
            "status": item["status"],
            "phase": item["phase"],
            "status_note": "pending=待确认（确认车道）；confirmed=已确认移交履约；"
                           "cancelled=已取消",
            "day": payload.get("day"),
            "amount_total": payload.get("amount_total"),
            "currency": payload.get("currency"),
            "lines": payload.get("lines") or [],
            "credit_reservation": payload.get("credit_reservation"),
            "request_id": payload.get("request_id"),
        })

    @app.get("/erp/supply-chain/v1/ue/orders/{order_id}/fulfillment")
    async def order_fulfillment(order_id: str,
                                ctx: dict = Depends(route("order.write"))):
        _owned_order(order_id, ctx["customer_id"])  # 越域闸
        d = app.state.drivers
        try:
            view = d.fulfillment.fulfillment_view(order_id)
        except TemplateError as exc:
            if exc.code != "NOT_FOUND":
                raise
            # 无履约计划：区分「未确认（无移交记录）→ 404」与「已确认待编排
            # （有移交记录）→ 如实报 handoff_pending 中间态，不伪造计划」
            handoff = next((e for e in d.orders._events()
                            if e.get("record") == "fulfillment_handoff"
                            and (e.get("payload") or {}).get("order_ref") == order_id),
                           None)
            if handoff is None:
                raise ApiError("NOT_FOUND",
                               f"订单无履约编排（未确认订单不产生履约移交）：{order_id}",
                               detail={"orderId": order_id,
                                       "hint": "订单经确认车道确认后移交交付编排"}) from exc
            return ok_shell({
                "order_ref": order_id, "plans": [],
                "handoff_ref": handoff.get("ref"),
                "handoff_pending": True,
                "note": "订单已确认并移交履约（handoff 记录在编排档）；"
                        "交付编排计划生成后此处返回 plans 明细",
            })
        return ok_shell(view)

    @app.post("/erp/supply-chain/v1/ue/orders/{order_id}/fulfillment")
    async def trigger_fulfillment(order_id: str,
                                  ctx: dict = Depends(route("order.write"))):
        """触发履约编排（add-erp-fulfillment-trigger）：移交记录 → 履约计划。

        此前 `plan_fulfillment` 只有模板层 API，REST 面没有任何入口，已确认订单
        只能永久停在 handoff_pending（产上 CP 闭环时只能进 pod 手跑，实锤缺口）。

        闸序沿用 UE 既有：租户 → 页面集 → grant(order.write) → 客户数据域。
        状态门如实：无移交记录 = 订单未确认 → 409 WORKFLOW_STATE，不伪造计划。
        幂等：已有计划直接回既有视图（`plan_fulfillment` 每次生成新 ref，重复
        调用会堆重复的直发/出库指令），带 `idempotent: true`。
        """
        _owned_order(order_id, ctx["customer_id"])  # 越域 404，不泄露存在性
        d = app.state.drivers
        try:
            view = d.fulfillment.fulfillment_view(order_id)
            return ok_shell({**view, "idempotent": True})
        except TemplateError as exc:
            if exc.code != "NOT_FOUND":
                raise
        handoff = next((e for e in d.orders._events()
                        if e.get("record") == "fulfillment_handoff"
                        and (e.get("payload") or {}).get("order_ref") == order_id),
                       None)
        if handoff is None:
            raise ApiError("WORKFLOW_STATE",
                           f"订单未确认，不产生履约移交：{order_id}",
                           detail={"orderId": order_id,
                                   "hint": "订单须经确认车道确认后才会移交履约编排"})
        payload = handoff.get("payload") or {}
        d.fulfillment.plan_fulfillment(
            handoff["ref"], order_id, str(payload.get("customer_id") or ""),
            payload.get("lines") or [], requested_by=ctx["customer_id"])
        return ok_shell(d.fulfillment.fulfillment_view(order_id))

    # ---- 4) 对账域（grant: statement.confirm）----

    @app.get("/erp/supply-chain/v1/ue/statements")
    async def list_statements(request: Request,
                              ctx: dict = Depends(route("statement.confirm"))):
        q = request.query_params
        period = (q.get("period") or "").strip() or None
        status = (q.get("status") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        rows = app.state.statements.list_statements(
            ctx["customer_id"], status=status, period=period)
        return list_shell(rows, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/ue/statements/{statement_id}")
    async def statement_detail(statement_id: str,
                               ctx: dict = Depends(route("statement.confirm"))):
        return ok_shell(app.state.statements.get_statement(
            ctx["customer_id"], statement_id))

    @app.post("/erp/supply-chain/v1/ue/statements/{statement_id}/confirm")
    async def confirm_statement(statement_id: str, body: dict[str, Any],
                                ctx: dict = Depends(route("statement.confirm"))):
        """确认门：确认→内核自平核对 proposal（pending）；异议→内部处置车道。

        超时/失败不假报成功：响应如实携带 proposal_status=pending。
        """
        decision = str(body.get("decision") or "").strip()
        if decision not in ("confirmed", "disputed"):
            raise ApiError("INVALID_ARGUMENT",
                           "decision 须为 confirmed / disputed",
                           detail={"decision": decision})
        if decision == "confirmed":
            return ok_shell(app.state.statements.confirm(
                ctx["customer_id"], statement_id))
        return ok_shell(app.state.statements.dispute(
            ctx["customer_id"], statement_id, str(body.get("disputeNote") or "")))

    # ---- 4b) 对账任务代理（grant: statement.confirm；内核长任务 task_id + 轮询）----
    #
    # 为什么放在 UE 对账域：契约里对账单确认门的上游就是「内核自平核对」
    # （api-contract §4 与各面 planned_lane 同口径），statement.confirm 本来就是
    # 「有权看对账数字」的权限码；新开一个 grant 码要改 paas registry（另一个仓），
    # 而这里不想为一个代理入口扩权限语义。故这两条是**契约外对账支撑入口**，
    # 不计入 19 条契约端点，客户 App 不消费，对账工作台/运维调用。
    #
    # 形态是「提交即返回 task_id，报告靠轮询」：内核 recon/engine.py 本来就是
    # queued + 后台线程 + 落库态，HTTP 同步等待只会把长任务超时搬到 REST 面
    # （design D13 已否掉同步代理）。

    @app.post("/erp/supply-chain/v1/ue/reconciliations", status_code=202)
    async def create_reconciliation(body: dict[str, Any],
                                    ctx: dict = Depends(route("statement.confirm"))):
        """提交两源对账：202 + task_id；用 poll_url 查状态与差异。"""
        left = _recon_source("left_source", body.get("left_source"))
        right = _recon_source("right_source", body.get("right_source"))
        retry = str(body.get("retry_task_id") or "").strip() or None
        try:
            out = app.state.kernel.run_reconciliation(left, right,
                                                      retry_task_id=retry)
        except ApiError as exc:
            raise _recon_degraded(exc) from exc
        out = out if isinstance(out, dict) else {"result": out}
        # 内核工具面会给结果盖 ok/rows_returned 戳；放进 data 会造成「data.ok」歧义
        task = {k: v for k, v in out.items() if k not in ("ok", "rows_returned")}
        task_id = str(task.get("task_id") or "")
        return ok_shell({
            **task,
            "poll_url": f"/erp/supply-chain/v1/ue/reconciliations/{task_id}",
            "note": "长任务：内核后台执行，本响应不等结果；按 poll_url 轮询"
                    "状态/进度/四类差异。零外部写入（只读滩头）。",
        })

    @app.get("/erp/supply-chain/v1/ue/reconciliations/{task_id}")
    async def get_reconciliation(task_id: str,
                                 ctx: dict = Depends(route("statement.confirm"))):
        """按 task_id 查对账报告（queued/running/done/failed/interrupted）。"""
        try:
            report = app.state.kernel.get_recon_report(task_id)
        except ApiError as exc:
            raise _recon_degraded(exc) from exc
        return ok_shell(report)

    # ---- 5) 开票域（grant: invoice.write）—— 申请受理 + 进度查询 ----
    #
    # 边界（add-invoice-request-lane design D1）：申请走内核**拟稿车道**
    # （submit_event → pending），审核是内核确认门 decide_event——契约里的
    # 「进后台审核车道」就是这两样，不另造本地状态台账。票据本体仍走客户既有
    # 开票渠道：本端不代开票、不生成票据文件、不采信调用方给的金额。

    @app.get("/erp/supply-chain/v1/ue/invoice-requests")
    async def list_invoice_requests(request: Request,
                                    ctx: dict = Depends(route("invoice.write"))):
        """本人开票申请列表：状态取内核投影（pending/accepted/rejected）。"""
        d = app.state.drivers
        q = request.query_params
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        try:
            out = d.invoices.requests_of(
                ctx["customer_id"],
                status=(q.get("status") or "").strip() or None,
                date_from=(q.get("from") or "").strip() or None,
                date_to=(q.get("to") or "").strip() or None)
        except ApiError as exc:
            raise invoice_degraded(exc) from exc
        # 分页只由统一壳做一次（管道层不截断），否则 total 与 rows_returned 对不上。
        return list_shell(out["rows"], offset=offset, limit=limit,
                          extra={k: v for k, v in out.items() if k != "rows"})

    @app.post("/erp/supply-chain/v1/ue/invoice-requests")
    async def create_invoice_request(body: dict[str, Any],
                                     ctx: dict = Depends(route("invoice.write"))):
        """受理开票申请：返回申请号与 pending，进后台审核车道。"""
        d = app.state.drivers
        try:
            out = d.invoices.submit_request(ctx["customer_id"], body)
        except ApiError as exc:
            raise invoice_degraded(exc) from exc
        return ok_shell(out)

    # ---- 6) 售后域（grant: aftersale.write）—— 申请受理与进度查询 ----
    #
    # 边界（add-aftersale-lane design D5）：申请是**流程记录**，不是入库事实。
    # 受理只写编排档，不向事件层写任何事件——客户提出申请时货还没回来，
    # 此时落 goods_returned 等于凭空造一次入库，撤回时还得冲正。
    # 处置面（运营侧 accepted→processing→completed）不在本单范围，如实写在 next_action。

    @app.post("/erp/supply-chain/v1/ue/aftersale-requests")
    async def create_aftersale(body: dict[str, Any],
                               ctx: dict = Depends(route("aftersale.write"))):
        """售后申请受理：返回申请单号，进「已受理，等待商家处置」。"""
        order_ref = str(body.get("orderId") or body.get("order_id")
                        or body.get("orderRef") or "").strip()
        if not order_ref:
            raise ApiError("INVALID_ARGUMENT", "缺 orderId（订单引用）")
        lines = body.get("lines")
        if not isinstance(lines, list) or not lines:
            raise ApiError("INVALID_ARGUMENT", "售后申请 lines 须为非空数组")
        evidence = body.get("evidence") or body.get("attachments") or []
        if not isinstance(evidence, list):
            raise ApiError("INVALID_ARGUMENT", "evidence 须为引用数组（不外传二进制）")
        d = app.state.drivers
        out = d.aftersales.request_aftersale(
            ctx["customer_id"], order_ref, lines,
            str(body.get("kind") or ""),
            evidence=[str(x) for x in evidence],
            requested_by=f"ue:{ctx['customer_id']}")
        return ok_shell(out)

    @app.get("/erp/supply-chain/v1/ue/aftersale-requests")
    async def list_aftersales(request: Request,
                              ctx: dict = Depends(route("aftersale.write"))):
        q = request.query_params
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        d = app.state.drivers
        out = d.aftersales.aftersale_requests(
            ctx["customer_id"],
            status=(q.get("status") or "").strip() or None,
            date_from=(q.get("from") or "").strip() or None,
            date_to=(q.get("to") or "").strip() or None,
            offset=offset, limit=limit)
        return list_shell(out["rows"], offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/ue/aftersale-requests/{request_id}")
    async def aftersale_detail(request_id: str,
                               ctx: dict = Depends(route("aftersale.write"))):
        """详情：他人单号与不存在单号同一条 404（契约 §0 不泄露存在性）。"""
        d = app.state.drivers
        return ok_shell(d.aftersales.aftersale_of(ctx["customer_id"], request_id))

    # ---- AI 助手入口：契约 §7 明确无独立 AI 端点（复用底座 chat/WS 面）----

    # ---- SE 业务员台 / SP 供应商 / CP 承运商三页面集（faces.py，同闸序同壳）----
    register_se_face(app, settings, app.state.drivers, app.state.kernel,
                     app.state.statements, app.state.pageset_auth)
    register_sp_face(app, settings, app.state.drivers, app.state.kernel,
                     app.state.pageset_auth)
    register_cp_face(app, settings, app.state.drivers, app.state.kernel,
                     app.state.pageset_auth)
    # ---- FI 财务后台（内部页集：不进 /pagesets 载荷，见 pagesets.INTERNAL_PAGESET_KEYS）----
    register_fi_face(app, settings, app.state.drivers, app.state.pageset_auth)
    # ---- WH 仓管作业（同属内部页集：不进载荷）----
    register_wh_face(app, settings, app.state.drivers, app.state.pageset_auth)
    # ---- 店面公开读面（change: establish-ai-native-ecommerce-stack 0.3）----
    register_storefront_face(app, settings, app.state.drivers)
    # 交易协商面（change establish-ai-native-trade-discovery 2.x）：卖方面，走
    # 既有 X-ERP-Tenant 头闸（不在 SHOP_PATHS 匿名豁免清单里）。
    register_trade_face(app, settings, app.state.drivers)

    # 请求计数（路由模板级，见 observability.py）。**必须最后注册**：Starlette 的
    # @app.middleware 是「后注册者在最外层」，放早了会被租户闸包在内侧，于是被闸
    # 拒掉的请求（401/403）根本到不了这里——而「被拒」恰恰是最该看的信号。
    instrument_middleware(app)
    # 访问日志再往外一层：它要读到**最终**状态码与业务码（403 是闸给出的，不是路由），
    # 且要给响应补 X-Request-Id。默认只在失败时落一行（ERP_ACCESS_LOG 控制，见 logs.py）。
    access_log_middleware(app)

    return app


def _sort_key(card: dict[str, Any], sort: str) -> tuple:
    """商品卡排序键：code/name/price（- 前缀表示倒序，由 reverse 承载）；
    非法 sort 如实 INVALID_ARGUMENT。"""
    field = sort.lstrip("-")
    if field == "name":
        return (str(card.get("title") or ""),)
    if field == "price":
        try:
            return (float(card.get("list_price") or 0),)
        except (TypeError, ValueError):
            return (0.0,)
    if field != "code":
        raise ApiError("INVALID_ARGUMENT", "sort 须为 code/name/-name/price/-price",
                       detail={"sort": sort})
    return (str(card.get("productId") or ""),)


