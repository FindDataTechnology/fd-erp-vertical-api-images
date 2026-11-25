"""目录三态与幂等（spec `trade-discovery`）。

这组测试守的是**判据本身**，不是实现细节：三态必须两两不同、登记必须收敛、
损坏的目录不能伪装成空目录。
"""
from __future__ import annotations

import json

import pytest

from fd_shop_directory import Directory, DirectoryUnavailable, ShopEntry


def mk(tmp_path, **kw):
    return Directory(tmp_path / "dir.jsonl")


def entry(tenant="acme", **kw):
    base = dict(tenant=tenant, title="Acme 商店",
                public_base="https://erp.example.com/shop/acme",
                categories=["饮品", "食品"], keywords=["矿泉水", "苏打水"])
    base.update(kw)
    return ShopEntry(**base)


def test_register_then_search_hits(tmp_path):
    d = mk(tmp_path)
    assert d.register(entry())["action"] == "created"
    out = d.search(category="饮品")
    assert out["state"] == "matched" and out["matched_count"] == 1
    assert out["matches"][0]["tenant"] == "acme"
    assert out["matches"][0]["public_base"].endswith("/shop/acme")


def test_register_is_idempotent(tmp_path):
    d = mk(tmp_path)
    d.register(entry())
    assert d.register(entry())["action"] == "updated"
    assert len(d.entries()) == 1


def test_every_match_carries_source_shop(tmp_path):
    d = mk(tmp_path)
    d.register(entry())
    d.register(entry(tenant="beta", title="Beta", public_base="https://b/shop/beta",
                     categories=["饮品"], keywords=["咖啡"]))
    out = d.search(category="饮品")
    assert out["matched_count"] == 2
    assert {m["tenant"] for m in out["matches"]} == {"acme", "beta"}
    assert all(m["public_base"] for m in out["matches"])


def test_unregistered_is_not_no_match(tmp_path):
    d = mk(tmp_path)
    d.register(entry())
    miss = d.search(tenant="ghost")
    nomatch = d.search(category="不存在的类目")
    assert miss["state"] == "unregistered" and miss["ok"] is False
    assert nomatch["state"] == "no_match" and nomatch["ok"] is True
    assert miss["state"] != nomatch["state"]


def test_unavailable_is_not_empty(tmp_path):
    d = mk(tmp_path)
    d.register(entry())
    (tmp_path / "dir.jsonl").write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(DirectoryUnavailable):
        d.search(category="饮品")


def test_corrupt_line_does_not_silently_drop_shops(tmp_path):
    """半行损坏说明写入被打断；静默跳过会让被截掉的店凭空消失。"""
    d = mk(tmp_path)
    d.register(entry())
    p = tmp_path / "dir.jsonl"
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"schema": "shop-directory/v1", "entry": {"tena')
    with pytest.raises(DirectoryUnavailable):
        d.entries()


def test_revoke_hides_from_search_but_shop_itself_unaffected(tmp_path):
    d = mk(tmp_path)
    d.register(entry())
    assert d.revoke("acme")["action"] == "revoked"
    assert d.search(category="饮品")["matched_count"] == 0
    # 目录撤回不改店面地址本身——调用方仍可用原地址直连（旁路语义）。
    assert d.search(tenant="acme")["state"] == "unregistered"


def test_revoke_unknown_is_reported(tmp_path):
    assert mk(tmp_path).revoke("ghost")["action"] == "not_registered"


def test_missing_file_reads_as_empty(tmp_path):
    """没有文件 = 空目录（第一次部署的正常状态），与「损坏」不同。"""
    assert mk(tmp_path).entries() == []


def test_no_price_or_stock_fields_stored(tmp_path):
    d = mk(tmp_path)
    d.register(entry())
    raw = json.loads((tmp_path / "dir.jsonl").read_text(encoding="utf-8").splitlines()[0])
    body = raw["entry"]
    for banned in ("price", "unit_price", "stock", "inventory", "qty"):
        assert banned not in body


def test_search_by_keyword_is_case_folded(tmp_path):
    d = mk(tmp_path)
    d.register(entry(keywords=["Mineral Water"]))
    assert d.search(q="mineral")["matched_count"] == 1


def test_region_filter(tmp_path):
    d = mk(tmp_path)
    d.register(entry(region="广州"))
    d.register(entry(tenant="beta", public_base="https://b", region="深圳"))
    assert d.search(region="广州")["matched_count"] == 1
