# -*- coding: utf-8 -*-
"""轻量验证：加载指定账号的 storage_state，访问生意参谋 coreIndex，确认登录态有效。

用法：python verify_state.py <账号> [日期]
"""
import sys
import os
import json
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, '_states')


def main():
    acct = sys.argv[1] if len(sys.argv) > 1 else None
    date = sys.argv[2] if len(sys.argv) > 2 else '2026-09-15'
    if not acct:
        print(__doc__)
        return

    state_path = os.path.join(STATE_DIR, acct.replace(':', '_') + '.json')
    if not os.path.exists(state_path):
        print('[缺 state]', state_path)
        return
    state = json.load(open(state_path, encoding='utf-8'))
    print('账号:', acct, '| cookies:', len(state.get('cookies', [])))

    dr = date + '|' + date
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=False,
                                    args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
        ctx = browser.new_context(storage_state=state, locale='zh-CN', timezone_id='Asia/Shanghai')
        page = ctx.new_page()
        try:
            page.goto('https://sycm.taobao.com/portal/home.htm', wait_until='domcontentloaded', timeout=40000)
        except Exception as e:
            print('[warn] 首页访问:', str(e)[:80])
        time.sleep(3)

        url = 'https://sycm.taobao.com/portal/coreIndex/new/overview/v3.json?needCycleCrc=true&dateType=day&dateRange=' + dr
        try:
            resp = page.request.get(url, headers={'referer': 'https://sycm.taobao.com/portal/home.htm'})
            body = resp.json()
            selfobj = (body.get('content') or {}).get('data', {}).get('self') or {}
            if selfobj:
                print('✅ 登录态有效，指标数:', len(selfobj))
                for k in ['netPaymentAmount', 'payAmt', 'uv', 'payByrCnt', 'payRate']:
                    v = selfobj.get(k)
                    if isinstance(v, dict):
                        v = v.get('value')
                    print('   %s = %s' % (k, v))
            else:
                print('❌ self 为空，登录态失效或被风控')
                print('   body keys:', list(body.keys())[:10])
                print('   摘要:', json.dumps(body, ensure_ascii=False)[:300])
        except Exception as e:
            print('[FAIL]', str(e)[:200])
        browser.close()


if __name__ == '__main__':
    main()
