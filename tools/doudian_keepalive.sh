#!/usr/bin/env bash
# ============================================================================
# 抖店登录态保活 / 体检（cron 每 15 分钟）
# ----------------------------------------------------------------------------
# 为什么需要：
#   抖店（fxg.jinritemai.com）登录态是**短效凭证**。2026-09-17 实测：
#   09:43 本机刷新 state 入库 → 09:53 服务器可免登录 → 10:00 已重定向回 /login/common。
#   隔夜必然失效 → 9:00 抓取只能走「邮箱登录」→ 撞字节 verify-center 拼图滑块
#   → **无人值守必失败**。这就是「上线后 9 点没抓到抖店数据」的根本原因。
#
# 本脚本每 15 分钟做一次「体检 + 重新导出 state 写回库」：
#   · 若抖店会话是**滑动续期** → 状态永不过期，9:00 抓取直接免登录成功（理想结果）
#   · 若是**硬 TTL** → 日志里会留下每次失效的时间戳，据此得出真实寿命，
#     再决定「提前 N 分钟预热」还是「改人工刷新 + 自动补抓」
#
# ⚠️ 严禁在 9:00 抓取窗口内跑：抖店同一账号只允许一个活跃会话，
#    保活开的浏览器会把正在抓取的会话顶掉（表现为切店/取数大面积失败）。
#    故 08:45–10:00 直接跳过。
#
# cron: */15 * * * * /opt/pw/doudian_keepalive.sh
# 日志: /opt/pw/logs/doudian_keepalive.log（滚动保留最近 2000 行）
# 状态: /opt/pw/logs/doudian_state_status.json（run_all.sh 会打印它）
# ============================================================================
set -u

# 9:00 抓取窗口（08:45–10:00）直接跳过，避免顶掉正在抓取的会话
NOW=$((10#$(date '+%H') * 60 + 10#$(date '+%M')))
if [ "$NOW" -ge 525 ] && [ "$NOW" -lt 600 ]; then
  exit 0
fi

# ★ 闸门 2：任何抖店抓取「进行中」都不跑。
#   抖店同一账号只允许一个活跃会话 —— 保活开的浏览器会把正在抓取的会话顶掉
#   （表现为切店/取数大面积失败）。抓取不止发生在 9:00：
#   fetch_reconcile 的定点补抓、网站上的「更新数据」按钮都会在白天任意时刻启一次，
#   所以只靠时间窗口不够，必须按「有没有进程在跑」判断。
#   `[l]ogin...` 是转义写法，避免 grep 匹配到自己这条 ps 管道。
if ps -ef | grep -q '[l]ogin_fetch_all.py'; then
  exit 0
fi

LOG_DIR="/opt/pw/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/doudian_keepalive.log"
STATUS="$LOG_DIR/doudian_state_status.json"

{
  echo "----- $(date '+%F %T') -----"
  cd /opt/pw/doudian || exit 1
  xvfb-run -a /opt/pw/venv/bin/python /opt/pw/doudian/keepalive.py \
      --status-file "$STATUS" 2>&1
  echo "rc=$?"
} >> "$LOG" 2>&1

if [ -f "$LOG" ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi
