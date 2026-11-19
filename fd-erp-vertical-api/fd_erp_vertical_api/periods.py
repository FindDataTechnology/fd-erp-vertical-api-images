"""期间关账的 API 驱动（change: add-period-close 任务 4.1）。

关账不是一个「竖井」能力，而是账本的时间轴：期间台账、落账闸、关账快照都在内核里
（`fd_erp_ledger_mcp.periods`）。这里只做一件事——把内核的五个动作包成驱动对象，
让 REST 面/编排层有一个稳定的绑定位置，**不重复任何判断**（顺序关账、只重开最近一期、
快照复算全在内核）。

为什么不做模板层封装：模板层（行业垂直）面向的是主子档/订单/库存这类业务对象；
「哪一期关了」是账本自身的属性，与行业无关。再包一层只会是纯转发，还会让两个地方
都能改关账语义。
"""
from __future__ import annotations

from typing import Any, Protocol


class _PeriodKernel(Protocol):
    def close_period(self, period: str, actor: str, note: str | None = None) -> Any: ...
    def reopen_period(self, period: str, actor: str, reason: str) -> Any: ...
    def period_status(self, period: str) -> Any: ...
    def period_report(self, period: str) -> Any: ...
    def list_periods(self) -> Any: ...


def _rows(payload: Any) -> list[dict[str, Any]]:
    """把内核期间清单归一成 list。

    **两种传输形态的返回形状不同**：`inprocess` 直接回 list，`mcp` 回
    `{"ok": true, "data": [...], "rows_returned": N}`。不归一的话，同一个端点在
    测试环境返回数组、在生产返回嵌套对象——而两边都「有数据」，谁也看不出错。
    （同族于 cost 车道的 `items`/`events`/`data` 键名教训。）
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "rows", "periods"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


class PeriodBook:
    """关账驱动：关账 / 重开 / 查状态 / 复算核对 / 期间清单。"""

    def __init__(self, kernel: _PeriodKernel):
        self._kernel = kernel

    def close(self, period: str, *, actor: str, note: str | None = None) -> Any:
        return self._kernel.close_period(period, actor, note=note)

    def reopen(self, period: str, *, actor: str, reason: str) -> Any:
        return self._kernel.reopen_period(period, actor, reason)

    def status(self, period: str) -> Any:
        return self._kernel.period_status(period)

    def report(self, period: str) -> Any:
        return self._kernel.period_report(period)

    def list(self) -> list[dict[str, Any]]:
        """期间清单（关账记录），形状已归一（见 `_rows`）。"""
        return _rows(self._kernel.list_periods())

    def overview(self, activity: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """期间清单 = 关账记录 ∪ 有分录的期间（change: fix-fi-surface-audit）。

        内核 `list_periods` 只是「关过或重开过的期间」；只拿它当清单，**有业务但没关账的
        月份会整月消失**——财务要选「这个月来关账」时看到空列表，分不清是「没业务」还是
        「没关过」。所以这里并上账簿按期间的凭证汇总，逐行给 `hasEntries`。
        """
        by_period: dict[str, dict[str, Any]] = {}
        for row in self.list():
            period = str(row.get("period") or "").strip()
            if not period:
                continue
            by_period[period] = {**row, "hasEntries": False,
                                 "vouchers": 0, "lines": 0, "lastBookedOn": None}
        for act in activity:
            period = str(act.get("period") or "").strip()
            if not period:
                continue
            row = by_period.setdefault(period, {
                "period": period, "closed": False, "actor": None, "decidedAt": None})
            row.update({"hasEntries": bool(act.get("hasEntries")),
                        "vouchers": int(act.get("vouchers") or 0),
                        "lines": int(act.get("lines") or 0),
                        "lastBookedOn": act.get("lastBookedOn")})
        return [by_period[p] for p in sorted(by_period)]


__all__ = ["PeriodBook"]
