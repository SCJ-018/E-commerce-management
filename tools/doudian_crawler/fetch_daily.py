# -*- coding: utf-8 -*-
"""抖店取数生产版 —— 罗盘商品列表 44 指标 → 云库「抖店单链接数据表」。

接口（已破解，无需签名 a_bogus）：
  GET /compass_api/shop/product/product/product_list
  date_type=2（自然日单日，严禁 21=自然周）&begin_date/end_date=YYYY/M/D 00:00:00&page_size=10（上限10，翻页抓全）
  在页面上下文内 fetch（带 cookie），返回 data[].cell_info（44 指标编码 + product_info）。

用法：
  python fetch_daily.py <店铺名> [YYYY-MM-DD]   # 日期缺省=昨天
  店铺名与「抖店账号表」一致；本地 _states/ 需有该店 state（login_save_state.py <店铺名> 生成）

落库口径（对齐影刀历史）：
  - 载体/品类/自卖合作 固定填「全部」
  - 比率列存小数（0.1834），金额/数量直接存
  - 主键 统计周期+商品编码 upsert；空值/非数字 → 0
"""
import sys
import os
import time
import json
import datetime
import urllib.parse
import subprocess

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, TOOLS_DIR)
sys.path.insert(0, BASE_DIR)
import shops  # noqa: E402
import pymysql  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from crawler_runtime import RequestGovernor  # noqa: E402


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

PRODUCT_LIST_URL = ('https://compass.jinritemai.com/shop/commodity/product-list'
                    '?from_page=%2Fshop%2Fsettlement-analysis')
API = 'https://compass.jinritemai.com/compass_api/shop/product/product/product_list'

# ── 限流（st=11001 请求过于频繁）退避参数 ────────────────────────────────────
# ⚠️ 2026-09-17 修复：fetch_products() 一直在引用 LIMIT_RETRY / LIMIT_BACKOFF，
#   但这两个常量**从未定义** → 一撞限流就抛 NameError，被调用方当「取数异常」吞掉，
#   真因（st=什么、msg 是什么）全被 `name 'LIMIT_RETRY' is not defined` 盖住，
#   排查时看到的是代码错误而不是限流，白绕一圈。现补上定义。
# 口径：只重试 1 次、退避 90s 就放弃本店。重试本身也是新请求，会续期限流窗口。
LIMIT_RETRY = 1
LIMIT_BACKOFF = 90
GOVERNOR = RequestGovernor('doudian')

# 44 指标编码（与影刀 §5 的 44 项一一对应；后 2 项混资落库表无列，仍抓取备用）
INDEX_44 = ('trans_amt,receive_amt,pay_amt,pay_cnt,pay_ucnt,pay_combo_cnt,per_user_price,'
            'settle_amt,real_commission,net_trans_amt,net_pay_cnt,pay_refund_receive_amt,'
            'refund_amt,refund_order_cnt,refund_ucnt,refund_combo_cnt,product_show_ucnt,'
            'product_show_cnt,product_click_ucnt,product_click_cnt,product_show_click_converse_uv_rate,'
            'ad_costed_amt,qc_ad_cost,ad_receive_amt,ad_receive_refund_amt,ad_cost_ratio,'
            'click_add_to_cart_uv,product_wish_ucnt,comment_good_eval_ratio,good_eval_cnt,'
            'product_bad_eval_order_cnt,product_bad_eval_ratio,product_quality_refund_order_cnt,'
            'product_quality_refund_ratio,complaint_order_cnt_lt14,complaint_ratio,'
            'unsatisfied_convcmnt_cnt,product_detail_show_uv,product_detail_click_uv_ratio,'
            'product_detail_pay_conversion_show_uv,product_detail_no_act_leave_ratio,'
            'platform_coupon_cost_amt,mix_coupon_shop_pay_amt,mix_coupon_new_shop_pay_ucnt')

# 落库列 ← 指标编码（抖店单链接数据表 42 个数据列）
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

# 金额类指标（罗盘接口单位是「分」，落库前 ÷100 转元；已与影刀历史客单价对照验证）
MONEY_CODES = {
    'trans_amt', 'receive_amt', 'pay_amt', 'per_user_price', 'settle_amt',
    'real_commission', 'net_trans_amt', 'pay_refund_receive_amt', 'refund_amt',
    'ad_costed_amt', 'qc_ad_cost', 'ad_receive_amt', 'ad_receive_refund_amt',
    'platform_coupon_cost_amt',
}

FETCH_JS = """
async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  const t = await r.text();
  return {status: r.status, body: t};
}
"""


def _f(v, default=0.0):
    """归一数值：None/空/非数字 → default。"""
    if v is None or v == '':
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _cell_metric(ci, code):
    """从 cell_info 取指标数值。结构 cell_info[code][code_index_values].index_values.value。"""
    node = ci.get(code) or {}
    iv = node.get(code + '_index_values') or {}
    vals = iv.get('index_values') or {}
    v = vals.get('value')
    if isinstance(v, dict):
        if 'value' in v:
            return v['value']
        if 'value_str' in v:
            return v['value_str']
    elif v is not None:
        return v
    return None


def _cell_text(ci, key):
    """从 cell_info 取文本（product_info 内）。"""
    node = (ci.get('product_info') or {}).get(key) or {}
    v = node.get('value') or {}
    if 'value_str' in v:
        return v['value_str']
    if 'value' in v:
        return str(v['value'])
    return ''


def fetch_products(page, date_str):
    """页面内 fetch 翻页抓全商品列表。返回 list[dict原始行]。

    限流（st=11001 请求过于频繁）退避策略见文件顶部 LIMIT_RETRY / LIMIT_BACKOFF。
    ★ 核心认知（2026-09-17 实测，别再改回去）：重试本身**也是一次新请求**，
      会不断续期抖店罗盘的滑动窗口 —— 越是死磕退避，窗口越不恢复。
      当天证据链：10:19 连抓 7 家后第 8 家限流 → 10:43（+24min）仍限流 →
      11:00 勉强抓 3 家又限流 → 11:07 **换本机网络+本机有头 Chrome 依然限流**。
      → 既不是服务器机房 IP 被风控，也不是浏览器环境问题，就是账号级接口配额。
      策略：重试 1 次后立即抛异常，把「等窗口清空」交给上层分钟级处理。
    """
    d = datetime.datetime.strptime(date_str, '%Y-%m-%d')
    bd = '%d/%02d/%02d 00:00:00' % (d.year, d.month, d.day)
    ed = bd
    rows = []
    page_no = 1
    retry = 0
    while True:
        # ⚠️ date_type 必须=2（自然日单日）。21 是「自然周」（返回以 end_date 结尾的一整周数据，
        # 金额虚高约5倍，2026-09-16 已踩坑重抓）。日期格式 YYYY/M/D 00:00:00。
        qs = ('date_type=2&begin_date=' + urllib.parse.quote(bd) + '&end_date=' + urllib.parse.quote(ed) +
              '&is_activity=false&activity_id=&key_word=&index_selected=' + urllib.parse.quote(INDEX_44) +
              '&sale_type=1&content_type=1&cate_ids_original=0&product_tab=0'
              '&only_abnormal=false&only_drop_gmv=false&only_drop_product_show=false'
              '&use_customize_gmv=false&use_customize_product_show=false'
              '&abnormal_threshold_gmv=0&abnormal_threshold_product_show=0'
              '&new_version=true&page_no=%d&page_size=10' % page_no)
        GOVERNOR.before()
        r = page.evaluate(FETCH_JS, API + '?' + qs)
        GOVERNOR.after(r.get('status', 0))
        b = json.loads(r['body'])
        st = b.get('st')
        data = b.get('data')
        pr = b.get('page_result') or {}
        total = pr.get('total') or 0
        if st != 0 or not isinstance(data, list) or not data:
            if st != 0:
                # 抖店常把限流编码放在 HTTP 200 响应体中；把它转换为
                # 共享冷却信号，避免下一家店立刻继续打同一账号配额。
                if str(st) in ('11001', '11002', '429'):
                    GOVERNOR.after(429)
                if retry < LIMIT_RETRY:
                    # 只重试 LIMIT_RETRY 次（默认 1 次、退避 90s）就放弃本店。
                    # 旧版是 4 次（30/60/90/120s，共 5 分钟）—— 但每次重试都是新请求，
                    # 实测反而把限流窗口续得更久（10:19 触发后 45 分钟不恢复）。
                    retry += 1
                    wait = LIMIT_BACKOFF
                    print('  第 %d 页 st=%s %r，退避 %ds 后重试 %d/%d...'
                          % (page_no, st, b.get('msg', ''), wait, retry, LIMIT_RETRY))
                    time.sleep(wait)
                    continue
                # ⚠️ 重试耗尽仍失败 → 必须抛异常！
                # 2026-09-16 踩坑：原来是 break，调用方把「限流失败」当成「真的没商品」，
                # 静默返回 0 行/半截行并落库，库里留下一周前的旧口径数据且零告警。
                raise RuntimeError('商品列表第 %d 页限流重试耗尽 (st=%s %r)'
                                   % (page_no, st, b.get('msg', '')))
            break  # st=0 且无数据 = 真的没有更多了
        retry = 0
        rows.extend(data)
        print('  第 %d 页: %d 条 (累计 %d / total %s)' % (page_no, len(data), len(rows), total))
        if len(rows) >= total or len(data) < 10:
            break
        page_no += 1
        # ⚠️ 翻页间隔 2.5s 太密：抖店罗盘对单账号「单位时间请求数」限流很敏感，
        #    2026-09-17 实测连抓 7 家店后第 8 家第 1 页即 st=11001，
        #    退避 13 分钟仍未恢复，导致剩余店铺全废。放到 4.5s 拉长请求间隔。
        time.sleep(4.5)
    return rows


def save_rows(conn, shop, date_str, rows):
    """替换式写入：先清该店该周期旧行，再插本次结果。

    为什么不用 upsert（2026-09-16 踩坑）：
      ① 口径混存——库里可能残留上一次「周口径(date_type=21)」的行，upsert 只覆盖
         本次抓到的商品编码，抓不全时会留下旧口径数据且无告警；
      ② 陈旧行——本次已无曝光的商品在 upsert 下会永远留着。
    → 改为「本次结果即该店该日全部快照」。⚠️ 仅在 fetch_products 成功返回后调用；
      抓取失败（限流）时调用方不调用本函数，旧数据保留并由汇总明确告警。
    """
    cols = ['店铺名', '统计周期', '商品名称', '商品编码', '载体', '品类', '自卖/合作'] + [c for c, _ in METRIC_MAP]
    out = []
    for r in rows:
        ci = r.get('cell_info') or {}
        pid = _cell_text(ci, 'product_id_value')
        pname = _cell_text(ci, 'product_name_value')
        if not pid:
            continue
        row = [shop['店铺名'], date_str, pname, pid, '全部', '全部', '全部']
        for _, code in METRIC_MAP:
            v = _f(_cell_metric(ci, code), 0.0)
            if code in MONEY_CODES:
                v = round(v / 100.0, 2)
            row.append(v)
        out.append(row)
    placeholders = ', '.join(['%s'] * len(cols))
    quoted = ', '.join('`%s`' % c for c in cols)
    sql = 'INSERT INTO `抖店单链接数据表` (%s) VALUES (%s)' % (quoted, placeholders)
    with conn.cursor() as cur:
        cur.execute('DELETE FROM `抖店单链接数据表` WHERE `店铺名`=%s AND `统计周期`=%s',
                    (shop['店铺名'], date_str))
        deleted = cur.rowcount or 0
        if not out:
            if deleted:
                print('  [warn] 本次抓取 0 行，已清掉旧 %d 行（若非预期请核查是否限流/接口变更）' % deleted)
            else:
                print('  [warn] 无有效行')
            return 0
        cur.executemany(sql, out)
    return len(out)


def state_path_for(shop_name):
    acc = shops.get_email_account()
    email = acc['邮箱'] if acc else 'pcl526@yeah.net'
    safe = ''.join(c for c in shop_name if c not in '\\/:*?"<>| ')
    return os.path.join(BASE_DIR, '_states', '抖店_%s_%s.json' % (
        email.replace('@', '_').replace('.', '_'), safe))


def fetch_one(shop_name, date_str):
    shop = None
    for s in shops.get_active_shops():
        if s['店铺名'] == shop_name:
            shop = s
            break
    if not shop:
        print('[FAIL] 抖店账号表中无此店铺:', shop_name)
        return None

    sp = state_path_for(shop_name)
    if not os.path.exists(sp):
        print('[FAIL] 无该店 state，先跑: python login_save_state.py "%s"' % shop_name)
        print('  期望文件:', sp)
        return None
    state = json.load(open(sp, encoding='utf-8'))
    print('state 文件:', os.path.basename(sp), '| cookies:', len(state.get('cookies', [])))

    result = {'店铺': shop_name, '日期': date_str}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=False,
                                    args=['--no-sandbox'])
        ctx = browser.new_context(storage_state=state, locale='zh-CN', timezone_id='Asia/Shanghai',
                                  viewport={'width': 1600, 'height': 950})
        page = ctx.new_page()

        print('[1] 打开罗盘商品列表页（建立会话）...')
        page.goto(PRODUCT_LIST_URL, wait_until='domcontentloaded', timeout=60000)
        time.sleep(10)
        if 'passport' in page.url or '扫码' in page.title():
            print('[FAIL] 罗盘需要重新登录（state 失效）')
            browser.close()
            return None

        print('[2] 翻页抓全商品列表（%s）...' % date_str)
        try:
            rows = fetch_products(page, date_str)
        except Exception as e:
            print('[FAIL] 取数失败:', e)
            browser.close()
            return None
        result['商品数'] = len(rows)
        print('  共 %d 个商品' % len(rows))
        browser.close()

    if rows:
        # 首条样本
        ci0 = rows[0].get('cell_info') or {}
        print('  样本:', _cell_text(ci0, 'product_name_value')[:30],
              '| 成交金额=', _cell_metric(ci0, 'trans_amt'))

    print('[3] 落库...')
    conn = shops.get_conn()
    try:
        n = save_rows(conn, shop, date_str, rows)
        conn.commit()
        print('  [库] 抖店单链接数据表 upsert %d 行' % n)
    finally:
        conn.close()
    return result


def main():
    raw = sys.argv[1:]
    no_map = '--no-map' in raw
    args = [a for a in raw if not a.startswith('--')]
    if not args:
        print(__doc__)
        print('可选参数: --no-map  抓完不自动跑品类增量映射')
        return
    target = args[0]
    date_str = args[1] if len(args) > 1 else (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    if target == 'all':
        for s in shops.get_active_shops():
            print('\n======== 取数：%s (%s) ========' % (s['店铺名'], date_str))
            try:
                fetch_one(s['店铺名'], date_str)
            except Exception as e:
                print('  异常:', e)
    else:
        fetch_one(target, date_str)

    # ★ 抓取收尾：自动补齐商品品类映射（增量）。
    #   放在「本轮全部店铺抓完之后」跑一次：映射按 (平台, 商品ID) 去重，
    #   逐店跑 13 次与跑 1 次结果完全相同，但只跑一次省掉 12 次全表扫描。
    if not no_map:
        auto_category_map('抖店')


if __name__ == '__main__':
    main()
