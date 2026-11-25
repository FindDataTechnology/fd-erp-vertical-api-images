"""店面公开读面（change establish-ai-native-ecommerce-stack 0.3）。

为什么是**独立路由族**而不是给 `ue/*` 开匿名口：UE 路由的身份闸是安全边界
（未知客户一律 404 且不泄露存在性）。给它加匿名白名单等于把「谁是公开的」
这条线搞模糊；独立族让公开边界一眼可数，也正好落 `erp-rest-surface-contract`
的「店面公开路由另立契约表，逐条标身份要求与理由」。

三条硬纪律：
1. **只出零售渠道已上架且有零售价的商品**：`visible_product_codes(channel_id=零售渠道)`
   给出池授权的可见集，再逐商品问 `resolve_retail_price`——没有零售价即不可售，
   必须从读面排除，MUST NOT 回落 B2B 价或商品主档 base_price。
2. **零售渠道未配置 = 店面关闭**：返回空集合，不回落成「全量商品可见」
   （那会把 B2B 货架直接公开出去）。
3. **不泄露内部口径**：响应不含 entity_id / supplier_id / 成本价 / 客户专属价。
   商品编码（catalog code）是对外商品把手，也是零售价规则的作用域键，故保留。
"""
from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from fastapi import FastAPI, Request, Response

from fd_erp_vertical_templates.errors import TemplateError

from .config import Settings
from .drivers import Drivers
from .envelope import list_shell, ok_shell, parse_page_params
from .errors import ApiError, adapt_exception

#: 下单幂等：进程内表（单副本）。存整份成功响应，同键重放直接回放——真实去重
#: 在编排层用订单号，这里只挡「客户端超时重试」这一类重复提交。
#: shortcut: 多副本时各进程各一份，须换共享存储或让网关做幂等。
_ORDER_IDEMPOTENCY: dict[str, dict[str, Any]] = {}


def _today() -> str:
    return date.today().isoformat()


#: 公开读面的缓存头：短窗 public 缓存。商品与零售价按天变，故用日期作 ETag 的
#: 弱因子——同一天同样的商品集拿到同一 ETag，客户端可 304 省掉重传。
#: 只给列表/详情两条只读路径加，不碰任何写面（店面本版没有写面）。
_CACHE_CONTROL = "public, max-age=300, stale-while-revalidate=60"


def _etag(day: str, *parts: Any) -> str:
    """ETag 只含**摘要**，不含原文。

    两个理由：① HTTP 头是 latin-1，把 `keyword=可售` 这类非 ASCII 参数直接拼进头会
    抛 UnicodeEncodeError（500，实测）；② ETag 会被写入客户端缓存与日志，摘要不带
    用户输入原文。hash 只用于去重，不做安全用途。
    """
    raw = "\x1f".join(str(p) for p in (day, *parts))
    return 'W/"shop-' + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] + '"'


def _set_cache_headers(response, etag: str) -> None:
    response.headers["Cache-Control"] = _CACHE_CONTROL
    response.headers["ETag"] = etag


#: 公开读面限流：按来源地址的滑动窗（进程内，单副本）。店面是匿名面，没有调用者
#: 身份可归因，只能按地址计；单副本部署下进程内计数即可（多副本时各自计数会放宽
#: 上限，接管方是边缘层——见 STOREFRONT-API.md §3）。
_RATE_WINDOW_SECONDS = 60
_RATE_MAX_PER_WINDOW = 120
_RATE_BUCKETS: dict[str, list[float]] = {}


def _rate_check(client_host: str) -> None:
    import time
    now = time.monotonic()
    bucket = _RATE_BUCKETS.setdefault(client_host, [])
    cutoff = now - _RATE_WINDOW_SECONDS
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= _RATE_MAX_PER_WINDOW:
        raise ApiError("RATE_LIMITED_PUBLIC", "公开读面请求过密，请退避后重试")
    bucket.append(now)
    # 桶数上限：清理长期不再出现的来源，避免匿名面把内存撑成无界字典
    if len(_RATE_BUCKETS) > 10_000:
        for key in [k for k, v in _RATE_BUCKETS.items()
                    if not v or v[-1] < cutoff]:
            _RATE_BUCKETS.pop(key, None)


def register_storefront_face(app: FastAPI, settings: Settings,
                             drivers: Drivers) -> None:
    """注册 `/shop/{tenant}/...` 公开读面。"""

    def shop_context(tenant: str) -> Drivers:
        """公开面的「租户闸」：路径里的租户必须等于本进程绑定租户。

        与 `X-ERP-Tenant` 头同语义，但走路径参数——匿名访客不持有任何头。
        跨租户一律 404（不泄露存在性），与 UE 侧口径一致。
        """
        if tenant != settings.tenant_id:
            # 不回显请求里的租户名：越域探测者不该从错误体里读到「我发的确实是这个值」，
            # 更不该能据此区分「该租户存在/不存在」。全局 tenant 闸的 `requested_tenant`
            # 是给运维诊断用的（那边是认证态请求），公开面不适用。
            raise ApiError("NOT_FOUND", "店面不存在")
        return drivers

    def _retail_price(d: Drivers, code: str, day: str) -> dict[str, Any] | None:
        """零售取价：无可售零售价返回 None（调用方据此排除该商品）。"""
        channel = settings.retail_channel
        if not channel:
            return None
        out = d.pricing.resolve_retail_price(settings.tenant_id, channel, code, day)
        return out if out.get("sellable") else None

    def _card(d: Drivers, code: str, day: str) -> dict[str, Any] | None:
        snap = d.master.find_by_code("product", code)
        if snap is None or snap.get("state") != "active":
            return None
        price = _retail_price(d, code, day)
        if price is None:
            return None
        payload = snap["payload"]
        return {
            "productId": code,
            "title": payload.get("name"),
            "category": payload.get("category"),
            "images": payload.get("images") or [],
            "unit_price": price["unit_price"],
            "currency": price["currency"],
            "price_source": price["rule_chain"][0]["rate_source"],
        }

    @app.get("/shop/{tenant}/products")
    async def shop_products(tenant: str, request: Request, response: Response):
        _rate_check(request.client.host if request.client else "unknown")
        d = shop_context(tenant)
        q = request.query_params
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        keyword = (q.get("keyword") or "").strip().lower()
        category = (q.get("categoryId") or "").strip()
        day = _today()
        channel = settings.retail_channel
        codes = (d.master.visible_product_codes(channel_id=channel)
                 if channel else [])
        cards: list[dict[str, Any]] = []
        for code in codes:
            card = _card(d, code, day)
            if card is None:
                continue
            if keyword and keyword not in str(card["title"] or "").lower() \
                    and keyword not in code.lower():
                continue
            if category and str(card["category"] or "") != category:
                continue
            cards.append(card)
        body = list_shell(cards, offset=offset, limit=limit,
                          extra={"day": day,
                                 "channel": channel,
                                 "caliber_note": "只含零售渠道可见、已上架且有零售价的商品"})
        # ETag 只含「影响结果集的东西」（日期 + 渠道 + 过滤参数 + 窗口），不含任何
        # 内部标识；条件请求命中直接 304，省的是重传，不是重新计算。
        etag = _etag(day, channel, keyword, category, offset, limit)
        _set_cache_headers(response, etag)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=dict(response.headers))
        return body

    @app.get("/shop/{tenant}/products/{product_id}")
    async def shop_product_detail(tenant: str, product_id: str,
                                  request: Request, response: Response):
        _rate_check(request.client.host if request.client else "unknown")
        d = shop_context(tenant)
        day = _today()
        channel = settings.retail_channel
        if not channel:
            raise ApiError("NOT_FOUND", "商品不存在或不可售",
                           detail={"productId": product_id})
        # 可见性先过渠道，再过零售价——与列表同一判据，避免「列表看不见、详情能打开」
        if product_id not in d.master.visible_product_codes(channel_id=channel):
            raise ApiError("NOT_FOUND", "商品不存在或不可售",
                           detail={"productId": product_id})
        card = _card(d, product_id, day)
        if card is None:
            raise ApiError("NOT_FOUND", "商品不存在或不可售",
                           detail={"productId": product_id})
        snap = d.master.find_by_code("product", product_id)
        card["units"] = snap["payload"].get("units") or []
        etag = _etag(day, channel, product_id)
        _set_cache_headers(response, etag)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=dict(response.headers))
        return ok_shell(card)

    # ---- 零售下单（change establish-ai-native-ecommerce-stack 0.7/0.9）----
    # 匿名可下单：零售买家没有客户档案、没有登录态。归属用下单时给出的
    # 收货联系信息（ship_to），格式不对就拒；**绝不**回落成「某个默认客户」。
    # 幂等靠调用方的 Idempotency-Key 头：同键重放返回首次订单，不重复下单。

    @app.post("/shop/{tenant}/orders")
    async def shop_create_order(tenant: str, body: dict[str, Any],
                                request: Request, response: Response):
        _rate_check(request.client.host if request.client else "unknown")
        d = shop_context(tenant)
        channel = settings.retail_channel
        if not channel:
            raise ApiError("NOT_FOUND", "店面不存在")

        lines = body.get("lines")
        if not isinstance(lines, list) or not lines:
            raise ApiError("INVALID_ARGUMENT", "lines 须为非空数组",
                           detail={"missing": "lines"})
        rows = [(str(r.get("productId") or r.get("product_code") or "").strip(),
                 r.get("qty"), str(r.get("unit") or ""))
                for r in lines if isinstance(r, dict)]
        if any(not code for code, _q, _u in rows):
            raise ApiError("INVALID_ARGUMENT", "订单行缺 productId")
        ship_to = body.get("shipTo")
        if not isinstance(ship_to, dict):
            raise ApiError("INVALID_ARGUMENT", "须提供收货信息 shipTo",
                           detail={"missing": "shipTo"})

        idem = (request.headers.get("idempotency-key") or "").strip()
        if idem:
            seen = _ORDER_IDEMPOTENCY.get(idem)
            if seen is not None:
                return ok_shell({**seen, "duplicate": True,
                                 "note": "同一 Idempotency-Key 已受理：返回首次订单"})

        # 委托身份（agent 代委托人下单）：两个头都由网关注入，客户端伪造不了
        # —— 网关会剥离并重注入（见 paas/server/routes/erp-proxy.js 的身份头纪律）。
        # 这里只做透传：有 grant_jti 才启用预算闸；无则按「人」下单（与改造前同）。
        grant_jti = (request.headers.get("X-ERP-Grant-Jti") or "").strip() or None
        grant_budget = (request.headers.get("X-ERP-Grant-Budget") or "").strip() or None
        if grant_jti and grant_budget:
            try:
                float(grant_budget)
            except ValueError as exc:
                raise ApiError("INVALID_ARGUMENT",
                               f"X-ERP-Grant-Budget 须为数字：{grant_budget!r}") from exc

        try:
            out = d.orders.accept_prepaid_order(
                [{"product_code": code, "qty": qty, **({"unit": unit} if unit else {})}
                 for code, qty, unit in rows],
                _today(), channel=channel, ship_to=ship_to,
                requested_by=("agent" if grant_jti else "storefront"),
                grant_jti=grant_jti, grant_budget=grant_budget)
        except TemplateError as exc:
            raise adapt_exception(exc) from exc

        result = {"order_ref": out["order_ref"], "status": out["status"],
                  "amount_total": out["amount_total"],
                  "currency": out["currency"],
                  "settlement_mode": out["settlement_mode"],
                  "payment_state": out["payment_state"],
                  "lines": out["priced_lines"]}
        if idem:
            _ORDER_IDEMPOTENCY[idem] = result
        response.headers["Location"] = f"/shop/{tenant}/orders/{out['order_ref']}"
        response.headers["Cache-Control"] = "no-store"
        return ok_shell(result)

    @app.get("/shop/{tenant}/orders/{order_ref}")
    async def shop_get_order(tenant: str, order_ref: str, request: Request):
        """按订单号查零售单：只回零售单，B2B 订单号一律 404（不泄露存在性）。

        匿名可查是因为订单号本身是不透明随机串（`SO-` + 10 位 hex），买家下单后
        拿着它就能查状态；这也是「不要求登录」与「不泄露他人订单」之间的唯一解。
        """
        _rate_check(request.client.host if request.client else "unknown")
        d = shop_context(tenant)
        try:
            ev = d.orders._latest(order_ref)
        except TemplateError as exc:
            raise ApiError("NOT_FOUND", "订单不存在", detail={}) from exc
        payload = ev.get("payload") or {}
        if ev.get("record") != "order" or payload.get("settlement_mode") != "prepaid":
            # B2B 订单走 UE 面（要客户身份）：这里不承认它的存在
            raise ApiError("NOT_FOUND", "订单不存在", detail={})
        payments = [e for e in d.orders._events()
                    if e.get("record") == "payment_received"
                    and e.get("order_ref") == order_ref
                    and e.get("status") == "accepted"]
        return ok_shell({
            "order_ref": order_ref,
            "status": ev.get("status"),
            "amount_total": payload.get("amount_total"),
            "currency": payload.get("currency"),
            "lines": payload.get("lines") or [],
            "payment_state": "received" if payments else "awaiting_payment",
            "shipped_to": {"name": (payload.get("ship_to") or {}).get("name")},
        })

    @app.post("/shop/{tenant}/orders/{order_ref}/payment")
    async def shop_record_payment(tenant: str, order_ref: str,
                                  body: dict[str, Any], request: Request):
        """商家侧登记到账（首版：人工确认 + 凭据引用）。

        **要商家身份**：这不是买家能做的动作。首版以 `X-ERP-Tenant` + 商家凭证头
        挡住（网关 PAT 落地前的形态，与 SP/CP 面的演示身份同法）；买家侧永远不可达。
        """
        requested = request.headers.get("X-ERP-Tenant")
        if not requested or requested != settings.tenant_id:
            raise ApiError("AUTH_REQUIRED", "登记到账需商家身份")
        d = shop_context(tenant)
        try:
            out = d.orders.record_payment_received(
                order_ref,
                amount=str(body.get("amount") or ""),
                currency=str(body.get("currency") or settings.base_currency),
                proof_ref=str(body.get("proofRef") or ""),
                idempotency_key=body.get("idempotencyKey") or None,
                recorded_by=str(body.get("recordedBy") or "merchant"),
            )
        except TemplateError as exc:
            raise adapt_exception(exc) from exc
        return ok_shell(out)
