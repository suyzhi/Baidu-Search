#!/usr/bin/env bash
# 自建 SearXNG —— 一个后端换回十几个搜索引擎（含 Google）。
#
# 为什么需要：直连 google/qwant/brave 全是 429/403；SearXNG 从服务端聚合请求，
# 命中率完全不同。它作为 websearch 适配器的一个 engine 接入（见 config/sources.yaml）。
#
# 密钥处理：config/searxng/settings.yml 只是**模板**（里面是占位符）。
# 首次运行会把模板复制到 .cache/searxng/settings.yml，并把 secret_key 换成随机值，
# 容器挂载的是后者 —— 仓库里绝不出现真实密钥。
#
# 用法：
#   ./scripts/start-searxng.sh            # 启动（端口 8889）
#   ./scripts/start-searxng.sh rotate     # 重新生成密钥并重启
#   curl -s 'http://127.0.0.1:8889/search?q=test&format=json' | head -c 200
set -euo pipefail

PORT="${SEARXNG_PORT:-8889}"
NAME="searxng"
TEMPLATE="config/searxng/settings.yml"
RUNTIME=".cache/searxng/settings.yml"

command -v docker >/dev/null || { echo "需要 docker（colima start 后再试）" >&2; exit 1; }
docker info >/dev/null 2>&1 || colima start

mkdir -p "$(dirname "$RUNTIME")"
if [ "${1:-start}" = "rotate" ] || [ ! -f "$RUNTIME" ]; then
  if [ ! -f "$TEMPLATE" ]; then
    echo "找不到模板 $TEMPLATE" >&2; exit 1
  fi
  SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
  python3 - "$TEMPLATE" "$RUNTIME" "$SECRET" <<'PY'
import pathlib, sys
template, out, secret = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
text = template.read_text(encoding="utf-8")
text = text.replace("CHANGEME-generated-by-start-searxng.sh", secret)
out.write_text(text, encoding="utf-8")
print("已生成运行时配置（随机 secret_key，长度 %d）" % len(secret))
PY
  chmod 600 "$RUNTIME" || true
fi

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart unless-stopped \
  -p "${PORT}:8080" \
  -v "$(pwd)/$RUNTIME:/etc/searxng/settings.yml:ro" \
  -e SEARXNG_BASE_URL="http://127.0.0.1:${PORT}/" \
  searxng/searxng:latest

echo "已启动 searxng（端口 ${PORT}），首次启动约 5~10 秒。"
echo "密钥在 ${RUNTIME}（已 gitignore），要换： $0 rotate"
echo "验证： curl -s 'http://127.0.0.1:${PORT}/search?q=test&format=json' | head -c 200"
