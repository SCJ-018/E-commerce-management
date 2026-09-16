# -*- coding: utf-8 -*-
"""人工登录 + 自动捕捉 storage_state（风控/验证码场景专用）。

用途：自动登录脚本触发风控/验证码过不了时，改用本脚本——
用户在浏览器里手动完成登录（含验证码/滑块），脚本自动检测登录成功并导出 storage_state。

用法：
  python manual_login_capture.py <账号>

流程：
1. 打开有头浏览器 → loginmyseller.taobao.com
2. 用户在浏览器窗口里手动登录（账号密码、验证码、滑块都自己来，脚本不干预）
3. 脚本每 2 秒检测一次登录态（cookie 出现 unb + cookie2/sgcookie 即认为登录成功）
4. 检测成功后自动访问生意参谋首页写入 localStorage，导出 storage_state 到 _states/
5. 导出后脚本关闭浏览器

提示：登录成功后页面跳转不用管，脚本只看 cookie。
"""
import sys
import os
import time
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, '_states')

from playwright.sync_api import sync_playwright

LOGIN_URL = 'https://loginmyseller.taobao.com/'
SYCM_HOME = 'https://sycm.taobao.com/portal/home.htm'

MAX_WAIT = 300  # 最多等 5 分钟（给验证码留时间）


def is_logged_in(ctx):
    ck = {c['name']: c['value'] for c in ctx.cookies() if 'taobao.com' in c.get('domain', '')}
    return bool(ck.get('unb')) and bool(ck.get('cookie2') or ck.get('sgcookie'))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    account = sys.argv[1]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel='chrome',
            headless=False,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-first-run',
                '--no-default-browser-check',
                '--no-sandbox',
            ],
        )
        ctx = browser.new_context(
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
            viewport={'width': 1400, 'height': 900},
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=60000)

        print('==============================================')
        print('请在浏览器窗口里【手动登录】账号：%s' % account)
        print('（账号密码、验证码、滑块都你来操作，脚本不干预）')
        print('登录成功后我会自动检测并导出 state，最多等 %d 秒' % MAX_WAIT)
        print('==============================================')

        deadline = time.time() + MAX_WAIT
        logged = False
        while time.time() < deadline:
            if is_logged_in(ctx):
                logged = True
                break
            time.sleep(2)

        if not logged:
            print('[FAIL] 超时未检测到登录成功（%d 秒）' % MAX_WAIT)
            browser.close()
            return

        print('检测到登录成功，等 cookie 稳定 + 写入 localStorage ...')
        # 访问生意参谋首页，让 localStorage / origins 完整写入
        try:
            page.goto(SYCM_HOME, wait_until='domcontentloaded', timeout=30000)
        except Exception as e:
            print('  [warn] 生意参谋访问异常（不影响 cookie 导出）:', e)
        time.sleep(4)

        state = ctx.storage_state()
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, account.replace(':', '_').replace('/', '_') + '.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        print('[OK] state 已保存:', path)
        print('接下来跑：python upload_state.py "%s"' % account)

        browser.close()


if __name__ == '__main__':
    main()
