# -*- coding: utf-8 -*-
"""从 Edge 已登录 profile 捕捉 storage_state（免重新登录、免验证码）。

场景：另一台电脑的 Edge 里，每个店铺用【独立 profile】登录过，直接读取登录态导出。

用法：
  python capture_edge.py scan                                  # 扫描所有 profile，打印各自登录的淘宝昵称
  python capture_edge.py capture "Profile 1" "优比熊旗舰店:螃蟹"  # 导出指定 profile 的登录态

前提（重要）：
  1. 先【完全关闭 Edge】——包括后台进程（任务管理器里确认没有 msedge.exe），
     否则 profile 被锁，脚本会启动失败。
  2. 本机装 Python + playwright：
        pip install playwright
     （用系统自带 Edge，无需 `playwright install` 下载浏览器）

导出的 state 会存到本脚本目录 _states/<账号>.json，把文件发回主电脑即可。
"""
import sys
import os
import json
import time
from urllib.parse import unquote

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, '_states')

# Edge 用户数据目录（Windows）
EDGE_UD = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Edge', 'User Data')


def profile_names():
    """读 Local State 拿 profile 目录 → 用户在 Edge 里起的名字 映射。"""
    names = {}
    try:
        with open(os.path.join(EDGE_UD, 'Local State'), encoding='utf-8') as f:
            data = json.load(f)
        for d, v in data.get('profile', {}).get('info_cache', {}).items():
            names[d] = v.get('name', d)
    except Exception:
        pass
    return names


def _launch(p, prof, headless):
    return p.chromium.launch_persistent_context(
        user_data_dir=EDGE_UD,
        channel='msedge',
        headless=headless,
        args=['--profile-directory=' + prof, '--no-sandbox',
              '--disable-blink-features=AutomationControlled'],
    )


def _nick(ctx):
    ck = {c['name']: c['value'] for c in ctx.cookies()}
    nick = ck.get('_nk_', '')
    try:
        nick = unquote(nick)
    except Exception:
        pass
    return nick


def scan():
    from playwright.sync_api import sync_playwright
    names = profile_names()
    profs = ['Default'] + sorted(d for d in os.listdir(EDGE_UD) if d.startswith('Profile '))
    print('Edge User Data:', EDGE_UD)
    print('发现 profile: %s' % ', '.join(profs))
    print('=' * 70)
    with sync_playwright() as p:
        for prof in profs:
            if not os.path.isdir(os.path.join(EDGE_UD, prof)):
                continue
            try:
                ctx = _launch(p, prof, headless=True)
                page = ctx.new_page()
                page.goto('https://www.taobao.com', wait_until='domcontentloaded', timeout=30000)
                time.sleep(2)
                print('%-14s | profile名=%-24s | 淘宝昵称=%s' % (prof, names.get(prof, ''), _nick(ctx)))
                ctx.close()
            except Exception as e:
                print('%-14s | 读取失败: %s' % (prof, str(e)[:80]))


def capture(prof, account):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _launch(p, prof, headless=False)
        page = ctx.new_page()
        print('访问生意参谋确认登录态 ...')
        page.goto('https://sycm.taobao.com/portal/home.htm', wait_until='domcontentloaded', timeout=40000)
        time.sleep(4)
        state = ctx.storage_state()
        os.makedirs(OUT_DIR, exist_ok=True)
        out = os.path.join(OUT_DIR, account.replace(':', '_').replace('/', '_') + '.json')
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        print('[OK] 已导出 %s -> %s （cookies %d 条）' % (account, out, len(state.get('cookies', []))))
        ctx.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == 'scan':
        scan()
    elif cmd == 'capture':
        if len(sys.argv) < 4:
            print('用法：python capture_edge.py capture "Profile 1" "账号名"')
            return
        capture(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
