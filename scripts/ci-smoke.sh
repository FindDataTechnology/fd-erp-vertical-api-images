#!/usr/bin/env bash
# 仓内镜像冒烟门（池的 image.yml 优先跑本脚本，跑在**构建后、推送前**）。
#
# 为什么需要它：本品无 env 直接 `docker run` 必然退出——`cli.py serve` 在
# InProcess 形态下要求 `ERP_LEDGER_DATABASE_URL`，而池的默认 boot 门不给 env
# ⇒ 结构性假失败（2026-11-14T21:55 与 2026-11-15T09:55 两次 failure）。
# 上一轮绕法是给池设 `SMOKE_MODE=skip`，那等于**这品从来没有冒烟**：
# 池的 generic gate 恰好在 2026-10-17 放走过一个包装缺件的镜像（见 paas 同名脚本），
# 而本品的真实故障类正是包装缺件（模板包/国别包 COPY 漏、运行时依赖漏装）。
#
# 判据（三条，都是本品真踩过或结构上会踩的）：
#   1. 容器起来并**保持运行**（无 env 直接退 = 配置面缺件；缺包装件 = ImportError 退）
#   2. `/health` 返 200（应用真装配完成，不是只有入口进程活着）
#   3. `/health` 的 `kernel_transport` == mcp（本次注入的形态；说明 env 被真读到，
#      而不是读了某个默认值）
# 不连内核：`ERP_KERNEL_URL` 指一个必然拒绝的本地端口——/health 不碰内核，
# 真连上反而会把「内核挂了」混进「镜像坏了」。
#
# 用法：bash scripts/ci-smoke.sh <registry>/<ns>/<image>:<tag>

set -uo pipefail

IMAGE="${1:?usage: ci-smoke.sh <image:tag>}"
NAME="erp-api-ci-smoke"
PORT=8100

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "smoke: image=$IMAGE"
cleanup

docker run -d --name "$NAME" \
  -e ERP_TENANT_ID=smoke \
  -e ERP_KERNEL_TRANSPORT=mcp \
  -e ERP_KERNEL_URL=http://127.0.0.1:9/mcp \
  "$IMAGE" >/dev/null || { echo "SMOKE FAILED — container refused to start"; exit 1; }

fail() {
  echo "SMOKE FAILED — $1"
  echo "---- container logs (tail 60) ----"
  docker logs "$NAME" 2>&1 | tail -60
  exit 1
}

body=""
for i in $(seq 1 30); do
  sleep 2
  body=$(docker exec "$NAME" python -c \
    "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:${PORT}/health',timeout=2).read().decode())" \
    2>/dev/null) && break
  [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ] \
    || fail "container exited after ~$((i * 2))s（无 env 直接退 = 配置缺件；ImportError = 包装缺件）"
  body=""
done

[ -n "$body" ] || fail "/health 未在 60s 内返回 200"
echo "smoke: /health -> $body"

echo "$body" | grep -q '"kernel_transport": *"mcp"' \
  || fail "/health 未回显注入的 kernel_transport=mcp（env 没被读到？）"

echo "smoke: OK（容器在跑 + /health 200 + 形态回显一致）"
