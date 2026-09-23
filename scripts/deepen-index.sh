#!/usr/bin/env bash
# 连续深挖 TG 索引 —— "源数量"有天花板，"索引深度"没有。
#
# 为什么需要：源数量已经挖到平台的边界（PanSou 插件 108/109、可用引擎 8、
# 公开 API 20、站点/频道候选的验证通过率只有 1.7~4.5%），而召回真正吃的是
# **索引里有多少条消息**。`index crawl --deepen` 每轮把每个频道的抓取位置往历史
# 推 N 页，反复跑就是线性加深：
#     实测 30 页/频道 ≈ +10 万条消息；60 页/频道 ≈ +20 万条
#     当前索引 73 万条 → 想上 600 万条（10 倍）就需要约 25~30 轮
#
# 用法：
#   ./scripts/deepen-index.sh                 # 默认 6 轮 × 60 页
#   ROUNDS=20 PAGES=100 ./scripts/deepen-index.sh   # 更狠（几小时，可挂夜里）
#   nohup ./scripts/deepen-index.sh > .cache/deepen.log 2>&1 &   # 后台跑
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

ROUNDS="${ROUNDS:-6}"
PAGES="${PAGES:-60}"
CONCURRENCY="${CONCURRENCY:-20}"

echo "开始深挖：${ROUNDS} 轮 × ${PAGES} 页/频道（并发 ${CONCURRENCY}）"
for round in $(seq 1 "$ROUNDS"); do
  echo "=== 第 $round/$ROUNDS 轮 $(date '+%m-%d %H:%M:%S') ==="
  .venv/bin/pansearch index crawl --pages "$PAGES" --deepen --concurrency "$CONCURRENCY" 2>&1 | tail -2
  .venv/bin/pansearch index stats 2>&1 | sed -n '2p'
done
echo "=== 完成 $(date '+%m-%d %H:%M:%S') ==="
echo "提示：想长期自动跑，把它挂进 launchd（见 scripts/install-schedule.sh 的 --index-pages 参数）"
