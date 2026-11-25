"""交易协商面（change establish-ai-native-trade-discovery 2.x）。

三条**卖方面**路由，与店面公开面分开挂：

  POST /erp/supply-chain/v1/trade/demands          买方声明需求（带 X-ERP-Trade-Buyer）
  POST /erp/supply-chain/v1/trade/quotes           卖方对询价单报价
  POST /erp/supply-chain/v1/trade/quotes/{ref}/accept  接受报价 → 生成订单

身份：走既有 `X-ERP-Tenant` 头闸（协商是**商家侧**动作，不是匿名面）。委托头
`x-erp-grant-*` 由网关注入（与 `/shop` 同一纪律，客户端自带的会被剥掉）。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request

from fd_erp_vertical_templates.errors import TemplateError

from .drivers import Drivers
from .envelope import ok_shell
from .errors import ApiError, adapt_exception

#: 买方标识头（询价单归属与未关闭上限按它计）。缺省回落 "agent"。
BUYER_HEADER = "X-ERP-Trade-Buyer"


def register_trade_face(app: FastAPI, settings, drivers: Drivers) -> None:
    def _ctx(request: Request) -> Drivers:
        # 租户头闸已由 tenant_gate 中间件在更外层判定（协商面不在 SHOP_PATHS 豁免清单里），
        # 这里只做「本进程绑定的 drivers」取用，不重复判租户。
        return drivers

    def _grant(request: Request) -> tuple[str | None, str | None]:
        jti = (request.headers.get("X-ERP-Grant-Jti") or "").strip() or None
        budget = (request.headers.get("X-ERP-Grant-Budget") or "").strip() or None
        return jti, budget

    @app.post("/erp/supply-chain/v1/trade/demands", status_code=201)
    async def create_demand(request: Request):
        d = _ctx(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        buyer = (request.headers.get(BUYER_HEADER) or "").strip() or "agent"
        try:
            out = d.trade.create_demand(
                lines=body.get("lines") or [],
                needed_by=body.get("neededBy") or body.get("needed_by"),
                ship_to=body.get("shipTo") or body.get("ship_to") or {},
                buyer_ref=buyer, requested_by="agent")
        except TemplateError as exc:
            raise adapt_exception(exc) from exc
        return ok_shell(out)

    @app.post("/erp/supply-chain/v1/trade/quotes", status_code=201)
    async def create_quote(request: Request):
        d = _ctx(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("INVALID_ARGUMENT", "请求体须为 JSON 对象")
        try:
            out = d.trade.create_quote(
                demand_ref=str(body.get("demandRef") or body.get("demand_ref") or ""),
                unit_price=body.get("unitPrice", body.get("unit_price")),
                currency=str(body.get("currency") or "CNY"),
                valid_until=body.get("validUntil", body.get("valid_until")),
                lead_time_days=body.get("leadTimeDays", body.get("lead_time_days")),
                moq=body.get("moq"),
                qty_offered=body.get("qtyOffered", body.get("qty_offered")),
                product_code=str(body.get("productId") or body.get("product_code") or ""),
                quoted_by="merchant")
        except TemplateError as exc:
            raise adapt_exception(exc) from exc
        return ok_shell(out)

    @app.post("/erp/supply-chain/v1/trade/quotes/{quote_ref}/accept",
              status_code=201)
    async def accept_quote(quote_ref: str, request: Request):
        d = _ctx(request)
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
        from datetime import date
        buyer = (request.headers.get(BUYER_HEADER) or "").strip() or "agent"
        try:
            out = d.trade.accept_quote(
                quote_ref=quote_ref, qty=body.get("qty"),
                orchestrator=d.orders, day=date.today().isoformat(),
                channel=channel, ship_to=ship, buyer_ref=buyer,
                requested_by=("agent" if jti else "merchant"),
                grant_jti=jti, grant_budget=budget)
        except TemplateError as exc:
            raise adapt_exception(exc) from exc
        return ok_shell(out)
