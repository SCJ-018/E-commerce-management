# -*- coding: utf-8 -*-
"""给指定人发送「后台账号信息」钉钉单聊消息（一次性下发工具）。

设计要点（对应「一定要确保信息和发送人是对应的」这一硬约束）：
  1. 收件人身份 = admin_accounts.account（即手机号），**不靠姓名匹配**
     → 手机号在 admin_accounts 内唯一；用 getbymobile 换出的 userid 直接定位到人。
  2. 换出的 userid 必须与钉钉通讯录反查回来的姓名**一致**，否则拒绝发送（防串号）。
  3. 逐人单发（一次只传 1 个 userId），一人一条独立请求 → 单条失败不影响他人，
     且能逐条判 invalidStaffIdList。
  4. dry-run 模式：只打印将要发送的内容，不真正发送。

用法（在服务器上跑）：
  /opt/ecom/venv/bin/python tools/send_accounts.py --who 李自豪 --dry-run
  /opt/ecom/venv/bin/python tools/send_accounts.py --who 李自豪
  /opt/ecom/venv/bin/python tools/send_accounts.py --mobile 13253557234
"""
import argparse
import json
import sys
import time

import pymysql
import requests

sys.path.insert(0, '/opt/ecom/backend')

SITE = 'julangkeji.site'
APP_KEY = 'dingjxvfpxfrgbgxrbyq'
APP_SECRET = '8DYTyv8Ge70ehgARHoIzr0E_VZvliKORd0nhc1W-y1W6JWncbYNaiCWt94Ox_JTt'
AGENT_ID = '4872122118'
LOG_PATH = '/opt/ecom/tools/_send_accounts_log.jsonl'


# ---------------- 钉钉 ----------------

def get_token():
    r = requests.post('https://api.dingtalk.com/v1.0/oauth2/accessToken',
                      json={'appKey': APP_KEY, 'appSecret': APP_SECRET}, timeout=15)
    d = r.json()
    if not d.get('accessToken'):
        raise SystemExit('!! 换 accessToken 失败: %s' % json.dumps(d, ensure_ascii=False))
    return d['accessToken']


def oapi(path, token, body):
    r = requests.post('https://oapi.dingtalk.com' + path,
                      params={'access_token': token}, json=body, timeout=20)
    return r.json()


def get_userid_by_mobile(token, mobile):
    d = oapi('/topapi/v2/user/getbymobile', token, {'mobile': mobile})
    if d.get('errcode') != 0:
        return None, '%s(%s)' % (d.get('errmsg'), d.get('errcode'))
    uid = (d.get('result') or {}).get('userid')
    return uid, None


def get_user_by_userid(token, userid):
    d = oapi('/topapi/v2/user/get', token, {'userid': userid})
    if d.get('errcode') != 0:
        return None
    return d.get('result') or {}


def send_text(token, robot_code, userid, content):
    """人与机器人单聊发送（只传 1 个 userId）"""
    body = {'robotCode': robot_code, 'userIds': [userid],
            'msgKey': 'sampleText',
            'msgParam': json.dumps({'content': content}, ensure_ascii=False)}
    r = requests.post('https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend',
                      json=body, headers={'x-acs-dingtalk-access-token': token}, timeout=30)
    try:
        d = r.json()
    except Exception:
        return False, 'HTTP %s 非 JSON: %s' % (r.status_code, r.text[:200])
    if r.status_code >= 400 or d.get('code'):
        return False, 'HTTP %s %s' % (r.status_code, d.get('message') or json.dumps(d, ensure_ascii=False))
    inv = d.get('invalidStaffIdList') or []
    if inv:
        return False, '未送达（invalidStaffIdList=%s）—— 成员可能不在应用可见范围内' % inv
    return True, d.get('processQueryKey') or ''


# ---------------- 消息体 ----------------

LINE = '──────────────────'

# 通用尾部：更新掉线提示（全员统一）
TAIL_BASE = (
    LINE + '\n'
    '🔧 系统仍在持续开发中，本后台会不定期更新版本。\n'
    '如遇突然掉线或提示登录失效，属正常现象，重新登录即可继续使用。'
)


def build(name, account, password, role):
    """按角色拼装文案（开发人员档保持原样，其余角色追加专属说明）"""
    head = (
        '【聚浪电商后台 · 账号信息】\n'
        + LINE + '\n'
        '姓名：%s\n'
        '账号：%s\n'
        '密码：%s\n'
        '角色：%s\n'
        + LINE + '\n'
        '后台网址：julangkeji.site\n'
        '登录后可在「个人中心」自行修改密码。\n'
        '请妥善保管，勿转发他人。'
    ) % (name, account, password, role)

    body = ROLE_BODY.get(role, '')
    if body:
        head += '\n' + LINE + '\n' + body
    return head + '\n' + TAIL_BASE


# 角色专属说明（开发人员刻意留空 —— 保持原文案）
ROLE_BODY = {
    '超级管理员':
        '📌 使用提醒\n'
        '请勿随意改动系统内数据，避免影响其他同事的正常使用。',

    '人事行政部':
        '📌 使用提醒\n'
        '请勿随意改动系统内数据，避免影响其他同事的正常使用。',

    '财务部':
        '📌 当前进度\n'
        '财务板块目前为参考样式，功能尚未开发完成，\n'
        '待后续对接后统一开放使用。',

    '种草部':
        '📌 使用说明\n'
        '· 已完善「种草账号列表」功能\n'
        '· 本次开放「AI 文案产出」功能测试，欢迎试用\n'
        '· 抖音数据已完成同步\n'
        '· 小红书当前风控较严，正在协商解决方案，暂停期间请勿频繁尝试\n'
        '\n'
        '📌 使用提醒\n'
        '请勿随意改动系统内数据；\n'
        '如遇问题或有改进建议，请先反馈给主管，\n'
        '由主管统一汇总后反馈给开发人员。',

    '临时账号':
        '📌 使用提醒\n'
        '请勿随意改动系统内数据，避免影响其他同事的正常使用。',
}


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--who', default='', help='按姓名精确筛选')
    ap.add_argument('--mobile', default='', help='按账号(手机号)精确筛选')
    ap.add_argument('--all', action='store_true', help='给全部启用账号发送')
    ap.add_argument('--dry-run', action='store_true', help='只打印不发送')
    ap.add_argument('--yes', action='store_true', help='跳过确认')
    args = ap.parse_args()

    conn = pymysql.connect(host='127.0.0.1', port=3306, user='ecom', password='Ecom@2026',
                           database='数据', charset='utf8mb4', autocommit=True)
    cur = conn.cursor()

    sql = 'SELECT id, name, account, password, role FROM admin_accounts WHERE 1=1'
    pm = []
    if args.who:
        sql += ' AND name = %s'; pm.append(args.who)
    if args.mobile:
        sql += ' AND account = %s'; pm.append(args.mobile)
    if not (args.who or args.mobile or args.all):
        raise SystemExit('请指定 --who / --mobile / --all 之一')
    sql += ' ORDER BY id'
    cur.execute(sql, pm)
    rows = cur.fetchall()
    if not rows:
        raise SystemExit('!! 未找到匹配的账号')
    print('匹配到 %d 条账号：%s' % (len(rows), '、'.join(r[1] for r in rows)))

    token = get_token()
    robot_code = APP_KEY

    okn = 0
    for aid, name, account, password, role in rows:
        print('\n===== id=%s %s (%s) =====' % (aid, name, account))

        # 1) 手机号 → userid（唯一键，不靠姓名）
        userid, err = get_userid_by_mobile(token, account)
        if not userid:
            print('  ✗ 手机号换 userid 失败：%s  → 跳过' % err)
            continue
        print('  手机号 → userid = %s' % userid)

        # 2) 反查校验：userid 对应的钉钉姓名必须与账号姓名一致
        u = get_user_by_userid(token, userid)
        dt_name = (u or {}).get('name')
        if dt_name != name:
            print('  ✗ 姓名校验失败：账号表=%s / 钉钉=%s  → 拒绝发送（防发错人）' % (name, dt_name))
            continue
        print('  ✓ 姓名校验通过：%s' % dt_name)

        content = build(name, account, password, role)
        if args.dry_run:
            print('  --- 将要发送的内容 ---')
            for line in content.split('\n'):
                print('    ' + line)
            print('  (dry-run，未发送)')
            continue

        oks, info = send_text(token, robot_code, userid, content)
        if oks:
            okn += 1
            print('  ✓ 已发送 processQueryKey=%s' % info)
        else:
            print('  ✗ 发送失败：%s' % info)

        # 台账
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'id': aid, 'name': name,
                                'account': account, 'userid': userid, 'ok': oks, 'info': info},
                               ensure_ascii=False) + '\n')

    if not args.dry_run:
        print('\n=== 发送完成：成功 %d / 共 %d ===' % (okn, len(rows)))
    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
