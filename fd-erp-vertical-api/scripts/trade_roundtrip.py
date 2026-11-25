#!/usr/bin/env python3
# 正本：fd-erp/tools/trade_roundtrip.py。本文件是**随镜像出片的副本**——链探针的定时读者
# （CronJob fd-trade-chain-probe）要在集群内跑它，而 fd-erp 仓不出镜像。两份内容必须逐字节
# 相同（fd-erp 的 check_all 有对拍）；改这里之前先改正本。
"""发现 → 协商 → 下单 一条龙真跑探针（change establish-ai-native-trade-discovery 3.1）。

按 `constraints/closed-loop-must-have-probe-and-reader`：宣称「闭环」的链必须有链探针，
四态退出码，证据取**上游原生返回**（不自己编摘要）。

四态退出码：
  0  全链路通过（发现 → 询价 → 报价 → 接受 → 订单成交价 = 报价）
  1  链路断（某一步返回了非预期结果——打印该步原始响应）
  2  未配置（缺必要参数/目录为空）
  3  上游不可达（目录或店面连不上）

用法：
  python3 fd-erp/tools/trade_roundtrip.py \\
      --directory http://127.0.0.1:8099 --api http://127.0.0.1:8088 \\
      --tenant acme [--grant <token>] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

OK, BROKEN, UNCONFIGURED, UNREACHABLE = 0, 1, 2, 3


class Step:
    def __init__(self, name: str):
        self.name = name

    def __call__(self, code: int, detail: dict | str) -> int:
        print(f"[{self.name}] {'OK' if code == OK else 'FAIL'} :: "
              f"{json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else detail}")
        return code


def _req(method: str, url: str, body: dict | None = None,
         headers: dict | None = None, timeout: int = 20) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    h = {"accept": "application/json"}
    if body is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:400].decode(errors="replace")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--directory", required=True, help="目录服务基址")
    ap.add_argument("--api", required=True, help="ERP API 基址")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--category", default="", help="按类目检索（缺省取目录第一家有类目的店）")
    ap.add_argument("--product", default="", help="商品编码（缺省取该店第一个商品）")
    ap.add_argument("--qty", default="1")
    ap.add_argument("--grant", default="", help="委托 Grant（带它就验预算闸）")
    ap.add_argument("--skip-accept", action="store_true",
                    help="只跑到报价（不产生订单）")
    a = ap.parse_args(argv)

    dir_base = a.directory.rstrip("/")
    api_base = a.api.rstrip("/")
    H = {"X-ERP-Tenant": a.tenant}

    # ---- 1) 发现 ----
    s = Step("1 discover")
    try:
        code, body = _req("GET", f"{dir_base}/directory/search"
                          + (f"?category={urllib.parse.quote(a.category)}" if a.category else ""))
    except (urllib.error.URLError, TimeoutError) as exc:
        return s(UNREACHABLE, {"directory": dir_base, "error": str(exc)})
    if code == 200 and body.get("state") == "matched":
        matches = body.get("matches") or []
    elif code == 200 and body.get("state") == "no_match":
        return s(UNCONFIGURED, {"reason": "目录里有店但没有匹配条件", "body": body})
    else:
        return s(BROKEN, {"http": code, "body": body})
    hit = next((m for m in matches if not a.tenant or m["tenant"] == a.tenant), matches[0])
    tenant = hit["tenant"]
    H["X-ERP-Tenant"] = tenant if a.tenant == hit["tenant"] else tenant
    s(OK, {"matched_count": body["matched_count"], "picked": tenant,
           "public_base": hit["public_base"]})

    # ---- 2) 读该店商品 ----
    s = Step("2 read products")
    try:
        code, prods = _req("GET", f"{api_base}/shop/{tenant}/products")
    except (urllib.error.URLError, TimeoutError) as exc:
        return s(UNREACHABLE, {"api": api_base, "error": str(exc)})
    rows = prods.get("data") or []
    if code != 200 or not rows:
        return s(BROKEN, {"http": code, "rows": len(rows), "body": str(prods)[:300]})
    product = a.product or str(rows[0].get("productId") or "")
    list_price = next((r.get("unit_price") for r in rows
                       if str(r.get("productId")) == product), None)
    s(OK, {"tenant": tenant, "product": product, "list_price": list_price,
           "rows": len(rows)})

    # ---- 3) 询价 ----
    s = Step("3 demand")
    code, dem = _req("POST", f"{api_base}/erp/supply-chain/v1/trade/demands", {
        "lines": [{"productId": product, "qty": a.qty}],
        "neededBy": _future(7),
        "shipTo": {"name": "链路探针", "phone": "13800000000"},
    }, H)
    if code != 201:
        return s(BROKEN, {"http": code, "body": dem})
    demand_ref = dem["data"]["demand_ref"]
    s(OK, {"demand_ref": demand_ref, "amount": dem["data"].get("amount")})

    # ---- 4) 报价（探针自己扮演卖方，报挂牌价 75%） ----
    s = Step("4 quote")
    quoted = _discount(list_price)
    code, quo = _req("POST", f"{api_base}/erp/supply-chain/v1/trade/quotes", {
        "demandRef": demand_ref, "unitPrice": quoted, "currency": "CNY",
        "validUntil": _future(3), "leadTimeDays": 3, "moq": 1,
        "qtyOffered": a.qty, "productId": product,
    }, H)
    if code != 201:
        return s(BROKEN, {"http": code, "body": quo})
    quote_ref = quo["data"]["quote_ref"]
    s(OK, {"quote_ref": quote_ref, "unit_price": quo["data"]["unit_price"],
           "list_price": list_price})

    if a.skip_accept:
        s = Step("5 accept")
        return s(OK, {"skipped": True, "quote_ref": quote_ref})

    # ---- 5) 接受 → 订单 ----
    s = Step("5 accept")
    headers = dict(H)
    if a.grant:
        headers["Authorization"] = f"Bearer {a.grant}"
    code, acc = _req("POST",
                     f"{api_base}/erp/supply-chain/v1/trade/quotes/{quote_ref}/accept", {
                         "qty": a.qty, "shipTo": {"name": "链路探针",
                                                  "phone": "13800000000"},
                         "channel": "retail-online"},
                     headers)
    if code != 201:
        return s(BROKEN, {"http": code, "body": acc})
    data = acc["data"]
    if not (data.get("quote_ref") == quote_ref
            and _amount_eq(data["amount_total"], quoted, a.qty)):
        return s(BROKEN, {"order": data, "why": "成交价或 quote_ref 不符",
                          "expected_unit": quoted, "qty": a.qty})
    s(OK, {"order_ref": data["order_ref"], "amount_total": data["amount_total"],
           "quote_ref": data["quote_ref"], "payment_state": data["payment_state"]})

    print(json.dumps({"ok": True, "tenant": tenant, "product": product,
                      "list_price": list_price, "quoted_price": quoted,
                      "order_ref": data["order_ref"],
                      "amount_total": data["amount_total"]},
                     ensure_ascii=False))
    return OK


def _amount_eq(total, unit, qty) -> bool:
    from decimal import Decimal
    try:
        return Decimal(str(total)) == Decimal(str(unit)) * Decimal(str(qty))
    except Exception:  # noqa: BLE001
        return False


def _discount(list_price) -> str:
    from decimal import Decimal
    if list_price in (None, ""):
        return "1"
    return str((Decimal(str(list_price)) * Decimal("0.75")).quantize(Decimal("0.01")))


def _future(days: int) -> str:
    from datetime import date, timedelta
    return (date.today() + timedelta(days=days)).isoformat()


if __name__ == "__main__":
    sys.exit(main())
