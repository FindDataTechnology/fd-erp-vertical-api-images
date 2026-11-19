"""开票回执车道装配（change: add-invoice-receipt-lane 任务 2.1）。

判据：
1. `drivers.invoice_receipts` 可用；申请侧 → 审核门 → 回执 → 未开票清单走通（真内核）；
2. 顺序不倒是硬约束：申请还是 pending 时录回执必须被拒；
3. 过门之后才能录，录完该申请从缺口清单里消失。
"""
from __future__ import annotations

from fd_erp_vertical_templates.errors import TemplateError


def _accept(d, ref: str) -> None:
    """走真实确认门把申请置为 accepted（唯一改变事件状态的入口）。"""
    d.kernel.decide_event(ref, "accepted", note="审核通过")


def test_receipt_can_only_be_recorded_after_the_gate(client_faces):
    d = client_faces.app.state.drivers
    assert d.invoice_receipts is not None

    submitted = d.invoices.submit_request("C-001", {
        "title": "示范客户一号", "taxId": "91310000MA1K3XYZ12",
        "orderId": "SO-1"})
    ref = submitted["requestId"]
    assert submitted["status"] == "pending"

    # 还没过门：录回执必须被拒（先开票后补审核等于绕开确认门）
    try:
        d.invoice_receipts.record(ref, "NO-8899", "2026-11-10", "1000")
    except TemplateError as exc:
        assert "pending" in exc.message and "先过确认门再开票" in exc.message
    else:  # pragma: no cover - 走到这里说明前置校验失效
        raise AssertionError("pending 申请竟然能录回执")

    assert d.invoice_receipts.unissued(["C-001"])["total"] == 0   # 待审不算缺口

    _accept(d, ref)

    gap = d.invoice_receipts.unissued(["C-001"])
    assert [r["requestRef"] for r in gap["rows"]] == [ref]

    out = d.invoice_receipts.record(ref, "NO-8899", "2026-11-10", "1000",
                                    tax_amount="115.04",
                                    expected_amount="1000")
    assert out["match"] == "matched"
    assert d.invoice_receipts.unissued(["C-001"])["total"] == 0

    rec = d.invoice_receipts.reconciliation(["C-001"])
    assert rec["counts"] == {"issued": 1, "unissued": 0,
                             "mismatched": 0, "unverified": 0}
    assert rec["rows"][0]["receipts"][0]["invoiceNo"] == "NO-8899"


def test_void_brings_the_gap_back(client_faces):
    d = client_faces.app.state.drivers
    ref = d.invoices.submit_request("C-002", {
        "title": "示范客户二号", "taxId": "91310000MA1K3XYZ34",
        "statementId": "ST-1"})["requestId"]
    _accept(d, ref)
    d.invoice_receipts.record(ref, "NO-9900", "2026-11-11", "500")
    assert d.invoice_receipts.unissued(["C-002"])["total"] == 0

    d.invoice_receipts.void("NO-9900", reason="票号录错，税局作废后重开")
    assert [r["requestRef"] for r in d.invoice_receipts.unissued(["C-002"])["rows"]] \
        == [ref]
    # 重新登记到同一申请（作废号不占位）
    again = d.invoice_receipts.record(ref, "NO-9900", "2026-11-12", "500")
    assert again["duplicate"] is False
