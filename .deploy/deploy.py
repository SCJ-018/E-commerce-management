#!/usr/bin/env python3
"""部署文件到服务器并重启 ecom 服务。用法（在项目根目录跑）: python .deploy/deploy.py"""
import os
import paramiko

HOST = "119.45.187.154"
USER = "root"
PWD = "pcl520526."

# 本地相对路径 -> 远程绝对路径
FILES = {
    'backend/app.py': '/opt/ecom/backend/app.py',
    'index.html': '/opt/ecom/index.html',
    'js/app.js': '/opt/ecom/js/app.js',
}


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, USER, PWD, timeout=15, banner_timeout=15, auth_timeout=15)

    # 备份远程旧文件（可回滚）
    c.exec_command('mkdir -p /opt/ecom/.backup')[1].channel.recv_exit_status()
    for remote in FILES.values():
        bak = remote.replace('/opt/ecom/', '/opt/ecom/.backup/')
        c.exec_command('mkdir -p "%s" && cp "%s" "%s"' % (os.path.dirname(bak), remote, bak))[1].channel.recv_exit_status()

    # 上传
    sftp = c.open_sftp()
    try:
        for local, remote in FILES.items():
            if os.path.exists(local):
                sftp.put(local, remote)
                print('up  ', local, '->', remote)
            else:
                print('skip', local, '(不存在)')
    finally:
        sftp.close()

    # 重启 + 状态
    stdin, stdout, stderr = c.exec_command('systemctl restart ecom && sleep 2 && systemctl is-active ecom', timeout=60)
    rc = stdout.channel.recv_exit_status()
    print('服务状态:', stdout.read().decode('utf-8', 'replace').strip() or stderr.read().decode('utf-8', 'replace').strip())
    c.close()
    print('完成')


if __name__ == '__main__':
    main()
