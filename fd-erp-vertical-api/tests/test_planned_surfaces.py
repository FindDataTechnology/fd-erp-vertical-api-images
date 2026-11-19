"""501 未接通面清单（planned-surfaces.json）与代码严格一致（optimize-erp-solution 3.7）。

清单由 `scripts/planned_surfaces.py` 从 AST 派生；这个用例是防腐化闸：
新增一个如实 501 端点却不回填清单 → 这里红。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "planned_surfaces.py"
BASELINE = REPO / "planned-surfaces.json"


def test_planned_surfaces_list_matches_code():
    r = subprocess.run([sys.executable, str(SCRIPT), "--check"],
                       capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "严格一致" in r.stdout


def test_planned_surfaces_baseline_shape():
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    # 总数不钉死：能力落地会让这份清单**变短**，钉死等于「实现反而让测试变红」。
    # 真正的不变量是「清单与 AST 派生严格一致」——那由 test_planned_surfaces_matches_code 守。
    assert data["count"] == len(data["endpoints"]) > 0
    # 逐条含路径 / 方法 / 规划车道 / 所属能力（3.7 的字段要求）
    for e in data["endpoints"]:
        assert e["path"].startswith("/erp/supply-chain/v1/")
        assert e["method"] in ("GET", "POST", "PUT", "PATCH", "DELETE")
        assert e["planned_lane"].strip()
        assert e["capability"] in ("supply-chain.ue", "supply-chain.se",
                                   "supply-chain.sp", "supply-chain.cp")
        assert e["http_status"] == 501
        assert e["source"].startswith("fd_erp_vertical_api/")


def test_planned_surfaces_detects_new_unregistered_501(tmp_path):
    """人为加一个 501 端点 → 比对必须报红（不是靠人抄，靠派生）。"""
    import shutil

    pkg = tmp_path / "fd_erp_vertical_api"
    shutil.copytree(REPO / "fd_erp_vertical_api", pkg,
                    ignore=shutil.ignore_patterns("__pycache__"))
    app = pkg / "app.py"
    probe = (
        "\n    @app.get(\"/erp/supply-chain/v1/ue/probe-501\")\n"
        "    async def probe_501(ctx: dict = Depends(route(\"catalog.read\"))):\n"
        "        raise ApiError(\"NOT_IMPLEMENTED\", \"探针\",\n"
        "                       detail={\"planned_lane\": \"planned_lane: 探针\"})\n"
    )
    app.write_text(app.read_text(encoding="utf-8") + probe, encoding="utf-8")
    shutil.copy(BASELINE, tmp_path / BASELINE.name)

    r = subprocess.run([sys.executable, str(SCRIPT), "--check",
                        "--root", str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 1, r.stdout
    assert "代码有、清单缺：GET /erp/supply-chain/v1/ue/probe-501" in r.stderr
