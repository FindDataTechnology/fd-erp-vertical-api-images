#!/usr/bin/env python3
"""演示数据一键重灌（幂等）——四页面集要看得见东西的那一套。

为什么要有这个脚本：2026-11-08 首次上产时，主数据/采购单/订单/运单全是人工
进 pod 敲 python 铺的，换台机器或换租户就得照着 notes 重做一遍，且顺序错了
（先建池后建商品、忘了 `sales_rep`、档闸要 entity_id 不要 code）会静默 404。
把口径固定成代码，就不再靠记忆。

用法（在 ERP 容器内跑，`ERP_TENANT_ID`/`ERP_API_DATA_DIR` 已在 envFrom 里）：

    python3 scripts/seed_demo.py                 # 建主数据 + 演示单据
    python3 scripts/seed_demo.py --no-docs       # 只建主数据（客户/供应商/商品/池）
    python3 scripts/seed_demo.py --check         # 只报现状，不写

幂等：按 `code` / 文本标记判存在，已存在即跳过（事件档 append-only，重复跑不会
重复建档）。信用额度**不在本脚本里**——它属内核库，走 `fd-erp-ledger-mcp
set-credit-limit`（账号/DNS 都在内核那侧），脚本只把要执行的命令打出来。

四端演示需要的最小闭环：
  客户 + 授权商品池 → UE 能看能下单（还须内核额度）
  客户 `sales_rep` → SE 有归属
  供应商 + 采购单推送 → SP 有单据（接单/发货反馈才能走）
  确认订单 + 履约计划 + 运单 → CP 有在途（承运商标识由运单 carrier 决定）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fd_erp_vertical_api.config import Settings  # noqa: E402
from fd_erp_vertical_api.drivers import build_drivers  # noqa: E402

CUSTOMER = {"code": "CUST-DEMO-001", "name": "演示客户（杭州仓配）",
            "contact": "张经理", "phone": "13800000000"}
SUPPLIER = {"code": "SUP-DEMO-001", "name": "演示供应商（华东食品）",
            "direct_ship": False}
PRODUCTS = [
    ("SKU-DEMO-001", "矿泉水 550ml", "饮品", "2.50"),
    ("SKU-DEMO-002", "苏打水 380ml", "饮品", "3.20"),
    ("SKU-DEMO-003", "纸杯 200 只", "耗材", "18.00"),
]
POOL = {"name": "演示授权池", "visibility": "authorized", "org_path": "/演示/华东"}
CARRIER = "CARRIER-DEMO-001"
SALES_REP = "platform-test@findmail.com"  # 与 erp-identity-bindings 里的会话账号一致


def _units():
    return [{"unit": "件", "kind": "base", "rate": 1},
            {"unit": "箱", "kind": "purchase", "rate": 24},
            {"unit": "组", "kind": "sales", "rate": 6},
            {"unit": "托", "kind": "warehouse", "rate": 144}]


def _ensure_master(md, kind: str, payload: dict, *, by: str = "seed") -> dict:
    """按 code 判存在；不存在则走确认车道建档（不是直接写档）。"""
    code = payload.get("code")
    if code:
        hit = md.find_by_code(kind, code)
        if hit is not None:
            print(f"  = {kind:12s} {code} 已存在")
            return hit
    req = md.create_entity(kind, payload, requested_by=by)
    md.confirm_request(req["request_id"], decided_by="seed-owner")
    snap = md.get_entity(req["entity_id"])
    print(f"  + {kind:12s} {code or payload.get('name')} → {req['entity_id']}")
    return snap


def _ensure_pool(md, customer_id: str, codes: list[str]) -> dict:
    pools = [p for p in md.list_entities(kind="product_pool")
             if (p.get("payload") or {}).get("name") == POOL["name"]]
    if pools:
        print(f"  = product_pool {POOL['name']} 已存在")
        return pools[0]
    req = md.create_entity("product_pool", {**POOL, "product_codes": codes},
                           requested_by="seed")
    md.confirm_request(req["request_id"], decided_by="seed-owner")
    auth = md.authorize_pool(req["entity_id"], customers=[customer_id])
    md.confirm_request(auth["request_id"], decided_by="seed-owner")
    print(f"  + product_pool {POOL['name']} → {req['entity_id']}（授权客户 1）")
    return md.get_entity(req["entity_id"])


def _ensure_purchase_order(d: dict, supplier_id: str, product_code: str) -> dict | None:
    pushed = [e for e in d["procurement"]._events()
              if e.get("record") == "purchase_order" and e.get("status") == "pushed"]
    if pushed:
        print(f"  = purchase_order {pushed[-1]['ref']} 已推送")
        return pushed[-1]
    demand = d["procurement"].submit_demand(
        "manual", [{"product_code": product_code, "qty": "120", "unit": "件"}],
        requested_by="seed")
    agg = d["procurement"].aggregate_demand([demand["ref"]], requested_by="seed")
    po = d["procurement"].propose_purchase_order(agg["ref"], supplier_id,
                                                 requested_by="seed")
    conf = d["procurement"].confirm_purchase_order(po["request_id"],
                                                   decided_by="seed-owner")
    d["procurement"].record_push(conf["ref"], channel="supplier-portal",
                                 operator="seed")
    print(f"  + purchase_order {conf['ref']} → 已推送供应商档案")
    return d["procurement"]._latest(conf["ref"])


def _ensure_order_chain(d: dict, customer_id: str, product_code: str) -> None:
    orders = [e for e in d["orders"]._events()
              if e.get("record") == "order" and e.get("event") == "proposed"]
    if orders:
        print(f"  = order {orders[-1]['ref']} 已存在")
        return
    out = d["orders"].accept_order(
        customer_id, [{"product_code": product_code, "qty": "2", "unit": "箱"}],
        date.today().isoformat(), requested_by="seed")
    if not out.get("accepted"):
        # 额度未登记时如实报出，不伪造
        print("  ! 下单未受理（多半是内核未登记信用额度）："
              f"{json.dumps(out.get('explanation') or {}, ensure_ascii=False)}")
        return
    conf = d["orders"].confirm_order(out["request_id"], decided_by="seed-owner")
    handoff = next(e for e in d["orders"]._events()
                   if e.get("ref") == conf["handoff_ref"])
    plan = d["fulfillment"].plan_fulfillment(
        conf["handoff_ref"], conf["order_ref"],
        handoff["payload"]["customer_id"], handoff["payload"]["lines"],
        requested_by="seed")
    d["fulfillment"].record_waybill(plan["ref"], "WB-DEMO-0001", CARRIER,
                                    requested_by="seed")
    print(f"  + order {conf['order_ref']} → 确认 → 计划 {plan['ref']} "
          f"→ 运单 WB-DEMO-0001（carrier={CARRIER}）")


def report(md, d: dict) -> None:
    customers = md.list_entities(kind="customer")
    products = md.list_entities(kind="product")
    pools = md.list_entities(kind="product_pool")
    print("现状："
          f"客户 {len(customers)} / 供应商 {len(md.list_entities(kind='supplier'))}"
          f" / 商品 {len(products)} / 商品池 {len(pools)}")
    for c in customers:
        p = c["payload"]
        print(f"  customer {p.get('code')} sales_rep={p.get('sales_rep')} "
              f"id={c['entity_id']}")
    if pools:
        print("  可见商品（按池）："
              f"{md.visible_product_codes(customer_id=customers[0]['entity_id']) if customers else []}")
    print(f"  订单 {len([e for e in d['orders']._events() if e.get('record') == 'order' and e.get('event') == 'proposed'])}"
          f" / 采购单 {len([e for e in d['procurement']._events() if e.get('record') == 'purchase_order'])}"
          f" / 运单 {len([e for e in d['fulfillment']._events() if e.get('record') == 'waybill'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description="演示数据一键重灌（幂等）")
    ap.add_argument("--check", action="store_true", help="只报现状，不写")
    ap.add_argument("--no-docs", action="store_true", help="只建主数据，不建演示单据")
    args = ap.parse_args()

    settings = Settings.from_env()
    d = build_drivers(settings, kernel=None)  # 主数据/采购/履约均不触内核
    md = d.master
    print(f"租户 {settings.tenant_id} · 数据目录 {settings.data_path()}")

    if args.check:
        report(md, {"orders": d.orders, "procurement": d.procurement,
                    "fulfillment": d.fulfillment})
        return 0

    print("主数据：")
    customer = _ensure_master(md, "customer", {**CUSTOMER, "sales_rep": SALES_REP})
    # sales_rep 可能早先没写（首灌就是这样），补一次变更提案
    if (customer["payload"] or {}).get("sales_rep") != SALES_REP:
        req = md.update_entity(customer["entity_id"], {"sales_rep": SALES_REP},
                               requested_by="seed")
        md.confirm_request(req["request_id"], decided_by="seed-owner")
        print(f"  ~ customer  补 sales_rep={SALES_REP}")
    supplier = _ensure_master(md, "supplier", SUPPLIER)
    codes = []
    for code, name, cat, price in PRODUCTS:
        _ensure_master(md, "product", {
            "code": code, "name": name, "category": cat, "base_price": price,
            "supplier_id": supplier["entity_id"], "units": _units()})
        codes.append(code)
    _ensure_pool(md, customer["entity_id"], codes)

    if not args.no_docs:
        print("演示单据：")
        _ensure_purchase_order(
            {"procurement": d.procurement}, supplier["entity_id"], codes[0])
        _ensure_order_chain({"orders": d.orders, "fulfillment": d.fulfillment},
                            customer["entity_id"], codes[0])

    print()
    report(md, {"orders": d.orders, "procurement": d.procurement,
                "fulfillment": d.fulfillment})
    print("\n还差一步（内核侧，本进程无库里连接，必须在内核容器跑）：")
    print(f"  fd-erp-ledger-mcp set-credit-limit --customer {customer['entity_id']} "
          f"--limit-amount 100000 --source-ref seed-demo")
    print("然后（平台侧）把该客户/供应商/承运商写进 erp-identity-bindings ConfigMap，")
    print("subPath 挂载不热更：改完 push → ArgoCD 同步 → rollout restart deploy/platform。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
