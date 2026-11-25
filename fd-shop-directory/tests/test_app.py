"""HTTP 面：写面守卫、三态响应码、以及「目录 503 不是 200 空数组」。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fd_shop_directory.app import WRITE_TOKEN_ENV, create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(WRITE_TOKEN_ENV, "t-secret")
    return TestClient(create_app(str(tmp_path / "dir.jsonl")))


def reg(client, **kw):
    body = {"tenant": "acme", "title": "Acme",
            "public_base": "https://erp/shop/acme",
            "categories": ["饮品"], "keywords": ["矿泉水"]}
    body.update(kw)
    return client.post("/directory/shops", json=body,
                       headers={"x-directory-write-token": "t-secret"})


def test_write_requires_token(client):
    r = client.post("/directory/shops", json={"tenant": "acme", "public_base": "https://x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "WRITE_DENIED"


def test_write_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(WRITE_TOKEN_ENV, raising=False)
    c = TestClient(create_app(str(tmp_path / "d.jsonl")))
    r = c.post("/directory/shops", json={"tenant": "a", "public_base": "https://x"})
    assert r.status_code == 403
    assert "未启用" in r.json()["error"]["message"]


def test_register_and_search_roundtrip(client):
    assert reg(client).status_code == 200
    out = client.get("/directory/search", params={"category": "饮品"}).json()
    assert out["state"] == "matched" and out["matches"][0]["tenant"] == "acme"


def test_search_unregistered_shape(client):
    reg(client)
    out = client.get("/directory/search", params={"tenant": "ghost"}).json()
    assert out["ok"] is False and out["state"] == "unregistered"


def test_search_unavailable_is_503(tmp_path, monkeypatch):
    monkeypatch.setenv(WRITE_TOKEN_ENV, "t")
    p = tmp_path / "d.jsonl"
    c = TestClient(create_app(str(p)))
    c.post("/directory/shops", json={"tenant": "a", "public_base": "https://x"},
           headers={"x-directory-write-token": "t"})
    p.write_text("{{{broken", encoding="utf-8")
    r = c.get("/directory/search", params={"q": "x"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "DIRECTORY_UNAVAILABLE"


def test_missing_required_fields(client):
    r = client.post("/directory/shops", json={"tenant": "acme"},
                    headers={"x-directory-write-token": "t-secret"})
    assert r.status_code == 400 and "public_base" in r.json()["error"]["detail"]["missing"]


def test_health(client):
    assert client.get("/health").json()["ok"] is True
