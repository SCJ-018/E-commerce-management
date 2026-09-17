# -*- coding: utf-8 -*-
"""爱搜（dso.aidso.com）登录态保鲜工具

背景：tools/aisou_scraper.py 靠 tools/aisou_localstorage.json（token/uid/d_m_p）复用登录态。
服务端会提前作废 token（虽然 JWT 里的 exp 还没到），失效后页面退化为「游客态」——
接口一律返回 {"code":800,"msg":"请先登录账号"}，采集器就会抓到游客版示例榜单
（典型症状：10 个关键词抓回来的词完全一样，比如「搜索词/什么/29.67亿」）。

用法（必须在本机跑，需要可见浏览器）：
    python tools/aisou_login.py

流程：
  1. 打开爱搜首页（弹出可见浏览器窗口），自动点出登录框；
  2. 你用扫码 / 账号密码完成登录；
  3. 脚本检测到登录成功后：导出 JSON.stringify(localStorage) 覆盖 tools/aisou_localstorage.json，
     并真机校验一次关键词查询接口是否返回 code 200。

跑完后需要把新文件传到服务器：python .deploy/ssx.py up tools/aisou_localstorage.json /opt/ecom/tools/aisou_localstorage.json
"""
import json
import os
import sys
import time
from urllib.parse import quote

sys.stdout.reconfigure(encoding='utf-8')

from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(BASE_DIR, 'aisou_profile')
OUT_FILE = os.path.join(BASE_DIR, 'aisou_localstorage.json')
HOME_URL = 'https://dso.aidso.com/'
PROBE_KEYWORD = '充电宝'
WAIT_TIMEOUT = 8 * 60
POLL_INTERVAL = 3

flags = {'login_ok': False, 'codes': [], 'find_word': None}

JS_GET_LS = """() => {
  const o = {};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    o[k] = localStorage.getItem(k);
  }
  return o;
}"""

JS_CLICK_LOGIN = """() => {
  const el = Array.from(document.querySelectorAll('button,a,span,div'))
    .find(e => /^(登录\\/注册|立即登录|登录|马上登录)$/.test((e.innerText || '').trim()));
  if (el) { el.click(); return (el.innerText || '').trim(); }
  return '';
}"""

JS_KILL_POPUP = """() => {
  let n = 0;
  document.querySelectorAll('div,section').forEach(e => {
    const t = (e.className || '') + '';
    const r = e.getBoundingClientRect();
    if (/mask|modal|overlay|popup|dialog|login-container/i.test(t) && r.width > 250 && r.height > 150) {
      e.style.display = 'none'; n++;
    }
  });
  return n;
}"""


def on_response(r):
    try:
        u = r.url
        if 'aidso' not in u:
            return
        if '/keyword/library_v2/find_word' in u:
            flags['find_word'] = r.json()
        elif 'user/info' in u or 'sub_account_info' in u:
            j = r.json()
            flags['codes'].append((u.split('?')[0].split('/')[-1], j.get('code')))
            if j.get('code') == 200:
                flags['login_ok'] = True
    except Exception:
        pass


def get_ls(page):
    try:
        return page.evaluate(JS_GET_LS) or {}
    except Exception:
        return {}


def main():
    print('=' * 60)
    print('爱搜登录态保鲜工具')
    print('Profile:', USER_DATA_DIR)
    print('输出   :', OUT_FILE)
    print('=' * 60)

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                USER_DATA_DIR, channel='chrome', headless=False,
                args=['--disable-blink-features=AutomationControlled'],
                viewport={'width': 1440, 'height': 900}, locale='zh-CN')
        except Exception as e:
            print('启动 Chrome 失败（%r），回退到内置 Chromium' % (e,))
            ctx = p.chromium.launch_persistent_context(
                USER_DATA_DIR, headless=False,
                args=['--disable-blink-features=AutomationControlled'],
                viewport={'width': 1440, 'height': 900}, locale='zh-CN')

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on('response', on_response)
        page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        print('\n正在打开爱搜首页...')
        page.goto(HOME_URL, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(6000)

        old_token = (get_ls(page).get('token') or '')
        print('当前 localStorage.token:', (old_token[:24] + '...') if old_token else '(空)')
        print('页面自检接口返回:', flags['codes'])

        if flags['login_ok']:
            print('\n✅ 已登录（profile 里残留的登录态仍然有效）')
        else:
            clicked = page.evaluate(JS_CLICK_LOGIN)
            print('\n已点击登录入口:', clicked or '（未找到，请在浏览器窗口手动点右上角「登录/注册」）')
            print('请在弹出的登录框里完成登录（扫码或账号密码）。最多等 %d 分钟。' % (WAIT_TIMEOUT // 60))

            deadline = time.time() + WAIT_TIMEOUT
            confirmed = False
            while time.time() < deadline:
                page.wait_for_timeout(POLL_INTERVAL * 1000)
                cur_token = (get_ls(page).get('token') or '')
                if cur_token and cur_token != old_token:
                    print('检测到 token 已更新，正在确认...')
                    flags['login_ok'] = False
                    page.reload(wait_until='domcontentloaded')
                    page.wait_for_timeout(7000)
                    if flags['login_ok']:
                        confirmed = True
                        break
                    print('  服务端仍未确认登录，继续等待（界面若有「请先登录」提示可再点一次登录）')
            if not confirmed and not flags['login_ok']:
                print('\n❌ 超时：未检测到登录成功，未写入文件。请重新运行本脚本。')
                ctx.close()
                return
            print('\n✅ 登录成功')

        ls = get_ls(page)
        with open(OUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(ls, f, ensure_ascii=False)
        print('已导出 localStorage（%d 个键: %s）-> %s' % (len(ls), ', '.join(ls.keys()), OUT_FILE))

        # 真机校验：查一个词，确认 find_word 接口返回 200
        print('\n校验关键词查询接口（%s）...' % PROBE_KEYWORD)
        flags['find_word'] = None
        page.goto('https://dso.aidso.com/KeywordDouyin/searchWord?keyword=' + quote(PROBE_KEYWORD),
                  wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(6000)
        page.evaluate(JS_KILL_POPUP)
        try:
            el = page.locator('input[placeholder*="多词联查"]').first
            el.click(timeout=5000)
            el.fill('')
            page.keyboard.type(PROBE_KEYWORD, delay=50)
            page.keyboard.press('Enter')
            page.wait_for_timeout(8000)
        except Exception as e:
            print('  自动输入失败（%r），改为点击「搜索词」标签重试' % (e,))
            page.evaluate("""() => {
              const s = Array.from(document.querySelectorAll('span'))
                .find(e => (e.innerText || '').trim() === '搜索词');
              if (s) s.click();
            }""")
            page.wait_for_timeout(8000)

        res = flags['find_word']
        if not res:
            print('  ⚠️ 未捕获到 find_word 请求，无法自动判定。请在浏览器里手动查一次看看有没有数据。')
        elif res.get('code') == 200:
            print('  ✅ find_word 返回 code=200，登录态可用。')
            print('  返回字段示例:', json.dumps(res, ensure_ascii=False)[:300])
        else:
            print('  ❌ find_word 返回:', json.dumps(res, ensure_ascii=False)[:200])
            print('     仍提示未登录，请确认登录的是有权限的账号。')

        print('\n下一步：把新登录态传到服务器')
        print('  python .deploy/ssx.py up tools/aisou_localstorage.json /opt/ecom/tools/aisou_localstorage.json')
        page.wait_for_timeout(3000)
        ctx.close()


if __name__ == '__main__':
    main()
