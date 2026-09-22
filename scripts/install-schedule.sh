#!/usr/bin/env bash
# 把 pansearch maintain 装成 macOS launchd 定时任务（默认每 6 小时一轮）。
#
# 为什么需要：索引加深与链接复验都是"不做就悄悄退化"的事 ——
# 索引不挖，召回停在原地；缓存过期后要等下一次搜索才重验，冷门关键词可能几个月没人搜。
#
# 用法：
#   ./scripts/install-schedule.sh            # 安装（每 6 小时）
#   PANSEARCH_INTERVAL_SECONDS=3600 ./scripts/install-schedule.sh   # 改成每 1 小时
#   ./scripts/install-schedule.sh status     # 看是否在跑
#   ./scripts/install-schedule.sh uninstall  # 卸载
set -euo pipefail

LABEL="com.pansearch.maintain"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$PROJECT/.venv/bin/pansearch"
INTERVAL="${PANSEARCH_INTERVAL_SECONDS:-21600}"
LOG="$PROJECT/.cache/maintain-launchd.log"

case "${1:-install}" in
  print)
    # 只打印 plist，不安装 —— 便于先 review（plutil -lint 可直接校验）
    MODE=print
    ;;
  uninstall)
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    echo "已卸载 $LABEL"
    exit 0
    ;;
  status)
    if launchctl list | grep -q "$LABEL"; then
      launchctl list | grep "$LABEL"
      echo "--- 最近报告 ---"
      python3 -c "import json,sys;d=json.load(open('$PROJECT/.cache/maintain-report.json'));print(json.dumps(d,ensure_ascii=False,indent=1)[:800])" 2>/dev/null || echo "（还没有报告）"
    else
      echo "未安装"
    fi
    exit 0
    ;;
esac

[ -x "$PY" ] || { echo "找不到 $PY —— 先 uv venv && uv pip install -e ." >&2; exit 1; }

mkdir -p "$(dirname "$PLIST")" "$PROJECT/.cache"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string><string>maintain</string>
    <string>--index-pages</string><string>3</string>
    <string>--verify-limit</string><string>400</string>
    <string>--min-interval</string><string>5</string>
    <string>--json</string><string>$PROJECT/.cache/maintain-report.json</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLISTEOF

if [ "${MODE:-install}" = "print" ]; then
  cat "$PLIST"
  rm -f "$PLIST"
  echo "（上面就是将要安装的 launchd 配置，未加载。校验： plutil -lint <文件>）" >&2
  exit 0
fi

launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST"
echo "已安装 $LABEL（每 $((INTERVAL / 3600)) 小时一轮；最小间隔 5 小时）"
echo "手动跑一轮： $PY maintain"
echo "看日志：     tail -f $LOG"
echo "卸载：       $0 uninstall"
