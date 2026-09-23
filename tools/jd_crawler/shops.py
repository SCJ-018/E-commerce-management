# -*- coding: utf-8 -*-
"""京东抓取 —— 账号配置读取（服务器库「京东账号表」）。

京东只有 1 家店：西西猫旗舰店（店铺ID 270373803），登录账号 `西西猫采集1`。
- get_active_shops()：运营中的京东店铺列表
- get_jd_account()：登录账号（含 登录状态 JSON 已解析）
- save_jd_state()：写回登录态
"""
import sys
import os
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pymysql

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 服务器库（主库）—— 与千牛/抖店 shops.py 完全相同的连接规则
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
    """运营中的京东店铺列表。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM `京东账号表` WHERE `是否运营`=1 ORDER BY `id`")
            return cur.fetchall()
    finally:
        conn.close()


def get_jd_account():
    """登录账号（含 state 解析）。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM `京东账号表` WHERE `是否运营`=1 ORDER BY `id` LIMIT 1")
            r = cur.fetchone()
        if r and r.get('登录状态'):
            try:
                r['state'] = json.loads(r['登录状态'])
            except Exception:
                r['state'] = None
        return r
    finally:
        conn.close()


def save_jd_state(account, state_obj):
    """写回登录态（按账号名匹配）。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE `京东账号表` SET `登录状态`=%s, `状态更新时间`=NOW() WHERE `账号`=%s",
                (json.dumps(state_obj, ensure_ascii=False), account))
            conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


if __name__ == '__main__':
    for s in get_active_shops():
        print('店铺:', s['店铺名'], '| ID', s['店铺ID'], '| 账号', s['账号'])
    acc = get_jd_account()
    if acc:
        print('账号:', acc['账号'], '| state:', '有' if acc.get('state') else '无')
