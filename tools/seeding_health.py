# -*- coding: utf-8 -*-
"""种草抓取健康检查 + 钉钉告警。

【要解决的问题】
「种草监测中台」每天 09:00~19:00 每 30 分钟自动抓一次抖音/小红书作品数据，但抓取失败时：
  · 后端只在日志里 print 一行，没有任何告警；
  · 作品数据文件一直不更新，页面上还是旧数据，肉眼看不出来；
  · 结果：2026-09-17 体检发现两个平台的数据已静默停更 15 天
    （小红书登录态失效、抖音被 Argus 风控 403），整个团队无人知晓。
本脚本就是补上这个缺口：让「失败」变成一条推送到人的消息。

【判据】任一平台命中即告警（以数据有没有产出为最终标准，不看脚本退出码）：
  0.  status=skipped             —— 该平台没有可抓账号，本轮主动跳过 → **不算异常，直接健康**
  1.  进度文件 status=error      —— 抓取脚本自己报告失败
  1.5 status=done 但 ok<accounts —— 部分账号没抓到（数据是真的，但要知道）
  2.  status=running 超 45 分钟  —— 进程静默死掉，来不及写状态（或卡死）
  3.  作品数据落后「最近一次应抓取时刻」> 3 小时 —— 最终判据：没有按计划产出新数据
  4.  作品数据文件不存在
  ⚠ 因为夜间（19:00 → 次日 09:00）不抓，判据 3 以「最近一次应抓取时刻」为参照，
    不能用「距今」——否则每天早上开跑前必然误报「停更 14 小时」。

【去重】异常内容不变时不重复打扰：同一异常 6 小时内只发一次；
        异常消失后立刻发一条「已恢复」，然后清空状态。

【接收人】只发下面白名单（与 tools/fetch_reconcile.py 的 ALERT_ONLY_* 口径一致）。
        ⚠️ dingtalk_push_users 表是和「每日数据分析日报」共用的，
           所以在这里过滤，**不要改表的 enabled 字段**（那会连带改掉日报收件人）。

【用法】
  python3 tools/seeding_health.py --dry-run   # 只体检打印，不发消息（上线前先跑这个）
  python3 tools/seeding_health.py             # 体检 + 按去重规则推送
  python3 tools/seeding_health.py --force     # 忽略冷却期，强制推送当前问题
  python3 tools/seeding_health.py --reset     # 清空去重状态（下次必定推送）

【退出码】
  0 = 健康
  3 = 有异常（已按去重规则处理告警）
  1 = 脚本自身异常
"""
import argparse
import datetime
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_SERVER = os.path.exists('/opt/ecom')

# ============================ 判据阈值 ============================
# 定时抓取间隔（秒）：与后端 app.py 的 _SEEDING_AUTO_INTERVAL 保持一致
CYCLE_SEC = 1800
# ★★ 自动抓取时间窗：与后端 app.py 的 _SEEDING_WINDOW_START / _SEEDING_WINDOW_END 保持一致。
#   只在 09:00~19:00 之间每半小时抓一轮，夜间不抓 —— 所以「停更」不能按
#   「距今多少小时」算，否则每天早上 09:00 都必然误报一次「停更 14 小时」。
WINDOW_START = 9
WINDOW_END = 19
# 新鲜度参照点缓冲（秒）：与后端 _SEEDING_FRESH_GRACE 保持一致，遮住「刚触发还在跑」的本轮
FRESH_GRACE_SEC = CYCLE_SEC * 4 // 3     # 2400 秒（40 分钟）
# status=running 超过这个时长 → 视为卡死/静默退出
RUNNING_STALE_SEC = int(CYCLE_SEC * 1.5)
# 作品数据落后「最近一次应抓取时刻」超过这个小时数 → 停更（3 小时 ≈ 漏了 6 轮）
STALE_HOURS = 3.0
# 同一异常最短重复提醒间隔（秒）
COOLDOWN_SEC = 6 * 3600

PLATFORMS = [
    {'key': 'douyin', 'label': '抖音', 'works': '_douyin_works.csv'},
    {'key': 'xhs', 'label': '小红书', 'works': '_xhs_works.json'},
]

# 临时静音某个平台（体检照跑，但不产生告警）。例：抖音 403 已知且短期内不修，
# 不想每 6 小时被打扰一次 → SKIP_PLATFORMS = ['douyin']  ← 知道自己在做什么再开
SKIP_PLATFORMS = []

STATE_FILE = os.path.join(BASE_DIR, '_seeding_alert_state.json')

# ============================ 告警接收人 ============================
# 只发这些成员（user_id 命中优先，其次按 name 匹配）。
# 两个列表都为空 = 不限制，发给所有启用成员（不建议）。
ALERT_ONLY_USER_IDS = ['17841605101729565']   # 李自豪
ALERT_ONLY_NAMES = ['李自豪']

# 钉钉应用 Client ID（会被 dingtalk_push_config.app_key 覆盖，这里只是兜底）
_PUSH_DEFAULT_APP_KEY = 'dingjxvfpxfrgbgxrbyq'


def _in_alert_whitelist(u):
    uid = (u.get('user_id') or '').strip()
    nm = (u.get('name') or '').strip()
    return bool((uid and uid in ALERT_ONLY_USER_IDS)
                or (nm and nm in ALERT_ONLY_NAMES))


# ============================ 数据库 ============================
# 与 tools/fetch_reconcile.py 同一套连接规则：服务器直连 3306，本地走 3307 隧道
SERVER_DB = {
    'host': '127.0.0.1',
    'port': 3306,
    'user': 'ecom',
    'password': 'Ecom@2026',
    'database': '数据',
    'charset': 'utf8mb4',
    'connect_timeout': 15,
}


def get_conn():
    import pymysql
    cfg = dict(SERVER_DB)
    cfg['port'] = 3306 if IS_SERVER else 3307
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor)


def _load_dingtalk_module():
    """钉钉客户端在 backend/dingtalk.py（服务器 /opt/ecom/backend/）"""
    cands = ['/opt/ecom/backend', os.path.join(os.path.dirname(BASE_DIR), 'backend')]
    for d in cands:
        if os.path.isfile(os.path.join(d, 'dingtalk.py')):
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                import dingtalk
                return dingtalk
            except Exception as e:
                print('[告警] 导入 dingtalk 失败: %s' % e)
                return None
    return None


# ============================ 体检 ============================
def _fmt_time(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime('%m-%d %H:%M')
    except Exception:
        return '-'


def _read_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _age_hours(path):
    """文件距今小时数；文件不存在返回 None"""
    if not os.path.exists(path):
        return None
    return (time.time() - os.path.getmtime(path)) / 3600.0


def _scheduled_slots(day):
    """某天时间窗内的全部抓取时刻（09:00 起每 CYCLE_SEC，含 19:00 收尾）"""
    slots = []
    t = day.replace(hour=WINDOW_START, minute=0, second=0, microsecond=0)
    end = day.replace(hour=WINDOW_END, minute=0, second=0, microsecond=0)
    step = datetime.timedelta(seconds=CYCLE_SEC)
    while t <= end:
        slots.append(t)
        t = t + step
    return slots


def _last_expected_run(now=None):
    """最近一次「本应已完成」的抓取时刻（本地时间）

    ★ 为什么需要（2026-09-18 时间窗改造）：
      自动抓取只在 09:00~19:00 进行，夜间本来就没有新数据。
      用「数据落后最近一次应抓取时刻多久」判停更，夜间空档自然被排除，不会误报。
    缓冲 FRESH_GRACE_SEC：给刚触发、还在跑的本轮留时间，避免没写完就被判停更。
    ⚠ 与 backend/app.py 的 _seeding_last_expected_run 逻辑必须完全一致。
    """
    now = now or datetime.datetime.now()
    ref = now - datetime.timedelta(seconds=FRESH_GRACE_SEC)
    today = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    for day in (today, today - datetime.timedelta(days=1)):
        slots = [s for s in _scheduled_slots(day) if s <= ref]
        if slots:
            return slots[-1]
    return (today - datetime.timedelta(days=1)).replace(
        hour=WINDOW_END, minute=0, second=0, microsecond=0)


def _count_works(path):
    """作品数据文件里的条数；不存在或读不出返回 0。

    .csv（抖音）= 数据行数；.json（小红书）= 数组长度。
    用于判据 1.6：status=done 但条数为 0 说明"成功"是假的。
    """
    if not os.path.exists(path):
        return 0
    try:
        if path.endswith('.csv'):
            import csv as _csv
            with open(path, 'r', encoding='utf-8-sig', newline='') as f:
                return sum(1 for _ in _csv.DictReader(f))
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0


def _check_platform(p):
    """单平台体检；返回 (items, sig_parts)。items 非空 = 有异常"""
    key, label = p['key'], p['label']
    works_path = os.path.join(BASE_DIR, p['works'])
    progress = _read_json(os.path.join(BASE_DIR, '_seeding_progress_%s.json' % key), {}) or {}
    status = (progress.get('status') or 'idle').strip()
    msg = (progress.get('msg') or '').strip()
    ts = float(progress.get('ts') or 0)
    run_age = (time.time() - ts) if ts else None

    items = []
    sig = [status]

    # 判据 0（新增 2026-09-18）：skipped = 该平台没有可抓账号，本轮主动跳过。
    #   ★ 这是「预期内的不抓」，不是故障：直接判定健康，跳过后面所有判据
    #     （否则会因为数据文件一直不更新而被判「停更」，天天误报）。
    if status == 'skipped':
        print('[体检] %s 本轮跳过抓取（%s），不参与告警判定' % (label, msg or '无可用账号'))
        return [], 'skipped'

    # 判据 1：脚本自己报告失败
    if status == 'error':
        items.append('本轮抓取失败：%s' % (msg or '未提供原因'))
        items.append('（进度：%s/%s，抓取时间 %s）'
                     % (progress.get('done', 0), progress.get('total', 0), _fmt_time(ts)))
        sig.append('error:' + msg[:120])

    # 判据 1.6：status=done 但「一条作品都没抓到」（2026-09-18 补）
    #   ★ 为什么需要：done 看起来是成功，但如果作品数为 0，说明接口能通、
    #     数据却全空（多为 cookie 半失效 / 风控软拦截）。此时数据文件根本没被覆写，
    #     光看 status 会误判为健康；配合判据 3 的数据新鲜度一起看才能发现。
    elif status == 'done':
        rows_now = _count_works(works_path)
        if rows_now == 0:
            items.append('本轮抓取标记为成功，但作品数为 0（数据文件为空或未写入）')
            sig.append('zerorows')

    # 判据 2：running 停留过久 → 进程静默死掉 / 卡死
    elif status == 'running' and run_age is not None and run_age > RUNNING_STALE_SEC:
        items.append('抓取疑似卡死或静默退出：进度停在 running 已 %.0f 分钟（%s 起）'
                     % (run_age / 60.0, _fmt_time(ts)))
        sig.append('stuck')

    # 判据 3 / 4：数据新鲜度 —— 最终判据
    #   ★ 参照点用「最近一次应抓取时刻」而不是「现在」：时间窗之外（夜里）本来就没数据，
    #     若按「距今」判，每天早上 09:00 开跑前都会误报一次「停更 14 小时」。
    age = _age_hours(works_path)
    if age is None:
        items.append('作品数据文件不存在：%s' % p['works'])
        sig.append('nofile')
    else:
        lag = (time.time() - _last_expected_run().timestamp()) / 3600.0
        if lag > STALE_HOURS:
            items.append('作品数据未按计划更新：落后最近一次应抓取时刻 **%.1f 小时**'
                         '（最后更新 %s）'
                         % (lag, _fmt_time(os.path.getmtime(works_path))))
            sig.append('stale')

    return items, '|'.join(sig)


def collect_problems():
    """返回 [{'key','label','items','sig'}]，只含有异常的平台"""
    problems = []
    for p in PLATFORMS:
        if p['key'] in SKIP_PLATFORMS:
            print('[体检] %s 已在 SKIP_PLATFORMS 中，跳过（不告警）' % p['label'])
            continue
        items, sig = _check_platform(p)
        if items:
            problems.append({'key': p['key'], 'label': p['label'], 'items': items, 'sig': sig})
    return problems


def build_alert_md(problems):
    now = datetime.datetime.now()
    lines = ['### ⚠️ 种草抓取异常 · %s' % now.strftime('%m-%d %H:%M'), '']
    for pr in problems:
        lines.append('**%s**' % pr['label'])
        for it in pr['items']:
            lines.append('- %s' % it)
        lines.append('')
    lines.append('---')
    lines.append('自动抓取：每天 09:00~19:00 每 30 分钟一轮；排查看 `tools/_scrape_<平台>.log`、'
                 '`tools/_seeding_progress_<平台>.json`，登录态过期则重新导出 cookie。')
    return '\n'.join(lines)


def build_recover_md(prev_problems):
    now = datetime.datetime.now()
    lines = ['### ✅ 种草抓取已恢复 · %s' % now.strftime('%m-%d %H:%M'), '']
    if prev_problems:
        lines.append('此前异常的平台：%s' % '、'.join(prev_problems))
    lines.append('')
    lines.append('两个平台当前均正常产出数据（作品数据新鲜、上一轮抓取无报错）。')
    return '\n'.join(lines)


# ============================ 发送 ============================
def send_dingtalk(title, md, log=print):
    """推给白名单成员。返回 'success'/'partial'/'fail'/'nouser'/'nocfg'/'nofile'/'error'"""
    dt = _load_dingtalk_module()
    if dt is None:
        log('[告警] 未找到 backend/dingtalk.py，跳过发送')
        return 'nofile'

    cfg = {'app_key': _PUSH_DEFAULT_APP_KEY, 'app_secret': '', 'robot_code': '', 'agent_id': ''}
    users = []
    conn = None
    try:
        conn = get_conn()
    except Exception as e:
        log('[告警] 连接数据库失败: %s' % e)

    if conn is not None:
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute('SELECT cfg_key, cfg_value FROM dingtalk_push_config')
                    for r in cur.fetchall():
                        k = r.get('cfg_key')
                        if k in cfg and r.get('cfg_value') is not None:
                            cfg[k] = r['cfg_value']
                except Exception as e:
                    log('[告警] 读推送配置失败: %s' % e)
                try:
                    cur.execute('SELECT id, name, mobile, user_id FROM dingtalk_push_users '
                                'WHERE enabled = 1 ORDER BY id')
                    users = cur.fetchall()
                except Exception as e:
                    log('[告警] 读推送人失败: %s' % e)
        finally:
            conn.close()

    # ★ 在此过滤接收人：表与「每日数据分析日报」共用，改表会误伤日报
    if ALERT_ONLY_USER_IDS or ALERT_ONLY_NAMES:
        n_before = len(users)
        users = [u for u in users if _in_alert_whitelist(u)]
        log('[告警] 接收人白名单命中 %d/%d 人（跳过 %d 人）'
            % (len(users), n_before, n_before - len(users)))

    if not (cfg.get('app_secret') or '').strip():
        log('[告警] 未配置 AppSecret，无法发送')
        return 'nocfg'
    if not users:
        log('[告警] 白名单内没有可发送的成员（当前只发：%s）'
            % '、'.join(ALERT_ONLY_NAMES or ALERT_ONLY_USER_IDS))
        return 'nouser'

    try:
        client = dt.DingTalkClient(cfg['app_key'], cfg['app_secret'],
                                   cfg.get('robot_code'), cfg.get('agent_id'))
    except Exception as e:
        log('[告警] 构造钉钉客户端失败: %s' % e)
        return 'error'

    ok, bad = 0, []
    for u in users:
        uid = (u.get('user_id') or '').strip()
        name = u.get('name') or ('id=%s' % u.get('id'))
        try:
            if not uid:
                uid = client.get_userid_by_mobile((u.get('mobile') or '').strip())
            r = client.send_markdown([uid], title, md)
            if (r or {}).get('invalidStaffIdList'):
                raise Exception('不在应用可见范围内')
            ok += 1
        except Exception as e:
            bad.append('%s: %s' % (name, e))
    log('[告警] 送达 %d/%d %s' % (ok, len(users), '；'.join(bad)))
    return 'success' if ok == len(users) else ('partial' if ok else 'fail')


# ============================ 去重状态 ============================
def _load_state():
    d = _read_json(STATE_FILE, {}) or {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault('sig', '')
    d.setdefault('ts', 0)
    d.setdefault('platforms', [])
    return d


def _save_state(d):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print('[告警] 写去重状态失败: %s' % e)


# ============================ 主流程 ============================
def main():
    ap = argparse.ArgumentParser(description='种草抓取健康检查 + 钉钉告警')
    ap.add_argument('--dry-run', action='store_true', help='只体检打印，不发消息')
    ap.add_argument('--force', action='store_true', help='忽略冷却期，强制推送当前问题')
    ap.add_argument('--reset', action='store_true', help='清空去重状态后退出')
    args = ap.parse_args()

    if args.reset:
        _save_state({'sig': '', 'ts': 0, 'platforms': []})
        print('[体检] 去重状态已清空（下次发现问题必定推送）')
        return 0

    problems = collect_problems()
    state = _load_state()
    now = time.time()
    sig = ';'.join('%s:%s' % (p['key'], p['sig']) for p in problems)
    prev_sig = state.get('sig') or ''
    last_ts = float(state.get('ts') or 0)
    since_last = now - last_ts if last_ts else None

    print('[体检] %s | 异常平台: %s'
          % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             '、'.join(p['label'] for p in problems) or '无'))
    for p in problems:
        for it in p['items']:
            print('    - [%s] %s' % (p['label'], it))

    if args.dry_run:
        print('[体检] --dry-run：不发送任何消息')
        if problems:
            print('[体检] 若正式运行将推送以下内容：')
            print('-' * 56)
            print(build_alert_md(problems))
            print('-' * 56)
        return 3 if problems else 0

    if problems:
        changed = sig != prev_sig
        cooled = since_last is None or since_last >= COOLDOWN_SEC
        if args.force or changed or cooled:
            reason = ('内容变化' if changed else
                      ('超过冷却期 %.1f 小时' % (since_last / 3600.0) if since_last else '首次'))
            print('[体检] 触发告警（%s）' % reason)
            st = send_dingtalk('⚠️ 种草抓取异常（%d 个平台）' % len(problems),
                               build_alert_md(problems))
            # 只有真的送达才记冷却时间，否则下一轮继续重试
            if st in ('success', 'partial'):
                _save_state({'sig': sig, 'ts': now,
                             'platforms': [p['label'] for p in problems]})
            else:
                print('[体检] 告警未送达（%s），下一轮会重试' % st)
        else:
            print('[体检] 同一异常已提醒过，冷却中（%.1f 小时前），本轮不重复打扰'
                  % (since_last / 3600.0))
        return 3

    # 无异常：若此前有告警，发一条恢复通知并清空状态
    if prev_sig:
        prev_names = state.get('platforms') or []
        print('[体检] 异常已消失，发送恢复通知')
        send_dingtalk('✅ 种草抓取已恢复', build_recover_md(prev_names))
        _save_state({'sig': '', 'ts': 0, 'platforms': []})
    else:
        print('[体检] 一切正常，无需告警')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print('[体检] 运行异常: %s' % e)
        sys.exit(1)
