# -*- coding: utf-8 -*-
"""抖店主表取数 —— 罗盘三接口 → 云库「店铺营销数据」16 字段。

⚠️ 全部接口 date_type=2（自然日单日）。21 是自然周，严禁使用（2026-09-16 踩坑）。
接口（页面内 fetch 免签名，需处于 compass.jinritemai.com 页面上下文）：
  1. /compass_api/shop/common/trade/core_index_v3
     → 支付金额/净支付金额/支付买家数/退款金额/订单退款率/客单价/推广总成交/推广净成交
  2. /compass_api/shop/common/trade/income_expense_index_v3
     → 推广花费（ad_costed_amt）
  3. /compass_api/shop/product/product_flow_analysis/flow_overview_card
     → 访客数（product_click_ucnt）/ 支付转化率（product_click_pay_converse_uv_rate）

字段映射（已与影刀历史 8-30 + 成交概览 Excel 双重对照锁定，见 2026-09-16 日志）：
  支付金额 = core.pay_amt/100          净支付金额 = (pay_amt - rfndsuc_amt)/100
  退款金额 = core.rfndsuc_amt/100      订单退款率 = refund_order_cnt/pay_cnt
  客单价   = core.per_usr_pay_amt/100  推广花费   = ie.ad_costed_amt/100
  推广总成交 = core.ad_income_amt/100  推广净成交 = core.ad_complex_com_amt/100
  访客数   = flow.product_click_ucnt   支付转化率 = flow.product_click_pay_converse_uv_rate
落库：平台=抖音，主键(店铺ID,日期,平台,品牌) upsert；比率存小数。
"""
import sys
import os
import time
import json
import datetime
import urllib.parse

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

CORE_API = 'https://compass.jinritemai.com/compass_api/shop/common/trade/core_index_v3'
IE_API = 'https://compass.jinritemai.com/compass_api/shop/common/trade/income_expense_index_v3'
FLOW_API = ('https://compass.jinritemai.com/compass_api/shop/product/'
            'product_flow_analysis/flow_overview_card')

# core_index_v3 需要的指标（6 个核心 + 3 个投放成交）
CORE_INDEX = ('pay_amt,pay_ucnt,pay_cnt,per_usr_pay_amt,rfndsuc_amt,refund_order_cnt,'
              'ad_income_amt,ad_complex_com_amt')
FLOW_INDEX = 'product_click_ucnt,product_click_pay_converse_uv_rate,pay_ucnt'

FETCH_JS = """
async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  const t = await r.text();
  return {status: r.status, body: t};
}
"""


def _enc(date_str):
    d = datetime.datetime.strptime(date_str, '%Y-%m-%d')
    return urllib.parse.quote('%d/%02d/%02d 00:00:00' % (d.year, d.month, d.day))


def _f(v, default=0.0):
    if v is None or v == '':
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fetch_json(page, url, what, max_retry=10):
    """页面内 fetch 取 JSON。限流(st=11001)时指数退避重试（15s 起步，最多 10 次）。

    三个罗盘接口与商品列表共用账号级限流额度，大店翻页会消耗额度导致小店第 1 页就
    被限流（st=11001「请求过于频繁」），绝不能当无数据跳过——必须退避重试。
    其它 st（100003 参数错 / 100008 moduleKey 空等）是永久性错误，快重试 2 次即抛异常。
    重试耗尽仍失败则抛异常，让调用方（fetch_one）明确报「主表失败」，而非静默落 0 行。
    """
    wait = 15
    last = None
    for attempt in range(max_retry):
        try:
            r = page.evaluate(FETCH_JS, url)
            b = json.loads(r['body'])
        except Exception as e:
            print('    [%s] fetch/解析异常: %s (退避 %ds, %d/%d)'
                  % (what, str(e)[:50], wait, attempt + 1, max_retry))
            time.sleep(wait)
            wait = int(wait * 1.5)
            continue
        st = b.get('st') if isinstance(b, dict) else None
        if st == 0:
            return b
        last = b
        # 限流 → 长退避重试；其它永久错误 → 快重试 2 次即止
        if st == 11001:
            backoff = wait
            wait = int(wait * 1.5)
        else:
            backoff = 5
            if attempt >= 2:
                break
        print('    [%s] st=%s %r (退避 %ds, %d/%d)'
              % (what, st, (b.get('msg', '') if isinstance(b, dict) else ''),
                 backoff, attempt + 1, max_retry))
        time.sleep(backoff)
    raise RuntimeError('%s 重试耗尽 (st=%s)' % (what, (last or {}).get('st')))


def _day(date_str):
    return datetime.datetime.strptime(date_str, '%Y-%m-%d').strftime('%Y-%m-%d')


def fetch_core(page, date_str):
    """成交概览 + 投放成交指标。返回 dict{code: value}（原始值，分）。"""
    enc = _enc(date_str)
    url = (CORE_API + '?date_type=2&end_date=%s&begin_date=%s&activity_id=&is_activity=false'
           '&operate_type=0&content_type=0&index_selected=%s&traffic_channel=1'
           % (enc, enc, urllib.parse.quote(CORE_INDEX)))
    b = _fetch_json(page, url, 'core_index_v3')
    card = b['data']['module_data']['homepage_core_index']['compass_general_multi_index_card_value']
    data = card['data'][0]
    out = {}
    for m in card['basic_meta']:
        nm = m['index_name']
        v = (data.get(nm) or {}).get('index_value', {}).get('value', {})
        out[nm] = v.get('value')
    return out


def fetch_income_expense(page, date_str):
    """投放消耗 ad_costed_amt（分）。"""
    enc = _enc(date_str)
    url = (IE_API + '?date_type=2&end_date=%s&begin_date=%s&activity_id=&is_activity=false'
           '&operate_type=0&content_type=0&traffic_channel=1&refund_type=1'
           '&select_ad_expense_ratio=ad_costed_expense_ratio_with_refund&select_ad_cost=ad_costed_amt'
           % (enc, enc))
    b = _fetch_json(page, url, 'income_expense_index_v3')
    # 结构：module_data.<module>.compass_general_multi_index_card_value.data[0].{code:{index_value:{value:{value}}}}
    md = b.get('data', {}).get('module_data', {})
    for _k, node in md.items():
        card = node.get('compass_general_multi_index_card_value') or {}
        rows = card.get('data') or []
        if rows:
            row = rows[0]
            v = ((row.get('ad_costed_amt') or {}).get('index_value') or {}).get('value', {})
            return v.get('value')
    return None


def fetch_flow(page, date_str):
    """访客数 + 支付转化率。返回 dict。"""
    enc = _enc(date_str)
    url = (FLOW_API + '?date_type=2&end_date=%s&begin_date=%s&activity_id=&is_activity=false&index_selected=%s'
           % (enc, enc, urllib.parse.quote(FLOW_INDEX)))
    b = _fetch_json(page, url, 'flow_overview_card')
    data = b.get('data')
    ci = None
    if isinstance(data, list) and data:
        ci = data[0].get('cell_info')
    elif isinstance(data, dict):
        ci = data.get('cell_info')
    out = {}
    if ci:
        for code in FLOW_INDEX.split(','):
            node = ci.get(code) or {}
            iv = node.get(code + '_index_value') or node.get(code + '_index_values') or {}
            out[code] = ((iv.get('index_values') or {}).get('value') or {}).get('value')
    return out


def build_row(shop, date_str, core, ie_cost, flow):
    """拼 16 字段行（dict，列名与库表一致）。"""
    pay_amt = _f(core.get('pay_amt'))
    rfndsuc = _f(core.get('rfndsuc_amt'))
    pay_cnt = _f(core.get('pay_cnt'))
    refund_order_cnt = _f(core.get('refund_order_cnt'))
    ucnt = _f(flow.get('product_click_ucnt'))
    row = {
        '店铺ID': str(shop['店铺ID']),
        '平台': '抖音',
        '店铺名': shop['店铺名'],
        '品牌': shop['品牌'],
        '日期': _day(date_str),
        '支付金额': round(pay_amt / 100.0, 2),
        '净支付金额': round((pay_amt - rfndsuc) / 100.0, 2),
        '访客数': int(ucnt),
        '支付买家数': int(_f(core.get('pay_ucnt'))),
        '支付转化率': round(_f(flow.get('product_click_pay_converse_uv_rate')), 4),
        '退款金额': round(rfndsuc / 100.0, 2),
        '订单退款率': round(refund_order_cnt / pay_cnt, 4) if pay_cnt else 0.0,
        '客单价': round(_f(core.get('per_usr_pay_amt')) / 100.0, 2),
        '推广花费': round(_f(ie_cost) / 100.0, 2),
        '推广总成交': round(_f(core.get('ad_income_amt')) / 100.0, 2),
        '推广净成交': round(_f(core.get('ad_complex_com_amt')) / 100.0, 2),
    }
    return row


def save_row(conn, row):
    cols = list(row.keys())
    placeholders = ', '.join(['%s'] * len(cols))
    quoted = ', '.join('`%s`' % c for c in cols)
    updates = ', '.join('`%s`=VALUES(`%s`)' % (c, c) for c in cols)
    sql = ('INSERT INTO `店铺营销数据` (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s'
           % (quoted, placeholders, updates))
    with conn.cursor() as cur:
        cur.execute(sql, [row[c] for c in cols])
    return 1


def fetch_one(page, conn, shop, date_str):
    """在 compass 页上下文抓一家店主表并落库。返回 row dict。"""
    core = fetch_core(page, date_str)
    ie_cost = fetch_income_expense(page, date_str)
    flow = fetch_flow(page, date_str)
    row = build_row(shop, date_str, core, ie_cost, flow)
    save_row(conn, row)
    print('  [库] 店铺营销数据 upsert | 支付 %s / 净支付 %s / 访客 %s / 推广花费 %s / 推广总成交 %s'
          % (row['支付金额'], row['净支付金额'], row['访客数'], row['推广花费'], row['推广总成交']))
    return row
