# -*- coding: utf-8 -*-
"""抖店批量切店铺 + 保存各店 state（复用已有登录态，无需再过滑块）。

原理：13 店共用 1 邮箱，已登录会话里通过工作台右上角
「店铺名 → 切换组织/店铺 → 请选择店铺」切换店铺，每切一家导出一份
storage_state 存 _states/抖店_<邮箱>_<店铺名>.json，供 fetch_daily.py 使用。

用法：
  python switch_shops.py                # 切全部缺 state 的店
  python switch_shops.py "店铺名"       # 只切一家（店铺名与抖店账号表一致）
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

STATE_DIR = os.path.join(BASE_DIR, '_states')
WORKBENCH = 'https://fxg.jinritemai.com/ffa/mshop/homepage/index'
ANCHOR_SHOP = '御车宝周口驰为网络科技有限公司专卖店'  # 已有 state 的起点店


def state_path(shop_name):
    acc = shops.get_email_account()
    email = acc['邮箱'] if acc else 'pcl526@yeah.net'
    safe = ''.join(c for c in shop_name if c not in '\\/:*?"<>| ')
    return os.path.join(STATE_DIR, '抖店_%s_%s.json' % (
        email.replace('@', '_').replace('.', '_'), safe))


def find_corner_shop(page, keyword):
    """找右上角（x>1200,y<80）含店铺名的最小元素坐标。"""
    hits = page.evaluate("""
    (kw) => {
      const out = [];
      document.querySelectorAll('body *').forEach(el => {
        const t = (el.textContent || '').trim();
        if (t.includes(kw) && t.length < 40 && el.children.length <= 3) {
          const r = el.getBoundingClientRect();
          if (r.x > 1200 && r.y < 80 && r.width > 0) {
            out.push({x: r.x, y: r.y, text: t.slice(0, 30)});
          }
        }
      });
      return out;
    }
    """, keyword)
    if not hits:
        return None
    hits.sort(key=lambda h: -h['x'])
    return hits[0]


def current_corner_shop(page):
    """读右上角当前店铺名。"""
    txt = page.evaluate("""
    () => {
      const els = document.querySelectorAll('body *');
      for (const el of els) {
        const r = el.getBoundingClientRect();
        if (r.x > 1200 && r.y < 80 && r.width > 0 && el.children.length <= 3) {
          const t = (el.textContent || '').trim();
          if (t.length >= 4 && t.length < 40 && !['消息','反馈'].includes(t)) return t;
        }
      }
      return '';
    }
    """)
    return txt or ''


def switch_to(page, shop_name, ctx, cur_kw):
    """在当前已登录会话里切到目标店铺。cur_kw=当前店铺名关键字（用于定位右上角入口）。
    返回 True/False。"""
    corner = find_corner_shop(page, cur_kw)
    if not corner:
        print('  [FAIL] 未找到右上角店铺名入口（kw=%s）' % cur_kw)
        return False
    print('  入口: %s @ (%d,%d)' % (corner['text'][:20], corner['x'], corner['y']))
    # 点右上角店铺名 → 弹层里点「切换组织/店铺」（click/hover 两种触发都试）
    def _menu_open():
        """用 evaluate 判断下拉是否已出现「切换组织/店铺」。"""
        return page.evaluate("""
        () => {
          document.querySelectorAll('body *').forEach(() => {});
          const els = document.querySelectorAll('body *');
          for (const el of els) {
            const t = (el.textContent || '').trim();
            if (t === '切换组织/店铺' || t === '切换组织/店铺退出') {
              const r = el.getBoundingClientRect();
              if (r.width > 0 && r.height > 0) return {x: r.x, y: r.y, text: t};
            }
          }
          // 兜底：找精确叶子节点
          for (const el of els) {
            const t = (el.textContent || '').trim();
            if (t.includes('切换组织') && t.length < 20) {
              const r = el.getBoundingClientRect();
              if (r.width > 0) return {x: r.x, y: r.y, text: t};
            }
          }
          return null;
        }
        """)

    opened = False
    for attempt in range(3):
        page.mouse.click(corner['x'] + 10, corner['y'] + 10)
        time.sleep(3)
        m = _menu_open()
        if m:
            opened = True
            break
        page.screenshot(path=os.path.join(BASE_DIR, '_sw_debug_%d.png' % attempt))
        # 试 hover 触发
        page.mouse.move(corner['x'] + 10, corner['y'] + 10)
        time.sleep(1)
        page.mouse.move(corner['x'] + 40, corner['y'] + 12)
        time.sleep(2.5)
        m = _menu_open()
        if m:
            opened = True
            break
        # 关掉可能误开的弹层（按 ESC / 点空白处）再重试
        page.keyboard.press('Escape')
        page.mouse.click(600, 500)
        time.sleep(2)
    if not opened:
        print('  [FAIL] 下拉里没有「切换组织/店铺」（截图 _sw_debug_*.png）')
        return False
    page.mouse.click(m['x'] + 15, m['y'] + 10)
    time.sleep(4)

    # 等店铺选择页
    deadline = time.time() + 30
    has_shop_page = False
    while time.time() < deadline:
        if page.locator('text=请选择店铺').count() > 0:
            has_shop_page = True
            break
        time.sleep(2)
    if not has_shop_page:
        print('  [FAIL] 未出现店铺选择页 URL=%s' % page.url[:80])
        return False

    # 点目标店铺
    item = page.locator('text=%s' % shop_name)
    if item.count() == 0:
        print('  [FAIL] 店铺选择页未找到: %s' % shop_name)
        return False
    item.first.click()
    time.sleep(8)

    # 验证右上角店铺名变化（直接按目标店名找右上角元素）
    deadline = time.time() + 30
    while time.time() < deadline:
        chk = find_corner_shop(page, shop_name[:6])
        if chk:
            print('  切换成功 → %s' % shop_name)
            return True
        time.sleep(2)
    chk = find_corner_shop(page, shop_name[:6])
    print('  [warn] 右上角未确认到目标店铺')
    return bool(chk)


def latest_state():
    """取 _states/ 下最新修改的「带店铺名」state 文件（按活跃店铺反查）。返回 (路径, 店铺名)；无则 (None, None)。"""
    best_path, best_mtime, best_shop = None, -1, None
    for s in shops.get_active_shops():
        p = state_path(s['店铺名'])
        if os.path.exists(p):
            mt = os.path.getmtime(p)
            if mt > best_mtime:
                best_path, best_mtime, best_shop = p, mt, s['店铺名']
    return best_path, best_shop


def main():
    args = sys.argv[1:]
    all_shops = [s['店铺名'] for s in shops.get_active_shops()]

    if args and args[0] != 'all':
        targets = [s for s in all_shops if s == args[0]]
        if not targets:
            print('[FAIL] 抖店账号表无此店铺:', args[0])
            return
    else:
        targets = [s for s in all_shops if not os.path.exists(state_path(s))]

    print('待切换店铺 %d 家:' % len(targets))
    for t in targets:
        print('  -', t)
    if not targets:
        print('全部店铺 state 已就绪')
        return

    anchor_state, anchor_shop = latest_state()
    if not anchor_state:
        print('[FAIL] 无任何可用 state，先跑 login_save_state.py')
        return
    print('起点店（最新 state）:', anchor_shop)

    ok, fail = [], []
    cur_kw = anchor_shop  # 当前所在店铺名（定位右上角入口用）
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=False,
                                    args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
        # 从起点店 state 起步；之后复用同一 context 依次切
        ctx = browser.new_context(storage_state=anchor_state, locale='zh-CN',
                                  timezone_id='Asia/Shanghai',
                                  viewport={'width': 1600, 'height': 950})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        page.goto(WORKBENCH, wait_until='domcontentloaded', timeout=60000)
        time.sleep(12)

        body_txt = page.inner_text('body')
        if '/login' in page.url or '扫码登录' in body_txt:
            print('[FAIL] 登录态失效，请重跑 login_save_state.py')
            browser.close()
            return

        for shop_name in targets:
            print('\n======== 切换 → %s ========' % shop_name)
            try:
                if switch_to(page, shop_name, ctx, cur_kw[:6]):
                    state = ctx.storage_state()
                    path = state_path(shop_name)
                    os.makedirs(STATE_DIR, exist_ok=True)
                    with open(path, 'w', encoding='utf-8') as f:
                        json.dump(state, f, ensure_ascii=False)
                    print('  state 已存:', os.path.basename(path),
                          '| cookies:', len(state.get('cookies', [])))
                    ok.append(shop_name)
                    cur_kw = shop_name  # 下一家从当前店名继续切
                else:
                    fail.append(shop_name)
            except Exception as e:
                print('  异常:', e)
                fail.append(shop_name)

        browser.close()

    print('\n==== 结果 ====')
    print('成功 %d:' % len(ok))
    for s in ok:
        print('  ✓', s)
    print('失败 %d:' % len(fail))
    for s in fail:
        print('  ✗', s)


if __name__ == '__main__':
    main()
