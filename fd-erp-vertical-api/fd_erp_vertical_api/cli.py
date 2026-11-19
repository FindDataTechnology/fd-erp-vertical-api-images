"""CLI：serve / check / version（形态照 fd-erp-ledger-mcp cli.py）。"""
from __future__ import annotations

import json

import click

from . import __version__
from .config import Settings


@click.group()
@click.version_option(__version__, prog_name="fd-erp-vertical-api")
def cli() -> None:
    """ERP 行业垂直 REST 服务面（UE 订货台）。"""


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8100, type=int, show_default=True)
def serve(host: str, port: int) -> None:
    """启动 REST 服务（uvicorn；每进程绑定 ERP_TENANT_ID 唯一租户）。"""
    import uvicorn

    settings = Settings.from_env()
    if settings.kernel_transport == "mcp" and not settings.kernel_url:
        raise click.ClickException("ERP_KERNEL_TRANSPORT=mcp 需设置 ERP_KERNEL_URL")
    if settings.kernel_transport == "inprocess" and not settings.database_url:
        raise click.ClickException("InProcess 内核形态需设置 ERP_LEDGER_DATABASE_URL")
    click.echo(json.dumps({
        "ok": True, "tenant_id": settings.tenant_id,
        "kernel_transport": settings.kernel_transport,
        "base_currency": settings.base_currency,
        "note": "DSN 等敏感值不回显（BYOK/零落库纪律）",
    }, ensure_ascii=False))
    from .app import create_app
    app = create_app(settings)
    # access_log=False：逐请求访问日志统一由 logs.py 的 JSON 行承担（默认只记失败）。
    # 保留 uvicorn 那份的话，每个请求会写两遍，而且探针每 10s 一次的 200 照样落盘——
    # 「errors 档 = 正常流量零日志行」就成了假话（2026-11-11 上产后实测发现）。
    uvicorn.run(app, host=host, port=port, access_log=False)


@cli.command("check")
def check() -> None:
    """自检：配置解析 + 页面集授权配置 + 错误码封闭集（不打印任何敏感值）。"""
    from .error_codes_selfcheck import assert_closed_catalog
    from .pagesets import KNOWN_GRANTS, KNOWN_PAGESET_KEYS, load_pageset_auth

    settings = Settings.from_env()
    auth = load_pageset_auth(inline_json=settings.pageset_auth_json,
                             file_path=settings.pageset_auth_file)
    catalog = assert_closed_catalog()
    click.echo(json.dumps({
        "ok": True, "version": __version__,
        "tenant_id": settings.tenant_id,
        "kernel_transport": settings.kernel_transport,
        "database_configured": bool(settings.database_url),
        "pageset_auth_keys": sorted(auth.grants_by_key.keys()),
        "known_pageset_keys": list(KNOWN_PAGESET_KEYS),
        "known_grants": sorted(KNOWN_GRANTS),
        "error_catalog": catalog,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
