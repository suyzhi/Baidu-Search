#!/usr/bin/env bash
# 双击启动 pansearch：拉起后端容器（PanSou / SearXNG）→ 启动网页 → 自动打开浏览器。
#
# 在 Finder 里双击即可（macOS 会用"终端"打开 .command 文件）。
# 关掉这个终端窗口或按 Ctrl+C 就停止网页服务；后端容器继续在后台跑，下次秒开。
#
# 每一步失败都不阻塞：Docker 不可用时 PanSou 会自动降级到公共实例，只是召回少一些。
cd "$(dirname "$0")" || exit 1

PORT="${PANSEARCH_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"
LOG=".cache/launcher.log"
mkdir -p .cache

pause_exit() {
  echo
  read -r -p "按回车关闭窗口…" _
  exit "${1:-1}"
}

echo "== pansearch =="

# 1. 已经在跑：直接打开浏览器
if curl -s -m 1 "${URL}/api/health" >/dev/null 2>&1; then
  echo "网页服务已在运行，打开 ${URL}"
  open "$URL"
  sleep 1
  exit 0
fi

# 2. Python 环境（首次运行自动安装）
if [ ! -x .venv/bin/pansearch ]; then
  echo "首次运行：创建虚拟环境并安装依赖…"
  if ! command -v uv >/dev/null 2>&1; then
    echo "✗ 需要 uv：brew install uv"
    pause_exit 1
  fi
  uv venv >>"$LOG" 2>&1 && uv pip install -e ".[web]" >>"$LOG" 2>&1 || {
    echo "✗ 依赖安装失败，详情见 ${LOG}"
    pause_exit 1
  }
fi

# 3. 后端容器：在跑就跳过，停了就 start，不存在才用脚本创建
#    （start-*.sh 每次都会删掉重建、重新拉镜像，不适合日常双击）
ensure_container() {
  local name="$1" script="$2"
  if docker ps --format '{{.Names}}' | grep -qx "$name"; then
    echo "✓ ${name} 已在运行"
  elif docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    if docker start "$name" >>"$LOG" 2>&1; then echo "✓ 已启动 ${name}"; else echo "⚠ ${name} 启动失败（见 ${LOG}）"; fi
  else
    echo "… 首次创建 ${name}（可能需要一两分钟）"
    if "$script" >>"$LOG" 2>&1; then echo "✓ 已创建 ${name}"; else echo "⚠ ${name} 创建失败（见 ${LOG}）"; fi
  fi
}

if command -v docker >/dev/null 2>&1; then
  if ! docker info >/dev/null 2>&1 && command -v colima >/dev/null 2>&1; then
    echo "… 启动 colima（约 10~30 秒）"
    colima start >>"$LOG" 2>&1 || true
  fi
  if docker info >/dev/null 2>&1; then
    ensure_container pansou scripts/start-pansou.sh
    ensure_container searxng scripts/start-searxng.sh
  else
    echo "⚠ Docker 未就绪：PanSou 将降级到公共实例（召回会少一些）"
  fi
else
  echo "⚠ 未安装 docker（brew install colima docker）：PanSou 将降级到公共实例"
fi

# 4. 网页服务（前台运行，会自动打开浏览器）
echo "✓ 启动网页 ${URL}   —— 关闭此窗口即停止"
exec .venv/bin/pansearch web --port "$PORT"
