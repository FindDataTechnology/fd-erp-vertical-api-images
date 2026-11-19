"""统一响应壳 + 分页续读（沿内核 result.py 语义，本包本地实现零内核 import）。

响应壳（契约固定）：
- 成功：{"ok": true, "data": ..., "rows_returned": n}
- 失败：{"ok": false, "error": {code, message, hint, detail}, "rows_returned": 0}

rows_returned 语义同内核 result.stamp：data 为数组时按行计；对象/写成功不可计
行数时落 0（以缺行数承载「调用入账、行数不可计」）。

体积上限 + 续读同内核 RESULT_MAX_ROWS=500：不静默截断、不放开上限；超限返回
truncated 标记 + 原始规模 + continuation（offset 续读）。
"""
from __future__ import annotations

from typing import Any

from .errors import ApiError

#: 单次响应统一体积上限（内核 result.py 同值同语义）
RESULT_MAX_ROWS = 500


def ok_shell(data: Any) -> dict[str, Any]:
    """成功壳：ok + data + rows_returned（数组按行计，其余 0 —— 内核 stamp 语义）。"""
    rows = len(data) if isinstance(data, list) else 0
    return {"ok": True, "data": data, "rows_returned": rows}


def error_shell(err: Any) -> dict[str, Any]:
    """失败壳：ok=false + error{code,message,hint,detail} + rows_returned=0。"""
    return {"ok": False, "error": err.to_error(), "rows_returned": 0}


def parse_page_params(offset: Any, limit: Any) -> tuple[int, int]:
    """查询串分页参数解析：非法（负数/非整数）一律 INVALID_ARGUMENT。"""
    try:
        off = int(offset) if offset is not None else 0
        lim = int(limit) if limit is not None else RESULT_MAX_ROWS
    except (TypeError, ValueError) as exc:
        raise ApiError("INVALID_ARGUMENT", "offset/limit 必须为整数",
                       detail={"offset": offset, "limit": limit}) from exc
    if off < 0 or lim < 0:
        raise ApiError("INVALID_ARGUMENT", "offset/limit 必须为非负整数",
                       detail={"offset": offset, "limit": limit})
    return off, lim


def paginate(rows: list[dict[str, Any]], *, offset: int, limit: int) -> dict[str, Any]:
    """统一上限 + 截断标记 + 原始规模 + 续读方式（内核 result.paginate 同语义）。

    不静默截断、不放开上限：cap=min(limit, RESULT_MAX_ROWS)；truncated 时附
    continuation={method: offset, next_offset}。
    """
    cap = min(limit, RESULT_MAX_ROWS) if limit else RESULT_MAX_ROWS
    window = rows[offset:offset + cap]
    total = len(rows)
    out: dict[str, Any] = {
        "data": window,
        "total": total,
        "offset": offset,
        "limit": cap,
        "truncated": offset + len(window) < total or total > RESULT_MAX_ROWS,
    }
    if out["truncated"]:
        out["continuation"] = {"method": "offset", "next_offset": offset + len(window)}
    return out


def list_shell(rows: list[dict[str, Any]], *, offset: int, limit: int,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """列表端点成功壳：分页窗口 + rows_returned（窗口行数）+ 附加元数据。"""
    page = paginate(rows, offset=offset, limit=limit)
    body: dict[str, Any] = {"ok": True, "rows_returned": len(page["data"])}
    body.update(page)
    if extra:
        body.update(extra)
    return body
