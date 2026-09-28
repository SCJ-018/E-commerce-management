# -*- coding: utf-8 -*-
"""抖店罗盘报表补数（安全回填版）。

用法：
  python report_backfill.py 2026-09-23 --missing

流程：商品列表「全部」和全店成交分析「成交概览」均通过页面下载，先做
字段、日期、商品 ID 集合校验，再按店铺事务入库。不会用 0 覆盖缺失报表，
也不会覆盖已经存在但商品集合不一致的商品快照。
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import shops  # noqa: E402
import login_fetch_all as lf  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

# 报表下载版本独立维护落库列定义。旧的接口抓取脚本不再是运行时依赖，
# 这样服务器可以删除旧 fetch_daily.py / fetch_main.py 而不影响补抓。
METRIC_MAP = [
    ('成交金额', 'trans_amt'),
    ('结算金额', 'receive_amt'),
    ('用户支付金额', 'pay_amt'),
    ('成交订单数', 'pay_cnt'),
    ('成交人数', 'pay_ucnt'),
    ('成交件数', 'pay_combo_cnt'),
    ('成交客单价', 'per_user_price'),
    ('商品结算金额（结算时间）', 'settle_amt'),
    ('实际佣金支出', 'real_commission'),
    ('净成交金额', 'net_trans_amt'),
    ('净成交订单数', 'net_pay_cnt'),
    ('成交退款金额', 'pay_refund_receive_amt'),
    ('退款金额(退款时间)', 'refund_amt'),
    ('退款订单数(退款时间)', 'refund_order_cnt'),
    ('退款人数(退款时间)', 'refund_ucnt'),
    ('退款件数(退款时间)', 'refund_combo_cnt'),
    ('商品曝光人数', 'product_show_ucnt'),
    ('商品曝光次数', 'product_show_cnt'),
    ('商品点击人数', 'product_click_ucnt'),
    ('商品点击次数', 'product_click_cnt'),
    ('曝光点击率（人数）', 'product_show_click_converse_uv_rate'),
    ('投放消耗（店铺被投）', 'ad_costed_amt'),
    ('投放消耗（推商品）', 'qc_ad_cost'),
    ('投放贡献成交金额', 'ad_receive_amt'),
    ('投放贡献成交退款金额', 'ad_receive_refund_amt'),
    ('投放费比（剔除退款、店铺被投）', 'ad_cost_ratio'),
    ('加购人数', 'click_add_to_cart_uv'),
    ('收藏人数', 'product_wish_ucnt'),
    ('评价好评率', 'comment_good_eval_ratio'),
    ('好评数', 'good_eval_cnt'),
    ('商品差评订单数', 'product_bad_eval_order_cnt'),
    ('商品差评率', 'product_bad_eval_ratio'),
    ('商品品质退货单量', 'product_quality_refund_order_cnt'),
    ('商品品质退货率', 'product_quality_refund_ratio'),
    ('投诉工单量', 'complaint_order_cnt_lt14'),
    ('投诉率', 'complaint_ratio'),
    ('客服不满意会话量', 'unsatisfied_convcmnt_cnt'),
    ('商详页曝光人数', 'product_detail_show_uv'),
    ('商详页曝光点击率', 'product_detail_click_uv_ratio'),
    ('商详页成交转化率', 'product_detail_pay_conversion_show_uv'),
    ('商详页跳失率', 'product_detail_no_act_leave_ratio'),
    ('平台消费券补贴金额', 'platform_coupon_cost_amt'),
]

DATE = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else (
    datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
USE_MISSING = '--missing' in sys.argv
DRY_RUN = '--dry-run' in sys.argv
TARGET_ARGS = [part.strip() for x in sys.argv[2:] if not x.startswith('--')
               for part in x.split(',') if part.strip()]
SLIDER_API_BASE = (os.environ.get('SLIDER_API_BASE') or 'https://julangkeji.site').rstrip('/')
SLIDER_KEY = os.environ.get('SLIDER_AGENT_KEY') or 'julang-doudian-slider-2026'
SLIDER_WAIT_SECONDS = int(os.environ.get('SLIDER_WAIT_SECONDS', '600'))
DL_DIR = os.path.join(BASE_DIR, '_downloads')
os.makedirs(DL_DIR, exist_ok=True)

TRADE_URL = (
    'https://compass.jinritemai.com/shop/business-part'
    '?defaultVisualType=all&date_type=20&date_value=%s%%2C%s&from_page=%%2Fshop'
)
PRODUCT_URL = (
    'https://compass.jinritemai.com/shop/commodity/product-list'
    '?from_page=%2Fshop%2Fsettlement-analysis'
)
DATE_VALUE = str(int(datetime.datetime.strptime(
    DATE, '%Y-%m-%d').replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp()))
TRADE_URL = TRADE_URL % (DATE_VALUE, DATE_VALUE)

NS = {
    'm': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'pr': 'http://schemas.openxmlformats.org/package/2006/relationships',
}


def norm_name(value):
    return str(value or '').strip().replace('（', '(').replace('）', ')')


def _col_num(ref):
    m = re.match(r'([A-Z]+)', ref or '')
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(path):
    """读取报表全部 sheet，保留空单元格位置并兼容 sharedStrings。"""
    with zipfile.ZipFile(path) as z:
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = ET.fromstring(z.read('xl/sharedStrings.xml'))
            shared = [''.join(t.text or '' for t in si.findall('.//m:t', NS))
                      for si in root.findall('m:si', NS)]
        wb = ET.fromstring(z.read('xl/workbook.xml'))
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        relmap = {x.attrib['Id']: x.attrib['Target'].lstrip('/')
                  for x in rels.findall('pr:Relationship', NS)}
        out = {}
        for sheet in wb.findall('m:sheets/m:sheet', NS):
            name = sheet.attrib.get('name', '')
            target = relmap.get(sheet.attrib.get('{%s}id' % NS['r']), '')
            if target and not target.startswith('xl/'):
                target = 'xl/' + target
            if not target or target not in z.namelist():
                continue
            root = ET.fromstring(z.read(target))
            rows = []
            for row in root.findall('.//m:sheetData/m:row', NS):
                vals = []
                for cell in row.findall('m:c', NS):
                    idx = _col_num(cell.attrib.get('r', ''))
                    while len(vals) <= idx:
                        vals.append('')
                    typ = cell.attrib.get('t')
                    v = cell.find('m:v', NS)
                    if typ == 's' and v is not None:
                        try:
                            vals[idx] = shared[int(v.text or '0')]
                        except (ValueError, IndexError):
                            vals[idx] = ''
                    elif typ == 'inlineStr':
                        vals[idx] = ''.join(t.text or '' for t in cell.findall('.//m:t', NS))
                    elif v is not None:
                        vals[idx] = v.text or ''
                rows.append(vals)
            out[name] = rows
        return out


def parse_date(value):
    s = str(value or '').strip()
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y%m%d'):
        try:
            return datetime.datetime.strptime(s[:10], fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    return s


def number(value):
    s = str(value or '').strip().replace(',', '')
    if s in ('', '-', '--', '—', '－'):
        return 0
    if s.endswith('%'):
        try:
            return Decimal(s[:-1]) / Decimal(100)
        except InvalidOperation:
            return 0
    try:
        return Decimal(s)
    except InvalidOperation:
        return 0


def click_exact_natural_day(page):
    loc = page.get_by_text('自然日', exact=True)
    for i in range(loc.count()):
        try:
            if loc.nth(i).is_visible():
                loc.nth(i).click()
                break
        except Exception:
            pass
    else:
        raise RuntimeError('页面未找到自然日控件')
    time.sleep(1)
    cells = page.locator('td[title="%s"]' % DATE)
    for i in range(cells.count()):
        try:
            # Aurora renders hidden duplicate calendar cells.  Calling click()
            # on the first hidden match incurs Playwright's full 30s timeout
            # and can make a valid report look like a failed shop.
            if not cells.nth(i).is_visible():
                continue
            cells.nth(i).click()
            time.sleep(3)
            return
        except Exception:
            pass
    raise RuntimeError('自然日控件中未找到 %s' % DATE)


def click_trade_date_if_available(page):
    """成交分析页使用“近1天/自定义”日期器，必须显式选择目标日。

    不能把 URL 的 date_value 当成已生效日期：实测页面仍显示近 1 天，
    下载文件实际是次日数据。优先走自定义日历，若版本提供自然日则兼容。
    """
    loc = page.get_by_text('自然日', exact=True)
    if any(_is_visible(loc.nth(i)) for i in range(loc.count())):
        click_exact_natural_day(page)
        return

    custom = page.get_by_text('自定义', exact=True)
    opened = False
    for i in range(custom.count()):
        if _is_visible(custom.nth(i)):
            custom.nth(i).click()
            opened = True
            break
    if not opened:
        raise RuntimeError('成交分析页未找到自然日或自定义日期控件')
    time.sleep(1)

    # Aurora 日历通常用 td[title=YYYY-MM-DD]；区间选择需要点击起止日两次。
    cells = page.locator('td[title="%s"]' % DATE)
    visible = [cells.nth(i) for i in range(cells.count()) if _is_visible(cells.nth(i))]
    if visible:
        visible[0].click()
        time.sleep(0.4)
        cells2 = page.locator('td[title="%s"]' % DATE)
        visible2 = [cells2.nth(i) for i in range(cells2.count()) if _is_visible(cells2.nth(i))]
        if visible2:
            visible2[-1].click()
        time.sleep(0.8)
        # 有些版本需要确认，有些点完第二次即关闭；只点可见的确认按钮。
        for label in ('确定', '完成'):
            ok = page.get_by_text(label, exact=True)
            for j in range(ok.count()):
                if _is_visible(ok.nth(j)):
                    ok.nth(j).click()
                    time.sleep(2)
                    break
            else:
                continue
            break
        print('    成交分析页已选择自定义日期:', DATE)
        return
    raise RuntimeError('成交分析自定义日历未找到 %s' % DATE)


def select_all_metrics(page):
    cfg = page.get_by_text('指标配置', exact=True)
    visible = [cfg.nth(i) for i in range(cfg.count())
               if _is_visible(cfg.nth(i))]
    if not visible:
        raise RuntimeError('商品列表未找到指标配置')
    visible[0].click()
    time.sleep(1)
    # 只点真正未选且未禁用的 checkbox label，避免点击分组标题和“异常商品”。
    clicked = 0
    labels = page.locator('label.aurora-checkbox-wrapper')
    for i in range(labels.count()):
        lab = labels.nth(i)
        try:
            if not lab.is_visible():
                continue
            cls = lab.get_attribute('class') or ''
            if 'disabled' in cls or 'checked' in cls or 'groupName' in cls:
                continue
            lab.click()
            clicked += 1
        except Exception:
            pass
    ok = page.get_by_text('确定', exact=True)
    for i in range(ok.count()):
        if _is_visible(ok.nth(i)):
            ok.nth(i).click()
            time.sleep(3)
            print('    指标配置确认，补选 %d 项' % clicked)
            return
    raise RuntimeError('指标配置未找到确定按钮')


def _is_visible(loc):
    try:
        return loc.is_visible()
    except Exception:
        return False


def _slider_request(path, method='GET', body=None, timeout=15):
    """通过现有滑块助手中继访问线上任务接口。"""
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(SLIDER_API_BASE + path, data=data, method=method)
    req.add_header('X-Slider-Key', SLIDER_KEY)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def request_slider_and_wait():
    """唤起本机滑块助手并等待其把新 state 写回数据库。"""
    try:
        r = _slider_request('/api/fetch/doudian/slider/request', 'POST', {})
        data = (r or {}).get('data') or {}
        task_id = data.get('taskId') or ''
        if not task_id:
            print('[滑块] 请求未创建任务：%s' % ((r or {}).get('msg') or '未知响应'))
            return False
        print('[滑块] 已唤起本机助手，任务 %s；请在前端弹出的 Chrome 中完成拼图' % task_id)
    except Exception as e:
        print('[滑块] 唤起助手失败：%s' % e)
        return False
    deadline = time.time() + SLIDER_WAIT_SECONDS
    while time.time() < deadline:
        try:
            r = _slider_request('/api/fetch/doudian/slider/status', timeout=15)
            view = (r or {}).get('data') or {}
            status = view.get('status') or ''
            print('[滑块] status=%s step=%s' % (status, view.get('step') or ''))
            if view.get('taskId') == task_id and status == 'done':
                print('[滑块] 本机已完成登录态更新，继续服务器补抓')
                return True
            if view.get('taskId') == task_id and status in ('fail', 'timeout'):
                print('[滑块] 本机滑块任务结束：%s' % (view.get('message') or status))
                return False
        except Exception as e:
            print('[滑块] 查询助手状态失败：%s' % e)
        time.sleep(5)
    print('[滑块] 等待超过 %ds，暂停本次日期' % SLIDER_WAIT_SECONDS)
    return False


def _write_account_state(state_path):
    fresh = shops.get_email_account() or {}
    state = fresh.get('state')
    if not state:
        return fresh, False
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False)
    return fresh, True


def _launch_report_browser(playwright, state_path):
    browser = playwright.chromium.launch(
        channel='chrome', headless=False,
        args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
    ctx = browser.new_context(
        storage_state=state_path, locale='zh-CN', timezone_id='Asia/Shanghai',
        viewport={'width': 1600, 'height': 950}, accept_downloads=True)
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    return browser, ctx, ctx.new_page()


def download_current(page, tag, menu_immediate=False):
    btn = page.get_by_text('下载明细', exact=True)
    visible = [btn.nth(i) for i in range(btn.count()) if _is_visible(btn.nth(i))]
    if not visible:
        raise RuntimeError('未找到下载明细按钮')
    direct = None
    if menu_immediate:
        # 成交分析页是“点击按钮并在同一事件循环点菜单项”的实现。
        try:
            with page.expect_download(timeout=120000) as di:
                visible[0].click()
                cur = page.get_by_text('下载当前明细', exact=True)
                opts = []
                deadline = time.time() + 5
                while time.time() < deadline and not opts:
                    opts = [cur.nth(i) for i in range(cur.count()) if _is_visible(cur.nth(i))]
                    if not opts:
                        time.sleep(0.2)
                if opts:
                    opts[0].click()
            direct = di.value
        except Exception:
            direct = None
    if direct is None and menu_immediate:
        raise RuntimeError('成交分析下载未触发文件')
    if direct is None:
        try:
            with page.expect_download(timeout=8000) as di:
                visible[0].click()
            direct = di.value
        except Exception:
            pass
    if direct is None:
        time.sleep(1)
        cur = page.get_by_text('下载当前明细', exact=True)
        opts = [cur.nth(i) for i in range(cur.count()) if _is_visible(cur.nth(i))]
        if not opts:
            raise RuntimeError('下载菜单未找到可见下载当前明细')
        with page.expect_download(timeout=120000) as di:
            opts[0].click()
        direct = di.value
    safe = re.sub(r'[^0-9A-Za-z_.-]+', '_', direct.suggested_filename)
    path = os.path.join(DL_DIR, '%s_%s_%s' % (tag, DATE, safe))
    direct.save_as(path)
    print('    已下载', os.path.basename(path), os.path.getsize(path), 'bytes')
    return path


def product_rows(path, allow_empty=False):
    sheets = read_xlsx(path)
    rows = sheets.get('全部')
    if not rows:
        raise RuntimeError('商品列表缺少「全部」sheet')
    headers = [norm_name(x) for x in rows[0]]
    # Some shops' current Compass export omits the unit-count metric
    # ``成交件数`` while still exporting order count and every other mapped
    # column.  It is a genuinely unavailable value in that report, so store
    # zero for that one metric; all dimensions and other metrics remain hard
    # requirements and still fail closed on schema drift.
    optional_metrics = {'成交件数'}
    required = ['统计周期', '商品名称', '商品编码', '载体', '品类', '自卖/合作'] + [
        norm_name(x[0]) for x in METRIC_MAP if x[0] not in optional_metrics]
    missing = [x for x in required if x not in headers]
    if missing:
        raise RuntimeError('商品列表缺字段: %s' % missing)
    idx = {h: i for i, h in enumerate(headers)}
    out = []
    ids = set()
    for raw in rows[1:]:
        if not any(str(x).strip() for x in raw):
            continue
        row = {h: (raw[i] if i < len(raw) else '') for h, i in idx.items()}
        if parse_date(row['统计周期']) != DATE:
            raise RuntimeError('商品列表日期异常: %r' % row['统计周期'])
        pid = str(row['商品编码']).strip()
        if not pid:
            raise RuntimeError('商品列表存在空商品编码')
        if pid in ids:
            raise RuntimeError('商品编码重复: %s' % pid)
        ids.add(pid)
        out.append(row)
    if not out and not allow_empty:
        raise RuntimeError('商品列表没有有效商品行')
    return out, ids, headers


def trade_row(path, shop):
    sheets = read_xlsx(path)
    rows = sheets.get('成交概览')
    if not rows or len(rows) < 2:
        raise RuntimeError('成交报表缺少「成交概览」第二行')
    headers = [norm_name(x) for x in rows[0]]
    raw = rows[1]
    row = {h: (raw[i] if i < len(raw) else '') for i, h in enumerate(headers)}
    if parse_date(row.get('日期')) != DATE:
        raise RuntimeError('成交报表日期异常: %r' % row.get('日期'))
    # Newer Compass exports no longer include a separate
    # ``投放贡献成交退款金额`` column.  In that format the only available
    # attribution value is ``投放贡献成交金额``; keep it as the net value
    # instead of rejecting an otherwise complete daily report.  Older exports
    # still use the explicit refund column and remain fully supported.
    needed = ['用户支付金额', '成交人数', '商品点击人数', '商品点击-成交转化率(人数)',
              '退款后用户支付金额(支付时间)', '退款金额(支付时间)', '退款率(支付时间)',
              '客单价', '投放消耗(店铺被投)', '投放贡献成交金额']
    miss = [x for x in needed if x not in row]
    if miss:
        raise RuntimeError('成交概览缺字段: %s' % miss)
    pay = number(row['用户支付金额'])
    net_pay = number(row['退款后用户支付金额(支付时间)'])
    ad_total = number(row['投放贡献成交金额'])
    ad_refund = number(row.get('投放贡献成交退款金额', 0))
    return {
        '店铺ID': str(shop['店铺ID']), '平台': '抖音', '店铺名': shop['店铺名'],
        '品牌': shop['品牌'], '日期': DATE,
        '支付金额': pay, '净支付金额': net_pay,
        '访客数': int(number(row['商品点击人数'])),
        '支付买家数': int(number(row['成交人数'])),
        '支付转化率': number(row['商品点击-成交转化率(人数)']),
        '退款金额': number(row['退款金额(支付时间)']),
        '订单退款率': number(row['退款率(支付时间)']),
        '客单价': number(row['客单价']),
        '推广花费': number(row['投放消耗(店铺被投)']),
        '推广总成交': ad_total,
        '推广净成交': ad_total - ad_refund,
    }


def existing_state(conn, shop_name):
    with conn.cursor() as cur:
        cur.execute('SELECT 商品编码 FROM `抖店单链接数据表` WHERE 店铺名=%s AND 统计周期=%s',
                    (shop_name, DATE))
        ids = {str(x['商品编码']) for x in cur.fetchall()}
        cur.execute('SELECT COUNT(*) n FROM `店铺营销数据` WHERE 店铺名=%s AND 平台=%s AND 日期=%s',
                    (shop_name, '抖音', DATE))
        main = int(cur.fetchone()['n'])
    return ids, main


def save_product(conn, shop, rows):
    # 报表前 48 列 = 6 个维度 + 42 个现有指标；店铺名由切店上下文补入。
    cols = ['店铺名', '统计周期', '商品名称', '商品编码', '载体', '品类', '自卖/合作'] + [x[0] for x in METRIC_MAP]
    values = []
    for r in rows:
        vals = [shop['店铺名'], parse_date(r['统计周期']), r['商品名称'], str(r['商品编码']).strip(),
                r['载体'], r['品类'], r['自卖/合作']]
        vals.extend(number(r.get(norm_name(name), 0)) for name, _ in METRIC_MAP)
        values.append(vals)
    quoted = ', '.join('`%s`' % c for c in cols)
    sql = 'INSERT INTO `抖店单链接数据表` (%s) VALUES (%s)' % (quoted, ','.join(['%s'] * len(cols)))
    with conn.cursor() as cur:
        cur.execute('DELETE FROM `抖店单链接数据表` WHERE 店铺名=%s AND 统计周期=%s',
                    (shop['店铺名'], DATE))
        cur.executemany(sql, values)
    return len(values)


def save_main(conn, row):
    cols = list(row.keys())
    quoted = ', '.join('`%s`' % c for c in cols)
    ph = ','.join(['%s'] * len(cols))
    updates = ', '.join('`%s`=VALUES(`%s`)' % (c, c) for c in cols)
    sql = 'INSERT INTO `店铺营销数据` (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s' % (quoted, ph, updates)
    with conn.cursor() as cur:
        cur.execute(sql, [row[c] for c in cols])
    return 1


def target_shops():
    all_shops = shops.get_active_shops()
    if TARGET_ARGS:
        wanted = set(TARGET_ARGS)
        return [s for s in all_shops if s['店铺名'] in wanted]
    if not USE_MISSING:
        return all_shops
    conn = shops.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT DISTINCT 店铺名 FROM `抖店单链接数据表` WHERE 统计周期=%s', (DATE,))
            prod = {r['店铺名'] for r in cur.fetchall()}
            cur.execute('SELECT DISTINCT 店铺名 FROM `店铺营销数据` WHERE 平台=%s AND 日期=%s', ('抖音', DATE))
            main = {r['店铺名'] for r in cur.fetchall()}
    finally:
        conn.close()
    return [s for s in all_shops if s['店铺名'] not in prod or s['店铺名'] not in main]


def main():
    targets = target_shops()
    if not targets:
        print('没有需要补抓的店铺')
        return 0
    acc = shops.get_email_account() or {}
    if not acc.get('state'):
        print('[FAIL] 数据库中没有有效抖店登录态')
        return 2
    state_path = os.path.join(BASE_DIR, '_report_backfill_state.json')
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(acc['state'], f, ensure_ascii=False)

    conn = shops.get_conn()
    try:
        with sync_playwright() as p:
            browser, ctx, page = _launch_report_browser(p, state_path)
            page.goto(lf.WORKBENCH, wait_until='domcontentloaded', timeout=60000)
            time.sleep(10)
            cur = lf.current_shop(page) or ''
            body = page.inner_text('body') or ''
            login_page = ('/login' in page.url or 'passport' in page.url or
                          '发送验证码' in body or '扫码登录' in body or not cur)
            if login_page:
                # 服务器无可见窗口：自动尝试邮箱登录；只有实际检测到拼图时才失败转人工。
                # 本地运行仍保留原行为，可手工完成滑块。
                if os.path.exists('/opt/pw'):
                    lf.UNATTENDED = True
                first_shop = targets[0]['店铺名']
                print('登录态已过期，尝试自动登录（先检测滑块）...')
                if not lf.do_login(page, first_shop):
                    # 服务器 Xvfb 没有可供人工拖拽的窗口，转交给本机常驻滑块助手。
                    # 助手完成后关闭旧上下文，重新加载数据库里的新 storage state。
                    if not request_slider_and_wait():
                        print('[FAIL] 滑块助手未完成，暂停本次日期')
                        browser.close(); return 3
                    browser.close()
                    acc, ok = _write_account_state(state_path)
                    if not ok:
                        print('[FAIL] 滑块助手完成但数据库没有新登录态')
                        return 3
                    browser, ctx, page = _launch_report_browser(p, state_path)
                    page.goto(lf.WORKBENCH, wait_until='domcontentloaded', timeout=60000)
                    time.sleep(10)
                    if '/login' in page.url or 'passport' in page.url:
                        print('[FAIL] 新登录态加载后仍在登录页')
                        browser.close(); return 3
                else:
                    shops.save_email_state(acc['邮箱'], ctx.storage_state())
                cur = lf.current_shop(page) or first_shop
                print('自动登录成功，已更新数据库登录态')
            results = []
            for shop in targets:
                name = shop['店铺名']
                print('\n===== %s =====' % name)
                last_error = None
                # 页面在下载大店报表后偶尔会停在空白/加载中状态，导致后续
                # 店铺找不到右上角入口。每家店最多恢复一次页面并重试；
                # 单店失败不会阻断同批其它店铺。
                for attempt in range(2):
                    stage = '准备切店'
                    try:
                        if not lf.current_shop(page):
                            stage = '恢复工作台'
                            page.goto(lf.WORKBENCH, wait_until='domcontentloaded', timeout=60000)
                            time.sleep(8)
                            cur = lf.current_shop(page) or cur
                        stage = '切店'
                        if name[:6] not in cur and not lf.switch_shop(page, name, cur):
                            raise RuntimeError('切店失败：未找到店铺入口或切换菜单')
                        cur = lf.current_shop(page) or name
                        trade = None
                        product = None
                        stage = '打开成交报表'
                        page.goto(TRADE_URL, wait_until='domcontentloaded', timeout=60000)
                        time.sleep(8)
                        stage = '选择成交日期'
                        click_trade_date_if_available(page)
                        stage = '下载成交报表'
                        trade = download_current(page, 'trade_' + re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]+', '_', name), menu_immediate=True)
                        stage = '打开商品报表'
                        page.goto(PRODUCT_URL, wait_until='domcontentloaded', timeout=60000)
                        time.sleep(8)
                        stage = '选择商品日期'
                        click_exact_natural_day(page)
                        stage = '配置商品指标'
                        select_all_metrics(page)
                        stage = '下载商品报表'
                        product = download_current(page, 'product_' + re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]+', '_', name))
                        stage = '校验报表字段'
                        mrow = trade_row(trade, shop)
                        # 商品列表 0 行表示当天没有商品明细，仍然要写入成交主表；
                        # 商品表保持 0 行，不把它当成抓取失败。
                        prows, pids, _ = product_rows(product, allow_empty=True)
                        old_ids, old_main = existing_state(conn, name)
                        if old_ids and pids and old_ids != pids:
                            raise RuntimeError('已有商品 ID 集合与报表不一致，拒绝覆盖（库=%d，报表=%d）' % (len(old_ids), len(pids)))
                        do_product = (not old_ids) and bool(prows)
                        do_main = not old_main
                        if DRY_RUN:
                            print('    校验通过：商品 %d 行；待写商品=%s，待写店铺主表=%s' %
                                  (len(prows), do_product, do_main))
                        else:
                            stage = '写入数据库'
                            if do_product:
                                n = save_product(conn, shop, prows)
                                print('    商品表写入 %d 行' % n)
                            if do_main:
                                save_main(conn, mrow)
                                print('    店铺营销表写入 1 行')
                            conn.commit()
                            print('    已提交事务')
                        results.append({'店铺': name, 'status': 'ok', 'products': len(prows),
                                        'write_product': do_product, 'write_main': do_main})
                        last_error = None
                        break
                    except Exception as e:
                        conn.rollback()
                        last_error = '%s：%s' % (stage, str(e))
                        print('    [FAIL attempt %d/2] %s' % (attempt + 1, last_error))
                        if attempt == 0:
                            try:
                                page.goto(lf.WORKBENCH, wait_until='domcontentloaded', timeout=60000)
                                time.sleep(8)
                                cur = lf.current_shop(page) or ''
                                print('    [RECOVER] 已重新打开工作台，继续处理下一次尝试')
                            except Exception as recover_error:
                                print('    [RECOVER FAIL] %s' % recover_error)
                if last_error:
                    results.append({'店铺': name, 'status': 'skip', 'error': last_error})
            browser.close()
    finally:
        conn.close()
    print('\nRESULT=' + json.dumps(results, ensure_ascii=False))
    return 0 if all(x['status'] == 'ok' for x in results) else 1


if __name__ == '__main__':
    sys.exit(main())
