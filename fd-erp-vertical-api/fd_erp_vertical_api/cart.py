"""进货单（cart）：进程内暂存的下单前 UI 状态 —— 非账务事实、非主数据。

边界定位（如实声明）：
- 进货单行是「当次取价快照」的暂存态：加行时刻经 pricing.resolve_price 取价
  留痕；价格预览与下单一律重新取价（不信任暂存价）；
- 不存任何账务事实（金额合计仅为展示），不占用信用额度（纯读预览）；
- 进程内存储（重启即失，零落库）：正式订单以编排层事件档为准（确认车道）。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any


class CartStore:
    """进程内进货单：键 (tenant_id, customer_id)，行含当次取价快照。"""

    def __init__(self) -> None:
        self._carts: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def lines(self, tenant_id: str, customer_id: str) -> list[dict[str, Any]]:
        return [dict(ln) for ln in self._carts.get((tenant_id, customer_id), [])]

    def add_line(self, tenant_id: str, customer_id: str,
                 snapshot: dict[str, Any]) -> dict[str, Any]:
        key = (tenant_id, customer_id)
        seed = f"{tenant_id}|{customer_id}|{snapshot['product']}|{time.time_ns()}"
        line = {"line_id": hashlib.sha1(seed.encode()).hexdigest()[:16],
                "qty": snapshot["qty"], "unit": snapshot.get("unit") or "",
                "added_at_note": "行快照为加行当次取价；下单/预览一律重新取价",
                **snapshot}
        self._carts.setdefault(key, []).append(line)
        return line

    def remove_line(self, tenant_id: str, customer_id: str,
                    line_id: str) -> list[dict[str, Any]]:
        key = (tenant_id, customer_id)
        lines = self._carts.get(key, [])
        kept = [ln for ln in lines if ln["line_id"] != line_id]
        if len(kept) == len(lines):
            from .errors import ApiError
            raise ApiError("NOT_FOUND", f"进货单行不存在：{line_id}",
                           detail={"line_id": line_id})
        self._carts[key] = kept
        return [dict(ln) for ln in kept]

    def clear(self, tenant_id: str, customer_id: str) -> None:
        self._carts.pop((tenant_id, customer_id), None)
