# -*- coding: utf-8 -*-
"""千牛取数生产版 —— 四维度取数 + 落库（云库「数据」）。

维度：
  ① 店铺日汇总   coreIndex 19 指标              → 「店铺营销数据」
  ② 推广总成交   万相台无界版 report/query.json → 「店铺营销数据.推广总成交」
  ③ 单链接       商品排行报表 top.json（分页）   → 「千牛单链接数据表」
  ④ 单链接推广   newproduct.json（分页）        → 「千牛单链接推广数据表」

用法：
  python fetch_daily.py <账号|all> [YYYY-MM-DD]   # 日期缺省=昨天
  （服务器上用 xvfb-run 包裹；本地直接跑，会弹 Chrome 窗口）

关键约束（实测踩坑）：
- 必须 headless=False（有头）。无头 coreIndex 触发阿里数据安全风控（bixi deny）。
- 万相台用同一 storage_state 直接 SSO，免登录；effectEqual=1（1天累计归因）与影刀历史口径一致。
- 缺字段/非数字一律填 0；单链接 507 条、推广 446 条需分页抓全（pageSize=100）。
"""
import sys
import os
import json
import time
import datetime
import urllib.parse
import subprocess

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import shops  # noqa: E402
import pymysql  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


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
    '/opt/pw/category_mapper',                                    # 服务器上抓取脚本所在目录
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

SYCM = 'https://sycm.taobao.com'
WULIANG = 'https://one.alimama.com/index.html'
WULIANG_QUERY = 'https://one.alimama.com/report/query.json'
PLATFORM = '千牛'

# ---------- 工具：数值归一 ----------
def _raw(v):
    """{'value': x} → x；其余原样。兼容接口返回 dict 包装 / 直接标量两种结构。"""
    if isinstance(v, dict) and 'value' in v:
        return v['value']
    return v


def _f(v, default=0.0):
    """转 float。None / 空对象 {} / 非数字 → default。"""
    v = _raw(v)
    if v is None or v == '':
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    return int(round(_f(v, default)))


def _s(v, default=''):
    v = _raw(v)
    if v is None:
        return default
    return str(v)


def _pct(v):
    """小数 → 百分比字符串 'xx.xx%'（用于单链接数据表 varchar 比率列）。"""
    return '%.2f%%' % (_f(v, 0.0) * 100)


# ---------- 维度① 店铺日汇总（coreIndex） ----------
def fetch_coreindex(page, date_str):
    dr = '%s|%s' % (date_str, date_str)
    url = SYCM + '/portal/coreIndex/new/overview/v3.json?needCycleCrc=true&dateType=day&dateRange=' + dr
    resp = page.request.get(url, headers={'referer': SYCM + '/portal/home.htm'})
    body = resp.json()
    selfobj = (body.get('content') or {}).get('data', {}).get('self') or {}
    if not selfobj:
        raise RuntimeError('coreIndex.self 为空（可能无权限/无数据/被风控）')

    def g(k):
        return _f(selfobj.get(k), 0.0)

    prom_spend = (g('p4pExpendAmt') + g('cubeAmt') + g('adStrategyAmt') +
                  g('admCostFamtQzt') + g('tkExpendAmt'))
    return {
        '支付金额': g('payAmt'),
        '净支付金额': g('netPaymentAmount'),
        '访客数': _i(selfobj.get('uv')),
        '支付买家数': _i(selfobj.get('payByrCnt')),
        '支付转化率': g('payRate'),
        '退款金额': g('rfdSucAmt'),
        '订单退款率': g('ordRfdRate'),
        '客单价': g('payPct'),
        '推广花费': prom_spend,
    }


# ---------- 维度② 推广总成交（万相台无界版） ----------
def fetch_wuliang_total(page, date_str):
    holder = {'csrf': None, 'lp': None}

    def on_req(req):
        if 'alimama.com' in req.url:
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(req.url).query)
                if not holder['csrf'] and q.get('csrfId'):
                    holder['csrf'] = q['csrfId'][0]
                if not holder['lp'] and q.get('loginPointId'):
                    holder['lp'] = q['loginPointId'][0]
            except Exception:
                pass

    page.on('request', on_req)
    page.goto(WULIANG, wait_until='domcontentloaded', timeout=60000)
    deadline = time.time() + 60
    while time.time() < deadline and not holder['csrf']:
        time.sleep(3)
        try:
            page.reload(wait_until='domcontentloaded', timeout=30000)
        except Exception:
            pass
    csrf = holder['csrf'] or 'dacef9a65de18ece9c6bad9fba109c62_1_1_1'  # 会话稳定值兜底
    lp = holder['lp'] or ''

    payload = {
        "bizCode": "universalBP", "fromRealTime": False, "source": "baseReport",
        "endTime": date_str, "unifyType": "zhai", "effectEqual": 1,
        "startTime": date_str, "splitType": "day",
        "queryFieldIn": ["alipayInshopAmt", "charge", "click"],
        "queryDomains": ["account"], "rptType": "account",
        "csrfId": csrf, "loginPointId": lp,
    }
    url = WULIANG_QUERY + '?csrfId=%s&bizCode=universalBP&loginPointId=%s' % (csrf, lp)
    resp = page.request.post(url, data=json.dumps(payload), fail_on_status_code=False,
                             headers={'content-type': 'application/json;charset=UTF-8',
                                      'referer': WULIANG, 'origin': 'https://one.alimama.com'})
    b = resp.json()
    lst = (b.get('data') or {}).get('list') or []
    return _f(lst[0].get('alipayInshopAmt'), 0.0) if lst else 0.0


# ---------- 维度③④ 分页取数 ----------
def fetch_paged(page, url_template, date_str, referer, order_by, extra=''):
    """分页抓全接口列表。返回 list[dict]。"""
    dr = '%s%%7C%s' % (date_str, date_str)
    all_rows = []
    page_no = 1
    max_pages = 60
    while page_no <= max_pages:
        url = url_template.format(dr=dr, page=page_no, order_by=order_by, extra=extra)
        resp = page.request.get(url, headers={'referer': referer})
        body = resp.json()
        if body.get('code') != 0:
            break
        d = body.get('data') or {}
        items = d.get('data') or []
        all_rows.extend(items)
        total = d.get('recordCount') or 0
        if not items or len(all_rows) >= total:
            break
        page_no += 1
    return all_rows


def fetch_link_items(page, date_str):
    """单链接（商品排行报表视图，dateType=day）。"""
    tpl = (SYCM + '/cc/item/view/top.json?dateRange={dr}&dateType=day'
           '&pageSize=100&page={page}&order=desc&orderBy={order_by}')
    return fetch_paged(page, tpl, date_str, SYCM + '/cc/item_rank', 'payAmt')


def fetch_promo_items(page, date_str):
    """单链接推广（商品排行页「推广商品」tab）。"""
    tpl = (SYCM + '/cc/item/view/dmp/newproduct.json?dateRange={dr}&dateType=day'
           '&pageSize=100&page={page}&order=desc&orderBy=fCharge&itemId=&follow=false')
    return fetch_paged(page, tpl, date_str, SYCM + '/cc/item_rank', 'fCharge')


# ---------- 落库 ----------
def _upsert(conn, table, cols, rows):
    """按列名列表批量 upsert（INSERT ... ON DUPLICATE KEY UPDATE）。"""
    if not rows:
        return 0
    placeholders = ', '.join(['%s'] * len(cols))
    quoted = ', '.join('`%s`' % c for c in cols)
    updates = ', '.join('`%s`=VALUES(`%s`)' % (c, c) for c in cols)
    sql = ('INSERT INTO `%s` (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s'
           % (table, quoted, placeholders, updates))
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def save_marketing(conn, shop, date_str, m, ad_total):
    cols = ['店铺ID', '平台', '店铺名', '品牌', '日期', '支付金额', '净支付金额', '访客数',
            '支付买家数', '支付转化率', '退款金额', '订单退款率', '客单价', '推广花费', '推广总成交']
    row = [shop['店铺ID'], PLATFORM, shop['账号'], shop['品牌'], date_str,
           m['支付金额'], m['净支付金额'], m['访客数'], m['支付买家数'],
           m['支付转化率'], m['退款金额'], m['订单退款率'], m['客单价'],
           m['推广花费'], ad_total]
    return _upsert(conn, '店铺营销数据', cols, [row])


def _goods_type(r):
    is_sub = _raw(r.get('isSubItem'))
    return '子商品' if str(is_sub) in ('1', 'True', 'true') else '主商品'


def save_link_items(conn, shop, date_str, rows):
    cols = ['店铺名', '统计日期', '商品ID', '商品名称', '主商品ID', '商品类型', '货号',
            '商品状态', '商品标签', '商品访客数', '商品浏览量', '平均停留时长',
            '商品详情页跳出率', '商品收藏人数', '商品加购件数', '商品加购人数',
            '下单买家数', '下单件数', '下单金额', '下单转化率', '支付买家数', '支付件数',
            '支付金额', '商品支付转化率', '支付新买家数', '支付老买家数', '老买家支付金额',
            '聚划算支付金额', '访客平均价值', '成功退款金额', '竞争力评分',
            '年累计支付金额', '月累计支付金额', '月累计支付件数', '搜索引导支付转化率',
            '搜索引导访客数', '搜索引导支付买家数', '结构化详情引导转化率', '结构化详情引导成交占比']
    out = []
    for r in rows:
        item = r.get('item') or {}
        out.append([
            shop['账号'], date_str, _s(r.get('itemId')), _s(item.get('title')),
            _s(r.get('mainProductId')), _goods_type(r), '0', _s(r.get('itemStatus')), '0',
            _f(r.get('itmUv')), _f(r.get('itmPv')), _f(r.get('stayTimeAvg')),
            _pct(r.get('itmBounceRate')), _i(r.get('itemCltByrCnt')),
            _i(r.get('itemCartCnt')), _i(r.get('itemCartByrCnt')),
            _i(r.get('crtByrCnt')), _i(r.get('crtItmQty')), _f(r.get('crtAmt')),
            _pct(r.get('crtRate')), _i(r.get('payByrCnt')), _i(r.get('payItmCnt')),
            _f(r.get('payAmt')), _pct(r.get('payRate')), _i(r.get('newPayByrCnt')),
            _i(r.get('payOldByrCnt')), _f(r.get('olderPayAmt')), _f(r.get('juPayAmt')),
            _f(r.get('uvAvgValue')), _f(r.get('sucRefundAmt')), 0,
            _f(r.get('ytdPayAmt')), _f(r.get('mtdPayAmt')), _f(r.get('mtdPayItmCnt')),
            _pct(r.get('seGuidePayRate')), _i(r.get('seGuideUv')), _i(r.get('seGuidePayByrCnt')),
            0.0, 0.0,
        ])
    return _upsert(conn, '千牛单链接数据表', cols, out)


def save_promo_items(conn, shop, date_str, rows):
    cols = ['店铺名', '统计日期', '商品ID', '商品名称', '推广消耗', '直接引导成交金额',
            '推广直接ROI', '展现量', '点击量', '点击率', '单次点击成本', '直接成交笔数',
            '总引导成交金额', '总ROI', '总引导成交笔数']
    out = []
    for r in rows:
        item = r.get('item') or {}
        out.append([
            shop['账号'], date_str, _s(r.get('itemId')), _s(item.get('title')),
            _f(r.get('fCharge')), _f(r.get('alipayDirAmt')), _f(r.get('pDROI')),
            _i(r.get('fImpression')), _i(r.get('click')),
            round(_f(r.get('fClickRate')) * 100, 4),  # 点击率存百分比数值
            _f(r.get('cpc')), _i(r.get('dOrdCnt')),
            _f(r.get('tPAmt')), _f(r.get('tROI')), _i(r.get('tOrdCnt')),
        ])
    return _upsert(conn, '千牛单链接推广数据表', cols, out)


# ---------- 单店取数 ----------
def fetch_one(account, date_str):
    shop = shops.get_shop(account)
    if not shop:
        print('[FAIL] 账号不存在或已停用:', account)
        return None
    if not shop.get('state'):
        print('[FAIL] 无登录态，请先跑 login_save_state.py:', account)
        return None

    result = {'账号': account, '日期': date_str}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel='chrome', headless=False,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox',
                  '--no-first-run', '--no-default-browser-check'],
        )
        ctx = browser.new_context(storage_state=shop['state'], locale='zh-CN',
                                  timezone_id='Asia/Shanghai',
                                  viewport={'width': 1600, 'height': 950})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()

        # 先访问生意参谋首页让会话生效
        try:
            page.goto(SYCM + '/portal/home.htm', wait_until='domcontentloaded', timeout=40000)
            time.sleep(2)
        except Exception as e:
            print('  [warn] 生意参谋首页:', e)

        # ① 店铺日汇总
        try:
            m = fetch_coreindex(page, date_str)
            result['营销'] = m
            print('  ①店铺日汇总: 净支付=%s 推广花费=%s' % (m['净支付金额'], m['推广花费']))
        except Exception as e:
            m = None
            result['营销'] = None
            print('  ①店铺日汇总失败:', e)

        # ② 推广总成交（万相台）
        ad_total = 0.0
        try:
            ad_total = fetch_wuliang_total(page, date_str)
            result['推广总成交'] = ad_total
            print('  ②推广总成交(万相台): %s' % ad_total)
        except Exception as e:
            result['推广总成交'] = None
            print('  ②推广总成交失败:', e)

        # ③ 单链接
        link_rows = []
        try:
            link_rows = fetch_link_items(page, date_str)
            result['单链接数'] = len(link_rows)
            print('  ③单链接: %d 条' % len(link_rows))
        except Exception as e:
            result['单链接数'] = 0
            print('  ③单链接失败:', e)

        # ④ 单链接推广
        promo_rows = []
        try:
            promo_rows = fetch_promo_items(page, date_str)
            result['推广数'] = len(promo_rows)
            print('  ④单链接推广: %d 条' % len(promo_rows))
        except Exception as e:
            result['推广数'] = 0
            print('  ④单链接推广失败:', e)

        browser.close()

    # ---- 落库 ----
    conn = shops.get_conn()
    try:
        if m is not None:
            n1 = save_marketing(conn, shop, date_str, m, ad_total)
            print('  [库] 店铺营销数据 upsert %d 行' % n1)
        n3 = save_link_items(conn, shop, date_str, link_rows)
        print('  [库] 千牛单链接数据表 upsert %d 行' % n3)
        n4 = save_promo_items(conn, shop, date_str, promo_rows)
        print('  [库] 千牛单链接推广数据表 upsert %d 行' % n4)
        conn.commit()
    finally:
        conn.close()

    return result


def main():
    raw = [a for a in sys.argv[1:]]
    no_map = '--no-map' in raw
    args = [a for a in raw if not a.startswith('--')]
    if not args:
        print(__doc__)
        print('可选参数: --no-map  抓完不自动跑品类增量映射')
        return
    target = args[0]
    date_str = args[1] if len(args) > 1 else (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    results = []
    if target == 'all':
        for s in shops.get_active_shops():
            print('\n======== 取数：%s (%s) ========' % (s['账号'], date_str))
            try:
                r = fetch_one(s['账号'], date_str)
                if r:
                    results.append(r)
            except Exception as e:
                print('  异常:', e)
    else:
        r = fetch_one(target, date_str)
        if r:
            results.append(r)

    out_path = os.path.join(BASE_DIR, '_fetch_result.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print('\n完成，结果已写:', out_path)

    # ★ 抓取收尾：自动补齐商品品类映射（增量）。
    #   放在「本轮全部账号抓完之后」跑一次：映射按 (平台, 商品ID) 去重，
    #   逐账号跑 N 次与跑 1 次结果完全相同，但只跑一次省掉 N-1 次全表扫描。
    if not no_map:
        auto_category_map('千牛')


if __name__ == '__main__':
    main()
