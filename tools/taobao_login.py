# -*- coding: utf-8 -*-
"""淘宝登录态录入：打开可见 Chrome，由用户自行扫码后导出 Cookie。"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, 'taobao_profile')
COOKIE_FILE = os.path.join(BASE_DIR, 'taobao_cookie.txt')
LOGIN_COOKIES = {'tracknick', 'unb', '_nk_'}
WAIT_SECONDS = 8 * 60


def _cookie_string(context):
    return '; '.join('%s=%s' % (c['name'], c['value']) for c in context.cookies())


def _logged_in(context):
    return bool({c['name'] for c in context.cookies('https://www.taobao.com')} & LOGIN_COOKIES)


def main():
    print('正在打开淘宝，请在 Chrome 窗口自行扫码登录（最长等待 8 分钟）...', flush=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, channel='chrome', headless=False,
            args=['--disable-blink-features=AutomationControlled', '--no-first-run', '--no-default-browser-check'],
            viewport={'width': 1440, 'height': 900}, locale='zh-CN')
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto('https://www.taobao.com/', wait_until='domcontentloaded', timeout=60000)
        except Exception as e:
            print('页面加载提示：%s' % str(e)[:160], flush=True)
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline and not _logged_in(ctx):
            page.wait_for_timeout(2000)
        if not _logged_in(ctx):
            print('未检测到淘宝登录态，未覆盖原 Cookie。', flush=True)
            ctx.close()
            return 2
        value = _cookie_string(ctx)
        with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
            f.write(value)
        print('淘宝登录态已保存（%d 个 Cookie 字段）。' % len(ctx.cookies()), flush=True)
        ctx.close()
        return 0


if __name__ == '__main__':
    sys.exit(main())
