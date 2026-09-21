# -*- coding: utf-8 -*-
"""抖音「登录态保鲜」工具（本机版 · 推荐路径）

为什么必须在本机跑
------------------
服务器（腾讯云 IDC）也能出二维码，但那张码要经过
「服务器截图 → 下载到本地 → 展示给你 → 你掏手机点开抖音扫一扫」才能被扫到，
这条链路实测 60~90 秒；而抖音 PC 登录码的**可扫窗口只有约 60 秒**
→ 服务器出码必然「一扫就报已过期」。

2026-09-17 实测对照：
  · 服务器出的码本身是**真码**（连续采样 6 轮无「过期」字样，且每 3 分钟自动换新）
  · 15:13:54 生成 → 15:15:10 用户扫码报过期，间隔 76 秒 → 死在窗口外，不是码坏了
  · 结论：服务器出码 = 链路比码的寿命长，属物理限制，改代码救不回来

本机跑则二维码直接弹在你自己桌面上，零传递延迟；且住宅 IP + 真实桌面 Chrome
风控最友好（抖店当年就是「服务器登不进、本机秒过」，差别在 IP 信誉/渲染指纹）。

用法
----
    python tools/douyin_login.py      # 或双击项目根目录的「启动抖音扫码登录.bat」

流程
----
 1. 弹出可见的系统 Chrome（★ 不用 Playwright 自带 chromium，免 install 且指纹真实）
 2. 已是登录态 → 直接导出 Cookie，跳过扫码
 3. 未登录 → 自动点出登录框 → 你手机扫码 → 自动导出并 **上传到服务器**
 4. 服务器 `/opt/ecom/tools/douyin_cookie.txt` 落盘，定时抓取无需人工介入

退出码：0 成功且已上传；2 超时未登录；3 Cookie 已存本地但上传失败；1 异常
"""
import os
import sys
import time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(BASE_DIR, 'douyin_profile')
COOKIE_FILE = os.path.join(BASE_DIR, 'douyin_cookie.txt')
# 热点宝抓取器读取此文件。扫码工具同步写入，避免“已登录但 07:00 抓取仍缺 Cookie”。
HOT_COOKIE_FILE = os.path.join(BASE_DIR, 'douyin_hot_cookie.txt')
HOME_URL = 'https://douhot.douyin.com/square/hotspot?active_tab=hotspot_search&date_window=24&sub_type=3001'
# 热点宝使用独立域名 Cookie；兼容普通抖音网页和热点宝两种登录态。
LOGIN_COOKIE = ('sessionid_douhot', 'sessionid', 'sessionid_ss')
WAIT_TIMEOUT = 6 * 60
POLL_INTERVAL = 2

# ★ 用系统 Chrome：指纹真实；也避免依赖 `playwright install chromium`（那步常被网络卡住）
CHROME_CHANNEL = 'chrome'
LAUNCH_ARGS = ['--disable-blink-features=AutomationControlled', '--no-first-run',
               '--no-default-browser-check']
INIT_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

REMOTE_COOKIE = '/opt/ecom/tools/douyin_cookie.txt'
REMOTE_HOT_COOKIE = '/opt/ecom/tools/douyin_hot_cookie.txt'

# 登录入口：JS 精确定位「登录」叶子节点 → 取真实坐标 → 鼠标点击
# ★ 教训：`button:has-text("登录")` / locator.click 会匹配到祖先或被遮挡节点，
#   一直等「可点击」直到超时；改坐标点击等同真人操作，稳。
FIND_LOGIN_JS = r"""
() => {
  const out = [];
  for (const e of document.querySelectorAll('*')) {
    const t = (e.textContent || '').trim();
    if (t !== '登录' || e.children.length > 0) continue;
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    if (r.width < 4 || r.height < 4) continue;
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
    out.push({x: r.x, y: r.y, w: r.width, h: r.height, tag: e.tagName});
  }
  out.sort((a, b) => (a.y - b.y) || (b.x - a.x));   // 首页登录入口在右上角
  return out;
}
"""


def _pick_cookie(context, name):
    names = (name,) if isinstance(name, str) else tuple(name)
    for c in context.cookies():
        if c['name'] in names:
            return c['value'] or ''
    return ''


def _cookie_str(context):
    return '; '.join('%s=%s' % (c['name'], c['value']) for c in context.cookies())


def _save_cookie(context):
    s = _cookie_str(context)
    with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
        f.write(s)
    with open(HOT_COOKIE_FILE, 'w', encoding='utf-8') as f:
        f.write(s)
    return s


def _find_login(page):
    try:
        return page.evaluate(FIND_LOGIN_JS) or []
    except Exception as e:
        print('      [警告] 定位登录入口失败：%s' % str(e)[:120])
        return []


def _click_login(page):
    """点出登录框（JS 定位 + 真实鼠标点击）。返回 (是否点到, 说明)"""
    cands = _find_login(page)
    if not cands:
        return False, '页面上找不到可见的「登录」入口'
    c = cands[0]
    cx, cy = c['x'] + c['w'] / 2.0, c['y'] + c['h'] / 2.0
    try:
        page.mouse.click(cx, cy)
    except Exception as e:
        return False, '鼠标点击失败：%s' % str(e)[:110]
    return True, ('已自动点出登录框（候选 %d 个，坐标 %.0f,%.0f）' % (len(cands), cx, cy))


def _upload(local_path, remote_path=REMOTE_COOKIE):
    """把 Cookie 传到服务器。

    连接参数直接复用 .deploy/ssx.py，避免 SSH 密码在两处各写一份。
    返回 (是否成功, 说明)。
    """
    try:
        import paramiko
    except Exception as e:
        return False, 'paramiko 不可用：%s' % str(e)[:100]

    host, user, pwd = '119.45.187.154', 'root', 'pcl520526.'
    dep = os.path.join(os.path.dirname(BASE_DIR), '.deploy')
    if dep not in sys.path:
        sys.path.insert(0, dep)
    try:
        import ssx                      # noqa: F401  （只取连接参数）
        host, user, pwd = ssx.HOST, ssx.USER, ssx.PWD
    except Exception:
        pass

    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host, 22, user, pwd, timeout=20, banner_timeout=20, auth_timeout=20)
        sftp = c.open_sftp()
        sftp.put(local_path, remote_path)
        size = sftp.stat(remote_path).st_size
        sftp.close()
        c.close()
        return True, '已上传 %s（%d 字节）' % (remote_path, size)
    except Exception as e:
        return False, '上传失败：%s' % str(e)[:180]


def main():
    from playwright.sync_api import sync_playwright

    print('=' * 60)
    print('抖音登录态保鲜（本机扫码 → 自动上传服务器）')
    print('浏览器  : 系统 Chrome（channel=%s）' % CHROME_CHANNEL)
    print('Profile : %s' % USER_DATA_DIR)
    print('本地存档: %s' % COOKIE_FILE)
    print('服务器  : %s' % REMOTE_COOKIE)
    print('=' * 60)

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                USER_DATA_DIR, channel=CHROME_CHANNEL, headless=False,
                viewport={'width': 1280, 'height': 900}, args=LAUNCH_ARGS,
                locale='zh-CN', timezone_id='Asia/Shanghai')
        except Exception as e:
            print('[错误] 拉起系统 Chrome 失败：%s' % str(e)[:220])
            print('       请确认已装 Google Chrome；或把 CHROME_CHANNEL 改成 None 走内置 chromium')
            return 1
        ctx.set_default_timeout(60000)
        ctx.add_init_script(INIT_JS)
        page = ctx.new_page()

        print('[1/4] 打开抖音网页版 ...')
        try:
            page.goto(HOME_URL, wait_until='domcontentloaded', timeout=60000)
        except Exception as e:
            print('      [警告] 首页加载异常（继续尝试）：%s' % str(e)[:150])
        time.sleep(5)

        sid = _pick_cookie(ctx, LOGIN_COOKIE)
        cands = _find_login(page)
        print('      可见「登录」入口 %d 个 | sessionid: %s'
              % (len(cands), '已存在' if sid else '不存在'))

        if sid and not cands:
            s = _save_cookie(ctx)
            print('[2/4] ✅ 检测到已是登录态，直接导出（%d 字节，%d 个字段）'
                  % (len(s), len(ctx.cookies())))
        else:
            print('[2/4] 未检测到登录态，自动点出登录框 ...')
            clicked, why = _click_login(page)
            print('      %s' % why)
            if not clicked:
                print('      ⚠ 请在弹出的浏览器窗口里手动点右上角「登录」')

            print('[3/4] 请用手机「抖音 App」扫浏览器窗口里的二维码')
            print('      扫完在手机上点「确认登录」。（最多等 %d 分钟）' % (WAIT_TIMEOUT // 60))
            deadline = time.time() + WAIT_TIMEOUT
            last_beat = time.time()
            got = False
            while time.time() < deadline:
                if _pick_cookie(ctx, LOGIN_COOKIE):
                    time.sleep(3)                 # 二次确认，避免抓到中间态
                    if _pick_cookie(ctx, LOGIN_COOKIE):
                        got = True
                        break
                if time.time() - last_beat > 30:
                    last_beat = time.time()
                    print('      ... 仍在等待扫码（剩 %d 秒）'
                          % max(0, int(deadline - time.time())))
                time.sleep(POLL_INTERVAL)

            if not got:
                print('[结果] ❌ 超时未检测到登录。请重新运行本脚本再试。')
                ctx.close()
                return 2
            s = _save_cookie(ctx)
            print('[3/4] ✅ 扫码登录成功，已导出完整 Cookie（%d 字节，%d 个字段）'
                  % (len(s), len(ctx.cookies())))

        print('[4/4] 上传到服务器 ...')
        if '--local-only' in sys.argv:
            print('      ✅ 已仅保存本地登录态（未上传服务器）')
            ctx.close()
            return 0
        up_ok, msg = _upload(COOKIE_FILE)
        print('      %s%s' % ('✅ ' if up_ok else '❌ ', msg))
        hot_ok, hot_msg = _upload(HOT_COOKIE_FILE, REMOTE_HOT_COOKIE)
        print('      %s%s' % ('✅ ' if hot_ok else '❌ ', hot_msg))
        up_ok = up_ok and hot_ok
        sid = _pick_cookie(ctx, LOGIN_COOKIE)
        print('      sessionid 前缀：%s...' % sid[:12])
        ctx.close()

    print('')
    if not up_ok:
        print('Cookie 已存在本地，可手动补传：')
        print('  python .deploy/ssx.py up tools/douyin_cookie.txt %s' % REMOTE_COOKIE)
        return 3
    print('完成。服务器定时抓取会自动带上登录态，不再受匿名 18 条限制。')
    print('（若脚本正在「等待扫码」阶段报超时，请直接重新双击运行一次即可。）')
    return 0


if __name__ == '__main__':
    code = 1
    try:
        code = main()
    except KeyboardInterrupt:
        print('\n已手动中断。')
        code = 130
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.exit(code)
