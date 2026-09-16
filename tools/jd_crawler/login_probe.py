# -*- coding: utf-8 -*-
"""京东京麦登录（一次性）：自动填账号密码 → 人工过验证码（若出现）→ 存登录态。

京麦 = 京东商家工作台，登录入口 https://passport.shop.jd.com
本脚本只做「登录 + 存 state」，不做抓数。成功后：
  - 写 _states/jd_state.json
  - 写回云库「京东账号表.登录状态」

用法：python login_probe.py
"""
import sys
import os
import time
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import shops  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

# ⚠️ passport.shop.jd.com 根路径是 404（"此页面没有找到"），不能直连。
# 正确做法：从 shop.jd.com 进入，京东会自行跳转到登录页（2026-09-16 实测）
LOGIN_URL = 'https://shop.jd.com'
HOME = 'https://shop.jd.com/jdm/home'
STATE_DIR = os.path.join(BASE_DIR, '_states')
OUT_TXT = os.path.join(BASE_DIR, '_jd_login_dump.txt')

log_lines = []


def L(msg):
    print(msg)
    log_lines.append(str(msg))


def dump_page(page, tag):
    """打印可见输入框/按钮，便于诊断登录页结构。"""
    info = page.evaluate("""
    () => {
      const vis = el => { const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0; };
      const inputs = [];
      document.querySelectorAll('input').forEach(el => {
        if (vis(el)) inputs.push({type: el.type, name: el.name, id: el.id,
          ph: el.placeholder, cls: String(el.className || '').slice(0, 70)});
      });
      const btns = [];
      document.querySelectorAll('button, a, div[class*=btn], span[class*=btn]').forEach(el => {
        if (vis(el)) {
          const t = (el.textContent || '').trim();
          if (t && t.length < 16) btns.push({tag: el.tagName, cls: String(el.className||'').slice(0,60), text: t});
        }
      });
      return {url: location.href, title: document.title, inputs, btns: btns.slice(0, 40)};
    }
    """)
    L('\n===== [%s] 页面诊断 =====' % tag)
    L('  url   = %s' % info['url'])
    L('  title = %s' % info['title'])
    L('  -- 可见 input (%d) --' % len(info['inputs']))
    for i in info['inputs']:
        L('    type=%-10s name=%-14s ph=%r cls=%s' % (i['type'], i['name'], i['ph'], i['cls']))
    L('  -- 可见按钮 (%d) --' % len(info['btns']))
    seen = set()
    for b in info['btns']:
        k = b['text']
        if k in seen:
            continue
        seen.add(k)
        L('    <%s> %r  cls=%s' % (b['tag'], b['text'], b['cls']))
    return info


def has_captcha(page):
    """粗判是否出现人机验证（京东常见滑块/拼图）。"""
    js = """
    () => {
      const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0; };
      const sels = ['#JDJRV-wrap-loginsubmit', '.JDJRV-slide-inner', 'img.jd_captcha_image',
                    '.JDJRV-bigimg', 'div.JDJRV', 'iframe[src*=captcha]', '.verify-wrap'];
      for (const s of sels) { const el = document.querySelector(s); if (vis(el)) return s; }
      const t = document.body ? document.body.innerText : '';
      for (const kw of ['拖动滑块', '请拖动', '完成拼图', '安全验证', '滑动验证']) {
        if (t.includes(kw)) return kw;
      }
      return '';
    }
    """
    try:
        return page.evaluate(js) or ''
    except Exception:
        return ''


def main():
    acc = shops.get_jd_account()
    if not acc:
        L('[FAIL] 京东账号表没有运营中的账号')
        return
    user, pwd = acc['账号'], acc['密码']
    L('京东账号: %s | 店铺: %s (%s)' % (user, acc['店铺名'], acc['店铺ID']))

    os.makedirs(STATE_DIR, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=False,
                                    args=['--disable-blink-features=AutomationControlled',
                                          '--no-first-run', '--no-default-browser-check', '--no-sandbox'])
        ctx = browser.new_context(locale='zh-CN', timezone_id='Asia/Shanghai',
                                  viewport={'width': 1600, 'height': 950})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()

        L('\n[1] 打开京麦入口（shop.jd.com），等其跳转到登录页...')
        page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=60000)
        time.sleep(10)
        L('  goto 后 URL: %s' % page.url)
        # 若还停在首页（没自动跳登录），主动点「登录」入口
        if 'passport' not in page.url and 'login' not in page.url.lower():
            for t in ['登录', '立即登录', '登录/注册', '请登录']:
                try:
                    loc = page.locator('text=%s' % t)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        L('  点击入口: %s' % t)
                        time.sleep(6)
                        break
                except Exception:
                    pass
        L('  当前 URL: %s' % page.url)
        dump_page(page, '登录页初始')
        page.screenshot(path=os.path.join(BASE_DIR, '_jd_login_1.png'))

        # 若有「账号登录」tab，先切过去
        for t in ['账号登录', '账号密码登录', '密码登录']:
            try:
                loc = page.locator('text=%s' % t)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    L('  点击 tab: %s' % t)
                    time.sleep(2)
                    break
            except Exception:
                pass

        # 填账号
        filled = False
        for sel in ['input[placeholder*="账号"]', 'input[placeholder*="邮箱"]',
                    'input[name="loginname"]', 'input#loginname',
                    'form.rcd-form input.rcd-input__inner', 'input.rcd-input__inner']:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.fill(user)
                    L('  [OK] 账号已填 (selector=%s)' % sel)
                    filled = True
                    break
            except Exception as e:
                L('  [warn] 账号 selector %s 失败: %s' % (sel, str(e)[:60]))
        if not filled:
            L('  [FAIL] 没找到账号输入框')
            dump_page(page, '未找到账号框')
            browser.close()
            return

        # 填密码
        for sel in ['input[placeholder*="密码"]', 'input[type=password]']:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.fill(pwd)
                    L('  [OK] 密码已填 (selector=%s)' % sel)
                    break
            except Exception as e:
                L('  [warn] 密码 selector %s 失败: %s' % (sel, str(e)[:60]))
        time.sleep(0.5)

        # 勾协议（若有）
        for sel in ['input[type=checkbox]']:
            try:
                loc = page.locator(sel)
                for i in range(loc.count()):
                    el = loc.nth(i)
                    if el.is_visible() and not el.is_checked():
                        el.click(force=True)
                        L('  勾选协议 checkbox#%d' % i)
                        time.sleep(0.3)
            except Exception:
                pass

        # 点登录
        clicked = False
        for sel in ['button:has-text("立即登录")', 'button:has-text("登 录")',
                    'button:has-text("登录")', 'div.rcd-button', 'button.rcd-button']:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(force=True)
                    L('  [OK] 已点登录 (selector=%s)' % sel)
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            L('  [FAIL] 没找到登录按钮')
        time.sleep(6)
        dump_page(page, '点登录后')
        page.screenshot(path=os.path.join(BASE_DIR, '_jd_login_2.png'))

        # 验证码 / 人工处理
        cap = has_captcha(page)
        if cap:
            L('\n⚠️ 检测到人机验证 (%s) —— 请在浏览器窗口人工完成，最多等 300s' % cap)
            deadline = time.time() + 300
            while time.time() < deadline:
                time.sleep(3)
                if not has_captcha(page):
                    L('  ✅ 验证已通过')
                    break
            else:
                L('  [warn] 300s 内验证未完成')

        # 等跳转
        L('\n[2] 等待登录跳转...')
        ok = False
        deadline = time.time() + 60
        while time.time() < deadline:
            u = page.url
            if 'passport.shop.jd.com' not in u and 'login' not in u:
                ok = True
                break
            time.sleep(2)
        L('  当前 URL: %s' % page.url)
        page.screenshot(path=os.path.join(BASE_DIR, '_jd_login_3.png'))

        # 进首页验证
        L('\n[3] 访问京麦首页验证登录态...')
        try:
            page.goto(HOME, wait_until='domcontentloaded', timeout=60000)
        except Exception as e:
            L('  [warn] goto home: %s' % str(e)[:80])
        time.sleep(10)
        body = page.inner_text('body')[:2000]
        logged = 'passport.shop.jd.com' not in page.url and '请输入账号' not in body
        L('  首页 URL: %s' % page.url)
        L('  登录判定: %s' % ('通过' if logged else '失败'))
        page.screenshot(path=os.path.join(BASE_DIR, '_jd_home.png'))
        dump_page(page, '京麦首页')

        # 存 state
        st = ctx.storage_state()
        f = os.path.join(STATE_DIR, 'jd_state.json')
        with open(f, 'w', encoding='utf-8') as fp:
            json.dump(st, fp, ensure_ascii=False)
        ck = [(c['name'], c['domain']) for c in st.get('cookies', [])]
        L('\n[4] cookie %d 条，写入 %s' % (len(ck), f))
        key_names = [n for n, _ in ck if n.lower() in (
            'pt_key', 'pt_pin', 'pin', 'thor', 'jda', 'sid', 'jsid', 'sdtoken')]
        L('  关键 cookie: %s' % (key_names or '(未匹配到已知名)'))
        try:
            shops.save_jd_state(acc['账号'], st)
            L('  ✅ 已写回云库「京东账号表.登录状态」')
        except Exception as e:
            L('  [warn] 写库失败: %s' % str(e)[:100])

        browser.close()

    with open(OUT_TXT, 'w', encoding='utf-8') as fp:
        fp.write('\n'.join(log_lines))
    print('\n完成。')


if __name__ == '__main__':
    main()
