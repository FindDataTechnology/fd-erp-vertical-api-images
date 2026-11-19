"""REST 面访问日志（establish-erp-independent-root 任务 3.4b）。

这一组钉的是「客户报障时能不能回答这一单为什么被拒」，具体三条：

1. **被闸拒掉的请求必须留痕**。访问日志中间件若注册在租户闸内侧，401/403 永远
   到不了它——而这恰恰是唯一值得逐条看的流量。断言放在最外层，靠用例钉住。
2. **租户明文不许落盘**，跨租户被拒时记的是请求方声称那个租户的哈希。
3. **正常流量默认不写日志**（errors 模式），否则一行一请求会把盘和检索都拖垮。

用例不需要数据库：租户闸/页面集闸在碰库之前就返回了。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FINDDATA = REPO.parent
for p in (str(REPO), str(FINDDATA / "fd-erp-vertical-templates"),
          str(FINDDATA / "fd-erp-ledger-mcp")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fd_erp_vertical_api import logs  # noqa: E402
from fd_erp_vertical_api.app import create_app  # noqa: E402
from fd_erp_vertical_api.config import Settings  # noqa: E402

GRANTS = {"supply-chain.ue": ["catalog.read"]}


@pytest.fixture()
def make_client(tmp_path):
    from fastapi.testclient import TestClient

    def _make(*, grants: dict | None = None):
        settings = Settings(
            tenant_id="t1", database_url="postgresql://x/y", base_currency="CNY",
            data_dir=str(tmp_path / "data"),
            pageset_auth_json=json.dumps(grants if grants is not None else GRANTS),
        )
        return TestClient(create_app(settings))

    return _make


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例从默认口径开始：errors 模式、无残留的非法值告警记录。"""
    monkeypatch.delenv(logs.ACCESS_LOG_ENV, raising=False)
    logs._warned_modes.clear()
    yield
    logs._warned_modes.clear()


def _lines(captured: str) -> list[dict]:
    out = []
    for line in captured.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") in (logs.ACCESS_EVENT, "access_log_mode_invalid"):
            out.append(row)
    return out


def _access(captured: str) -> list[dict]:
    return [r for r in _lines(captured) if r["event"] == logs.ACCESS_EVENT]


def test_tenant_gate_rejection_is_logged(make_client, capfd):
    """被租户闸 401 拒掉的请求必须落一行，且带业务码与路由模板。"""
    client = make_client()
    r = client.get("/erp/supply-chain/v1/ue/orders/ORD-20261111-0001")
    assert r.status_code == 401

    rows = _access(capfd.readouterr().out)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["status"] == 401
    assert row["code"] == "AUTH_REQUIRED"
    assert row["method"] == "GET"
    # 路由模板：id 段归一，不把真实单号写进日志
    assert row["route"] == "/erp/supply-chain/v1/ue/orders/{id}"
    assert "ORD-20261111-0001" not in json.dumps(row, ensure_ascii=False)
    assert row["request_id"]
    assert row["dur_ms"] >= 0


def test_cross_tenant_denied_logs_hash_not_plaintext(make_client, capfd):
    """跨租户被拒：记请求方声称租户的哈希，明文一个字都不落盘。"""
    client = make_client()
    r = client.get("/erp/supply-chain/v1/ue/orders",
                   headers={"X-ERP-Tenant": "intruder-corp"})
    assert r.status_code == 403

    out = capfd.readouterr().out
    rows = _access(out)
    assert len(rows) == 1, rows
    assert rows[0]["code"] == "TENANT_DENIED"
    assert rows[0]["tenant_hash"] == logs.tenant_hash("intruder-corp")
    assert "intruder-corp" not in out
    # detail 只记键名（值里有租户名），键名不含租户值
    assert rows[0]["detail_keys"] == ["cell_tenant", "requested_tenant"]


def test_success_is_quiet_by_default(make_client, capfd):
    """errors 模式：200 不产生任何访问日志行（探针每 10s 一次，不能写盘）。"""
    client = make_client()
    assert client.get("/health").status_code == 200
    assert _access(capfd.readouterr().out) == []


def test_all_mode_logs_success(make_client, capfd, monkeypatch):
    monkeypatch.setenv(logs.ACCESS_LOG_ENV, "all")
    client = make_client()
    assert client.get("/health").status_code == 200
    rows = _access(capfd.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["route"] == "/health" and rows[0]["status"] == 200
    assert rows[0]["tenant_hash"] == logs.tenant_hash("t1")


def test_off_mode_logs_nothing(make_client, capfd, monkeypatch):
    monkeypatch.setenv(logs.ACCESS_LOG_ENV, "off")
    client = make_client()
    assert client.get("/erp/supply-chain/v1/ue/orders").status_code == 401
    assert _lines(capfd.readouterr().out) == []


def test_invalid_mode_warns_once_and_falls_back(make_client, capfd, monkeypatch):
    """写错开关值要看得见：回落 errors 并告警一次，而不是静默当成 errors。"""
    monkeypatch.setenv(logs.ACCESS_LOG_ENV, "verbose")
    client = make_client()
    client.get("/erp/supply-chain/v1/ue/orders")
    client.get("/erp/supply-chain/v1/ue/orders")
    rows = _lines(capfd.readouterr().out)
    warns = [r for r in rows if r["event"] == "access_log_mode_invalid"]
    assert len(warns) == 1, warns
    assert warns[0]["configured"] == "verbose"
    assert len(_access("\n".join(json.dumps(r) for r in rows))) == 2  # errors 模式仍生效


def test_request_id_roundtrip_and_injection_rejected(make_client, capfd):
    """调用方的 request-id 原样回传（可跨网关关联）；危险值一律换新。"""
    client = make_client()
    ok = "req-abc-123"
    r = client.get("/erp/supply-chain/v1/ue/orders", headers={"X-Request-Id": ok})
    assert r.headers[logs.REQUEST_ID_HEADER] == ok

    evil = "x\n{\"event\":\"forged\"}"
    r = client.get("/erp/supply-chain/v1/ue/orders", headers={"X-Request-Id": evil})
    rid = r.headers[logs.REQUEST_ID_HEADER]
    assert rid != evil and len(rid) == 16
    out = capfd.readouterr().out
    assert "forged" not in out


def test_grant_recorded_on_rejection(make_client, capfd):
    """缺 grant 时日志要指到具体那一条权限码（否则只知道 403、不知道缺什么）。"""
    client = make_client(grants={"supply-chain.ue": []})
    r = client.get("/erp/supply-chain/v1/ue/orders",
                   headers={"X-ERP-Tenant": "t1", "X-ERP-Customer": "C-001"})
    assert r.status_code == 403
    rows = _access(capfd.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["code"] == "GRANT_REQUIRED"
    assert rows[0]["grant"] == "order.write"


REDACTION_VECTORS = [
    ({"password": "p@ss"}, ["p@ss"], []),
    ({"apiKey": "sk-live-SECRET"}, ["sk-live-SECRET"], []),
    ({"api-key": "sk-SECRET"}, ["sk-SECRET"], []),
    ({"API_KEY": "sk-SECRET"}, ["sk-SECRET"], []),
    ({"detail": {"dsn": "postgresql://u:secret@h/db"}}, ["secret"], []),
    ({"items": [{"api_key": "k1"}, {"note": "ok"}]}, ["k1"], ["ok"]),
    ({"msg": "conn failed postgresql://u:secret@h/db"}, ["secret"], []),
    ({"headers": {"authorization": "Bearer abc123"}}, ["abc123"], []),
    ({"tenant_id": "acme-corp"}, ["acme-corp"], ["tenant_hash"]),
    ([{"password": "deep"}], ["deep"], []),
    ({"items": [[{"password": "deep"}]]}, ["deep"], []),
    ({"count": 3, "ok": True, "ratio": 0.5, "note": "正常"}, [], ["正常"]),
]


@pytest.mark.parametrize("payload,forbidden,required", REDACTION_VECTORS)
def test_redaction_vectors(payload, forbidden, required):
    """与 fd-erp/shared/log-contract.json 的 12 条向量同集（跨仓闸还会再跑一遍）。"""
    text = json.dumps(logs.redact(payload), ensure_ascii=False, sort_keys=True)
    for needle in forbidden:
        assert needle not in text, (payload, text)
    for needle in required:
        assert needle in text, (payload, text)


def test_serve_turns_off_uvicorn_access_log(monkeypatch, tmp_path):
    """uvicorn 自带的 access log 必须关掉。

    不关的话每个请求写两遍，而且 `/health` 探针每 10s 一次的 200 照样落盘——
    「errors 档 = 正常流量零日志行」就成假话（2026-11-11 上产后实测撞到）。
    """
    import uvicorn
    from click.testing import CliRunner

    from fd_erp_vertical_api import cli as cli_mod

    seen: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(
        cli_mod.Settings, "from_env",
        classmethod(lambda cls: cli_mod.Settings(
            tenant_id="t1", database_url="postgresql://x/y",
            data_dir=str(tmp_path / "data"))))

    result = CliRunner().invoke(cli_mod.cli, ["serve", "--host", "127.0.0.1", "--port", "8100"])
    assert result.exit_code == 0, result.output
    assert seen.get("access_log") is False, seen
