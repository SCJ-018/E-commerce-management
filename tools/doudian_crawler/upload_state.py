# -*- coding: utf-8 -*-
"""把本地 _states/ 下的抖店 state 上传到服务器写「抖店邮箱账号表.登录状态」。

用法：
  python upload_state.py
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
MYSQL_PWD = os.environ.get('FETCH_DB_PASSWORD', '')
MYSQL_DB = os.environ.get('FETCH_DB_NAME', '')

EMAIL = 'pcl526@yeah.net'


def main():
    fname = '抖店_' + EMAIL.replace('@', '_').replace('.', '_') + '.json'
    p = os.path.join(STATE_DIR, fname)
    if not os.path.exists(p):
        print('本地无 state 文件:', p)
        return

    with open(p, encoding='utf-8') as f:
        state = json.load(f)
    state_json = json.dumps(state, ensure_ascii=False)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(SSH_HOST, 22, SSH_USER, SSH_PWD, timeout=15, banner_timeout=15, auth_timeout=15)
    sftp = c.open_sftp()
    try:
        c.exec_command('mkdir -p %s' % REMOTE_DIR)[1].channel.recv_exit_status()
        remote_path = '%s/%s' % (REMOTE_DIR, fname)
        sftp.put(p, remote_path)

        esc = state_json.replace("\\", "\\\\").replace("'", "''")
        sql = "UPDATE `抖店邮箱账号表` SET `登录状态`='%s', `状态更新时间`=NOW() WHERE `邮箱`='%s';" % (
            esc, EMAIL.replace("'", "''"))
        tmp_local = os.path.join(BASE_DIR, '_tmp_dd_state_%d.sql' % os.getpid())
        with open(tmp_local, 'w', encoding='utf-8') as f:
            f.write(sql)
        tmp_remote = '/tmp/_upd_dd_state_%d.sql' % os.getpid()
        sftp.put(tmp_local, tmp_remote)
        os.remove(tmp_local)
        cmd = "mysql -uecom -p'%s' %s < %s 2>&1" % (MYSQL_PWD, MYSQL_DB, tmp_remote)
        _i, o, e = c.exec_command(cmd, timeout=60)
        o.channel.recv_exit_status()
        err = e.read().decode('utf-8', 'replace').strip()
        if err and 'insecure' not in err:
            print('[warn] 写入异常:', err)
        else:
            print('已写入:', EMAIL, '->', remote_path)

        # 校验
        _i, o, e = c.exec_command(
            "mysql -uecom -p'%s' %s -e \"SELECT `邮箱`, IF(`登录状态` IS NULL OR `登录状态`='', '空', CONCAT('有(', LENGTH(`登录状态`), ')')) st, `状态更新时间` FROM `抖店邮箱账号表`\"" % (MYSQL_PWD, MYSQL_DB),
            timeout=60)
        o.channel.recv_exit_status()
        print(o.read().decode('utf-8', 'replace'))
    finally:
        sftp.close()
        c.close()


if __name__ == '__main__':
    main()
