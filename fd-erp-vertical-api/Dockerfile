# fd-erp-vertical-api — ERP 纵向 REST 服务面（serve-erp-vertical-api 1.1）
#
# 构建上下文约定：**上下文必须同时含两个同级包目录**（本包依赖的模板包不在 PyPI）：
#   <context>/fd-erp-vertical-templates/   ← 模板层（主数据/取价/编排/履约/报表）
#   <context>/fd-erp-vertical-api/         ← 本包
# 出片（⚠ 不能直接拿本仓或工作区当快照源：fd-build-snapshot 只吃 git 仓且必须显式 --ref，
# 而工作区根不是 git 仓；直接以本仓为源会报
# `failed to compute cache key: "/fd-erp-vertical-api/fd_erp_vertical_api": not found`）：
#   需先做「双包 vendor 快照源」——两个包并排 clone 进一个临时 git 仓，提交后出片。
#   完整命令与坑见 fd-infra-deploy/docs/erp-image-build.md。
#
# 纪律（docs/IMAGE-RELEASE.md）：上游源即默认（不引国内镜像源）、单层 <300MB、
# 非 root 运行；探针打 /health（见 fd_erp_vertical_api/app.py 的 HEALTH_PATHS）。

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 1) 模板层先装（独立一层：变更频率低于本包，重建可复用缓存）
COPY fd-erp-vertical-templates /opt/fd-erp-vertical-templates
RUN pip install --no-cache-dir /opt/fd-erp-vertical-templates

# 1b) **构建期**断言国别包真的随轮子装进来了（2026-11-15 事故的移位防线）。
#     `fd_erp_vertical_templates/localization.py` 用
#     `Path(__file__).resolve().parents[1] / "localization" / "cn"` 定位数据包：在源码树里
#     `parents[1]` 是仓根、数据就在那儿；装成 wheel 后 `parents[1]` 是 site-packages，
#     数据若没随包发布就只到 Pod 启动那一刻才炸——形态是 CrashLoop（6 次重启、
#     `ERP_CN_CHART … 国别包缺文件：manifest.json`），而构建期、单测、`pip install`
#     全都绿灯。这里在构建期就用包自己的加载器装一遍（`CnPack` 默认 verify_on_load，
#     连 manifest 的 sha256 一起校验），缺一个文件就让镜像构建失败。
#     两套准则都试：大表走的是 `accounts-large.json`，只试小表会漏掉半份数据。
RUN python -c "from fd_erp_vertical_templates.localization import CHART_LARGE, CnPack; \
small = CnPack(); large = CnPack(chart=CHART_LARGE); \
print('cn pack in image: small=%d large=%d tax=%d' % (len(small.accounts()), len(large.accounts()), len(small.tax_codes()))); \
assert len(small.accounts()) == 156 and len(large.accounts()) == 286 and len(small.tax_codes()) == 62, '国别包未随轮子装进镜像'"

# 2) 运行时依赖显式列出（**不要**依赖 `pip install .` 的依赖解析：本包 pyproject
#    把 fd-erp-vertical-templates 记为普通依赖，走解析会去 PyPI 找不存在的包）。
#    fastmcp 必须显式装：McpKernelClient 懒导入它，而它在模板包里只是 dev extra
#    —— 漏装的表现是 ERP_KERNEL_TRANSPORT=mcp 下第一次调用才 ImportError。
#    prometheus_client 同理（2026-11-10 实锤）：/metrics 的 observability.py 在模块顶层
#    import 它，而 pyproject 只把它列在 dev extra —— 漏装的表现是**整个进程启动即
#    ModuleNotFoundError**（比 fastmcp 更狠，不是懒加载），Pod 直接 CrashLoop。
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "fastmcp>=2" "prometheus_client>=0.20"

# 3) 本包（--no-deps：依赖已在上面装齐，只装包体与 CLI 入口）
COPY fd-erp-vertical-api/pyproject.toml fd-erp-vertical-api/README.md ./
COPY fd-erp-vertical-api/fd_erp_vertical_api ./fd_erp_vertical_api
RUN pip install --no-cache-dir --no-deps .

# 4) 非 root + 事件档目录（PVC 挂载点；serve-erp-vertical-api D2）
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data && chown -R appuser:appuser /app /data
USER appuser

ENV ERP_API_DATA_DIR=/data
EXPOSE 8100

# 探针只用标准库（slim 镜像没有 curl）；/health 免租户闸，否则 Pod 永远不 Ready
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=2)"]

CMD ["fd-erp-vertical-api", "serve", "--host", "0.0.0.0", "--port", "8100"]
