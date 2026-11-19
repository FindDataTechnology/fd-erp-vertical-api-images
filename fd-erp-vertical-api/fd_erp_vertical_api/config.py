"""进程配置：一个 cell 绑定一个租户（照内核 Settings 语义）。

- ERP_TENANT_ID            本进程绑定的唯一租户（缺省 default）
- ERP_LEDGER_DATABASE_URL  内核库 DSN（只透传给内核客户端，本包不直连）
- ERP_BASE_CURRENCY        记账本位币（缺省 CNY）
- ERP_API_DATA_DIR         模板层 JSONL 事件档目录（缺省 ./data）
- ERP_KERNEL_TRANSPORT     inprocess（缺省）| mcp
- ERP_KERNEL_URL           transport=mcp 时的内核 MCP 工具面地址
- ERP_KERNEL_TOKEN         内核 bearer 门的钥匙（与内核 MCP_BEARER_TOKEN 同值）；
                           内核开了门而这里为空 ⇒ 每个碰账本的端点 401→500
- ERP_PAGESET_AUTH         页面集授权配置（内联 JSON），优先于文件
- ERP_PAGESET_AUTH_FILE    页面集授权配置 JSON 文件路径（fail-closed）
- ERP_FINANCE_STAFF        财务后台任职名单（内联 JSON），优先于文件；
                           缺失/不可解析/名单为空 ⇒ 没有人能进财务后台
- ERP_FINANCE_STAFF_FILE   财务任职名单 JSON 文件路径
- ERP_WMS_SNAPSHOT_FILE    WMS 库存快照 JSON 文件路径（缺省无快照源，如实降级）
- ERP_CN_CHART             中国国别包所依据的准则文件名（accounts-small.json 缺省 /
                           accounts-large.json）；非法值装配即拒，不静默退回

BYOK 纪律：外部系统凭证只经环境变量注入，本模块不读取、不缓存、不落库任何
凭证明文；DSN 等敏感值绝不进日志（本模块无日志输出）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value not in ("", None) else default


def _int_env(name: str, default: int) -> int:
    """整数环境变量：非法值退回默认（配置错误不得让服务起不来）。"""
    raw = _env(name)
    try:
        return max(1, int(raw)) if raw is not None else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """一个服务进程 = 恰好一个租户（内核 tenancy 语义对齐）。

    database_url 带 repr=False：DSN 含口令，repr/日志零外泄（BYOK/零落库纪律）。
    """

    tenant_id: str = "default"
    database_url: str | None = field(default=None, repr=False)
    base_currency: str = "CNY"
    data_dir: str = "data"
    kernel_transport: str = "inprocess"  # inprocess | mcp
    kernel_url: str | None = field(default=None, repr=False)
    #: 内核 bearer 门的钥匙。repr=False 与 kernel_url 同档：它是凭据。
    kernel_token: str | None = field(default=None, repr=False)
    pageset_auth_json: str | None = field(default=None, repr=False)
    pageset_auth_file: str | None = field(default=None, repr=False)
    finance_staff_json: str | None = field(default=None, repr=False)
    finance_staff_file: str | None = field(default=None, repr=False)
    wms_snapshot_file: str | None = None
    #: 信用占用失效时点上限（天），与内核同口径（change: close-orphan-credit-reservations）。
    #: 内核缺该字段时自己回退到同一默认值；这里显式声明是为了让部署侧可配。
    credit_reservation_max_days: int = 90
    #: 中国国别包所依据的准则（change: add-cn-chart-selection-wiring）。
    #: 取 localization/cn 下的**准则文件名**而不是自造 small|large 别名：CnPack 的词汇表
    #: 就是文件名，多一层别名映射就多一处「映射写错」的可能。非法值由装配层拒。
    cn_chart: str = "accounts-small.json"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            tenant_id=_env("ERP_TENANT_ID", "default") or "default",
            database_url=_env("ERP_LEDGER_DATABASE_URL"),
            base_currency=_env("ERP_BASE_CURRENCY", "CNY") or "CNY",
            data_dir=_env("ERP_API_DATA_DIR", "data") or "data",
            kernel_transport=(_env("ERP_KERNEL_TRANSPORT", "inprocess")
                              or "inprocess"),
            kernel_url=_env("ERP_KERNEL_URL"),
            kernel_token=_env("ERP_KERNEL_TOKEN"),
            pageset_auth_json=_env("ERP_PAGESET_AUTH"),
            pageset_auth_file=_env("ERP_PAGESET_AUTH_FILE"),
            finance_staff_json=_env("ERP_FINANCE_STAFF"),
            finance_staff_file=_env("ERP_FINANCE_STAFF_FILE"),
            wms_snapshot_file=_env("ERP_WMS_SNAPSHOT_FILE"),
            credit_reservation_max_days=_int_env("ERP_CREDIT_RESERVATION_MAX_DAYS", 90),
            cn_chart=_env("ERP_CN_CHART", "accounts-small.json")
            or "accounts-small.json",
        )

    def data_path(self) -> Path:
        return Path(self.data_dir)
