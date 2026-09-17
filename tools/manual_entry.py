# -*- coding: utf-8 -*-
"""手工录入/修正「店铺营销数据」—— 用户在平台后台亲自核对后口述的数据。

为什么存在
    抓取受平台风控影响时（如抖店罗盘 st=11001 账号级限流），某店某日可能
    长时间拿不到数据。用户手工核对后的数值允许直接落库，避免对账永远缺项。

★ 重要：写进去的是**人工值**，不是抓取值。
   脚本会在 `fetch_reconcile_logs` 追加一条 `action='manual'` 的审计记录，
   detail 里写明「字段=值」与来源，便于以后区分「这行是抓来的还是人填的」。

用法（服务器：/opt/pw/venv/bin/python；本机：系统 python 走 3307 隧道）
    # 古豪汽车脚垫 2026-09-16，全部指标写 0（默认就是全 0）
    python manual_entry.py --platform douyin --shop-id 258352127 --date 2026-09-16

    # 只覆盖部分字段（其余仍为 0）
    ... --set 访客数=3 --set 支付金额=698

    # 干跑：只打印将要写入的内容，不落库
    ... --dry-run

平台别名：douyin / 抖音 → 平台值「抖音」（抖店账号表）
          qianniu / 千牛 → 「千牛」（千牛账号表，需 --shop-name）
          jd / 京东     → 「京东」（京东账号表，品牌默认 西西猫）
"""
import sys
import os
import json
import argparse
import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'doudian_crawler'))

import pymysql

# 指标字段（除主键外的全部列）；默认 0
FIELDS = [
    '支付金额', '净支付金额', '访客数', '支付买家数', '支付转化率',
    '退款金额', '订单退款率', '客单价',
    '推广花费', '推广总成交', '推广净成交',
]

PLATFORMS = {
    'douyin':  ('抖音', '抖店账号表'),
    '抖音':    ('抖音', '抖店账号表'),
    'qianniu': ('千牛', '千牛账号表'),
    '千牛':    ('千牛', '千牛账号表'),
    'jd':      ('京东', '京东账号表'),
    '京东':    ('京东', '京东账号表'),
}

BRAND_DEFAULT = {'京东': '西西猫'}   # 京东账号表没有「品牌」列（与抓取脚本一致）


def get_conn():
    """服务器上 /opt/pw 存在 → 直连 3306；否则（本机）走 SSH 隧道 3307。"""
    host, port = ('127.0.0.1', 3306) if os.path.exists('/opt/pw') else ('127.0.0.1', 3307)
    return pymysql.connect(host=host, port=port, user='ecom', password='Ecom@2026',
                           database='数据', charset='utf8mb4',
                           cursorclass=pymysql.cursors.DictCursor)


def lookup_shop(conn, plat, table, shop_id, shop_name, brand):
    """从平台账号表补齐 店铺名 / 品牌 / 店铺ID。"""
    with conn.cursor() as cur:
        if shop_id:
            cur.execute("SELECT * FROM `%s` WHERE `店铺ID`=%%s" % table, (shop_id,))
        elif shop_name:
            cols = [c['Field'] for c in _columns(conn, table)]
            key = '店铺名' if '店铺名' in cols else '账号'
            cur.execute("SELECT * FROM `%s` WHERE `%s`=%%s" % (table, key), (shop_name,))
        else:
            raise SystemExit('[x] 必须给 --shop-id 或 --shop-name')
        row = cur.fetchone()
    if not row:
        raise SystemExit('[x] 账号表里找不到这家店（table=%s shop_id=%s shop_name=%s）'
                         % (table, shop_id, shop_name))
    return {
        '店铺ID': row['店铺ID'],
        '店铺名': row.get('店铺名') or shop_name or row.get('账号'),
        '品牌':   brand or row.get('品牌') or BRAND_DEFAULT.get(plat) or '其他',
    }


def _columns(conn, table):
    with conn.cursor() as cur:
        cur.execute("SHOW COLUMNS FROM `%s`" % table)
        return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--platform', required=True, help='douyin/抖音 | qianniu/千牛 | jd/京东')
    ap.add_argument('--date', required=True, help='YYYY-MM-DD')
    ap.add_argument('--shop-id', default='', help='店铺ID（推荐，避免中文转义问题）')
    ap.add_argument('--shop-name', default='', help='店铺名（千牛账号表没有店铺名列，需用它）')
    ap.add_argument('--brand', default='', help='品牌（覆盖账号表）')
    ap.add_argument('--set', action='append', default=[],
                    metavar='字段=值', help='覆盖某个指标，可重复')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--note', default='用户手工核对后录入')
    a = ap.parse_args()

    if a.platform not in PLATFORMS:
        raise SystemExit('[x] 未知平台：%s（可选 %s）' % (a.platform, ' / '.join(PLATFORMS)))
    plat, table = PLATFORMS[a.platform]

    # 解析 --set
    vals = {f: 0 for f in FIELDS}
    for kv in a.set:
        if '=' not in kv:
            raise SystemExit('[x] --set 需要 字段=值 形式：%s' % kv)
        k, v = kv.split('=', 1)
        k = k.strip()
        if k not in FIELDS:
            raise SystemExit('[x] 未知字段：%s（可用：%s）' % (k, '、'.join(FIELDS)))
        vals[k] = float(v) if '.' in v else int(v)

    conn = get_conn()
    try:
        shop = lookup_shop(conn, plat, table, a.shop_id, a.shop_name, a.brand)
        # 主键 = (店铺ID, 平台, 品牌, 日期)
        cols = ['店铺ID', '平台', '店铺名', '品牌', '日期'] + FIELDS
        argv = [shop['店铺ID'], plat, shop['店铺名'], shop['品牌'], a.date] + \
               [vals[f] for f in FIELDS]
        upd = ', '.join('`%s`=VALUES(`%s`)' % (f, f) for f in FIELDS)
        sql = ("INSERT INTO `店铺营销数据` (%s) VALUES (%s) "
               "ON DUPLICATE KEY UPDATE %s"
               % (', '.join('`%s`' % c for c in cols),
                  ', '.join(['%s'] * len(cols)), upd))

        print('平台=%s 店铺=%s(ID=%s) 品牌=%s 日期=%s'
              % (plat, shop['店铺名'], shop['店铺ID'], shop['品牌'], a.date))
        print('将写入：' + json.dumps(vals, ensure_ascii=False))
        if a.dry_run:
            print('[dry-run] 未落库')
            return 0

        with conn.cursor() as cur:
            cur.execute("SELECT * FROM `店铺营销数据` WHERE `店铺ID`=%s AND `平台`=%s "
                        "AND `品牌`=%s AND `日期`=%s",
                        (shop['店铺ID'], plat, shop['品牌'], a.date))
            before = cur.fetchone()
            cur.execute(sql, argv)
            conn.commit()
            cur.execute("SELECT * FROM `店铺营销数据` WHERE `店铺ID`=%s AND `平台`=%s "
                        "AND `品牌`=%s AND `日期`=%s",
                        (shop['店铺ID'], plat, shop['品牌'], a.date))
            after = cur.fetchone()

        print('[ok] %s（原%s）' % ('更新' if before else '新增',
                                 '有行' if before else '无行'))
        print('落库后：' + json.dumps(
            {k: str(v) for k, v in after.items()} if after else {}, ensure_ascii=False))

        # ── 审计：记录这一行是人工录入的 ──
        detail = json.dumps({'source': a.note, 'values': vals},
                            ensure_ascii=False)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `fetch_reconcile_logs` "
                "(`target_date`,`round_no`,`platform`,`active_count`,`ok_count`,"
                "`missing_shops`,`action`,`final_status`,`alert_status`,`detail`) "
                "VALUES (%s,0,%s,1,1,%s,'manual','ok','none',%s)",
                (a.date, plat, '' if after else shop['店铺名'], detail))
            conn.commit()
        print('[ok] 已在 fetch_reconcile_logs 留审计记录（action=manual）')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
