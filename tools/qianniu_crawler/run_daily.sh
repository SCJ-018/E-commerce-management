#!/usr/bin/env bash
# 千牛每日取数 wrapper（服务器 /opt/pw 下，供 cron / systemd 调度）
# 用法: run_daily.sh [账号|all] [YYYY-MM-DD]
#   - 账号缺省 all（遍历「是否运营=1」且已登录的店铺）
#   - 日期缺省 = 昨天
# 关键：xvfb-run 提供虚拟显示，因为取数必须 headless=False（无头会触发阿里风控）
set -u
cd "$(dirname "$0")"

TARGET="${1:-all}"
DATE="${2:-$(date -d 'yesterday' '+%Y-%m-%d')}"

LOG_DIR="/opt/pw/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/fetch_${DATE}.log"

{
  echo "===== $(date '+%F %T') start target=$TARGET date=$DATE ====="
  xvfb-run -a /opt/pw/venv/bin/python /opt/pw/fetch_daily.py "$TARGET" "$DATE"
  echo "===== $(date '+%F %T') done rc=$? ====="
} >> "$LOG" 2>&1
