# -*- coding: utf-8 -*-
"""千牛登录 + 保存 Storage State（一次性人工动作，本地有头模式跑）。

流程：
1. 启动浏览器（channel=chrome，有头，反检测）
2. 打开 loginmyseller.taobao.com
3. 遍历 frames 找到跨域 iframe「alibaba-login-box」，填账号密码登录
4. 滑块：自动试 1 次，失败则停下等人工拖（有头模式）
5. 判定登录成功（cookie 有 unb + cookie2/sgcookie）
6. storage_state() 导出 → 写回「千牛账号表.登录状态」

用法：
  python login_save_state.py <账号> [--headless]
  python login_save_state.py all   # 逐个登录所有运营中的店（不推荐一次跑完，滑块多）

账号格式：子账号写「主账号:子账号」，如「贝朵星球母婴用品:螃蟹」
"""
import sys
import os
import time
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 本地登录脚本：state 只存本地文件（MySQL 3306 不对公网开放，上传写库由 upload_state.py 走 SSH 完成）
# 账号密码来源：优先从服务器库读（若可连），否则用内置 ACCOUNTS 兜底
ACCOUNTS = {
    '贝朵星球母婴用品:螃蟹': 'pcl520526',
    '茵贝旗舰店:螃蟹': 'pcl520526',
    'tb561659733566:螃蟹': 'pcl520526',
    '御车宝汽车用品旗舰店:螃蟹': 'pcl520526',
    '暖序家居旗舰店:螃蟹': 'pcl520526',
    '古豪旗舰店:彦伟': 'abc123456',
    '出胜豫海专卖店:樱子': 'yh123456',
    '徕逸汽车用品旗舰店:影刀': 'zxc123456',
    '御车宝道晴专卖店:螃蟹': 'pcl520526',
    '优比熊旗舰店:螃蟹': 'pcl520526',
    '御车宝豫鹰专卖店:螃蟹': 'pcl520526',
    '皇状元汽车用品旗舰店:螃蟹': 'pcl520526',
    '出胜汽车用品旗舰店:螃蟹': 'pcl520526',
    '俊徽车品旗舰店:螃蟹': 'pcl520526',
    '锦耐车品旗舰店:螃蟹': 'pcl520526',
    '西西猫旗舰店:螃蟹': 'pcl520526',
}

STATE_DIR = os.path.join(BASE_DIR, '_states')

try:
    import shops  # noqa: E402
    HAS_SHOPS = True
except Exception:
    HAS_SHOPS = False

from playwright.sync_api import sync_playwright  # noqa: E402

LOGIN_URL = 'https://loginmyseller.taobao.com/'

# 本地浏览器：channel 用 chrome
CHROME_CHANNEL = 'chrome'


def _pwd(account):
    if HAS_SHOPS:
        try:
            s = shops.get_shop(account)
            if s and s.get('密码'):
                return s['密码']
        except Exception:
            pass  # 连不上库就回退内置 ACCOUNTS
    return ACCOUNTS.get(account, '')


def _save_state_file(account, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, account.replace(':', '_').replace('/', '_') + '.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False)
    print('  state 已存本地:', path)
    return path


def is_logged_in(ctx):
    ck = {c['name']: c['value'] for c in ctx.cookies() if 'taobao.com' in c.get('domain', '')}
    return bool(ck.get('unb')) and bool(ck.get('cookie2') or ck.get('sgcookie'))


def find_login_frame(page):
    """遍历 frames，找到含登录表单的跨域 iframe。"""
    for f in page.frames:
        try:
            if f.locator('#fm-login-id').count() > 0:
                return f
        except Exception:
            pass
    return None


def try_slide(page):
    """自动尝试滑块一次（阿里 baxia 老版）。返回 True=通过 False=失败。"""
    try:
        # 找嵌套 iframe 里的滑轨和手柄
        track = None
        handle = None
        for f in page.frames:
            if f.locator('#nc_1_n1t').count() > 0:
                track = f.locator('#nc_1_n1t')
                handle = f.locator('#nc_1_n1z')
                break
        if not track or not handle:
            return False
        tb = track.bounding_box()
        hb = handle.bounding_box()
        if not tb or not hb:
            return False
        # 终点：手柄左边缘贴住滑轨右边缘
        target_x = tb['x'] + tb['width'] - hb['width'] / 2
        start_x = hb['x'] + hb['width'] / 2
        start_y = hb['y'] + hb['height'] / 2
        # 预热
        handle.hover()
        time.sleep(0.6)
        # 按下
        handle.dispatch_event('mousedown', {'clientX': start_x, 'clientY': start_y, 'buttons': 1})
        time.sleep(0.26)
        # 分段拖拽（ease-in-out 64 步）
        steps = 64
        for i in range(1, steps + 1):
            ratio = i / steps
            # ease-in-out
            eased = ratio * ratio * (3 - 2 * ratio)
            x = start_x + (target_x - start_x) * eased
            y = start_y + (-1 if i % 2 else 1) * 1
            handle.dispatch_event('mousemove', {'clientX': x, 'clientY': y, 'buttons': 1})
            time.sleep(0.02)
        time.sleep(0.42)
        handle.dispatch_event('mouseup', {'clientX': target_x, 'clientY': start_y, 'buttons': 0})
        time.sleep(1.5)
        # 判读：滑块是否消失
        for f in page.frames:
            if f.locator('#nc_1_n1t').count() > 0:
                return False
        return True
    except Exception as e:
        print('  [slide error]', e)
        return False


def login_one(account, headless=False):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel=CHROME_CHANNEL,
            headless=headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-first-run',
                '--no-default-browser-check',
                '--no-sandbox',
            ],
        )
        ctx = browser.new_context(
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
            viewport={'width': 1400, 'height': 900},
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        print('[1/6] 打开登录页...')
        page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=60000)
        time.sleep(3)

        # 找到登录 iframe
        frame = find_login_frame(page)
        if not frame:
            print('  [warn] 未找到登录 iframe，尝试直接主文档定位')
            frame = page
        print('[2/6] 切换「密码登录」标签')
        try:
            txt_links = frame.locator("text=密码登录")
            if txt_links.count() > 0:
                txt_links.first.click()
                time.sleep(1)
        except Exception as e:
            print('  [warn] 切换密码登录失败', e)

        print('[3/6] 填账号密码')
        frame.locator('#fm-login-id').fill(account)
        time.sleep(0.3)
        pwd = _pwd(account)
        frame.locator('#fm-login-password').fill(pwd)
        time.sleep(0.3)

        print('[4/6] 提交登录')
        frame.locator("button[type='submit']").click()
        time.sleep(4)

        # 处理滑块
        print('[5/6] 检查滑块')
        slide_tried = False
        for attempt in range(1):  # 只自动试 1 次
            if is_logged_in(ctx):
                break
            if try_slide(page):
                print('  滑块已通过')
                slide_tried = True
                time.sleep(3)
            else:
                slide_tried = True
                print('  自动滑块未通过，等待人工拖拽（有头模式）...')
                # 有头模式下等待人工操作，最多 120 秒
                for _ in range(120):
                    if is_logged_in(ctx):
                        print('  人工登录成功')
                        break
                    time.sleep(1)

        if not is_logged_in(ctx):
            # 再给一点时间等跳转
            time.sleep(5)
        if not is_logged_in(ctx):
            print('[FAIL] 登录未成功。账号=%s' % account)
            browser.close()
            return False

        print('[6/6] 登录成功，导出 storage_state')
        state = ctx.storage_state()
        _save_state_file(account, state)

        # 验证一下能访问生意参谋
        try:
            page.goto('https://sycm.taobao.com/portal/home.htm', wait_until='domcontentloaded', timeout=30000)
            time.sleep(3)
            print('  生意参谋首页访问：', page.title()[:30])
        except Exception as e:
            print('  [warn] 生意参谋访问异常', e)

        browser.close()
        return True


def main():
    args = [a for a in sys.argv[1:]]
    headless = '--headless' in args
    args = [a for a in args if a != '--headless']

    if not args:
        print(__doc__)
        return
    target = args[0]

    if target == 'all':
        for acct in ACCOUNTS:
            print('\n======== 登录：%s ========' % acct)
            try:
                login_one(acct, headless=headless)
            except Exception as e:
                print('  异常：', e)
    else:
        login_one(target, headless=headless)


if __name__ == '__main__':
    main()
