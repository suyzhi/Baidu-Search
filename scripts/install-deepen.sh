#!/usr/bin/env bash
# 把"深挖 TG 索引"装成 macOS launchd 常驻任务。
#
# 为什么用 launchd 而不是 nohup：nohup 起的进程会随终端/会话结束被回收（实测过），
# 而深度索引要连续跑几个小时到几天。launchd 每 N 秒拉起一轮，跑完就退出，
# 崩了下一轮自动再来；日志落盘、状态可查。
#
# 节奏（默认）：每 2 小时一轮，每轮 2 次 × 60 页/频道 ≈ +40 万条消息/轮
#   → 约 480 万条/天 → 从 73 万条到 600 万条（10 倍）约 1.5 天
#
# 用法：
#   ./scripts/install-deepen.sh                 # 安装（默认 2 小时一轮）
#   PANSEARCH_DEEPEN_INTERVAL=3600 ./scripts/install-deepen.sh   # 改成 1 小时
#   ./scripts/install-deepen.sh status          # 看是否在跑 + 最近进度
#   ./scripts/install-deepen.sh uninstall       # 卸载
#   ./scripts/install-deepen.sh now             # 立刻跑一轮（不等定时）
set -euo pipefail

LABEL="com.pansearch.deepen"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNNER="$PROJECT/scripts/deepen-index.sh"
LOG="$PROJECT/.cache/deepen-launchd.log"
INTERVAL="${PANSEARCH_DEEPEN_INTERVAL:-7200}"
ROUNDS="${PANSEARCH_DEEPEN_ROUNDS:-2}"
PAGES="${PANSEARCH_DEEPEN_PAGES:-60}"

case "${1:-install}" in
  uninstall)
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    echo "已卸载 $LABEL（索引数据保留）"
    exit 0
    ;;
  status)
    if launchctl list | grep -q "$LABEL"; then
      launchctl list | grep "$LABEL"
      echo "--- 最近日志 ---"
      tail -6 "$LOG" 2>/dev/null || echo "（还没跑过）"
      echo "--- 当前索引 ---"
      (cd "$PROJECT" && .venv/bin/pansearch index stats 2>&1 | sed -n '2,3p')
    else
      echo "未安装（$LABEL 不在 launchctl 列表里）"
    fi
    exit 0
    ;;
  now)
    echo "立刻跑一轮…"
    ROUNDS="$ROUNDS" PAGES="$PAGES" bash "$RUNNER" 2>&1 | tail -4
    exit 0
    ;;
esac

[ -x "$RUNNER" ] || { echo "找不到 $RUNNER" >&2; exit 1; }
mkdir -p "$(dirname "$PLIST")" "$PROJECT/.cache"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd "$PROJECT"; ROUNDS=$ROUNDS PAGES=$PAGES CONCURRENCY=16 ./scripts/deepen-index.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>5</integer>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST"
echo "已安装 ${LABEL}：每 $((INTERVAL / 3600)) 小时一轮，每轮 ${ROUNDS} 次 × ${PAGES} 页"
echo "看状态： ./scripts/install-deepen.sh status"
echo "立刻跑： ./scripts/install-deepen.sh now"
echo "卸载：   ./scripts/install-deepen.sh uninstall"
