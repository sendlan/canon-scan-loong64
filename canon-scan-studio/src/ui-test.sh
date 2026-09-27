#!/bin/bash
# 开发期无头验收：起 Xvfb + WebKit 原生外壳 → 截图 → 可选驱动一次扫描连拍
#
# 用法：
#   bash ui-test.sh idle           只截初始界面
#   bash ui-test.sh scan [dpi]     点「开始扫描」并连拍
#   bash ui-test.sh click X Y      点任意坐标
#   bash ui-test.sh stop           清理（只清本脚本自己起的进程）
#
# ⚠️ 隔离原则（踩过的坑，务必遵守）：
#   1) 只操作**本脚本自己启动**的进程（记录 PID），绝不 pgrep -f 全局匹配 ——
#      否则会把用户桌面上正开着的「Canon 扫描」一起杀掉。
#   2) 只杀自己起的 Xvfb（按 PID），绝不 `pkill -x Xvfb`（会杀掉无关显示）。
#   3) 截图目录默认在仓库外（~/tmp-work/canon-ui-shots），可用 CANON_SHOTS 覆盖，
#      不往源码树里丢产物。
set -u

SRC="$(cd "$(dirname "$0")" && pwd)"
SHOTS="${CANON_SHOTS:-$HOME/tmp-work/canon-ui-shots}"
PY=/usr/bin/python3
PORT="${CANON_PORT:-8899}"
DISP="${CANON_DISP:-:99}"
BASE="http://127.0.0.1:$PORT"

PID_XVFB=""; PID_BACKEND=""; PID_SHELL=""
mkdir -p "$SHOTS"

cleanup() {
  [ -n "$PID_SHELL" ]   && kill "$PID_SHELL"   2>/dev/null
  [ -n "$PID_BACKEND" ] && kill "$PID_BACKEND" 2>/dev/null
  [ -n "$PID_XVFB" ]    && kill "$PID_XVFB"    2>/dev/null
  sleep 1
}

ensure_xvfb() {
  if ! xdpyinfo -display "$DISP" >/dev/null 2>&1; then
    Xvfb "$DISP" -screen 0 1440x900x24 -nolisten tcp >/dev/null 2>&1 &
    PID_XVFB=$!
    sleep 3
  fi
}

ensure_backend() {
  if ! curl -sf "$BASE/api/info" >/dev/null 2>&1; then
    echo "[ui] 后端未运行，启动之"
    mkdir -p "$HOME/.cache/canon-studio"
    nohup "$PY" "$SRC/canon_webd.py" --port "$PORT" \
      >>"$SHOTS/webd.log" 2>&1 &
    PID_BACKEND=$!
    sleep 2
  else
    echo "[ui] 后端已在 $BASE（非本脚本启动，stop 时不会动它）"
  fi
}

start_shell() {
  export DISPLAY="$DISP"
  export WEBKIT_DISABLE_COMPOSITING_MODE=1
  nohup "$PY" "$SRC/canon-studio-shell.py" "$BASE/" \
    >"$SHOTS/webkit-run.log" 2>&1 &
  PID_SHELL=$!
  sleep "${1:-16}"
}

shot() {
  DISPLAY="$DISP" scrot -o "$SHOTS/$1" 2>/dev/null
  echo "[ui] $1  $(stat -c%s "$SHOTS/$1" 2>/dev/null)B  → $SHOTS/$1"
}

find_blue_button() {
  "$PY" - "$SHOTS/$1" <<'EOF'
import sys
from PIL import Image
im = Image.open(sys.argv[1]).convert('RGB')
px = im.load()
pts = []
for y in range(im.height // 2, im.height, 2):
    for x in range(40, 320, 2):
        r, g, b = px[x, y]
        if b > 200 and 100 < g < 190 and r < 120:
            pts.append((x, y))
if pts:
    print(sum(p[0] for p in pts) // len(pts), sum(p[1] for p in pts) // len(pts))
else:
    print("")
EOF
}

case "${1:-idle}" in
  stop)
    cleanup
    echo "[ui] 已清理（只清了本脚本启动的进程）"
    ;;

  idle)
    ensure_xvfb; ensure_backend; start_shell 16
    shot ui-idle.png
    ;;

  click)
    DISPLAY="$DISP" xdotool mousemove "${2:-0}" "${3:-0}" click 1
    echo "[ui] 已点击 ($2,$3)"
    ;;

  scan)
    DPI="${2:-150}"
    ensure_xvfb; ensure_backend; start_shell 16
    shot ui-idle.png
    POS=$(find_blue_button ui-idle.png)
    if [ -z "$POS" ]; then echo "[ui] 未找到开始扫描按钮"; exit 1; fi
    echo "[ui] 点「开始扫描」($POS)"
    DISPLAY="$DISP" xdotool mousemove $POS click 1
    for t in 5 9 13 18 24 30 36; do
      sleep 3
      shot "scan-t${t}.png"
    done
    echo "[ui] 截图在 $SHOTS"
    ;;

  *)
    echo "用法: bash ui-test.sh {idle|scan [dpi]|click X Y|stop}" >&2
    exit 2
    ;;
esac
