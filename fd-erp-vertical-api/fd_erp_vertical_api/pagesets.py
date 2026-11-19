"""页面集授权（GET /erp/supply-chain/v1/pagesets 数据源 + 端点闸）。

与 paas 客户端精确对齐（paas/miniapp/src/pagesets/authorize.ts parsePagesetAuth）：
- 线格式：{"pagesets": [{"key": "supply-chain.ue", "grants": ["catalog.read", ...]}, ...]}
- 客户端严格解析：key 必须在其 KNOWN_KEYS 白名单（supply-chain.ue/se/sp/cp）、
  grants 必须全为字符串，任一元素形态非法 → 整个 payload 作废（fail-closed）。
  因此本端点只可能输出白名单内的 key 与封闭集内的 grant 码 —— 绝不输出会让
  客户端整体作废的形态。

授权配置源（本包简单配置面，fail-closed）：
- Settings.pageset_auth_json（内联 JSON）优先；
- 否则 Settings.pageset_auth_file（JSON 文件，形如 {"supply-chain.ue": ["catalog.read", ...]}）；
- 缺失/不可解析/形态非法 → 空授权（无页面集可见，所有 /ue/* 端点 403）。
fail-closed 双向：服务端配置缺失 → 不可见；客户端解析失败 → 不渲染（paas 侧）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import ApiError

#: **对外发布**的页集 key（与 paas/miniapp/src/pagesets/authorize.ts KNOWN_KEYS 一字不差；
#: GET /pagesets 的输出只会遍历这一层。多吐一个客户端不认识的 key，客户端会整体
#: fail-closed 作废——连带把四个业务页集一起打掉，所以这一层不许随手加。
PUBLISHED_PAGESET_KEYS: tuple[str, ...] = (
    "supply-chain.ue",
    "supply-chain.se",
    "supply-chain.sp",
    "supply-chain.cp",
)

#: **内部**页集 key：只用于服务端端点闸，**绝不进 /pagesets 载荷**。
#: 存在的理由：公司自己的财报属于财务后台（桌面/工具），不属于客户订货小程序；
#: 但它同样需要「页集授权 + grant + 身份头」三件齐备，而不是只凭租户头裸开。
#:
#: `supply-chain.wh`（2026-11-16，change: add-inventory-operations-lane 任务 3.1）同族：
#: 仓管作业不属于四个对外角色中的任何一个——UE 是客户、SE 是业务员、SP 是供应商、
#: CP 是承运商，把「按仓收发货与盘点」套上其中任何一个都是错的人。走内部页集而不是
#: 新开对外页集，是因为对外页集 key 与 grant 是**封闭集**，镜像在
#: `paas/miniapp/src/pagesets/registry.ts`：多吐一个客户端不认识的 key，客户端
#: `parsePagesetAuth` 会整包作废（连带四个业务页集一起打掉）。内部页集只在服务端守门、
#: 不进载荷，所以这条车道不必改 paas 那棵树，也不动客户端契约。
INTERNAL_PAGESET_KEYS: tuple[str, ...] = (
    "supply-chain.fi",
    "supply-chain.wh",
)

#: 契约锚：与 paas 客户端 `KNOWN_KEYS` **一字不差**（`tests/test_authz_contract.py` 逐项对拍）。
#: 只含对外发布层——内部页集进这个元组会立刻让那条跨仓契约闸变红，那是对的：
#: 客户端不认识它，服务端就不该把它算进「客户端契约」。
KNOWN_PAGESET_KEYS: tuple[str, ...] = PUBLISHED_PAGESET_KEYS

#: 服务端实际接受的全部页集 key（对外 + 内部）。授权配置按这一层收敛。
ALL_PAGESET_KEYS: tuple[str, ...] = PUBLISHED_PAGESET_KEYS + INTERNAL_PAGESET_KEYS

#: grant 码封闭集（paas/miniapp/src/pagesets/registry.ts 四页面集 requiredGrants
#: 与页级 requiredGrants 的并集；配置中出现集外码一律丢弃 —— 封闭集纪律）
KNOWN_GRANTS: frozenset[str] = frozenset({
    # UE 订货台（supply-chain.ue）
    "catalog.read", "order.write", "statement.confirm",
    "invoice.write", "aftersale.write", "ai.chat",
    # SE 业务员（supply-chain.se）
    "crm.read", "crm.visit.write", "order.write.proxy",
    "alert.handle", "statement.confirm.proxy",
    # SP 供应商（supply-chain.sp）
    "po.read", "po.accept", "delivery.feedback",
    "statement.read", "invoice.upload",
    # CP 承运商（supply-chain.cp）
    "waybill.read", "waybill.accept", "dock.reserve",
    "pickup.checkin", "pod.report", "freight.statement.read",
})

#: **内部** grant 码（只服务内部页集，不进客户端契约）。
#: 与页集 key 同样分两层：`KNOWN_GRANTS` 是契约锚（等于 paas 四个页集 requiredGrants 并集），
#: 内部面的权限码放这里，`ALL_GRANTS` 才是服务端实际接受的集合。
INTERNAL_GRANTS: frozenset[str] = frozenset({
    # FI 财务后台（supply-chain.fi）：读三张法定报表与增值税申报口径
    "finance.read",
    # FI 财务后台：登记申报数、发票与过账（写）。读权限 MUST NOT 覆盖它。
    "finance.write",
    # FI 财务后台：确认/退回提案（拍板）。读与写都 MUST NOT 覆盖它——
    # 「能登记一张票」与「能让这笔分录成为事实」是两件事，后者是账本不可逆的一步。
    "finance.confirm",
    # WH 仓管作业（supply-chain.wh）：看单据与余额（数量面唯一出处）
    "inventory.read",
    # WH 仓管作业：提交入库/出库/调拨/盘点单（落**拟稿**事实，不投影、不过账）
    "inventory.write",
    # WH 仓管作业：拍板（过确认门 + 投影 + 过账）。读与写都 MUST NOT 覆盖它——
    # 「能开一张单」与「能让这批货成为账面事实」是两件事，后者动的是对象余额。
    "inventory.confirm",
})

#: 服务端实际接受的全部 grant 码（契约锚 + 内部）；授权配置按这一层收敛。
ALL_GRANTS: frozenset[str] = KNOWN_GRANTS | INTERNAL_GRANTS


@dataclass(frozen=True)
class PagesetAuth:
    """租户页面集授权：key → 已授权 grant 码集（只含白名单 key 与封闭集 grant）。"""

    grants_by_key: dict[str, frozenset[str]]

    def grants_of(self, pageset_key: str) -> frozenset[str] | None:
        """未授权 key 返回 None（不可见）；授权返回 grant 集（可为空集）。"""
        return self.grants_by_key.get(pageset_key)

    def wire_payload(self) -> dict[str, Any]:
        """GET /pagesets 响应 data：与 paas parsePagesetAuth 逐字段对齐。"""
        # 只遍历对外发布层：内部页集（supply-chain.fi）即使已授权也不能出现在这里，
        # 否则客户端解析遇到未知 key 会整包作废。
        return {"pagesets": [
            {"key": key, "grants": sorted(self.grants_by_key[key])}
            for key in PUBLISHED_PAGESET_KEYS if key in self.grants_by_key
        ]}


def _normalize(raw: Any) -> PagesetAuth:
    """配置 → PagesetAuth：非白名单 key、封闭集外 grant、非字符串一律丢弃。"""
    out: dict[str, frozenset[str]] = {}
    if not isinstance(raw, dict):
        return PagesetAuth({})
    for key, grants in raw.items():
        if key not in ALL_PAGESET_KEYS or not isinstance(grants, list):
            continue
        cleaned = sorted({g for g in grants if isinstance(g, str) and g in ALL_GRANTS})
        out[key] = frozenset(cleaned)
    return PagesetAuth(out)


def load_pageset_auth(*, inline_json: str | None = None,
                      file_path: str | None = None) -> PagesetAuth:
    """加载页面集授权配置（fail-closed：任何缺失/损坏 → 空授权，绝不猜默认）。"""
    payload: Any = None
    if inline_json:
        try:
            payload = json.loads(inline_json)
        except json.JSONDecodeError:
            return PagesetAuth({})
    elif file_path:
        try:
            with open(file_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return PagesetAuth({})
    return _normalize(payload)


def require_grant(auth: PagesetAuth, pageset_key: str, grant: str) -> frozenset[str]:
    """端点闸：页面集未授权 → PAGESET_FORBIDDEN；已授权缺 grant → GRANT_REQUIRED。

    返回该页面集授权 grant 集（调用方可继续做页级细粒度判断）。
    """
    grants = auth.grants_of(pageset_key)
    if grants is None:
        raise ApiError("PAGESET_FORBIDDEN",
                       f"当前租户未授权页面集：{pageset_key}",
                       detail={"pageset_key": pageset_key})
    if grant not in grants:
        raise ApiError("GRANT_REQUIRED",
                       f"当前账号缺少所需权限码：{grant}",
                       detail={"pageset_key": pageset_key, "grant": grant})
    return grants


# ---- 财务后台的人员授权（change: add-fi-staff-authorization）----
#
# 为什么需要这一层：`require_grant` 判的是**部署**（这个页面集允许哪些 grant），
# 它不认识调用者。fi 面又没有任何数据范围（财务就是看全公司的账），所以
# 「谁能碰财务」没有第二道闸——补上 `supply-chain.fi` 的授权等于把财务后台
# 发给所有已登录用户，而留痕标签会让他们看起来像是有授权的经办人。

#: 财务授权三档，强度递增（更强的档包含更弱的档）。
FINANCE_GRANT_TIERS: tuple[str, ...] = (
    "finance.read", "finance.write", "finance.confirm",
)

#: 程序身份：REST 面上不得代表财务操作。与各车道的 `PROGRAM_ACTORS` 同一组名字。
#: 这一条**不靠名单放行**：名单是「哪些人」，程序不是人；将来要接自动化月结，
#: 得先有服务账号机制（有独立的凭据与审计），而不是把 `asset-engine` 写进人的名单。
#: 取各车道 `PROGRAM_ACTORS` 的并集——两处口径必须一致，否则「谁是程序」在 REST 层与
#: 车道层各有一份答案（`asset-engine` 设门槛会在车道被拒、在 REST 层却放行）。
PROGRAM_IDENTITIES: frozenset[str] = frozenset({
    "system", "agent",
    "asset-engine", "billing-engine", "posting-engine", "bad-debt-engine",
    "cost-engine",
    # 调汇车道的默认落款（`FxRevaluation.revalue(submitted_by=...)` 的缺省值）与它的
    # 引擎名。漏进来会被「不在任职名单」拦成 403「缺权限码」，而正确答案是 400
    # 「须人工经办」——**两种拒绝指向的处置完全不同**：前者让人去配名单，后者让人换人。
    "fx-engine", "fx-revaluation",
})

#: 配置里可用的短键（`{"read": [...]}`）到全名的映射。
_FINANCE_TIER_ALIASES: dict[str, str] = {
    "read": "finance.read", "write": "finance.write", "confirm": "finance.confirm",
}


@dataclass(frozen=True)
class FinanceStaff:
    """谁能碰财务后台：grant 档 → 允许的人员标识集。

    空表 = 没有人（fail-closed）。「没配置」与「全都行」是两件事，这里只认前者。
    """

    by_grant: dict[str, frozenset[str]]

    @property
    def configured(self) -> bool:
        return any(self.by_grant.get(t) for t in FINANCE_GRANT_TIERS)

    def allows(self, grant: str, who: str) -> bool:
        """该人员是否可做 `grant` 这一档（单调闭合：更强的档也放行更弱的）。"""
        person = str(who or "").strip().lower()
        if not person or grant not in FINANCE_GRANT_TIERS:
            return False
        start = FINANCE_GRANT_TIERS.index(grant)
        return any(person in self.by_grant.get(tier, frozenset())
                   for tier in FINANCE_GRANT_TIERS[start:])


def _normalize_finance_staff(raw: Any) -> FinanceStaff:
    """配置 → FinanceStaff：只认三档、只认非空字符串，其余丢弃（不猜）。"""
    out: dict[str, frozenset[str]] = {}
    if not isinstance(raw, dict):
        return FinanceStaff({})
    for key, people in raw.items():
        tier = _FINANCE_TIER_ALIASES.get(str(key).strip().lower(),
                                         str(key).strip().lower())
        if tier not in FINANCE_GRANT_TIERS or not isinstance(people, list):
            continue
        out[tier] = frozenset(
            str(p).strip().lower() for p in people
            if isinstance(p, str) and str(p).strip())
    return FinanceStaff(out)


def load_finance_staff(*, inline_json: str | None = None,
                       file_path: str | None = None) -> FinanceStaff:
    """加载财务任职名单（fail-closed：缺失/损坏 → 空名单，没有人通过）。"""
    payload: Any = None
    if inline_json:
        try:
            payload = json.loads(inline_json)
        except json.JSONDecodeError:
            return FinanceStaff({})
    elif file_path:
        try:
            with open(file_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return FinanceStaff({})
    return _normalize_finance_staff(payload)


def require_finance_staff(staff: FinanceStaff, grant: str, who: str) -> None:
    """人员闸：不在名单 → `STAFF_NOT_AUTHORIZED`。

    拒绝消息分两种，因为处置方向不同：整体没配置要去部署侧登记，
    个人不在名单要找管理员加名字。
    """
    if staff.allows(grant, who):
        return
    if not staff.configured:
        raise ApiError(
            "STAFF_NOT_AUTHORIZED",
            "财务后台未配置任职名单（ERP_FINANCE_STAFF）——对所有人关闭",
            detail={"grant": grant, "configured": False,
                    "fix": "部署侧设置 ERP_FINANCE_STAFF="
                           '{"read":["..."],"write":["..."],"confirm":["..."]}'})
    raise ApiError(
        "STAFF_NOT_AUTHORIZED",
        f"{str(who or '（空）').strip() or '（空）'} 不在财务 {grant} 名单内",
        detail={"grant": grant, "configured": True,
                "fix": f"由管理员把该人员加入 ERP_FINANCE_STAFF.{grant}"})
