# -*- coding: utf-8 -*-
"""验证抖店 state 有效性：加载 state 访问工作台 + 罗盘，确认不被重定向回登录页。"""
import sys
import os
import time
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
from playwright.sync_api import sync_playwright  # noqa: E402

STATE = os.path.join(BASE_DIR, '_states', '抖店_pcl526_yeah_net.json')
WORKBENCH = 'https://fxg.jinritemai.com/ffa/mshop/homepage/index'
COMPASS = 'https://compass.jinritemai.com'


def main():
    state = json.load(open(STATE, encoding='utf-8'))
    print('state cookies:', len(state.get('cookies', [])))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=False,
                                    args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
        ctx = browser.new_context(storage_state=state, locale='zh-CN', timezone_id='Asia/Shanghai')
        page = ctx.new_page()

        print('[1] 访问抖店工作台...')
        try:
            page.goto(WORKBENCH, wait_until='domcontentloaded', timeout=40000)
        except Exception as e:
            print('  [warn]', e)
        time.sleep(5)
        url1 = page.url
        print('  URL:', url1[:80])
        print('  标题:', page.title()[:40])
        ok1 = '/login' not in url1
        print('  工作台:', '✅ 有效' if ok1 else '❌ 被重定向回登录页')

        print('[2] 访问罗盘...')
        try:
            page.goto(COMPASS, wait_until='domcontentloaded', timeout=40000)
        except Exception as e:
            print('  [warn]', e)
        time.sleep(5)
        url2 = page.url
        print('  URL:', url2[:80])
        ok2 = 'passport' not in url2 and '/login' not in url2
        print('  罗盘:', '✅ 有效' if ok2 else '❌ 需要登录')

        browser.close()
        print('\n结论:', '登录态有效' if (ok1 and ok2) else '登录态无效')


if __name__ == '__main__':
    main()
