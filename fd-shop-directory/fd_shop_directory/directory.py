"""店目录：跨店发现的唯一入口（change establish-ai-native-trade-discovery 1.1-1.4）。

**为什么是独立进程**：每家店是绑定单租户的进程（一进程一租户），它只知道自己的数据。
跨店检索必须有一个不绑定单租户的地方——但那个地方也只能存**指针**，不能存商品数据，
否则隔离前提（只有店进程能读自己的主档）就被目录绕过了。

**目录存什么**（`ShopEntry`）：店面标识、展示名、公开读面地址、类目、关键词、地区。
**目录不存什么**：价格、库存、商品详情。这些只有店面自己的公开读面是真源。

**三态必须可区分**（spec `trade-discovery`）：未收录 / 无匹配 / 目录不可读。
前两者是 200 但结果体不同（`unregistered` vs `matched_count=0`），后者是 503。
用空数组同时表示三者，等于让调用方把「网络里没人卖」当成「我查不到」。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "shop-directory/v1"


class DirectoryUnavailable(RuntimeError):
    """目录不可读：数据根打不开/JSONL 损坏。**必须与「没有结果」区分**。"""


@dataclass
class ShopEntry:
    """一家店的登记项：**只有指针**，没有任何商品数据。"""

    tenant: str
    title: str
    public_base: str
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    region: str = ""
    registered_at: float = 0.0
    revoked_at: float = 0.0

    def as_row(self) -> dict[str, Any]:
        return asdict(self)

    def searchable(self) -> bool:
        return self.revoked_at == 0.0


def _norm(s: Any) -> str:
    return str(s or "").strip().casefold()


class Directory:
    """JSONL 指针表 + 原子写。全量驻内存（`shortcut` 见设计 D1：约一万家店以内）。

    并发：单进程 NodePort 部署，读多写少；写走一把锁 + 临时文件 rename，
    读者永远看到完整文件（原子写是这条链唯一要守的东西）。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    # ---- 读 ----

    def _load(self) -> list[ShopEntry]:
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DirectoryUnavailable(f"目录不可读：{exc}") from exc
        out: list[ShopEntry] = []
        for n, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                # 损坏的 JSONL 不等于「目录为空」——半行损坏说明写入被打断，
                # 静默跳过会让被截掉的那些店凭空消失。
                raise DirectoryUnavailable(
                    f"目录第 {n} 行损坏（{exc.msg}）：拒绝以残缺目录回答检索") from exc
            if not isinstance(rec, dict) or rec.get("schema") != SCHEMA:
                raise DirectoryUnavailable(f"目录第 {n} 行 schema 非法")
            body = rec.get("entry") or {}
            try:
                out.append(ShopEntry(**body))
            except TypeError as exc:
                raise DirectoryUnavailable(
                    f"目录第 {n} 行字段非法：{exc}") from exc
        return out

    def entries(self) -> list[ShopEntry]:
        return [e for e in self._load() if e.searchable()]

    # ---- 写 ----

    def _rewrite(self, entries: Iterable[ShopEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".dir-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for e in entries:
                    fh.write(json.dumps({"schema": SCHEMA, "entry": e.as_row()},
                                        ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    def register(self, entry: ShopEntry) -> dict[str, Any]:
        """登记/更新一家店。同一 `tenant` 收敛为一条（幂等）。

        返回 `created` 或 `updated`——调用方据此区分「新收录」与「刷新指针」。
        """
        if not entry.tenant or not entry.public_base:
            raise ValueError("tenant 与 public_base 必填")
        with self._lock:
            current = self._load()
            entry.registered_at = time.time()
            hit = next((i for i, e in enumerate(current) if e.tenant == entry.tenant), None)
            if hit is None:
                current.append(entry)
                action = "created"
            else:
                current[hit] = entry
                action = "updated"
            self._rewrite(current)
        return {"tenant": entry.tenant, "action": action}

    def revoke(self, tenant: str) -> dict[str, Any]:
        """撤回登记：该店从检索中消失，**但店面自身不受影响**（目录是旁路）。"""
        with self._lock:
            current = self._load()
            hit = next((i for i, e in enumerate(current) if e.tenant == tenant), None)
            if hit is None:
                return {"tenant": tenant, "action": "not_registered"}
            if current[hit].revoked_at:
                return {"tenant": tenant, "action": "already_revoked"}
            current[hit].revoked_at = time.time()
            self._rewrite(current)
        return {"tenant": tenant, "action": "revoked"}

    # ---- 检索 ----

    def search(self, *, category: str = "", q: str = "", region: str = "",
               tenant: str = "") -> dict[str, Any]:
        """类目精确 + 关键词子串（大小写归一），不做语义检索（设计 D3）。

        返回体区分三态：
          - `unregistered`：查询指定了 tenant 但目录里没有它（未收录）；
          - `matched_count=0` 且 `searched_shops>0`：目录里有店但没有匹配（无匹配）；
          - 抛 `DirectoryUnavailable`：目录不可读（不可用）。
        """
        entries = self.entries()
        if tenant:
            hit = next((e for e in entries if e.tenant == tenant), None)
            if hit is None:
                return {"ok": False, "state": "unregistered", "tenant": tenant,
                        "note": "该店未登记进目录。未收录 ≠ 不存在：请直接访问其公开读面核实。",
                        "searched_shops": len(entries), "matches": []}
        cat = _norm(category)
        needle = _norm(q)
        reg = _norm(region)
        matches: list[dict[str, Any]] = []
        for e in entries:
            if tenant and e.tenant != tenant:
                continue
            if reg and _norm(e.region) != reg:
                continue
            cats = [_norm(c) for c in e.categories]
            if cat and cat not in cats:
                continue
            hay = [_norm(k) for k in e.keywords] + cats + [_norm(e.title)]
            if needle and not any(needle in h for h in hay):
                continue
            matches.append({
                "tenant": e.tenant, "title": e.title, "public_base": e.public_base,
                "categories": e.categories, "region": e.region,
                "matched_on": ("category" if cat else ("keyword" if needle else "all")),
            })
        return {"ok": True, "state": "matched" if matches else "no_match",
                "searched_shops": len(entries), "matched_count": len(matches),
                "matches": matches,
                "note": "" if matches else "目录里有店，但没有匹配该条件的店（与「未收录」不同）。"}
