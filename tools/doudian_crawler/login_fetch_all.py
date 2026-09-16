# -*- coding: utf-8 -*-
"""抖店一体化抓取：登录一次 → 同一会话内依次切店 → 罗盘抓数 → 落库。

背景：切店铺产生的 state 是短效临时授权（几分钟失效），
「每店独立 state」方案不成立。本脚本在单个浏览器会话内完成全部工作：
  1. 有头浏览器，优先用已有 state 免登录，失效则走邮箱登录（人工过拼图滑块，最多等 240s）
  2. 登录后选第一个目标店铺
  3. for 每家运营中店铺：切店 → 打开罗盘商品列表 → 页面内 fetch 翻页抓全 → upsert 云库
  4. 结束把会话 state 写回「抖店邮箱账号表.登录状态」

用法：
  python login_fetch_all.py [YYYY-MM-DD] [店铺名,店铺名]
  python login_fetch_all.py [YYYY-MM-DD] [店铺名,...] --no-map   # 抓完不自动跑品类增量映射
"""
import sys
import os
import time
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import shops  # noqa: E402
import fetch_daily  # noqa: E402  (fetch_products / save_rows / PRODUCT_LIST_URL)
import fetch_main  # noqa: E402  (主表 16 字段：core_index_v3 + income_expense + flow_overview)
from playwright.sync_api import sync_playwright  # noqa: E402

LOGIN_URL = 'https://fxg.jinritemai.com/login/common?channel=zhaoshang'
WORKBENCH = 'https://fxg.jinritemai.com/ffa/mshop/homepage/index'
STATE_DIR = os.path.join(BASE_DIR, '_states')


def has_captcha(page):
    for s in ['img.captcha-verify-image', '#vc_captcha_box', 'div.captcha-slider']:
        try:
            loc = page.locator(s)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    return True
        except Exception:
            pass
    return False


def do_login(page, first_shop):
    """邮箱登录 + 过滑块 + 选第一个店铺。"""
    acc = shops.get_email_account()
    email, pwd = acc['邮箱'], acc['密码']
    print('  邮箱登录:', email, '| 首选店铺:', first_shop)
    page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=60000)
    time.sleep(3)
    # 切「邮箱登录」tab：页面 React hydration 慢，刚加载完点击可能无效（点击丢失、
    # 表单不切换），必须重试点击并等 input[name=email] 真正出现（2026-09-16 踩坑：
    # 固定 sleep 后单次点击 → fill 30s 超时崩溃）
    deadline = time.time() + 30
    while time.time() < deadline and page.locator('input[name=email]').count() == 0:
        try:
            tab = page.locator('text=邮箱登录')
            if tab.count() > 0:
                tab.first.click()
        except Exception:
            pass
        time.sleep(2)
    if page.locator('input[name=email]').count() == 0:
        print('  [FAIL] 邮箱登录表单 30s 内未出现')
        return False
    page.locator('input[name=email]').first.fill(email)
    time.sleep(0.3)
    page.locator('input[name=password]').first.fill(pwd)
    time.sleep(0.3)
    try:
        agree = page.locator('input.auxo-checkbox-input')
        if agree.count() > 0 and not agree.first.is_checked():
            agree.first.click(force=True)
    except Exception:
        pass
    btn = page.locator('button.account-center-action-button')
    btn.first.click(force=True) if btn.count() > 0 else page.locator('button:has-text("登录")').first.click(force=True)
    time.sleep(3)

    if has_captcha(page):
        print('  ⚠️ 出现拼图滑块！请在浏览器窗口人工拖拽（最多等 240s）')
        deadline = time.time() + 240
        while time.time() < deadline and has_captcha(page):
            time.sleep(2)
        print('  滑块已消失' if not has_captcha(page) else '  [warn] 滑块仍在')
    time.sleep(2)
    if '/login' in page.url:
        try:
            btn = page.locator('button.account-center-action-button')
            btn.first.click(force=True) if btn.count() > 0 else page.locator('button:has-text("登录")').first.click(force=True)
            time.sleep(4)
        except Exception:
            pass

    # 等店铺选择页 → 点第一个目标店铺
    deadline = time.time() + 30
    while time.time() < deadline:
        if page.locator('text=请选择店铺').count() > 0:
            item = page.locator('text=%s' % first_shop)
            if item.count() == 0:
                print('  [FAIL] 店铺选择页未找到: %s' % first_shop)
                return False
            item.first.click()
            time.sleep(8)
            break
        if '/login' not in page.url:
            break
        time.sleep(2)

    # 双重验证：URL + 无登录表单
    try:
        page.goto(WORKBENCH, wait_until='domcontentloaded', timeout=30000)
    except Exception:
        pass
    time.sleep(10)
    body = page.inner_text('body')
    lf = sum(1 for s in ['input[name=mobile]', 'input[name=email]', 'text=扫码登录', 'text=手机登录']
             if page.locator(s).count() > 0)
    ok = '/login' not in page.url and lf < 2 and '扫码登录' not in body
    print('  登录验证:', '通过' if ok else '失败')
    return ok


def find_corner(page, kw):
    hits = page.evaluate("""
    (kw) => {
      const out = [];
      document.querySelectorAll('body *').forEach(el => {
        const t = (el.textContent || '').trim();
        if (t.includes(kw) && t.length < 40 && el.children.length <= 3) {
          const r = el.getBoundingClientRect();
          if (r.x > 1200 && r.y < 80 && r.width > 0) out.push({x: r.x, y: r.y});
        }
      });
      return out;
    }
    """, kw)
    if not hits:
        return None
    hits.sort(key=lambda h: -h['x'])
    return hits[0]


def switch_shop(page, target, cur_kw):
    """当前会话切到目标店铺。cur_kw=当前所在店铺名（由调用方跟踪）。"""
    # 切店必须在工作台页面操作（罗盘右上角下拉没有「切换组织/店铺」）
    if '/ffa/mshop' not in page.url:
        page.goto(WORKBENCH, wait_until='domcontentloaded', timeout=60000)
        time.sleep(10)

    # 关掉可能遮挡的运营弹窗（中秋报名/大促提示等，会挡住右上角下拉）
    for _ in range(2):
        closed = False
        try:
            for txt in ['我知道了', '我知道啦', '暂不', '关闭']:
                dlg = page.locator('text=%s' % txt)
                if dlg.count() > 0 and dlg.first.is_visible():
                    dlg.first.click()
                    time.sleep(1.5)
                    closed = True
                    break
        except Exception:
            pass
        if not closed:
            break

    # 已在该店：右上角直接能找到目标店名
    if find_corner(page, target[:6]):
        return True

    corner = find_corner(page, cur_kw[:6])
    if not corner:
        print('    [FAIL] 未找到右上角店铺名入口（kw=%s）' % cur_kw[:6])
        return False

    # 点击店铺名 → 弹层里点「切换组织/店铺」（click/hover 双触发 + 3 次重试）
    def _menu_open():
        sw = page.locator('text=切换组织/店铺')
        if sw.count() > 0 and sw.first.is_visible():
            return sw
        return None

    opened = None
    for attempt in range(3):
        page.mouse.click(corner['x'] + 10, corner['y'] + 10)
        time.sleep(3)
        opened = _menu_open()
        if opened:
            break
        page.screenshot(path=os.path.join(BASE_DIR, '_sw_debug_%d.png' % attempt))
        # 试 hover 触发
        page.mouse.move(corner['x'] + 10, corner['y'] + 10)
        time.sleep(1)
        page.mouse.move(corner['x'] + 40, corner['y'] + 12)
        time.sleep(2.5)
        opened = _menu_open()
        if opened:
            break
        # 关掉可能误开的弹层再重试
        page.keyboard.press('Escape')
        page.mouse.click(600, 500)
        time.sleep(2)
    if not opened:
        print('    [FAIL] 下拉里没有「切换组织/店铺」（截图 _sw_debug_*.png）')
        return False
    opened.first.click()
    time.sleep(4)

    deadline = time.time() + 30
    while time.time() < deadline and page.locator('text=请选择店铺').count() == 0:
        time.sleep(2)
    item = page.locator('text=%s' % target)
    if item.count() == 0:
        print('    [FAIL] 店铺选择页未找到: %s' % target)
        return False
    item.first.click()
    time.sleep(8)
    # 验证
    deadline = time.time() + 25
    while time.time() < deadline:
        chk = find_corner(page, target[:6])
        if chk:
            return True
        time.sleep(2)
    print('    [warn] 未确认切换到目标店')
    return False


def main():
    argv = [a for a in sys.argv[1:] if a != '--no-map']
    no_map = '--no-map' in sys.argv[1:]
    date_str = argv[0] if len(argv) > 0 else \
        (time.strftime('%Y-%m-%d', time.localtime(time.time() - 86400)))
    all_active = [s['店铺名'] for s in shops.get_active_shops()]
    all_shops = all_active
    # 可选第 2 参数：只抓指定店铺（逗号分隔），用于账号级限流后补抓失败小店。
    # ⚠️ 只影响「抓取循环」；登录态检查仍须在全部运营店铺里找当前店（见下方），
    # 否则 state 停在非目标店会被误判成「脏 state」→ 触发无谓登录（2026-09-16 踩坑）
    if len(argv) > 1:
        only = [x.strip() for x in argv[1].split(',') if x.strip()]
        missing = [x for x in only if x not in all_active]
        if missing:
            print('[warn] 指定店铺不在运营列表中，忽略: %s' % ', '.join(missing))
        all_shops = [x for x in all_active if x in only]
        if not all_shops:
            print('[FAIL] 过滤后无目标店铺，退出')
            return
    print('目标日期: %s | 抓取店铺 %d 家（运营中 %d 家）' % (date_str, len(all_shops), len(all_active)))

    # 优先用「抖店邮箱账号表.登录状态」（上次会话写回的最新 state）免登录
    reuse = None
    try:
        acc = shops.get_email_account()
        if acc and acc.get('state'):
            reuse = os.path.join(STATE_DIR, '_db_state.json')
            with open(reuse, 'w', encoding='utf-8') as f:
                json.dump(acc['state'], f, ensure_ascii=False)
            print('[登录] 尝试用 DB 会话 state 免登录...')
    except Exception as e:
        print('  [warn] 读 DB state 失败:', e)
    if not reuse:
        for fn in os.listdir(STATE_DIR):
            if fn.endswith('.json'):
                p = os.path.join(STATE_DIR, fn)
                if reuse is None or os.path.getmtime(p) > os.path.getmtime(reuse):
                    reuse = p

    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=False,
                                    args=['--disable-blink-features=AutomationControlled',
                                          '--no-first-run', '--no-default-browser-check', '--no-sandbox'])
        ctx = browser.new_context(
            storage_state=reuse if reuse else None, locale='zh-CN',
            timezone_id='Asia/Shanghai', viewport={'width': 1600, 'height': 950})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        page.goto(WORKBENCH, wait_until='domcontentloaded', timeout=60000)
        time.sleep(12)
        body = page.inner_text('body')
        logged = '/login' not in page.url and '扫码登录' not in body and '发送验证码' not in body
        logged_shop = None
        if logged:
            # 严格校验：右上角必须能找到「任一」运营中店铺名，否则是脏 state（切店后短效失效）。
            # 注意用 all_active（全部运营店），不能用 all_shops（可能被过滤成少数几家）
            for s in all_active:
                if find_corner(page, s[:6]):
                    logged_shop = s
                    break
            if not logged_shop:
                logged = False
        if logged:
            print('[登录] state 有效，免登录（当前在: %s）' % logged_shop)
        else:
            print('[登录] state 失效/脏，走邮箱登录（需要人工过滑块）...')
            if not do_login(page, all_shops[0]):
                print('[FAIL] 登录失败，退出')
                browser.close()
                return
            logged_shop = all_shops[0]

        # 依次抓取（cur_kw 跟踪当前所在店铺）
        cur_kw = logged_shop
        if not switch_shop(page, all_shops[0], cur_kw):
            print('[FAIL] 切到首店失败，退出')
            browser.close()
            return
        cur_kw = all_shops[0]
        conn = shops.get_conn()
        summary = []
        try:
            for i, shop_name in enumerate(all_shops, 1):
                print('\n======== [%d/%d] %s ========' % (i, len(all_shops), shop_name))
                shop = next(s for s in shops.get_active_shops() if s['店铺名'] == shop_name)
                if not switch_shop(page, shop_name, cur_kw):
                    summary.append((shop_name, '切店失败'))
                    continue
                cur_kw = shop_name
                print('  打开罗盘商品列表...')
                page.goto(fetch_daily.PRODUCT_LIST_URL, wait_until='domcontentloaded', timeout=60000)
                time.sleep(10)
                if 'passport' in page.url:
                    summary.append((shop_name, '罗盘需登录'))
                    continue
                try:
                    rows = fetch_daily.fetch_products(page, date_str)
                except Exception as e:
                    summary.append((shop_name, '取数异常: %s' % str(e)[:60]))
                    continue
                print('  共 %d 个商品，落库...' % len(rows))
                n = fetch_daily.save_rows(conn, shop, date_str, rows)
                conn.commit()
                # 主表 16 字段（店铺营销数据）：core_index_v3 + income_expense + flow_overview
                try:
                    fetch_main.fetch_one(page, conn, shop, date_str)
                    conn.commit()
                    summary.append((shop_name, 'OK %d 行 + 主表' % n))
                except Exception as e:
                    conn.rollback()
                    summary.append((shop_name, 'OK %d 行, 主表失败: %s' % (n, str(e)[:50])))
        finally:
            conn.close()

        # 会话 state 写回邮箱账号表（供下次尝试免登录）
        try:
            acc = shops.get_email_account()
            st = ctx.storage_state()
            shops.save_email_state(acc['邮箱'], st)
            print('\n[+] 会话 state 已写回「抖店邮箱账号表」')
        except Exception as e:
            print('  [warn] 写回 state 失败:', e)

        browser.close()

    print('\n============ 汇总 ============')
    bad = []
    for name, r in summary:
        print('  %-24s %s' % (name[:24], r))
        if not r.startswith('OK') or '主表失败' in r:
            bad.append((name, r))
    if bad:
        # ⚠️ 失败店铺的库里可能残留旧口径数据（如上一次的周口径），必须重抓。
        # 2026-09-16 踩坑：限流静默失败 → 旧周口径数据留在库里且无人发现。
        print('\n⚠️  以下 %d 家未完整落库，库里可能残留旧口径数据，务必重抓：' % len(bad))
        for n, r in bad:
            print('    - %-24s %s' % (n[:24], r))
        print('  重抓：python login_fetch_all.py %s "%s"'
              % (date_str, ','.join(n for n, _ in bad)))
    else:
        print('\n✅ 全部 %d 家完整落库' % len(summary))

    # ★ 抓取收尾：自动补齐商品品类映射（增量）。复用 fetch_daily 的钩子。
    #   放在「本轮全部店铺抓完之后」跑一次：映射按 (平台, 商品ID) 去重，
    #   逐店跑 13 次与跑 1 次结果完全相同，但只跑一次省掉 12 次全表扫描。
    if not no_map:
        fetch_daily.auto_category_map('抖店')


if __name__ == '__main__':
    main()
