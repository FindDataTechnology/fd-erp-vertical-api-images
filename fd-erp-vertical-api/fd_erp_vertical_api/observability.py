"""运行指标：`GET /metrics`（optimize-erp-solution 6.1 收尾，API 侧）。

为什么单独一个模块：内核那边已有 `fd_erp_ledger_mcp/observability.py`（账务面指标），
本服务是 REST 面，关心的是「哪些端点被打、打成什么状态码、错误码分布」。两者同形
（prometheus_client + 纯内存 + 零数据库往返），但指标族不同，不共用注册表。

暴露面纪律（与内核一致）：
- 只绑集群内地址、不挂公网 Ingress（清单侧约束）
- 指标不带租户明文：本服务一进程一租户，租户是常量，进了标签只会让基数虚高且泄露
- 路径标签用**路由模板**（`/ue/orders/{order_id}`）而非原始 URL，否则 id 进标签会
  把序列数打爆
"""
from __future__ import annotations

import re
from typing import Iterable

from prometheus_client import (CONTENT_TYPE_LATEST, CollectorRegistry, Counter,
                               Histogram, generate_latest)

#: 桶集**只许 import**，不许在本文件手写字面量（change 3.5）：两处 histogram 各自手写时，
#: 一边多一个 10.0 上界而两边各自都自洽 —— 只有跨服务比才看得出。真源在
#: `fd-erp/shared/semantics-contract.json` 的 `metrics.canonical`，由
#: `fd-erp/tools/gen_metrics_buckets.py` 生成；`check_semantics_contract.py` 会拦住手写。
from ._buckets_generated import SECONDS_BUCKETS

#: 独立注册表：不污染 prometheus_client 的全局默认注册表（测试与多 app 实例友好）
REGISTRY = CollectorRegistry(auto_describe=True)

REQUESTS = Counter(
    "erp_api_requests_total",
    "REST 请求数（按路由模板 + 方法 + 状态码）",
    ["route", "method", "status"],
    registry=REGISTRY,
)
ERRORS = Counter(
    "erp_api_errors_total",
    "业务错误数（按封闭错误码）",
    ["code"],
    registry=REGISTRY,
)
LATENCY = Histogram(
    "erp_api_request_seconds",
    "REST 请求耗时（按路由模板）",
    ["route"],
    registry=REGISTRY,
    buckets=SECONDS_BUCKETS,
)

#: 形如 /erp/supply-chain/v1/ue/orders/<任意 id> 的路径段替换成 {id}。
#: 只替换「最后一段且不像固定词」的 id；固定词（orders/products 等）保留原样。
_ID_SEGMENT = re.compile(r"/([0-9a-fA-F]{6,}|[A-Z]+-[A-Z0-9-]{3,}|\d+)(?=/|$)")


def route_template(path: str) -> str:
    """把具体 id 归一到路由模板，避免标签基数爆炸。"""
    path = path.split("?", 1)[0]
    return _ID_SEGMENT.sub("/{id}", path)


def requests_observe(route: str, method: str, status: int, seconds: float) -> None:
    REQUESTS.labels(route=route, method=method, status=str(status)).inc()
    LATENCY.labels(route=route).observe(seconds)


def error_observe(code: str) -> None:
    """记一次业务错误（错误码来自封闭集 errors.ERROR_CATALOG）。"""
    ERRORS.labels(code=str(code or "UNKNOWN")).inc()


def instrument_middleware(app) -> None:
    """把计数挂到每条请求上。

    走 http 中间件而不是逐端点装饰器：本服务 63 条路由，逐条加埋点既易漏又会把
    路由清单写两遍。中间件是唯一必经点，且能拿到 status_code。
    `/metrics` 自身不计数（否则每抓一次就自我放大）。
    """
    import time

    @app.middleware("http")
    async def _metrics_mw(request, call_next):
        if request.url.path == "/metrics":
            return await call_next(request)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            try:
                requests_observe(route_template(request.url.path),
                                 request.method, status,
                                 time.perf_counter() - started)
            except Exception:  # noqa: BLE001 - 观测失败绝不影响业务响应
                pass


def render_metrics() -> bytes:
    return generate_latest(REGISTRY)


def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST


def reset_for_tests() -> None:
    """测试用：清空计数（注册表是模块级单例，跨用例会互相污染）。"""
    for metric in (REQUESTS, ERRORS, LATENCY):
        metric.clear()


__all__: Iterable[str] = (
    "REGISTRY", "REQUESTS", "ERRORS", "LATENCY", "route_template",
    "requests_observe", "error_observe", "instrument_middleware",
    "render_metrics", "metrics_content_type", "reset_for_tests",
)
