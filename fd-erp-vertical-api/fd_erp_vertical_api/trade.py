"""交易协商面（change establish-ai-native-trade-discovery 2.x）。

**两套前缀，同一组处理器**：

  /erp/supply-chain/v1/trade/...   商家侧（走 X-ERP-Tenant 头闸 + 会话）
  /shop/{tenant}/trade/...         匿名可达（店面族，租户由路径参数校验）

**为什么必须有第二套**：agent 没有浏览器会话，网关的 `/erp` 是 fail-closed（无会话 401）。
店面的 `/shop` 族专为「无会话但有委托」设计——它验 Bearer 里的 Grant 并注入
`x-erp-grant-*`，这恰好就是协商需要的身份。只在 `/erp` 下挂，agent 就只能在集群内
绕过网关直连 ERP（那样 Grant 不验签、预算闸拿不到额度——2026-11-25 实测到）。

身份纪律与店面同源：`/shop/{tenant}/trade/*` 在 `SHOP_PATHS` 里显式登记（匿名豁免清单），
租户不符一律 404；委托头只认网关注入。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import FastAPI, Request

from fd_erp_vertical_templates.errors import TemplateError

from .drivers import Drivers
from .envelope import ok_shell
from .errors import ApiError, adapt_exception

#: 买方标识头（询价单归属与未关闭上限按它计）。缺省回落 "agent"。
BUYER_HEADER = "X-ERP-Trade-Buyer"

def register_trade_face(app: FastAPI, settings, drivers: Drivers) -> None:
    def _grant(request: Request) -> tuple[str | None, str | None]:
        jti = (request.headers.get("X-ERP-Grant-Jti") or "").strip() or None
        budget = (request.headers.get("X-ERP-Grant-Budget") or "").strip() or None
        return jti, budget

    def _shop_tenant(tenant: str | None) -> None:
        """匿名族的租户闸：路径里的租户必须等于本进程绑定租户，否则 404。

        与 `storefront.shop_context` 同一口径（不回显被请求的租户名，不泄露存在性）。
        """
        if tenant is None:
            return  # 商家侧前缀不传租户（走头闸）
        if tenant != settings.tenant_id:
            raise ApiError("NOT_FOUND", "店面不存在")

    async def create_demand(request: Request, tenant: str | None = None):
        _shop_tenant(tenant)
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        buyer = (request.headers.get(BUYER_HEADER) or "").strip() or "agent"
        try:
            return ok_shell(drivers.trade.create_demand(
                lines=body.get("lines") or [],
                needed_by=body.get("neededBy") or body.get("needed_by"),
                ship_to=body.get("shipTo") or body.get("ship_to") or {},
                buyer_ref=buyer,
                requested_by=("agent" if _grant(request)[0] else "merchant")))
        except TemplateError as exc:
            raise adapt_exception(exc) from exc

    async def create_quote(request: Request, tenant: str | None = None):
        _shop_tenant(tenant)
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        try:
            return ok_shell(drivers.trade.create_quote(
                demand_ref=str(body.get("demandRef") or body.get("demand_ref") or ""),
                unit_price=body.get("unitPrice", body.get("unit_price")),
                currency=str(body.get("currency") or "CNY"),
                valid_until=body.get("validUntil", body.get("valid_until")),
                lead_time_days=body.get("leadTimeDays", body.get("lead_time_days")),
                moq=body.get("moq"),
                qty_offered=body.get("qtyOffered", body.get("qty_offered")),
                product_code=str(body.get("productId") or body.get("product_code") or ""),
                quoted_by="merchant"))
        except TemplateError as exc:
            raise adapt_exception(exc) from exc

    async def accept_quote(request: Request, quote_ref: str,
                           tenant: str | None = None):
        _shop_tenant(tenant)
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        jti, budget = _grant(request)
        ship = body.get("shipTo") or body.get("ship_to")
        if not isinstance(ship, dict) or not ship:
            raise ApiError("INVALID_ARGUMENT",
                           "接受报价须提供收货信息 shipTo（履约必需）",
                           detail={"missing": "shipTo"})
        channel = str(body.get("channel") or settings.retail_channel or "")
        if not channel:
            raise ApiError("INVALID_ARGUMENT",
                           "接受报价须声明渠道（channel）：零售价与履约按渠道",
                           detail={"missing": "channel"})
        buyer = (request.headers.get(BUYER_HEADER) or "").strip() or "agent"
        try:
            return ok_shell(drivers.trade.accept_quote(
                quote_ref=quote_ref, qty=body.get("qty"),
                orchestrator=drivers.orders, day=date.today().isoformat(),
                channel=channel, ship_to=ship, buyer_ref=buyer,
                requested_by=("agent" if jti else "merchant"),
                grant_jti=jti, grant_budget=budget))
        except TemplateError as exc:
            raise adapt_exception(exc) from exc

    # 两条前缀各挂一份。**路径必须是字面量**：`check_rest_surface` 的 AST 只认
    # `@app.<verb>(<Constant>)`——`add_api_route` 和 f-string 都扫不到，契约里写了
    # 反而判「悬空」（2026-11-25 连续踩两次）。字符串重复比闸失明划算。
    @app.post("/erp/supply-chain/v1/trade/demands", status_code=201)
    async def erp_create_demand(request: Request):
        return await create_demand(request)

    @app.post("/shop/{tenant}/trade/demands", status_code=201)
    async def shop_create_demand(request: Request, tenant: str):
        return await create_demand(request, tenant)

    @app.post("/erp/supply-chain/v1/trade/quotes", status_code=201)
    async def erp_create_quote(request: Request):
        return await create_quote(request)

    @app.post("/shop/{tenant}/trade/quotes", status_code=201)
    async def shop_create_quote(request: Request, tenant: str):
        return await create_quote(request, tenant)

    @app.post("/erp/supply-chain/v1/trade/quotes/{quote_ref}/accept", status_code=201)
    async def erp_accept_quote(request: Request, quote_ref: str):
        return await accept_quote(request, quote_ref)

    @app.post("/shop/{tenant}/trade/quotes/{quote_ref}/accept", status_code=201)
    async def shop_accept_quote(request: Request, quote_ref: str, tenant: str):
        return await accept_quote(request, quote_ref, tenant)
