"""库存作业面（change: add-inventory-operations-lane 任务 3.1/3.2）。

**零数据库**：本 suite 不连 erp_test_b，也不起真内核——用一个只实现库存作业面真正
用到的六个方法的内核替身。理由不是「图快」：真库不可达时按 conftest 的夹具整批
skip，看起来是绿的，实际一条判据都没跑。这一面要验的东西（闸序、封闭错误码、
调拨半边确认）全在 API 层，与数据库无关。

判据：

1. 三件套闸序不能换：**页集 → grant → 身份头**（`supply-chain.wh` 是内部页集，
   只在服务端守门、不进 /pagesets 载荷）；
2. 非法 payload 一律**封闭错误码**（400/401/403/409），不许塌成 500；
3. 提交只落拟稿事实：`pendingLines` 才是「还没成为账面事实」的那部分；
4. 出库/调拨**整单核量**：不足就一行都不写（不留半张单落下的半批事实）；
5. 调拨只确认一侧时，GET 单张必须看得见 `sides` 逐笔状态与 `halfConfirmed`，
   且整单状态**不推进**（`scope.finalized == False`）；
6. 逐行状态读**内核当前状态**，读不到时标 `unknown` 并在 `status_source` 点名——
   不拿提交时的快照充数，也不猜成 accepted。
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fd_erp_vertical_api.app import create_app
from fd_erp_vertical_api.cart import CartStore
from fd_erp_vertical_api.config import Settings
from fd_erp_vertical_api.errors import ERROR_CATALOG
from fd_erp_vertical_api.pagesets import load_pageset_auth

TENANT = "tnt-wh"
TENANT_HEADER = {"X-ERP-Tenant": TENANT}
WH_HEADER = "X-ERP-Warehouse"
BASE = "/erp/supply-chain/v1/wh"

ALL_WH_GRANTS = ["inventory.read", "inventory.write", "inventory.confirm"]
READ_ONLY = ["inventory.read"]
UNKNOWN_LOT = "__unknown__"


# ---- 内核替身 ---------------------------------------------------------------

class _StubError(Exception):
    """带封闭错误码的替身错误（KernelProxy 按 code 翻成同名 ApiError）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class StubKernel:
    """最小内核：事件档 + 数量面投影/过账，只实现本面用到的六个方法。

    `balances` 以 (sku, 仓库, 批次) 为键；`fail_query` 用来构造「内核读不到」。
    """

    def __init__(self, balances: dict[tuple[str, str, str], str] | None = None):
        self.events: dict[str, dict] = {}
        self.seq = 0
        self.balances = dict(balances or {})
        self.decided: list[tuple[str, str, str | None]] = []
        self.project_calls: list[tuple[int, str]] = []
        self.posted = True
        self.fail_query: Exception | None = None

    # 事件层
    def submit_event(self, event_type, stable_id, occurred_at, payload,
                     source_ref=None):
        if stable_id in self.events:  # 幂等：同 stable_id 不重复落事实
            return {**self.events[stable_id], "duplicate": True}
        self.seq += 1
        self.events[stable_id] = {
            "id": self.seq, "stable_id": stable_id, "event_type": event_type,
            "occurred_at": occurred_at, "payload": dict(payload),
            "status": "pending", "source_ref": source_ref,
        }
        return {**self.events[stable_id], "duplicate": False}

    def query_events(self, event_type=None, lane=None, status=None, limit=100,
                     after_event_id=0):
        if self.fail_query is not None:
            raise self.fail_query
        rows = [dict(e) for e in self.events.values()
                if event_type in (None, e["event_type"])]
        # 内核 query_events 的真实返回是 `{items, completeness}`（见
        # fd_erp_vertical_templates/cost.py 的同一处踩坑记录）。替身若自造 `rows`，
        # 这一面读错键也会一路绿——那正是本 suite 存在的理由。
        return {"items": rows, "completeness": {"returned": len(rows)}}

    def decide_event(self, stable_id, decision, note=None):
        ev = self.events.get(stable_id)
        if ev is None:
            raise _StubError("NOT_FOUND", f"事件不存在：{stable_id}")
        if ev["status"] != "pending":
            raise _StubError("EVENT_STATE",
                             f"事件状态不允许 {ev['status']} → {decision}")
        ev["status"] = decision
        self.decided.append((stable_id, decision, note))
        return dict(ev)

    # 数量面
    def project_event(self, event_id, object_type, object_key, quantity_delta,
                      unit, dimension=None):
        ev = next((e for e in self.events.values() if e["id"] == event_id), None)
        if ev is None:
            raise _StubError("NOT_FOUND", f"事件不存在：{event_id}")
        if ev["status"] != "accepted":
            raise _StubError("EVENT_STATE",
                             f"只有 accepted 的事实才能投影（当前 {ev['status']}）")
        dim = dimension or {}
        key = (object_key, str(dim.get("warehouse") or ""),
               str(dim.get("lot") or ""))
        after = Decimal(self.balances.get(key, "0")) + Decimal(str(quantity_delta))
        self.balances[key] = str(after)
        self.project_calls.append((int(event_id), key[0]))
        return {"quantity": str(after)}

    def post_event(self, event_id):
        if not self.posted:  # 缺过账规则：不当成已入账
            return {"posted": False, "reason": "rule_missing_or_incomplete"}
        return {"posted": True, "txn_id": f"txn-{event_id}"}

    def query_object_balance(self, object_type, object_key, dimension=None):
        dim = dimension or {}
        key = (object_key, str(dim.get("warehouse") or ""),
               str(dim.get("lot") or ""))
        return {"quantity": self.balances.get(key, "0")}


# ---- 夹具 -------------------------------------------------------------------

def _client(tmp_path, kernel=None, *, pagesets=None) -> TestClient:
    payload = pagesets if pagesets is not None else {"supply-chain.wh": ALL_WH_GRANTS}
    settings = Settings(
        tenant_id=TENANT,
        database_url="postgresql://stub-not-used/erp",  # 注入替身内核，永不连
        base_currency="CNY",
        data_dir=str(tmp_path / "data"),
        pageset_auth_json=json.dumps(payload),
    )
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    app = create_app(settings, kernel or StubKernel(),
                     pageset_auth=load_pageset_auth(
                         inline_json=json.dumps(payload)),
                     cart_store=CartStore())
    return TestClient(app)


def _h(who: str = "wh-zhang") -> dict:
    return {**TENANT_HEADER, WH_HEADER: who}


def _code(resp) -> str:
    body = resp.json()
    assert body.get("ok") is False, body
    assert body.get("rows_returned") == 0, body
    code = body["error"]["code"]
    # 封闭码：任何一处漏登记都会在这里翻出来（未登记码会塌成 INTERNAL 500）
    assert code in ERROR_CATALOG, f"未登记的错误码：{code}"
    return code


RECEIPT = {"warehouse": "WH-A",
           "lines": [{"sku": "S1", "quantity": "10", "unit": "pcs"}]}


# ---- 1) 闸：401 / 403（页集）/ 403（grant）/ 400（程序身份）-----------------

def test_missing_warehouse_header_is_401(tmp_path):
    """缺身份头：租户头是常量，不能单独当凭据——四组端点各验一次。"""
    c = _client(tmp_path)
    cases = [
        ("post", f"{BASE}/receipts", RECEIPT),
        ("get", f"{BASE}/receipts", None),
        ("post", f"{BASE}/transfers", {"fromWarehouse": "WH-A",
                                       "toWarehouse": "WH-B", "lines": []}),
        ("get", f"{BASE}/documents/IR-xxx", None),
    ]
    for method, url, body in cases:
        resp = (c.post(url, json=body, headers=TENANT_HEADER) if method == "post"
                else c.get(url, headers=TENANT_HEADER))
        assert resp.status_code == 401, (url, resp.status_code, resp.text)
        assert _code(resp) == "AUTH_REQUIRED"
        assert WH_HEADER in resp.json()["error"]["message"]


def test_pageset_not_granted_is_403_and_names_the_key(tmp_path):
    """内部页集也要授权：没有任何 wh 授权 ⇒ PAGESET_FORBIDDEN（不是 200 空）。"""
    c = _client(tmp_path, pagesets={"supply-chain.ue": ["catalog.read"]})
    resp = c.post(f"{BASE}/receipts", json=RECEIPT, headers=_h())
    assert resp.status_code == 403
    assert _code(resp) == "PAGESET_FORBIDDEN"
    assert "supply-chain.wh" in resp.json()["error"]["message"]


def test_read_only_grant_cannot_write(tmp_path):
    """读权限 MUST NOT 覆盖写与拍板：两个方向各验一次。"""
    c = _client(tmp_path, pagesets={"supply-chain.wh": READ_ONLY})
    resp = c.post(f"{BASE}/receipts", json=RECEIPT, headers=_h())
    assert resp.status_code == 403
    assert _code(resp) == "GRANT_REQUIRED"
    assert resp.json()["error"]["detail"]["grant"] == "inventory.write"

    # 写成之后仍不能拍板（换一个带写权限但不带 confirm 的配置）
    c2 = _client(tmp_path, pagesets={"supply-chain.wh":
                                     ["inventory.read", "inventory.write"]})
    made = c2.post(f"{BASE}/receipts", json=RECEIPT, headers=_h())
    assert made.status_code == 200, made.text
    ref = made.json()["data"]["ref"]
    resp = c2.post(f"{BASE}/documents/{ref}/confirm", json={}, headers=_h())
    assert resp.status_code == 403
    assert _code(resp) == "GRANT_REQUIRED"
    assert resp.json()["error"]["detail"]["grant"] == "inventory.confirm"


def test_program_identity_cannot_act_for_warehouse(tmp_path):
    """程序身份不靠名单放行：REST 面只接人工经办 ⇒ 400 而不是 403。"""
    c = _client(tmp_path)
    resp = c.post(f"{BASE}/receipts", json=RECEIPT, headers=_h("asset-engine"))
    assert resp.status_code == 400
    assert _code(resp) == "INVALID_ARGUMENT"
    assert "须人工经办" in resp.json()["error"]["message"]


# ---- 2) 三组端点的 400（非法 payload 一律封闭码）--------------------------

def test_receipt_without_lines_is_400(tmp_path):
    c = _client(tmp_path)
    resp = c.post(f"{BASE}/receipts", json={"warehouse": "WH-A"}, headers=_h())
    assert resp.status_code == 400
    assert _code(resp) == "INVALID_ARGUMENT"
    assert "至少需要一行" in resp.json()["error"]["message"]


def test_receipt_without_warehouse_is_400(tmp_path):
    c = _client(tmp_path)
    resp = c.post(f"{BASE}/receipts",
                  json={"lines": [{"sku": "S1", "quantity": "1"}]}, headers=_h())
    assert resp.status_code == 400
    assert _code(resp) == "INVALID_ARGUMENT"
    assert "缺仓库" in resp.json()["error"]["message"]


def test_transfer_same_warehouse_is_400(tmp_path):
    c = _client(tmp_path)
    resp = c.post(f"{BASE}/transfers",
                  json={"fromWarehouse": "WH-A", "toWarehouse": "WH-A",
                        "lines": [{"sku": "S1", "quantity": "1"}]}, headers=_h())
    assert resp.status_code == 400
    assert _code(resp) == "INVALID_ARGUMENT"
    assert "不是调拨" in resp.json()["error"]["message"]


def test_bad_pagination_is_400(tmp_path):
    c = _client(tmp_path)
    resp = c.get(f"{BASE}/receipts?offset=-1", headers=_h())
    assert resp.status_code == 400
    assert _code(resp) == "INVALID_ARGUMENT"


def test_issue_shortage_rejects_whole_document_and_writes_nothing(tmp_path):
    """整单核量：不足就一行都不写——半张单落下的事实比没有更难收拾。"""
    k = StubKernel({("S1", "WH-A", UNKNOWN_LOT): "3"})
    c = _client(tmp_path, k)
    before = len(k.events)
    resp = c.post(f"{BASE}/issues",
                  json={"warehouse": "WH-A",
                        "lines": [{"sku": "S1", "quantity": "2"},
                                  {"sku": "S1", "quantity": "3"}]}, headers=_h())
    assert resp.status_code == 400
    assert _code(resp) == "INVALID_ARGUMENT"
    err = resp.json()["error"]
    assert err["detail"]["available"] == "3.000000"
    assert err["detail"]["requested"] == "5.000000"
    assert err["detail"]["shortage"] == "2.000000"
    assert len(k.events) == before, "核量失败仍写了事件：半张单落下了事实"


# ---- 3) 200：提交 → 拍板 → 投影（含过账读数）-----------------------------

def test_receipt_confirm_projects_and_posts(tmp_path):
    k = StubKernel()
    c = _client(tmp_path, k)

    made = c.post(f"{BASE}/receipts", json=RECEIPT, headers=_h())
    assert made.status_code == 200, made.text
    doc = made.json()["data"]
    assert doc["status"] == "pending"
    assert doc["pendingLines"] == 1
    assert doc["requestedBy"] == "wh-zhang"  # 身份头进留痕，不是常量
    ref = doc["ref"]

    done = c.post(f"{BASE}/documents/{ref}/confirm", json={}, headers=_h("wh-li"))
    assert done.status_code == 200, done.text
    out = done.json()["data"]
    assert out["decidedOk"] == 1 and out["decidedFailed"] == 0
    assert out["operator"] == "wh-li"
    assert out["scope"] == {"side": None, "line": None,
                            "narrowed": False, "finalized": True}
    line = out["project"]["lines"][0]
    assert line["ok"] is True
    assert line["posted"] is True          # 有过账规则 ⇒ posted
    assert line["balance_after"] == "10.000000"
    # 内核确认门的 actor 固定 human（不自报身份），经办人留在本层
    assert [d[1] for d in k.decided] == ["accepted"]

    # 拍板后整单状态推进为 confirmed
    detail = c.get(f"{BASE}/documents/{ref}", headers=_h())
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "confirmed"


def test_missing_posting_rule_is_reported_not_silently_posted(tmp_path):
    """缺过账规则：posted=false + posting_reason，不当成已入账。"""
    k = StubKernel()
    k.posted = False
    c = _client(tmp_path, k)
    ref = c.post(f"{BASE}/receipts", json=RECEIPT, headers=_h()).json()["data"]["ref"]
    out = c.post(f"{BASE}/documents/{ref}/confirm", json={},
                 headers=_h()).json()["data"]
    line = out["project"]["lines"][0]
    assert line["posted"] is False
    assert line["posting_reason"] == "rule_missing_or_incomplete"


def test_confirm_on_rejected_document_is_409(tmp_path):
    c = _client(tmp_path)
    ref = c.post(f"{BASE}/receipts", json=RECEIPT, headers=_h()).json()["data"]["ref"]

    no_reason = c.post(f"{BASE}/documents/{ref}/reject", json={}, headers=_h())
    assert no_reason.status_code == 400
    assert _code(no_reason) == "INVALID_ARGUMENT"
    assert "reason" in no_reason.json()["error"]["message"]

    ok = c.post(f"{BASE}/documents/{ref}/reject",
                json={"reason": "到货与送货单不符"}, headers=_h("wh-li"))
    assert ok.status_code == 200, ok.text
    body = ok.json()["data"]
    assert body["document"]["status"] == "rejected"
    assert body["rejectedOk"] == 1
    assert body["operator"] == "wh-li"

    again = c.post(f"{BASE}/documents/{ref}/confirm", json={}, headers=_h())
    assert again.status_code == 409
    assert _code(again) == "WORKFLOW_STATE"
    assert "驳回" in again.json()["error"]["message"]


def test_document_not_found_is_404(tmp_path):
    c = _client(tmp_path)
    resp = c.get(f"{BASE}/documents/IR-NOPE", headers=_h())
    assert resp.status_code == 404
    assert _code(resp) == "NOT_FOUND"


def test_list_returns_paging_and_caliber_note(tmp_path):
    """列表要能读出「分页窗口」与「数量面出处」——余额不是外部 WMS 实时库存。"""
    c = _client(tmp_path)
    for i in range(3):
        c.post(f"{BASE}/receipts",
               json={"warehouse": "WH-A",
                     "lines": [{"sku": f"S{i}", "quantity": "1"}]}, headers=_h())
    resp = c.get(f"{BASE}/receipts?limit=2", headers=_h())
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["rows_returned"] == 2
    assert body["kind"] == "receipt"
    assert "query_object_balance" in body["caliber_note"]
    # 出库单是另一个 kind：互不串台
    assert c.get(f"{BASE}/issues", headers=_h()).json()["total"] == 0


# ---- 4) 任务 3.2：调拨只确认一侧 ⇒ sides 逐笔状态可见 --------------------

def test_transfer_half_confirm_shows_sides_and_does_not_finalize(tmp_path):
    """调拨是两笔事实：只拍一侧时另一侧仍在 pending，整单**不许**宣布 confirmed。"""
    k = StubKernel({("S1", "WH-A", UNKNOWN_LOT): "100"})
    c = _client(tmp_path, k)
    made = c.post(f"{BASE}/transfers",
                  json={"fromWarehouse": "WH-A", "toWarehouse": "WH-B",
                        "lines": [{"sku": "S1", "quantity": "7"}]}, headers=_h())
    assert made.status_code == 200, made.text
    ref = made.json()["data"]["ref"]
    assert made.json()["data"]["pendingLines"] == 2  # 两条事实：out + in

    half = c.post(f"{BASE}/documents/{ref}/confirm",
                  json={"side": "out"}, headers=_h("wh-li"))
    assert half.status_code == 200, half.text
    hb = half.json()["data"]
    assert hb["decidedOk"] == 1
    assert hb["scope"] == {"side": "out", "line": None,
                           "narrowed": True, "finalized": False}
    assert "整单状态不推进" in hb["note"]

    detail = c.get(f"{BASE}/documents/{ref}", headers=_h()).json()["data"]
    sides = detail["sides"]
    assert set(sides) == {"out", "in"}
    assert sides["out"] == {**sides["out"], "lines": 1, "pending": 0,
                            "accepted": 1, "rejected": 0, "unknown": 0,
                            "statuses": ["accepted"], "settled": True}
    assert sides["in"]["pending"] == 1 and sides["in"]["settled"] is False
    assert detail["halfConfirmed"] is True
    # 逐行状态：两条事实各自可见，且 out 的当前状态不是提交时的快照
    by_side = {row["side"]: row for row in detail["lines"]}
    assert by_side["out"]["event_status"] == "accepted"
    assert by_side["out"]["event_status_submitted"] == "pending"
    assert by_side["in"]["event_status"] == "pending"
    # 整单状态仍是 pending：半边确认不是整单成功
    assert detail["status"] == "pending"
    assert "不是提交时的快照" in detail["status_source"]


def test_transfer_second_side_then_finalizes(tmp_path):
    """补齐另一侧才 finalize；两侧都过门后整单才是 confirmed。"""
    k = StubKernel({("S1", "WH-A", UNKNOWN_LOT): "100"})
    c = _client(tmp_path, k)
    ref = c.post(f"{BASE}/transfers",
                 json={"fromWarehouse": "WH-A", "toWarehouse": "WH-B",
                       "lines": [{"sku": "S1", "quantity": "4"}]},
                 headers=_h()).json()["data"]["ref"]
    c.post(f"{BASE}/documents/{ref}/confirm", json={"side": "out"}, headers=_h())
    assert c.get(f"{BASE}/documents/{ref}",
                 headers=_h()).json()["data"]["halfConfirmed"] is True

    done = c.post(f"{BASE}/documents/{ref}/confirm", json={"side": "in"},
                  headers=_h())
    assert done.status_code == 200
    assert done.json()["data"]["scope"]["finalized"] is False  # 仍是筛选调用

    detail = c.get(f"{BASE}/documents/{ref}", headers=_h()).json()["data"]
    assert detail["halfConfirmed"] is False
    assert detail["sides"]["out"]["settled"] is True
    assert detail["sides"]["in"]["settled"] is True


def test_stocktake_without_difference_writes_no_fact(tmp_path):
    """盘点：无差异不出事实（不写零差异），差异行才落 goods_adjusted。"""
    k = StubKernel({("S1", "WH-A", UNKNOWN_LOT): "10",
                    ("S2", "WH-A", UNKNOWN_LOT): "5"})
    c = _client(tmp_path, k)
    made = c.post(f"{BASE}/stocktakes",
                  json={"warehouse": "WH-A",
                        "counts": [{"sku": "S1", "counted_qty": "10"},   # 无差异
                                   {"sku": "S2", "counted_qty": "3"}]},  # 盘亏 2
                  headers=_h())
    assert made.status_code == 200, made.text
    body = made.json()["data"]
    assert body["lineCount"] == 1
    assert len(k.events) == 1
    ev = next(iter(k.events.values()))
    assert ev["event_type"] == "goods_adjusted"
    assert ev["payload"]["reason"] == "stocktake_loss"
    # 差异带符号（盘亏为负）：无符号会让投影把盘亏做成盘盈
    assert ev["payload"]["quantity"] == "-2.000000"
    assert body["lineCount"] == 1


# ---- 5) 逐行状态读不到时如实降级（不猜 accepted）-------------------------

def test_unreadable_kernel_status_degrades_to_unknown(tmp_path):
    k = StubKernel()
    c = _client(tmp_path, k)
    ref = c.post(f"{BASE}/receipts", json=RECEIPT, headers=_h()).json()["data"]["ref"]
    k.fail_query = RuntimeError("kernel unreachable")

    detail = c.get(f"{BASE}/documents/{ref}", headers=_h())
    assert detail.status_code == 200  # 单据本身读得到，只是状态不可知
    body = detail.json()["data"]
    assert [row["event_status"] for row in body["lines"]] == ["unknown"]
    assert "读不到" in body["status_source"]
    assert "不要据此判断是否已确认" in body["status_source"]
    # 不得悄悄当成 accepted / pending 之外的成功态
    assert body["halfConfirmed"] is False


# ---- 6) 内核返回键：四种写法都得读得出来（自证臂）-----------------------

@pytest.mark.parametrize("shape", ["items", "events", "data", "rows"])
def test_event_rows_tolerates_every_key_the_layers_have_used(tmp_path, shape):
    """键名认错不报错、只报假状态 ⇒ 四种写法逐个钉一遍。

    真内核用 `items`（`{items, completeness}`）；其余是历史包法与替身用过的写法。
    把 `_EVENT_ROW_KEYS` 删掉任意一个，对应那条参数立刻红——这就是这一臂的用处：
    「只认一个键」的写法在别的形状上会静默报 `not_found`，而单据照样 200（本 suite
    初版就是这样把 `rows` 当成真键，对着真内核每个行都被读成「不存在」）。
    """
    k = StubKernel()
    base = k.query_events

    def cloned(**kw):
        out = base(**kw)
        return {shape: out["items"], "completeness": out["completeness"]}

    k.query_events = cloned
    c = _client(tmp_path, k)
    ref = c.post(f"{BASE}/receipts", json=RECEIPT, headers=_h()).json()["data"]["ref"]

    body = c.get(f"{BASE}/documents/{ref}", headers=_h()).json()["data"]
    assert [row["event_status"] for row in body["lines"]] == ["pending"], body
    assert "不是提交时的快照" in body["status_source"]
    assert body["sides"] == {} and body["halfConfirmed"] is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
