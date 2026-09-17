"""
开发人员异常告警 —— 后台任何报错，直接钉钉单聊「李自豪」。

设计要点
--------
· 收件人不写死：优先读 `dingtalk_push_users` 表里名字含「李自豪」的成员
  （与「钉钉推送」页 / 种草中台用的是同一份名单，换手机号或 userId 在页面上改即可）；
  表里查不到时回落到环境变量 `DEV_ALERT_USER_ID` / `DEV_ALERT_MOBILE`（写在 .env，不改代码就能换人）。
· 通道复用 backend/dingtalk.py 的企业内部应用机器人单聊（与每日报告、抓取告警同一条链路）。
· 一次故障只吵一次：按「签名」做冷却（默认 300 秒），并且每天最多发 DAILY_LIMIT 条，
  超限只打日志；发送失败只 print，绝不再向上抛异常（避免告警自身把服务打挂）。
· 实际发送在后台 daemon 线程里做，接口/请求线程不会被钉钉接口的 RTT 拖住。
· 告警发送过程中再出错会被直接吞掉（线程局部标记），不会递归触发。

调用方式（业务代码只需一行）
--------------------------
    from dev_alert import notify_dev, notify_dev_exception   # 或 app.py 里的 _dev_alert()
    notify_dev('抓取脚本失败', detail='...', signature='script:xxx.py', source='抓取')
    notify_dev_exception('种草自动更新', e)                  # 自动带堆栈
"""

import os
import threading
import time
import traceback

# 开发人员（收件人）——表里按这个名字模糊匹配
DEV_NAME = os.environ.get('DEV_ALERT_NAME', '李自豪').strip() or '李自豪'

COOLDOWN_SECONDS = int(os.environ.get('DEV_ALERT_COOLDOWN', '300') or 300)  # 同签名冷却
DAILY_LIMIT = int(os.environ.get('DEV_ALERT_DAILY_LIMIT', '100') or 100)    # 每日上限
RECIPIENT_TTL = 600        # 收件人缓存时长（秒）
MAX_DETAIL = 1400          # 详情截断长度（钉钉 markdown 报文别太长）

# 由 init_dev_alert() 注入（避免 app.py ↔ dev_alert 循环导入）
_db_execute = None
_client_factory = None
_resolve_userid = None

_lock = threading.Lock()
_last_sent = {}                        # 签名 -> 上次发送时间戳
_day = {'key': '', 'count': 0}         # 当日计数
_recipient_cache = {'ts': 0.0, 'row': None, 'miss': False}
_tls = threading.local()               # 防递归标记


def init_dev_alert(db_execute, client_factory, resolve_userid):
    """注入 app.py 里的依赖：SQL 执行器、钉钉客户端工厂、userId 解析器"""
    global _db_execute, _client_factory, _resolve_userid
    _db_execute = db_execute
    _client_factory = client_factory
    _resolve_userid = resolve_userid
    print('[开发告警] 已启用：后台报错将钉钉通知「%s」' % DEV_NAME)


# ======================== 收件人 ========================

def dev_recipient(force=False):
    """返回收件人 dict（dingtalk_push_users 的行结构），找不到返回 None"""
    now = time.time()
    if not force and _recipient_cache['row'] and now - _recipient_cache['ts'] < RECIPIENT_TTL:
        return _recipient_cache['row']

    row = None
    if _db_execute is not None:
        try:
            rows = _db_execute(
                'SELECT id, name, mobile, user_id FROM dingtalk_push_users '
                'WHERE name LIKE %s ORDER BY id LIMIT 1', ['%' + DEV_NAME + '%']) or []
            if rows:
                row = rows[0]
        except Exception as e:
            print('[开发告警] 读取收件人失败: %s' % e)

    if not row:
        uid = (os.environ.get('DEV_ALERT_USER_ID') or '').strip()
        mobile = (os.environ.get('DEV_ALERT_MOBILE') or '').strip()
        if uid or mobile:
            row = {'id': 0, 'name': DEV_NAME, 'mobile': mobile, 'user_id': uid}

    _recipient_cache['ts'] = now
    _recipient_cache['row'] = row
    if not row:
        print('[开发告警] 未找到收件人：dingtalk_push_users 里没有名字含「%s」的成员，'
              '也没配 DEV_ALERT_USER_ID/DEV_ALERT_MOBILE' % DEV_NAME)
    return row


# ======================== 对外入口 ========================

def notify_dev(title, detail='', signature=None, source='', force=False):
    """投递一条告警（非阻塞）。返回 True 表示已进入发送队列。"""
    try:
        if getattr(_tls, 'active', False):
            return False  # 告警链路上的二次异常：吞掉，防止递归
        sig = signature or ('%s|%s' % (source, title))
        now = time.time()
        with _lock:
            today = time.strftime('%Y-%m-%d')
            if _day['key'] != today:
                _day['key'] = today
                _day['count'] = 0
            if not force:
                if now - _last_sent.get(sig, 0) < COOLDOWN_SECONDS:
                    return False
                if _day['count'] >= DAILY_LIMIT:
                    print('[开发告警] 今日告警已达上限 %d 条，丢弃：%s' % (DAILY_LIMIT, title))
                    return False
            _last_sent[sig] = now
            _day['count'] += 1
        threading.Thread(target=_send, args=(title, detail, source),
                         daemon=True, name='dev-alert-send').start()
        return True
    except Exception as e:
        print('[开发告警] 入队失败: %s' % e)
        return False


def notify_dev_exception(source, exc, extra='', signature=None):
    """异常专用入口：自动带上异常类型与堆栈"""
    try:
        title = '%s：%s' % (source or '运行异常', exc)
        if len(title) > 120:
            title = title[:120] + '…'
        detail = ''
        if extra:
            detail += extra.rstrip() + '\n\n'
        detail += ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return notify_dev(title, detail,
                          signature=signature or ('exc:%s:%s' % (source, type(exc).__name__)),
                          source=source)
    except Exception:
        return False


# ======================== 发送 ========================

def _compose(title, detail, source):
    lines = [
        '### 【系统告警】%s' % title,
        '',
        '- **时间**：%s' % time.strftime('%Y-%m-%d %H:%M:%S'),
        '- **服务**：电商后台管理（julangkeji.site）',
        '- **来源**：%s' % (source or '后台服务'),
    ]
    lines.append('')
    lines.append('**详情**')
    lines.append('')
    text = (detail or '(无附加信息)').strip()
    if len(text) > MAX_DETAIL:
        text = text[:MAX_DETAIL] + '\n…（已截断）'
    lines.append('```')
    lines.append(text)
    lines.append('```')
    return '\n'.join(lines)


def _send(title, detail, source):
    _tls.active = True
    try:
        if _client_factory is None or _resolve_userid is None:
            print('[开发告警] 模块未初始化（init_dev_alert 未调用），告警未发送：%s' % title)
            return
        row = dev_recipient()
        if not row:
            return
        client = _client_factory()
        uid, err = _resolve_userid(client, row)
        if err or not uid:
            print('[开发告警] 解析「%s」的 userId 失败: %s' % (row.get('name') or DEV_NAME, err or '空'))
            return
        head = '【系统告警】' + title
        r = client.send_markdown([uid], head[:100], _compose(title, detail, source))
        if (r or {}).get('invalidStaffIdList'):
            print('[开发告警] 发送失败：%s 不在应用可见范围内' % (row.get('name') or DEV_NAME))
            return
        print('[开发告警] 已推送钉钉：%s' % title)
    except Exception as e:
        print('[开发告警] 发送失败: %s' % e)
    finally:
        _tls.active = False
