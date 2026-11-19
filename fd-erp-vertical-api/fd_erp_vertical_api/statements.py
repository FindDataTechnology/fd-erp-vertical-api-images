"""对账单域：客户对账单派生 + 确认门 + 异议处置车道。

数据源（零旁路，与契约「query_entries/reports」对位）：
- 行项目 = 编排层订单事件档（OrderOrchestrator，本人所属客户）按月聚合，
  每行携带取价规则链 provenance；信用占用侧 provenance 来自订单 payload 内
  的内核 reserve_credit 结果（kernel_result）；
- 状态真源 = 内核 proposal 面：确认动作提交「自平核对分录」proposal（pending，
  借贷同科目等额，落账净额为零、不改任何余额 —— 沿 recon/writeback 白名单
  先例）进确认门；list_proposals 按 stable_id 对位得 confirmed/rejected；
  REST 不直接落账，也绝不假报已确认（提交回执如实为 pending）；
- 异议（disputed）= ThresholdEvaluator 处置任务派单（确认车道 pending），
  进入内部处置车道。

如实挂账：金蝶往来面同源拉取（连接器 read-back）属任务 3.1 挂账（待客户
凭证），当前行项目源为编排层订单 + 内核信用占用 —— README 注记。
"""
from __future__ import annotations

import hashlib
from typing import Any

from fd_erp_vertical_templates.errors import TemplateError

from .drivers import Drivers
from .errors import ApiError
from .kernel_client import proposal_rows


def _statement_id(tenant: str, customer_id: str, period: str) -> str:
    seed = f"statement|{tenant}|{customer_id}|{period}"
    return f"ST-{hashlib.sha1(seed.encode()).hexdigest()[:10].upper()}"


def _stable_id(tenant: str, sid: str) -> str:
    return f"statement-confirm|{tenant}|{sid}"


class StatementService:
    """对账单派生与确认/异议车道（kernel 只经 KernelClient 协议触达）。"""

    def __init__(self, drivers: Drivers, kernel):
        self._d = drivers
        self._kernel = kernel

    # ---- 派生 ----

    def _order_events(self, customer_id: str) -> list[dict[str, Any]]:
        """本人所属客户的订单当前态（cancelled 不进对账单，如实排除）。"""
        state: dict[str, dict[str, Any]] = {}
        for ev in self._d.orders._events():  # 编排层事件档（同源 read 面）
            if ev.get("record") != "order":
                continue
            payload = ev.get("payload") or {}
            ref = ev.get("ref")
            phase = ev.get("event")
            if phase == "proposed":
                if payload.get("customer_id") == customer_id:
                    state[ref] = {"phase": "proposed", "payload": dict(payload)}
            elif phase in ("confirmed", "cancelled") and ref in state:
                merged = dict(state[ref]["payload"])
                merged.update(payload)
                state[ref] = {"phase": phase, "payload": merged}
        return [{"ref": ref, **v} for ref, v in state.items()
                if v["phase"] != "cancelled"]

    def _confirmation_states(self) -> dict[str, str]:
        """stable_id → proposal 状态（内核 proposal 面为状态真源）。"""
        out: dict[str, str] = {}
        listed = self._kernel.list_proposals()
        for row in proposal_rows(listed):
            sid = str(row.get("stable_id") or "")
            if sid.startswith("statement-confirm|"):
                out[sid] = str(row.get("status") or "")
        return out

    def list_statements(self, customer_id: str, *, status: str | None = None,
                        period: str | None = None) -> list[dict[str, Any]]:
        """按月聚合订单 → 对账单列表（状态经内核 proposal 面解析）。"""
        buckets: dict[str, dict[str, Any]] = {}
        for item in self._order_events(customer_id):
            payload = item["payload"]
            day = str(payload.get("day") or "")
            if len(day) < 7:
                continue
            key = day[:7]
            if period and key != period:
                continue
            bucket = buckets.setdefault(key, {
                "period": key, "orders": [], "amount_total": 0.0,
                "currency": payload.get("currency") or "CNY"})
            bucket["orders"].append({
                "order_ref": item["ref"],
                "day": day,
                "amount_total": payload.get("amount_total"),
                "order_status": item["phase"],
                "lines": payload.get("lines") or [],
                "credit_reservation": payload.get("credit_reservation"),
            })
            try:
                bucket["amount_total"] += float(payload.get("amount_total") or 0)
            except (TypeError, ValueError):
                pass
        states = self._confirmation_states()
        out: list[dict[str, Any]] = []
        for key in sorted(buckets):
            sid = _statement_id(self._d.tenant_id, customer_id, key)
            bucket = buckets[key]
            st = states.get(_stable_id(self._d.tenant_id, sid))
            resolved = ("confirmed" if st == "confirmed"
                        else "disputed_pending" if st == "rejected"
                        else "confirming" if st == "pending" else "open")
            if status and resolved != status:
                continue
            out.append({
                "statement_id": sid, "period": key,
                "customer_id": customer_id,
                "amount_total": f"{bucket['amount_total']:.6f}".rstrip("0").rstrip("."),
                "currency": bucket["currency"], "status": resolved,
                "order_count": len(bucket["orders"]),
                "orders": bucket["orders"],
                "source_note": "行项目=编排层订单事件（同源 read 面）；"
                               "金蝶往来同源拉取属任务 3.1 挂账",
            })
        return out

    def get_statement(self, customer_id: str, statement_id: str) -> dict[str, Any]:
        for st in self.list_statements(customer_id):
            if st["statement_id"] == statement_id:
                return st
        raise ApiError("NOT_FOUND", f"对账单不存在或不属于当前客户：{statement_id}",
                       detail={"statement_id": statement_id})

    # ---- 确认门 / 异议 ----

    def confirm(self, customer_id: str, statement_id: str, *,
                confirmed_by: str = "customer") -> dict[str, Any]:
        """确认回执：自平核对分录 proposal（pending）进确认门 —— 不假报成功。

        幂等：stable_id 幂等键在内核侧；已有 pending/confirmed/rejected 提案时
        如实回放既有状态，不重推。
        """
        statement = self.get_statement(customer_id, statement_id)
        sid_stable = _stable_id(self._d.tenant_id, statement_id)
        existing = self._confirmation_states().get(sid_stable)
        if existing == "confirmed":
            return {"statement_id": statement_id, "proposal_status": "confirmed",
                    "duplicate": True, "note": "该对账单确认已过确认门（幂等回放）"}
        if existing == "pending":
            return {"statement_id": statement_id, "proposal_status": "pending",
                    "duplicate": True,
                    "note": "确认提案已在确认门待决（幂等回放，不重推）"}
        amount = statement["amount_total"] or "0"
        lines = [
            {"account_code": "1300", "direction": "debit", "amount": amount,
             "currency": statement["currency"]},
            {"account_code": "1300", "direction": "credit", "amount": amount,
             "currency": statement["currency"]},
        ]
        prop = self._kernel.submit_proposal(
            stable_id=sid_stable,
            lines=lines, kind="journal",
            lane_post="statement-confirm", lane_title="对账单确认（客户回执）",
            source_ref=f"statement:{statement_id}",
            submitted_by=confirmed_by)
        return {
            "statement_id": statement_id,
            "proposal_id": prop.get("proposal_id"),
            "proposal_status": "pending",
            "duplicate": bool(prop.get("duplicate")),
            "note": "确认已提交内核确认门（pending）：确认落账前对账单不记已确认；"
                    "自平核对分录净额为零，不改任何余额",
        }

    def dispute(self, customer_id: str, statement_id: str, note: str) -> dict[str, Any]:
        """异议 → 内部处置车道（ThresholdEvaluator 派单，pending 确认车道）。"""
        self.get_statement(customer_id, statement_id)  # 域校验：存在且属本人
        if not str(note or "").strip():
            raise ApiError("INVALID_ARGUMENT", "异议须附 disputeNote（可解释拒绝纪律）")
        alert = {"triggered": True, "metric": "statement_disputed",
                 "value": statement_id, "severity": "high",
                 "disposal_task": "对账单异议处置：核对差异、回执客户并留痕"}
        try:
            task = self._d.disposal.raise_disposal_task(
                alert, title=f"对账单异议：{statement_id}（{str(note)[:80]}）")
        except TemplateError as exc:  # 车道装配异常如实上抛，不吞
            raise ApiError("INTERNAL", f"处置任务派单失败：{exc.message}",
                           detail=exc.detail) from exc
        return {"statement_id": statement_id, "disputed": True,
                "disposal_request_id": task.get("request_id"),
                "disposal_status": task.get("status"),
                "note": "异议进入内部处置车道（确认车道 pending）；"
                        "对账单状态标 disputed_pending"}
