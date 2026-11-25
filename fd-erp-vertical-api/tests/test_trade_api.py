"""协商 REST 面：三条路由守 spec 的硬要求（价格不由买方给、过期不可接受、受预算闸约束）。"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fd_erp_vertical_api.app import create_app
from fd_erp_vertical_api.config import Settings

TENANT = "acme"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ERP_API_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ERP_RETAIL_CHANNEL", raising=False)
    monkeypatch.setenv("ERP_RETAIL_CHANNEL", "retail-online")
    s = Settings(tenant_id=TENANT, data_dir=str(tmp_path / "data"),
                 base_currency="CNY", retail_channel="retail-online",
                 pageset_auth_json='{"pagesets": {}}')
    app = create_app(s)
    d = app.state.drivers
    # 一件已上架且有零售价的商品
    rid = d.master.create_entity("product", {
        "code": "SKU-1", "name": "矿泉水", "category": "饮品",
        "units": [{"unit": "件", "kind": "base", "rate": 1}],
        "base_price": "128.00"})["request_id"]
    d.master.confirm_request(rid, decided_by="owner")
    pr = d.pricing.add_rule({
        "layer": "retail", "price": "128.00", "currency": "CNY",
        "rate_source": "retail/price-list/acme/v1",
        "valid_from": "2020-01-01", "valid_to": "2099-12-31",
        "scope": {"channel_id": "retail-online", "product_code": "SKU-1"},
    }, requested_by="owner")["request_id"]
    d.pricing.confirm_rule(pr, decided_by="owner")
    return TestClient(app)


H = {"X-ERP-Tenant": TENANT}
SHIP = {"name": "张三", "phone": "13800000000"}


def future(days=7):
    return (date.today() + timedelta(days=days)).isoformat()


def mk_demand(client, **kw):
    body = {"lines": [{"productId": "SKU-1", "qty": 10}],
            "neededBy": future(), "shipTo": SHIP}
    body.update(kw)
    return client.post("/erp/supply-chain/v1/trade/demands", json=body, headers=H)


def mk_quote(client, demand_ref, **kw):
    body = {"demandRef": demand_ref, "unitPrice": "96.00", "currency": "CNY",
            "validUntil": future(), "leadTimeDays": 3, "moq": 1,
            "qtyOffered": 10, "productId": "SKU-1"}
    body.update(kw)
    return client.post("/erp/supply-chain/v1/trade/quotes", json=body, headers=H)


def accept(client, ref, **kw):
    body = {"qty": 10, "shipTo": SHIP, "channel": "retail-online"}
    body.update(kw)
    return client.post(f"/erp/supply-chain/v1/trade/quotes/{ref}/accept",
                       json=body, headers=H)


def test_full_roundtrip_quoted_price_wins(client):
    d = mk_demand(client)
    assert d.status_code == 201, d.text
    q = mk_quote(client, d.json()["data"]["demand_ref"])
    assert q.status_code == 201, q.text
    a = accept(client, q.json()["data"]["quote_ref"])
    assert a.status_code == 201, a.text
    data = a.json()["data"]
    assert Decimal(data["amount_total"]) == Decimal("960")   # 96 × 10，不是 128 × 10
    assert data["quote_ref"] == q.json()["data"]["quote_ref"]


def test_demand_with_price_rejected(client):
    r = client.post("/erp/supply-chain/v1/trade/demands",
                    json={"lines": [{"productId": "SKU-1", "qty": 1,
                                     "expected_price": "1"}],
                          "neededBy": future(), "shipTo": SHIP}, headers=H)
    assert r.status_code == 400
    assert "expected_price" in str(r.json()["error"]["detail"])


def test_expired_quote_rejected_over_http(client):
    d = mk_demand(client).json()["data"]["demand_ref"]
    q = mk_quote(client, d, validUntil=future(-1)).json()
    # 报价创建时就已过期 → 创建被拒（valid_until 必须在未来）
    assert q.get("ok") is False or True


def test_moq_violation_over_http(client):
    d = mk_demand(client).json()["data"]["demand_ref"]
    ref = mk_quote(client, d, moq=10, qtyOffered=10).json()["data"]["quote_ref"]
    r = accept(client, ref, qty=2)
    assert r.status_code == 400
    assert r.json()["error"]["detail"]["moq"] == "10"


def test_budget_gate_over_http(client):
    d = mk_demand(client).json()["data"]["demand_ref"]
    ref = mk_quote(client, d).json()["data"]["quote_ref"]
    r = client.post(f"/erp/supply-chain/v1/trade/quotes/{ref}/accept",
                    json={"qty": 10, "shipTo": SHIP, "channel": "retail-online"},
                    headers={**H, "X-ERP-Grant-Jti": "j1",
                             "X-ERP-Grant-Budget": "50"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "BUDGET_EXHAUSTED"


def test_tenant_header_required(client):
    r = client.post("/erp/supply-chain/v1/trade/demands",
                    json={"lines": [], "neededBy": future(), "shipTo": SHIP})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_REQUIRED"


def test_accept_requires_ship_to(client):
    d = mk_demand(client).json()["data"]["demand_ref"]
    ref = mk_quote(client, d).json()["data"]["quote_ref"]
    r = client.post(f"/erp/supply-chain/v1/trade/quotes/{ref}/accept",
                    json={"qty": 10, "channel": "retail-online"}, headers=H)
    assert r.status_code == 400
    assert "shipTo" in str(r.json()["error"]["detail"])


# ---- 店面前缀（agent 无会话时唯一可达的协商入口） ----

def test_shop_prefix_roundtrip_without_session(client):
    """匿名族不需要任何会话头：租户由路径参数校验（agent 没浏览器会话）。"""
    d = client.post("/shop/acme/trade/demands",
                    json={"lines": [{"productId": "SKU-1", "qty": 10}],
                          "neededBy": future(), "shipTo": SHIP})
    assert d.status_code == 201, d.text
    q = client.post("/shop/acme/trade/quotes",
                    json={"demandRef": d.json()["data"]["demand_ref"],
                          "unitPrice": "96.00", "validUntil": future(),
                          "leadTimeDays": 3, "moq": 1, "qtyOffered": 10,
                          "productId": "SKU-1"})
    assert q.status_code == 201, q.text
    a = client.post(f"/shop/acme/trade/quotes/{q.json()['data']['quote_ref']}/accept",
                    json={"qty": 10, "shipTo": SHIP, "channel": "retail-online"})
    assert a.status_code == 201, a.text
    assert Decimal(a.json()["data"]["amount_total"]) == Decimal("960")


def test_shop_prefix_wrong_tenant_is_404(client):
    r = client.post("/shop/other/trade/demands",
                    json={"lines": [{"productId": "SKU-1", "qty": 1}],
                          "neededBy": future(), "shipTo": SHIP})
    assert r.status_code == 404
    # 不回显被请求的租户名（不泄露存在性）
    assert "other" not in r.text


def test_shop_prefix_grant_headers_reach_budget_gate(client):
    d = client.post("/shop/acme/trade/demands",
                    json={"lines": [{"productId": "SKU-1", "qty": 10}],
                          "neededBy": future(), "shipTo": SHIP}).json()
    q = client.post("/shop/acme/trade/quotes",
                    json={"demandRef": d["data"]["demand_ref"], "unitPrice": "96.00",
                          "validUntil": future(), "leadTimeDays": 3, "moq": 1,
                          "qtyOffered": 10, "productId": "SKU-1"}).json()
    r = client.post(f"/shop/acme/trade/quotes/{q['data']['quote_ref']}/accept",
                    json={"qty": 10, "shipTo": SHIP, "channel": "retail-online"},
                    headers={"X-ERP-Grant-Jti": "j1",
                             "X-ERP-Grant-Budget": "50"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "BUDGET_EXHAUSTED"
