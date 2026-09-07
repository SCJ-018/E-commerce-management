# -*- coding: utf-8 -*-
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend'))
from config import DB_CONFIG
import pymysql

conn = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()
for t in ['店铺营销数据', '品类营销数据', '千牛单链接推广数据表', 'daily_analysis_reports', '天猫竞品表', '天猫榜单表', '抖音热搜榜单表']:
    cur.execute("SHOW COLUMNS FROM `%s`" % t)
    cols = cur.fetchall()
    print("=== %s (%d列) ===" % (t, len(cols)))
    for c in cols:
        print("  %-28s %s" % (c['Field'], c['Type']))
    print()
conn.close()
