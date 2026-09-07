#!/usr/bin/env python3
"""一次性 SSH 部署助手：run <cmd> | up <local> <remote> | get <remote> <local>
用法:
  python ssx.py run "uname -a"
  python ssx.py up ./backend /opt/app/backend
"""
import sys, os, socket, select, threading
import paramiko

HOST = "119.45.187.154"
USER = os.environ.get("SSH_USER", "root")
PWD  = "pcl520526."
PORT = 22

def conn():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, PORT, USER, PWD, timeout=15, banner_timeout=15, auth_timeout=15)
    return c

def run(cmd):
    c = conn()
    try:
        stdin, stdout, stderr = c.exec_command(cmd, timeout=600)
        out = stdout.read().decode('utf-8', 'replace')
        err = stderr.read().decode('utf-8', 'replace')
        rc = stdout.channel.recv_exit_status()
        if out: sys.stdout.write(out)
        if err: sys.stderr.write(err)
        sys.exit(rc if rc is not None else 0)
    finally:
        c.close()

def tunnel():
    """本地端口转发：127.0.0.1:3307 -> 服务器 127.0.0.1:3306 (MySQL)"""
    c = conn()
    t = c.get_transport()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 3307))
    sock.listen(5)
    print("隧道已建立: 127.0.0.1:3307 -> %s:3306 (MySQL)" % HOST)
    print("保持本窗口运行，导入工具里连 127.0.0.1:3307（Ctrl+C 退出）")
    while True:
        client, addr = sock.accept()
        try:
            chan = t.open_channel("direct-tcpip", ("127.0.0.1", 3306), addr)
        except Exception as e:
            print("open_channel 失败:", e)
            client.close()
            continue
        threading.Thread(target=_bridge, args=(client, chan), daemon=True).start()

def _bridge(src, dst):
    try:
        while True:
            r, _, _ = select.select([src, dst], [], [])
            if src in r:
                data = src.recv(8192)
                if not data:
                    break
                dst.sendall(data)
            if dst in r:
                data = dst.recv(8192)
                if not data:
                    break
                src.sendall(data)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try:
                s.close()
            except Exception:
                pass

def up(local, remote):
    c = conn()
    sftp = c.open_sftp()
    try:
        if os.path.isdir(local):
            sftp.mkdir(remote) if not _exists(sftp, remote) else None
            for root, dirs, files in os.walk(local):
                rel = os.path.relpath(root, local)
                rdir = remote if rel == '.' else remote + '/' + rel.replace('\\', '/')
                for d in dirs:
                    p = rdir + '/' + d
                    if not _exists(sftp, p):
                        sftp.mkdir(p)
                for f in files:
                    lp = os.path.join(root, f)
                    rp = rdir + '/' + f
                    sftp.put(lp, rp)
                    print('up', rp)
        else:
            sftp.put(local, remote)
            print('up', remote)
    finally:
        sftp.close(); c.close()

def get(remote, local):
    c = conn(); sftp = c.open_sftp()
    try:
        sftp.get(remote, local); print('got', remote, '->', local)
    finally:
        sftp.close(); c.close()

def _exists(sftp, path):
    try:
        sftp.stat(path); return True
    except IOError:
        return False

if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit('usage: run <cmd> | up <local> <remote> | get <remote> <local>')
    mode = sys.argv[1]
    if mode == 'run':
        run(sys.argv[2])
    elif mode == 'tunnel':
        tunnel()
    elif mode == 'up':
        up(sys.argv[2], sys.argv[3])
    elif mode == 'get':
        get(sys.argv[2], sys.argv[3])
    else:
        sys.exit('bad mode')
