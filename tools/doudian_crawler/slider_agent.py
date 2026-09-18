# -*- coding: utf-8 -*-
"""抖店滑块助手（本机常驻）—— 后台页面「手动拖滑块」按钮的执行端。

═══ 它解决什么 ═══
抖店登录态是**账号级**的短效凭证（14 家店共用 1 个邮箱，实测约 40 分钟失效）。
服务器是 IDC IP + Xvfb 虚拟屏：拼图滑块过不去，也没有任何窗口存在于人的屏幕上。
所以「登录 + 拖滑块」只能在本机做 —— 但原来要人离开后台页面、跑去桌面双击
「启动抖店登录.bat」，很别扭。

现在：后台点「手动拖滑块」→ 云端记一条任务 → 本助手每 3 秒轮询领走 →
自动拉起本机有头 Chrome → 你手动拖滑块完成登录 → state 传回云库 → 页面回显结果。

═══ 怎么用 ═══
双击项目根目录的「启动滑块助手.bat」即可（保持窗口开着）。
想让它在开机后自动待命：Win+R 输入 shell:startup，把该 bat 的快捷方式丢进去。

调试用：
    python slider_agent.py --once      # 只轮询一次（看能不能连上后台）
    python slider_agent.py --selftest  # 只测连通性与密钥，不执行登录
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

BASE = os.path.dirname(os.path.abspath(__file__))            # tools/doudian_crawler
ROOT = os.path.dirname(os.path.dirname(BASE))                # 项目根
LOCAL_LOGIN = os.path.join(BASE, '_local_login_all.py')      # 本机登录 + 上传（人工过滑块）
LOG_FILE = os.path.join(BASE, 'slider_agent.log')
LOG_MAX = 2 * 1024 * 1024                                    # 日志超过 2MB 滚动一次

API_BASE = (os.environ.get('SLIDER_API_BASE') or 'https://julangkeji.site').rstrip('/')
# ★ 必须与 backend/app.py 的 _SLIDER_KEY 一致（线上可用环境变量覆盖）
KEY = os.environ.get('SLIDER_AGENT_KEY') or 'julang-doudian-slider-2026'
POLL_SEC = 3                                                 # 轮询间隔；后端离线判定 30s
LOCK_PORT = 17899                                            # 单实例锁：占住这个本地端口
PY = sys.executable
HOSTNAME = socket.gethostname()

_sock_lock = None


def log(msg):
    line = '[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _rotate_log():
    try:
        if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX:
            bak = LOG_FILE + '.1'
            if os.path.isfile(bak):
                os.remove(bak)
            os.rename(LOG_FILE, bak)
    except Exception:
        pass


def single_instance():
    """抢本地端口当锁：抢不到说明已经有一个助手在跑，直接退出，避免重复领任务"""
    global _sock_lock
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('127.0.0.1', LOCK_PORT))
        s.listen(1)
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return False
    _sock_lock = s          # 持有引用，进程结束前不释放
    return True


def _req(path, method='GET', body=None, timeout=15):
    url = API_BASE + path
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('X-Slider-Key', KEY)
    req.add_header('X-Slider-Host', HOSTNAME)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _report(task_id, status, step, message):
    try:
        _req('/api/fetch/doudian/slider/report', 'POST',
             {'taskId': task_id, 'status': status, 'step': step, 'message': message})
        return True
    except Exception as e:
        log('  ! 回报 %s 失败：%s' % (status, e))
        return False


def _run_local_login():
    """跑本机登录脚本，把它的输出实时打到本窗口 + 日志。返回退出码。"""
    try:
        p = subprocess.Popen([PY, LOCAL_LOGIN], cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding='utf-8', errors='replace', bufsize=1)
    except Exception as e:
        log('  ! 启动登录脚本失败：%s' % e)
        return -1
    for line in p.stdout:
        line = line.rstrip()
        if line:
            log('  | ' + line)
    try:
        p.stdout.close()
    except Exception:
        pass
    p.wait()
    return p.returncode


def handle_one():
    """轮询一次；领到任务就执行。返回 True 表示这次处理了任务。"""
    q = '/api/fetch/doudian/slider/pending?host=%s&pid=%s' % (quote(HOSTNAME), os.getpid())
    r = _req(q)
    task = ((r or {}).get('data') or {}).get('task')
    if not task:
        return False
    tid = task.get('taskId') or '?'
    log('★ 领到任务 %s —— 即将弹出 Chrome，请完成邮箱登录并拖动拼图滑块' % tid)
    _report(tid, 'running',
            '本机 Chrome 已打开 —— 请完成邮箱登录 / 拖动拼图滑块',
            '')
    rc = _run_local_login()
    if rc == 0:
        _report(tid, 'done', '登录成功，登录态已上传云库，可以点「更新数据」了',
                '耗时约 40 分钟内有效，请尽快触发抓取')
        log('★ 任务 %s 完成' % tid)
    else:
        _report(tid, 'fail', '本机登录未完成',
                '退出码 %s；详见本机助手窗口日志（本机窗口未关闭时可重试）' % rc)
        log('★ 任务 %s 失败 rc=%s' % (tid, rc))
    return True


def selftest():
    """只测连通性 + 密钥，不执行任何登录动作"""
    log('连通性自检 → %s' % API_BASE)
    try:
        r = _req('/api/fetch/doudian/slider/status', timeout=10)
        log('  status 接口 OK：code=%s' % r.get('code'))
    except Exception as e:
        log('  ! status 接口失败：%s' % e)
        return 1
    try:
        r = _req('/api/fetch/doudian/slider/pending?host=%s&pid=%s'
                 % (quote(HOSTNAME), os.getpid()), timeout=10)
        log('  pending 接口 OK（密钥有效）：code=%s' % r.get('code'))
    except urllib.error.HTTPError as e:
        log('  ! pending 返回 HTTP %s —— 密钥不匹配（检查 SLIDER_AGENT_KEY）' % e.code)
        return 2
    except Exception as e:
        log('  ! pending 失败：%s' % e)
        return 3
    log('自检通过：助手能连上后台，密钥正确。')
    return 0


def main():
    args = set(sys.argv[1:])
    _rotate_log()
    if '--selftest' in args:
        return selftest()

    print('=' * 64)
    print(' 抖店滑块助手 —— 后台「手动拖滑块」按钮的执行端')
    print('=' * 64)
    print(' 后台地址 : %s' % API_BASE)
    print(' Python   : %s' % PY)
    print(' 本机登录 : %s' % LOCAL_LOGIN)
    print(' 日志     : %s' % LOG_FILE)
    print('-' * 64)

    if not os.path.isfile(LOCAL_LOGIN):
        log('! 找不到 %s —— 本机登录链路不完整，无法执行' % LOCAL_LOGIN)
        return 1
    if not single_instance():
        log('已有一个滑块助手在运行（占用本地端口 %d），本进程退出' % LOCK_PORT)
        return 0

    log('助手已启动，开始轮询（每 %d 秒一次）—— 后台点「手动拖滑块」即可。' % POLL_SEC)
    if '--once' in args:
        try:
            n = 1 if handle_one() else 0
            log('--once 结束（处理任务数 %d）' % n)
        except Exception as e:
            log('--once 失败：%s' % e)
            return 1
        return 0

    fails = 0
    while True:
        try:
            handle_one()
            if fails:
                log('与后台恢复连接（此前连续失败 %d 次）' % fails)
                fails = 0
        except urllib.error.HTTPError as e:
            fails += 1
            if fails in (1, 5, 20):
                log('! 后台返回 HTTP %s（密钥不匹配或服务异常）' % e.code)
        except Exception as e:
            fails += 1
            # 只在第 1/5/60 次以及之后每 60 次打印，避免断网时日志刷屏
            if fails in (1, 5) or fails % 60 == 0:
                log('! 轮询失败（第 %d 次）：%s' % (fails, e))
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n（已手动停止）')
