"""`fd-shop-directory serve`：起来一个目录进程（环境变量全部可覆盖）。"""
from __future__ import annotations

import argparse
import os

from .app import create_app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fd-shop-directory")
    ap.add_argument("command", nargs="?", default="serve", choices=["serve"])
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8099")))
    ap.add_argument("--data", default=os.environ.get("SHOP_DIRECTORY_DATA"),
                    help="目录 JSONL 路径（缺省 /data/shop-directory.jsonl）")
    a = ap.parse_args(argv)
    import uvicorn

    uvicorn.run(create_app(a.data), host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
