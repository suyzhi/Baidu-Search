#!/usr/bin/env bash
# 自建 SearXNG —— 一个后端换回十几个搜索引擎（含 Google）。
#
# 为什么需要：直连 google/qwant/brave 全是 429/403；SearXNG 从服务端聚合请求，
# 命中率完全不同。它作为 websearch 适配器的一个 engine 接入（见 config/sources.yaml）。
#
# 用法：
#   ./scripts/start-searxng.sh            # 启动（端口 8889）
#   curl -s 'http://127.0.0.1:8889/search?q=test&format=json' | head -c 200
set -euo pipefail

PORT="${SEARXNG_PORT:-8889}"
NAME="searxng"
CONF_DIR="${SEARXNG_CONF:-config/searxng}"

command -v docker >/dev/null || { echo "需要 docker（colima start 后再试）" >&2; exit 1; }
docker info >/dev/null 2>&1 || colima start

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart unless-stopped \
  -p "${PORT}:8080" \
  -v "$(cd "$(dirname "$CONF_DIR")" && pwd)/$(basename "$CONF_DIR")/settings.yml:/etc/searxng/settings.yml:ro" \
  -e SEARXNG_BASE_URL="http://127.0.0.1:${PORT}/" \
  searxng/searxng:latest

echo "已启动 searxng（端口 ${PORT}），首次启动约 5~10 秒。"
echo "验证： curl -s 'http://127.0.0.1:${PORT}/search?q=test&format=json' | head -c 200"
