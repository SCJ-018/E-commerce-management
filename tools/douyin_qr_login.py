# -*- coding: utf-8 -*-
"""抖音扫码登录（服务器版 · 方案乙）

为什么需要它
------------
原先保鲜靠「本机跑 tools/douyin_login.py 扫码 → 把 douyin_cookie.txt 传上服务器」，
需要你的电脑参与。本脚本把这一步搬到服务器上：直接出一张二维码图片，
你用手机扫完，Cookie 就落在服务器 `tools/douyin_cookie.txt`，
定时抓取无需任何人干预。

运行（服务器，必须在 xvfb 下用 /opt/pw 的解释器，与抖店链路同源）
--------------------------------------------------------------
    cd /opt/ecom/tools
    nohup xvfb-run -a /opt/pw/venv/bin/python douyin_qr_login.py \\
        > /tmp/douyin_qr.log 2>&1 &

产物
----
    tools/douyin_qrcode.png    登录二维码（下载给用户扫）
    tools/douyin_cookie.txt    登录成功后的完整 Cookie（含 HttpOnly 字段）
    tools/douyin_profile/      持久化浏览器 profile（下次已登录可直接导出）

退出码：0 = 登录成功并已写入 Cookie；2 = 超时未完成登录；1 = 异常

注意：抖音的登录风控比读数据严（抖店就卡在这一环），若平台弹出滑块/安全验证，
      脚本会截下当时的页面（tools/douyin_qrcode.png）供排查。
"""
import json
import os
import sys
import time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, 'douyin_cookie.txt')
QR_FILE = os.path.join(BASE_DIR, 'douyin_qrcode.png')
PROFILE_DIR = os.path.join(BASE_DIR, 'douyin_profile')
ACCOUNTS_FILE = os.path.join(BASE_DIR, 'seeding_accounts.json')

HOME_URL = 'https://www.douyin.com/'
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
LAUNCH_ARGS = ['--disable-blink-features=AutomationControlled', '--no-first-run',
               '--no-default-browser-check', '--no-sandbox']
INIT_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

# 登录入口：不在页面里找固定选择器，而是用 JS 精确定位「登录」叶子节点
# ★ 实测教训：`button:has-text("登录")` 这类选择器能匹配到祖先/被遮挡节点，
#   Playwright 的 locator.click 会一直等它「可点击」→ Timeout 4000ms 失败。
#   改为先取真实坐标、再用鼠标点击（等同于真人操作）。
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
    out.push({x: r.x, y: r.y, w: r.width, h: r.height,
              tag: e.tagName, cls: (e.className || '').toString().slice(0, 60)});
  }
  out.sort((a, b) => (a.y - b.y) || (b.x - a.x));   // 首页登录入口在右上角
  return out;
}
"""
# 二维码候选元素探测（用于诊断与精确裁切）
# 二维码特征：正方形、边长 100~420、多为 base64（src 以 data: 开头）或 canvas；
# 按特征打分排序，避免误裁到视频封面。
QR_PROBE_JS = r"""
() => {
  const out = [];
  for (const e of document.querySelectorAll('canvas, img')) {
    const r = e.getBoundingClientRect();
    if (r.width < 80 || r.height < 80) continue;
    const src = (e.getAttribute('src') || '');
    const ratio = r.width / Math.max(1, r.height);
    const item = {tag: e.tagName, w: Math.round(r.width), h: Math.round(r.height),
                  x: Math.round(r.x), y: Math.round(r.y),
                  isData: src.indexOf('data:') === 0,
                  square: ratio > 0.9 && ratio < 1.1,
                  src: src.slice(0, 36)};
    item.score = (item.isData ? 4 : 0) + (item.square ? 2 : 0)
               + ((item.w >= 100 && item.w <= 420) ? 1 : 0);
    out.push(item);
  }
  out.sort((a, b) => (b.score - a.score) || ((b.w * b.h) - (a.w * a.h)));
  return out.slice(0, 12);
}
"""
SESSION_COOKIE = 'sessionid'      # 登录成功后才有的核心字段
WAIT_TIMEOUT = 210                # 单轮等待（抖音二维码约 3 分钟失效，单轮别等太久）
MAX_ROUNDS = 3                    # 二维码失效后自动刷新，最多刷几轮
POLL = 2
DSF = 3                           # 截图缩放倍率：二维码放大 3 倍，手机更好扫
PAD = 28                          # 裁剪留白（CSS 像素，乘 DSF 后是 84 物理像素）
META_FILE = os.path.join(BASE_DIR, 'douyin_qr_meta.json')

# 二维码失效时，弹窗里会出现「已失效/已过期/点击刷新」之类的叶子节点 → 点它换新码
FIND_REFRESH_JS = r"""
() => {
  const keys = ['刷新', '失效', '过期', '重新获取', '已失效'];
  for (const e of document.querySelectorAll('*')) {
    const t = (e.textContent || '').trim();
    if (e.children.length > 0) continue;
    if (!keys.some(k => t.indexOf(k) >= 0)) continue;
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    if (r.width < 4 || r.height < 4) continue;
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
    return {x: r.x + r.width / 2, y: r.y + r.height / 2, text: t.slice(0, 20)};
  }
  return null;
}
"""

# 登录态校验：拉一个真实账号的作品列表，匿名时接口会带「看更多最新作品」引导
VERIFY_JS = r"""
async (args) => {
  const {sec, count} = args;
  const u = '/aweme/v1/web/aweme/post/?device_platform=webapp&aid=6383'
    + '&channel=channel_pc_web&sec_user_id=' + sec + '&max_cursor=0&count=' + count
    + '&cookie_enabled=true&platform=PC';
  try {
    const r = await fetch(u, {credentials: 'include'});
    const j = await r.json();
    const list = j.aweme_list || [];
    return {http: r.status, code: j.status_code, n: list.length,
            tip: j.not_login_module || null, hasMore: j.has_more};
  } catch (e) {
    return {err: String(e).slice(0, 120)};
  }
}
"""


def log(msg):
    print(msg, flush=True)


def _pick(context, name):
    for c in context.cookies():
        if c['name'] == name:
            return c['value'] or ''
    return ''


def _cookie_str(context):
    return '; '.join('%s=%s' % (c['name'], c['value']) for c in context.cookies())


def _save_cookie(context):
    s = _cookie_str(context)
    with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
        f.write(s)
    return s


def _write_meta(cand):
    """记录本轮二维码的生成时间与位置，供外部判断「这张码是几点出的、还能不能扫」。

    抖音二维码有效期约 3 分钟；meta 里的 ts 就是判定新鲜度的唯一依据。
    """
    try:
        meta = {
            'ts': time.time(),
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'size': ([cand['w'], cand['h']] if cand else None),
            'pos': ([cand['x'], cand['y']] if cand else None),
            'file': QR_FILE,
        }
        with open(META_FILE, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False)
    except Exception as e:
        log('[二维码] meta 写入失败：%s' % str(e)[:110])


def _refresh_qr(page):
    """让二维码换新：优先点弹窗里的「点击刷新」，退不到就整体重载页面重进登录。

    为什么要它：抖音二维码约 3 分钟失效，单轮等 8 分钟时，后 5 分钟里
    用户扫到的其实是死码 —— 上一轮的失败就是这么来的。
    """
    try:
        r = page.evaluate(FIND_REFRESH_JS)
    except Exception as e:
        log('[续期] 探测刷新入口失败：%s' % str(e)[:110])
        r = None
    if r:
        try:
            page.mouse.click(r['x'], r['y'])
            log('[续期] 已点击弹窗「%s」→ 二维码换新' % r['text'])
            return True
        except Exception as e:
            log('[续期] 点击「%s」失败：%s' % (r['text'], str(e)[:110]))
    log('[续期] 弹窗内无刷新入口，改为重载页面重新点「登录」')
    try:
        page.goto(HOME_URL, wait_until='domcontentloaded', timeout=60000)
        time.sleep(5)
        ok, why = _click_login(page)
        log('[续期] %s' % why)
        return ok
    except Exception as e:
        log('[续期] 重载失败：%s' % str(e)[:130])
        return False


def _find_login(page):
    """页面上可见的「登录」入口候选（按「最上、最右」排序）"""
    try:
        return page.evaluate(FIND_LOGIN_JS) or []
    except Exception as e:
        log('[警告] 定位登录入口失败：%s' % str(e)[:120])
        return []


def _click_login(page):
    """点出登录框：JS 定位 + 真实鼠标点击。返回 (是否点到, 说明)"""
    cands = _find_login(page)
    if not cands:
        return False, '页面上找不到可见的「登录」入口'
    c = cands[0]
    cx, cy = c['x'] + c['w'] / 2.0, c['y'] + c['h'] / 2.0
    try:
        page.mouse.click(cx, cy)
    except Exception as e:
        return False, '鼠标点击失败：%s' % str(e)[:110]
    return True, ('已点击「登录」入口（%s，候选 %d 个，坐标 %.0f,%.0f）'
                  % (c['tag'], len(cands), cx, cy))


def _shot_qr(page):
    """截二维码（供用户扫）。

    优先按坐标裁切：弹窗里的二维码是 canvas / base64 <img>，
    用 locator.screenshot 容易因动画或遮挡失败，坐标裁切实测更稳。
    取坐标前先滚到顶部，保证 getBoundingClientRect 与页面坐标一致。
    找不到候选就截整个视口，至少让用户能看到弹窗内容。
    """
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    cands = []
    try:
        cands = page.evaluate(QR_PROBE_JS) or []
    except Exception as e:
        log('[二维码] 候选探测失败：%s' % str(e)[:110])
    if cands:
        log('[二维码] 候选 %d 个：%s' % (
            len(cands),
            '; '.join('%s %dx%d@%d,%d%s%s'
                      % (c['tag'], c['w'], c['h'], c['x'], c['y'],
                         ' base64' if c.get('isData') else '',
                         ' 方' if c.get('square') else '')
                      for c in cands[:4])))
        c = cands[0]
        pad = PAD
        clip = {'x': max(0, c['x'] - pad), 'y': max(0, c['y'] - pad),
                'width': c['w'] + pad * 2, 'height': c['h'] + pad * 2}
        try:
            page.screenshot(path=QR_FILE, clip=clip)
            log('[二维码] 已按坐标裁切 %dx%d(css) -> %s（%d 倍缩放）'
                % (clip['width'], clip['height'], QR_FILE, DSF))
            _write_meta(c)
            return True
        except Exception as e:
            log('[二维码] 坐标裁切失败：%s' % str(e)[:110])
    try:
        page.screenshot(path=QR_FILE, full_page=False)
        log('[二维码] 未找到二维码候选，已截当前视口 -> %s' % QR_FILE)
        _write_meta(None)
        return True
    except Exception as e:
        log('[二维码] 截图失败：%s' % str(e)[:140])
        return False


def _first_douyin_sec():
    """取账号表里第一个抖音账号的 sec_user_id（用于登录态校验）"""
    try:
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return '', ''
    sys.path.insert(0, BASE_DIR)
    try:
        from douyin_video_scraper import extract_sec_user_id  # 复用同一套解析
    except Exception:
        return '', ''
    for a in data:
        if not isinstance(a, dict):
            continue
        if (a.get('platform') or 'douyin').strip().lower() != 'douyin':
            continue
        sec = extract_sec_user_id(a.get('homepage') or '')
        if sec:
            return sec, (a.get('name') or '').strip()
    return '', ''


def _verify(page):
    """用真实账号拉一次作品列表，确认登录态真的生效（而不是只有 cookie 名字）"""
    sec, name = _first_douyin_sec()
    if not sec:
        log('[校验] 跳过：没有可用的抖音测试账号')
        return None
    try:
        r = page.evaluate(VERIFY_JS, {'sec': sec, 'count': 18}) or {}
    except Exception as e:
        log('[校验] 执行失败：%s' % str(e)[:140])
        return None
    tip = r.get('tip')
    log('[校验] 账号「%s」: HTTP=%s status_code=%s 条数=%s has_more=%s 登录引导=%s'
        % (name, r.get('http'), r.get('code'), r.get('n'),
           r.get('hasMore'), '有' if tip else '无'))
    if tip:
        log('[校验] ❌ 仍被判为未登录（接口返回「看更多最新作品」引导）')
        return False
    log('[校验] ✅ 登录态生效（接口不再返回登录引导）')
    return True


def main():
    from playwright.sync_api import sync_playwright

    log('=' * 60)
    log('抖音扫码登录（服务器版）')
    log('Cookie 输出: %s' % COOKIE_FILE)
    log('二维码输出: %s' % QR_FILE)
    log('=' * 60)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, channel='chrome', headless=False, args=LAUNCH_ARGS,
            locale='zh-CN', timezone_id='Asia/Shanghai',
            viewport={'width': 1440, 'height': 900},
            device_scale_factor=DSF, user_agent=UA)
        ctx.set_default_timeout(60000)
        ctx.add_init_script(INIT_JS)
        page = ctx.new_page()

        log('[1/4] 打开抖音首页 ...')
        try:
            page.goto(HOME_URL, wait_until='domcontentloaded', timeout=60000)
        except Exception as e:
            log('[警告] 首页加载异常（继续尝试）：%s' % str(e)[:160])
        time.sleep(6)
        try:
            log('    标题=%s | URL=%s' % (page.title(), page.url))
        except Exception:
            pass

        # 已登录（profile 残留有效登录态）→ 直接导出
        sid = _pick(ctx, SESSION_COOKIE)
        cands = _find_login(page)
        log('    可见「登录」入口 %d 个 | sessionid: %s'
            % (len(cands), '已存在' if sid else '不存在'))
        if sid and not cands:
            s = _save_cookie(ctx)
            log('[结果] ✅ 检测到已登录，已导出 Cookie（%d 字节，%d 个字段）'
                % (len(s), len(ctx.cookies())))
            _verify(page)
            ctx.close()
            return 0

        log('[2/4] 未检测到登录态，点出登录框 ...')
        ok, why = _click_login(page)
        log('    %s' % why)
        if not ok:
            log('    请在浏览器窗口手动点右上角「登录」（脚本会继续等待）')
        time.sleep(6)

        # 多轮：抖音二维码约 3 分钟失效，到期自动换新码，省得用户反复喊「重新出」
        for rnd in range(1, MAX_ROUNDS + 1):
            log('')
            log('===== 第 %d/%d 轮：出二维码 =====' % (rnd, MAX_ROUNDS))
            if rnd > 1:
                _refresh_qr(page)
                time.sleep(6)
            log('[3/4] 截取二维码 ...')
            _shot_qr(page)
            lv = _pick(ctx, SESSION_COOKIE)
            log('    当前 sessionid: %s' % ('已出现' if lv else '尚未出现（正常，等待扫码）'))
            log('    请用手机「抖音 App」扫码，扫完在手机上点「确认登录」')
            log('    本张码 %d 秒内有效，超时自动换新' % WAIT_TIMEOUT)

            deadline = time.time() + WAIT_TIMEOUT
            last_beat = time.time()
            while time.time() < deadline:
                if _pick(ctx, SESSION_COOKIE):
                    time.sleep(3)                     # 二次确认，避免抓到中间态
                    if _pick(ctx, SESSION_COOKIE):
                        s = _save_cookie(ctx)
                        log('[结果] ✅ 扫码登录成功，已写入 Cookie（%d 字节，%d 个字段）'
                            % (len(s), len(ctx.cookies())))
                        _verify(page)
                        ctx.close()
                        return 0
                # 每 30 秒心跳一次，便于外部观察是否还在等
                if time.time() - last_beat > 30:
                    last_beat = time.time()
                    log('[心跳] 第 %d 轮剩余 %d 秒 ...（页面 %s）'
                        % (rnd, int(deadline - time.time()), page.url))
                time.sleep(POLL)

            log('[续期] 第 %d 轮二维码已到有效期上限，准备换新' % rnd)

        log('[结果] ❌ %d 轮二维码均未完成登录。可能原因：未及时扫/未在手机点确认/触发安全验证'
            % MAX_ROUNDS)
        _shot_qr(page)
        log('    已将当前页面截到 %s 供排查' % QR_FILE)
        ctx.close()
        return 2


if __name__ == '__main__':
    try:
        code = main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.exit(code)
