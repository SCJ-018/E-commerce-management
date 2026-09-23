# -*- coding: utf-8 -*-
"""抖店抓取 —— 账号配置读取（服务器库「抖店账号表」「抖店邮箱账号表」）。

13 个抖店店铺共用 1 个邮箱登录（抖店邮箱账号表），登录后进罗盘切店铺。
- get_active_shops()：运营中的抖店店铺列表（店铺名/店铺ID/品牌）
- get_email_account()：登录邮箱（含 登录状态 JSON 已解析）
- save_email_state()：写回邮箱登录态
"""
import sys
import os
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pymysql

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 服务器库（主库）—— 与千牛 shops.py 完全相同的连接规则
SERVER_DB = {
    'host': '127.0.0.1',      # 脚本在服务器本机跑时用 127.0.0.1
    'port': 3306,
    'user': os.environ.get('FETCH_DB_USER', ''),
    'password': os.environ.get('FETCH_DB_PASSWORD', ''),
    'database': os.environ.get('FETCH_DB_NAME', ''),
    'charset': 'utf8mb4',
}


def _resolve_conn():
    if os.path.exists('/opt/pw'):
        return '127.0.0.1', 3306
    return '127.0.0.1', 3307


def get_conn():
    cfg = dict(SERVER_DB)
    cfg['host'], cfg['port'] = _resolve_conn()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor)


def get_active_shops():
    """运营中的抖店店铺列表。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM `抖店账号表` WHERE `是否运营`=1 ORDER BY `id`")
            return cur.fetchall()
    finally:
        conn.close()


def get_email_account():
    """登录邮箱账号（含 state 解析）。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM `抖店邮箱账号表` WHERE `是否运营`=1 ORDER BY `id` LIMIT 1")
            r = cur.fetchone()
        if r and r.get('登录状态'):
            try:
                r['state'] = json.loads(r['登录状态'])
            except Exception:
                r['state'] = None
        return r
    finally:
        conn.close()


def save_email_state(email, state_obj):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE `抖店邮箱账号表` SET `登录状态`=%s, `状态更新时间`=NOW() WHERE `邮箱`=%s",
                (json.dumps(state_obj, ensure_ascii=False), email))
            conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


if __name__ == '__main__':
    shops = get_active_shops()
    print('运营中抖店店铺数：', len(shops))
    for s in shops:
        print('  -', s['店铺名'], '| 店铺ID', s['店铺ID'], '| 品牌', s['品牌'])
    acc = get_email_account()
    if acc:
        print('\n登录邮箱:', acc['邮箱'], '| state:', '有' if acc.get('state') else '无')
