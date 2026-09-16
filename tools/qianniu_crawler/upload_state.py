# -*- coding: utf-8 -*-
"""把本地 _states/*.json（storage_state）上传到服务器并写入「千牛账号表.登录状态」。

流程：SFTP 上传 state 文件到服务器 /opt/pw/states/ → 服务器端 mysql UPDATE 写库。

用法：
  python upload_state.py            # 上传 _states 下所有 state
  python upload_state.py 贝朵星球母婴用品:螃蟹   # 只传一个
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


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None

    # 收集本地 state 文件
    files = []
    if target:
        fname = target.replace(':', '_').replace('/', '_') + '.json'
        p = os.path.join(STATE_DIR, fname)
        if os.path.exists(p):
            files.append((target, p))
        else:
            print('本地无 state 文件:', p)
            return
    else:
        if not os.path.isdir(STATE_DIR):
            print('无 _states 目录，先跑 login_save_state.py')
            return
        for fname in os.listdir(STATE_DIR):
            if fname.endswith('.json'):
                acct = fname[:-5].replace('_', ':')  # 反推账号（含: 分隔）
                files.append((acct, os.path.join(STATE_DIR, fname)))

    if not files:
        print('无 state 文件可传')
        return

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(SSH_HOST, 22, SSH_USER, SSH_PWD, timeout=15, banner_timeout=15, auth_timeout=15)
    sftp = c.open_sftp()
    try:
        # 确保远程目录存在
        c.exec_command('mkdir -p %s' % REMOTE_DIR)[1].channel.recv_exit_status()

        for acct, local_path in files:
            # 读本地 JSON，检查内容合法
            with open(local_path, encoding='utf-8') as f:
                state = json.load(f)
            state_json = json.dumps(state, ensure_ascii=False)

            remote_name = os.path.basename(local_path)
            remote_path = '%s/%s' % (REMOTE_DIR, remote_name)
            sftp.put(local_path, remote_path)

            # 服务器端写库（用临时 SQL 文件，避免 shell 转义问题）
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
            if err and 'insecure' not in err:
                print('  [warn]', acct, '写入异常:', err)
            else:
                print('  已写入:', acct, '->', remote_path)

        # 校验
        print('\n校验写入结果：')
        _i, o, e = c.exec_command(
            "mysql -uecom -p'%s' %s -e \"SELECT `账号`, IF(`登录状态` IS NULL OR `登录状态`='', '空', CONCAT('有(', LENGTH(`登录状态`), ')')) st, `状态更新时间` FROM `千牛账号表` ORDER BY id\"" % (MYSQL_PWD, MYSQL_DB),
            timeout=60)
        o.channel.recv_exit_status()
        print(o.read().decode('utf-8', 'replace'))
    finally:
        sftp.close()
        c.close()


if __name__ == '__main__':
    main()
