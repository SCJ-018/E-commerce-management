# -*- coding: utf-8 -*-
"""抖店登录态「保活 + 体检」。

【为什么需要它】
 抖店（fxg.jinritemai.com）的登录态是**短效凭证**：
   2026-09-17 实测 —— 09:43 本机刷新 state 入库 → 09:53 服务器可免登录 →
   10:00 已失效（重定向回 /login/common）。
 也就是说隔夜必然失效 → 9:00 定时抓取只能走「邮箱登录」→ 触发字节
 verify-center 拼图滑块 → **无人值守必然失败**。

 这就是「上线后 9 点没抓到抖店数据」的根本原因（叠加当时 rc=0 假成功，
 连失败都被静默了）。

【本脚本做两件事】
 1. `--check`   体检：用库里的 state 打开工作台，判定登录态是否可用。
                结论写 `--status-file`，供 run_all.sh / 告警链路读取。
 2. 保活（默认） 体检通过后用**同一浏览器会话**重新导出 storage_state 并写回
                「抖店邮箱账号表.登录状态」。若抖店的会话是滑动续期，
                高频保活就能避免隔夜失效；若为硬 TTL，多次运行会暴露真实寿命
                （日志里有每次的可用/失效时间戳）。

【用法】
  python keepalive.py                 # 体检 + 保活（写回 state）
  python keepalive.py --check         # 只体检不写回
  python keepalive.py --status-file /opt/pw/logs/doudian_state_status.json
  python keepalive.py --headless      # 无头（⚠️ 可能触发风控，默认有头）

【退出码】
  0 = 登录态可用（已保活）
  4 = 登录态已失效（需人工重新登录：tools/doudian_crawler/login_save_state.py）
  1 = 脚本自身异常
"""
import sys
import os
import json
import time
import argparse
import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

IS_SERVER = os.path.exists('/opt/pw')
WORKBENCH = 'https://fxg.jinritemai.com/ffa/mshop/homepage/index'
DEFAULT_STATUS = ('/opt/pw/logs/doudian_state_status.json' if IS_SERVER
                  else os.path.join(BASE_DIR, '_state_status.json'))


def _now():
    return datetime.datetime.now().strftime('%F %T')


def _write_status(path, obj):
    obj = dict(obj)
    obj['ts'] = time.time()
    obj['time'] = _now()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print('  [warn] 写状态文件失败:', e)


JS_SHOP_ENTRY = """
() => {
  // ★ 用语义 class 定位，不用文字匹配：真实节点文本是**完整店名**
  //   （如「御车宝周口驰为网络科技有限公司专卖店」），且被 CSS 截断到 96px。
  const el = document.querySelector('[class*="headerShopName"]')
          || document.querySelector('[class*="index_userName"]');
  if (!el) return null;
  const t = (el.innerText || el.textContent || '').trim();
  const r = el.getBoundingClientRect();
  if (!t || r.width <= 0) return null;
  return {text: t, x: Math.round(r.x), y: Math.round(r.y)};
}
"""


def current_shop(page):
    """工作台右上角当前店铺名（完整文本）；读不到返回 None。"""
    try:
        info = page.evaluate(JS_SHOP_ENTRY)
    except Exception:
        info = None
    return (info or {}).get('text') or None


def main():
    ap = argparse.ArgumentParser(description='抖店登录态保活 + 体检')
    ap.add_argument('--check', action='store_true', help='只体检，不写回 state')
    ap.add_argument('--headless', action='store_true', help='无头模式（默认有头）')
    ap.add_argument('--status-file', default=DEFAULT_STATUS)
    ap.add_argument('--wait', type=int, default=14, help='页面等待秒数')
    a = ap.parse_args()

    # 必须在 import shops 之前拿到账号（shops 会连库：服务器 3306 / 本地 3307）
    import shops
    from playwright.sync_api import sync_playwright

    print('===== %s 抖店登录态%s =====' % (_now(), '体检' if a.check else '保活'))
    try:
        acc = shops.get_email_account()
    except Exception as e:
        print('[FAIL] 读「抖店邮箱账号表」失败:', e)
        _write_status(a.status_file, {'ok': False, 'reason': 'db-error: %s' % e})
        return 1
    if not acc or not acc.get('state'):
        print('[FAIL] 库里没有 state（从未登录过？）')
        _write_status(a.status_file, {'ok': False, 'reason': 'no-state'})
        return 4
    email = acc.get('邮箱') or ''
    print('账号: %s' % email)

    names = []
    try:
        names = [s['店铺名'][:6] for s in shops.get_active_shops()][:20]
    except Exception:
        pass

    rv = 1
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel='chrome', headless=a.headless,
            args=['--disable-blink-features=AutomationControlled', '--no-first-run',
                  '--no-default-browser-check', '--no-sandbox'])
        ctx = browser.new_context(storage_state=acc['state'], locale='zh-CN',
                                  timezone_id='Asia/Shanghai',
                                  viewport={'width': 1600, 'height': 950})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        try:
            page.goto(WORKBENCH, wait_until='domcontentloaded', timeout=60000)
            time.sleep(a.wait)

            # ★ 判据修正（2026-09-17，这次修的是**假阴性**）：
            #   原逻辑 = 「URL 含不含 /login」+「能否读到店名」，两个都会误报「失效」：
            #     · 抖店是 SPA，会话过期后的跳转是**客户端**跳转，URL 滞后几十秒 ——
            #       实测 11:42 体检打出「登录页=False 当前店=<读不到>」，而**同一份 state**
            #       用 /opt/pw/_probe_state.py 看已经是 /login/common 的登录表单了。
            #     · 登录后若停在「请选择店铺」（会话有效、只是没选店）同样读不到店名，
            #       老逻辑把它判成「失效」→ 误报，还会误导人去重新登录。
            #   现在以 **body 是否出现登录表单特征** 为主判据（它不依赖渲染时序），URL 只作辅助。
            url, body, login_form, cur = '', '', False, None
            for _ in range(4):
                url = page.url
                try:
                    body = page.inner_text('body')
                except Exception:
                    body = ''
                login_form = ('发送验证码' in body) or ('扫码登录' in body)
                if not login_form:
                    cur = current_shop(page)
                    if cur:
                        break
                time.sleep(4)

            pick_shop = '请选择店铺' in body
            # 会话有效 = 没有登录表单，且能读到店名 **或** 停在「请选择店铺」（二者都说明会话还在）
            ok = (not login_form) and (bool(cur) or pick_shop)
            print('URL: %s' % url[:90])
            print('判定: %s（登录表单=%s 当前店=%s 选店页=%s）'
                  % ('✅ 可用' if ok else '❌ 失效', login_form, cur or '<读不到>', pick_shop))

            if ok and not a.check:
                try:
                    shops.save_email_state(email, ctx.storage_state())
                    print('[+] 已用本会话重新导出并写回「抖店邮箱账号表.登录状态」')
                except Exception as e:
                    print('  [warn] 写回 state 失败:', e)
            rv = 0 if ok else 4
            _write_status(a.status_file, {
                'ok': ok, 'on_login': login_form, 'url': url[:200],
                'current_shop': (cur or ''), 'pick_shop': pick_shop,
                'email': email,
                'reason': '' if ok else ('login-page' if login_form else 'no-shop-entry'),
            })
        except Exception as e:
            print('[FAIL] 体检异常:', e)
            _write_status(a.status_file, {'ok': False, 'reason': 'exc: %s' % str(e)[:200]})
            rv = 1
        finally:
            try:
                browser.close()
            except Exception:
                pass

    if rv == 4:
        print('\n⚠️  抖店登录态已失效 —— 无人值守抓取不可能成功。')
        print('    需人工恢复：本机 `python tools/doudian_crawler/login_save_state.py`')
        print('    （过拼图滑块）→ `python tools/doudian_crawler/upload_state.py`')
    return rv


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
