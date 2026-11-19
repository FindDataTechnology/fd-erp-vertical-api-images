"""测试夹具：真库 erp_test_b（127.0.0.1:30440）+ InProcess 内核 + TestClient。

- DSN：优先 ERP_LEDGER_DATABASE_URL；否则读 secrets/erp-demo.env 的
  ERP_LEDGER_DATABASE_URL 行并把库名 erp_demo 替换为 erp_test_b。
  口令绝不打印（本文件任何路径都不输出 DSN）。
- migrate 一次（session），逐用例 TRUNCATE 内核表（约束层不可 mock，同模板
  包 conftest 形态；测试侧允许 import 内核/psycopg —— boundary 审计只扫包本体）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FINDDATA = REPO_ROOT.parent
for p in (str(REPO_ROOT), str(FINDDATA / "fd-erp-ledger-mcp"),
          str(FINDDATA / "fd-erp-vertical-templates")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fd_erp_ledger_mcp.db import connection, migrate  # noqa: E402
from fd_erp_ledger_mcp.fx import seed_fx_facts  # noqa: E402
from fd_erp_ledger_mcp.universal_accounts import seed_universal_accounts  # noqa: E402

from fd_erp_vertical_api.app import create_app  # noqa: E402
from fd_erp_vertical_api.cart import CartStore  # noqa: E402
from fd_erp_vertical_api.config import Settings  # noqa: E402
from fd_erp_vertical_api.kernel_client import InProcessKernelClient  # noqa: E402
from fd_erp_vertical_api.pagesets import load_pageset_auth  # noqa: E402

TENANT = "api-e2e"
TENANT_B = "intruder-tenant"

FULL_GRANTS = {
    "supply-chain.ue": ["catalog.read", "order.write", "statement.confirm",
                        "invoice.write", "aftersale.write", "ai.chat"],
}
NO_STATEMENT_GRANTS = {
    "supply-chain.ue": ["catalog.read", "order.write"],
}
#: 四页面集全量授权（SE/SP/CP 面 e2e 用；grant 集 = paas registry requiredGrants）
FULL_GRANTS_4 = {
    **FULL_GRANTS,
    "supply-chain.se": ["catalog.read", "order.write.proxy", "crm.read",
                        "crm.visit.write", "alert.handle",
                        "statement.confirm.proxy", "ai.chat"],
    "supply-chain.sp": ["po.read", "po.accept", "delivery.feedback",
                        "statement.read", "invoice.upload", "ai.chat"],
    "supply-chain.cp": ["waybill.read", "waybill.accept", "dock.reserve",
                        "pickup.checkin", "pod.report",
                        "freight.statement.read", "ai.chat"],
}


def _swap_db(dsn: str) -> str:
    """库名 erp_demo → erp_test_b（只换库名，用户/口令原样；绝不打印）。"""
    if dsn.endswith("/erp_demo"):
        return dsn[: -len("/erp_demo")] + "/erp_test_b"
    return dsn


def _resolve_dsn() -> str:
    dsn = os.environ.get("ERP_LEDGER_DATABASE_URL")
    if dsn:
        return _swap_db(dsn)
    secrets = FINDDATA / "secrets" / "erp-demo.env"
    for line in secrets.read_text(encoding="utf-8").splitlines():
        if line.startswith("ERP_LEDGER_DATABASE_URL="):
            return _swap_db(line.split("=", 1)[1].strip())
    pytest.skip("未找到 ERP_LEDGER_DATABASE_URL（env 或 secrets/erp-demo.env）")


@pytest.fixture(scope="session")
def dsn() -> str:
    return _resolve_dsn()


@pytest.fixture(scope="session")
def db_settings(dsn):
    s = Settings(tenant_id=TENANT, database_url=dsn, base_currency="CNY")
    migrate(s)
    return s


@pytest.fixture()
def reset_db(db_settings):
    with psycopg.connect(db_settings.database_url, autocommit=True) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('erp.allow_maintenance','on', true)")
                cur.execute(
                    "TRUNCATE proposals, journal_entries, fx_facts, recon_reports, "
                    "recon_task_items, audit, accounts, "
                    "fee_details, billing_rules, "
                    "credit_registry, credit_reservations, "
                    # 事件层七表（establish-erp-event-kernel 7.1 起 REST 面会写它们）：
                    # 漏掉就会让上一个用例的到货事实漏进下一个用例的库存与分录。
                    # business_events 的 TRUNCATE 由 erp.allow_maintenance 开关放行。
                    "business_events, event_revisions, objects, object_events, "
                    "object_balances, posting_rules, posting_links, posting_gaps, "
                    # 期间台账（add-period-close）：漏掉它，上一个用例关掉的期间会
                    # 拦住下一个用例的第一次落账——症状是「凭空 PERIOD_CLOSED」。
                    "accounting_periods "
                    "RESTART IDENTITY CASCADE")
    with connection(db_settings) as conn:
        seed_universal_accounts(conn, TENANT)
    return db_settings


def _make_settings(tmp_path, dsn, *, pageset_json=None, wms_file=None,
                   tenant=TENANT) -> Settings:
    return Settings(
        tenant_id=tenant, database_url=dsn, base_currency="CNY",
        data_dir=str(tmp_path / "data"),
        pageset_auth_json=pageset_json,
        wms_snapshot_file=str(wms_file) if wms_file else None,
    )


def _make_app(tmp_path, dsn, *, pageset_json=None, wms_file=None) -> "TestClient":
    from fastapi.testclient import TestClient
    settings = _make_settings(tmp_path, dsn, pageset_json=pageset_json,
                              wms_file=wms_file)
    kernel = InProcessKernelClient(settings)
    auth = load_pageset_auth(inline_json=pageset_json)
    app = create_app(settings, kernel, pageset_auth=auth, cart_store=CartStore())
    return TestClient(app)


@pytest.fixture()
def client(reset_db, tmp_path, dsn):
    """主应用：全量 UE 授权 + WMS 快照文件。"""
    wms = tmp_path / "wms.json"
    wms.write_text(json.dumps([
        {"product_code": "SKU-001", "warehouse": "WH-华东", "qty": "66",
         "status": "available", "occurred_at": "2026-11-07T08:00:00"},
    ]), encoding="utf-8")
    c = _make_app(tmp_path, dsn, pageset_json=json.dumps(FULL_GRANTS),
                  wms_file=wms)
    c._settings = _make_settings(tmp_path, dsn, pageset_json=json.dumps(FULL_GRANTS),
                                 wms_file=wms)
    return c


@pytest.fixture()
def client_no_statement_grant(reset_db, tmp_path, dsn):
    return _make_app(tmp_path, dsn, pageset_json=json.dumps(NO_STATEMENT_GRANTS))


@pytest.fixture()
def client_unauthorized(reset_db, tmp_path, dsn):
    """fail-closed：无授权配置。"""
    return _make_app(tmp_path, dsn, pageset_json=None)


@pytest.fixture()
def client_faces(reset_db, tmp_path, dsn):
    """SE/SP/CP 三面应用：四页面集全量授权（FULL_GRANTS_4）。"""
    return _make_app(tmp_path, dsn, pageset_json=json.dumps(FULL_GRANTS_4))


def _confirm_master(client: "TestClient", kind: str, payload: dict,
                    code: str, *, authorize: dict | None = None) -> str:
    """建档确认车道：create → confirm（商品池再 authorize → confirm）。"""
    created = client.app.state.drivers.master.create_entity(kind, payload)
    client.app.state.drivers.master.confirm_request(created["request_id"])
    if kind == "product_pool" and authorize:
        req = client.app.state.drivers.master.authorize_pool(
            created["entity_id"], **authorize)
        client.app.state.drivers.master.confirm_request(req["request_id"])
    return created["entity_id"]


@pytest.fixture()
def seeded(client):
    """主数据种子：两个客户 + 一个商品（带分类/图片/多单位）+ 授权商品池。

    并给两客户设置内核信用额度（账本记账，出处留痕）。
    """
    cust1 = _confirm_master(client, "customer",
                            {"code": "C-001", "name": "示范客户一号"}, "C-001")
    cust2 = _confirm_master(client, "customer",
                            {"code": "C-002", "name": "示范客户二号"}, "C-002")
    prod = _confirm_master(client, "product", {
        "code": "SKU-001", "name": "示范商品 A", "category": "饮料/水",
        "base_price": "10", "images": ["https://cdn.example/img/a.png"],
        "units": [{"unit": "个", "kind": "base", "rate": "1"},
                  {"unit": "箱", "kind": "sales", "rate": "10"}],
    }, "SKU-001")
    pool = _confirm_master(client, "product_pool", {
        "name": "UE 订货池", "visibility": "authorized",
        "org_path": "/集团/华东", "product_codes": ["SKU-001"],
    }, "pool", authorize={"customers": [cust1, cust2]})
    from fd_erp_ledger_mcp import credit as kernel_credit
    from fd_erp_ledger_mcp.db import connection as kconn
    settings = client._settings
    for cid in (cust1, cust2):
        with kconn(settings) as conn:
            kernel_credit.set_credit_limit(
                conn, tenant_id=TENANT, customer_id=cid, limit_amount="5000",
                currency="CNY", source_ref="policy:demo-credit-policy-2026")
    return {"cust1": cust1, "cust2": cust2, "prod": prod, "pool": pool}


def _h(customer_id: str | None = None, tenant: str = TENANT) -> dict:
    headers = {"X-ERP-Tenant": tenant}
    if customer_id:
        headers["X-ERP-Customer"] = customer_id
    return headers


def _h_se(staff: str = "S-001", tenant: str = TENANT) -> dict:
    """SE 身份头：业务员工号（归属关系 = 客户主数据 sales_rep）。"""
    return {"X-ERP-Tenant": tenant, "X-ERP-Staff": staff}


def _h_sp(supplier: str, tenant: str = TENANT) -> dict:
    """SP 身份头：供应商档案实体 id 或编码。"""
    return {"X-ERP-Tenant": tenant, "X-ERP-Supplier": supplier}


def _h_cp(carrier: str, tenant: str = TENANT) -> dict:
    """CP 身份头：承运商标识（运单 carrier 域）。"""
    return {"X-ERP-Tenant": tenant, "X-ERP-Carrier": carrier}


@pytest.fixture()
def seeded_faces(client_faces):
    """三面主数据种子：业务员名下客户（sales_rep）+ 供应商 + 商品（供货关联）
    + 授权商品池 + 内核信用额度。"""
    cust1 = _confirm_master(client_faces, "customer",
                            {"code": "C-001", "name": "示范客户一号",
                             "sales_rep": "S-001"}, "C-001")
    cust2 = _confirm_master(client_faces, "customer",
                            {"code": "C-002", "name": "示范客户二号",
                             "sales_rep": "S-002"}, "C-002")
    supplier = _confirm_master(client_faces, "supplier",
                               {"code": "SUP-001", "name": "示范供应商一号"},
                               "SUP-001")
    supplier_b = _confirm_master(client_faces, "supplier",
                                 {"code": "SUP-002", "name": "示范供应商二号"},
                                 "SUP-002")
    prod = _confirm_master(client_faces, "product", {
        "code": "SKU-001", "name": "示范商品 A", "category": "饮料/水",
        "base_price": "10", "supplier_id": supplier,
        "images": ["https://cdn.example/img/a.png"],
        "units": [{"unit": "个", "kind": "base", "rate": "1"},
                  {"unit": "箱", "kind": "sales", "rate": "10"}],
    }, "SKU-001")
    pool = _confirm_master(client_faces, "product_pool", {
        "name": "全端订货池", "visibility": "authorized",
        "org_path": "/集团/华东", "product_codes": ["SKU-001"],
    }, "pool", authorize={"customers": [cust1, cust2]})
    from fd_erp_ledger_mcp import credit as kernel_credit
    from fd_erp_ledger_mcp.db import connection as kconn
    settings = client_faces.app.state.settings
    for cid in (cust1, cust2):
        with kconn(settings) as conn:
            kernel_credit.set_credit_limit(
                conn, tenant_id=TENANT, customer_id=cid, limit_amount="5000",
                currency="CNY", source_ref="policy:demo-credit-policy-2026")
    return {"cust1": cust1, "cust2": cust2, "supplier": supplier,
            "supplier_b": supplier_b, "prod": prod, "pool": pool}
