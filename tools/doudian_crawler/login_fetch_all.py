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
from datetime import datetime

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

# ── 无人值守标记 ──────────────────────────────────────────────────────────
# 服务器上浏览器跑在 xvfb 虚拟屏（实测 Xvfb :99 640x480），**没有任何窗口显示在
# 谁的屏幕上** —— 所以「请在浏览器窗口人工拖拽滑块」那句提示在服务器环境下永远无效，
# 只换来一次 240s 的白等（batch + 多日期还会乘上去）。
# 命中该标记时遇到拼图验证立刻失败退出，用一条明确的人工恢复指引替代那 4 分钟。
# 由后端 /api/fetch/trigger（app.py _fetch_exec）与 cron 的 run_all.sh 注入。
UNATTENDED = os.environ.get('PW_UNATTENDED') == '1'

# 登录态时效上限（分钟）：state 实测约 40 分钟失效，取 25 分钟给「抓完 14 家店」留余量
STATE_MAX_AGE_MIN = 25


def state_age_minutes():
    """「抖店邮箱账号表.登录状态」距今多少分钟；读不到返回 None。"""
    try:
        acc = shops.get_email_account() or {}
        if not acc.get('state'):
            return -1                       # -1 = 压根没有 state
        st = acc.get('状态更新时间')
        if not st:
            return -1
        return int((datetime.now() - st).total_seconds() // 60)
    except Exception as e:
        print('  [warn] 登录态时效自检失败:', e)
        return None


CAPTCHA_JS = r"""
() => {
  // 真滑块弹窗 vs 常驻 verify-center 容器的区分点：
  //   常驻 iframe[src*="captcha"] 的**默认尺寸就是 300×150**，is_visible() 会认，
  //   但真弹窗容器远大于此。→ 要求 尺寸 ≥ 280×180 且 opacity ≥ 0.15。
  const big = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return null;
    if (parseFloat(st.opacity || '1') < 0.15) return null;
    if (r.width < 280 || r.height < 180) return null;
    return {w: Math.round(r.width), h: Math.round(r.height)};
  };
  const boxes = [];
  ['#captcha_container', 'iframe[src*="captcha"]', '[id*="captcha"]',
   'img.captcha-verify-image', '#vc_captcha_box', 'div.captcha-slider',
   'div[class*="captcha"]'].forEach(sel => {
    document.querySelectorAll(sel).forEach(el => { if (big(el)) boxes.push(sel); });
  });
  // 文案判据：字节弹窗的标题与操作提示都在**父页面**（不在 iframe 内）
  const t = (document.body && document.body.innerText) || '';
  const m = t.match(/(请完成下列验证[^\n]{0,10}|按住左边按钮[^\n]{0,12}|拖动[^\n]{0,6}(滑块|拼图)|(滑块|拼图)[^\n]{0,5}验证)/);
  return {boxes: boxes, text: m ? m[1] : ''};
}
"""

# 登录成功信号：进入「请选择店铺」弹层 / 工作台
LOGIN_OK_JS = r"""
() => {
  const t = (document.body && document.body.innerText) || '';
  if (/请选择店铺|选择店铺|抖店工作台|店铺管理/.test(t)) return true;
  return false;
}
"""


def has_captcha(page):
    """检测拼图验证弹窗是否**真的**出现（尺寸/透明度过滤 + 父页面文案，双判据）。

    两轮踩坑记录（别再退回任一旧写法）：
      · 第一版只有 img.captcha-verify-image / #vc_captcha_box / div.captcha-slider
        —— 字节 verify-center 一个都不匹配 → **漏检** → 撞「1105 滑动滑块」。
      · 第二版加一堆 [id*="captcha"] + is_visible() → **反向误判**：
        Playwright 的 is_visible() 不看 opacity、也不管 iframe 默认尺寸 300×150，
        于是「登录早就成功、页面都跳到『请选择店铺』」仍被判成有滑块 →
        白等 240s 超时 → 转判「登录验证失败」。
      · 实测（2026-09-17 10:39 本地登录器）：邮箱登录**全程无滑块**就通过了，
        所谓「出现拼图滑块」多半是误判。所以判据必须收得很紧。
    """
    try:
        d = page.evaluate(CAPTCHA_JS)
    except Exception:
        return False
    return bool(d.get('text')) or bool(d.get('boxes'))


def login_landed(page):
    """是否已登录成功（进入店铺选择 / 工作台）。SPA 里 URL 可能一直停在 /login，
    所以不能只看 URL；但 URL 一离开登录域即可判成功。"""
    try:
        if '/login' not in page.url and 'passport' not in page.url:
            return True
        return bool(page.evaluate(LOGIN_OK_JS))
    except Exception:
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
        # ★ 2026-09-17：已登录时访问 /login/common 会被重定向到工作台（**没有**邮箱表单），
        #   老逻辑在这里直接判「表单未出现 → 登录失败」，把好端端的登录态判死。
        cur = current_shop(page)
        if cur:
            print('  [登录] 页面已是登录态（当前店: %s），无需重新登录' % cur)
            return True
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
        # 无人值守环境（服务器 xvfb 虚拟屏）根本没有窗口可拖 → 别等，直接失败。
        # 2026-09-17 用户实况：线上点了「更新数据」只看到
        #   「⚠️ 出现拼图滑块！请在浏览器窗口人工拖拽（最多等 240s）  没有出现浏览器弹窗」
        # —— 因为这个窗口物理上就不存在于任何人的屏幕上（Xvfb :99 640x480）。
        if UNATTENDED:
            print('  [FAIL] 无人值守环境出现拼图滑块，虚拟屏上没有窗口可拖 —— 立即放弃')
            print('         抖店登录态已失效。请在本机恢复后重抓：')
            print('           1) python tools/doudian_crawler/login_save_state.py')
            print('           2) python tools/doudian_crawler/upload_state.py')
            return False
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


JS_KILL_HEADER_POPUP = """
() => {
  // ⚠️ 2026-09-17：右上角「最新直播」浮层卡（x≈1213-1493, y≈32-97）会盖住店名。
  // 原 find_corner 返回的是最靠右的命中节点(1440,20)，脚本点 (1450,33) —— 正好落在
  // 这张卡上 → 下拉永远弹不出来 → 表现为「下拉里没有『切换组织/店铺』」。
  // 本地能过、服务器不过，只差 1~2px 的布局/字体差异。
  const killed = [];
  const pats = ['layerCard', 'layerWrap', 'layerTitle', 'guideMask', 'guidePop',
                'activityCard', 'newGuide', 'popover'];
  document.querySelectorAll('body *').forEach(el => {
    const cls = (el.className || '').toString();
    if (!cls) return;
    if (!pats.some(p => cls.includes(p))) return;
    const r = el.getBoundingClientRect();
    if (r.width < 100 || r.height < 40) return;
    if (r.y > 200) return;          // 只清顶部区域，别误伤正文
    el.style.display = 'none';
    killed.push(cls.slice(0, 40));
  });
  return killed;
}
"""


# ============================================================================
# 右上角「店名入口」——★ 一律用语义 class 定位，不要用文字匹配
# ----------------------------------------------------------------------------
# 2026-09-17 服务器实测（xvfb + 系统 Chrome）：
#   · 真实节点：`div.headerShopName.index_headerShopName__2wP1V`
#     （子节点 `div.index_userName__16Isl`），文本是**完整店名**
#   · `page.locator(...).click()` / `.hover()` / `mouse.click(中心)` **全部弹不出下拉**
#     （中心点落进右上角「最新直播」浮层卡 x1213-1493,y32-97 的命中区，
#       真实指针事件被浮层吃掉；本地因 1~2px 布局差异侥幸避开 → 线上专有 bug）
#   · **JS 直接对元素派发完整指针事件序列 → 稳定弹出**（已实测 ✅）
# 所以：打开下拉一律走 JS 派发；Playwright 原生点击只在最后兜底。
# ============================================================================
JS_SHOP_ENTRY = """
() => {
  const el = document.querySelector('[class*="headerShopName"]')
          || document.querySelector('[class*="index_userName"]');
  if (!el) return null;
  const t = (el.innerText || el.textContent || '').trim();
  const r = el.getBoundingClientRect();
  if (!t || r.width <= 0) return null;
  return {text: t, x: Math.round(r.x), y: Math.round(r.y),
          w: Math.round(r.width), h: Math.round(r.height)};
}
"""

# 对「店铺名入口」派发完整指针事件序列（lvl=向上取几层祖先一起派发）
JS_OPEN_SHOP_MENU = """
(arg) => {
  const lvl = arg.lvl || 0;
  let el = document.querySelector('[class*="headerShopName"]')
        || document.querySelector('[class*="index_userName"]');
  if (!el) return {ok: false, why: 'no-el'};
  for (let i = 0; i < lvl && el.parentElement; i++) el = el.parentElement;
  const r = el.getBoundingClientRect();
  const cx = r.x + r.width / 2;
  const cy = r.y + Math.min(r.height / 2, 5);
  const mk = (t, C) => new C(t, {bubbles: true, cancelable: true, view: window,
                                 button: 0, clientX: cx, clientY: cy});
  ['pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'mousemove',
   'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(t => {
    try { el.dispatchEvent(mk(t, t.startsWith('pointer') ? PointerEvent : MouseEvent)); }
    catch (e) {}
  });
  return {ok: true, lvl: lvl, text: (el.innerText || '').trim().slice(0, 30),
          cls: (el.className || '').toString().slice(0, 50),
          rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]};
}
"""

# JS 点击「含指定文字的最内层可见元素」（下拉项、店铺选择页的店铺行）
JS_CLICK_TEXT = """
(kw) => {
  const cands = [];
  document.querySelectorAll('body *').forEach(el => {
    const t = (el.textContent || '').trim();
    if (!t.includes(kw) || t.length > 60 || el.children.length > 3) return;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) cands.push({el: el, area: r.width * r.height});
  });
  if (!cands.length) return {ok: false, why: 'no-candidate'};
  cands.sort((a, b) => a.area - b.area);
  const el = cands[0].el;
  const r = el.getBoundingClientRect();
  const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
  const mk = (t, C) => new C(t, {bubbles: true, cancelable: true, view: window,
                                 button: 0, clientX: cx, clientY: cy});
  [el, el.parentElement].forEach(node => {
    if (!node) return;
    ['pointerover', 'mouseover', 'pointerdown', 'mousedown',
     'pointerup', 'mouseup', 'click'].forEach(t => {
      try { node.dispatchEvent(mk(t, t.startsWith('pointer') ? PointerEvent : MouseEvent)); }
      catch (e) {}
    });
  });
  return {ok: true, text: (el.textContent || '').trim().slice(0, 24),
          rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]};
}
"""


def current_shop(page):
    """当前工作台右上角显示的店铺名（完整文本）。失败返回 None。"""
    try:
        info = page.evaluate(JS_SHOP_ENTRY)
    except Exception:
        info = None
    return (info or {}).get('text') or None


def find_corner(page, kw):
    """右上角店名入口。返回 {x,y,w,h}；kw 为空则直接返回入口本身。

    ★ 首选语义 class（`headerShopName`）——文字匹配在服务器上不可靠：
      真实节点文本是**完整店名**（如「御车宝周口驰为网络科技有限公司专卖店」），
      且宽度只有 96px（CSS 截断），按「前缀 + 长度<40」的文字扫描容易漏/错。
    """
    try:
        info = page.evaluate(JS_SHOP_ENTRY)
    except Exception:
        info = None
    if info:
        if not kw or kw in info['text']:
            return {'x': info['x'], 'y': info['y'], 'w': info['w'], 'h': info['h']}
        return None

    # ── 兜底：老式文字扫描（页面结构变化时仍有机会命中）──
    hits = page.evaluate("""
    (kw) => {
      const out = [];
      document.querySelectorAll('body *').forEach(el => {
        const t = (el.textContent || '').trim();
        if (t.includes(kw) && t.length < 40 && el.children.length <= 3) {
          const r = el.getBoundingClientRect();
          if (r.width > 0 && r.height > 0 && r.y < 90 && r.x > 900)
            out.push({x: r.x, y: r.y, w: r.width, h: r.height});
        }
      });
      return out;
    }
    """, kw)
    if not hits:
        return None
    hits.sort(key=lambda h: (h['w'] * h['h'], -h['x']))
    return hits[0]


JS_SW_DUMP = """
() => {
  const out = {vp: [window.innerWidth, window.innerHeight], dpr: window.devicePixelRatio,
               corners: [], hits: []};
  document.querySelectorAll('body *').forEach(el => {
    const t = (el.textContent || '').trim();
    if (!t || t.length > 40 || el.children.length > 3) return;
    const r = el.getBoundingClientRect();
    if (r.width <= 0) return;
    if (r.x > 1100 && r.y < 80)
      out.corners.push({t: t.slice(0, 20), x: Math.round(r.x), y: Math.round(r.y),
                        w: Math.round(r.width), h: Math.round(r.height)});
    if (t.includes('切换') || t.includes('退出') || t.includes('店铺信息'))
      out.hits.push({t: t.slice(0, 24), x: Math.round(r.x), y: Math.round(r.y),
                     w: Math.round(r.width), h: Math.round(r.height),
                     cls: (el.className || '').toString().slice(0, 40)});
  });
  return out;
}
"""


JS_STORE_OPEN = """
(arg) => {
  // ★ 2026-09-17：像素点击在服务器上打不开店铺下拉（点明明落在店名上，
  //   但 elementFromPoint 可能被透明浮层吃掉 → 事件根本没到店名元素）。
  //   改为「先做命中测试找出遮挡层并隐藏 → 再对店名元素本身派发完整指针事件序列」，
  //   彻底绕开像素命中与浮层遮挡；lvl 用于逐层向上试祖先节点。
  const lvl = arg.lvl || 0;
  // ★ 一律用语义 class 定位：文字匹配在服务器上不可靠
  //   （真实节点文本是**完整店名**「御车宝周口驰为网络科技有限公司专卖店」，
  //     宽度只有 96px 被 CSS 截断，按前缀扫描会漏）
  const node = document.querySelector('[class*="headerShopName"]')
            || document.querySelector('[class*="index_userName"]');
  if (!node) return {ok: false, why: 'no-el'};
  const c = {el: node, r: node.getBoundingClientRect()};
  const cx = c.r.x + c.r.width / 2;
  const cy = c.r.y + Math.min(c.r.height / 2, 5);

  // ── 命中测试：点击点上压着谁？──
  let top = document.elementFromPoint(cx, cy);
  const chain = [];
  let p = top, n = 0;
  while (p && n < 8) { chain.push(p); p = p.parentElement; n++; }
  // 「自己人」判据要双向：top 是店名后代 / top 是店名祖先 都算没被遮挡
  const selfOk = !!top && (top === c.el || c.el.contains(top) || top.contains(c.el)
                           || chain.indexOf(c.el) >= 0);
  let blocker = null;
  if (!selfOk && top) {
    const br = top.getBoundingClientRect();
    blocker = {tag: top.tagName, cls: (top.className || '').toString().slice(0, 70),
               x: Math.round(br.x), y: Math.round(br.y),
               w: Math.round(br.width), h: Math.round(br.height),
               pe: getComputedStyle(top).pointerEvents};
    top.style.display = 'none';
  }

  let target = c.el;
  for (let i = 0; i < lvl && target.parentElement; i++) target = target.parentElement;

  const mk = (type, Ctor) => new Ctor(type, {bubbles: true, cancelable: true,
                                             view: window, button: 0,
                                             clientX: cx, clientY: cy});
  ['pointerover', 'pointerenter', 'mouseover', 'mouseenter',
   'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(t => {
    try {
      target.dispatchEvent(mk(t, t.startsWith('pointer') ? PointerEvent : MouseEvent));
    } catch (e) {}
  });
  return {ok: true, lvl: lvl, selfOk: selfOk, blocker: blocker,
          cands: 1,
          target: {tag: target.tagName,
                   cls: (target.className || '').toString().slice(0, 60),
                   cur: getComputedStyle(target).cursor},
          el: {x: Math.round(c.r.x), y: Math.round(c.r.y),
               w: Math.round(c.r.width), h: Math.round(c.r.height)},
          point: [Math.round(cx), Math.round(cy)]};
}
"""


def _menu_open(page):
    """右上角店铺下拉是否已弹出（找「切换组织/店铺」）。"""
    try:
        sw = page.locator('text=切换组织/店铺')
        if sw.count() > 0 and sw.first.is_visible():
            return sw
    except Exception:
        pass
    return None


def switch_shop(page, target, cur_kw):
    """当前会话切到目标店铺。cur_kw=当前所在店铺名（由调用方跟踪）。"""
    # 切店必须在工作台页面操作（罗盘右上角下拉没有「切换组织/店铺」）
    if '/ffa/mshop' not in page.url:
        page.goto(WORKBENCH, wait_until='domcontentloaded', timeout=60000)
        time.sleep(10)

    # 先清掉顶部浮层卡（「最新直播」等），否则点击会落到浮层上、下拉打不开
    try:
        killed = page.evaluate(JS_KILL_HEADER_POPUP)
        if killed:
            print('    [overlay] 已隐藏顶部浮层 %d 个: %s' % (len(killed), killed[:3]))
    except Exception:
        pass

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

    corner = find_corner(page, '')      # '' → 直接返回入口本身（走 class 定位）
    if not corner:
        print('    [FAIL] 右上角找不到店名入口（headerShopName）—— 页面结构变了？')
        return False
    print('    [入口] 店名元素 %s（当前店: %s）'
          % (json.dumps(corner, ensure_ascii=False), current_shop(page) or '<读不到>'))

    # ── 打开店铺下拉：JS 派发事件为主（不吃遮挡），原生点击/hover 兜底 ──
    # 试序：(JS, lvl0) (原生, lvl0) (JS, lvl1) (JS, lvl2) (hover)
    def _toast_blur():
        try:
            page.keyboard.press('Escape')
            page.mouse.click(600, 500)
            time.sleep(1.2)
        except Exception:
            pass

    opened = None
    blocker_reported = False
    info = None
    plan = [('js', 0), ('mouse', 0), ('js', 1), ('js', 2), ('hover', 0)]
    for step, (mode, lvl) in enumerate(plan):
        if step:
            _toast_blur()
        try:
            info = page.evaluate(JS_STORE_OPEN, {'lvl': lvl})
        except Exception as e:
            print('      [dbg] JS 定位失败:', e)
            info = None
        if info is not None and not info.get('ok'):
            print('    [FAIL] 右上角找不到店名入口（why=%s）' % info.get('why'))
            return False
        if info and info.get('blocker') and not blocker_reported:
            blocker_reported = True
            print('      [blocker] 点击点 (x=%d,y=%d) 上压着 %s → 已隐藏'
                  % (info['point'][0], info['point'][1],
                     json.dumps(info['blocker'], ensure_ascii=False)))
        if mode == 'js':
            print('      [js] lvl=%d 对 %s 派发指针事件'
                  % (lvl, json.dumps((info or {}).get('target'), ensure_ascii=False)))
            time.sleep(2.5)
        elif mode == 'mouse':
            pt = (info or {}).get('point')
            if not pt:
                continue
            print('      [mouse] 原生点击 (%d,%d)' % (pt[0], pt[1]))
            page.mouse.click(pt[0], pt[1])
            time.sleep(2.5)
        else:
            pt = (info or {}).get('point')
            if not pt:
                continue
            print('      [hover] (%d,%d)' % (pt[0], pt[1]))
            page.mouse.move(pt[0], pt[1])
            time.sleep(1)
            page.mouse.move(pt[0] + 30, pt[1])
            time.sleep(2)
        opened = _menu_open(page)
        if opened:
            print('      ✅ 下拉已弹出（%s lvl=%d）' % (mode, lvl))
            break
        page.screenshot(path=os.path.join(BASE_DIR, '_sw_try_%d.png' % step))

    if not opened:
        print('    [FAIL] 下拉里没有「切换组织/店铺」（截图 _sw_try_*.png）')
        # ── 失败现场取证：区分「点不开下拉」还是「下拉里没这项」──
        try:
            d = page.evaluate(JS_SW_DUMP)
            print('      视口=%s dpr=%s' % (d.get('vp'), d.get('dpr')))
            print('      右上角候选(x>1100,y<80): %s'
                  % json.dumps(d.get('corners', [])[:8], ensure_ascii=False))
            print('      含「切换/退出/店铺信息」的可见节点: %s'
                  % json.dumps(d.get('hits', [])[:8], ensure_ascii=False))
            page.screenshot(path=os.path.join(BASE_DIR, '_sw_fail_full.png'),
                            full_page=False)
        except Exception as e:
            print('      现场取证失败:', e)
        return False
    # 点「切换组织/店铺」：JS 派发优先（服务器实测真实鼠标点击常被浮层吃掉），原生兜底
    try:
        r = page.evaluate(JS_CLICK_TEXT, '切换组织/店铺')
        print('      [js] 点「切换组织/店铺」→ %s'
              % json.dumps(r, ensure_ascii=False)[:110])
    except Exception as e:
        print('      [warn] JS 点「切换组织/店铺」异常:', e)
    time.sleep(4)
    if page.locator('text=请选择店铺').count() == 0 and _menu_open(page):
        try:
            opened.first.click(timeout=8000)
            time.sleep(4)
        except Exception as e:
            print('      [warn] 原生点「切换组织/店铺」失败: %s' % str(e)[:80])

    deadline = time.time() + 30
    while time.time() < deadline and page.locator('text=请选择店铺').count() == 0:
        time.sleep(2)

    # 点目标店铺行：同样 JS 派发优先
    clicked = False
    try:
        r = page.evaluate(JS_CLICK_TEXT, target)
        if r.get('ok'):
            print('      [js] 已点店铺行: %s' % r.get('text'))
            clicked = True
        else:
            print('      [warn] JS 未匹配到店铺行（%s），改用原生点击' % r.get('why'))
    except Exception as e:
        print('      [warn] JS 点店铺行异常:', e)
    if not clicked:
        item = page.locator('text=%s' % target)
        if item.count() == 0:
            print('    [FAIL] 店铺选择页未找到: %s' % target)
            return False
        item.first.click()
    time.sleep(8)

    # 验证：读右上角真实店名（class 定位）
    deadline = time.time() + 25
    while time.time() < deadline:
        cur = current_shop(page)
        if cur and target[:6] in cur:
            print('    ✅ 已切换到 %s' % cur)
            return True
        time.sleep(2)
    print('    [warn] 未确认切换到目标店（当前读到: %s）' % (current_shop(page) or '<读不到>'))
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
            return 2
    print('目标日期: %s | 抓取店铺 %d 家（运营中 %d 家）' % (date_str, len(all_shops), len(all_active)))

    # ★ 登录态时效自检
    #   过期的后果：state 免登录失败 → 走邮箱登录 → 撞拼图滑块 → 干等 240s → 退出。
    #   一条命令白烧 4~5 分钟，batch(4家/条) × N 天还会把这笔账乘上去。
    #   服务器（UNATTENDED）没人能过滑块 → 硬拦；本机有头模式还能人工拖 → 只提醒。
    age = state_age_minutes()
    if age is not None:
        bad = (age < 0) or (age > STATE_MAX_AGE_MIN)
        if age < 0:
            print('  [warn] 抖店邮箱账号表没有可用登录态')
        elif bad:
            print('  [warn] 抖店登录态已过期 %d 分钟（实测寿命约 40 分钟）' % age)
        else:
            print('[登录] 登录态时效自检通过（%d 分钟前刷新）' % age)
        if bad:
            if UNATTENDED:
                print('[FAIL] 服务器上无法自助登录（虚拟屏没人能过拼图滑块），终止')
                print('       请在本机运行 tools/doudian_crawler/login_save_state.py')
                print('       再运行 tools/doudian_crawler/upload_state.py 上传登录态')
                return 2
            print('       本机有头模式，继续尝试邮箱登录（可人工过滑块）')

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

        # 只有**确认已登录**才允许写回 state。
        # 2026-09-17 踩坑：上一版无脑写回，结果「登录失败」那次也把
        # 停在登录页的未认证会话覆盖回库 → 把好凭证一起毁掉，形成
        # 「state 越跑越死」的自我循环。失败时保留原凭证才是对的。
        state_ok = {'v': False}

        def _save_state(tag=''):
            """★ 会话 state 写回（仅在确认登录成功后）。

            抖店登录态很短效（实测 10:05 刷新 → 10:11 可用 → 10:44 已失效）。
            但「忘记写回」与「写回垃圾」都会让 state 永久死亡：
              · 失败路径不写回 → 丢掉了本次重新签发的 cookie（旧 bug）
              · 失败路径乱写回 → 未认证会话覆盖好凭证（新 bug，本函数修的就是它）
            """
            if not state_ok['v']:
                print('[!] 本次未确认登录态，跳过 state 写回（保护原凭证）%s' % tag)
                return
            try:
                acc = shops.get_email_account()
                shops.save_email_state(acc['邮箱'], ctx.storage_state())
                print('[+] 会话 state 已写回「抖店邮箱账号表」%s' % tag)
            except Exception as e:
                print('  [warn] 写回 state 失败:', e)

        summary = []
        try:
            page.goto(WORKBENCH, wait_until='domcontentloaded', timeout=60000)
            time.sleep(12)
            body = page.inner_text('body')
            logged = ('/login' not in page.url and '扫码登录' not in body
                      and '发送验证码' not in body)
            # ★ 登录态判据不再依赖「文字匹配找到店名」：
            #   2026-09-17 踩坑 —— 老逻辑用 find_corner(店名前缀) 扫描，
            #   服务器上扫不到（真实节点文本是**完整店名**、宽度被 CSS 截断）
            #   → 明明登录有效却被判成「state 失效/脏」→ 转去邮箱登录 → 撞滑块 → 全军覆没。
            #   现在改为「URL 不在登录页 + 无登录表单」为主，读店名只作辅助信息。
            logged_shop = current_shop(page)
            if logged and not logged_shop:
                for _ in range(3):
                    time.sleep(4)
                    logged_shop = current_shop(page)
                    if logged_shop:
                        break
            if logged:
                print('[登录] state 有效，免登录（当前在: %s）' % (logged_shop or '<读不到>'))
            else:
                print('[登录] state 失效/脏，走邮箱登录（需要人工过滑块）...')
                if not do_login(page, all_shops[0]):
                    print('[FAIL] 登录失败，退出')
                    return 2
                logged_shop = all_shops[0]
            state_ok['v'] = True        # 已确认登录 → 允许写回 state

            # 依次抓取（cur_kw 跟踪当前所在店铺）
            cur_kw = logged_shop
            if not switch_shop(page, all_shops[0], cur_kw):
                print('[FAIL] 切到首店失败，退出')
                return 2
            cur_kw = all_shops[0]
            conn = shops.get_conn()
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
                    product_error = None
                    try:
                        n = fetch_daily.save_rows(conn, shop, date_str, rows)
                        conn.commit()
                    except Exception as e:
                        # 商品明细表历史主键只有 (统计周期, 商品编码)，不同店铺
                        # 共享商品编码时会冲突。回滚明细事务，继续写店铺营销主表，
                        # 避免一处明细冲突把整家店的日汇总也丢掉。
                        conn.rollback()
                        n = 0
                        product_error = str(e)[:180]
                        print('  [warn] 商品明细落库冲突，保留旧明细并继续写主表:', product_error)
                    # 主表 16 字段（店铺营销数据）：core_index_v3 + income_expense + flow_overview
                    try:
                        fetch_main.fetch_one(page, conn, shop, date_str)
                        conn.commit()
                        summary.append((shop_name, ('OK %d 行 + 主表' % n)
                                        if not product_error else
                                        '主表已写，商品明细冲突: %s' % product_error))
                    except Exception as e:
                        conn.rollback()
                        summary.append((shop_name, 'OK %d 行, 主表失败: %s' % (n, str(e)[:50])))
            finally:
                conn.close()
        finally:
            _save_state('（本次结束）')
            try:
                browser.close()
            except Exception:
                pass

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

    # 退出码：0=全部完整落库，3=有店铺未落库（让 cron / fetch_reconcile 感知失败）
    return 3 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
