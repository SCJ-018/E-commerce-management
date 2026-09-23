# -*- coding: utf-8 -*-
"""抓取对账 + 自动补抓编排器。

【要解决的问题】
每天 9:00 的三平台定时抓取（千牛 / 抖店 / 京东），只要有店铺因为
「登录态失效 / 切店失败 / 取数异常 / 账号限流」没落库，旧脚本只在日志里打一行
「⚠️ 以下 N 家未完整落库」，然后 rc=0 静默退出 —— cron 以为成功，
只有人工去翻日志才会发现缺数据。

【本脚本做三件事】
1. 从「店铺账号管理」对应的三张账号表读取 `是否运营=1` 的店铺
   （与抓取脚本 shops.py 完全同一份来源，账号管理页的「运营中」开关即生效）
2. 对账：**运营中但目标日期没有落库的店铺** = 缺失
   （以库里有没有数据为准，不看抓取脚本退出码 —— 退出码会骗人，数据不会）
3. 对缺失店铺重新唤起对应平台的抓取程序（定点重抓），最多 N 轮；
   仍有缺失 → 写 `fetch_reconcile_logs` 表 + 钉钉告警

【平台值映射（重要）】
  ┌────────────┬────────────────┬────────────────────────────┐
  │ 账号表      │ 店铺营销数据.平台 │ 抓取入口                    │
  ├────────────┼────────────────┼────────────────────────────┤
  │ 千牛账号表  │ 千牛            │ fetch_daily.py <账号> <日期>│
  │ 抖店账号表  │ **抖音**        │ doudian/login_fetch_all.py  │
  │ 京东账号表  │ 京东            │ jd/fetch_main.py --date     │
  └────────────┴────────────────┴────────────────────────────┘
  注意：抖店落库的平台值是「抖音」不是「抖店」—— 沿用早期影刀口径，勿改。

【重抓粒度】
  · 千牛 per_shop：一家店一个进程（各店独立登录态）
  · 抖店 单会话：缺失店铺**全部塞进一次** login_fetch_all.py 调用
    （登录态短效，多批次 = 多会话 = 后面的批次必挂）
  · 京东 whole：单店平台，全量重跑

【用法】
  python fetch_reconcile.py [YYYY-MM-DD] [--rounds 2] [--platform 千牛,抖店,京东]
                            [--skip-fetch] [--no-alert] [--dry-run]
  · 日期缺省 = 昨天
  · --skip-fetch  只对账不补抓（体检 / 冒烟测试）
  · --dry-run     不写库、不告警，只打印
  · 服务器上由 /opt/pw/run_all.sh 调度（cron 0 9 * * *）

【退出码】
  0 = 三平台运营中店铺全部落库
  3 = 仍有缺失（已写对账日志 + 已告警）
  1 = 脚本自身异常
"""
import sys
import os
import json
import time
import signal
import argparse
import datetime
import threading
import subprocess

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_SERVER = os.path.exists('/opt/pw')
PW_BASE = '/opt/pw' if IS_SERVER else BASE_DIR


# ============================ 数据库 ============================
# 与 tools/*/shops.py 完全一致的连接规则：服务器直连 3306，本地走 3307 隧道
SERVER_DB = {
    'host': '127.0.0.1',
    'port': 3306,
    'user': os.environ.get('FETCH_DB_USER', ''),
    'password': os.environ.get('FETCH_DB_PASSWORD', ''),
    'database': os.environ.get('FETCH_DB_NAME', ''),
    'charset': 'utf8mb4',
    'connect_timeout': 15,
}


def get_conn():
    import pymysql
    host = '127.0.0.1' if IS_SERVER else '127.0.0.1'
    port = 3306 if IS_SERVER else 3307
    cfg = dict(SERVER_DB)
    cfg['host'], cfg['port'] = host, port
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor)


# ============================ 平台配置 ============================
# cli_col = 该平台抓取脚本用来定位店铺的字段
PLATFORMS = [
    {
        'key': '千牛',                      # 店铺营销数据.平台 取值
        'table': '千牛账号表',
        'cli_col': '账号',                  # fetch_daily.py 第一个参数＝账号（含 :子账号）
        'local': ('qianniu_crawler', 'fetch_daily.py'),
        'server': ('', 'fetch_daily.py'),   # /opt/pw/fetch_daily.py
        'style': 'per_shop',                # 一家店一个进程
    },
    {
        'key': '抖音',                      # ← 抖店在库里的平台名
        'table': '抖店账号表',
        'cli_col': '店铺名',
        'local': ('doudian_crawler', 'login_fetch_all.py'),
        'server': ('doudian', 'login_fetch_all.py'),
        'style': 'batch',
        # ★ 批大小反复调过两次，两个约束互相拉扯，别再随手改：
        #   ① 批次太多 → 每个批次开一个新浏览器会话、消费同一份 state，
        #      曾出现「后面批次全挂」（09:57 实测 4 批全挂）。
        #      但那次的真因是 state 本身已过期（09:43 签发 → 10:00 失效），
        #      不是「拆批」原罪 —— 脚本已改成**每批跑完无条件写回新 state**，
        #      下一批拿到的是新鲜凭证，拆批因此可行。
        #   ② 一次抓完所有 → 请求过于密集，撞抖店账号级限流
        #      （st=11001 请求过于频繁，2026-09-17 10:19 实测：连抓 7 家后
        #      第 8 家第 1 页就限流，退避 13 分钟仍不恢复，剩余 4 家全废）。
        #   → 折中：每批 1 家 + 批间冷却，把请求摊开在时间轴上。
        'batch_size': 1,
        # 批间冷却。2026-09-17 实测：45s 太小 —— 抖店罗盘是账号级配额，
        # 连抓 3 家就有概率触发 st=11001（且窗口 >45 分钟），所以拉到 180s。
        # 代价：14 家全量补抓要多花 ~40 分钟；收益：不再烂尾在最后几家。
        'batch_cool': 180,
        'alias': '抖店',                    # 对外展示名
    },
    {
        'key': '京东',
        'table': '京东账号表',
        'cli_col': '店铺名',
        'local': ('jd_crawler', 'fetch_main.py'),
        'server': ('jd', 'fetch_main.py'),
        'style': 'whole',                   # 单店平台，全量重跑一次
        'alias': '京东',
    },
]

SHOP_TABLE = '店铺营销数据'
LOG_TABLE = 'fetch_reconcile_logs'
# 连续多少个批次/店铺登录失败才整体止损（见 refetch() 里 rc==2 分支）
LOGIN_FAIL_LIMIT = 3

_LOG_DDL = """
CREATE TABLE IF NOT EXISTS `%s` (
    id INT AUTO_INCREMENT PRIMARY KEY,
    target_date DATE NOT NULL,
    round_no INT NOT NULL DEFAULT 0,
    platform VARCHAR(16) NOT NULL,
    active_count INT NOT NULL DEFAULT 0,
    ok_count INT NOT NULL DEFAULT 0,
    missing_shops TEXT,
    action VARCHAR(32) NOT NULL DEFAULT '',
    final_status VARCHAR(16) NOT NULL DEFAULT '',
    alert_status VARCHAR(16) NOT NULL DEFAULT '',
    detail TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_target_date (target_date),
    KEY idx_platform (platform)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""" % LOG_TABLE


# ============================ 账号表 / 落库读取 ============================

def load_active_shops(conn, p):
    """平台运营中店铺：[{id, shop_id, label, cli_value}]"""
    with conn.cursor() as cur:
        cur.execute('SELECT * FROM `%s` WHERE `是否运营`=1 ORDER BY `id`' % p['table'])
        rows = cur.fetchall()
    out = []
    for r in rows:
        val = (r.get(p['cli_col']) or '').strip()
        out.append({
            'id': r.get('id'),
            'shop_id': str(r.get('店铺ID') or '').strip(),
            'label': val,
            'cli_value': val,
        })
    return out


def load_ingested_ids(conn, platform, date_str):
    """目标日期已落库的店铺ID 集合"""
    with conn.cursor() as cur:
        cur.execute(
            'SELECT DISTINCT `店铺ID` FROM `%s` WHERE `平台`=%%s AND `日期`=%%s' % SHOP_TABLE,
            (platform, date_str))
        return set(str(r['店铺ID']).strip() for r in cur.fetchall())


def reconcile(conn, date_str, platforms):
    """对账：{平台: {platform, display, active, ok, missing, no_id}}"""
    result = {}
    for p in platforms:
        active = load_active_shops(conn, p)
        ingested = load_ingested_ids(conn, p['key'], date_str)
        missing = [s for s in active if s['shop_id'] and s['shop_id'] not in ingested]
        no_id = [s for s in active if not s['shop_id']]
        result[p['key']] = {
            'platform': p['key'],
            'display': p.get('alias') or p['key'],
            'active': active,
            'ok': len(active) - len(missing) - len(no_id),
            'missing': missing,
            'no_id': no_id,
        }
    return result


# ============================ 补抓 ============================

def _py_exe():
    return '/opt/pw/venv/bin/python' if IS_SERVER else sys.executable


def _crawler_path(p):
    sub, fname = p['server'] if IS_SERVER else p['local']
    parts = [PW_BASE] + ([sub] if sub else []) + [fname]
    return os.path.join(*parts)


def _wrap(cmd):
    """服务器上抓取必须 headless=False（阿里/抖音风控），用 xvfb-run 提供虚拟显示"""
    # ★ 必须 -u：抓取脚本的 stdout 是管道（非 tty），默认块缓冲会让「打印的进度」
    #   卡在缓冲区里，实时日志看起来像没在跑。2026-09-17 排障需要。
    if cmd and 'python' in os.path.basename(cmd[0]) and '-u' not in cmd[:2]:
        cmd = [cmd[0], '-u'] + cmd[1:]
    if IS_SERVER and os.path.exists('/usr/bin/xvfb-run'):
        return ['xvfb-run', '-a'] + cmd
    return cmd


def _stream_path():
    """抓取子进程的实时输出落盘位置。

    2026-09-17 踩坑：原来 `_run` 用 `subprocess.run(capture_output=True)`，
    子进程几十万字节输出全缓冲在内存，只有结束后才回捞最后 20 行 ——
    结果「抓取跑了 20 分钟没动静」时既 tail 不到日志、也不知道卡在哪一家，
    只能靠 py-spy 去 dump 栈。改成边跑边写文件，排障时直接 tail 即可。
    """
    d = os.path.join(PW_BASE, 'logs')
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = PW_BASE
    return os.path.join(d, 'fetch_stream_%s.log' % datetime.date.today().isoformat())


def _kill_tree(proc, grace=3):
    """超时强杀：连**整个进程组**一起清，不要只杀最外层。

    ★ 2026-09-17 修（服务器实测残留 7 个孤儿 Xvfb，:99~:105，横跨 6 小时）：
      命令被 _wrap() 包成 `xvfb-run -a python xxx.py`，而 xvfb-run 是 shell 脚本，
      它自己再去启 Xvfb 和 python。原来的 proc.kill() 只杀 xvfb-run 这一层 ——
      它下面的 Xvfb / python / Chrome 全部变孤儿被 init 收养，继续跑到底：
        · Xvfb 没有「父进程断开就自退」的机制 → 只增不减
        · Chrome 没退干净会持续吃 CPU（实测单渲染进程 156%）
      配合 Popen(start_new_session=True)，子进程自成一个进程组，这里 killpg 一锅端。
      注意：start_new_session 只改会话、不改 cgroup —— systemd 的
      KillMode=control-group 依然能在重启 ecom 时把这些进程一起带走，不影响运维。
    """
    if proc is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=grace)
        return
    except Exception:
        pass
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        pass


def _run(cmd, timeout, log_lines, cwd=None):
    if IS_SERVER:
        inner = ' '.join("'%s'" % c.replace("'", "'\\''") for c in cmd)
        show = inner
        cmd = ['bash', '-lc', 'cd /opt/pw && exec ' + inner]
        cwd = None
    else:
        show = ' '.join(c if ' ' not in c else '"%s"' % c for c in cmd)
    log_lines.append('$ ' + show)

    tail, rc, killed = [], None, False
    try:
        with open(_stream_path(), 'a', encoding='utf-8', errors='replace') as fh:
            fh.write('\n===== %s  $ %s\n'
                     % (datetime.datetime.now().strftime('%F %T'), show))
            fh.flush()
            cp = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=cwd, bufsize=1, text=True, encoding='utf-8', errors='replace',
                start_new_session=True)
            # ★ 超时必须杀整个进程组（理由见 _kill_tree）：只 kill 最外层会留下孤儿
            #   Xvfb / Chrome 继续吃 CPU，下一次抓取就更卡。
            timer = threading.Timer(timeout, lambda: (setattr(cp, '_killed', True), _kill_tree(cp)))
            timer.start()
            try:
                for line in cp.stdout:
                    line = line.rstrip('\n')
                    fh.write(line + '\n')
                    fh.flush()                 # 必须 flush：否则 tail 看不出进度
                    tail.append(line)
                    if len(tail) > 60:
                        tail.pop(0)
            finally:
                timer.cancel()
                cp.wait()
        rc = cp.returncode
        killed = getattr(cp, '_killed', False)
    except Exception as e:
        log_lines.append('  [err] %s' % e)
        return -1

    for line in tail[-20:]:
        log_lines.append('  ' + line)
    if killed:
        log_lines.append('  [err] 超时 %ds，已强杀' % timeout)
        return -9
    log_lines.append('  → rc=%s' % rc)
    return rc


def refetch(p, date_str, missing, log_lines):
    """对缺失店铺重新唤起抓取程序。返回 (是否执行过, 目标店铺数)"""
    values = [s['cli_value'] for s in missing if s['cli_value']]
    if not values:
        log_lines.append('  [warn] %s 缺失店铺没有可用的抓取标识，跳过' % p['key'])
        return False, 0
    exe = _py_exe()
    script = _crawler_path(p)

    if p['style'] == 'per_shop':
        for i, v in enumerate(values, 1):
            log_lines.append('  [%d/%d] 重抓 %s: %s' % (i, len(values), p['key'], v))
            _run(_wrap([exe, script, v, date_str]), 1800, log_lines,
                 cwd=os.path.dirname(script))
        return True, len(values)

    if p['style'] == 'batch':
        # batch_size=0 / None → 单批（= 整个缺失列表在同一个浏览器会话里抓完）
        size = p.get('batch_size') or len(values)
        cool = p.get('batch_cool') or 0
        total = (len(values) + size - 1) // size
        nofetch_streak = 0
        login_fails = 0
        for bi in range(0, len(values), size):
            chunk = values[bi:bi + size]
            log_lines.append('  [%d/%d] 重抓 %s %d 家: %s'
                             % (bi // size + 1, total, p['key'], len(chunk), ', '.join(chunk)))
            n0 = len(log_lines)
            rc = _run(_wrap([exe, script, date_str, ','.join(chunk)]), 3600, log_lines,
                      cwd=os.path.dirname(script))
            # rc=2 是「登录失败」。老逻辑直接 break 掉整个平台 —— 但登录失败
            # 经常只是**某一次**的瞬时抖动（页面没加载出来 / 邮箱 tab 没点到），
            # 一站卡住不该拖死后面所有店。改为「跳过这家，继续下一家」，
            # 只有连续 LOGIN_FAIL_LIMIT 家都登不上才止损（那是 state 真过期，
            # 只能人工本机刷新，继续跑纯属白等）。
            if rc == 2:
                login_fails += 1
                log_lines.append('  [skip] 登录失败（连续 %d 次）→ 跳过 %s，继续下一家'
                                 % (login_fails, ', '.join(chunk)))
                if login_fails >= LOGIN_FAIL_LIMIT:
                    left = total - (bi // size) - 1
                    log_lines.append(
                        '  [stop] 连续 %d 家登录失败（state 过期/需人工过滑块），'
                        '结束本平台，剩余 %d 家交给下一轮/对账告警' % (login_fails, left))
                    break
                time.sleep(10)          # 别把登录端点也打紧
                continue
            # ★★ 撞限流必须**立刻停掉整个平台**，不能继续下一家。
            #   2026-09-17 实测：st=11001 是账号级滑动窗口，**每一次重试/下一家请求
            #   都会续期窗口** —— 连抓 3~7 家触发后，45 分钟都不恢复；换本机网络、
            #   换有头 Chrome 一样限流（已排除 IP/浏览器环境因素）。
            #   继续跑下一家 = 又发一轮请求 = 窗口再续期，纯属自我伤害。
            seg = log_lines[n0:]
            if any('st=11001' in ln for ln in seg):
                left = total - (bi // size) - 1
                log_lines.append(
                    '  [stop] 撞账号级限流 st=11001 → 立即结束本平台（继续请求只会续期窗口），'
                    '剩余 %d 家交给下一轮/对账告警' % left)
                break
            # 兜底：连续 2 批一行数据都没取到（非限流的其他静默失败）也止损
            got = any('第 1 页' in ln or 'OK ' in ln for ln in seg)
            nofetch_streak = 0 if got else nofetch_streak + 1
            if nofetch_streak >= 2:
                log_lines.append('  [stop] 连续 %d 批未取到任何数据，'
                                 '提前结束本平台，等待下轮/人工' % nofetch_streak)
                break
            if bi + size < len(values) and cool:
                log_lines.append('    [cool] 批间冷却 %ds（规避抖店接口限流）' % cool)
                time.sleep(cool)
        return True, len(values)

    log_lines.append('  重抓 %s（全量 %d 家）' % (p['key'], len(values)))
    _run(_wrap([exe, script, '--date', date_str]), 1800, log_lines,
         cwd=os.path.dirname(script))
    return True, len(values)


# ============================ 对账日志 ============================

def ensure_log_table(conn):
    with conn.cursor() as cur:
        cur.execute(_LOG_DDL)
    conn.commit()


def write_log(conn, date_str, round_no, plat, action, alert_status, detail):
    with conn.cursor() as cur:
        cur.execute(
            'INSERT INTO `%s` (target_date, round_no, platform, active_count, ok_count, '
            'missing_shops, action, final_status, alert_status, detail) '
            'VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s)' % LOG_TABLE,
            (date_str, round_no, plat['platform'], len(plat['active']), plat['ok'],
             ', '.join(s['label'] for s in plat['missing'])[:2000],
             action,
             'ok' if not plat['missing'] else 'missing',
             alert_status,
             (detail or '')[:2000]))
    conn.commit()


# ============================ 钉钉告警 ============================

# ★ 必须用 Client ID（ding 开头），App ID / AgentId 都换不到 token。
# 正常情况下会被 dingtalk_push_config.app_key 覆盖，这里只是兜底默认值。
_PUSH_DEFAULT_APP_KEY = 'dingjxvfpxfrgbgxrbyq'  # 「小钉」Client ID

# ★★ 抓取进度告警的「接收人白名单」（2026-09-17 用户要求：只告知李自豪，其他人不打扰）
#
# ⚠️⚠️ 绝对不要用「改 dingtalk_push_users.enabled」的办法来限制本告警！
#   那张表**同时**供「数据分析日报」使用（backend/app.py `_push_users()` →
#   `_push_daily_report()`），改 enabled 会把日报的收件人一起改掉/删掉。
#   两条链路必须在本脚本内单独过滤 —— 表是共用的，用途不是。
#
# 匹配规则：`user_id` 命中优先，其次按 `name` 匹配（人员改名后靠 user_id 才认得住）。
# 留空（两个列表都空）= 不限制，恢复「发给所有启用中成员」的旧行为。
ALERT_ONLY_USER_IDS = ['17841605101729565']   # 李自豪
ALERT_ONLY_NAMES = ['李自豪']


def _in_alert_whitelist(u):
    """该推送人是否在白名单内（见 ALERT_ONLY_* 上方注释）"""
    uid = (u.get('user_id') or '').strip()
    nm = (u.get('name') or '').strip()
    return bool((uid and uid in ALERT_ONLY_USER_IDS)
                or (nm and nm in ALERT_ONLY_NAMES))


def _load_dingtalk_module():
    """钉钉客户端在 backend/dingtalk.py（服务器 /opt/ecom/backend/）"""
    cands = ['/opt/ecom/backend', os.path.join(os.path.dirname(BASE_DIR), 'backend')]
    for d in cands:
        if os.path.isfile(os.path.join(d, 'dingtalk.py')):
            if d not in sys.path:
                sys.path.insert(0, d)
            import dingtalk
            return dingtalk
    return None


def send_alert(conn, title, text, log_lines):
    """把对账结果推给「钉钉推送」里启用中的成员。永不抛异常。"""
    try:
        dt = _load_dingtalk_module()
        if dt is None:
            log_lines.append('[告警] 未找到 backend/dingtalk.py，跳过')
            return 'nofile'

        cfg = {'app_key': _PUSH_DEFAULT_APP_KEY, 'app_secret': ''}
        users = []
        with conn.cursor() as cur:
            try:
                cur.execute('SELECT cfg_key, cfg_value FROM dingtalk_push_config')
                for r in cur.fetchall():
                    if r.get('cfg_key') in cfg and r.get('cfg_value') is not None:
                        cfg[r['cfg_key']] = r['cfg_value']
            except Exception as e:
                log_lines.append('[告警] 读推送配置失败: %s' % e)
            try:
                cur.execute('SELECT id, name, mobile, user_id FROM dingtalk_push_users '
                            'WHERE enabled=1 ORDER BY id')
                users = cur.fetchall()
                # ★ 只发给白名单（当前=李自豪）。表与「数据分析日报」共用，故在此过滤而非改表。
                if ALERT_ONLY_USER_IDS or ALERT_ONLY_NAMES:
                    n_before = len(users)
                    users = [u for u in users if _in_alert_whitelist(u)]
                    log_lines.append('[告警] 接收人白名单命中 %d/%d 人（跳过 %d 人）'
                                     % (len(users), n_before, n_before - len(users)))
            except Exception as e:
                log_lines.append('[告警] 读推送人失败: %s' % e)

        if not (cfg.get('app_secret') or '').strip():
            log_lines.append('[告警] 未配置 AppSecret，无法发送')
            return 'nocfg'
        if not users:
            if ALERT_ONLY_USER_IDS or ALERT_ONLY_NAMES:
                log_lines.append('[告警] 白名单内没有可发送的成员（当前只发：%s）'
                                 % '、'.join(ALERT_ONLY_NAMES or ALERT_ONLY_USER_IDS))
            else:
                log_lines.append('[告警] 没有启用中的推送人')
            return 'nouser'

        client = dt.DingTalkClient(cfg['app_key'], cfg['app_secret'],
                                   cfg.get('robot_code'), cfg.get('agent_id'))
        ok, bad = 0, []
        for u in users:
            uid = (u.get('user_id') or '').strip()
            name = u.get('name') or ('id=%s' % u.get('id'))
            try:
                if not uid:
                    uid = client.get_userid_by_mobile((u.get('mobile') or '').strip())
                    with conn.cursor() as cur:
                        cur.execute('UPDATE dingtalk_push_users SET user_id=%s WHERE id=%s',
                                    (uid, u.get('id')))
                    conn.commit()
                r = client.send_markdown([uid], title, text)
                if (r or {}).get('invalidStaffIdList'):
                    raise Exception('不在应用可见范围内')
                ok += 1
            except Exception as e:
                bad.append('%s: %s' % (name, e))
        log_lines.append('[告警] 送达 %d/%d %s' % (ok, len(users), '；'.join(bad)))
        return 'ok' if ok == len(users) else ('partial' if ok else 'fail')
    except Exception as e:
        log_lines.append('[告警] 发送异常: %s' % e)
        return 'error'


def build_alert_md(date_str, result, round_no):
    head = ('已完成 **%d** 轮对账补抓，以下账号仍 **运营中但无数据**：' % round_no
            if round_no else '本次仅对账未补抓，以下账号 **运营中但无数据**：')
    lines = ['### ⚠️ 抓取对账未通过 · %s' % date_str, '', head, '']
    for plat in result.values():
        if not plat['missing']:
            continue
        lines.append('**%s**（%d/%d 家未落库）'
                     % (plat['display'], len(plat['missing']), len(plat['active'])))
        for s in plat['missing'][:20]:
            lines.append('- %s' % (s['label'] or s['shop_id']))
        if len(plat['missing']) > 20:
            lines.append('- …另有 %d 家' % (len(plat['missing']) - 20))
        lines.append('')
    lines.append('> 常见原因：登录态失效（需人工过滑块刷新）、账号被限流、店铺已下架。')
    lines.append('> 口径：店铺账号管理「运营中」列表 vs 店铺营销数据。')
    return '\n'.join(lines)


# ============================ 主流程 ============================

def main():
    ap = argparse.ArgumentParser(description='抓取对账 + 自动补抓')
    ap.add_argument('date', nargs='?', default=None, help='目标日期 YYYY-MM-DD，默认昨天')
    ap.add_argument('--rounds', type=int, default=2, help='补抓轮次上限，默认 2')
    ap.add_argument('--platform', default='', help='只处理指定平台（逗号分隔），默认全部')
    ap.add_argument('--skip-fetch', action='store_true', help='只对账不补抓')
    ap.add_argument('--no-alert', action='store_true', help='不发送钉钉告警')
    ap.add_argument('--dry-run', action='store_true', help='不写库、不告警，只打印')
    a = ap.parse_args()

    date_str = a.date or (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    print('===== %s 抓取对账 目标日期=%s rounds=%d%s ====='
          % (datetime.datetime.now().strftime('%F %T'), date_str, a.rounds,
             ' [dry-run]' if a.dry_run else ''))

    plats = PLATFORMS
    if a.platform:
        want = [x.strip() for x in a.platform.replace('，', ',').split(',') if x.strip()]
        alias = {p['alias']: p['key'] for p in PLATFORMS if p.get('alias')}
        want = [alias.get(w, w) for w in want]
        plats = [p for p in PLATFORMS if p['key'] in want]
        if not plats:
            print('[FAIL] --platform 无匹配（可选：千牛,抖店,京东）')
            return 1

    alert_log = []
    conn = get_conn()
    if not a.dry_run:
        try:
            ensure_log_table(conn)
        except Exception as e:
            print('[warn] 建对账日志表失败: %s' % e)

    try:
        result = reconcile(conn, date_str, plats)
        for v in result.values():
            print('  [对账] %-4s 运营中 %d 家 / 已落库 %d 家 / 缺失 %d 家'
                  % (v['display'], len(v['active']), v['ok'], len(v['missing'])))
            for s in v['missing']:
                print('         - 缺 %s (%s)' % (s['label'], s['shop_id']))
            for s in v['no_id']:
                print('         ! %s 未填店铺ID，无法对账' % s['label'])

        missing_all = [v for v in result.values() if v['missing']]
        final_round = 0
        round_detail = []

        # ---------- 补抓轮次 ----------
        if missing_all and not a.skip_fetch:
            for rnd in range(1, a.rounds + 1):
                final_round = rnd
                print('\n----- 第 %d 轮补抓 -----' % rnd)
                lines = []
                acted = []
                for p in plats:
                    v = result[p['key']]
                    if not v['missing']:
                        continue
                    did, cnt = refetch(p, date_str, v['missing'], lines)
                    if did:
                        acted.append('%s×%d' % (v['display'], cnt))
                print('\n'.join(lines) if lines else '  （无缺失，跳过）')
                round_detail.append('R%d: %s' % (rnd, ' / '.join(acted) or '无动作'))
                if not acted:
                    break

                result = reconcile(conn, date_str, plats)
                for v in result.values():
                    print('  [复检] %-4s 已落库 %d 家 / 仍缺 %d 家'
                          % (v['display'], v['ok'], len(v['missing'])))
                missing_all = [v for v in result.values() if v['missing']]
                if not missing_all:
                    break

        # ---------- 收尾：告警 + 写日志 ----------
        ok_all = not missing_all
        alert_status = ''
        if not ok_all and not a.no_alert and not a.dry_run:
            md = build_alert_md(date_str, result, final_round or a.rounds)
            alert_status = send_alert(conn, '抓取对账未通过 · %s' % date_str, md, alert_log)
            print('\n'.join(alert_log))
        elif not ok_all and a.dry_run:
            print('\n[dry-run] 跳过告警：\n' + build_alert_md(date_str, result,
                                                             final_round or a.rounds))

        if not a.dry_run:
            detail = ' | '.join(round_detail)
            for plat in result.values():
                write_log(conn, date_str, final_round, plat,
                          'refetch' if final_round else 'none', alert_status, detail)

        print('\n============ 对账结论 ============')
        if ok_all:
            print('✅ 三平台运营中店铺全部落库（%s）' % date_str)
        else:
            print('⚠️  仍有 %d 个平台存在未落库店铺（%s）' % (len(missing_all), date_str))
            for v in missing_all:
                print('    %-4s 缺 %d 家：%s'
                      % (v['display'], len(v['missing']),
                         ', '.join(s['label'] for s in v['missing'])))
        return 0 if ok_all else 3
    finally:
        conn.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print('[FAIL] 对账脚本异常: %s' % e)
        sys.exit(1)
