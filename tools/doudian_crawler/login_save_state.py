# -*- coding: utf-8 -*-
"""抖店登录 + 保存 Storage State（本地有头模式，人工过拼图滑块）。

13 个抖店店铺共用 1 个邮箱登录，只需登录一次。

流程：
1. 启动浏览器（channel=chrome，有头，反检测）
2. 打开 fxg.jinritemai.com/login/common?channel=zhaoshang
3. 点「邮箱登录」切换（若默认是手机号登录）
4. 填邮箱 + 密码 + 勾协议 + 点登录
5. 拼图滑块：检测到 → 提示人工拖拽（有头模式，最多等 180s）
6. 判定登录成功（URL 离开 login / 有 sessionid cookie）
7. storage_state() 导出 → 存本地 _states/抖店_<邮箱>.json

用法：
  python login_save_state.py
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

LOGIN_URL = 'https://fxg.jinritemai.com/login/common?channel=zhaoshang'
WORKBENCH_URL = 'https://fxg.jinritemai.com/ffa/mshop/homepage/index'

STATE_DIR = os.path.join(BASE_DIR, '_states')


def _state_fname(email, shop_name=None):
    base = '抖店_' + email.replace('@', '_').replace('.', '_')
    if shop_name:
        safe = ''.join(c for c in shop_name if c not in '\\/:*?"<>| ')
        base += '_' + safe
    return base + '.json'


def _save_state_file(email, state, shop_name=None):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, _state_fname(email, shop_name))
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False)
    print('  state 已存本地:', path)
    return path


def _snap(page, name):
    """安全截图。★ 浏览器被关掉时**绝不能**让异常冒出去。

    2026-09-18 事故：`page.screenshot()` 抛 TargetClosedError（用户拖完滑块顺手关了
    Chrome 窗口），异常打断了 login()，导致后面那行「保存登录态」根本没执行 ——
    用户明明登录成功了，前端却一直显示「已过期」。
    截图只是给人看的，凭证才是要命的东西 —— 任何时候都别让前者挡住后者。
    """
    try:
        page.screenshot(path=os.path.join(BASE_DIR, name))
    except Exception:
        pass


def _dump_state(ctx, email, shop_name, tag=''):
    """安全导出 storage_state 落盘。浏览器已关闭时返回 None（不抛异常）。

    设计成「可重复调用、后一次覆盖前一次」：登录流程里分几个时点各存一次，
    后面任何一个环节崩掉（关窗口 / 网络抖动 / 平台改版），前面已拿到的凭证都还在。
    """
    try:
        state = ctx.storage_state()
    except Exception as e:
        print('  [warn] 导出登录态失败（%s），跳过本次保存：%s' % (tag, e))
        return None
    if not state or not state.get('cookies'):
        print('  [warn] 导出登录态为空（%s），跳过本次保存' % tag)
        return None
    try:
        path = _save_state_file(email, state, shop_name)
    except Exception as e:
        print('  [warn] 写登录态文件失败（%s）：%s' % (tag, e))
        return None
    print('  ✅ 凭证已落盘（%s）' % tag)
    return path


def is_logged_in(page):
    """判定登录成功：URL 离开 login 页面 或 有 sessionid 系列 cookie。"""
    url = page.url
    if 'jinritemai.com' in url and '/login' not in url:
        return True
    ck = {c['name']: c['value'] for c in page.context.cookies()}
    return any('sessionid' in k.lower() for k in ck)


CAPTCHA_JS = r"""
() => {
  // 真滑块弹窗 vs 常驻 verify-center 容器的区分点：
  //   常驻 iframe[src*="captcha"] 的**默认尺寸就是 300×150**，is_visible() 会认，
  //   但真弹窗的容器远大于此。所以要求 尺寸 ≥ 280×180 且 opacity ≥ 0.15。
  const big = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return null;
    if (parseFloat(st.opacity || '1') < 0.15) return null;
    if (r.width < 280 || r.height < 180) return null;
    return {w: Math.round(r.width), h: Math.round(r.height),
            id: el.id || '', cls: (el.className || '').toString().slice(0, 40)};
  };
  const boxes = [];
  ['#captcha_container', 'iframe[src*="captcha"]', '[id*="captcha"]',
   'img.captcha-verify-image', '#vc_captcha_box', 'div.captcha-slider',
   'div[class*="captcha"]'].forEach(sel => {
    document.querySelectorAll(sel).forEach(el => {
      const b = big(el);
      if (b) boxes.push(b);
    });
  });
  // 文案判据：字节弹窗的标题与操作提示都在**父页面**（不在 iframe 内）
  const t = (document.body && document.body.innerText) || '';
  const m = t.match(/(请完成下列验证[^\n]{0,10}|按住左边按钮[^\n]{0,12}|拖动[^\n]{0,6}(滑块|拼图)|(滑块|拼图)[^\n]{0,5}验证)/);
  return {boxes: boxes, text: m ? m[1] : ''};
}
"""

# 登录成功信号：进入「请选择店铺」弹层 / 工作台，或已经离开登录页
LOGIN_OK_JS = r"""
() => {
  const t = (document.body && document.body.innerText) || '';
  if (/请选择店铺|选择店铺|抖店工作台|店铺管理/.test(t)) return true;
  return false;
}
"""


def has_captcha(page):
    """检测拼图验证弹窗是否**真的**出现。

    ⚠️ 2026-09-17 二次修正（关键，别再退回旧写法）：
      第一版只有 img.captcha-verify-image / #vc_captcha_box / div.captcha-slider，
      字节 verify-center 一个都不匹配 → **漏检** → 撞「1105 滑动滑块」卡死。
      第二版加了一堆 [id*="captcha"] 选择器 + is_visible()，结果**反向误判** ——
      Playwright 的 is_visible() 只要求「有非空 box 且非 visibility:hidden」，
      **不看 opacity、也不管 iframe 的默认尺寸**。verify-center 的
      `iframe[src*="captcha"]` 是常驻 DOM 的，默认 300×150，于是「登录早就成功、
      页面都跳到『请选择店铺』了」还被判成有滑块 → 白等 300s 超时 →
      转判「登录验证失败」→ 整条自动补抓链回滚。
      现在改为：尺寸/透明度过滤 + 父页面文案命中，两道判据任一命中才算。
    """
    try:
        d = page.evaluate(CAPTCHA_JS)
    except Exception:
        return False
    return bool(d.get('text')) or bool(d.get('boxes'))


def login_landed(page):
    """是否已经登录成功（进到店铺选择 / 工作台）。用于等待循环里**优先放行**，"""
    try:
        if '/login' not in page.url and 'passport' not in page.url:
            return True
        return bool(page.evaluate(LOGIN_OK_JS))
    except Exception:
        return False


def login(shop_name=None):
    acc = shops.get_email_account()
    if not acc:
        print('[FAIL] 抖店邮箱账号表无运营中邮箱')
        return False
    email = acc['邮箱']
    pwd = acc['密码']
    print('登录邮箱:', email, '| 目标店铺:', shop_name or '(默认御车宝周口驰为)')

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel='chrome', headless=False,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-first-run', '--no-default-browser-check', '--no-sandbox',
            ],
        )
        ctx = browser.new_context(
            locale='zh-CN', timezone_id='Asia/Shanghai',
            viewport={'width': 1400, 'height': 900},
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()

        print('[1/5] 打开登录页...')
        page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=60000)
        time.sleep(3)

        # 切换「邮箱登录」tab（页面默认手机登录，右上角有「手机登录 | 邮箱登录」两个 tab）
        print('[2/5] 切换邮箱登录 + 填表')
        try:
            tab = page.locator('text=邮箱登录')
            if tab.count() > 0:
                tab.first.click()
                time.sleep(1.5)
        except Exception as e:
            print('  [warn] 切换邮箱登录:', e)

        # 填邮箱（切换后应出现 email 输入框）
        email_box = page.locator('input[name=email]')
        if email_box.count() == 0:
            email_box = page.locator('input[placeholder*="邮箱"]')
        if email_box.count() == 0:
            email_box = page.locator('input[type=text], input:not([type])').first
        email_box.first.fill(email)
        time.sleep(0.3)

        # 填密码
        pwd_box = page.locator('input[type=password]')
        pwd_box.first.fill(pwd)
        time.sleep(0.3)

        # 勾协议（auxo-checkbox-input）
        try:
            agree = page.locator('input.auxo-checkbox-input')
            if agree.count() > 0 and not agree.first.is_checked():
                agree.first.click(force=True)
                time.sleep(0.3)
        except Exception as e:
            print('  [warn] 勾协议:', e)

        # 点登录（滑块容器可能拦截点击，先 force 试一次）
        print('[3/5] 提交登录')
        try:
            btn = page.locator('button.account-center-action-button')
            if btn.count() > 0:
                btn.first.click(force=True, timeout=5000)
            else:
                page.locator('button:has-text("登录")').first.click(force=True, timeout=5000)
        except Exception as e:
            print('  [warn] 点登录:', e)
        time.sleep(3)

        # 检测拼图滑块 → 等人工拖完。
        # ★ 2026-09-17 改为**以「登录是否成功」为主判据**：抖店是 SPA，
        #   登录成功后 URL 可能一直停在 /login（旧逻辑 `'/login' not in url` 永远不成立），
        #   而 verify-center 的常驻 iframe 又会被误判成滑块 → 死等满 300s 才走。
        #   现在只要看到「请选择店铺/工作台」就立刻放行。
        print('[4/5] 等待登录结果（若出现拼图验证弹窗则需人工拖动）')
        print('      ⚠️ 拖完滑块后**请不要关闭 Chrome 窗口** —— 脚本会自己关；')
        print('         提前关窗口会让已登录的凭证保存不下来（前端会一直显示「已过期」）。')
        _snap(page, '_dd_before_captcha.png')
        warned = has_captcha(page)
        if warned:
            print('  ⚠️⚠️ 出现拼图验证弹窗！请在弹出的窗口里**按住左边按钮拖动**把拼图补齐')
            print('      （弹窗标题「请完成下列验证后继续」，最多等 300 秒）')
        else:
            print('  未检测到滑块，等待登录跳转...')
        deadline = time.time() + 300
        last, landed, saved_early = 0, False, None
        while time.time() < deadline:
            if login_landed(page):
                landed = True
                break
            # ★ 浏览器被关掉就立刻退出，别干等满 300s 才报「未见登录成功信号」——
            #   那会让用户以为是平台问题，其实只是窗口被提前关了。
            try:
                page.title()
            except Exception:
                print('  [FAIL] Chrome 窗口已被关闭 —— 登录流程无法继续，登录态拿不到。')
                print('         下一轮里请在拖完滑块后**不要手动关窗口**，'
                      '脚本走完会自己关（约需 1~2 分钟）。')
                return False
            if not warned and has_captcha(page):
                warned = True
                print('  ⚠️⚠️ 出现拼图验证弹窗！请按住左边按钮拖动把拼图补齐（最多等 300 秒）')
            waited = int(300 - (deadline - time.time()))
            if waited // 30 > last:
                last = waited // 30
                print('      ...已等待 %ds（剩余 %ds）' % (waited, 300 - waited))
            time.sleep(2)
        _snap(page, '_dd_after_captcha.png')
        if landed:
            print('  ✅ 登录成功：%s' % page.url[:80])
            # ★★ 登录成功的**当下**立刻落盘（2026-09-18 事故后新增）。
            #    后面还有选店铺 / 工作台验证 / 罗盘探测 —— 那些都只是「锦上添花」，
            #    而凭证是命根子：任何一个后续步骤抛异常（最常见是用户顺手关了窗口），
            #    都不能让已经到手的登录态丢掉。此刻浏览器一定还活着（刚 evaluate 过）。
            saved_early = _dump_state(ctx, email, shop_name, '登录成功后即时')
        else:
            print('  [warn] 300s 内未见登录成功信号，截图 _dd_after_captcha.png')

        # 滑块通过后若仍在登录页 → 再点一次登录按钮（force 绕过残留容器拦截）
        time.sleep(2)
        if '/login' in page.url:
            print('  仍在登录页，补点一次登录按钮...')
            try:
                btn = page.locator('button.account-center-action-button')
                if btn.count() > 0:
                    btn.first.click(force=True, timeout=5000)
                else:
                    page.locator('button:has-text("登录")').first.click(force=True, timeout=5000)
                time.sleep(4)
            except Exception as e:
                print('  [warn] 补点登录:', e)

        # 等待「请选择店铺」页面（13 店共用 1 邮箱，登录后必须选店铺）
        print('[4/5] 等待店铺选择页...')
        has_shop_page = False
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if page.locator('text=请选择店铺').count() > 0:
                    has_shop_page = True
                    break
            except Exception:
                pass
            if '/login' not in page.url:
                break
            time.sleep(2)

        if has_shop_page:
            target = shop_name or '御车宝周口驰为网络科技有限公司专卖店'
            print('  店铺选择页出现，点击目标店铺:', target)
            _snap(page, '_dd_shop_select.png')
            # ★ 凭证已在上一步落盘 → 这里崩掉也不能中断流程（浏览器被关是最常见原因）
            try:
                item = page.locator('text=%s' % target)
                if item.count() == 0:
                    print('[FAIL] 店铺选择页未找到目标店铺: %s' % target)
                    # 打印可选店铺
                    try:
                        body_txt = page.inner_text('body')
                        print('--- 页面文本（截断）---')
                        print(body_txt[:1500])
                    except Exception:
                        pass
                    browser.close()
                    return False
                item.first.click()
                time.sleep(8)
            except Exception as e:
                print('  [warn] 选择店铺失败（浏览器可能已被关闭）：%s' % e)
        else:
            print('  未出现店铺选择页（可能已记住上次选择）')

        # 等待 URL 离开 login
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if '/login' not in page.url and 'jinritemai.com' in page.url:
                    break
            except Exception:
                break
            time.sleep(2)

        # ★ 选完店铺后再存一次：此时带店铺上下文的 cookie 更完整，
        #   覆盖掉上面「登录成功即时」那份兜底版本（同路径覆盖，不留垃圾文件）。
        _dump_state(ctx, email, shop_name, '选店铺后')

        # 最终验证：访问工作台首页。抖店是 SPA —— URL 可能不跳但内容渲染登录表单，
        # 必须同时检查「URL 不含 /login」+「页面无登录输入框」
        print('[5/5] 验证登录态（访问工作台）')
        try:
            page.goto(WORKBENCH_URL, wait_until='domcontentloaded', timeout=30000)
        except Exception:
            pass
        time.sleep(10)
        _snap(page, '_dd_after_login.png')
        login_form = 0
        for s in ['input[name=mobile]', 'input[name=email]', 'text=扫码登录', 'text=手机登录']:
            try:
                if page.locator(s).count() > 0 and page.locator(s).first.is_visible():
                    login_form += 1
            except Exception:
                pass
        try:
            cur_url = page.url or ''
        except Exception:
            cur_url = ''
        url_ok = '/login' not in cur_url
        print('  URL:', cur_url[:80])
        print('  URL 判定:', '过' if url_ok else '在登录页', '| 页面登录表单特征:', login_form, '个')
        if (not url_ok) or login_form >= 2:
            print('[FAIL] 登录态无效（页面仍是登录表单），截图 _dd_after_login.png')
            browser.close()
            return False

        print('  工作台验证通过')
        # ★ 第三次（正式）保存：覆盖前面两份，取最完整的一份。
        #   若浏览器已在此刻被关闭 → 返回 None，但前面那份仍在，不影响这次登录的成果。
        final = _dump_state(ctx, email, shop_name, '工作台验证通过')
        if not final:
            print('  [warn] 本次未能导出登录态；若前面已提示「凭证已落盘」则登录态仍然有效。')
            browser.close()
            return bool(saved_early)

        # 同一会话内立即验证罗盘
        print('[+] 同会话验证罗盘...')
        try:
            page.goto('https://compass.jinritemai.com/shop/commodity/product-list',
                      wait_until='domcontentloaded', timeout=40000)
        except Exception:
            pass
        time.sleep(10)
        _snap(page, '_dd_compass_same.png')
        qr = 0
        for s in ['text=扫码登录', 'img[class*="qrcode"]', 'text=抖音App扫码']:
            try:
                if page.locator(s).count() > 0:
                    qr += 1
            except Exception:
                pass
        try:
            compass_url = page.url or ''
        except Exception:
            compass_url = ''
        print('  罗盘 URL:', compass_url[:80], '| 扫码特征:', qr, '个')

        browser.close()
        return True


if __name__ == '__main__':
    # 用法: python login_save_state.py [店铺名]
    # 店铺名需与「请选择店铺」页面显示的一致，如「御车宝周口驰为网络科技有限公司专卖店」
    shop = sys.argv[1] if len(sys.argv) > 1 else None
    login(shop)
