# -*- coding: utf-8 -*-
"""千牛抓取 —— 店铺账号配置读取（从服务器库「千牛账号表」读）。

供登录脚本 / 取数脚本共用。
- get_active_shops()：返回所有「是否运营=1」的千牛账号
- get_shop(key)：按账号名精确取单店

数据库：直接连服务器库（腾讯云 119.45.187.154 主库）。
"""
import sys
import os
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pymysql

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 服务器库（主库）
SERVER_DB = {
    'host': '127.0.0.1',      # 脚本在服务器本机跑时用 127.0.0.1
    'port': 3306,
    'user': 'ecom',
    'password': 'Ecom@2026',
    'database': '数据',
    'charset': 'utf8mb4',
}

# 连接目标统一是「云数据库」= 腾讯云服务器(119.45.187.154)上的 MySQL：
#   - 脚本在服务器上跑（存在 /opt/pw 目录）→ 直连 127.0.0.1:3306（服务器本机）
#   - 本地跑 → 走 SSH 隧道 127.0.0.1:3307（先跑 `python .deploy/ssx.py tunnel` 建隧道）
# 内网自建库 192.168.2.10 已废弃，不再使用。
import socket


def _resolve_conn():
    if os.path.exists('/opt/pw'):
        return '127.0.0.1', 3306
    return '127.0.0.1', 3307


def get_conn():
    cfg = dict(SERVER_DB)
    cfg['host'], cfg['port'] = _resolve_conn()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor)


def get_active_shops():
    """返回运营中的千牛账号列表（含 登录状态 JSON 已解析）。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM `千牛账号表` WHERE `是否运营`=1 ORDER BY `id`")
            rows = cur.fetchall()
        for r in rows:
            r['state'] = None
            if r.get('登录状态'):
                try:
                    r['state'] = json.loads(r['登录状态'])
                except Exception:
                    r['state'] = None
        return rows
    finally:
        conn.close()


def get_shop(account):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM `千牛账号表` WHERE `账号`=%s", (account,))
            r = cur.fetchone()
        if r and r.get('登录状态'):
            try:
                r['state'] = json.loads(r['登录状态'])
            except Exception:
                r['state'] = None
        return r
    finally:
        conn.close()


def save_state(account, state_obj):
    """把 storage_state JSON 写回账号表。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE `千牛账号表` SET `登录状态`=%s, `状态更新时间`=NOW() WHERE `账号`=%s",
                (json.dumps(state_obj, ensure_ascii=False), account))
            conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


if __name__ == '__main__':
    shops = get_active_shops()
    print('运营中千牛账号数：', len(shops))
    for s in shops:
        print('  -', s['账号'], '| 店铺ID', s['店铺ID'], '| 品牌', s['品牌'], '| state:', '有' if s['state'] else '无')
