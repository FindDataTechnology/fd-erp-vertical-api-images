#!/usr/bin/env python3
"""未接通面（如实 501）机器可读清单生成 / 比对（optimize-erp-solution 3.7）。

为什么用 AST 而不是手抄：3.7 的验收是「代码里 501 端点集合与清单严格一致」。
手抄清单一定会腐化（新增 501 端点没人记得回填），所以清单由本脚本从源码派生，
比对也走同一条派生路径——任一侧多了/少了立即报红。

判据（全部来自源码，不 import 应用、不连数据库）：
- 路由：`@app.<method>("<path>")` 装饰器（FastAPI 注册面）；
- 501：处理器里 `raise ApiError("NOT_IMPLEMENTED", ...)`，或 `raise` 一个本模块内
  返回 `ApiError("NOT_IMPLEMENTED", ...)` 的辅助函数（faces.py 的
  `_not_implemented`）；
- planned_lane：上述 raise 的 `detail={"planned_lane": ...}`（ApiError 形态）或
  辅助函数的第 2 个位置参数（`_not_implemented` 形态），必须是可静态折叠的
  字符串；折叠不出来就有意报错——动态车道等于没法核对的挂账。

用法：
    python3 scripts/planned_surfaces.py                 # 打印当前派生结果（JSON）
    python3 scripts/planned_surfaces.py --write          # 重写 planned-surfaces.json
    python3 scripts/planned_surfaces.py --check          # 与清单比对，零差异退出 0
    python3 scripts/planned_surfaces.py --check --root /tmp/probe   # 比对临时副本

退出码：0 = 一致；1 = 有差异/派生失败（差异逐条打印，不静默）。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

PACKAGE = "fd_erp_vertical_api"
SOURCE_FILES = ("app.py", "faces.py")
BASELINE_NAME = "planned-surfaces.json"
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
GRANT_FUNCS = ("route", "se_route", "sp_route", "cp_route")
#: 路径段 → 所属能力（页面集；契约 40 条的四个页面集 + UE 共用）
CAPABILITY_BY_SEGMENT = {
    "ue": "supply-chain.ue",
    "se": "supply-chain.se",
    "sp": "supply-chain.sp",
    "cp": "supply-chain.cp",
    "fi": "supply-chain.fi",     # 内部财务面（无对外页集；见 INTERNAL-API.md）
}


def _fold_str(node: ast.AST | None) -> str | None:
    """静态折叠字符串常量（含隐式拼接与全常量 f-string）；折叠不出来返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                return None
        return "".join(parts)
    return None


def _api_error_code(call: ast.Call) -> str | None:
    """`ApiError("<code>", ...)` → code；其它调用返回 None。"""
    func = call.func
    name = func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None)
    if name != "ApiError" or not call.args:
        return None
    return _fold_str(call.args[0])


def _raises_not_implemented(fn: ast.AST, helpers: set[str]) -> ast.Raise | None:
    """处理器体内第一处「端点未实现」的 501 raise（直接或经辅助函数）。

    排除 except 块里的 raise：那是**条件降级**（如内核不可达时 501 + planned_lane），
    说明端点本体有实现，不能把这类降级算成未接通面。
    """
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    for node in ast.walk(fn):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        call = node.exc
        hit = _api_error_code(call) == "NOT_IMPLEMENTED" or (
            isinstance(call.func, ast.Name) and call.func.id in helpers)
        if not hit or _inside_except(node, parents):
            continue
        return node
    return None


def _inside_except(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, ast.ExceptHandler):
            return True
        cur = parents.get(cur)
    return False


def _own_nodes(fn: ast.AST) -> list[ast.AST]:
    """函数自身体（排除嵌套函数定义：嵌套处理器不算外层函数的实现）。"""
    out: list[ast.AST] = []
    stack = list(getattr(fn, "body", []))
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, FUNC_TYPES):
            continue  # 嵌套函数的 501 归它自己
        stack.extend(ast.iter_child_nodes(node))
    return out


def _returns_not_implemented(fn: ast.AST) -> bool:
    """本模块的「501 辅助函数」（faces._not_implemented 形态：return ApiError(...)）。"""
    return any(_api_error_code(n) == "NOT_IMPLEMENTED"
               for n in _own_nodes(fn) if isinstance(n, ast.Call))


def _planned_lane(rz: ast.Raise, helpers: set[str]) -> str | None:
    call = rz.exc
    assert isinstance(call, ast.Call)
    if _api_error_code(call) == "NOT_IMPLEMENTED":
        for kw in call.keywords:
            if kw.arg == "detail" and isinstance(kw.value, ast.Dict):
                for key, val in zip(kw.value.keys, kw.value.values):
                    if _fold_str(key) == "planned_lane":
                        return _fold_str(val)
        return None
    # 辅助函数形态：_not_implemented(message, planned_lane)
    if isinstance(call.func, ast.Name) and call.func.id in helpers:
        if len(call.args) >= 2:
            return _fold_str(call.args[1])
    return None


def _route_decorators(fn: ast.AST) -> list[tuple[str, str, int]]:
    """`@app.<method>("<path>")` → [(METHOD, path, lineno)]。"""
    out: list[tuple[str, str, int]] = []
    for dec in getattr(fn, "decorator_list", []):
        if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
            continue
        if dec.func.attr not in HTTP_METHODS:
            continue
        path = _fold_str(dec.args[0]) if dec.args else None
        if path is None:
            continue
        out.append((dec.func.attr.upper(), path, dec.lineno))
    return out


def _grant(fn: ast.AST) -> str | None:
    """从参数默认值 `Depends(route("grant.code"))` 提取权限码。"""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "Depends" or not node.args:
            continue
        inner = node.args[0]
        if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Name):
            continue
        if inner.func.id in GRANT_FUNCS and inner.args:
            return _fold_str(inner.args[0])
    return None


def _capability_and_domain(path: str) -> tuple[str, str]:
    parts = [p for p in path.split("/") if p]
    # /erp/supply-chain/v1/<pageset>/<domain>...
    if len(parts) >= 5 and parts[1] == "supply-chain":
        pageset = parts[3]
        domain = parts[4] if len(parts) > 4 else ""
        return CAPABILITY_BY_SEGMENT.get(pageset, pageset), domain
    return "unknown", ""


def derive(root: Path) -> dict:
    """从源码派生 501 端点清单（确定性：同样源码 → 逐字节同样的输出）。"""
    pkg = root / PACKAGE
    endpoints: list[dict] = []
    for name in SOURCE_FILES:
        path = pkg / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = [n for n in ast.walk(tree) if isinstance(n, FUNC_TYPES)]
        helpers = {
            fn.name for fn in functions
            if _returns_not_implemented(fn) and not _route_decorators(fn)
        }
        for fn in functions:
            routes = _route_decorators(fn)
            if not routes:
                continue
            rz = _raises_not_implemented(fn, helpers)
            if rz is None:
                continue
            lane = _planned_lane(rz, helpers)
            if not lane:
                raise SystemExit(
                    f"FAIL: {PACKAGE}/{name}:{rz.lineno} 的 501 端点 "
                    f"{routes[0][1]} 没有可静态折叠的 planned_lane"
                    "（动态车道无法核对，请改成字面量）")
            lane = lane.strip()
            for prefix in ("planned_lane:", "planned_lane："):
                if lane.startswith(prefix):
                    lane = lane[len(prefix):].strip()
            for method, route_path, lineno in routes:
                capability, domain = _capability_and_domain(route_path)
                endpoints.append({
                    "method": method,
                    "path": route_path,
                    "capability": capability,
                    "domain": domain,
                    "grant": _grant(fn),
                    "http_status": 501,
                    "error_code": "NOT_IMPLEMENTED",
                    "planned_lane": lane,
                    "source": f"{PACKAGE}/{name}:{lineno}",
                })
    endpoints.sort(key=lambda e: (e["path"], e["method"]))
    return {
        "schema": "fd-erp-vertical-api/planned-surfaces/v1",
        "generated_by": "scripts/planned_surfaces.py (AST 派生，勿手改)",
        "source_files": [f"{PACKAGE}/{n}" for n in SOURCE_FILES],
        "count": len(endpoints),
        "by_capability": _count_by(endpoints, "capability"),
        "endpoints": endpoints,
    }


def _count_by(endpoints: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in endpoints:
        out[e[key]] = out.get(e[key], 0) + 1
    return dict(sorted(out.items()))


def _key(entry: dict) -> tuple[str, str]:
    return entry["method"], entry["path"]


def diff(baseline: dict, live: dict) -> list[str]:
    """清单与源码派生结果的差异（逐条人话，退出码由调用方定）。"""
    want = {_key(e): e for e in baseline.get("endpoints", [])}
    got = {_key(e): e for e in live.get("endpoints", [])}
    problems: list[str] = []
    for k in sorted(got.keys() - want.keys()):
        problems.append(f"+ 代码有、清单缺：{k[0]} {k[1]}（planned_lane={got[k]['planned_lane'][:40]}…）")
    for k in sorted(want.keys() - got.keys()):
        problems.append(f"- 清单有、代码已无：{k[0]} {k[1]}（清单纯腐化，删掉这一条）")
    for k in sorted(want.keys() & got.keys()):
        for field in ("capability", "domain", "grant", "http_status",
                      "error_code", "planned_lane"):
            if want[k].get(field) != got[k].get(field):
                problems.append(
                    f"~ 字段漂移 {k[0]} {k[1]} [{field}]："
                    f"清单={want[k].get(field)!r} 代码={got[k].get(field)!r}")
    if baseline.get("count") != live.get("count"):
        problems.append(f"~ count 不一致：清单={baseline.get('count')} 代码={live.get('count')}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="501 未接通面清单：派生 / 比对")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                    help="包父目录（默认本仓根）")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true", help="重写 planned-surfaces.json")
    group.add_argument("--check", action="store_true", help="与清单比对，零差异才 0")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    baseline_path = root / BASELINE_NAME
    live = derive(root)

    if args.write:
        baseline_path.write_text(
            json.dumps(live, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"written {baseline_path} ({live['count']} endpoints)")
        print("by_capability:", json.dumps(live["by_capability"], ensure_ascii=False))
        return 0

    if args.check:
        if not baseline_path.exists():
            print(f"FAIL: 清单不存在：{baseline_path}（先跑 --write）", file=sys.stderr)
            return 1
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        problems = diff(baseline, live)
        if problems:
            print(f"FAIL: 501 清单与代码不一致（{len(problems)} 处）：", file=sys.stderr)
            for p in problems:
                print("  " + p, file=sys.stderr)
            return 1
        print(f"OK: {live['count']} 个 501 端点与代码严格一致")
        print("by_capability:", json.dumps(live["by_capability"], ensure_ascii=False))
        return 0

    print(json.dumps(live, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
