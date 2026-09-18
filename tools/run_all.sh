#!/usr/bin/env bash
# ============================================================================
# 三平台每日抓取 + 对账补抓 总编排
# ----------------------------------------------------------------------------
# 背景：三个平台原先各有一条 cron（都在 9:00）。只要有一家店因为
#       「登录态失效 / 切店失败 / 取数异常」没落库，旧脚本只在日志里打一行提示，
#       然后 rc=0 静默退出 —— cron 以为成功，没人知道缺数据。
#
# 本脚本把链路串成一条：
#   ① 千牛 → ② 抖店 → ③ 京东  （串行，避免并发 Chrome 抢内存 / 被风控）
#   ④ 对账：读「店铺账号管理」三张账号表的「运营中」列表，
#      比对「店铺营销数据」目标日期，找出运营中但没落库的店铺
#      → 定点重新唤起对应平台抓取程序补抓（最多 2 轮）
#      → 仍有缺失则写对账日志表 + 钉钉告警
#
# cron: 0 9 * * * /opt/pw/run_all.sh
# 用法: run_all.sh [YYYY-MM-DD]     # 日期缺省 = 昨天
# 日志: /opt/pw/logs/reconcile_<日期>.log（各平台明细仍写各自的日志文件）
# ============================================================================
set -u

# 无人值守标记：抓取脚本据此判断「本机是 xvfb 虚拟屏、没人能拖拼图滑块」，
# 遇到验证立刻失败退出，而不是干等 240s（batch + 多日期会把 4 分钟乘上去）。
# 2026-09-17 加：服务器上那句「请在浏览器窗口人工拖拽」永远是空话。
export PW_UNATTENDED=1

DATE="${1:-$(date -d 'yesterday' '+%Y-%m-%d')}"
LOG_DIR="/opt/pw/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/reconcile_${DATE}.log"
PY="/opt/pw/venv/bin/python"

(
  echo "########## $(date '+%F %T') 全平台抓取开始 date=$DATE ##########"

  echo
  echo "===== [1/4] 千牛抓取 ====="
  /opt/pw/run_daily.sh all "$DATE"

  echo
  echo "===== [2/4] 抖店抓取 ====="
  # 前置体检：把「登录态状态文件」的打点打出来（保活 cron 每 5 分钟刷新一次）。
  # 抖店登录态是短效凭证，失效后抓取只能走邮箱登录 → 撞拼图滑块 → 必失败；
  # 这里留痕是为了让「为什么今天没抓到」一眼可查，不再靠翻 long log。
  if [ -f "$LOG_DIR/doudian_state_status.json" ]; then
    echo "[抖店] 登录态状态文件: $(cat "$LOG_DIR/doudian_state_status.json" | tr -d '\n')"
  else
    echo "[抖店] ⚠️ 无登录态状态文件（保活 cron 没跑过？）"
  fi
  /opt/pw/doudian/run_daily.sh "$DATE"

  echo
  echo "===== [3/4] 京东抓取 ====="
  /opt/pw/jd/run_daily.sh "$DATE"

  echo
  echo "===== [4/4] 对账 + 自动补抓 ====="
  "$PY" /opt/pw/fetch_reconcile.py "$DATE" --rounds 2
  RC=$?
  echo "===== $(date '+%F %T') 对账退出码 rc=$RC ====="
  exit $RC
) >> "$LOG" 2>&1

RC=$?
echo "[run_all] date=$DATE rc=$RC log=$LOG"
exit $RC
