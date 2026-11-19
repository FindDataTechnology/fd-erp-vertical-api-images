"""授权面与契约面：跨租户拒绝 / fail-closed / grant 闸 / 501 如实 / 分页与壳。"""
from __future__ import annotations

import json

from fd_erp_vertical_api.pagesets import KNOWN_GRANTS, KNOWN_PAGESET_KEYS

from .conftest import TENANT, _h

# paas/miniapp/src/pagesets/authorize.ts KNOWN_KEYS（对齐锚：一字不差）
PAAS_KNOWN_KEYS = ["supply-chain.ue", "supply-chain.se",
                   "supply-chain.sp", "supply-chain.cp"]

# paas/miniapp/src/pagesets/registry.ts 四页面集 requiredGrants 的并集
# （对齐锚：SE/SP/CP 面后端化后仍须逐字相等 —— 不等须同步 paas 侧）
PAAS_REQUIRED_GRANTS_UNION = {
    # UE
    "catalog.read", "order.write", "statement.confirm",
    "invoice.write", "aftersale.write", "ai.chat",
    # SE
    "order.write.proxy", "crm.read", "crm.visit.write",
    "alert.handle", "statement.confirm.proxy",
    # SP
    "po.read", "po.accept", "delivery.feedback",
    "statement.read", "invoice.upload",
    # CP
    "waybill.read", "waybill.accept", "dock.reserve",
    "pickup.checkin", "pod.report", "freight.statement.read",
}


def test_pageset_key_catalog_matches_paas_client():
    """pagesets 端点输出 key 白名单与 paas 客户端严格解析白名单逐字对齐。"""
    assert list(KNOWN_PAGESET_KEYS) == PAAS_KNOWN_KEYS
    # 端点只可能输出白名单 key（load_pageset_auth 丢弃未知 key）
    from fd_erp_vertical_api.pagesets import load_pageset_auth
    auth = load_pageset_auth(inline_json=json.dumps({
        "supply-chain.ue": ["catalog.read"],
        "supply-chain.mall": ["catalog.read"],  # 非白名单 key：丢弃（fail-closed）
    }))
    assert auth.grants_of("supply-chain.ue") is not None
    assert auth.grants_of("supply-chain.mall") is None
    wire = auth.wire_payload()["pagesets"]
    assert wire == [{"key": "supply-chain.ue", "grants": ["catalog.read"]}]
    # grant 封闭集：集外码一律丢弃
    auth2 = load_pageset_auth(inline_json=json.dumps(
        {"supply-chain.ue": ["catalog.read", "root.everything"]}))
    assert auth2.grants_of("supply-chain.ue") == frozenset({"catalog.read"})
    assert "root.everything" not in KNOWN_GRANTS


def test_cross_tenant_denied(client, seeded):
    r = client.get("/erp/supply-chain/v1/pagesets", headers=_h(None, tenant="intruder"))
    assert r.status_code == 403
    body = r.json()
    assert body["ok"] is False and body["rows_returned"] == 0
    assert body["error"]["code"] == "TENANT_DENIED"
    assert "hint" in body["error"] and "detail" in body["error"]


def test_missing_tenant_header_unauthorized(client):
    r = client.get("/erp/supply-chain/v1/pagesets")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_REQUIRED"


def test_unknown_customer_404_no_existence_leak(client, seeded):
    r = client.get("/erp/supply-chain/v1/ue/products",
                   headers=_h("nonexistent-customer"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_fail_closed_no_auth_config(client_unauthorized, seeded):
    """无授权配置 → pagesets 空 + /ue/* 全 403（fail-closed，不可见即不可调）。"""
    r = client_unauthorized.get("/erp/supply-chain/v1/pagesets",
                                headers=_h(seeded["cust1"]))
    assert r.status_code == 200
    assert r.json()["data"]["pagesets"] == []
    r = client_unauthorized.get("/erp/supply-chain/v1/ue/products",
                                headers=_h(seeded["cust1"]))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "PAGESET_FORBIDDEN"


def test_grant_required(client_no_statement_grant, seeded):
    """页面集已授权但缺 statement.confirm → 对账域 403 GRANT_REQUIRED。"""
    r = client_no_statement_grant.get("/erp/supply-chain/v1/ue/statements",
                                      headers=_h(seeded["cust1"]))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "GRANT_REQUIRED"
    # 已授权域不受影响
    r = client_no_statement_grant.get("/erp/supply-chain/v1/ue/products",
                                      headers=_h(seeded["cust1"]))
    assert r.status_code == 200


def test_grant_catalog_matches_paas_registry_union():
    """grant 封闭集与 paas registry 四页面集 requiredGrants 并集精确相等。"""
    assert set(KNOWN_GRANTS) == PAAS_REQUIRED_GRANTS_UNION


#: 契约里有、后端能力未就绪的 UE 端点（如实 501）。这份清单会随能力落地**变短**：
#: 售后三条（`add-aftersale-lane`）、开票两条（`add-invoice-request-lane`）都已实现，
#: 已移出。清单里留着已实现的端点，就变成「实现反而让测试变红」，最后没人敢实现。
#: UE 面目前 19/19 全部实现，故为空——只剩 SE/SP/CP 的挂账面，见
#: `tests/test_faces_e2e.py` 里各面的 501 段。
UE_UNIMPLEMENTED: tuple[tuple[str, str, dict | None], ...] = ()


def test_unimplemented_endpoints_return_501(client, seeded):
    """契约里有、后端能力未就绪的端点如实 501 NOT_IMPLEMENTED（禁止假实现）。"""
    for method, path, payload in UE_UNIMPLEMENTED:
        r = (client.get(path, headers=_h(seeded["cust1"])) if method == "GET"
             else client.post(path, headers=_h(seeded["cust1"]), json=payload))
        assert r.status_code == 501, (method, path, r.status_code)
        err = r.json()["error"]
        assert err["code"] == "NOT_IMPLEMENTED"
        assert err["detail"].get("planned_lane")
    assert UE_UNIMPLEMENTED == (), (
        "UE 面已 19/19 实现；若新增挂账端点，把它加回本清单并说明为什么还没做")


def test_pagination_cap_and_envelope(client, seeded):
    """分页续读 + 体积上限 + 统一响应壳（沿内核 result.py 语义）。"""
    d = client.app.state.drivers
    for i in range(3):
        req = d.master.create_entity("product", {
            "code": f"SKU-P{i}", "name": f"分页商品 {i}", "base_price": "3",
            "units": [{"unit": "个", "kind": "base", "rate": "1"}]})
        d.master.confirm_request(req["request_id"])
        preq = d.master.create_pool({
            "name": f"分页池 {i}", "visibility": "authorized",
            "org_path": "/集团/华东", "product_codes": [f"SKU-P{i}"]})
        d.master.confirm_request(preq["request_id"])
        areq = d.master.authorize_pool(preq["entity_id"],
                                       customers=[seeded["cust1"]])
        d.master.confirm_request(areq["request_id"])

    r = client.get("/erp/supply-chain/v1/ue/products",
                   headers=_h(seeded["cust1"]), params={"limit": 2, "offset": 0})
    body = r.json()
    assert body["ok"] is True and body["rows_returned"] == 2
    assert body["total"] == 4 and body["limit"] == 2
    assert body["truncated"] is True
    assert body["continuation"] == {"method": "offset", "next_offset": 2}
    r = client.get("/erp/supply-chain/v1/ue/products",
                   headers=_h(seeded["cust1"]), params={"limit": 2, "offset": 2})
    assert r.json()["rows_returned"] == 2 and r.json()["truncated"] is False
    r = client.get("/erp/supply-chain/v1/ue/products",
                   headers=_h(seeded["cust1"]), params={"page": 2, "limit": 2})
    assert r.json()["offset"] == 2

    # 体积上限：limit 超请求上限被 cap 到 500，不放开
    r = client.get("/erp/supply-chain/v1/ue/products",
                   headers=_h(seeded["cust1"]), params={"limit": 99999})
    assert r.json()["limit"] == 500

    # 非法分页参数 → 400 封闭码
    r = client.get("/erp/supply-chain/v1/ue/products",
                   headers=_h(seeded["cust1"]), params={"offset": -1})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_error_envelope_shape_and_closed_codes(client):
    """错误壳四件齐：code/message/hint/detail；ok=false + rows_returned=0。"""
    r = client.get("/erp/supply-chain/v1/ue/products/SKU-NOPE",
                   headers=_h("any-customer"))
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False and body["rows_returned"] == 0
    err = body["error"]
    assert set(err) >= {"code", "message", "hint", "detail"}
    from fd_erp_vertical_api.errors import ERROR_CATALOG
    assert err["code"] in ERROR_CATALOG


def test_malformed_body_maps_to_invalid_argument(client, seeded):
    """非对象 JSON 体 → 400 INVALID_ARGUMENT（不外溢 500）。"""
    r = client.post("/erp/supply-chain/v1/ue/cart/lines", headers=_h(seeded["cust1"]),
                    json=[1, 2, 3])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_selfcheck_closed_catalog():
    from fd_erp_vertical_api.error_codes_selfcheck import assert_closed_catalog
    codes = assert_closed_catalog()
    for required in ("NOT_WIRED", "NOT_FOUND", "UPSTREAM_UNAVAILABLE",
                     "INVALID_ARGUMENT", "NOT_IMPLEMENTED", "PAGESET_FORBIDDEN",
                     "GRANT_REQUIRED", "AUTH_REQUIRED", "TENANT_DENIED",
                     "CREDIT_EXCEEDED"):
        assert required in codes
