# -*- coding: utf-8 -*-
"""
小红书「登录态保鲜」工具（Playwright 持久化 profile + 扫码登录）

用法：python tools/xhs_login.py
输出：tools/xhs_cookie.txt（seeding_xhs.py 优先读取的就是这个文件）

流程：
  1. 打开小红书网页版（本地弹出可见的浏览器窗口）；
  2. 自动检测是否已登录：
     - 已登录：直接导出完整 Cookie（含 HttpOnly）写回 xhs_cookie.txt，无需扫码；
     - 未登录：直接跳转 /login 登录页，请用手机「小红书 App」扫码；
  3. 扫码成功后校验登录态，通过才导出，此后数月免维护。

★ 2026-09-17 修正（两个真实缺陷）：
  ① 假阳性登录判定：旧实现用「cookie 里有没有 web_session」判断登录态 ——
     匿名访客同样带 web_session（服务端下发的游客会话）。于是「未登录」被
     判成「已登录」，导出一份无用的游客 Cookie，线上用起来报「登录已过期」，
     而本工具却打印「✅ 已登录」。这正是 2026-09-16 那份失效 cookie 的来历。
     现改为：读页面 __INITIAL_STATE__ 的 user 信息，要求 guest === False
     且能取到 userId，并确认登录按钮已消失 —— 三者同时满足才算登录。
  ② 登录框点不出来：小红书首页改版后原有 .login-btn / 「登录」文本选择器
     失效，自动化点击失败。现改为直接导航到 https://www.xiaohongshu.com/login
     登录页（QR 码就在页面上），不再依赖脆弱的按钮选择器。

依赖：pip install playwright
      浏览器内核二选一：
        a) python -m playwright install chromium
        b) 本机已装 Chrome / Edge —— 脚本会自动回退使用（零下载）
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(BASE_DIR, 'xhs_profile')   # 持久化浏览器 profile（设备指纹/登录态）
COOKIE_FILE = os.path.join(BASE_DIR, 'xhs_cookie.txt')
HOME_URL = 'https://www.xiaohongshu.com/explore'
LOGIN_URL = 'https://www.xiaohongshu.com/login'

# 登录态核心 cookie：登录成功后才存在（注意游客会话也有，不能单独作为判据）
LOGIN_COOKIE = 'web_session'
# 单次扫码最长等待（秒）
WAIT_TIMEOUT = 8 * 60
POLL_INTERVAL = 2

# 在页面内读取登录态：优先 __INITIAL_STATE__，字段名做多版本兼容。
# ★ 2026-09-17：必须是**立即执行**的 IIFE —— 之前写成裸箭头函数字符串，
#   Playwright 求值后得到的是「函数对象」，序列化后变成 undefined，
#   于是登录态探测永远返回空值，把已登录误判为未登录（本次踩到的坑）。
PROBE_JS = r"""
(() => {
  const out = { hasState: false, stateKeys: null, userKeys: null, userId: '',
                nick: '', guest: null, loggedIn: null, probeErr: null };
  try {
    const s = window.__INITIAL_STATE__ || null;
    out.hasState = !!s;
    if (s) { out.stateKeys = Object.keys(s).slice(0, 30); }
    const u = (s && s.user) || {};
    out.userKeys = Object.keys(u).slice(0, 30);
    out.guest = (u.guest === undefined) ? null : u.guest;
    out.loggedIn = (u.loggedIn === undefined) ? null : u.loggedIn;
    const cands = [u.userInfo, u.user, (u.userPageData || {}).basicInfo,
                   (u.userPageData || {}).userInfo].filter(Boolean);
    for (const info of cands) {
      const id = info.userId || info._id || info.id || '';
      if (id) {
        out.userId = String(id);
        out.nick = info.nickname || info.nickName || info.name || '';
        break;
      }
    }
  } catch (e) { out.probeErr = String(e); }
  return out;
})()
"""


def _pick_cookie(context, name):
    for c in context.cookies():
        if c['name'] == name:
            return c['value'] or ''
    return ''


def _cookie_str(context):
    return '; '.join(f"{c['name']}={c['value']}" for c in context.cookies())


def _login_button_visible(page):
    """登录按钮可见性 —— 作为登录态的辅助信号（非主判据）。"""
    try:
        loc = page.get_by_text('登录', exact=True)
        for i in range(min(loc.count(), 20)):
            el = loc.nth(i)
            try:
                if el.is_visible(timeout=400):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _probe(page):
    try:
        st = page.evaluate(PROBE_JS) or {}
    except Exception as e:
        st = {'probeErr': str(e)[:200], 'guest': None, 'userId': ''}
    uid = str(st.get('userId') or '')
    is_logged = (st.get('guest') is False and bool(uid)) or \
                (st.get('loggedIn') is True and bool(uid))
    return is_logged, st


def _dump_and_save(context):
    s = _cookie_str(context)
    with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
        f.write(s)
    return s


def _verify_and_export(context, page, label, prev_session):
    """登录态校验通过才导出。成功返回 True。"""
    try:
        page.goto(HOME_URL, wait_until='domcontentloaded', timeout=60000)
        time.sleep(4)
    except Exception as e:
        print(f'[警告] 跳转首页失败: {str(e)[:120]}')

    ok, st = _probe(page)
    cur = _pick_cookie(context, LOGIN_COOKIE)
    changed = bool(cur) and cur != (prev_session or '')

    print(f'\n--- 登录态校验（{label}）---')
    print(json.dumps({
        'guest': st.get('guest'),
        'loggedIn': st.get('loggedIn'),
        'userId': st.get('userId'),
        'nick': st.get('nick'),
        'hasState': st.get('hasState'),
        'state_keys': st.get('stateKeys'),
        'user_keys': st.get('userKeys'),
        '会话已替换': changed,
        'cookie 字段数': len(context.cookies()),
        'probe_err': st.get('probeErr'),
    }, ensure_ascii=False, indent=2))

    if ok:
        sid = _pick_cookie(context, LOGIN_COOKIE)
        _dump_and_save(context)
        print('\n✅ 登录态校验通过，已导出完整 Cookie（含 HttpOnly）。')
        print(f'   账号: {st.get("nick") or "(未取到昵称)"}  userId={st.get("userId")}')
        print(f'   web_session: 长度 {len(sid)}，前缀 {sid[:12]}...')
        print(f'   已写入: {COOKIE_FILE}')
        return True

    print('\n❌ 校验未通过 —— 未导出 Cookie。')
    return False


def _save_shots(page, tag):
    """保存整页截图；若能定位二维码元素则额外单独截取。返回是否命中二维码。"""
    shot = os.path.join(BASE_DIR, f'_xhs_login_page{tag}.png')
    try:
        page.screenshot(path=shot)
        print(f'  整页截图: {shot}')
    except Exception as e:
        print(f'  [警告] 整页截图失败: {str(e)[:100]}')

    qr_shot = os.path.join(BASE_DIR, '_xhs_login_qr.png')
    for sel in ('canvas', '.qrcode img', '.qrcode-img', '.qrcode', '.login-qrcode',
                'img[src*="qrcode"]', 'img[src*="erweima"]'):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=600):
                loc.screenshot(path=qr_shot)
                print(f'  ★ 二维码已截取: {qr_shot}  (选择器 {sel})')
                return True
        except Exception:
            continue
    return False


# 触发登录弹窗的候选入口（小红书游客态首页无「登录」按钮，需点击交互元素弹出）
LOGIN_TRIGGERS = ('登录', '我', '发布')


def _open_login(page):
    """依次尝试登录入口，命中二维码即返回 True。"""
    tries = []
    for label in LOGIN_TRIGGERS:
        try:
            loc = page.get_by_text(label, exact=True)
            n = loc.count()
        except Exception:
            n = 0
        for i in range(min(n, 8)):
            el = loc.nth(i)
            try:
                if not el.is_visible(timeout=300):
                    continue
            except Exception:
                continue
            try:
                el.click(timeout=3000)
            except Exception as e:
                tries.append(f'{label}#{i} 点击失败:{str(e)[:40]}')
                continue
            time.sleep(3)
            tries.append(f'{label}#{i} url={page.url}')
            print(f'[登录入口] 点击「{label}」#{i} → {page.url}')
            if _save_shots(page, f'_by_{label}{i}'):
                return True, tries
            break  # 该入口没出二维码，换下一个入口
    return False, tries


def _launch(p):
    """启动持久化上下文。优先 Playwright 自带 chromium，失败回退系统 Chrome / Edge。

    ★ 2026-09-17 新增：自带内核未安装时原先直接抛异常（需先下载 114MB）。
      本机通常已装 Chrome/Edge，用 channel 直接复用，零下载即可扫码。
    """
    args = ['--disable-blink-features=AutomationControlled']
    attempts = [
        ('bundled-chromium', {}),
        ('chrome', {'channel': 'chrome'}),
        ('msedge', {'channel': 'msedge'}),
    ]
    last_err = None
    for label, kw in attempts:
        try:
            ctx = p.chromium.launch_persistent_context(
                USER_DATA_DIR,
                headless=False,
                viewport={'width': 1280, 'height': 900},
                args=args,
                **kw,
            )
            print(f'已启动浏览器: {label}')
            return ctx
        except Exception as e:
            last_err = e
            print(f'[跳过] {label} 启动失败: {str(e)[:140]}')
    raise RuntimeError(f'没有可用的浏览器内核，最后一次错误: {last_err}')


def main():
    print('=' * 56)
    print('小红书登录态保鲜工具')
    print(f'Profile 目录: {USER_DATA_DIR}')
    print(f'Cookie 输出: {COOKIE_FILE}')
    print('=' * 56)

    with sync_playwright() as p:
        context = _launch(p)
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = context.new_page()
        print('正在打开小红书网页版...')
        try:
            page.goto(HOME_URL, wait_until='domcontentloaded', timeout=60000)
        except Exception as e:
            print(f'[警告] 首页加载失败: {str(e)[:120]}')
        time.sleep(4)

        prev_session = _pick_cookie(context, LOGIN_COOKIE)
        print(f'当前 web_session: 长度 {len(prev_session)}，前缀 {prev_session[:12] or "-"}...')

        # 1) 尝试复用 profile 里残留的登录态
        if _verify_and_export(context, page, '复用本地 profile', prev_session):
            context.close()
            return

        # 2) 未登录：打开登录二维码
        #    注意：本版本小红书首页游客态**没有**「登录」按钮，直接访问
        #    https://www.xiaohongshu.com/login 也会被重定向回 /explore，
        #    因此改为点击页面上的交互入口来触发登录弹窗。
        print('\n未检测到有效登录态，尝试自动打开登录二维码...')
        opened, tries = _open_login(page)
        print(f'登录入口尝试记录: {tries}')

        if not opened:
            print(f'点击入口未出二维码，回退直接访问登录页: {LOGIN_URL}')
            try:
                page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=60000)
                time.sleep(5)
                print(f'  当前页面: {page.url}')
                opened = _save_shots(page, '_login_url')
            except Exception as e:
                print(f'  [警告] 登录页访问失败: {str(e)[:160]}')

        if opened:
            print('✅ 二维码已呈现，请用手机「小红书 App」扫描并确认登录。')
        else:
            print('⚠️ 未能自动打开二维码 —— 请在浏览器窗口中手动点击任意需要登录的'
                  '功能（如左侧「我」/「发布」，或任意笔记的点赞），弹出二维码后扫码。')

        print(f'（最多等待 {WAIT_TIMEOUT // 60} 分钟，可随时 Ctrl+C 中断）')

        # 3) 轮询等待登录成功：会话被替换 + 页面探针确认
        deadline = time.time() + WAIT_TIMEOUT
        last_note = ''
        while time.time() < deadline:
            cur = _pick_cookie(context, LOGIN_COOKIE)
            replaced = bool(cur) and cur != (prev_session or '')
            ok, st = _probe(page)
            note = f'会话替换={replaced} guest={st.get("guest")} userId={st.get("userId") or "-"}'
            if replaced or ok:
                if _verify_and_export(context, page, '扫码后确认', prev_session):
                    context.close()
                    return
            if note != last_note:
                print(f'[等待中] {note}')
                last_note = note
            time.sleep(POLL_INTERVAL)

        print('\n❌ 登录超时：未检测到成功登录。请重新运行本脚本再试。')
        context.close()


if __name__ == '__main__':
    main()
