# -*- coding: utf-8 -*-
"""批量上传这 8 个新店的 storage_state 到云库（一次 SSH 连接）。

用法：python batch_upload_state.py
"""
import sys
import os
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import paramiko

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, '_states')

SSH_HOST = '119.45.187.154'
SSH_USER = 'root'
SSH_PWD = 'pcl520526.'
REMOTE_DIR = '/opt/pw/states'
MYSQL_PWD = 'Ecom@2026'
MYSQL_DB = '数据'

ACCOUNTS = [
    '御车宝道晴专卖店:螃蟹',
    '优比熊旗舰店:螃蟹',
    '御车宝豫鹰专卖店:螃蟹',
    '皇状元汽车用品旗舰店:螃蟹',
    '出胜汽车用品旗舰店:螃蟹',
    '俊徽车品旗舰店:螃蟹',
    '锦耐车品旗舰店:螃蟹',
    '西西猫旗舰店:螃蟹',
]


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(SSH_HOST, 22, SSH_USER, SSH_PWD, timeout=15, banner_timeout=15, auth_timeout=15)
    sftp = c.open_sftp()
    try:
        c.exec_command('mkdir -p %s' % REMOTE_DIR)[1].channel.recv_exit_status()

        for acct in ACCOUNTS:
            fname = acct.replace(':', '_') + '.json'
            lp = os.path.join(STATE_DIR, fname)
            if not os.path.exists(lp):
                print('[缺文件]', acct, lp)
                continue
            with open(lp, encoding='utf-8') as f:
                state = json.load(f)
            state_json = json.dumps(state, ensure_ascii=False)

            rp = '%s/%s' % (REMOTE_DIR, fname)
            sftp.put(lp, rp)

            esc = state_json.replace("\\", "\\\\").replace("'", "''")
            sql = "UPDATE `千牛账号表` SET `登录状态`='%s', `状态更新时间`=NOW() WHERE `账号`='%s';" % (esc, acct.replace("'", "''"))
            tmp_local = os.path.join(BASE_DIR, '_tmp_state_%d.sql' % os.getpid())
            with open(tmp_local, 'w', encoding='utf-8') as f:
                f.write(sql)
            tmp_remote = '/tmp/_upd_state_%d.sql' % os.getpid()
            sftp.put(tmp_local, tmp_remote)
            os.remove(tmp_local)

            cmd = "mysql -uecom -p'%s' %s < %s 2>&1" % (MYSQL_PWD, MYSQL_DB, tmp_remote)
            _i, o, e = c.exec_command(cmd, timeout=60)
            o.channel.recv_exit_status()
            err = e.read().decode('utf-8', 'replace').strip()
            print('写入', acct, '->', 'OK' if (not err or 'insecure' in err) else ('WARN ' + err))

        # 校验
        print()
        _i, o, e = c.exec_command(
            "mysql -uecom -p'%s' %s --default-character-set=utf8mb4 -e \"SELECT id, `账号`, IF(`登录状态` IS NULL OR `登录状态`='','空',CONCAT('有(',LENGTH(`登录状态`),')')) st, `状态更新时间` FROM `千牛账号表` ORDER BY id\"" % (MYSQL_PWD, MYSQL_DB),
            timeout=60)
        o.channel.recv_exit_status()
        print(o.read().decode('utf-8', 'replace'))
    finally:
        sftp.close()
        c.close()


if __name__ == '__main__':
    main()
