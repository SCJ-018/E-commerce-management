#!/usr/bin/env python3
"""把本地内网 MySQL「数据」库整库同步到腾讯云服务器（覆盖式）。

用法（项目根目录）:
  python .deploy/sync_db.py           全流程：本地导出 -> 上传 -> 导入 -> 核对行数
  python .deploy/sync_db.py --verify  只核对行数（不上传、不导入）

说明：
- 本地连接信息取自 backend/config.py（唯一真源，密码来自 .env），不硬编码
- 用 pymysql 手写导出，规避 Windows 下 mysqldump 中文库名/表名 argv 乱码
- 生成的 SQL 不含 CREATE DATABASE / USE，导入时用 `mysql <库名>` 指定，避免账号建库权限问题
- 远程 SQL 用单引号包裹（反引号在双引号里会被 shell 当命令替换执行，会静默失败）
"""
import gzip
import os
import sys
from decimal import Decimal
from datetime import date, datetime as dt, time, timedelta

import paramiko
import pymysql

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'backend'))
import config as local_config  # noqa: E402

HOST = "119.45.187.154"
USER = "root"
PWD = "pcl520526."
PORT = 22

SRV_DB_USER = 'ecom'
SRV_DB_PASS = 'Ecom@2026'

OUT = os.path.join(HERE, 'db_dump.sql')
REMOTE_GZ = '/root/db_dump.sql.gz'
REMOTE_LOG = '/root/db_import.log'
CHUNK = 1000


def local_cfg():
    return {k: v for k, v in local_config.DB_CONFIG.items()
            if k in ('host', 'port', 'user', 'password', 'database', 'charset')}


def connect_remote():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, PORT, USER, PWD, timeout=15, banner_timeout=15, auth_timeout=15)
    return c


def remote_run(c, cmd, timeout=1800, show=True):
    _in, out, err = c.exec_command(cmd, timeout=timeout)
    rc = out.channel.recv_exit_status()
    o = out.read().decode('utf-8', 'replace')
    e = err.read().decode('utf-8', 'replace')
    if show:
        if o.strip():
            print(o.strip())
        if e.strip():
            print('[stderr]', e.strip())
    return rc, o, e


def remote_mysql_int(c, db, sql):
    """在服务器上执行一条返回单个数字的 SQL（单引号包裹，反引号才不会被 shell 吃掉）"""
    cmd = ("mysql -u%s -p'%s' -N --default-character-set=utf8mb4 %s -e '%s' 2>/dev/null"
           % (SRV_DB_USER, SRV_DB_PASS, db, sql))
    _rc, out, _e = remote_run(c, cmd, show=False)
    try:
        return int(out.strip().splitlines()[-1])
    except Exception:
        return -1


def table_names(conn, db):
    cur = conn.cursor()
    cur.execute(
        "SELECT TABLE_NAME FROM information_schema.tables "
        "WHERE table_schema=%s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME", (db,))
    return [r[0] for r in cur.fetchall()]


def count_local():
    cfg = local_cfg()
    conn = pymysql.connect(**cfg)
    names = table_names(conn, cfg['database'])
    counts = {}
    for t in names:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM `%s`" % t)
        counts[t] = cur.fetchone()[0]
    conn.close()
    print('本地 %s@%s 库 %s：%d 张表' % (cfg['user'], cfg['host'], cfg['database'], len(counts)))
    return cfg['database'], counts


def lit(conn, v):
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
    if isinstance(v, dt):
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


def dump_local(db, counts):
    cfg = local_cfg()
    conn = pymysql.connect(**cfg)
    cur = conn.cursor()
    cur.execute(
        "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.tables "
        "WHERE table_schema=%s ORDER BY TABLE_NAME", (db,))
    tables = cur.fetchall()

    with open(OUT, 'w', encoding='utf-8', buffering=1 << 20) as f:
        f.write("SET NAMES utf8mb4;\n")
        f.write("SET FOREIGN_KEY_CHECKS = 0;\n")
        f.write("SET UNIQUE_CHECKS = 0;\n")
        f.write("SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\n\n")

        for tname, ttype in tables:
            if ttype == 'VIEW':
                cur.execute("SHOW CREATE VIEW `%s`" % tname)
                _, ddl = cur.fetchone()
                f.write("DROP VIEW IF EXISTS `%s`;\n%s;\n\n" % (tname, ddl))
                continue

            cur.execute("SHOW CREATE TABLE `%s`" % tname)
            _, ddl = cur.fetchone()
            f.write("DROP TABLE IF EXISTS `%s`;\n%s;\n" % (tname, ddl))

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
                f.write(',\n'.join('(' + ','.join(lit(conn, v) for v in row) + ')'
                                   for row in rows) + ';\n')
                n += len(rows)
            dcur.close()
            counts[tname] = n
            print('  dump %-28s %7d rows' % (tname, n))

        f.write("\nSET FOREIGN_KEY_CHECKS = 1;\n")
        f.write("SET UNIQUE_CHECKS = 1;\n")

    conn.close()
    print('导出完成：%s（%.1f MB）' % (OUT, os.path.getsize(OUT) / 1024 / 1024))

    gz = OUT + '.gz'
    with open(OUT, 'rb') as fin, gzip.open(gz, 'wb', compresslevel=6) as fout:
        while True:
            b = fin.read(1 << 20)
            if not b:
                break
            fout.write(b)
    print('压缩完成：%s（%.1f MB）' % (gz, os.path.getsize(gz) / 1024 / 1024))
    return gz


def verify(c, db, counts):
    print('\n=== 行数核对（本地 vs 服务器）===')
    bad = []
    for tname, n in sorted(counts.items()):
        rn = remote_mysql_int(c, db, "SELECT COUNT(*) FROM `%s`" % tname)
        ok = (rn == n)
        if not ok:
            bad.append((tname, n, rn))
        print('  %s %-28s 本地 %7d  服务器 %7d' % ('OK  ' if ok else 'DIFF', tname, n, rn))

    total = remote_mysql_int(
        c, db,
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()")
    print('\n服务器表总数：%s（本地 %d）' % (total, len(counts)))
    if bad:
        print('以下 %d 张表行数不一致：' % len(bad))
        for t, n, rn in bad:
            print('  %s  本地 %d / 服务器 %d' % (t, n, rn))
    else:
        print('全部表行数一致 ✓')
    return bad


def main():
    verify_only = '--verify' in sys.argv
    db, counts = count_local()

    if verify_only:
        c = connect_remote()
        try:
            bad = verify(c, db, counts)
        finally:
            c.close()
        sys.exit(1 if bad else 0)

    gz = dump_local(db, counts)
    c = connect_remote()
    try:
        sftp = c.open_sftp()
        try:
            sftp.put(gz, REMOTE_GZ)
        finally:
            sftp.close()
        print('已上传 ->', REMOTE_GZ)

        cmd = ("gunzip -c %s | mysql -u%s -p'%s' --default-character-set=utf8mb4 %s "
               "2> %s; echo EXIT=$?" % (REMOTE_GZ, SRV_DB_USER, SRV_DB_PASS, db, REMOTE_LOG))
        _rc, o, _e = remote_run(c, cmd, timeout=3600)
        print('导入返回：', o.strip())

        _rc2, o2, _e2 = remote_run(c, 'grep -v "Using a password" %s | tail -5' % REMOTE_LOG, show=False)
        if o2.strip():
            print('导入日志尾部：\n' + o2.strip())

        bad = verify(c, db, counts)
    finally:
        c.close()

    print('\n同步完成' + ('（有 %d 张表不一致，见上）' % len(bad) if bad else '，全部一致'))


if __name__ == '__main__':
    main()
