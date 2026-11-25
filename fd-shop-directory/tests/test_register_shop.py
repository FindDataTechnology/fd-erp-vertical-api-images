"""登记脚本：派生 vs 不登记（两条负例比正例更重要）。"""
from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "fd-erp" / "tools" / "register_shop.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("register_shop", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _H(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def do_GET(self):  # noqa: N802
        _H.seen.append(("GET", self.path))
        body = _H.routes.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        self._json(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        payload = self.rfile.read(n)
        _H.seen.append(("POST", self.path, payload.decode(), self.headers.get("x-directory-write-token")))
        _H.routes[self.path] = {"ok": True, "action": "created"}
        self._json({"ok": True, "action": "created"})

    def _json(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):  # 静音
        pass


@pytest.fixture
def server():
    _H.routes = {"/shop/acme/products": {"ok": True, "data": [
        {"productId": "SKU-1", "title": "矿泉水", "category": "饮品"},
        {"productId": "SKU-2", "title": "苏打水", "category": "饮品"},
        {"productId": "SKU-3", "title": "饼干", "category": "食品"},
        {"productId": "SKU-4", "title": "无类目商品"},
    ]}, "/directory/search?tenant=acme": {"ok": True, "state": "matched", "matched_count": 1}}
    _H.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_derives_categories_from_shop_not_handwritten(server):
    mod = load_tool()
    cats, kws, _ = mod.derive(server, "acme")
    assert set(cats) == {"饮品", "食品"}   # 去重，且不含空类目
    assert len(cats) == 2
    assert set(kws) == {"矿泉水", "苏打水", "饼干", "无类目商品"}


def test_register_posts_derived_payload(server):
    mod = load_tool()
    rc = mod.main(["--tenant", "acme", "--shop-api", server,
                   "--public-base", "https://erp/shop/acme/", "--directory", server,
                   "--token", "t"])
    assert rc == 0
    post = [s for s in _H.seen if s[0] == "POST"][0]
    body = json.loads(post[2])
    assert body["public_base"] == "https://erp/shop/acme"   # 尾部 / 已归一
    assert set(body["categories"]) == {"饮品", "食品"}
    assert post[3] == "t"


def test_shop_unreadable_does_not_register():
    """店面读不到 ⇒ 不登记，且不留半条空类目记录（负例，比正例重要）。"""
    mod = load_tool()
    # 一个必然连不上的地址
    rc = mod.main(["--tenant", "acme", "--shop-api", "http://127.0.0.1:1",
                   "--public-base", "https://x", "--directory", "http://127.0.0.1:1",
                   "--token", "t"])
    assert rc == 1


def test_missing_token_is_param_error():
    mod = load_tool()
    assert mod.main(["--tenant", "a", "--shop-api", "http://x",
                     "--public-base", "https://y", "--directory", "http://z"]) == 2


def test_dry_run_writes_nothing(server):
    mod = load_tool()
    _H.seen.clear()
    rc = mod.main(["--tenant", "acme", "--shop-api", server,
                   "--public-base", "https://erp/shop/acme",
                   "--directory", server, "--dry-run"])
    assert rc == 0
    assert not [s for s in _H.seen if s[0] == "POST"]
