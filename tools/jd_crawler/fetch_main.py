# -*- coding: utf-8 -*-
"""京东抓取主程序 —— 商智商品明细(32指标) → 云库「京东单链接数据表」40 列；
                                   交易概况 → 云库「店铺营销数据」京东 16 字段。

══════════════════════════════════════════════════════════════════
2026-09-16 逆向结论（务必先读，否则一定踩坑）
══════════════════════════════════════════════════════════════════
① 旧版商智 sz.jd.com / ppzh.jd.com 已下线（26 年底关停，实测已跳新版）。
   新版 = https://jdsz.jd.com，入口跳转 /szweb/view/index/home.html 耗时 10~18s 不稳，
   必须 page.wait_for_url('**/szweb/**')，不能固定 sleep。

② ⚠️ szgateway 网关对「裸 fetch」一律返回 {code:-402,"desc":"不安全的请求"}，
   与登录态无关。必须带页面同款请求头：
       x-requested-with: XMLHttpRequest
       user-mnp : 32 位 hex（登录态签名，随会话变化）
       user-mup : 毫秒时间戳
       uuid     : uuid4
   做法：先打开商品明细页，用 page.on('request') 抓页面自己发的 productTable 请求的
   all_headers()，再拿这几个头去回放 fetch。实测 code=0。

③ ⚠️ 「商品明细」页的「实时」模式只有 20 个指标；点「昨天」(离线, dateType=day)
   才开放 32 个指标（含搜索类 3 + 商详停留 1 + 曝光 2 + 下单主题 6）。
   影刀当年用的就是离线模式（下载中心存证文件名带「离线_不包括对比时间_分天下载」）。
   ⇒ 本脚本不走 UI 导出，直接回放接口，用全 32 个指标编码取数。

④ 「昨天」按钮用 Playwright 普通 click 一定失败（resolve 到隐藏 <li> 超时）。
   本脚本不需要点它 —— 接口的 realtime:false + dateType:day 就是离线口径。

⑤ 响应 body.data[0] 是**汇总行**（$summary=true, spu_id="合计"），必须跳过。

⑥ 比率类指标接口返回**小数**（0.0807），而库表存的是**百分数**（8.0800）。
   ×100 的 5 个列：成交转化率 / 搜索点击率 / 加购转化率 / 下单转化率 / 下单成交转化率。
   （商品人均浏览量、商详平均停留时长、UV价值 等**不乘**。）

⑦ 字段口径已交叉验证：
   - `item_num` == 库表「货号」（32/32 SPU 实测完全一致）
   - `cate_1/2/3` 是类目ID：6728=汽车用品 / 6745=汽车装饰 / 11883=汽车脚垫（其余见 CATE_MAP）
   - 库表实际 40 列 = 建表 39 列 + 「店铺名」（线上 ALTER 加的；
     database/单链接数据表.sql 是旧版，别被它误导）

⑧ 「店铺营销数据」的京东行是**下单口径**（2026-09-16 用 08-28/29/30 三天真值逐项比对，
   100% 命中）：
       支付金额   ← 下单金额    净支付金额 ← 成交金额
       支付买家数 ← 下单客户数   支付转化率 ← 下单转化率
       退款金额   ← 退款金额    订单退款率 ← 退款单量 ÷ 下单单量
   推广三项历史恒为 0.00 / 0.00 / NULL（京东侧不填）。

⚠️ 必须 headless=False（或用 xvfb-run）；受管 Python 3.13 无 playwright，用系统 Python 3.12。
"""
import sys
import os
import time
import json
import uuid
import datetime
import subprocess

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, TOOLS_DIR)
sys.path.insert(0, BASE_DIR)

import shops  # noqa: E402
from crawler_runtime import RequestGovernor  # noqa: E402

GOVERNOR = RequestGovernor('jd')


# ── 抓取收尾：自动补齐商品品类映射（增量）────────────────────────────────────
# 以**子进程**方式调 tools/category_mapper/mapper.py，与抓取进程隔离：
#   · 映射失败/超时都不会影响抓取结果，也不污染抓取进程的 sys.path / 浏览器上下文
#   · 只查本平台（--platform），且映射表里已存在的 ID 一律不动 → 幂等，抓完跑一次安全
#   · 无新商品时秒退（不调 LLM、不写库）
# 关闭方式：命令行加 --no-map，或设环境变量 CATMAP_DISABLE=1
_CATMAP_CANDIDATES = (
    os.path.join(os.path.dirname(BASE_DIR), 'category_mapper'),   # 开发目录 tools/category_mapper
    os.path.join(BASE_DIR, 'category_mapper'),
    '/opt/ecom/tools/category_mapper',                            # 线上站点目录
    '/opt/pw/category_mapper',
)


def _find_mapper():
    p = os.environ.get('CATMAP_MAPPER', '')
    if p and os.path.isfile(p):
        return p
    for d in _CATMAP_CANDIDATES:
        f = os.path.join(d, 'mapper.py')
        if os.path.isfile(f):
            return f
    return ''


def auto_category_map(platform, timeout=900):
    """抓取结束后自动跑一次品类增量映射。永不抛异常。"""
    if os.environ.get('CATMAP_DISABLE') == '1':
        print('[品类] 已按 CATMAP_DISABLE=1 跳过自动映射')
        return False
    mapper = _find_mapper()
    if not mapper:
        print('[品类] ⚠️ 未找到 category_mapper/mapper.py，跳过自动映射'
              '（可用环境变量 CATMAP_MAPPER 指定完整路径）')
        return False

    print('\n[品类] ── 抓取完成，自动执行增量品类映射（平台=%s）──' % platform)
    t0 = time.time()
    env = dict(os.environ)
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    try:
        rc = subprocess.run([sys.executable, mapper, '--platform', platform],
                            cwd=os.path.dirname(mapper), env=env, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print('[品类] ⚠️ 自动映射超时(>%ds)，已放弃；可手动补跑：python "%s"' % (timeout, mapper))
        return False
    except Exception as e:
        print('[品类] ⚠️ 自动映射异常（不影响抓取）:', str(e)[:200])
        return False

    dt = time.time() - t0
    if rc == 0:
        print('[品类] ✅ 自动映射完成（%.1fs）' % dt)
        return True
    print('[品类] ⚠️ 自动映射退出码 %s（%.1fs），不影响本次抓取' % (rc, dt))
    return False

# ══════════════════════════ 常量 ══════════════════════════
JDSZ_HOME = 'https://jdsz.jd.com'
DETAIL_PAGE = 'https://jdsz.jd.com/szweb/view/product/productDetail.html'
TRADE_PAGE = 'https://jdsz.jd.com/szweb/view/tradeAnalysis/tradeSummary.html'

DETAIL_API = 'https://szgateway.jd.com/api/lowcode/productDetail/table/productTable.ajax'
TRADE_API = 'https://szgateway.jd.com/api/lowcode/tradeSummary/summary/getSummary.ajax'

STATE_PATH = os.path.join(BASE_DIR, '_states', 'jd_state.json')

# 京东账号表没有「品牌」列（11 列：id/账号/密码/店铺ID/店铺名/是否运营/备注/
# 创建时间/更新时间/登录状态/状态更新时间），而「店铺营销数据」要求「品牌」。
# 库里京东历史 61 行的「品牌」恒为「西西猫」，故用常量兜底。
BRAND_DEFAULT = '西西猫'

# 库表列名 → 接口指标编码（32 个）
IND = {
    # 成交主题
    '成交金额': 'jdr_sch_trade_deal_ord_ord_amt_sz_trade_deal_snapshot',
    '成交商品件数': 'jdr_sch_trade_deal_ord_sku_qtty_sz_trade_deal_snapshot',
    '成交单量': 'jdr_sch_trade_deal_ord_ord_qtty_sz_trade_deal_snapshot',
    '成交客户数': 'jdr_sch_user_deal_ord_user_cnt_sz_user_deal_snapshot',
    '成交转化率': 'fo_jdr_sch_industry_deal_rate',
    '客单价': 'fo_jdr_sch_trade_deal_ord_amt_user_sz_trade_deal_snapshot',
    '件单价': 'fo_jdr_sch_trade_deal_ord_amt_qtty_sz_trade_deal_snapshot',
    'UV价值': 'fo_jdr_sch_uv_value_sz',
    # 流量主题
    '商品浏览量': 'jdr_sch_traffic_brow_sku__page_qtty_traffic_plat_item_di_sz_bsg',
    '商品访客数': 'jdr_sch_traffic_brow_sku__page_cnt_traffic_plat_item_di_sz_bsg',
    '商品人均浏览量': 'fo_jdr_sch_fo_flow_item_detail_view_pv_per_uv',
    '商详平均停留时长': 'fo_jdr_sch_fo_fsd_flow_item_detail_view_avg_stay_duration_per',
    '商品曝光次数': 'jdr_sch_traffic_exposure_event_qtty_sz_exposure_base',
    '商品曝光人数': 'jdr_sch_traffic_exposure_event_dis_qtty_sz_exposure_base',
    '搜索曝光次数': 'jdr_sch_search_exposure_sku_piece_search_standard_sz',
    '搜索点击次数': 'jdr_sch_search_click_page_qtty_search_standard_sz',
    '搜索点击率': 'fo_jdr_sch_click_page_qtty_exposure_sku_piece_rate_search_standard_sz',
    # 加购主题
    '加购商品件数': 'jdr_sch_sku_add_cart_sku_sku_piece_shopping_cart',
    '加购客户数': 'jdr_sch_sku_add_cart_sku_user_qtty_product_user_cart_add_minus_sz_bsg_shoppingcart@increase',
    '加购商品件数（正向）': 'jdr_sch_sku_add_cart_sku_sku_piece_cart_add_sz_bsg',
    '加购商品件数（负向）': 'jdr_sch_sku_add_cart_sku_sku_piece_cart_minus_sz_bsg',
    '加购转化率': 'fo_jdr_sch_add_cart_user_uv_rate@increase',
    '加购金额': 'jdr_sch_sku_add_cart_sku_sku_amt_shopping_cart',
    # 下单主题（plc = place order）
    '下单金额': 'jdr_sch_trade_plc_ord_ord_amt_sz_trade_valid_snapshot',
    '下单商品件数': 'jdr_sch_trade_plc_ord_sku_qtty_sz_trade_valid_snapshot',
    '下单单量': 'jdr_sch_trade_plc_ord_ord_qtty_sz_trade_valid_snapshot',
    '下单客户数': 'jdr_sch_user_plc_ord_user_cnt_sz_user_valid_snapshot',
    '下单转化率': 'fo_jdr_sch_skuvisit_valid_rate',
    '下单成交转化率': 'fo_jdr_sch_deal_valid_rate_v2',
    # 售后主题
    '取消及售后退款金额': 'fo_jdr_sch_trade_cancel_refund_ord_amt',
    '取消及售后退款商品件数': 'fo_jdr_sch_trade_cancel_refund_sku_qtty',
    '取消及售后退款单量': 'fo_jdr_sch_trade_cancel_refund_ord_qtty',
}

IND_LIST = list(IND.values())

# 需要 ×100 的列（接口给小数，库表存百分数）
PCT_COLS = ('成交转化率', '搜索点击率', '加购转化率', '下单转化率', '下单成交转化率')

# 类目ID → 名称（6728/6745/11883 实测交叉验证；其余取自 getDims.ajax）
CATE_MAP = {
    '6728': '汽车用品', '6745': '汽车装饰', '11883': '汽车脚垫',
    '6743': '美容清洗', '13179': '特殊商品', '13180': '特殊商品', '28093': '特殊商品',
    '28094': '补差价链接', '38982': '车载布艺装饰', '39010': '座垫',
    '830': '手机配件', '866': '手机壳/保护套', '867': '手机贴膜',
}

# 交易概况指标（店铺营销数据用）
# ⚠️ 「店铺营销数据」的京东行整体是**下单口径**，不是成交口径（2026-09-16 用
#    08-28/29/30 三天真值逐项比对确认，100% 命中）：
#      支付金额   ← 下单金额      支付买家数 ← 下单客户数
#      支付转化率 ← 下单转化率     净支付金额 ← 成交金额
#      订单退款率 ← 退款单量 ÷ 下单单量
TRADE_IND = [
    # 支付金额 ← 下单金额
    'jdr_sch_trade_plc_ord_ord_amt_sz_trade_valid_snapshot',
    # 净支付金额 ← 成交金额
    'jdr_sch_trade_deal_ord_ord_amt_sz_trade_deal_snapshot',
    # 访客数 ← 店铺访客数
    'jdr_sch_traffic_enter_shop__browse_page_cnt_shop_last_src',
    # 支付买家数 ← 下单客户数
    'jdr_sch_user_plc_ord_user_cnt_sz_user_valid_snapshot',
    # 支付转化率 ← 下单转化率
    'fo_jdr_sch_skuvisit_valid_rate',
    # 客单价
    'fo_jdr_sch_trade_deal_ord_amt_user_sz_trade_deal_snapshot',
    # 退款金额 / 退款单量
    'fo_jdr_sch_trade_cancel_refund_ord_amt',
    'fo_jdr_sch_trade_cancel_refund_ord_qtty',
    # 订单退款率分母 ← 下单单量
    'jdr_sch_trade_plc_ord_ord_qtty_sz_trade_valid_snapshot',
]


# ══════════════════════════ 工具 ══════════════════════════
def L(msg):
    print(msg, flush=True)


def _f(v, default=0.0):
    if v is None or v == '':
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    return int(round(_f(v, default)))


def _yesterday():
    return (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')


def _prev(date_str):
    d = datetime.datetime.strptime(date_str, '%Y-%m-%d')
    return (d - datetime.timedelta(days=1)).strftime('%Y-%m-%d')


# ══════════════════════════ 浏览器 ══════════════════════════
GATEWAY_HEADERS_JS = """
async (arg) => {
  try {
    const r = await fetch(arg.url, {
      method: 'POST', credentials: 'include',
      headers: arg.h, body: arg.b
    });
    return r.status + '|' + (await r.text());
  } catch (e) { return 'ERR|' + String(e); }
}
"""

# 关掉商智的广告/引导弹层。⚠️ 实测：不关弹层，明细页的 productTable 会拖到 90s+
# 甚至一直不发（弹层挡住表格初始化）。这是取数慢的根因。
JS_KILL_POPUP = """
() => {
  const killed = [];
  document.querySelectorAll('div, section, aside').forEach(el => {
    const cls = String(el.className || '');
    if (/dialog|modal|mask|overlay|popup|pop-box|banner-wrap/i.test(cls)) {
      const r = el.getBoundingClientRect();
      if (r.width > 250 && r.height > 150) {
        try { el.style.display = 'none'; killed.push('hide:' + cls.slice(0, 50)); } catch(e) {}
      }
    }
  });
  return killed;
}
"""

# 点商智页面上的日期 tab（「昨天」/「今天」）——用于催页面重新发起查询
JS_TAB = """
(t) => {
  const els = [...document.querySelectorAll('li, span, a, div')];
  const hits = els.filter(el => (el.innerText || '').trim() === t);
  hits.sort((a, b) => a.children.length - b.children.length);
  if (!hits.length) return '';
  const el = hits[0];
  try { el.click(); return el.tagName + '|' + String(el.className || '').slice(0, 50); }
  catch (e) { return 'ERR:' + e.message; }
}
"""


def kill_popups(page):
    """清理商智广告/引导遮挡层，返回被隐藏的元素类名列表。"""
    try:
        return page.evaluate(JS_KILL_POPUP) or []
    except Exception:
        return []


def launch(headless=False):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        channel='chrome', headless=headless,
        args=['--no-first-run', '--no-default-browser-check', '--no-sandbox'])
    kw = {}
    if os.path.exists(STATE_PATH):
        kw['storage_state'] = STATE_PATH
    ctx = browser.new_context(locale='zh-CN', timezone_id='Asia/Shanghai',
                              viewport={'width': 1600, 'height': 950}, **kw)
    page = ctx.new_page()
    return pw, browser, ctx, page


def capture_headers_for(page, page_url, key, wait=300, label=''):
    """打开商智页，抓**目标接口自身**的请求头（用于绕过 -402/-407）。

    ⚠️ 三个坑（全部实测）：
    ① 必须分两步：先 goto 首页等 SPA 起来（jdsz.jd.com → /szweb/view/index/home.html
       要 10~18s），再 goto 目标页。
    ② `user-mnp` 是**按接口路径签名**的（每个请求的 mnp 都不一样）：
       拿 A 接口的头去调 B 接口会返回 `code=-407 不安全的请求`。必须精确匹配目标接口 URL。
    ③ 目标接口**可能很晚才发**：商智账号里若已勾选全部 32 个指标（本脚本/影刀的用法），
       页面自己的首次查询会慢到 ~90s 才发出 productTable。所以 wait 要给足（默认 300s）。
    """
    box = {}

    def on_req(r):
        if 'szgateway.jd.com' not in r.url or key not in r.url:
            return
        if r.method != 'POST':
            return
        try:
            h = r.all_headers()
        except Exception:
            return
        if not h.get('user-mnp'):
            return
        box.clear()
        box.update(h)

    tag = label or key
    page.on('request', on_req)
    try:
        # ① 先把 SPA 跑起来（未在 szweb 内才需要）
        if '/szweb/' not in page.url:
            page.goto(JDSZ_HOME, wait_until='domcontentloaded', timeout=60000)
            try:
                page.wait_for_url('**/szweb/**', timeout=60000)
            except Exception:
                pass
            time.sleep(5)
        # ② 再进目标页
        page.goto(page_url, wait_until='domcontentloaded', timeout=60000)
        nudged = False
        for i in range(wait):
            time.sleep(1)
            if box:
                L('    [头] %s 第 %ds 抓到（mnp=%s）' % (tag, i + 1, box.get('user-mnp', '')[:8]))
                break
            # 持续清理遮挡层（弹层是 productTable 不发的根因）
            if (i + 1) % 3 == 0:
                k = kill_popups(page)
                if k:
                    L('    [头] 清掉遮挡层 %d 个: %s' % (len(k), ', '.join(k[:3])))
            # 40s 还没动静 → 主动切「昨天」催页面重发查询
            if not nudged and (i + 1) >= 40:
                nudged = True
                try:
                    r = page.evaluate(JS_TAB, '昨天')
                except Exception as e:
                    r = 'ERR:' + str(e)[:50]
                try:
                    u, t = page.url[:70], page.title()[:24]
                except Exception:
                    u, t = '?', '?'
                L('    [头] 40s 未触发，主动切「昨天」催查询 -> %s' % (r or '未找到tab'))
                L('    [头] 当前 url=%s title=%s' % (u, t))
            if (i + 1) % 30 == 0:
                L('    [头] 等 %s 中… %ds' % (tag, i + 1))
    finally:
        try:
            page.remove_listener('request', on_req)
        except Exception:
            pass

    if not box.get('user-mnp'):
        raise RuntimeError(
            '未抓到 %s 的网关头（等满 %ds）。可能登录态失效或商智服务慢。\n'
            '请先跑: python login_probe.py 重新登录。' % (key, wait))

    keep = {k: v for k, v in box.items()
            if k.lower() in ('x-requested-with', 'user-mnp', 'user-mup', 'uuid',
                             'accept', 'content-type', 'accept-language')}
    keep.setdefault('x-requested-with', 'XMLHttpRequest')
    keep.setdefault('content-type', 'application/json')
    keep.setdefault('accept', 'application/json, text/plain, */*')
    keep.setdefault('uuid', str(uuid.uuid4()))
    keep.setdefault('user-mup', str(int(time.time() * 1000)))
    return keep


def replay_post(page, url, payload, headers, what, max_retry=2):
    """带网关头回放 POST，返回 body。"""
    body = json.dumps(payload, ensure_ascii=False)
    for attempt in range(max_retry):
        GOVERNOR.before()
        try:
            raw = page.evaluate(GATEWAY_HEADERS_JS, {'url': url, 'h': headers, 'b': body})
        except Exception as e:
            if attempt >= max_retry - 1:
                raise
            L('    [%s] 页面异常 %s，重试' % (what, str(e)[:60]))
            time.sleep(min(5 * (attempt + 1), 15))
            continue
        status, _, txt = raw.partition('|')
        try:
            GOVERNOR.after(int(status))
        except ValueError:
            GOVERNOR.after(0)
        if status != '200':
            raise RuntimeError('%s HTTP %s：%s' % (what, status, txt[:200]))
        d = json.loads(txt)
        code = (d.get('header') or {}).get('code')
        if code == 0:
            return d
        # -402/-407「不安全的请求」= 网关头失效/不匹配，交给调用方重建头后重试
        if code in (-402, -407):
            # 网关业务码可能仍是 HTTP 200；按受限响应进入短冷却，
            # 避免“刷新头后立即重放”形成请求尖峰。
            GOVERNOR.after(403)
            raise RuntimeError('NEED_REFRESH_HEADERS')
        raise RuntimeError('%s code=%s %s' % (what, code, (d.get('header') or {}).get('desc')))
    raise RuntimeError('%s 重试耗尽' % what)


# ══════════════════════════ 商品明细 ══════════════════════════
def fetch_detail(page, headers, date_str):
    """取单日全部 SPU × 32 指标。返回原始行 list（已剔除汇总行）。"""
    payload = {
        'proType': 'spu', 'realtime': False, 'interval': 'DAY', 'dateType': 'day',
        'startDate': date_str, 'endDate': date_str,
        'compareStartDate': _prev(date_str), 'compareEndDate': _prev(date_str),
        'compareType': 'hb', 'channel': 'all',
        'indicators': IND_LIST, 'onlyAttention': False,
    }
    d = replay_post(page, DETAIL_API, payload, headers, 'productTable')
    data = d['body']['data']
    rows = []
    for r in data:
        if r.get('$summary'):
            continue
        spu = str(r.get('spu_id') or '').strip()
        if not spu or spu == '合计':
            continue
        rows.append(r)
    return rows


def build_detail_rows(shop, date_str, raw_rows):
    """原始行 → 「京东单链接数据表」40 列 dict 列表。"""
    out = []
    for r in raw_rows:
        row = {
            '店铺名': shop['店铺名'],
            '时间': date_str,
            'SPU': str(r.get('spu_id')),
            'SPU名称': (r.get('name') or '')[:255],
            '一级类目': CATE_MAP.get(str(r.get('cate_1')), str(r.get('cate_1') or '')),
            '二级类目': CATE_MAP.get(str(r.get('cate_2')), str(r.get('cate_2') or '')),
            '三级类目': CATE_MAP.get(str(r.get('cate_3')), str(r.get('cate_3') or '')),
            '货号': str(r.get('item_num') or '').strip(),
        }
        for col, code in IND.items():
            v = _f(r.get(code))
            if col in PCT_COLS:
                v = round(v * 100.0, 4)
            elif col in ('商品人均浏览量', '商详平均停留时长'):
                v = round(v, 4)
            elif col.endswith('金额') or col in ('客单价', '件单价', 'UV价值'):
                v = round(v, 2)
            else:
                v = _i(v)
            row[col] = v
        out.append(row)
    return out


DETAIL_COLS = ['店铺名', '时间', 'SPU', 'SPU名称', '一级类目', '二级类目', '三级类目', '货号'] + list(IND.keys())


def save_detail(conn, rows):
    if not rows:
        return 0
    placeholders = ', '.join(['%s'] * len(DETAIL_COLS))
    quoted = ', '.join('`%s`' % c for c in DETAIL_COLS)
    updates = ', '.join('`%s`=VALUES(`%s`)' % (c, c) for c in DETAIL_COLS if c not in ('时间', 'SPU'))
    sql = ('INSERT INTO `京东单链接数据表` (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s, `时间`=`时间`'
           % (quoted, placeholders, updates))
    with conn.cursor() as cur:
        cur.executemany(sql, [[r[c] for c in DETAIL_COLS] for r in rows])
    return len(rows)


# ══════════════════════════ 店铺营销数据 ══════════════════════════
def fetch_shop_summary(page, headers, date_str):
    payload = {
        'realtime': False, 'interval': 'DAY', 'dateType': 'day',
        'startDate': date_str, 'endDate': date_str,
        'compareStartDate': _prev(date_str), 'compareEndDate': _prev(date_str),
        'compareType': 'hb', 'channel': 'all', 'indicators': TRADE_IND,
    }
    d = replay_post(page, TRADE_API, payload, headers, 'tradeSummary')
    data = d['body']['data']
    return data[0] if isinstance(data, list) and data else {}


def build_shop_row(shop, date_str, s):
    """「店铺营销数据」京东行。

    ⚠️ 京东行是**下单口径**（见 TRADE_IND 注释），三天真值比对确认。
    推广三项历史恒为 0.00 / 0.00 / NULL（京东侧不填），按历史口径显式写入。
    """
    plc_amt = _f(s.get('jdr_sch_trade_plc_ord_ord_amt_sz_trade_valid_snapshot'))   # 下单金额
    deal_amt = _f(s.get('jdr_sch_trade_deal_ord_ord_amt_sz_trade_deal_snapshot'))  # 成交金额
    refund_amt = _f(s.get('fo_jdr_sch_trade_cancel_refund_ord_amt'))
    refund_qtty = _f(s.get('fo_jdr_sch_trade_cancel_refund_ord_qtty'))
    plc_qtty = _f(s.get('jdr_sch_trade_plc_ord_ord_qtty_sz_trade_valid_snapshot'))
    return {
        '店铺ID': str(shop['店铺ID']),
        '平台': '京东',
        '店铺名': shop['店铺名'],
        '品牌': shop.get('品牌') or BRAND_DEFAULT,
        '日期': date_str,
        '支付金额': round(plc_amt, 2),                       # ← 下单金额
        '净支付金额': round(deal_amt, 2),                    # ← 成交金额
        '访客数': _i(s.get('jdr_sch_traffic_enter_shop__browse_page_cnt_shop_last_src')),
        '支付买家数': _i(s.get('jdr_sch_user_plc_ord_user_cnt_sz_user_valid_snapshot')),  # ← 下单客户数
        '支付转化率': round(_f(s.get('fo_jdr_sch_skuvisit_valid_rate')), 4),              # ← 下单转化率
        '退款金额': round(refund_amt, 2),
        '订单退款率': round(refund_qtty / plc_qtty, 4) if plc_qtty else 0.0,              # 退款单量÷下单单量
        '客单价': round(_f(s.get('fo_jdr_sch_trade_deal_ord_amt_user_sz_trade_deal_snapshot')), 2),
        '推广花费': 0.0,
        '推广总成交': 0.0,
        '推广净成交': None,
    }


def save_shop_row(conn, row):
    cols = list(row.keys())
    placeholders = ', '.join(['%s'] * len(cols))
    quoted = ', '.join('`%s`' % c for c in cols)
    updates = ', '.join('`%s`=VALUES(`%s`)' % (c, c) for c in cols)
    sql = ('INSERT INTO `店铺营销数据` (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s'
           % (quoted, placeholders, updates))
    with conn.cursor() as cur:
        cur.execute(sql, [row[c] for c in cols])
    return 1


# ══════════════════════════ 主流程 ══════════════════════════
def run(date_str, dry=False, headless=False, no_map=False):
    date_str = date_str or _yesterday()
    L('═══ 京东抓取 %s ═══' % date_str)

    shop_list = shops.get_active_shops()
    if not shop_list:
        L('没有运营中的京东店铺，退出')
        return False
    shop = shop_list[0]
    L('店铺: %s (ID %s)' % (shop['店铺名'], shop['店铺ID']))

    conn = None if dry else shops.get_conn()
    pw = browser = page = None
    ok = False
    try:
        pw, browser, ctx, page = launch(headless=headless)

        h_detail = capture_headers_for(page, DETAIL_PAGE, 'productTable')
        L('[头] 商品明细网关头就绪 (user-mnp=%s...)' % h_detail.get('user-mnp', '')[:8])

        # ── 商品明细 32 指标 ──
        try:
            raw = fetch_detail(page, h_detail, date_str)
        except RuntimeError as e:
            if 'NEED_REFRESH_HEADERS' not in str(e):
                raise
            L('[头] -402/-407，重建商品明细头后重试')
            h_detail = capture_headers_for(page, DETAIL_PAGE, 'productTable')
            raw = fetch_detail(page, h_detail, date_str)
        L('[明细] 接口返回 %d 个 SPU' % len(raw))
        rows = build_detail_rows(shop, date_str, raw)
        if rows:
            r0 = rows[0]
            L('[明细] 样例 SPU=%s | 货号=%s | 成交金额=%s | 搜索曝光=%s | 商详停留=%s | 下单金额=%s'
              % (r0['SPU'], r0['货号'], r0['成交金额'], r0['搜索曝光次数'],
                 r0['商详平均停留时长'], r0['下单金额']))
        else:
            L('[明细] ⚠️ 无数据')

        # ── 交易概况 ──
        try:
            h_trade = capture_headers_for(page, TRADE_PAGE, 'getSummary')
            try:
                s = fetch_shop_summary(page, h_trade, date_str)
            except RuntimeError as e:
                if 'NEED_REFRESH_HEADERS' not in str(e):
                    raise
                L('[头] -402/-407，重建交易概况头后重试')
                h_trade = capture_headers_for(page, TRADE_PAGE, 'getSummary')
                s = fetch_shop_summary(page, h_trade, date_str)
            shop_row = build_shop_row(shop, date_str, s)
            L('[店铺] 支付金额=%s 访客=%s 支付买家=%s 支付转化率=%s 客单价=%s'
              % (shop_row['支付金额'], shop_row['访客数'], shop_row['支付买家数'],
                 shop_row['支付转化率'], shop_row['客单价']))
        except Exception as e:
            L('[店铺] ⚠️ 失败: %s' % str(e)[:150])
            shop_row = None

        if dry:
            L('[dry] 不写库。明细 %d 行' % len(rows))
            if rows:
                L('[dry] 首行全字段: %s' % json.dumps(rows[0], ensure_ascii=False))
            return True

        n = save_detail(conn, rows)
        conn.commit()
        L('[库] 京东单链接数据表 upsert %d 行' % n)
        if shop_row:
            save_shop_row(conn, shop_row)
            conn.commit()
            L('[库] 店铺营销数据 upsert 1 行')
            ok = True
        else:
            # 没写进「店铺营销数据」= 对账口径上的失败，必须让退出码体现出来
            L('[库] ⚠️ 店铺营销数据未写入（交易概况取数失败）')
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            if pw:
                pw.stop()
        except Exception:
            pass

    # ★ 抓取收尾：自动补齐商品品类映射（增量；只查京东。--no-map / CATMAP_DISABLE=1 可关）
    #   注意：dry-run 在上面已 return，不会触发映射。
    if not no_map:
        auto_category_map('京东')
    return ok


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='统计日 YYYY-MM-DD，默认昨天')
    ap.add_argument('--dry', action='store_true', help='只取数不写库')
    ap.add_argument('--headless', action='store_true', help='无头（服务器请用 xvfb-run 包裹）')
    ap.add_argument('--no-map', action='store_true', help='抓完不自动跑品类增量映射')
    a = ap.parse_args()
    # 退出码：0=已落库，3=未落库（让 cron / fetch_reconcile 能感知失败）
    sys.exit(0 if run(a.date, dry=a.dry, headless=a.headless, no_map=a.no_map) else 3)
