#!/usr/bin/env python3
"""部署前端 + 后端到腾讯云服务器并重启 ecom 服务。

用法（在项目根目录跑）: python .deploy/deploy.py

要点：
- 上传前先把服务器上的同名文件备份到 /opt/ecom/.backup/<时间戳>/（保留原目录结构，可整体回滚）
- index.html 部署时自动把本地开发版 Vue 换成线上生产版（vue.global.js -> vue.global.prod.js），本地文件不动
- 不上传 backend/config.py / backend/requirements.txt：服务器上是独立版本（DB 指向 127.0.0.1、requirements 含 gunicorn）
"""
import os
import sys
import datetime

import paramiko

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HOST = "119.45.187.154"
USER = "root"
PWD = "pcl520526."
PORT = 22
REMOTE_ROOT = "/opt/ecom"
SERVICE = "ecom"

# 单文件：本地相对路径 -> 远程绝对路径
FILES = {
    'js/app.js': REMOTE_ROOT + '/js/app.js',
    'css/style.css': REMOTE_ROOT + '/css/style.css',
    'css/hr.css': REMOTE_ROOT + '/css/hr.css',
    'css/content-studio.css': REMOTE_ROOT + '/css/content-studio.css',
    'css/seeding-monitor.css': REMOTE_ROOT + '/css/seeding-monitor.css',
    'backend/app.py': REMOTE_ROOT + '/backend/app.py',
    'backend/content_studio_api.py': REMOTE_ROOT + '/backend/content_studio_api.py',
    'backend/dingtalk.py': REMOTE_ROOT + '/backend/dingtalk.py',
    # 日报长图渲染（钉钉图片消息用）：模块缺失时日报推送会直接失败，必须随部署上线
    'backend/report_image.py': REMOTE_ROOT + '/backend/report_image.py',
    'backend/hr_api.py': REMOTE_ROOT + '/backend/hr_api.py',
    # 天猫榜单周采集：backend/app.py 通过 subprocess 调用，必须与后端一起发布。
    'tools/tmall_ranklist_scraper.py': REMOTE_ROOT + '/tools/tmall_ranklist_scraper.py',
}

# 目录：本地目录 -> 远程目录（递归上传，自动建远程子目录）
DIRS = {
    'js/vue': REMOTE_ROOT + '/js/vue',
    'js/vendor': REMOTE_ROOT + '/js/vendor',
}

LOCAL_INDEX = 'index.html'
REMOTE_INDEX = REMOTE_ROOT + '/index.html'
DEV_VUE = 'src="js/vendor/vue.global.js"'
PROD_VUE = 'src="js/vendor/vue.global.prod.js"'


def build_payloads():
    """收集所有要上传的内容 -> [(remote_path, bytes), ...]"""
    items = []

    for local, remote in FILES.items():
        if not os.path.isfile(local):
            print('skip', local, '(不存在)')
            continue
        with open(local, 'rb') as f:
            items.append((remote, f.read()))
        print('read', local, '->', remote)

    for ldir, rdir in DIRS.items():
        if not os.path.isdir(ldir):
            print('skip', ldir, '(目录不存在)')
            continue
        for root, _dirs, files in os.walk(ldir):
            for fn in files:
                lp = os.path.join(root, fn)
                rel = os.path.relpath(lp, ldir).replace('\\', '/')
                with open(lp, 'rb') as f:
                    items.append((rdir + '/' + rel, f.read()))
                print('read', lp.replace('\\', '/'), '->', rdir + '/' + rel)

    # index.html：部署时换生产版 Vue（本地文件保持开发版不变）
    with open(LOCAL_INDEX, encoding='utf-8') as f:
        html = f.read()
    if DEV_VUE in html:
        html = html.replace(DEV_VUE, PROD_VUE)
        print('note  index.html 的 Vue 已切换为生产版 vue.global.prod.js')
    elif PROD_VUE in html:
        print('note  index.html 已是生产版 Vue')
    else:
        print('warn  index.html 未找到 Vue script 标签，原样上传')
    items.append((REMOTE_INDEX, html.encode('utf-8')))

    return items


def main():
    items = build_payloads()
    if not items:
        sys.exit('没有可上传的文件')

    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = '%s/.backup/%s' % (REMOTE_ROOT, ts)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, PORT, USER, PWD, timeout=15, banner_timeout=15, auth_timeout=15)

    try:
        # 1) 备份服务器同名旧文件（cp --parents 保留目录结构）
        remotes = ' '.join('"%s"' % r for r, _ in items)
        cmd = ('mkdir -p "%s" && for f in %s; do [ -e "$f" ] && cp --parents "$f" "%s"/ ; done; '
               'du -sh "%s" 2>/dev/null' % (backup_dir, remotes, backup_dir, backup_dir))
        _in, out, err = c.exec_command(cmd, timeout=180)
        out.channel.recv_exit_status()
        print('备份 ->', backup_dir, out.read().decode('utf-8', 'replace').strip())

        # 2) 建远程目录并上传
        sftp = c.open_sftp()
        try:
            dirs = sorted({os.path.dirname(r) for r, _ in items})
            for d in dirs:
                parts = d.strip('/').split('/')
                cur = ''
                for p in parts:
                    cur += '/' + p
                    try:
                        sftp.stat(cur)
                    except IOError:
                        sftp.mkdir(cur)
            for remote, data in items:
                with sftp.open(remote, 'wb') as fh:
                    fh.write(data)
                print('up  ', remote, len(data), 'bytes')
        finally:
            sftp.close()

        # 3) 重启并检查服务
        _in, out, err = c.exec_command(
            'systemctl restart %s && sleep 2 && systemctl is-active %s' % (SERVICE, SERVICE),
            timeout=120)
        out.channel.recv_exit_status()
        status = out.read().decode('utf-8', 'replace').strip()
        print('服务状态:', status or err.read().decode('utf-8', 'replace').strip())

        # 4) 本地端口自测
        _in, out, err = c.exec_command(
            'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/', timeout=30)
        print('本地 5000 端口 HTTP:', out.read().decode('utf-8', 'replace').strip())

        # 5) 最近的错误日志（有输出才说明有问题）
        _in, out, err = c.exec_command(
            'journalctl -u %s --since "2 minutes ago" --no-pager | grep -iE "error|traceback|exception" | tail -20'
            % SERVICE, timeout=60)
        errs = out.read().decode('utf-8', 'replace').strip()
        if errs:
            print('[服务日志告警]\n' + errs)
        else:
            print('服务日志无错误')
    finally:
        c.close()

    print('部署完成（回滚: 把 %s 里的文件拷回 %s）' % (backup_dir, REMOTE_ROOT))


if __name__ == '__main__':
    main()
