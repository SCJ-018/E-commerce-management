#!/usr/bin/env python3
"""纯 pymysql 导出（绕开 Windows mysqldump 中文库名/表名 argv 乱码）。
生成 UTF-8 的 db_dump.sql：CREATE DATABASE + 建表 DDL + 扩展 INSERT。
"""
import sys
import pymysql
from datetime import date, datetime, time, timedelta
from decimal import Decimal

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SRC = dict(host='192.168.2.10', user='root', password='123456', charset='utf8mb4')
OUT = 'db_dump.sql'
CHUNK = 1000

conn = pymysql.connect(**SRC)
cur = conn.cursor()

# 业务库名
cur.execute("SHOW DATABASES")
system = {'information_schema', 'mysql', 'performance_schema', 'sys'}
dbs = [r[0] for r in cur.fetchall() if r[0] not in system]
assert len(dbs) == 1, '预期 1 个业务库，实际 ' + str(dbs)
db = dbs[0]
conn.select_db(db)
print('库名:', db)

def lit(v):
    if v is None:
        return 'NULL'
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, datetime):
        return "'" + v.strftime('%Y-%m-%d %H:%M:%S') + "'"
    if isinstance(v, date):
        return "'" + v.strftime('%Y-%m-%d') + "'"
    if isinstance(v, time):
        return "'" + v.strftime('%H:%M:%S') + "'"
    if isinstance(v, timedelta):
        return "'" + str(v) + "'"
    if isinstance(v, bytes):
        return "X'" + v.hex() + "'"
    if isinstance(v, str):
        return "'" + conn.escape_string(v) + "'"
    return "'" + conn.escape_string(str(v)) + "'"

# 表清单（基表 + 视图）
cur.execute(
    "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.tables WHERE table_schema=%s ORDER BY TABLE_NAME",
    (db,))
tables = cur.fetchall()

f = open(OUT, 'w', encoding='utf-8')
f.write("SET NAMES utf8mb4;\n")
f.write("SET FOREIGN_KEY_CHECKS = 0;\n")
f.write("SET UNIQUE_CHECKS = 0;\n\n")
f.write("CREATE DATABASE IF NOT EXISTS `%s` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n" % db)
f.write("USE `%s`;\n\n" % db)

for tname, ttype in tables:
    print('  dump', ttype, tname, end=' ')
    if ttype == 'VIEW':
        cur.execute("SHOW CREATE VIEW `%s`" % tname)
        _, ddl = cur.fetchone()
        f.write("DROP VIEW IF EXISTS `%s`;\n" % tname)
        f.write(ddl + ";\n\n")
        continue
    cur.execute("SHOW CREATE TABLE `%s`" % tname)
    _, ddl = cur.fetchone()
    f.write("DROP TABLE IF EXISTS `%s`;\n" % tname)
    f.write(ddl + ";\n")

    # 数据（流式读）
    dcur = conn.cursor(pymysql.cursors.SSCursor)
    dcur.execute("SELECT * FROM `%s`" % tname)
    cols = [d[0] for d in dcur.description]
    collist = '`' + '`,`'.join(cols) + '`'
    n = 0
    while True:
        rows = dcur.fetchmany(CHUNK)
        if not rows:
            break
        f.write("INSERT INTO `%s` (%s) VALUES\n" % (tname, collist))
        vals = []
        for row in rows:
            vals.append('(' + ','.join(lit(v) for v in row) + ')')
        f.write(',\n'.join(vals) + ';\n')
        n += len(rows)
    dcur.close()
    print('(%d rows)' % n)

f.write("\nSET FOREIGN_KEY_CHECKS = 1;\n")
f.write("SET UNIQUE_CHECKS = 1;\n")
f.close()
conn.close()
print('完成 ->', OUT)
