"""文档承诺的响应字段必须真出现在响应里（change: guard-fi-response-field-promise）。

**这道闸防的是什么**：`INTERNAL-API.md` 的「响应要点」是散文，写「`totals` + `balanced` +
`unmapped`」时没有任何东西验证过这三个键真在响应里。散文无法比较，**字段名可以**。

同族的三次事故都长同一个样子——两份东西各自自洽、没有任何一处会比较它们：
`common.py` 声明的字段没被 chart 读过、内联 ConfigMap 副本与真源分叉、vendored 契约与所有者分叉。
这里是第四对：**文档写的** 与 **代码返回的**。

**判据的边界（刻意划清，别偷偷放宽）**：
- 只判**键名在不在**，不判取值、类型、嵌套路径——那会变成一份要跟着代码改的 JSON Schema，
  而取值口径在各自的端点测试里已经钉过；
- 只判 **GET**：写操作的响应形状由状态机决定（同一次 POST 在 `preview`/`duplicate`/
  `deferred` 下形状不同），承诺它就是把「没校验」写成「已承诺」。POST 行在文档里留 `—`；
- 列表端点对**每一行**判，除非该键登记为条件字段（那时按各行并集判）。

**种子是「月底关账后」的一个月**：期初、销售、采购、发票（含认证与红字）、税率、申报数、
坏账费率、资产、库存收发，再把 2026-10 / 2026-11 顺序关账——`GET /fi/periods/{period}/report`
只有关过账才答，而它是这张表里唯一「必须先做一件事」才能读的端点。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from fd_erp_vertical_api.pagesets import load_pageset_auth

TENANT = "tnt-demo"
BASE = "/erp/supply-chain/v1/fi"
PERIOD = "2026-11"
OPENING_PERIOD = "2026-10"          # 期初落在 2026-10-31 ⇒ 顺序关账必须先关它
ROSTER = {"read": ["cfo@example"], "write": ["cfo@example"],
          "confirm": ["cfo@example"]}
STAFF_ROSTER = json.dumps(ROSTER)
GRANTS = {"supply-chain.fi": ["finance.read", "finance.write", "finance.confirm"]}
H = {"X-ERP-Tenant": TENANT, "X-ERP-Staff": "cfo@example"}
DOC = Path(__file__).resolve().parents[1] / "INTERNAL-API.md"

# 每行至少声明几个字段：一两个字段的「承诺」等于没说，而这条下限定得低是有意的——
# 它不是质量线，是**反腐化**线（下面还有一条总数下限）。
MIN_FIELDS_PER_ROW = 2
# 总承诺字段数的显式下限。只有端点被删除时才允许下调，否则「把闸修绿」的最省力办法
# 就是删承诺。上调不需要理由：新端点本来就该多承诺几个字段。
MIN_TOTAL_FIELDS = 340

# 条件字段：**只在这条端点的特定状态下出现**的顶层键。它们不因不在场而失败，
# 但必须①已登记理由；②是该端点**声明字段的子集**（防拼错，防这张表腐烂成「什么都行」）。
MAY_BE_ABSENT: dict[tuple[str, str], str] = {
    (f"{BASE}/reports/vat-return", "warning"):
        "只有账面口径与申报口径需要提示差异时才出现；种子是一致的一期，故不知在场",
    (f"{BASE}/periods/{{period}}", "actor"):
        "未关账的期间没有关账人（种子关过账，所以它在场；换个状态就不在）",
    (f"{BASE}/periods/{{period}}", "decidedAt"):
        "未关账的期间没有决定时间",
    (f"{BASE}/periods/{{period}}", "note"):
        "关账时没写备注就没有这个键",
    (f"{BASE}/periods/{{period}}", "snapshot"):
        "只有关账记录才带快照",
    (f"{BASE}/books/balances", "unclassified"):
        "账上有落不进任何报表项目的科目时才出现（表外科目点名），正常账没有",
    (f"{BASE}/reports/cash-flow-indirect", "unclassified"):
        "直接法有落不进现金流归位表的科目时才出现",
    (f"{BASE}/reports/cash-flow-indirect", "note"):
        "有未归位科目或缺汇率提示时才出现",
    (f"{BASE}/reports/cash-flow-indirect", "error"):
        "只在两法对不上（或无法自证）时出现",
    (f"{BASE}/reports/fx-revaluation-preview", "note"):
        "有外币余额但未登记期末汇率时才出现",
}


PARAMS = {"period": PERIOD, "asOf": "2026-11-30", "dimension": "customer",
          "value": "C-A", "accounts": "1100", "sku": "SKU-001", "qty": "1",
          "warehouse": "WH-A", "limit": "5"}

# 两个账簿端点的 `truncated`/`rowCount` 只在真被截断时出现。给它 `limit=1` 而不是把它
# 们塞进豁免表——**能造出来的状态就别豁免**，豁免越多这道闸越像装饰。
PARAMS_OVERRIDE = {
    f"{BASE}/books/ledger/{{account_code}}": {"limit": "1"},
    f"{BASE}/books/journal": {"limit": "1"},
}


# ---------------------------------------------------------------- 解析文档

def _cell_fields(cell: str) -> list[str]:
    """把「响应字段」单元格拆成键名列表。反引号/顿号/空格都认——写的人会用中文标点。"""
    text = cell.replace("`", "")
    parts = re.split(r"[,，、]", text)
    return [p.strip() for p in parts if p.strip()]


def _promises(doc: str | None = None) -> dict[str, list[str]]:
    """从 INTERNAL-API.md 读出 {路径: [字段]}，只取 GET 行。"""
    text = doc if doc is not None else DOC.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    in_table = False
    for line in text.splitlines():
        if not line.startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0] == "方法" and len(cells) >= 2 and cells[1] == "路径":
            in_table = len(cells) == 6          # 端点表（六列）；§0 的四列闸表不算
            continue
        if not in_table or len(cells) != 6 or cells[0] not in ("GET", "POST"):
            continue
        m = re.search(r"`([^`]+)`", cells[1])
        if not m:
            continue
        path, cell = m.group(1), cells[5]
        if cells[0] == "GET":
            out[path] = _cell_fields(cell)
    return out


# ---------------------------------------------------------------- 判据本身（纯函数，可单测）

def _keys(payload: Any) -> set[str]:
    """响应里**必定在场**的键。

    - `data` 是对象 ⇒ 它的键；
    - `data` 是列表 ⇒ 各行的**交集**（列表承诺按行算：某一行没有这个键就是没有）；
    - 其它（空列表 / 不是对象）⇒ 空集（一条承诺都没兑现）。
    """
    if isinstance(payload, dict):
        return set(payload)
    if isinstance(payload, list):
        rows = [r for r in payload if isinstance(r, dict)]
        if not rows:
            return set()
        common = set(rows[0])
        for row in rows[1:]:
            common &= set(row)
        return common
    return set()


def _missing_fields(declared: list[str], payload: Any,
                    conditional: set[str] | None = None) -> list[str]:
    """哪些声明字段没出现。

    `conditional` 里的键**完全不参与**这条断言：它们的在场由状态决定，而这个种子只走一种
    状态。那些字段的在场另有归属——`truncated`/`rowCount` 由 `test_fi_books` 钉，
    红字标记由 `test_fi_invoices` 钉，`unclassified` 由账簿表外科目那条钉。
    """
    required = _keys(payload)
    skip = conditional or set()
    return [f for f in declared if f not in skip and f not in required]


# ---------------------------------------------------------------- 夹具

def _app(tmp_path, dsn):
    from fastapi.testclient import TestClient

    from fd_erp_vertical_api.app import create_app
    from fd_erp_vertical_api.cart import CartStore
    from fd_erp_vertical_api.config import Settings
    from fd_erp_vertical_api.kernel_client import InProcessKernelClient

    payload = json.dumps(GRANTS)
    settings = Settings(tenant_id=TENANT, database_url=dsn, base_currency="CNY",
                        data_dir=str(tmp_path / "data"), pageset_auth_json=payload,
                        finance_staff_json=STAFF_ROSTER)
    kernel = InProcessKernelClient(settings)
    app = create_app(settings, kernel,
                     pageset_auth=load_pageset_auth(inline_json=payload),
                     cart_store=CartStore())
    return TestClient(app), settings


def _confirm_all(kernel) -> int:
    pending = kernel.list_proposals(status="pending")["data"]
    for prop in pending:
        kernel.confirm_proposal(proposal_id=prop["id"], confirmed_by="cfo@example")
    return len(pending)


def _seed(client, settings) -> str:
    """把「一个月底已关账的账」造出来；返回一条真实 txnId 供 `{txnId}` 用。"""
    from fd_erp_ledger_mcp import posting
    from fd_erp_ledger_mcp.db import connection

    kernel = client.app.state.kernel
    d = client.app.state.drivers

    # 到货过账规则（人工过门）：没有它，入库投影不落分录，「存货」这条断言就没业务可依
    with connection(settings) as conn:
        drafted = posting.submit_posting_rule(
            conn, tenant_id=TENANT, event_type="goods_received",
            rule={"lines": [
                {"account_code": "1200", "direction": "debit",
                 "amount_from": "payload.total_cost"},
                {"account_code": "2000", "direction": "credit",
                 "amount_from": "payload.total_cost"}]},
            actor="cfg-eng")
        posting.approve_posting_rule(conn, tenant_id=TENANT,
                                     event_type="goods_received",
                                     version=drafted["version"], approved_by="cfo")

    d.opening.register([{"account_code": "1000", "direction": "debit", "amount": "5000"},
                        {"account_code": "3000", "direction": "credit", "amount": "5000"}],
                       as_of="2026-10-31")
    d.cn_tax.post_sale("K-S1", "2260", rate="13", gross=True, customer="C-A",
                       booked_on="2026-11-05", submitted_by="cfo@example")
    d.cn_tax.post_purchase("K-P1", "1130", rate="13", gross=True,
                           booked_on="2026-11-06", submitted_by="cfo@example")
    _confirm_all(kernel)

    d.cn_invoices.register("sale", "vat-special", "INV-1", net="1000",
                           issued_on="2026-11-07")
    d.cn_invoices.certify("INV-1", certified_on="2026-11-08", by="cfo@example")
    # 红字净额填**正数**：车道自己取反（填 -100 会被拒）
    d.cn_invoices.register_red("sale", "vat-special", "INV-R1", net="100",
                               red_of="INV-1", reason="退货", issued_on="2026-11-09")
    d.cn_filing.register_rates({"city_construction": "7", "education_surcharge": "3",
                               "local_education_surcharge": "2", "income_tax": "25"},
                              effective_from="2026-01", source="现行税法")
    d.cn_filing.register_vat_return(PERIOD, source="电子税局", sales="2000",
                                    outputVat="260", inputVat="130",
                                    inputTransferOut="0", openingCredit="0")
    d.bad_debts.set_schedule([{"maxDays": 30, "rate": "0.005"},
                              {"maxDays": None, "rate": "0.3"}],
                             registered_by="cfo@example")
    d.assets.register("FA-1", "机床", cost="12000", acquired_on="2026-01-15",
                      useful_life_months=12, registered_by="cfo@example")

    def accept(ref: str) -> None:
        for line in d.inventory.document(ref)["lines"]:
            kernel.decide_event(line["stable_id"], "accepted", note="仓管审核通过")

    rc = d.inventory.receive(warehouse="WH-A", lines=[
        {"sku": "SKU-001", "qty": "10", "unit": "个", "cost": "100", "currency": "CNY"}])
    accept(rc["ref"])
    d.inventory.project(rc["ref"])
    io = d.inventory.issue(warehouse="WH-A", lines=[
        {"sku": "SKU-001", "qty": "4", "unit": "个"}])
    accept(io["ref"])
    d.inventory.project(io["ref"])

    txn = d.books.subsidiary_ledger("1100", PERIOD)["rows"][0]["txnId"]

    # 顺序关账：越过未关期间会被拒（`2026-10` 是期初那一期）
    _confirm_all(kernel)
    for period in (OPENING_PERIOD, PERIOD):
        res = client.post(f"{BASE}/periods/{period}/close", headers=H, json={})
        assert res.status_code == 200, res.text
    return str(txn)


@pytest.fixture()
def ledger(reset_db, tmp_path, dsn):
    client, settings = _app(tmp_path, dsn)
    txn = _seed(client, settings)
    return client, txn


def _resolve(path: str, txn: str, proposal_id: str) -> str:
    return (path.replace("{period}", PERIOD).replace("{number}", "INV-1")
                # 明细账要取一个**有两笔**的科目：只有一笔的科目被 `limit=1` 截不到，
                # 「截断时才有 truncated」就永远验不出。应付账款有两笔（采购 + 到货入库）。
                .replace("{asset_id}", "FA-1").replace("{account_code}", "2000")
                .replace("{txn_id}", txn).replace("{proposal_id}", proposal_id))


# ---------------------------------------------------------------- 1) 文档自身的形态

def test_every_get_row_declares_at_least_two_fields():
    """空承诺不算通过：`—`、空白、一个字段都过不了。

    这条防的是「把闸修绿」的最省力办法——不修字段，删承诺。
    """
    bad = {p: f for p, f in _promises().items() if len(f) < MIN_FIELDS_PER_ROW}
    assert not bad, f"这些 GET 行没有真承诺：{bad}"


def test_total_promise_count_does_not_shrink():
    total = sum(len(f) for f in _promises().values())
    assert total >= MIN_TOTAL_FIELDS, (
        f"承诺字段总数 {total} < 下限 {MIN_TOTAL_FIELDS}：删端点才允许下调这个数")


def test_conditional_fields_are_declared_first():
    """豁免表的键必须先是**声明过**的字段。

    否则一个拼错的键（`unclassifed`）会安静地待在这张表里：它永远「不在场」，
    而豁免表本来就不要求在场——于是谁也不会发现它根本没被承诺过。
    """
    promises = _promises()
    bad = []
    for (path, field), _reason in MAY_BE_ABSENT.items():
        declared = promises.get(path)
        if declared is None:
            bad.append(f"{path}：文档里没有这条 GET 行")
        elif field not in declared:
            bad.append(f"{path}：{field} 未在该行声明（拼错还是表腐烂？）")
    assert not bad, "豁免表里有没被承诺的键：\n" + "\n".join(bad)


def test_every_conditional_field_has_a_reason():
    empty = [f"{p}::{f}" for (p, f), r in MAY_BE_ABSENT.items() if len(r.strip()) < 6]
    assert not empty, f"这些条件字段没写理由：{empty}"


# ---------------------------------------------------------------- 2) 判据本身能红能绿

@pytest.mark.parametrize("payload,declared,conditional,expected", [
    ({"a": 1, "b": 2}, ["a", "b"], set(), []),
    ({"a": 1}, ["a", "b"], set(), ["b"]),                        # 缺一个
    ([{"a": 1, "b": 2}, {"a": 3, "b": 4}], ["a", "b"], set(), []),
    ([{"a": 1}, {"a": 3}], ["a", "b"], set(), ["b"]),            # 行级：某一行没有就不算有
    ({"a": 1}, ["a", "b"], {"b"}, []),                           # 条件字段不参与断言
    ([{"a": 1}, {"a": 3}], ["a", "b"], {"b"}, []),               # 同上（列表形态）
    ([], ["a"], set(), ["a"]),                                   # 空列表：一条承诺都不算兑现
    ("not a payload", ["a"], set(), ["a"]),
])
def test_missing_fields_judges_both_shapes(payload, declared, conditional, expected):
    assert _missing_fields(declared, payload, conditional) == expected


# ---------------------------------------------------------------- 3) 真库：39 条 GET

def test_all_promised_fields_are_present(ledger):
    """一条一条打真库，把「声明了但没返回」的收集起来一次性报。

    不逐条 parametrize 是有意的：种子（期初 + 库存 + 两张发票 + 关两期账）代价高，
    而这里要的是**一张完整的缺口清单**，不是第一个红点。
    """
    client, txn = ledger
    proposal_id = str(client.app.state.kernel.list_proposals()["data"][0]["id"])
    promises = _promises()
    problems: list[str] = []
    for path, declared in promises.items():
        url = _resolve(path, txn, proposal_id)
        params = {**PARAMS, **PARAMS_OVERRIDE.get(path, {})}
        res = client.get(url, params=params, headers=H)
        if res.status_code != 200:
            problems.append(f"{path} → HTTP {res.status_code}：{res.text[:120]}")
            continue
        body = res.json()
        assert body.get("ok") is True, f"{path} 没走统一壳：{body}"
        cond = {f for (p, f) in MAY_BE_ABSENT if p == path}
        missing = _missing_fields(declared, body.get("data"), cond)
        if missing:
            problems.append(f"{path} → 缺 {missing}")
    assert not problems, "文档承诺了但响应里没有：\n" + "\n".join(problems)


def test_the_promise_table_matches_the_real_route_table(ledger):
    """反向两条：文档里的 GET 行必须都是真实路由（防幽灵行），
    真实路由也必须都在文档里（防新端点悄悄绕过这道闸）。

    拿 OpenAPI 面当「真实端点清单」——它比文档权威，且文档里的占位符
    （`{period}`/`{assetId}`）与 FastAPI 的路径模板逐字相同，可以直接比，不必替换。
    """
    client, _txn = ledger
    promises = set(_promises())
    spec = client.get("/openapi.json", headers=H).json()
    prefix = "/erp/supply-chain/v1/fi/"
    routes = {route for route, methods in spec["paths"].items()
              if route.startswith(prefix) and "get" in methods}
    ghosts = sorted(promises - routes)
    undocumented = sorted(routes - promises)
    assert not ghosts, f"文档里有、代码里没有的 GET 行：{ghosts}"
    assert not undocumented, f"代码里有、文档里没承诺的 GET 端点：{undocumented}"
