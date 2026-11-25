"""目录 HTTP 面（change establish-ai-native-trade-discovery 1.1/1.3）。

四条路由，全部**公开**（发现是匿名动作——访客没有账号）：

  GET  /health                       探针
  GET  /directory/shops              列出已收录的店（不含已撤回）
  POST /directory/shops              登记/更新（幂等，按 tenant 收敛）
  POST /directory/shops/{tenant}/revoke  撤回登记
  GET  /directory/search             跨店检索（三态可区分）

**写操作要不要认证**：目录写代表「这家店对外声称自己是谁」。当前部署在内网 NodePort，
不走公网；一旦对外暴露，写操作必须加商家身份（与 `establish-ai-native-ecommerce-stack`
的「公开读面 vs 商家面」同一口径）。此处的选择是**先只做读面公开**，写面由
`SHOP_DIRECTORY_WRITE_TOKEN` 守卫（未设 = 只允许集群内调用，即无 token 时写 403）。
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .directory import Directory, DirectoryUnavailable, ShopEntry

WRITE_TOKEN_ENV = "SHOP_DIRECTORY_WRITE_TOKEN"


class WriteDenied(PermissionError):
    """目录写面被拒（未启用 / 令牌错）。403，与「不存在的店」区分。"""


def _err(status: int, code: str, message: str, **detail: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "ok": False,
        "error": {"code": code, "message": message, "detail": detail},
        "rows_returned": 0,
    })


def create_app(data_path: str | None = None) -> FastAPI:
    app = FastAPI(title="fd-shop-directory", version="0.1.0",
                  description="店目录：跨店发现的唯一入口（只存指针与类目）")
    root = data_path or os.environ.get("SHOP_DIRECTORY_DATA") or "/data/shop-directory.jsonl"
    app.state.directory = Directory(root)

    def _write_guard(request: Request) -> None:
        """写面守卫：设了 token 就必须带，没设则只允许集群内（无 token 即 403）。

        为什么默认拒绝而不是默认放行：目录写错了会让 agent 去一家不存在的店下单
        （或更糟：指向别人的公开面）。默认放行等于把这个风险交给「谁想起来设」。
        """
        want = os.environ.get(WRITE_TOKEN_ENV, "").strip()
        if not want:
            raise WriteDenied("目录写面未启用：设置 SHOP_DIRECTORY_WRITE_TOKEN 后才可登记")
        got = (request.headers.get("x-directory-write-token") or "").strip()
        if got != want:
            raise WriteDenied("目录写令牌无效")

    @app.exception_handler(WriteDenied)
    async def _denied(_request: Request, exc: WriteDenied):
        return _err(403, "WRITE_DENIED", str(exc))

    @app.exception_handler(DirectoryUnavailable)
    async def _unavailable(_request: Request, exc: DirectoryUnavailable):
        # 503 而不是空数组：调用方必须能区分「目录挂了」与「没有匹配」（spec 三态）。
        return _err(503, "DIRECTORY_UNAVAILABLE", str(exc))

    @app.get("/health")
    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "fd-shop-directory"}

    @app.get("/directory/shops")
    async def list_shops() -> dict[str, Any]:
        rows = [e.as_row() for e in app.state.directory.entries()]
        return {"ok": True, "data": rows, "rows_returned": len(rows)}

    @app.get("/directory/search")
    async def search(category: str = "", q: str = "", region: str = "",
                     tenant: str = "") -> dict[str, Any]:
        return app.state.directory.search(category=category, q=q, region=region,
                                          tenant=tenant)

    @app.post("/directory/shops")
    async def register(request: Request) -> Any:
        _write_guard(request)
        body = await request.json()
        if not isinstance(body, dict):
            return _err(400, "INVALID_ARGUMENT", "请求体须为 JSON 对象")
        cats = body.get("categories") or []
        kws = body.get("keywords") or []
        if not isinstance(cats, list) or not isinstance(kws, list):
            return _err(400, "INVALID_ARGUMENT", "categories/keywords 须为数组")
        try:
            entry = ShopEntry(
                tenant=str(body.get("tenant") or "").strip(),
                title=str(body.get("title") or "").strip(),
                public_base=str(body.get("public_base") or "").strip(),
                categories=[str(c) for c in cats],
                keywords=[str(k) for k in kws],
                region=str(body.get("region") or "").strip(),
            )
        except (TypeError, ValueError) as exc:
            return _err(400, "INVALID_ARGUMENT", f"登记项非法：{exc}")
        if not entry.tenant or not entry.public_base:
            return _err(400, "INVALID_ARGUMENT", "tenant 与 public_base 必填",
                        missing=[k for k in ("tenant", "public_base")
                                 if not getattr(entry, k)])
        return app.state.directory.register(entry)

    @app.post("/directory/shops/{tenant}/revoke")
    async def revoke(tenant: str, request: Request) -> Any:
        _write_guard(request)
        return app.state.directory.revoke(tenant)

    return app
