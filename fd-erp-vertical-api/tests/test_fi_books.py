"""三本账端点（change: add-fi-books-endpoints）。

判据（对应 spec `erp-cn-localization` 新增要求）：

1. 余额表：期初 + 本期借 − 本期贷 = 期末、逐科目；不平如实带 `error`；
2. 表外科目仍出行并被列进 `unclassified`（不静默归类）；
3. 明细账逐行余额正确；`limit` 只截显示行，合计与期末按全量；
4. 往来对账单只认显式维度，指定 `accounts` 时对回总账；
5. 三本账只读；缺必填参数 400（不给空表）。

零替身：真库 + InProcess 内核 + 真实车道。
"""
from __future__ import annotations

import json

import pytest

from fd_erp_vertical_api.pagesets import load_pageset_auth

FI_RO = {"supply-chain.fi": ["finance.read"]}
FI_RW = {"supply-chain.fi": ["finance.read", "finance.write"]}
TENANT_HEADER = {"X-ERP-Tenant": "tnt-demo"}
BASE = "/erp/supply-chain/v1/fi"
PERIOD = "2026-11"
H = {**TENANT_HEADER, "X-ERP-Staff": "cfo@example"}
STAFF_ROSTER = json.dumps({"read": ["cfo@example"],
                           "write": ["cfo@example"],
                           "confirm": ["cfo@example"]})


def _app(tmp_path, dsn, grants):
    from fastapi.testclient import TestClient

    from fd_erp_vertical_api.app import create_app
    from fd_erp_vertical_api.cart import CartStore
    from fd_erp_vertical_api.config import Settings
    from fd_erp_vertical_api.kernel_client import InProcessKernelClient

    payload = json.dumps(grants)
    settings = Settings(tenant_id="tnt-demo", database_url=dsn,
                        base_currency="CNY", data_dir=str(tmp_path / "data"),
                        pageset_auth_json=payload, finance_staff_json=STAFF_ROSTER)
    kernel = InProcessKernelClient(settings)
    app = create_app(settings, kernel,
                     pageset_auth=load_pageset_auth(inline_json=payload),
                     cart_store=CartStore())
    return TestClient(app)


@pytest.fixture()
def ro(reset_db, tmp_path, dsn):
    return _app(tmp_path, dsn, FI_RO)


def _confirm_all(d) -> int:
    pending = d.kernel.list_proposals(status="pending")["data"]
    for prop in pending:
        d.kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")
    return len(pending)


def _seed(client) -> None:
    """一笔销售（挂应收 C-A）+ 一笔采购（挂应付）。"""
    d = client.app.state.drivers
    d.cn_tax.post_sale("BK-S1", "1130", rate="13", gross=True, customer="C-A",
                       booked_on="2026-11-05", submitted_by="cfo@example")
    d.cn_tax.post_purchase("BK-P1", "565", rate="13", gross=True,
                           booked_on="2026-11-06", submitted_by="cfo@example")
    _confirm_all(d)


def _pending(d) -> int:
    return len(d.kernel.list_proposals(status="pending")["data"])


def _row(payload, code):
    return next(r for r in payload["rows"] if r["accountCode"] == code)


# ---- 1) 科目余额表 ----

def test_balances_on_an_empty_ledger(ro):
    body = ro.get(f"{BASE}/books/balances", params={"period": PERIOD},
                  headers=H).json()["data"]
    assert body["rows"] == []
    assert body["balanced"] is True
    assert body["totals"] == {"opening": "0.00", "debit": "0.00",
                              "credit": "0.00", "closing": "0.00"}


def test_balances_is_three_column_and_balances(ro):
    _seed(ro)
    d = ro.app.state.drivers
    before = len(d.kernel.query_entries(account_code="1100")["entries"])

    body = ro.get(f"{BASE}/books/balances", params={"period": PERIOD},
                  headers=H).json()["data"]
    assert body["balanced"] is True, body.get("error")
    assert body["totals"]["debit"] == body["totals"]["credit"]

    ar = _row(body, "1100")                       # 应收：借 1130
    assert ar["debit"] == "1130.00" and ar["credit"] == "0.00"
    assert ar["closing"] == "1130.00" and ar["direction"] == "debit"
    ap = _row(body, "2000")                       # 应付：贷 565
    assert ap["credit"] == "565.00" and ap["closing"] == "-565.00"
    assert ap["direction"] == "credit"
    # 只读：分录条数不变
    assert len(d.kernel.query_entries(account_code="1100")["entries"]) == before


# ---- 2) 明细账 ----

def test_ledger_running_balance(ro):
    _seed(ro)
    body = ro.get(f"{BASE}/books/ledger/1100", params={"period": PERIOD},
                  headers=H).json()["data"]
    assert body["accountCode"] == "1100"
    assert body["opening"] == "0.00"
    assert body["rows"][-1]["balance"] == "1130.00"    # 逐行余额走到期末
    assert body["closingBalance"] == "1130.00"
    assert body["balanced"] is True


def test_ledger_limit_truncates_only_the_displayed_rows(ro):
    """截断只截显示行：合计与期末仍按全量算，否则「最后一行余额」会骗人。"""
    d = ro.app.state.drivers
    for i in range(3):
        d.cn_tax.post_sale(f"BK-M{i}", "113", rate="13", gross=True, customer="C-A",
                           booked_on=f"2026-11-0{5 + i}", submitted_by="cfo@example")
    _confirm_all(d)

    body = ro.get(f"{BASE}/books/ledger/1100",
                  params={"period": PERIOD, "limit": 2}, headers=H).json()["data"]
    assert body["truncated"] is True
    assert body["rowCount"] == 3
    assert len(body["rows"]) == 2
    # 合计与期末按三行算（113 × 3）
    assert body["closingBalance"] == "339.00"
    assert body["totals"]["debit"] == "339.00"


def test_ledger_requires_an_account(ro):
    res = ro.get(f"{BASE}/books/ledger/%20", headers=H)
    assert res.status_code in (400, 404, 405), res.text


# ---- 3) 往来对账单 ----

def test_party_statement_reconciles_to_the_ledger(ro):
    _seed(ro)
    body = ro.get(f"{BASE}/books/party-statement",
                  params={"dimension": "customer", "value": "C-A",
                          "period": PERIOD, "accounts": "1100"},
                  headers=H).json()["data"]
    assert body["dimensionValue"] == "C-A"
    assert body["closingBalance"] == "1130.00"          # 余额口径「借 − 贷」：应收为正
    assert body["direction"] == "debit"
    assert body["reconciled"] is True
    assert body["consistent"] is True, body.get("error")
    check = body["reconciliation"][0]
    assert check["accountCode"] == "1100"
    assert check["thisParty"] == "1130.00"
    assert check["difference"] == "0.00"


def test_party_statement_lists_unattributed_entries(ro):
    """未标维度的同类分录被单列进 `unattributed`，不摊给任何往来方。"""
    d = ro.app.state.drivers
    d.cn_tax.post_sale("BK-S2", "226", rate="13", gross=True, customer="C-A",
                       booked_on="2026-11-07", submitted_by="cfo@example")
    d.cn_tax.post_sale("BK-S3", "113", rate="13", gross=True,
                       booked_on="2026-11-08", submitted_by="cfo@example")
    _confirm_all(d)

    body = ro.get(f"{BASE}/books/party-statement",
                  params={"dimension": "customer", "value": "C-A",
                          "period": PERIOD, "accounts": "1100"},
                  headers=H).json()["data"]
    check = body["reconciliation"][0]
    assert check["thisParty"] == "226.00"
    assert check["unattributed"] == "113.00"            # 那笔没标客户的
    assert check["ledgerClosing"] == "339.00"
    assert check["difference"] == "0.00"
    assert body["consistent"] is True


def test_party_statement_without_accounts_skips_reconciliation(ro):
    _seed(ro)
    body = ro.get(f"{BASE}/books/party-statement",
                  params={"dimension": "customer", "value": "C-A",
                          "period": PERIOD}, headers=H).json()["data"]
    assert body["reconciled"] is False
    assert "reconciliation" not in body


@pytest.mark.parametrize("params", [
    {"value": "C-A"},                                   # 缺 dimension
    {"dimension": "customer"},                          # 缺 value
])
def test_party_statement_requires_both_keys(ro, params):
    res = ro.get(f"{BASE}/books/party-statement", params=params, headers=H)
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "INVALID_ARGUMENT"


# ---- 4) 序时账与凭证（change: add-fi-voucher-and-journal）----

def test_journal_lists_vouchers_with_balanced_totals(ro):
    _seed(ro)
    body = ro.get(f"{BASE}/books/journal", params={"period": PERIOD},
                  headers=H).json()["data"]
    assert body["balanced"] is True, body.get("error")
    # 销售 3 行（应收 / 收入 / 销项）+ 采购 3 行（成本 / 进项 / 应付）
    assert body["vouchers"] == 2 and body["lines"] == 6
    rows = {r["sourceRef"]: r for r in body["rows"]}
    sale = rows["vat-sale:BK-S1"]
    assert sale["debit"] == "1130.00" and sale["credit"] == "1130.00"
    assert sale["balanced"] is True
    assert sale["isClosing"] is False


def test_journal_limit_truncates_only_the_listing(ro):
    _seed(ro)
    body = ro.get(f"{BASE}/books/journal",
                  params={"period": PERIOD, "limit": 1}, headers=H).json()["data"]
    assert body["truncated"] is True and body["rowCount"] == 2
    assert len(body["rows"]) == 1
    assert body["totals"]["debit"] == "1695.00"              # 1130 + 565，按全量


def test_voucher_opens_the_entries_behind_a_ledger_line(ro):
    """从明细账拿 txnId → 打开凭证看借贷两方（查账的最后一步）。"""
    _seed(ro)
    ledger = ro.get(f"{BASE}/books/ledger/1100", params={"period": PERIOD},
                    headers=H).json()["data"]
    txn = ledger["rows"][0]["txnId"]
    assert txn

    body = ro.get(f"{BASE}/books/voucher/{txn}", headers=H).json()["data"]
    assert body["txnId"] == txn
    assert body["balanced"] is True, body.get("error")
    codes = [r["accountCode"] for r in body["lines"]]
    assert "1100" in codes and "4000" in codes
    assert body["totals"]["debit"] == body["totals"]["credit"]
    assert body["sourceRef"] == "vat-sale:BK-S1"


def test_voucher_surfaces_a_closing_voucher(ro):
    d = ro.app.state.drivers
    _seed(ro)
    d.closing.transfer(PERIOD, submitted_by="cfo@example")
    _confirm_all(d)
    journal = ro.get(f"{BASE}/books/journal", params={"period": PERIOD},
                     headers=H).json()["data"]
    closing = [r for r in journal["rows"] if r["isClosing"]]
    assert len(closing) == 1
    body = ro.get(f"{BASE}/books/voucher/{closing[0]['txnId']}",
                  headers=H).json()["data"]
    assert body["isClosing"] is True


def test_unknown_voucher_is_404(ro):
    res = ro.get(f"{BASE}/books/voucher/nope", headers=H)
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"
    assert res.json()["error"]["detail"]["txnId"] == "nope"


def test_books_are_read_only(ro):
    _seed(ro)
    d = ro.app.state.drivers
    before = (len(d.kernel.query_entries(account_code="1100")["entries"]),
              _pending(d))
    ro.get(f"{BASE}/books/journal", params={"period": PERIOD}, headers=H)
    txn = ro.get(f"{BASE}/books/ledger/1100", params={"period": PERIOD},
                 headers=H).json()["data"]["rows"][0]["txnId"]
    ro.get(f"{BASE}/books/voucher/{txn}", headers=H)
    assert (len(d.kernel.query_entries(account_code="1100")["entries"]),
            _pending(d)) == before
