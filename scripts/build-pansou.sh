#!/usr/bin/env bash
# 从上游源码构建 PanSou 镜像 —— 为什么必须自建：
#
#   1. **官方 latest 镜像只带 74 个插件**，而仓库 plugin/ 目录有 110 个；
#   2. 更要命的是上游 main.go 里只 import 了 75 个插件 —— 仓库里还有
#      **35 个已经写好的网盘搜索插件从来没被任何镜像加载过**。
#      本脚本按目录自动补齐这些 import（排除成人站与编译不过的包），
#      把可检索的网盘搜索站从 74 个提到 100+ 个。
#
# 用法：
#   ./scripts/build-pansou.sh     # 克隆 + 补 import + 构建（约 3~8 分钟）
#   ./scripts/start-pansou.sh     # 本地有 pansou:local 就优先用它
set -euo pipefail

SRC_DIR="${PANSOU_SRC:-.cache/pansou-src}"
IMAGE="${PANSOU_BUILD_IMAGE:-pansou:local}"
REPO="${PANSOU_REPO:-https://github.com/fish2018/pansou.git}"
# 插件不再默认排除任何站点：javdb（成人站）默认**收进来**，
# 需要过滤时用 pansearch 的 --sfw（见 config/sources.yaml 的 adult_sources），
# 而不是在构建阶段把它删掉 —— 构建和过滤是两件事。
SKIP="${PANSOU_PLUGIN_SKIP:-}"

command -v docker >/dev/null || { echo "需要 docker（colima start 后再试）" >&2; exit 1; }
docker info >/dev/null 2>&1 || colima start

if [ ! -d "$SRC_DIR/.git" ]; then
  mkdir -p "$(dirname "$SRC_DIR")"
  git clone --depth 1 "$REPO" "$SRC_DIR"
else
  git -C "$SRC_DIR" pull --ff-only || true
fi

# ---- 1. 让没有 buildx 的经典构建器也能构建 ----
ARCH="$(uname -m)"; [ "$ARCH" = "arm64" ] || ARCH="amd64"
python3 - "$SRC_DIR" "$ARCH" <<'PY'
import pathlib, sys
src, arch = pathlib.Path(sys.argv[1]), sys.argv[2]
nl = chr(10)                      # 用 chr(10) 而不是 \n：避免脚本被 shell/python 两层转义咬到
t = (src / "Dockerfile").read_text()
t = t.replace("FROM --platform=$BUILDPLATFORM golang:1.24-alpine AS builder",
              "FROM golang:1.24-alpine AS builder")
t = t.replace("GOARCH=${TARGETARCH}", "GOARCH=" + arch)
t = t.replace("RUN go mod download",
              "ENV GOPROXY=https://goproxy.cn,direct" + nl + "RUN go mod download", 1)
(src / "Dockerfile.local").write_text(t)
print("Dockerfile.local 已生成（arch=%s）" % arch)
PY

# ---- 2. 补齐 main.go 里缺失的插件 import（可反复调用，已补过就跳过）----
patch_imports() {
  python3 - "$SRC_DIR" "$SKIP" "$@" <<'PY'
import pathlib, re, sys
src = pathlib.Path(sys.argv[1])
skip = {s for s in sys.argv[2].split(",") if s} | {s for s in sys.argv[3:] if s}
main = src / "main.go"
text = main.read_text()
imported = set(re.findall(r'pansou/plugin/([A-Za-z0-9_]+)"', text))
# 没有 .go 文件的目录（上游半成品，如 pioz）直接跳过，否则 go build 会报 not in std
dirs = sorted(p.name for p in (src / "plugin").iterdir()
              if p.is_dir() and any(p.glob("*.go")))
todo = [d for d in dirs if d not in imported and d not in skip]
if not todo:
    print("main.go 已包含全部插件 import，无需补")
    sys.exit(0)
anchor = None
for m in re.finditer(r'^\s*_ "pansou/plugin/[A-Za-z0-9_]+"\s*$', text, re.M):
    anchor = m
assert anchor, "main.go 里找不到插件 import 块"
lines = "".join(('\t_ "pansou/plugin/%s"' % p) + chr(10) for p in todo)
text = text[:anchor.end() + 1] + lines + text[anchor.end() + 1:]
main.write_text(text)
print("新增 %d 个插件 import：%s" % (len(todo), ", ".join(todo)))
PY
}

BUILD_LOG="$(mktemp)"
for attempt in 1 2 3; do
  patch_imports
  echo "构建 ${IMAGE}（第 ${attempt} 次）…"
  if docker build -f "$SRC_DIR/Dockerfile.local" -t "$IMAGE" "$SRC_DIR" >"$BUILD_LOG" 2>&1; then
    echo "构建成功"
    break
  fi
  # 注意 set -e：grep 没匹配时返回 1，要 || true，否则脚本静默退出
  fails="$(grep -oE 'pansou/plugin/[A-Za-z0-9_]+' "$BUILD_LOG" | sed 's#.*/##' | sort -u | tr '\n' ' ' || true)"
  echo "构建失败，尝试剔除编译不过的插件：${fails:-（没解析出包名）}"
  tail -15 "$BUILD_LOG"
  [ -z "$fails" ] && exit 1
  for pkg in $fails; do
    python3 - "$SRC_DIR" "$pkg" <<'PY'
import pathlib, sys, re
main = pathlib.Path(sys.argv[1]) / "main.go"
pkg = sys.argv[2]
t = main.read_text()
t = re.sub(r'^\s*_ "pansou/plugin/' + re.escape(pkg) + r'"\s*' + chr(10), "", t, flags=re.M)
main.write_text(t)
PY
  done
done
docker image inspect "$IMAGE" >/dev/null || { echo "构建未成功" >&2; exit 1; }

echo
echo "构建完成：$IMAGE"
echo "插件目录：$(ls -d "$SRC_DIR"/plugin/*/ | wc -l | tr -d ' ') 个；main.go 已接线：$(grep -c 'pansou/plugin/' "$SRC_DIR/main.go") 个"
echo "启动： ./scripts/start-pansou.sh"
