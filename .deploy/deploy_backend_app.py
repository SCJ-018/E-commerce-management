#!/usr/bin/env python3
"""定向部署后端文件到腾讯云并重启 ecom 服务（不上传前端）。

用法（在项目根目录跑）:
  python .deploy/deploy_backend_app.py                        # 默认 app.py + douyin_hot_scraper.py
  python .deploy/deploy_backend_app.py backend/app.py         # 只部署指定文件

与 deploy.py 的区别：不碰 js/css/index.html，改动只涉及后端时用它，避免把
未验证的前端改动一起带上线。上传前同样备份到 /opt/ecom/.backup/<时间戳>/。
"""
import datetime
import os
import sys

import paramiko

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HOST = "119.45.187.154"
USER = "root"
PWD = "pcl520526."
PORT = 22
REMOTE_ROOT = "/opt/ecom"
SERVICE = "ecom"
DEFAULT_FILES = ["backend/app.py", "tools/douyin_hot_scraper.py"]


def main():
    files = sys.argv[1:] or DEFAULT_FILES
    items = []
    for local in files:
        if not os.path.isfile(local):
            sys.exit('找不到 %s' % local)
        remote = REMOTE_ROOT + '/' + local
        with open(local, 'rb') as f:
            items.append((local, remote, f.read()))
        print('read %s -> %s (%d bytes)' % (local, remote, len(items[-1][2])))

    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = '%s/.backup/%s' % (REMOTE_ROOT, ts)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, PORT, USER, PWD, timeout=15, banner_timeout=15, auth_timeout=15)

    try:
        remotes = ' '.join('"%s"' % r for _, r, _ in items)
        _in, out, err = c.exec_command(
            'mkdir -p "%s" && for f in %s; do [ -e "$f" ] && cp --parents "$f" "%s"/ ; done; echo backup-ok'
            % (backup_dir, remotes, backup_dir), timeout=60)
        out.channel.recv_exit_status()
        print('备份 ->', backup_dir, out.read().decode('utf-8', 'replace').strip())

        sftp = c.open_sftp()
        try:
            for _, remote, data in items:
                with sftp.open(remote, 'wb') as fh:
                    fh.write(data)
                print('up  ', remote, len(data), 'bytes')
        finally:
            sftp.close()

        _in, out, err = c.exec_command(
            'systemctl restart %s && sleep 2 && systemctl is-active %s' % (SERVICE, SERVICE),
            timeout=120)
        out.channel.recv_exit_status()
        print('服务状态:', out.read().decode('utf-8', 'replace').strip())

        _in, out, err = c.exec_command(
            'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/', timeout=30)
        print('本地 5000 端口 HTTP:', out.read().decode('utf-8', 'replace').strip())
    finally:
        c.close()


if __name__ == '__main__':
    main()
