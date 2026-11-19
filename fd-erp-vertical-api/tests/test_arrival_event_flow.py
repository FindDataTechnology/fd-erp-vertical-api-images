"""到货 → 事件 → 数量面 → 分录 的 REST 端到端（establish-erp-event-kernel 7.1/7.2）。

真库真链路：不 mock 内核，走 InProcessKernelClient → 内核模块 → Postgres。

三处数据必须一致，且**三处是三种事实**：
  ① 事件层（business_events）：一笔 goods_received，带数量/单位/仓库/批次与来源；
  ② 数量面（object_balances）：sku 余额 = 到货量，带水位；
  ③ 账本（journal_entries）：只有**审批并生效的过账规则**才会产生分录。

第 3 条是这张测试的重点：没有规则时余额照旧成立、分录为空、事件仍成立——
  这正是「缺规则如实挂账、不猜科目」，也是「三处一致」不能被写成「三处都有值」的原因。
"""
from __future__ import annotations

from .conftest import TENANT, _h_sp


def _push_po(client, supplier_id: str, qty: str = "120") -> str:
    """租户侧铺底：需求 → 汇总 → 采购单 → 确认 → 推送（→ SP 可见域）。"""
    d = client.app.state.drivers
    demand = d.procurement.submit_demand(
        "manual", [{"product_code": "SKU-001", "qty": qty, "unit": "个"}])
    agg = d.procurement.aggregate_demand([demand["ref"]])
    po = d.procurement.propose_purchase_order(agg["ref"], supplier_id)
    d.procurement.confirm_purchase_order(po["request_id"])
    d.procurement.record_push(po["ref"], channel="supplier-portal")
    return po["ref"]


def _approve_goods_received_rule(client) -> None:
    """登记并审批 goods_received 的过账规则（规则属于账务口径，必须人工过门）。"""
    from fd_erp_ledger_mcp import posting
    from fd_erp_ledger_mcp.db import connection
    settings = client.app.state.settings
    with connection(settings) as conn:
        drafted = posting.submit_posting_rule(
            conn, tenant_id=TENANT, event_type="goods_received",
            rule={"lines": [
                {"account_code": "1405", "direction": "debit",
                 "amount_from": "payload.cost"},
                {"account_code": "2202", "direction": "credit",
                 "amount_from": "payload.cost"}]},
            actor="cfg-eng")
        posting.approve_posting_rule(conn, tenant_id=TENANT,
                                    event_type="goods_received",
                                    version=drafted["version"], approved_by="cfo")


def _events(client, stable_prefix: str = "") -> list[dict]:
    from fd_erp_ledger_mcp.db import connection, fetch_all
    settings = client.app.state.settings
    with connection(settings) as conn:
        return fetch_all(conn, "SELECT * FROM business_events "
                               "WHERE tenant_id=%s ORDER BY id", (TENANT,))


def _entries(client) -> list[dict]:
    from fd_erp_ledger_mcp.db import connection, fetch_all
    settings = client.app.state.settings
    with connection(settings) as conn:
        return fetch_all(conn, "SELECT * FROM journal_entries ORDER BY id")


def test_7_1_arrival_writes_event_stock_and_entries(client_faces, seeded_faces):
    """到货预告 → 事件 + 数量面 + 分录三处一致（规则已审批的那条路）。"""
    _approve_goods_received_rule(client_faces)
    po_ref = _push_po(client_faces, seeded_faces["supplier"])

    r = client_faces.post(
        "/erp/supply-chain/v1/sp/arrival-notices", headers=_h_sp("SUP-001"),
        json={"poRef": po_ref, "lines": [
            {"product_code": "SKU-001", "qty": "120", "unit": "个",
             "warehouse": "WH1", "lot": "L2301", "cost": "38400"}]})
    assert r.status_code == 200, r.text
    out = r.json()["data"]
    line = out["kernel"]["lines"][0]
    assert out["kernel"]["ok"] is True
    assert line["posted"] is True, line
    assert line["txn_id"]
    assert line["quantity"] == "120"

    # ① 事件层：一笔 goods_received，来源可追溯、载荷带维度
    events = _events(client_faces)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "goods_received" and ev["status"] == "accepted"
    assert ev["stable_id"] == f"{out['ref']}:1"
    assert ev["source_ref"] == out["ref"]
    assert ev["payload"]["quantity"] == "120"
    assert ev["payload"]["warehouse"] == "WH1"

    # ② 数量面：余额 = 到货量，且带水位与重算一致
    r = client_faces.get(
        "/erp/supply-chain/v1/sp/inventory?sku=SKU-001&warehouse=WH1&lot=L2301",
        headers=_h_sp("SUP-001"))
    assert r.status_code == 200, r.text
    stock = r.json()["data"]
    assert float(stock["quantity"]) == 120.0
    assert stock["unit"] == "个"
    assert stock["as_of_event_id"] == ev["id"]
    assert stock["drift"] is False
    # 读数来源必须标注：数量面是本板块已确认事实的投影，不是外部 WMS 实时库存
    # （change: add-inventory-operations-lane 任务 2.2）
    assert stock["source"] == "board-confirmed-facts"
    assert "本板块已确认事实" in stock["caliber_note"]
    assert "外部 WMS" in stock["caliber_note"]

    # ③ 账本：分录按审批过的规则派生，借 1405 / 贷 2202
    entries = _entries(client_faces)
    assert len(entries) == 2
    by_dir = {(e["account_code"], e["direction"]): float(e["amount"]) for e in entries}
    assert by_dir[("1405", "debit")] == 38400.0
    assert by_dir[("2202", "credit")] == 38400.0


def test_7_1_without_posting_rule_stock_holds_and_ledger_stays_empty(
        client_faces, seeded_faces):
    """无生效规则：事件与库存照旧成立，分录为零，且台账挂待补——不猜科目。"""
    po_ref = _push_po(client_faces, seeded_faces["supplier"], qty="7")

    r = client_faces.post(
        "/erp/supply-chain/v1/sp/arrival-notices", headers=_h_sp("SUP-001"),
        json={"poRef": po_ref, "lines": [
            {"product_code": "SKU-001", "qty": "7", "unit": "个",
             "warehouse": "WH1", "lot": "L9001"}]})
    assert r.status_code == 200, r.text
    line = r.json()["data"]["kernel"]["lines"][0]
    assert line["posted"] is False
    assert line["posting_code"] == "POSTING_RULE_MISSING"
    assert float(line["balance"]) == 7.0

    assert len(_events(client_faces)) == 1
    assert _entries(client_faces) == []

    from fd_erp_ledger_mcp.db import connection, fetch_all
    settings = client_faces.app.state.settings
    with connection(settings) as conn:
        gaps = fetch_all(conn, "SELECT * FROM posting_gaps "
                               "WHERE resolved_at IS NULL")
    assert len(gaps) == 1 and gaps[0]["event_type"] == "goods_received"


def test_7_2_inventory_is_object_projection_not_ledger_quantity(
        client_faces, seeded_faces):
    """库存端点答数量面；账本面无数量查询入口（数量不混进分录）。"""
    po_ref = _push_po(client_faces, seeded_faces["supplier"], qty="5")
    client_faces.post(
        "/erp/supply-chain/v1/sp/arrival-notices", headers=_h_sp("SUP-001"),
        json={"poRef": po_ref, "lines": [
            {"product_code": "SKU-001", "qty": "5", "unit": "个"}]})

    # 未指定库位/批次 → 查的是「未知维度」那批货（写入侧同一个 __unknown__）
    r = client_faces.get("/erp/supply-chain/v1/sp/inventory?sku=SKU-001",
                         headers=_h_sp("SUP-001"))
    assert r.status_code == 200, r.text
    stock = r.json()["data"]
    assert float(stock["quantity"]) == 5.0
    assert stock["dimension"] == {"warehouse": "__unknown__", "lot": "__unknown__"}

    # 指定了别的库位 → 是该维度上的空余额，不是「有 5 个」
    r = client_faces.get(
        "/erp/supply-chain/v1/sp/inventory?sku=SKU-001&warehouse=WH9",
        headers=_h_sp("SUP-001"))
    assert r.json()["data"]["quantity"] is None

    # 账本表里没有数量列（数量只在对象面）
    from fd_erp_ledger_mcp.db import connection, fetch_all
    settings = client_faces.app.state.settings
    with connection(settings) as conn:
        cols = [c["column_name"] for c in fetch_all(
            conn, "SELECT column_name FROM information_schema.columns "
                  "WHERE table_schema='public' AND table_name='journal_entries'")]
    assert "quantity" not in cols


def test_7_2_inventory_is_scoped_to_the_supplier(client_faces, seeded_faces):
    """库存跨供应商同表：不是本供应商的货一律 404，不靠「查到了」推断可见性。"""
    po_ref = _push_po(client_faces, seeded_faces["supplier"], qty="3")
    client_faces.post(
        "/erp/supply-chain/v1/sp/arrival-notices", headers=_h_sp("SUP-001"),
        json={"poRef": po_ref, "lines": [
            {"product_code": "SKU-001", "qty": "3", "unit": "个"}]})

    r = client_faces.get("/erp/supply-chain/v1/sp/inventory?sku=SKU-001",
                         headers=_h_sp("SUP-002"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"

    # 缺 sku → 400（不是返回全部库存）
    r = client_faces.get("/erp/supply-chain/v1/sp/inventory",
                         headers=_h_sp("SUP-001"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_7_1_delivery_feedback_also_lands_facts(client_faces, seeded_faces):
    """发货反馈自动生成到货预告，事实层结论一并回给调用方（SP 页面集看得见）。"""
    po_ref = _push_po(client_faces, seeded_faces["supplier"], qty="9")
    r = client_faces.post(
        f"/erp/supply-chain/v1/sp/purchase-orders/{po_ref}/delivery-feedback",
        headers=_h_sp("SUP-001"),
        json={"lines": [{"product_code": "SKU-001", "qty": "9", "unit": "个",
                         "warehouse": "WH2", "lot": "L7001"}]})
    assert r.status_code == 200, r.text
    kernel = r.json()["data"]["kernel"]
    assert kernel["lines"][0]["event_id"]
    assert len(_events(client_faces)) == 1


def test_7_3_honest_501s_stay_red_after_wiring(client_faces, seeded_faces):
    """接线不得顺手把未实现端点「变绿」（7.3）。

    两个方向都钉住：
      · 新增的库存端点不得混进 501 清单（清单是 AST 派生的诚实清单）；
      · 清单里的邻居端点运行时仍是 501，并带 planned_lane。
    """
    import json
    from pathlib import Path

    baseline = json.loads(
        (Path(__file__).resolve().parent.parent / "planned-surfaces.json")
        .read_text(encoding="utf-8"))
    paths = {e["path"] for e in baseline["endpoints"]}
    assert "/erp/supply-chain/v1/sp/inventory" not in paths
    # 不钉总数：清单会随能力落地而变短（售后三条即如此）。真正要守的是
    # 「实现过的端点不再出现在清单里」——上面那条断言就是它，按 路径 点名而不是按总量。
    assert baseline["count"] == len(baseline["endpoints"]) > 0
    assert not any("aftersale" in p_ for p_ in paths), "已实现的售后端点仍在 501 清单里"

    r = client_faces.get("/erp/supply-chain/v1/sp/statements",
                         headers=_h_sp("SUP-001"))
    assert r.status_code == 501
    assert r.json()["error"]["code"] == "NOT_IMPLEMENTED"
    assert r.json()["error"].get("detail", {}).get("planned_lane")

