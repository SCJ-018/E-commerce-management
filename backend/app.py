"""
电商后台管理系统 - Flask API 服务
连接远程 MySQL 数据库，提供 RESTful API
启动方式：python app.py  （默认监听 0.0.0.0:5000）
"""
import os
import json
import re
import traceback
import time
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from queue import Queue
from threading import Lock

import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import RequestEntityTooLarge
import pymysql

from config import DB_CONFIG, DB_POOL_SIZE, DB_PING_BEFORE_QUERY, DEEPSEEK_API_KEY, DEEPSEEK_API_URL, DEEPSEEK_MODEL, DEEPSEEK_SELECTION_API_KEY

# 品类分类规则（供「品类营销数据」返回各品类的命中关键词）
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), 'tools', 'category_mapper'))
    import mapper as _category_mapper
except Exception:
    _category_mapper = None

app = Flask(__name__)
CORS(app)

# 限制单个请求体大小：违规词检测会把多张图片 base64 一次性上传，
# 超过上限返回 413，由前端分批请求，避免超大请求被静默丢弃或超时。
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024  # 64 MB


@app.errorhandler(RequestEntityTooLarge)
def _handle_413(e):
    return jsonify({'error': '请求体过大，请减少单次上传图片数量', 'success': False, 'results': []}), 413


# ======================== 数据库连接池 ========================

class DBConnectionPool:
    """简易 MySQL 连接池，支持自动重连"""

    def __init__(self, pool_size=5):
        self._pool = Queue(maxsize=pool_size)
        self._lock = Lock()
        self._size = pool_size
        self._count = 0

    def _create_conn(self):
        """创建一个新连接，并设置自动重连"""
        conn = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        # ping 检测连接是否存活
        conn.ping()
        return conn

    def get(self):
        """从池中获取一个可用连接"""
        try:
            # 尝试从队列中取一个空闲连接
            conn = self._pool.get(block=False)
            # 检测连接是否还活着
            if DB_PING_BEFORE_QUERY:
                try:
                    conn.ping()
                except Exception:
                    # 连接断开，创建新的
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = self._create_conn()
            return conn
        except Exception:
            # 队列为空或取连接失败，创建新连接
            with self._lock:
                if self._count < self._size:
                    self._count += 1
                    return self._create_conn()
            # 池已满，阻塞等待
            conn = self._pool.get(block=True, timeout=5)
            if DB_PING_BEFORE_QUERY:
                try:
                    conn.ping()
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = self._create_conn()
            return conn

    def put(self, conn):
        """归还连接到池中"""
        if conn:
            try:
                self._pool.put_nowait(conn)
            except Exception:
                # 池已满，关闭这个连接
                try:
                    conn.close()
                except Exception:
                    pass

    def close_all(self):
        """关闭池中所有连接"""
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Exception:
                pass


# 全局连接池
pool = DBConnectionPool(pool_size=DB_POOL_SIZE)


def get_db():
    """从连接池获取数据库连接"""
    return pool.get()


def return_db(conn):
    """归还连接到连接池"""
    pool.put(conn)


def db_execute(sql, params=None, fetch=True, max_retries=2):
    """执行 SQL。fetch=True 返回查询结果，否则返回受影响行数。支持自动重试。"""
    last_error = None
    for attempt in range(max_retries + 1):
        conn = None
        try:
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch:
                    result = cur.fetchall()
                    return_db(conn)
                    return result
                conn.commit()
                rows = cur.rowcount
                return_db(conn)
                return rows
        except pymysql.err.OperationalError as e:
            # 连接级别错误 → 丢弃连接，重试
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            last_error = e
            if attempt < max_retries:
                time.sleep(0.3 * (attempt + 1))  # 递增等待
                continue
            raise
        except Exception:
            if conn:
                return_db(conn)
            traceback.print_exc()
            raise
    raise last_error


def db_execute_insert(sql, params=None, max_retries=2):
    """执行 INSERT，返回新插入的 id。支持自动重试。"""
    last_error = None
    for attempt in range(max_retries + 1):
        conn = None
        try:
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                conn.commit()
                last_id = cur.lastrowid
                return_db(conn)
                return last_id
        except pymysql.err.OperationalError as e:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            last_error = e
            if attempt < max_retries:
                time.sleep(0.3 * (attempt + 1))
                continue
            raise
        except Exception:
            if conn:
                return_db(conn)
            traceback.print_exc()
            raise
    raise last_error


# ======================== DeepSeek AI 分析引擎 ========================

def call_deepseek_api(system_prompt, user_message, temperature=0.3, max_tokens=4096, api_key=None):
    """调用 DeepSeek API 生成分析报告，返回文本内容；失败返回 None。api_key 可指定专用 key，默认用全局 key。"""
    headers = {
        'Authorization': f'Bearer {api_key or DEEPSEEK_API_KEY}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': DEEPSEEK_MODEL,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_message},
        ],
        'temperature': temperature,
        'max_tokens': max_tokens,
        'stream': False,
    }
    try:
        resp = requests.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=120)
        if resp.status_code == 200:
            result = resp.json()
            return result['choices'][0]['message']['content']
        else:
            print(f'[DeepSeek] API error {resp.status_code}: {resp.text[:300]}')
            return None
    except Exception as e:
        print(f'[DeepSeek] 请求异常: {e}')
        return None


def _clean_ai_html(raw):
    """清洗 DeepSeek 返回：去掉开头的说明文字与 markdown 代码围栏，只保留 HTML 片段"""
    if not raw:
        return raw
    s = raw.strip()
    # 去掉开头的说明文字 / ```html 围栏：从第一个 '<' 开始
    i = s.find('<')
    if i > 0:
        s = s[i:]
    # 去掉结尾的 ``` 围栏及多余说明：截到最后一个 '>' 结束
    j = s.rfind('>')
    if j != -1 and j + 1 < len(s):
        s = s[:j + 1]
    return s.strip()


def _escape_html(s):
    """最小化 HTML 转义，防止 AI 文本中的特殊字符破坏页面"""
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _parse_json(raw):
    """从 AI 返回中提取 JSON 对象（容忍 markdown 围栏、前后说明文字、尾逗号）"""
    if not raw:
        return {}
    s = raw.strip()
    if s.startswith('```'):
        nl = s.find('\n')
        s = s[nl + 1:] if nl != -1 else ''
    if s.rstrip().endswith('```'):
        s = s.rstrip()[:-3]
    s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    i = s.find('{')
    j = s.rfind('}')
    if i != -1 and j > i:
        frag = s[i:j + 1]
        try:
            return json.loads(frag)
        except Exception:
            try:
                return json.loads(re.sub(r',\s*([}\]])', r'\1', frag))
            except Exception:
                return {}
    return {}


_fallback_kw_cache = {}


def _category_keywords(category):
    """品类关键词：优先用 RULES 规则词；无则从该品类实际商品标题提取（DeepSeek，带内存缓存）。"""
    if _category_mapper:
        kws = _category_mapper.RULES.get(category)
        if kws:
            return kws
    cached = _fallback_kw_cache.get(category)
    if cached is not None:
        return cached
    titles = []
    try:
        rows = db_execute("SELECT `商品名称快照` FROM `商品品类映射表` WHERE `统一品类`=%s", [category])
        titles = [r['商品名称快照'] for r in rows if r.get('商品名称快照')]
    except Exception:
        titles = []
    kws = _extract_keywords(category, titles)
    _fallback_kw_cache[category] = kws
    return kws


def _extract_keywords(category, titles):
    """从商品标题提取关键词；LLM 不可用时退回用标题前段"""
    if not titles:
        return []
    sample = '\n'.join(titles[:20])
    sys_p = '你是电商商品关键词提取助手。从商品标题中提取最能代表这一类商品的 2-5 个简短关键词（每个 2-6 字）。'
    user = f'品类「{category}」的商品标题：\n{sample}\n\n只输出一个 JSON 对象：{{"keywords":["关键词1","关键词2"]}}'
    raw = call_deepseek_api(sys_p, user, max_tokens=300)
    parsed = _parse_json(raw)
    if isinstance(parsed, dict) and isinstance(parsed.get('keywords'), list):
        kws = [str(k).strip() for k in parsed['keywords'] if str(k).strip()]
        if kws:
            return kws
    # 退回：取标题前段
    return [t[:10] for t in titles[:3]]


def _gather_link_data(target_date):
    """收集抖店/京东/千牛单链接数据（按日期聚合）"""
    links = {}

    # 抖店：含投放消耗/佣金/补贴等成本字段
    try:
        r = db_execute("""
            SELECT
                COALESCE(SUM(成交金额), 0)        AS payment,
                COALESCE(SUM(净成交金额), 0)      AS net_payment,
                COALESCE(SUM(成交订单数), 0)      AS orders,
                COALESCE(SUM(成交人数), 0)        AS buyers,
                COALESCE(SUM(成交件数), 0)        AS items,
                COALESCE(SUM(成交退款金额), 0)    AS refund,
                COALESCE(AVG(成交客单价), 0)      AS aov,
                COALESCE(SUM(实际佣金支出), 0)    AS commission,
                COALESCE(SUM(`投放消耗（店铺被投）`), 0) AS ad_store,
                COALESCE(SUM(`投放消耗（推商品）`), 0)   AS ad_product,
                COALESCE(SUM(投放贡献成交金额), 0) AS ad_gmv,
                COALESCE(SUM(平台消费券补贴金额), 0) AS subsidy,
                COALESCE(SUM(商品曝光人数), 0)    AS exposure_people,
                COALESCE(SUM(商品曝光次数), 0)    AS exposure_times,
                COALESCE(SUM(商品点击次数), 0)    AS click_times
            FROM 抖店单链接数据表
            WHERE 统计周期 = %s
        """, [target_date])[0]
        links['抖店'] = {
            'payment': float(r['payment'] or 0),
            'netPayment': float(r['net_payment'] or 0),
            'orders': int(r['orders'] or 0),
            'buyers': int(r['buyers'] or 0),
            'items': int(r['items'] or 0),
            'refund': float(r['refund'] or 0),
            'aov': float(r['aov'] or 0),
            'commission': float(r['commission'] or 0),
            'adSpend': float(r['ad_store'] or 0) + float(r['ad_product'] or 0),
            'adGmv': float(r['ad_gmv'] or 0),
            'subsidy': float(r['subsidy'] or 0),
            'visitors': int(r['exposure_people'] or 0),
            'views': int(r['exposure_times'] or 0),
            'clicks': int(r['click_times'] or 0),
            'hasSpend': True,
        }
    except Exception as e:
        links['抖店'] = {'error': str(e), 'hasSpend': True, 'payment': 0}

    # 京东：无推广花费/消耗字段
    try:
        r = db_execute("""
            SELECT
                COALESCE(SUM(成交金额), 0)            AS payment,
                COALESCE(SUM(成交单量), 0)            AS orders,
                COALESCE(SUM(成交客户数), 0)          AS buyers,
                COALESCE(SUM(成交商品件数), 0)        AS items,
                COALESCE(SUM(取消及售后退款金额), 0)  AS refund,
                COALESCE(AVG(客单价), 0)              AS aov,
                COALESCE(SUM(下单金额), 0)            AS order_amount,
                COALESCE(SUM(加购金额), 0)            AS cart_amount,
                COALESCE(SUM(商品访客数), 0)          AS visitors,
                COALESCE(SUM(商品浏览量), 0)          AS views,
                COALESCE(SUM(商品曝光次数), 0)        AS exposure_times,
                COALESCE(SUM(商品曝光人数), 0)        AS exposure_people
            FROM 京东单链接数据表
            WHERE 时间 = %s
        """, [target_date])[0]
        links['京东'] = {
            'payment': float(r['payment'] or 0),
            'orders': int(r['orders'] or 0),
            'buyers': int(r['buyers'] or 0),
            'items': int(r['items'] or 0),
            'refund': float(r['refund'] or 0),
            'aov': float(r['aov'] or 0),
            'orderAmount': float(r['order_amount'] or 0),
            'cartAmount': float(r['cart_amount'] or 0),
            'visitors': int(r['visitors'] or 0),
            'views': int(r['views'] or 0),
            'exposureTimes': int(r['exposure_times'] or 0),
            'exposurePeople': int(r['exposure_people'] or 0),
            'hasSpend': False,
        }
    except Exception as e:
        links['京东'] = {'error': str(e), 'hasSpend': False, 'payment': 0}

    # 千牛：无投放消耗，含聚划算促销金额
    try:
        r = db_execute("""
            SELECT
                COALESCE(SUM(支付金额), 0)        AS payment,
                COALESCE(SUM(支付件数), 0)        AS orders,
                COALESCE(SUM(支付买家数), 0)      AS buyers,
                COALESCE(SUM(下单金额), 0)        AS order_amount,
                COALESCE(SUM(下单件数), 0)        AS order_items,
                COALESCE(SUM(成功退款金额), 0)    AS refund,
                COALESCE(SUM(聚划算支付金额), 0)  AS promo_amount,
                COALESCE(SUM(商品访客数), 0)      AS visitors,
                COALESCE(SUM(商品浏览量), 0)      AS views,
                COALESCE(AVG(访客平均价值), 0)    AS visitor_value
            FROM 千牛单链接数据表
            WHERE 统计日期 = %s
        """, [target_date])[0]
        links['千牛'] = {
            'payment': float(r['payment'] or 0),
            'orders': int(r['orders'] or 0),
            'buyers': int(r['buyers'] or 0),
            'orderAmount': float(r['order_amount'] or 0),
            'orderItems': int(r['order_items'] or 0),
            'refund': float(r['refund'] or 0),
            'promoAmount': float(r['promo_amount'] or 0),
            'visitors': int(r['visitors'] or 0),
            'views': int(r['views'] or 0),
            'visitorValue': float(r['visitor_value'] or 0),
            'hasSpend': False,
        }
    except Exception as e:
        links['千牛'] = {'error': str(e), 'hasSpend': False, 'payment': 0}

    return links


def _analyze_links(links):
    """计算单链接数据「推广花费/消耗 vs 产出」比例，并标记问题"""
    link_issues = []
    for plat, lk in links.items():
        if lk.get('error'):
            link_issues.append({'platform': plat, 'dim': '数据获取', 'value': '-', 'level': 'warning',
                                'msg': f'数据读取异常：{lk["error"]}'})
            continue
        payment = lk.get('payment', 0) or 0
        refund = lk.get('refund', 0) or 0
        commission = lk.get('commission', 0) or 0
        subsidy = lk.get('subsidy', 0) or 0
        ad_spend = lk.get('adSpend', 0) or 0
        ad_gmv = lk.get('adGmv', 0) or 0
        total_cost = ad_spend + commission + subsidy

        lk['totalCost'] = total_cost
        lk['refundRate'] = round(refund / payment * 100, 2) if payment > 0 else 0

        # 综合消耗产出比（产出 / 综合消耗）
        if total_cost > 0:
            ratio = payment / total_cost
            lk['costOutputRatio'] = round(ratio, 2)
            if ratio < 1:
                link_issues.append({'platform': plat, 'dim': '综合消耗产出比', 'value': f'{ratio:.2f}', 'level': 'danger',
                                    'msg': f'综合消耗 {total_cost:,.2f} 元已超过产出 {payment:,.2f} 元，整体亏损'})
            elif ratio < 2:
                link_issues.append({'platform': plat, 'dim': '综合消耗产出比', 'value': f'{ratio:.2f}', 'level': 'warning',
                                    'msg': '综合消耗产出比偏低，建议压缩佣金/投放/补贴成本'})
        else:
            lk['costOutputRatio'] = None

        # 投放产出比（仅抖店有投放消耗字段）
        if lk.get('hasSpend') and ad_spend > 0:
            if ad_gmv > 0:
                ad_roi = ad_gmv / ad_spend
                lk['adRoi'] = round(ad_roi, 2)
                if ad_roi < 1:
                    link_issues.append({'platform': plat, 'dim': '投放产出比', 'value': f'{ad_roi:.2f}', 'level': 'danger',
                                        'msg': f'投放消耗 {ad_spend:,.2f} 元 > 投放贡献成交 {ad_gmv:,.2f} 元，投放亏损'})
            else:
                lk['adRoi'] = 0
                link_issues.append({'platform': plat, 'dim': '投放产出比', 'value': '0', 'level': 'danger',
                                    'msg': f'投放消耗 {ad_spend:,.2f} 元但投放贡献成交为 0，投放完全无效'})
        else:
            lk['adRoi'] = None

        # 退款率
        if lk['refundRate'] > 20:
            link_issues.append({'platform': plat, 'dim': '退款率', 'value': f"{lk['refundRate']:.2f}%", 'level': 'warning',
                                'msg': '退款率偏高'})

    return link_issues


def _gather_qianniu_promo(target_date):
    """收集千牛单链接推广数据（推广消耗 vs 产出）"""
    try:
        r = db_execute("""
            SELECT
                COALESCE(SUM(推广消耗), 0)        AS spend,
                COALESCE(SUM(直接引导成交金额), 0) AS direct_gmv,
                COALESCE(SUM(总引导成交金额), 0)   AS total_gmv,
                COALESCE(SUM(直接成交笔数), 0)     AS direct_orders,
                COALESCE(SUM(总引导成交笔数), 0)   AS total_orders,
                COALESCE(SUM(展现量), 0)           AS impressions,
                COALESCE(SUM(点击量), 0)           AS clicks,
                COALESCE(AVG(单次点击成本), 0)     AS cpc,
                COUNT(DISTINCT 商品ID)             AS products,
                COUNT(DISTINCT 店铺名)             AS stores
            FROM 千牛单链接推广数据表
            WHERE 统计日期 = %s
        """, [target_date])[0]
        spend = float(r['spend'] or 0)
        direct_gmv = float(r['direct_gmv'] or 0)
        total_gmv = float(r['total_gmv'] or 0)
        impressions = int(r['impressions'] or 0)
        clicks = int(r['clicks'] or 0)
        return {
            'spend': spend,
            'directGmv': direct_gmv,
            'totalGmv': total_gmv,
            'directOrders': int(r['direct_orders'] or 0),
            'totalOrders': int(r['total_orders'] or 0),
            'impressions': impressions,
            'clicks': clicks,
            'cpc': float(r['cpc'] or 0),
            'products': int(r['products'] or 0),
            'stores': int(r['stores'] or 0),
            'directRoi': round(direct_gmv / spend, 2) if spend > 0 else 0,
            'totalRoi': round(total_gmv / spend, 2) if spend > 0 else 0,
            'ctr': round(clicks / impressions * 100, 2) if impressions > 0 else 0,
        }
    except Exception as e:
        return {'error': str(e)}


def _analyze_qianniu_promo(promo):
    """计算千牛推广数据的 ROI 并标记问题"""
    issues = []
    if promo.get('error'):
        issues.append({'platform': '千牛推广', 'dim': '数据获取', 'value': '-', 'level': 'warning',
                       'msg': f'数据读取异常：{promo["error"]}'})
        return issues
    spend = promo.get('spend', 0) or 0
    if spend > 0:
        if promo['totalRoi'] < 1:
            issues.append({'platform': '千牛推广', 'dim': '总ROI', 'value': f"{promo['totalRoi']:.2f}", 'level': 'danger',
                           'msg': f"推广消耗 {spend:,.2f} 元 > 总引导成交 {promo['totalGmv']:,.2f} 元，推广整体亏损"})
        elif promo['totalRoi'] < 2:
            issues.append({'platform': '千牛推广', 'dim': '总ROI', 'value': f"{promo['totalRoi']:.2f}", 'level': 'warning',
                           'msg': '推广总ROI偏低，产出接近消耗，需优化投放'})
        if promo['directRoi'] < 1:
            issues.append({'platform': '千牛推广', 'dim': '直接ROI', 'value': f"{promo['directRoi']:.2f}", 'level': 'danger',
                           'msg': f"直接引导成交 {promo['directGmv']:,.2f} 元低于推广消耗 {spend:,.2f} 元"})
    return issues


def gather_daily_data(target_date):
    """收集指定日期的全维度数据：店铺营销数据 + 抖店/京东/千牛单链接数据"""
    row = db_execute("""
        SELECT
            COALESCE(SUM(净支付金额), 0) AS net_payment,
            COALESCE(SUM(支付金额), 0)   AS payment,
            COALESCE(SUM(退款金额), 0)   AS refund_amount,
            COALESCE(SUM(推广花费), 0) AS ad_spend,
            COALESCE(SUM(推广总成交), 0)   AS ad_total,
            COALESCE(SUM(访客数), 0)      AS visitors,
            COALESCE(SUM(支付买家数), 0)   AS payers,
            0 AS cart,
            COALESCE(AVG(支付转化率), 0)   AS conv_rate,
            COALESCE(AVG(订单退款率), 0)       AS order_refund_rate,
            COALESCE(AVG(客单价), 0)       AS aov
        FROM 店铺营销数据
        WHERE 日期 = %s
    """, [target_date])[0]

    # 分平台数据
    platform_rows = db_execute("""
        SELECT
            平台,
            COALESCE(SUM(净支付金额), 0) AS net_payment,
            COALESCE(SUM(退款金额), 0)   AS refund_amount,
            COALESCE(SUM(推广花费), 0) AS ad_spend,
            COALESCE(SUM(推广总成交), 0)   AS ad_total,
            COALESCE(SUM(访客数), 0)      AS visitors,
            COALESCE(SUM(支付买家数), 0)   AS payers
        FROM 店铺营销数据
        WHERE 日期 = %s
        GROUP BY 平台
        ORDER BY net_payment DESC
    """, [target_date])

    # 分品牌数据 (TOP10)
    brand_rows = db_execute("""
        SELECT
            品牌,
            COALESCE(SUM(净支付金额), 0) AS net_payment,
            COALESCE(SUM(访客数), 0)      AS visitors
        FROM 店铺营销数据
        WHERE 日期 = %s
        GROUP BY 品牌
        ORDER BY net_payment DESC
        LIMIT 10
    """, [target_date])

    # 分店铺数据 (TOP15，包含退款/推广/转化核心指标)
    store_rows = db_execute("""
        SELECT
            平台,
            店铺名,
            COALESCE(SUM(净支付金额), 0) AS net_payment,
            COALESCE(SUM(退款金额), 0)   AS refund_amount,
            COALESCE(SUM(推广花费), 0) AS ad_spend,
            COALESCE(SUM(推广总成交), 0)   AS ad_total,
            COALESCE(SUM(访客数), 0)      AS visitors,
            COALESCE(SUM(支付买家数), 0)   AS payers,
            COALESCE(AVG(支付转化率), 0)   AS conv_rate,
            COALESCE(AVG(订单退款率), 0)       AS refund_rate,
            COALESCE(AVG(客单价), 0)       AS aov
        FROM 店铺营销数据
        WHERE 日期 = %s
        GROUP BY 平台, 店铺名
        ORDER BY net_payment DESC
        LIMIT 15
    """, [target_date])

    # 单链接数据（抖店/京东/千牛）+ 消耗产出比分析
    links = _gather_link_data(target_date)
    link_issues = _analyze_links(links)

    # 千牛单链接推广数据（推广消耗 vs 产出）
    qianniu_promo = _gather_qianniu_promo(target_date)
    promo_issues = _analyze_qianniu_promo(qianniu_promo)

    # 品类维度汇总（复用品类营销聚合逻辑）
    try:
        category = _gather_category_data(str(target_date), str(target_date))
    except Exception as e:
        category = {'totals': {}, 'categories': [], 'error': str(e)}

    return {
        'date': str(target_date),
        'summary': {
            'netPayment': float(row['net_payment']),
            'payment': float(row['payment']),
            'refundAmount': float(row['refund_amount']),
            'adSpend': float(row['ad_spend']),
            'adTotal': float(row['ad_total']),
            'visitors': int(row['visitors']),
            'payers': int(row['payers']),
            'cart': int(row['cart']),
            'convRate': round(float(row['conv_rate']) * 100, 2),
            'orderRefundRate': round(float(row['order_refund_rate']) * 100, 2),
            'aov': round(float(row['aov']), 2),
        },
        'byPlatform': [{
            'name': r['平台'],
            'netPayment': float(r['net_payment']),
            'refundAmount': float(r['refund_amount']),
            'adSpend': float(r['ad_spend']),
            'adTotal': float(r['ad_total']),
            'visitors': int(r['visitors']),
            'payers': int(r['payers']),
        } for r in platform_rows],
        'byBrand': [{
            'name': r['品牌'],
            'netPayment': float(r['net_payment']),
            'visitors': int(r['visitors']),
        } for r in brand_rows],
        'byStore': [{
            'platform': r['平台'],
            'name': r['店铺名'],
            'netPayment': float(r['net_payment']),
            'refundAmount': float(r['refund_amount']),
            'adSpend': float(r['ad_spend']),
            'adTotal': float(r['ad_total']),
            'visitors': int(r['visitors']),
            'payers': int(r['payers']),
            'convRate': round(float(r['conv_rate']) * 100, 2),
            'refundRate': round(float(r['refund_rate']) * 100, 2),
            'aov': round(float(r['aov']), 2),
        } for r in store_rows],
        'links': links,
        'linkIssues': link_issues,
        'qianniuPromo': qianniu_promo,
        'promoIssues': promo_issues,
        'category': category,
    }


def _build_analysis_context(target_date):
    """把 gather_daily_data 结果转成中文 key 的知识库上下文，供每日数据分析智能体使用"""
    data = gather_daily_data(target_date)
    sm = data['summary']
    return {
        '日期': str(target_date),
        '营销综合': {
            '净支付金额': sm['netPayment'],
            '支付金额': sm['payment'],
            '退款金额': sm['refundAmount'],
            '推广花费': sm['adSpend'],
            '推广总成交': sm['adTotal'],
            '访客数': sm['visitors'],
            '支付买家数': sm['payers'],
            '支付转化率(%)': sm['convRate'],
            '订单退款率(%)': sm['orderRefundRate'],
            '客单价': sm['aov'],
        },
        '分平台': [
            {'平台': p['name'], '净支付金额': p['netPayment'], '退款金额': p['refundAmount'],
             '推广花费': p['adSpend'], '推广总成交': p['adTotal'], '访客数': p['visitors'], '支付买家数': p['payers']}
            for p in data['byPlatform']
        ],
        '分品牌': [
            {'品牌': b['name'], '净支付金额': b['netPayment'], '访客数': b['visitors']}
            for b in data['byBrand']
        ],
        '分店铺': [
            {'平台': s['platform'], '店铺名': s['name'], '净支付金额': s['netPayment'], '退款金额': s['refundAmount'],
             '推广花费': s['adSpend'], '推广总成交': s['adTotal'], '访客数': s['visitors'], '支付买家数': s['payers'],
             '支付转化率(%)': s['convRate'], '退款率(%)': s['refundRate'], '客单价': s['aov']}
            for s in data['byStore']
        ],
        '单链接数据': data['links'],
        '单链接预警': data['linkIssues'],
        '千牛推广数据': data['qianniuPromo'],
        '千牛推广预警': data['promoIssues'],
        '品类数据': data['category'],
    }


def _build_report_html(data, refund_rate, roi, conv, aov, warnings, insights=None, ai_available=False):
    """固定 HTML 骨架 + AI 洞察填空：框架与数据表格由代码渲染，AI 只提供各章节分析文字"""
    insights = insights or {}
    sm = data['summary']

    def insight(text):
        if not text:
            return ''
        t = _escape_html(text).replace('\n', '<br>')
        return (f'<div style="margin-top:12px;padding:12px 16px;background:#f0f9ff;'
                f'border-left:3px solid #1677ff;border-radius:4px;font-size:0.9rem;'
                f'color:#334155;line-height:1.7">{t}</div>')
    platforms_html = '<table class="da-table"><thead><tr><th>平台</th><th>净支付</th><th>退款</th><th>花费</th><th>ROI</th></tr></thead><tbody>'
    for p in data['byPlatform']:
        p_roi = round(p['adTotal'] / p['adSpend'], 2) if p['adSpend'] > 0 else 0
        platforms_html += f"<tr><td>{p['name']}</td><td>&yen;{p['netPayment']:,.2f}</td><td>&yen;{p['refundAmount']:,.2f}</td><td>&yen;{p['adSpend']:,.2f}</td><td>{p_roi:.2f}</td></tr>"
    platforms_html += '</tbody></table>'

    warn_html = ''.join([f'<li class="da-warn-{w["level"]}">{w["dim"]}：{w["value"]}</li>' for w in warnings]) if warnings else '<li style="color:#16a34a">未发现显著异常指标</li>'

    # 店铺表格
    stores_html = '<table class="da-table"><thead><tr><th>平台</th><th>店铺</th><th>净支付</th><th>退款率</th><th>转化率</th><th>客单价</th></tr></thead><tbody>'
    for s in data.get('byStore', []):
        s_warn = ' style="color:#e11d48;font-weight:600"' if s['refundRate'] > 20 or s['convRate'] < 1 else ''
        stores_html += f"<tr{s_warn}><td>{s['platform']}</td><td>{s['name']}</td><td>&yen;{s['netPayment']:,.2f}</td><td>{s['refundRate']:.2f}%</td><td>{s['convRate']:.2f}%</td><td>&yen;{s['aov']:.2f}</td></tr>"
    stores_html += '</tbody></table>'

    # 单链接数据表格（含推广花费/消耗与产出比）
    link_rows = ''
    for plat, lk in data.get('links', {}).items():
        if lk.get('error'):
            link_rows += f'<tr><td>{plat}</td><td colspan="7">数据读取异常：{lk["error"]}</td></tr>'
            continue
        rr = lk.get('refundRate', 0)
        rr_cls = ' style="color:#e11d48;font-weight:600"' if rr > 20 else ''
        if lk.get('hasSpend'):
            ad_roi = lk.get('adRoi')
            cor = lk.get('costOutputRatio')
            ad_roi_txt = f'{ad_roi:.2f}' if ad_roi is not None else '-'
            cor_txt = f'{cor:.2f}' if cor is not None else '-'
            ad_roi_cls = ' style="color:#e11d48;font-weight:600"' if (ad_roi is not None and ad_roi < 1) else ''
            cor_cls = ' style="color:#e11d48;font-weight:600"' if (cor is not None and cor < 1) else ''
            link_rows += (f'<tr><td>{plat}</td><td>&yen;{lk["payment"]:,.2f}</td><td>{lk["orders"]:,}</td>'
                          f'<td>{lk["buyers"]:,}</td><td{rr_cls}>{rr:.2f}%</td>'
                          f'<td>&yen;{lk.get("totalCost", 0):,.2f}</td><td{ad_roi_cls}>{ad_roi_txt}</td><td{cor_cls}>{cor_txt}</td></tr>')
        else:
            link_rows += (f'<tr><td>{plat}</td><td>&yen;{lk["payment"]:,.2f}</td><td>{lk["orders"]:,}</td>'
                          f'<td>{lk["buyers"]:,}</td><td{rr_cls}>{rr:.2f}%</td>'
                          f'<td style="color:#94a3b8">无消耗字段</td><td>-</td><td>-</td></tr>')
    links_html = ('<table class="da-table"><thead><tr><th>平台</th><th>成交/支付</th><th>订单</th><th>买家</th><th>退款率</th><th>综合消耗</th><th>投放产出比</th><th>综合消耗产出比</th></tr></thead><tbody>'
                  + link_rows + '</tbody></table>')

    # 单链接消耗产出比预警
    link_warn_html = ''.join([f'<li class="da-warn-{w["level"]}">[{w["platform"]}] {w["dim"]}：{w["value"]} —— {w["msg"]}</li>'
                              for w in data.get('linkIssues', [])])

    # 千牛推广预警
    promo_warn_html = ''.join([f'<li class="da-warn-{w["level"]}">[{w["platform"]}] {w["dim"]}：{w["value"]} —— {w["msg"]}</li>'
                               for w in data.get('promoIssues', [])])

    # 千牛单链接推广数据
    promo = data.get('qianniuPromo', {})
    if promo.get('error'):
        promo_html = f'<p style="color:#e11d48">千牛推广数据读取异常：{promo["error"]}</p>'
    else:
        promo_roi_cls = ' style="color:#e11d48;font-weight:600"' if promo.get('totalRoi', 0) < 1 else ''
        promo_html = (f'<table class="da-table"><thead><tr><th>推广消耗</th><th>直接引导成交</th><th>直接ROI</th><th>总引导成交</th><th>总ROI</th><th>点击率</th><th>单次点击成本</th><th>推广商品数</th></tr></thead><tbody>'
                      f'<tr><td>&yen;{promo.get("spend", 0):,.2f}</td><td>&yen;{promo.get("directGmv", 0):,.2f}</td><td>{promo.get("directRoi", 0):.2f}</td>'
                      f'<td>&yen;{promo.get("totalGmv", 0):,.2f}</td><td{promo_roi_cls}>{promo.get("totalRoi", 0):.2f}</td>'
                      f'<td>{promo.get("ctr", 0):.2f}%</td><td>&yen;{promo.get("cpc", 0):.2f}</td><td>{promo.get("products", 0):,}</td></tr></tbody></table>')

    # 店铺问题自动检测
    store_warnings = []
    for s in data.get('byStore', []):
        issues = []
        if s['refundRate'] > 20: issues.append(f"退款率{s['refundRate']:.1f}%偏高")
        if s['convRate'] < 1: issues.append(f"转化率{s['convRate']:.2f}%极低")
        if s['netPayment'] == 0 and s['visitors'] > 0: issues.append("有流量无成交")
        if issues:
            store_warnings.append(f"<li class=\"da-warn-warning\">[{s['platform']}] {s['name']}：{'；'.join(issues)}</li>")

    # 品类分析
    cat = data.get('category', {})
    cat_totals = cat.get('totals', {})
    cat_list = cat.get('categories', [])
    if cat.get('error'):
        cat_html = f'<p style="color:#e11d48">品类数据读取异常：{cat["error"]}</p>'
    elif cat_list:
        cat_rows = ''
        for c in cat_list:
            rr_cls = ' style="color:#e11d48;font-weight:600"' if c['refundRate'] > 20 else ''
            roi_cls = ' style="color:#e11d48;font-weight:600"' if (c['spend'] > 0 and c['roi'] < 1) else ''
            kws = _category_keywords(c['category'])
            kw_span = ''
            if kws:
                kw_text = '、'.join(kws)
                kw_span = (f' <span title="{kw_text}" style="display:inline-block;max-width:150px;'
                           f'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'
                           f'vertical-align:bottom;color:#94a3b8;font-size:0.78rem;cursor:help">{kw_text}</span>')
            cat_rows += (f'<tr><td>{c["category"]}{kw_span}</td><td>&yen;{c["payment"]:,.2f}</td><td{rr_cls}>{c["refundRate"]:.2f}%</td>'
                         f'<td>&yen;{c["spend"]:,.2f}</td><td>&yen;{c["ad_gmv"]:,.2f}</td><td{roi_cls}>{c["roi"]:.2f}</td></tr>')
        cat_html = ('<table class="da-table"><thead><tr><th>品类</th><th>成交</th><th>退款率</th><th>消耗</th><th>产出</th><th>ROI</th></tr></thead><tbody>'
                    + cat_rows + '</tbody></table>')
    else:
        cat_html = '<p style="color:#94a3b8">暂无品类数据</p>'

    return f"""
<div class="analysis-section">
  <h3><i class="fa-solid fa-coins"></i> 营销数据综合分析</h3>
  <p>净支付金额 <span class="highlight">&yen;{sm['netPayment']:,.2f}</span>，退款金额 <span class="highlight">&yen;{sm['refundAmount']:,.2f}</span>，退款率 <span class="{'warn' if refund_rate > 20 else 'highlight'}">{refund_rate:.2f}%</span>。</p>
  <p>推广花费 <span class="highlight">&yen;{sm['adSpend']:,.2f}</span>，推广总成交 <span class="highlight">&yen;{sm['adTotal']:,.2f}</span>，ROI <span class="{'danger' if roi < 1.0 else 'highlight'}">{roi:.2f}</span>。</p>
  <p>访客数 <span class="highlight">{sm['visitors']:,}</span>，支付买家数 <span class="highlight">{sm['payers']:,}</span>，支付转化率 <span class="{'warn' if conv < 2 else 'highlight'}">{conv:.2f}%</span>，客单价 <span class="highlight">&yen;{aov:.2f}</span>。</p>
  {platforms_html}
  {insight(insights.get('营销综合'))}
</div>
<div class="analysis-section">
  <h3><i class="fa-solid fa-chart-simple"></i> 单链接数据综合分析</h3>
  <p>下表为抖店/京东/千牛单链接数据（按日期汇总）。<span class="highlight">综合消耗产出比 = 成交金额 ÷ (投放消耗+佣金+补贴)</span>，低于 1 表示投入产出倒挂、整体亏损。</p>
  {links_html}
  {insight(insights.get('单链接'))}
</div>
<div class="analysis-section">
  <h3><i class="fa-solid fa-bullhorn"></i> 千牛单链接推广数据分析</h3>
  <p>千牛推广数据：推广消耗与直接/总引导成交的产出比。<span class="highlight">总ROI = 总引导成交金额 ÷ 推广消耗</span>，低于 1 表示推广投入亏损。</p>
  {promo_html}
  {insight(insights.get('千牛推广'))}
</div>
<div class="analysis-section">
  <h3><i class="fa-solid fa-tags"></i> 品类分析</h3>
  <p>按统一品类汇总的成交、退款、消耗与产出比。<span class="highlight">ROI = 产出 ÷ 消耗</span>，低于 1 表示该品类投放亏损。</p>
  {cat_html}
  {insight(insights.get('品类'))}
</div>
<div class="analysis-section">
  <h3><i class="fa-solid fa-shop"></i> 店铺经营分析</h3>
  {stores_html}
  {"<ul class=\"da-warn-list\">" + ''.join(store_warnings) + "</ul>" if store_warnings else "<p style=\"color:#16a34a\">各店铺核心指标均处于正常范围。</p>"}
  {insight(insights.get('店铺'))}
</div>
<div class="analysis-section">
  <h3><i class="fa-solid fa-triangle-exclamation"></i> 异常预警与建议</h3>
  <ul class="da-warn-list">{warn_html}{link_warn_html}{promo_warn_html}</ul>
  {insight(insights.get('异常建议'))}
  {'' if ai_available else '<p style="margin-top:12px;color:#94a3b8;font-size:0.85rem">注：AI 分析服务暂不可用，以上为规则生成的报告。</p>'}
</div>
"""


# ======================== 统一响应格式 ========================

def success(data=None, msg='ok'):
    return jsonify({'code': 0, 'msg': msg, 'data': data})

def fail(msg='error', code=1):
    return jsonify({'code': code, 'msg': msg, 'data': None})


# ======================== 健康检查 ========================

@app.route('/api/health')
def health():
    """连接测试端点 — 前端用来判断后端是否可用"""
    conn = None
    try:
        conn = get_db()
        conn.ping()
        return_db(conn)
        return success({'db': 'connected', 'pool': 'active'}, '后端服务运行正常，数据库已连接')
    except Exception as e:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return fail(f'数据库连接失败: {str(e)}')


# ======================== 商品管理 ========================

@app.route('/api/products', methods=['GET'])
def list_products():
    """商品列表（支持 ?search=关键词 & category=分类）"""
    try:
        sql = 'SELECT * FROM products'
        params = []
        conditions = []

        search = request.args.get('search', '').strip()
        category = request.args.get('category', '').strip()

        if search:
            conditions.append('name LIKE %s')
            params.append(f'%{search}%')
        if category:
            conditions.append('category = %s')
            params.append(category)

        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)

        sql += ' ORDER BY id DESC'
        rows = db_execute(sql, params)
        # 转换 Decimal → float
        for r in rows:
            r['price'] = float(r['price'])
        return success(rows)
    except Exception as e:
        return fail(str(e))


@app.route('/api/products', methods=['POST'])
def create_product():
    """新增商品"""
    try:
        data = request.get_json(force=True)
        sql = """INSERT INTO products (name, category, price, stock, status)
                 VALUES (%s, %s, %s, %s, %s)"""
        new_id = db_execute_insert(sql, (
            data.get('name', ''),
            data.get('category', ''),
            data.get('price', 0),
            data.get('stock', 0),
            data.get('status', '在售'),
        ))
        # 返回完整记录
        row = db_execute('SELECT * FROM products WHERE id = %s', [new_id])
        if row:
            row[0]['price'] = float(row[0]['price'])
        return success(row[0] if row else None, '商品添加成功')
    except Exception as e:
        return fail(str(e))


@app.route('/api/products/<int:pid>', methods=['PUT'])
def update_product(pid):
    """更新商品"""
    try:
        data = request.get_json(force=True)
        sql = """UPDATE products SET name=%s, category=%s, price=%s,
                 stock=%s, status=%s WHERE id=%s"""
        db_execute(sql, (
            data.get('name', ''),
            data.get('category', ''),
            data.get('price', 0),
            data.get('stock', 0),
            data.get('status', '在售'),
            pid,
        ), fetch=False)
        row = db_execute('SELECT * FROM products WHERE id = %s', [pid])
        if row:
            row[0]['price'] = float(row[0]['price'])
        return success(row[0] if row else None, '商品更新成功')
    except Exception as e:
        return fail(str(e))


@app.route('/api/products/<int:pid>', methods=['DELETE'])
def delete_product(pid):
    """删除商品"""
    try:
        db_execute('DELETE FROM products WHERE id = %s', [pid], fetch=False)
        return success(None, '商品已删除')
    except Exception as e:
        return fail(str(e))


# ======================== 订单管理 ========================

@app.route('/api/orders', methods=['GET'])
def list_orders():
    """订单列表（支持 ?search=关键词 & status=状态）"""
    try:
        sql = 'SELECT * FROM orders'
        params = []
        conditions = []

        search = request.args.get('search', '').strip()
        status = request.args.get('status', '').strip()

        if search:
            conditions.append('(id LIKE %s OR customer LIKE %s)')
            params.extend([f'%{search}%', f'%{search}%'])
        if status:
            conditions.append('status = %s')
            params.append(status)

        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)

        sql += ' ORDER BY date DESC, id DESC'
        rows = db_execute(sql, params)
        for r in rows:
            r['amount'] = float(r['amount'])
            r['date'] = str(r['date']) if isinstance(r['date'], date) else r['date']
        return success(rows)
    except Exception as e:
        return fail(str(e))


@app.route('/api/orders', methods=['POST'])
def create_order():
    """新增订单（自动生成订单号 ORD + 年月日 + 序号）"""
    try:
        data = request.get_json(force=True)
        today = date.today()
        prefix = f'ORD{today.strftime("%Y%m%d")}'

        # 查询当天已有最大序号
        rows = db_execute(
            'SELECT id FROM orders WHERE id LIKE %s ORDER BY id DESC LIMIT 1',
            [f'{prefix}%']
        )
        if rows:
            last_num = int(rows[0]['id'][-2:])
            seq = f'{last_num + 1:02d}'
        else:
            seq = '01'

        order_id = prefix + seq
        order_date = data.get('date') or str(today)

        sql = """INSERT INTO orders (id, customer, product, qty, amount, status, date)
                 VALUES (%s, %s, %s, %s, %s, %s, %s)"""
        db_execute(sql, (
            order_id,
            data.get('customer', ''),
            data.get('product', ''),
            data.get('qty', 1),
            data.get('amount', 0),
            data.get('status', '待发货'),
            order_date,
        ), fetch=False)

        row = db_execute('SELECT * FROM orders WHERE id = %s', [order_id])
        if row:
            row[0]['amount'] = float(row[0]['amount'])
            row[0]['date'] = str(row[0]['date']) if isinstance(row[0]['date'], date) else row[0]['date']
        return success(row[0] if row else None, '订单添加成功')
    except Exception as e:
        return fail(str(e))


@app.route('/api/orders/<string:oid>', methods=['PUT'])
def update_order(oid):
    """更新订单"""
    try:
        data = request.get_json(force=True)
        sql = """UPDATE orders SET customer=%s, product=%s, qty=%s,
                 amount=%s, status=%s, date=%s WHERE id=%s"""
        db_execute(sql, (
            data.get('customer', ''),
            data.get('product', ''),
            data.get('qty', 1),
            data.get('amount', 0),
            data.get('status', '待发货'),
            data.get('date', ''),
            oid,
        ), fetch=False)
        row = db_execute('SELECT * FROM orders WHERE id = %s', [oid])
        if row:
            row[0]['amount'] = float(row[0]['amount'])
            row[0]['date'] = str(row[0]['date']) if isinstance(row[0]['date'], date) else row[0]['date']
        return success(row[0] if row else None, '订单更新成功')
    except Exception as e:
        return fail(str(e))


@app.route('/api/orders/<string:oid>', methods=['DELETE'])
def delete_order(oid):
    """删除订单"""
    try:
        db_execute('DELETE FROM orders WHERE id = %s', [oid], fetch=False)
        return success(None, '订单已删除')
    except Exception as e:
        return fail(str(e))


# ======================== 客户管理 ========================

@app.route('/api/customers', methods=['GET'])
def list_customers():
    """客户列表（支持 ?search=关键词）"""
    try:
        sql = 'SELECT * FROM customers'
        params = []

        search = request.args.get('search', '').strip()
        if search:
            sql += ' WHERE name LIKE %s OR phone LIKE %s'
            params.extend([f'%{search}%', f'%{search}%'])

        sql += ' ORDER BY id DESC'
        rows = db_execute(sql, params)
        for r in rows:
            r['reg_date'] = str(r['reg_date']) if isinstance(r['reg_date'], date) else r['reg_date']
        return success(rows)
    except Exception as e:
        return fail(str(e))


@app.route('/api/customers', methods=['POST'])
def create_customer():
    """新增客户"""
    try:
        data = request.get_json(force=True)
        reg_date = data.get('regDate') or data.get('reg_date') or str(date.today())

        sql = """INSERT INTO customers (name, phone, email, address, reg_date)
                 VALUES (%s, %s, %s, %s, %s)"""
        new_id = db_execute_insert(sql, (
            data.get('name', ''),
            data.get('phone', ''),
            data.get('email', ''),
            data.get('address', ''),
            reg_date,
        ))
        row = db_execute('SELECT * FROM customers WHERE id = %s', [new_id])
        if row:
            r = row[0]
            r['reg_date'] = str(r['reg_date']) if isinstance(r['reg_date'], date) else r['reg_date']
        return success(row[0] if row else None, '客户添加成功')
    except Exception as e:
        return fail(str(e))


@app.route('/api/customers/<int:cid>', methods=['PUT'])
def update_customer(cid):
    """更新客户"""
    try:
        data = request.get_json(force=True)
        reg_date = data.get('regDate') or data.get('reg_date') or ''

        sql = """UPDATE customers SET name=%s, phone=%s, email=%s,
                 address=%s, reg_date=%s WHERE id=%s"""
        db_execute(sql, (
            data.get('name', ''),
            data.get('phone', ''),
            data.get('email', ''),
            data.get('address', ''),
            reg_date,
            cid,
        ), fetch=False)
        row = db_execute('SELECT * FROM customers WHERE id = %s', [cid])
        if row:
            r = row[0]
            r['reg_date'] = str(r['reg_date']) if isinstance(r['reg_date'], date) else r['reg_date']
        return success(row[0] if row else None, '客户更新成功')
    except Exception as e:
        return fail(str(e))


@app.route('/api/customers/<int:cid>', methods=['DELETE'])
def delete_customer(cid):
    """删除客户"""
    try:
        db_execute('DELETE FROM customers WHERE id = %s', [cid], fetch=False)
        return success(None, '客户已删除')
    except Exception as e:
        return fail(str(e))


# ======================== 数据看板统计 ========================

@app.route('/api/dashboard/stats', methods=['GET'])
def dashboard_stats():
    """数据看板首页 - 从店铺营销数据表获取真实统计"""
    try:
        today = date.today()

        # 找最近有数据的日期（今天优先，没有则取昨天）
        latest_date_row = db_execute(
            "SELECT MAX(日期) AS d FROM 店铺营销数据 WHERE 日期 <= %s", [today]
        )[0]
        latest_date = latest_date_row['d']
        if latest_date is None:
            return success({
                'netPayment': 0, 'payment': 0, 'refundAmount': 0, 'refundRate': 0,
                'adSpend': 0, 'adTotal': 0, 'roi': 0,
                'visitors': 0, 'payers': 0, 'netChange': None, 'trends': [],
                'latestDate': str(today),
            })

        prev_date = latest_date - timedelta(days=1)

        row_latest = db_execute("""
            SELECT
                COALESCE(SUM(净支付金额), 0) AS net_payment,
                COALESCE(SUM(退款金额), 0) AS refund_amount,
                COALESCE(SUM(推广花费), 0) AS ad_spend,
                COALESCE(SUM(访客数), 0) AS visitors,
                COALESCE(SUM(支付买家数), 0) AS payers,
                COALESCE(SUM(支付金额), 0) AS payment,
                COALESCE(SUM(推广总成交), 0) AS ad_total
            FROM 店铺营销数据
            WHERE 日期 = %s
        """, [latest_date])[0]

        row_prev = db_execute("""
            SELECT COALESCE(SUM(净支付金额), 0) AS net_payment
            FROM 店铺营销数据 WHERE 日期 = %s
        """, [prev_date])[0]

        latest_net   = float(row_latest['net_payment'])
        latest_refund = float(row_latest['refund_amount'])
        latest_ad    = float(row_latest['ad_spend'])
        visitors    = int(row_latest['visitors'])
        payers      = int(row_latest['payers'])
        payment     = float(row_latest['payment'])
        ad_total    = float(row_latest['ad_total'])
        prev_net    = float(row_prev['net_payment'])

        # 退款率
        refund_rate = (latest_refund / latest_net * 100) if latest_net > 0 else 0
        # ROI
        roi = (ad_total / latest_ad) if latest_ad > 0 else 0

        # 环比变化
        if prev_net > 0:
            net_change = round((latest_net - prev_net) / prev_net * 100, 1)
        else:
            net_change = None

        # 近7天趋势
        trends = []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            row = db_execute(
                "SELECT COALESCE(SUM(净支付金额), 0) AS v FROM 店铺营销数据 WHERE 日期 = %s",
                [d]
            )[0]
            trends.append({'date': str(d), 'revenue': float(row['v'])})

        return success({
            'netPayment':    round(latest_net, 2),
            'payment':       round(payment, 2),
            'refundAmount':  round(latest_refund, 2),
            'refundRate':    round(refund_rate, 2),
            'adSpend':       round(latest_ad, 2),
            'adTotal':       round(ad_total, 2),
            'roi':           round(roi, 4),
            'visitors':      visitors,
            'payers':        payers,
            'netChange':     net_change,
            'trends':        trends,
            'latestDate':    str(latest_date),
        })
    except Exception as e:
        return fail(str(e))


# ======================== 营销数据总览 ========================

@app.route('/api/marketing/filters', methods=['GET'])
def marketing_filters():
    """返回可选的平台和品牌列表"""
    try:
        platforms = db_execute('SELECT DISTINCT 平台 FROM 店铺营销数据 ORDER BY 平台')
        brands    = db_execute('SELECT DISTINCT 品牌 FROM 店铺营销数据 ORDER BY 品牌')
        return success({
            'platforms': [r['平台'] for r in platforms],
            'brands':    [r['品牌'] for r in brands],
        })
    except Exception as e:
        return fail(str(e))


@app.route('/api/marketing/overview', methods=['GET'])
def marketing_overview():
    """营销数据总览 - 从店铺营销数据表聚合计算各指标，支持日期范围、平台、品牌筛选"""
    try:
        start_date = request.args.get('start')
        end_date   = request.args.get('end')
        platform   = request.args.get('platform', '').strip()
        brand      = request.args.get('brand', '').strip()

        conditions = []
        params = []
        if start_date and end_date:
            conditions.append('日期 >= %s AND 日期 <= %s')
            params.extend([start_date, end_date])
        elif start_date:
            conditions.append('日期 >= %s')
            params.append(start_date)
        elif end_date:
            conditions.append('日期 <= %s')
            params.append(end_date)
        if platform:
            conditions.append('平台 = %s')
            params.append(platform)
        if brand:
            conditions.append('品牌 = %s')
            params.append(brand)

        where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

        # 汇总店铺营销数据（按筛选日期）
        row = db_execute(f"""
            SELECT
                COALESCE(SUM(净支付金额), 0) AS net_payment,
                COALESCE(SUM(退款金额), 0) AS refund_amount,
                COALESCE(SUM(推广花费), 0) AS ad_spend,
                COALESCE(SUM(访客数), 0) AS visitors,
                COALESCE(SUM(支付买家数), 0) AS payers,
                COALESCE(SUM(支付金额), 0) AS payment,
                COALESCE(SUM(推广总成交), 0) AS ad_total
            FROM 店铺营销数据
            {where_clause}
        """, params)[0]

        net_payment   = float(row['net_payment'])
        refund_amount = float(row['refund_amount'])
        ad_spend      = float(row['ad_spend'])
        visitors      = int(row['visitors'])
        payers        = int(row['payers'])
        payment       = float(row['payment'])
        ad_total      = float(row['ad_total'])

        # 金额退款率 = 退款金额 / 净支付金额
        refund_rate = (refund_amount / net_payment * 100) if net_payment > 0 else 0
        # 当日ROI = 推广总成交金额 / 推广花费
        roi = (ad_total / ad_spend) if ad_spend > 0 else 0

        # ---- 环比：上期同等长度的数据 ----
        comparison = None
        if start_date and end_date:
            s = datetime.strptime(start_date, '%Y-%m-%d').date()
            e = datetime.strptime(end_date, '%Y-%m-%d').date()
        else:
            # 没有日期范围时默认对比近一次有数据的日期
            s = e = date.today()

        period_days = (e - s).days + 1
        prev_s = s - timedelta(days=period_days)
        prev_e = s - timedelta(days=1)

        # 上期 WHERE
        prev_conds = ['日期 >= %s', '日期 <= %s']
        prev_params = [prev_s, prev_e]
        if platform:
            prev_conds.append('平台 = %s')
            prev_params.append(platform)
        if brand:
            prev_conds.append('品牌 = %s')
            prev_params.append(brand)
        prev_where = 'WHERE ' + ' AND '.join(prev_conds)

        prev_row = db_execute(f"""
            SELECT
                COALESCE(SUM(净支付金额), 0) AS net_payment,
                COALESCE(SUM(退款金额), 0) AS refund_amount,
                COALESCE(SUM(推广花费), 0) AS ad_spend,
                COALESCE(SUM(推广总成交), 0) AS ad_total,
                COALESCE(SUM(访客数), 0) AS visitors,
                COALESCE(SUM(支付买家数), 0) AS payers
            FROM 店铺营销数据
            {prev_where}
        """, prev_params)[0]

        prev_net = float(prev_row['net_payment'])
        prev_refund = float(prev_row['refund_amount'])
        prev_ad_spend = float(prev_row['ad_spend'])
        prev_ad_total = float(prev_row['ad_total'])
        prev_visitors = int(prev_row['visitors'])
        prev_payers = int(prev_row['payers'])

        # 上期退款率 & ROI
        prev_refund_rate = (prev_refund / prev_net * 100) if prev_net > 0 else 0
        prev_roi = (prev_ad_total / prev_ad_spend) if prev_ad_spend > 0 else 0

        def pct(cur, prev):
            """返回百分比变化，无法计算时为 None"""
            if prev > 0:
                return round((cur - prev) / prev * 100, 1)
            elif prev == 0 and cur > 0:
                return None  # 从 0 到有数据，无法除，前端显示 "新增"
            return None

        comparison = {
            'periodLabel': f'{prev_s} ~ {prev_e}',
            'isSingleDay': period_days == 1,
            'netPayment':   pct(net_payment, prev_net),
            'refundAmount': pct(refund_amount, prev_refund),
            'refundRate':   round(refund_rate - prev_refund_rate, 2),
            'adSpend':      pct(ad_spend, prev_ad_spend),
            'adTotal':      pct(ad_total, prev_ad_total),
            'roi':          round(roi - prev_roi, 4),
            'visitors':     pct(visitors, prev_visitors),
            'payers':       pct(payers, prev_payers),
        }

        # 每日趋势数据（供前端 sparkline 和图表分析区使用）
        trends = []
        daily_fields = """
            日期,
            COALESCE(SUM(净支付金额), 0) AS net,
            COALESCE(SUM(退款金额), 0) AS refund,
            COALESCE(SUM(推广花费), 0) AS ad_spend,
            COALESCE(SUM(推广总成交), 0) AS ad_total,
            COALESCE(SUM(访客数), 0) AS vis,
            COALESCE(SUM(支付买家数), 0) AS pay,
            COALESCE(SUM(支付金额), 0) AS payment,
            0 AS cart,
            COALESCE(AVG(支付转化率), 0) AS conv_rate,
            COALESCE(AVG(订单退款率), 0) AS order_refund_rate,
            COALESCE(AVG(客单价), 0) AS aov
        """
        if start_date and end_date:
            trows = db_execute(f"""
                SELECT {daily_fields}
                FROM 店铺营销数据
                {where_clause}
                GROUP BY 日期 ORDER BY 日期
            """, params)
        else:
            trows = []
            for i in range(6, -1, -1):
                d = date.today() - timedelta(days=i)
                row = db_execute(f"""
                    SELECT {daily_fields}
                    FROM 店铺营销数据 WHERE 日期 = %s
                """, [d])[0]
                trows.append(row)

        # 汇总聚合（用于漏斗等单值模块）
        agg = {
            'totalVisitors': 0, 'totalCart': 0, 'totalPayers': 0,
            'totalPayment': 0.0, 'totalRefund': 0.0,
            'totalAdSpend': 0.0, 'totalAdRev': 0.0, 'totalAdTotal': 0.0,
            'avgConvRate': 0.0, 'avgOrderRefundRate': 0.0, 'avgAOV': 0.0,
        }
        trends = []
        for trow in trows:
            net = float(trow['net'])
            refund = float(trow['refund'])
            day_ad_spend = float(trow['ad_spend'])
            pay = int(trow['pay'])
            vis = int(trow['vis'])
            cart = int(trow['cart'])
            day_payment = float(trow['payment'])
            conv = float(trow['conv_rate'])
            order_rr = float(trow['order_refund_rate'])
            aov = float(trow['aov'])
            day_ad_total = float(trow['ad_total'])

            trends.append({
                'date': str(trow['日期']),
                'netPayment': net,
                'refundAmount': refund,
                'refundRate': round(refund / net * 100, 2) if net > 0 else 0,
                'adSpend': day_ad_spend,
                'roi': round(day_ad_total / day_ad_spend, 4) if day_ad_spend > 0 else 0,
                'visitors': vis,
                'payers': pay,
                'payment': day_payment,
                'cart': cart,
                'convRate': round(conv * 100, 2),
                'orderRefundRate': round(order_rr * 100, 2),
                'aov': round(aov, 2),
                'adTotal': day_ad_total,
            })
            agg['totalVisitors'] += vis
            agg['totalCart'] += cart
            agg['totalPayers'] += pay
            agg['totalPayment'] += day_payment
            agg['totalRefund'] += refund
            agg['totalAdSpend'] += day_ad_spend
            agg['totalAdRev'] += day_ad_total
            agg['totalAdTotal'] += day_ad_total

        # 汇总平均
        n = len(trends) or 1
        agg['avgConvRate'] = round(agg['totalPayers'] / agg['totalVisitors'] * 100, 2) if agg['totalVisitors'] > 0 else 0
        agg['avgOrderRefundRate'] = round(sum(t['orderRefundRate'] for t in trends) / n, 2)
        agg['avgAOV'] = round(agg['totalPayment'] / agg['totalPayers'], 2) if agg['totalPayers'] > 0 else 0

        return success({
            'netPayment':   round(net_payment, 2),
            'payment':      round(payment, 2),
            'refundAmount': round(refund_amount, 2),
            'refundRate':   round(refund_rate, 2),
            'adSpend':      round(ad_spend, 2),
            'adTotal':      round(ad_total, 2),
            'roi':          round(roi, 4),
            'visitors':     visitors,
            'payers':       payers,
            'trends':       trends,
            'comparison':   comparison,
            'agg':          agg,
        })
    except Exception as e:
        return fail(str(e))


# ======================== 品类营销数据 ========================

# 各平台单链接表 → 品类映射聚合的字段口径（与订单详情「全部平台」共同字段一致）
_CAT_PLATFORM = {
    '抖店': {'table': '抖店单链接数据表', 'id': '商品编码', 'date': '统计周期',
             'payment': '成交金额', 'orders': '成交订单数', 'buyers': '成交人数', 'refund': '成交退款金额',
             'spend_sql': "COALESCE(SUM(t.`投放消耗（店铺被投）`),0) + COALESCE(SUM(t.`投放消耗（推商品）`),0)",
             'ad_gmv': '投放贡献成交金额'},
    '京东': {'table': '京东单链接数据表', 'id': 'SPU', 'date': '时间',
             'payment': '成交金额', 'orders': '成交单量', 'buyers': '成交客户数', 'refund': '取消及售后退款金额'},
    '千牛': {'table': '千牛单链接数据表', 'id': '商品ID', 'date': '统计日期',
             'payment': '支付金额', 'orders': '支付件数', 'buyers': '支付买家数', 'refund': '成功退款金额'},
}

# 千牛投放数据在单独的推广表（按商品ID关联）
_QN_PROMO = {'table': '千牛单链接推广数据表', 'id': '商品ID', 'date': '统计日期',
             'spend': '推广消耗', 'ad_gmv': '总引导成交金额'}


def _gather_category_data(start_date='', end_date=''):
    """按统一品类汇总成交/消耗/产出数据（品类营销页 + 每日分析共用）。
    返回 {'totals': {...}, 'categories': [...]}"""
    agg = {}  # 品类 -> {payment, orders, buyers, refund, products, spend, ad_gmv}
    for plat, meta in _CAT_PLATFORM.items():
        where = ["m.`平台` = %s"]
        params = [plat]
        if start_date:
            where.append(f"t.`{meta['date']}` >= %s")
            params.append(start_date)
        if end_date:
            where.append(f"t.`{meta['date']}` <= %s")
            params.append(end_date)
        spend_sql = meta.get('spend_sql', '0')
        ad_gmv_col = meta.get('ad_gmv')
        ad_gmv_sql = f"COALESCE(SUM(t.`{ad_gmv_col}`), 0)" if ad_gmv_col else '0'
        sql = f"""
            SELECT m.`统一品类` AS cat,
                   COALESCE(SUM(t.`{meta['payment']}`), 0) AS payment,
                   COALESCE(SUM(t.`{meta['orders']}`), 0)   AS orders,
                   COALESCE(SUM(t.`{meta['buyers']}`), 0)   AS buyers,
                   COALESCE(SUM(t.`{meta['refund']}`), 0)   AS refund,
                   COUNT(DISTINCT t.`{meta['id']}`)         AS products,
                   {spend_sql}                               AS spend,
                   {ad_gmv_sql}                              AS ad_gmv
            FROM `商品品类映射表` m
            JOIN `{meta['table']}` t ON m.`平台商品ID` = t.`{meta['id']}`
            WHERE {' AND '.join(where)}
              AND m.`统一品类` NOT IN ('补差价链接')
            GROUP BY m.`统一品类`
        """
        rows = db_execute(sql, params)
        for r in rows:
            cat = r['cat']
            g = agg.get(cat)
            if g is None:
                g = {'category': cat, 'payment': 0.0, 'orders': 0, 'buyers': 0,
                     'refund': 0.0, 'products': 0, 'spend': 0.0, 'ad_gmv': 0.0}
                agg[cat] = g
            g['payment'] += float(r['payment'])
            g['orders'] += int(r['orders'])
            g['buyers'] += int(r['buyers'])
            g['refund'] += float(r['refund'])
            g['products'] += int(r['products'])
            g['spend'] += float(r['spend'])
            g['ad_gmv'] += float(r['ad_gmv'])

    # 千牛：投放数据在推广表，单独按商品ID聚合后并入
    qn_where = ["m.`平台` = '千牛'"]
    qn_params = []
    if start_date:
        qn_where.append(f"p.`{_QN_PROMO['date']}` >= %s")
        qn_params.append(start_date)
    if end_date:
        qn_where.append(f"p.`{_QN_PROMO['date']}` <= %s")
        qn_params.append(end_date)
    qn_sql = f"""
        SELECT m.`统一品类` AS cat,
               COALESCE(SUM(p.`{_QN_PROMO['spend']}`), 0)  AS spend,
               COALESCE(SUM(p.`{_QN_PROMO['ad_gmv']}`), 0) AS ad_gmv
        FROM `商品品类映射表` m
        JOIN `{_QN_PROMO['table']}` p ON m.`平台商品ID` = p.`{_QN_PROMO['id']}`
        WHERE {' AND '.join(qn_where)}
          AND m.`统一品类` NOT IN ('补差价链接')
        GROUP BY m.`统一品类`
    """
    for r in db_execute(qn_sql, qn_params):
        g = agg.get(r['cat'])
        if g is not None:
            g['spend'] += float(r['spend'])
            g['ad_gmv'] += float(r['ad_gmv'])

    categories = list(agg.values())
    for c in categories:
        c['payment'] = round(c['payment'], 2)
        c['refund'] = round(c['refund'], 2)
        c['spend'] = round(c['spend'], 2)
        c['ad_gmv'] = round(c['ad_gmv'], 2)
        c['refundRate'] = round(c['refund'] / c['payment'] * 100, 2) if c['payment'] > 0 else 0
        c['roi'] = round(c['ad_gmv'] / c['spend'], 4) if c['spend'] > 0 else 0
    categories.sort(key=lambda x: x['payment'], reverse=True)

    total_payment = round(sum(c['payment'] for c in categories), 2)
    total_refund = round(sum(c['refund'] for c in categories), 2)
    total_spend = round(sum(c['spend'] for c in categories), 2)
    total_ad_gmv = round(sum(c['ad_gmv'] for c in categories), 2)
    totals = {
        'categoryCount': len(categories),
        'productCount': sum(c['products'] for c in categories),
        'payment': total_payment,
        'orders': sum(c['orders'] for c in categories),
        'buyers': sum(c['buyers'] for c in categories),
        'refund': total_refund,
        'refundRate': round(total_refund / total_payment * 100, 2) if total_payment > 0 else 0,
        'spend': total_spend,
        'adGmv': total_ad_gmv,
        'roi': round(total_ad_gmv / total_spend, 4) if total_spend > 0 else 0,
    }
    return {'totals': totals, 'categories': categories}


@app.route('/api/category-marketing/data', methods=['GET'])
def category_marketing_data():
    """品类营销数据：把「商品品类映射表」关联三张单链接表，按统一品类汇总真实成交数据"""
    try:
        start_date = request.args.get('start', '').strip()
        end_date = request.args.get('end', '').strip()
        result = _gather_category_data(start_date, end_date)
        # 附上各品类的命中关键词（供前端展示）
        for c in result['categories']:
            c['keywords'] = _category_keywords(c['category'])
        return success(result)
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


# ======================== 分平台/店铺详细数据 ========================

@app.route('/api/platform-store/data', methods=['GET'])
def platform_store_data():
    """分平台/店铺详细数据 - 按平台和店铺名维度聚合，支持日期和平台筛选"""
    try:
        start_date = request.args.get('start')
        end_date   = request.args.get('end')
        platform   = request.args.get('platform', '').strip()

        conditions = []
        params = []
        if start_date and end_date:
            conditions.append('日期 >= %s AND 日期 <= %s')
            params.extend([start_date, end_date])
        elif start_date:
            conditions.append('日期 >= %s')
            params.append(start_date)
        elif end_date:
            conditions.append('日期 <= %s')
            params.append(end_date)
        if platform:
            conditions.append('平台 = %s')
            params.append(platform)

        where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

        # ---- 按平台聚合 ----
        platform_rows = db_execute(f"""
            SELECT
                平台,
                COUNT(DISTINCT 店铺名) AS store_count,
                COALESCE(SUM(净支付金额), 0) AS net_payment,
                COALESCE(SUM(退款金额), 0) AS refund_amount,
                COALESCE(SUM(推广花费), 0) AS ad_spend,
                COALESCE(SUM(推广总成交), 0) AS ad_total,
                COALESCE(SUM(访客数), 0) AS visitors,
                COALESCE(SUM(支付买家数), 0) AS payers,
                COALESCE(SUM(支付金额), 0) AS payment,
                0 AS cart,
                COALESCE(AVG(支付转化率), 0) AS conv_rate,
                COALESCE(AVG(订单退款率), 0) AS order_refund_rate,
                COALESCE(AVG(客单价), 0) AS aov
            FROM 店铺营销数据
            {where_clause}
            GROUP BY 平台
            ORDER BY net_payment DESC
        """, params)

        # ---- 计算平台占比 ----
        total_net = sum(float(r['net_payment']) for r in platform_rows)
        platforms = []
        for r in platform_rows:
            net = float(r['net_payment'])
            platforms.append({
                'name': r['平台'],
                'storeCount': int(r['store_count']),
                'netPayment': round(net, 2),
                'refundAmount': round(float(r['refund_amount']), 2),
                'adSpend': round(float(r['ad_spend']), 2),
                'adTotal': round(float(r['ad_total']), 2),
                'adTotal': round(float(r['ad_total']), 2),
                'visitors': int(r['visitors']),
                'payers': int(r['payers']),
                'payment': round(float(r['payment']), 2),
                'cart': int(r['cart']),
                'convRate': round(float(r['conv_rate']) * 100, 2),
                'orderRefundRate': round(float(r['order_refund_rate']) * 100, 2),
                'aov': round(float(r['aov']), 2),
                'share': round(net / total_net * 100, 2) if total_net > 0 else 0,
            })

        # ---- 按店铺(平台+店铺名)聚合 ----
        store_rows = db_execute(f"""
            SELECT
                平台,
                店铺名,
                MAX(品牌) AS 品牌,
                COALESCE(SUM(净支付金额), 0) AS net_payment,
                COALESCE(SUM(退款金额), 0) AS refund_amount,
                COALESCE(SUM(推广花费), 0) AS ad_spend,
                COALESCE(SUM(推广总成交), 0) AS ad_total,
                COALESCE(SUM(访客数), 0) AS visitors,
                COALESCE(SUM(支付买家数), 0) AS payers,
                COALESCE(SUM(支付金额), 0) AS payment,
                0 AS cart,
                COALESCE(AVG(支付转化率), 0) AS conv_rate,
                COALESCE(AVG(订单退款率), 0) AS order_refund_rate,
                COALESCE(AVG(客单价), 0) AS aov
            FROM 店铺营销数据
            {where_clause}
            GROUP BY 平台, 店铺名
            ORDER BY net_payment DESC
        """, params)

        stores = []
        for i, r in enumerate(store_rows):
            net = float(r['net_payment'])
            ad_spend = float(r['ad_spend'])
            ad_total = float(r['ad_total'])
            store_name = r['店铺名']
            stores.append({
                'rank': i + 1,
                'platform': r['平台'],
                'brand': r['品牌'],
                'name': f"{store_name}",
                'fullName': f"{r['平台']} - {store_name}",
                'netPayment': round(net, 2),
                'refundAmount': round(float(r['refund_amount']), 2),
                'adSpend': round(ad_spend, 2),
                'adTotal': round(ad_total, 2),
                'visitors': int(r['visitors']),
                'payers': int(r['payers']),
                'payment': round(float(r['payment']), 2),
                'cart': int(r['cart']),
                'convRate': round(float(r['conv_rate']) * 100, 2),
                'orderRefundRate': round(float(r['order_refund_rate']) * 100, 2),
                'aov': round(float(r['aov']), 2),
                'roi': round(ad_total / ad_spend, 4) if ad_spend > 0 else 0,
                'refundRate': round(float(r['refund_amount']) / net * 100, 2) if net > 0 else 0,
                'share': round(net / total_net * 100, 2) if total_net > 0 else 0,
            })

        # ---- 每日趋势（按平台聚合的日数据，供平台趋势图使用） ----
        platform_trends = {}
        if start_date and end_date:
            trend_rows = db_execute(f"""
                SELECT 平台, 日期,
                    COALESCE(SUM(净支付金额), 0) AS net_payment,
                    COALESCE(SUM(退款金额), 0) AS refund_amount,
                    COALESCE(SUM(推广花费), 0) AS ad_spend,
                    COALESCE(SUM(推广总成交), 0) AS ad_total,
                        COALESCE(SUM(访客数), 0) AS visitors,
                    COALESCE(SUM(支付买家数), 0) AS payers
                FROM 店铺营销数据
                {where_clause}
                GROUP BY 平台, 日期
                ORDER BY 日期
            """, params)
            for tr in trend_rows:
                pname = tr['平台']
                if pname not in platform_trends:
                    platform_trends[pname] = []
                platform_trends[pname].append({
                    'date': str(tr['日期']),
                    'netPayment': round(float(tr['net_payment']), 2),
                    'refundAmount': round(float(tr['refund_amount']), 2),
                    'adSpend': round(float(tr['ad_spend']), 2),
                    'adTotal': round(float(tr['ad_total']), 2),
                    'visitors': int(tr['visitors']),
                    'payers': int(tr['payers']),
                })

        # ---- 店铺每日趋势（前10店铺） ----
        store_trends = {}
        top_stores = [s['fullName'] for s in stores[:10]]
        if top_stores and start_date and end_date:
            # 为每个top店铺查询趋势
            for ts in top_stores:
                parts = ts.split(' - ', 1)
                if len(parts) == 2:
                    p, b = parts[0], parts[1]
                    s_conds = ['平台 = %s', '店铺名 = %s']
                    s_params = [p, b]
                    if start_date and end_date:
                        s_conds.append('日期 >= %s AND 日期 <= %s')
                        s_params.extend([start_date, end_date])
                    s_where = 'WHERE ' + ' AND '.join(s_conds)
                    str_rows = db_execute(f"""
                        SELECT 日期,
                            COALESCE(SUM(净支付金额), 0) AS net_payment,
                            COALESCE(SUM(退款金额), 0) AS refund_amount,
                            COALESCE(SUM(推广花费), 0) AS ad_spend,
                            COALESCE(SUM(访客数), 0) AS visitors,
                            COALESCE(SUM(支付买家数), 0) AS payers
                        FROM 店铺营销数据
                        {s_where}
                        GROUP BY 日期 ORDER BY 日期
                    """, s_params)
                    store_trends[ts] = [{
                        'date': str(sr['日期']),
                        'netPayment': round(float(sr['net_payment']), 2),
                        'refundAmount': round(float(sr['refund_amount']), 2),
                        'adSpend': round(float(sr['ad_spend']), 2),
                        'visitors': int(sr['visitors']),
                        'payers': int(sr['payers']),
                    } for sr in str_rows]

        return success({
            'platforms': platforms,
            'stores': stores,
            'platformTrends': platform_trends,
            'storeTrends': store_trends,
            'totalStores': len(stores),
            'totalNetPayment': round(total_net, 2),
        })
    except Exception as e:
        return fail(str(e))


# ======================== 管理员账户管理 ========================

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """登录验证 - 从数据库校验账号密码，返回角色权限列表"""
    try:
        data = request.get_json(force=True)
        account = data.get('username', '').strip()
        password = data.get('password', '').strip()

        if not account or not password:
            return fail('请输入账号和密码')

        row = db_execute(
            'SELECT * FROM admin_accounts WHERE account = %s AND password = %s',
            [account, password]
        )
        if not row:
            return fail('账号或密码错误')

        admin = row[0]
        if admin['status'] == 'disabled':
            return fail('该账号已被禁用')

        # 更新最后登录时间
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        db_execute(
            'UPDATE admin_accounts SET last_login = %s WHERE id = %s',
            [now_str, admin['id']], fetch=False
        )

        # 从 admin_roles 表获取该角色的权限列表
        import json
        permissions = []
        role_row = db_execute(
            'SELECT permissions FROM admin_roles WHERE name = %s',
            [admin['role']]
        )
        if role_row:
            perms_data = role_row[0]['permissions']
            permissions = json.loads(perms_data) if isinstance(perms_data, str) else perms_data

        return success({
            'id': admin['id'],
            'name': admin['name'],
            'account': admin['account'],
            'role': admin['role'],
            'status': admin['status'],
            'lastLogin': now_str,
            'permissions': permissions,
        }, '登录成功')
    except Exception as e:
        return fail(str(e))


# ======================== 角色与权限管理 API ========================

@app.route('/api/admin/roles', methods=['GET'])
def admin_roles_list():
    """获取所有角色及其权限"""
    try:
        import json
        rows = db_execute('SELECT id, name, permissions, created_at FROM admin_roles ORDER BY id')
        result = []
        for r in rows:
            perms = json.loads(r['permissions']) if isinstance(r['permissions'], str) else r['permissions']
            result.append({
                'id': r['id'],
                'name': r['name'],
                'permissions': perms,
                'createdAt': str(r['created_at']) if r['created_at'] else '',
            })
        return success(result)
    except Exception as e:
        return fail(str(e))


@app.route('/api/admin/roles', methods=['POST'])
def admin_roles_create():
    """新增角色"""
    try:
        import json
        data = request.get_json(force=True)
        name = data.get('name', '').strip()
        permissions = data.get('permissions', [])
        if not name:
            return fail('角色名称不能为空')
        existing = db_execute('SELECT id FROM admin_roles WHERE name = %s', [name])
        if existing:
            return fail('该角色名称已存在')
        db_execute(
            'INSERT INTO admin_roles (name, permissions) VALUES (%s, %s)',
            [name, json.dumps(permissions, ensure_ascii=False)], fetch=False
        )
        return success(None, '角色已创建')
    except Exception as e:
        return fail(str(e))


@app.route('/api/admin/roles/<int:role_id>', methods=['PUT'])
def admin_roles_update(role_id):
    """更新角色权限"""
    try:
        import json
        data = request.get_json(force=True)
        name = data.get('name', '').strip()
        permissions = data.get('permissions', [])
        if not name:
            return fail('角色名称不能为空')
        db_execute(
            'UPDATE admin_roles SET name = %s, permissions = %s WHERE id = %s',
            [name, json.dumps(permissions, ensure_ascii=False), role_id], fetch=False
        )
        return success(None, '角色已更新')
    except Exception as e:
        return fail(str(e))


@app.route('/api/admin/roles/<int:role_id>', methods=['DELETE'])
def admin_roles_delete(role_id):
    """删除角色"""
    try:
        db_execute('DELETE FROM admin_roles WHERE id = %s', [role_id], fetch=False)
        return success(None, '角色已删除')
    except Exception as e:
        return fail(str(e))


@app.route('/api/admin/accounts', methods=['GET'])
def admin_list():
    """管理员列表"""
    try:
        rows = db_execute(
            'SELECT id, name, account, password, role, status, last_login, created_at FROM admin_accounts ORDER BY id'
        )
        admins = []
        for r in rows:
            admins.append({
                'id': r['id'],
                'name': r['name'],
                'account': r['account'],
                'password': r['password'],
                'role': r['role'],
                'status': r['status'],
                'lastLogin': r['last_login'] or '',
                'createdAt': str(r['created_at']) if r['created_at'] else '',
            })
        return success(admins)
    except Exception as e:
        return fail(str(e))


@app.route('/api/admin/accounts', methods=['POST'])
def admin_create():
    """新增管理员"""
    try:
        data = request.get_json(force=True)
        name = data.get('name', '').strip()
        account = data.get('account', '').strip()
        password = data.get('password', '').strip()
        role = data.get('role', '运营主管').strip()
        status = data.get('status', 'enabled').strip()

        if not name or not account or not password:
            return fail('请填写完整信息')

        # 检查账号是否已存在
        existing = db_execute('SELECT id FROM admin_accounts WHERE account = %s', [account])
        if existing:
            return fail('账号已存在')

        new_id = db_execute_insert(
            'INSERT INTO admin_accounts (name, account, password, role, status) VALUES (%s,%s,%s,%s,%s)',
            [name, account, password, role, status]
        )
        return success({'id': new_id}, '管理员已创建')
    except Exception as e:
        return fail(str(e))


@app.route('/api/admin/accounts/<int:aid>', methods=['PUT'])
def admin_update(aid):
    """更新管理员信息（姓名、密码、角色、状态）"""
    try:
        data = request.get_json(force=True)
        updates = []
        params = []

        if 'name' in data:
            updates.append('name = %s')
            params.append(data['name'].strip())
        if 'password' in data and data['password'].strip():
            updates.append('password = %s')
            params.append(data['password'].strip())
        if 'role' in data:
            updates.append('role = %s')
            params.append(data['role'].strip())
        if 'status' in data:
            updates.append('status = %s')
            params.append(data['status'].strip())

        if not updates:
            return fail('没有要更新的数据')

        # 不允许修改 admin 账号的角色和状态（保护超级管理员）
        row = db_execute('SELECT account FROM admin_accounts WHERE id = %s', [aid])
        if not row:
            return fail('管理员不存在')
        if row[0]['account'] == 'admin':
            # admin 只能改自己的密码
            allowed = [u for u in updates if 'password' in u or 'name' in u]
            if len(allowed) != len(updates):
                return fail('不能修改超级管理员的角色或状态')

        params.append(aid)
        sql = 'UPDATE admin_accounts SET ' + ', '.join(updates) + ' WHERE id = %s'
        db_execute(sql, params, fetch=False)
        return success(None, '更新成功')
    except Exception as e:
        return fail(str(e))


@app.route('/api/admin/accounts/<int:aid>', methods=['DELETE'])
def admin_delete(aid):
    """删除管理员"""
    try:
        row = db_execute('SELECT account FROM admin_accounts WHERE id = %s', [aid])
        if not row:
            return fail('管理员不存在')
        if row[0]['account'] == 'admin':
            return fail('不能删除超级管理员账号')
        db_execute('DELETE FROM admin_accounts WHERE id = %s', [aid], fetch=False)
        return success(None, '已删除')
    except Exception as e:
        return fail(str(e))


# ======================== 数据库管理（通用CRUD） ========================

# 允许通过浏览器管理的表（白名单，防SQL注入）
ALLOWED_TABLES = {'products', 'orders', 'customers'}

# 各表的主键列名
TABLE_PK = {
    'products':  'id',
    'orders':    'id',
    'customers': 'id',
}

# 各表的可编辑列（排除自动生成的字段）
TABLE_EDITABLE_COLS = {
    'products':  ['name', 'category', 'price', 'stock', 'status'],
    'orders':    ['customer', 'product', 'qty', 'amount', 'status', 'date'],
    'customers': ['name', 'phone', 'email', 'address', 'reg_date'],
}


def _check_table(table_name):
    """校验表名合法性"""
    if table_name not in ALLOWED_TABLES:
        raise ValueError(f'不允许操作表: {table_name}')


def _serialize_rows(rows):
    """将查询结果中的 date/datetime/Decimal 转为 JSON 兼容类型"""
    for r in rows:
        for k, v in r.items():
            if isinstance(v, (date, datetime)):
                r[k] = str(v)
            elif isinstance(v, Decimal):
                r[k] = float(v)
    return rows


@app.route('/api/db/tables', methods=['GET'])
def db_list_tables():
    """列出所有可管理的表及其列信息"""
    try:
        tables = []
        for t in ALLOWED_TABLES:
            cols = db_execute(f'SHOW FULL COLUMNS FROM `{t}`')
            tables.append({
                'name': t,
                'pk': TABLE_PK.get(t, 'id'),
                'editableCols': TABLE_EDITABLE_COLS.get(t, []),
                'columns': [{
                    'field': c['Field'],
                    'type': c['Type'],
                    'comment': c.get('Comment', ''),
                    'nullable': c.get('Null', 'YES') == 'YES',
                    'key': c.get('Key', ''),
                } for c in cols],
            })
        return success(tables)
    except Exception as e:
        return fail(str(e))


@app.route('/api/db/tables/<table_name>/rows', methods=['GET'])
def db_list_rows(table_name):
    """查询表数据（支持 ?search=&page=&pageSize=）"""
    try:
        _check_table(table_name)
        search = request.args.get('search', '').strip()
        page = max(1, int(request.args.get('page', 1)))
        page_size = min(100, max(1, int(request.args.get('pageSize', 15))))

        sql = f'SELECT * FROM `{table_name}`'
        count_sql = f'SELECT COUNT(*) AS total FROM `{table_name}`'
        params = []

        if search:
            cols = db_execute(f'SHOW COLUMNS FROM `{table_name}`')
            text_cols = [
                c['Field'] for c in cols
                if any(t in c['Type'].lower() for t in ('char', 'text', 'varchar'))
            ]
            if text_cols:
                conditions = ' OR '.join([f'`{c}` LIKE %s' for c in text_cols])
                sql += f' WHERE ({conditions})'
                count_sql += f' WHERE ({conditions})'
                params = [f'%{search}%'] * len(text_cols)

        total = db_execute(count_sql, params)[0]['total']

        # 排序：products/customers 按 id DESC，orders 按 date DESC, id DESC
        if table_name == 'orders':
            sql += ' ORDER BY date DESC, id DESC'
        else:
            sql += ' ORDER BY id DESC'

        sql += f' LIMIT {page_size} OFFSET {(page - 1) * page_size}'
        rows = db_execute(sql, params)

        return success({
            'rows': _serialize_rows(rows),
            'total': total,
            'page': page,
            'pageSize': page_size,
            'totalPages': max(1, (total + page_size - 1) // page_size),
        })
    except Exception as e:
        return fail(str(e))


@app.route('/api/db/tables/<table_name>/rows', methods=['POST'])
def db_insert_row(table_name):
    """插入新行"""
    try:
        _check_table(table_name)
        data = request.get_json(force=True)

        # 仅保留可编辑列
        editable = TABLE_EDITABLE_COLS.get(table_name, [])
        filtered = {k: v for k, v in data.items() if k in editable}

        if not filtered:
            return fail('没有可插入的数据')

        cols_sql = ', '.join([f'`{k}`' for k in filtered.keys()])
        vals_sql = ', '.join(['%s'] * len(filtered))
        sql = f'INSERT INTO `{table_name}` ({cols_sql}) VALUES ({vals_sql})'

        pk_col = TABLE_PK.get(table_name, 'id')
        pk_val = db_execute_insert(sql, list(filtered.values()))

        row = db_execute(f'SELECT * FROM `{table_name}` WHERE `{pk_col}` = %s', [pk_val])
        return success(_serialize_rows(row)[0] if row else None, '插入成功')
    except Exception as e:
        return fail(str(e))


@app.route('/api/db/tables/<table_name>/rows/<pk_value>', methods=['PUT'])
def db_update_row(table_name, pk_value):
    """更新行"""
    try:
        _check_table(table_name)
        data = request.get_json(force=True)

        editable = TABLE_EDITABLE_COLS.get(table_name, [])
        filtered = {k: v for k, v in data.items() if k in editable}

        if not filtered:
            return fail('没有可更新的数据')

        set_clause = ', '.join([f'`{k}` = %s' for k in filtered.keys()])
        pk_col = TABLE_PK.get(table_name, 'id')

        # 自动转换 pk_value 类型（products/customers 用 int，orders 用 string）
        if table_name in ('products', 'customers'):
            pk_value = int(pk_value)

        sql = f'UPDATE `{table_name}` SET {set_clause} WHERE `{pk_col}` = %s'
        params = list(filtered.values()) + [pk_value]
        affected = db_execute(sql, params, fetch=False)
        return success({'affected': affected}, '更新成功')
    except Exception as e:
        return fail(str(e))


@app.route('/api/db/tables/<table_name>/rows/<pk_value>', methods=['DELETE'])
def db_delete_row(table_name, pk_value):
    """删除行"""
    try:
        _check_table(table_name)
        pk_col = TABLE_PK.get(table_name, 'id')

        if table_name in ('products', 'customers'):
            pk_value = int(pk_value)

        sql = f'DELETE FROM `{table_name}` WHERE `{pk_col}` = %s'
        affected = db_execute(sql, [pk_value], fetch=False)
        return success({'affected': affected}, '删除成功')
    except Exception as e:
        return fail(str(e))


# ======================== 每日数据分析 ========================

@app.route('/api/analysis/agent', methods=['POST'])
def analysis_agent():
    """每日数据分析智能体：检索该模块知识库（店铺营销/单链接/推广/品类）+ DeepSeek 分析"""
    try:
        payload = request.get_json(silent=True) or {}
        question = (payload.get('question') or '').strip()
        if not question:
            return fail('请输入分析需求')

        target_str = (payload.get('date') or '').strip()
        try:
            target_date = datetime.strptime(target_str, '%Y-%m-%d').date() if target_str else (date.today() - timedelta(days=1))
        except ValueError:
            target_date = date.today() - timedelta(days=1)

        context = _build_analysis_context(target_date)
        ctx_json = json.dumps(context, ensure_ascii=False, default=str)

        sys_p = (
            '# 角色定义\n'
            '你是"数据分析智能体"，一名资深电商经营数据分析师，服务于同时经营多平台（淘宝/京东/拼多多/抖音/快手等）的商家。'
            '你只基于给定的知识库数据做分析，绝不编造。\n\n'
            '# 数据来源与结构\n'
            '你会收到一份结构化的知识库检索结果（JSON），包含该商家的四类经营数据，均为同一天（指定日期）：\n\n'
            '1. 营销综合（营销综合）\n'
            '   字段：净支付金额、支付金额、退款金额、推广花费、推广总成交、访客数、支付买家数、支付转化率(%)、订单退款率(%)、客单价\n'
            '   含义：整体经营健康度\n\n'
            '2. 分平台 / 分品牌 / 分店铺数据（分平台 / 分品牌 / 分店铺）\n'
            '   字段：平台、店铺名、品牌、净支付金额、退款金额、推广花费、推广总成交、访客数、支付买家数、支付转化率(%)、退款率(%)、客单价\n'
            '   含义：横向对比各平台与各店铺表现差异\n\n'
            '3. 单链接数据（单链接数据：抖店/京东/千牛）\n'
            '   字段：成交金额、订单数、买家数、退款、退款率、客单价、投放消耗、佣金、补贴、投放成交、投放产出比、综合消耗产出比\n'
            '   含义：各平台单链接的成交质量与投放效率\n\n'
            '4. 千牛推广数据（千牛推广数据）\n'
            '   字段：推广消耗、直接引导成交、直接ROI、总引导成交、总ROI、展现量、点击量、点击率、单次点击成本、成交笔数、推广商品数、涉及店铺数\n'
            '   含义：千牛付费投放的效率\n\n'
            '5. 品类汇总数据（品类数据）\n'
            '   字段：品类、成交、退款率、推广消耗、推广产出、ROI\n'
            '   含义：各品类贡献与盈亏\n\n'
            '# 分析框架（严格按此顺序执行）\n\n'
            '## 第一步：经营总览\n'
            '基于营销综合判断当日整体健康度：\n'
            '- 净支付金额规模、退款率（>20% 偏高、>30% 严重）、ROI（<1 亏损）、转化率（<2% 偏低）、客单价\n'
            '- 一句话定性：今日经营是"盈利/持平/亏损"\n\n'
            '## 第二步：平台对比\n'
            '对比各平台净支付、退款率、投放 ROI 差异，指出贡献最大与拖后腿的平台。\n\n'
            '## 第三步：店铺诊断\n'
            '对比各店铺核心指标，点名表现优异（高成交+低退款+高ROI）与需关注（有流量无成交、退款率/转化率异常）的店铺。\n\n'
            '## 第四步：单链接与投放效率\n'
            '分析抖店/京东/千牛单链接的成交质量与消耗产出比，评估付费投放是否划算，指出投放效率最高/最低的平台。\n\n'
            '## 第五步：品类分析\n'
            '对比各品类成交、退款率、ROI，点名贡献最大、亏损（ROI<1）或退款率异常的品类。\n\n'
            '## 第六步：异常与建议\n'
            '汇总所有异常（退款率偏高、ROI<1、转化率极低、有流量无成交等），给出具体可执行的优化建议（按优先级排序）。\n\n'
            '# 输出格式（严格遵守）\n\n'
            '## 第一部分：分析结论\n'
            '标题用"## 分析结论"，中文自然语言，分要点，200~400字，包含：\n'
            '- 当日经营定性（1句）\n'
            '- 平台/店铺/品类层面的关键发现（2~3句）\n'
            '- 最需要立即处理的异常与行动建议（1~2句）\n\n'
            '## 第二部分：推荐卡片\n'
            '紧接一个 JSON 数组（用 ```json 代码块包裹），每个元素字段如下：\n'
            '{"type":"store"|"platform"|"category"|"alert","title":"卡片标题","subtitle":"说明","metric":"关键数值","tags":"逗号分隔标签","reason":"一句话点评/建议"}\n\n'
            '卡片生成规则：\n'
            '- 只挑数据中真实存在、最值得关注的 3~8 条\n'
            '- store 卡片：title=店铺名（含平台），subtitle=净支付/退款率/转化率，metric=净支付金额，type="store"\n'
            '- platform 卡片：title=平台名，subtitle=投放ROI/退款率，metric=净支付金额，type="platform"\n'
            '- category 卡片：title=品类名，subtitle=消耗/产出，metric=ROI，type="category"\n'
            '- alert 卡片：title=异常项（如"退款率偏高"），subtitle=涉及对象，metric=异常数值，type="alert"\n'
            '- tags 须含关键标签，如：盈利/亏损、预警、优化、表现优异等\n'
            '- 金额数据直接引用原始数值，不做计算或估算\n\n'
            '## 第三部分：后续引导\n'
            '在 JSON 代码块之后，用一句话引导用户下一步操作，例如：\n'
            '- "需要我深挖某个店铺的退款原因吗？"\n'
            '- "要我对比近几天的趋势变化吗？"\n\n'
            '# 硬性约束\n'
            '- 所有数据必须来自知识库检索结果，严禁编造不存在的商品、数字或关键词\n'
            '- 金额、ROI、退款率等直接引用原始数据，不做计算或估算（除已有字段外不自行换算）\n'
            '- 涉及具体店铺名/品牌名时客观陈述数据表现，不做主观贬低\n'
            '- 若某数据源无数据或读取异常，在结论中明确说明"XX数据缺失/异常"\n'
            '- 分析结论、卡片、预警必须对应知识库中真实存在的数据'
        )
        user_msg = f'知识库检索结果（JSON）：\n{ctx_json}\n\n用户需求：{question}\n\n请按要求输出分析结论和推荐卡片。'

        raw = call_deepseek_api(sys_p, user_msg, temperature=0.4, max_tokens=4096,
                                api_key=DEEPSEEK_SELECTION_API_KEY)

        cards = _ps_agent_parse_cards(raw)
        analysis = raw or ''
        if raw:
            analysis = re.sub(r'```json\s*\[.*?\]\s*```', '', analysis, flags=re.DOTALL)
            analysis = re.sub(r'\[\s*\{.*?\}\s*(?:,\s*\{.*?\}\s*)*\]', '', analysis, flags=re.DOTALL)
            analysis = re.sub(r'```[a-zA-Z]*', '', analysis)
            analysis = analysis.strip()

        sm = context['营销综合']
        return success({
            'analysis': analysis,
            'cards': cards,
            'meta': {
                'date': str(target_date),
                '净支付金额': sm['净支付金额'],
                '退款率': sm['订单退款率(%)'],
                'ROI': round(sm['推广总成交'] / sm['推广花费'], 2) if sm['推广花费'] > 0 else 0,
                'raw_available': bool(raw),
            },
        }, 'ok')
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


@app.route('/api/analysis/generate', methods=['POST'])
def generate_analysis():
    """生成指定日期数据分析报告 — 模板规则 + DeepSeek AI 洞见，默认分析昨日"""
    try:
        # 支持传入日期参数，否则默认昨日
        target_str = (request.get_json(silent=True) or {}).get('date', '').strip()
        if target_str:
            target_date = datetime.strptime(target_str, '%Y-%m-%d').date()
        else:
            target_date = date.today() - timedelta(days=1)

        # 收集指定日期数据
        data = gather_daily_data(target_date)
        sm = data['summary']

        if sm['netPayment'] == 0 and sm['visitors'] == 0:
            return fail(f'{target_date} 暂无营销数据，无法生成分析报告', code=404)

        # 模板规则：计算指标 & 预警
        refund_rate = round(sm['refundAmount'] / sm['netPayment'] * 100, 2) if sm['netPayment'] > 0 else 0
        roi = round(sm['adTotal'] / sm['adSpend'], 4) if sm['adSpend'] > 0 else 0
        conv = sm['convRate']
        aov = sm['aov']

        warnings = []
        if refund_rate > 20:
            warnings.append({'dim': '退款率偏高', 'value': f'{refund_rate}%', 'level': 'warning'})
        if refund_rate > 30:
            warnings[-1]['level'] = 'danger'
        if roi < 1.0:
            warnings.append({'dim': 'ROI偏低', 'value': str(roi), 'level': 'warning' if roi > 0.5 else 'danger'})
        if conv < 2.0:
            warnings.append({'dim': '转化率偏低', 'value': f'{conv}%', 'level': 'warning'})

        # 店铺级预警
        store_warnings = []
        for s in data['byStore']:
            issues = []
            if s['refundRate'] > 20: issues.append(f"退款率{s['refundRate']:.1f}%偏高")
            if s['convRate'] < 1: issues.append(f"转化率{s['convRate']:.2f}%极低")
            if s['netPayment'] == 0 and s['visitors'] > 0: issues.append("有流量无成交")
            if issues:
                store_warnings.append({'store': f"[{s['platform']}] {s['name']}", 'issues': '；'.join(issues), 'level': 'danger' if s['refundRate'] > 30 or s['convRate'] < 0.5 else 'warning'})

        # 单链接消耗产出比预警（已在 gather_daily_data 中计算）
        link_warnings = data.get('linkIssues', [])
        promo_warnings = data.get('promoIssues', [])

        # 单链接数据可读化（供 prompt 使用）
        link_lines = []
        for plat, lk in data['links'].items():
            if lk.get('error'):
                link_lines.append(f"## {plat}单链接数据\n- 数据读取异常：{lk['error']}")
                continue
            line = (f"## {plat}单链接数据\n"
                    f"- 成交/支付金额 {lk['payment']:,.2f} 元，订单 {lk['orders']:,} 单，买家 {lk['buyers']:,} 人，"
                    f"退款 {lk['refund']:,.2f} 元（退款率 {lk.get('refundRate', 0):.2f}%），客单价 {lk.get('aov', 0):.2f} 元")
            if lk.get('hasSpend'):
                line += (f"\n- 投放消耗 {lk['adSpend']:,.2f} 元，佣金 {lk.get('commission', 0):,.2f} 元，"
                         f"补贴 {lk.get('subsidy', 0):,.2f} 元 → 综合消耗 {lk.get('totalCost', 0):,.2f} 元\n"
                         f"- 投放贡献成交 {lk.get('adGmv', 0):,.2f} 元，投放产出比 {lk.get('adRoi')}，综合消耗产出比 {lk.get('costOutputRatio')}")
            else:
                line += "\n- 该平台无推广花费/投放消耗字段"
            link_lines.append(line)

        # 千牛单链接推广数据可读化
        promo = data.get('qianniuPromo', {})
        if promo.get('error'):
            promo_line = f"- 数据读取异常：{promo['error']}"
        else:
            promo_line = (f"- 推广消耗 {promo['spend']:,.2f} 元，直接引导成交 {promo['directGmv']:,.2f} 元（直接ROI {promo['directRoi']:.2f}），"
                          f"总引导成交 {promo['totalGmv']:,.2f} 元（总ROI {promo['totalRoi']:.2f}）\n"
                          f"- 展现量 {promo['impressions']:,}，点击量 {promo['clicks']:,}，点击率 {promo['ctr']:.2f}%，单次点击成本 {promo['cpc']:.2f} 元\n"
                          f"- 直接成交笔数 {promo['directOrders']:,}，总引导成交笔数 {promo['totalOrders']:,}，推广商品数 {promo['products']:,}，涉及店铺 {promo['stores']:,}")

        # 构建 DeepSeek prompt
        system_prompt = """你是一位资深的电商数据分析师。请根据提供的店铺营销数据、抖店/京东/千牛单链接销售数据、千牛单链接推广数据与品类汇总数据，输出一份 JSON 格式的分析洞察。

JSON 必须包含以下 6 个字符串字段：
- "营销综合"：店铺营销数据整体表现（净支付、退款率、ROI、转化率、客单价），对比各平台差异
- "单链接"：抖店/京东/千牛单链接成交、退款、客单价的横向对比，指出表现最好和最差的平台
- "千牛推广"：千牛推广的消耗、直接/总引导成交、ROI、点击率、单次点击成本，评估投放效率
- "品类"：各品类成交/消耗/ROI 表现，点名贡献最大、亏损(ROI<1)或退款率异常的品类
- "店铺"：各店铺核心指标对比，点名表现优异和需关注的店铺
- "异常建议"：汇总退款率偏高、ROI低于1等异常，给出具体可执行的优化建议

要求：
- 只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块、不要 HTML 标签
- 每个字段 2-4 句简洁专业的分析，可引用数据中的数字
- 对亏损、退款率异常要直接点名"""

        # 品类数据可读化（供 prompt 使用）
        cat = data.get('category', {})
        cat_totals = cat.get('totals', {})
        cat_list = cat.get('categories', [])
        if cat.get('error'):
            cat_lines = [f"- 品类数据读取异常：{cat['error']}"]
        elif cat_list:
            cat_lines = [f"- 品类总数 {cat_totals.get('categoryCount', 0)}，成交 {cat_totals.get('payment', 0):,.2f} 元，退款率 {cat_totals.get('refundRate', 0):.2f}%，推广消耗 {cat_totals.get('spend', 0):,.2f} 元，产出 {cat_totals.get('adGmv', 0):,.2f} 元，总ROI {cat_totals.get('roi', 0):.4f}"]
            for c in cat_list:
                cat_lines.append(f"- {c['category']}：成交 {c['payment']:,.2f} 元，退款率 {c['refundRate']:.2f}%，消耗 {c['spend']:,.2f} 元，产出 {c['ad_gmv']:,.2f} 元，ROI {c['roi']}")
        else:
            cat_lines = ['- 暂无品类数据']

        user_message = f"""请分析以下电商数据并生成 HTML 报告：

## 一、店铺营销数据（日期：{target_date}）
- 净支付金额：{sm['netPayment']:,.2f} 元
- 支付金额：{sm['payment']:,.2f} 元
- 退款金额：{sm['refundAmount']:,.2f} 元
- 金额退款率：{refund_rate:.2f}%
- 推广花费：{sm['adSpend']:,.2f} 元
- 推广总成交：{sm['adTotal']:,.2f} 元
- ROI：{roi:.4f}
- 访客数：{sm['visitors']:,}
- 支付买家数：{sm['payers']:,}
- 支付转化率：{conv:.2f}%
- 客单价：{aov:.2f} 元
- 订单退款率：{sm['orderRefundRate']:.2f}%

## 分平台数据
{chr(10).join([f"- {p['name']}：净支付 {p['netPayment']:,.2f} 元，退款 {p['refundAmount']:,.2f} 元，推广花费 {p['adSpend']:,.2f} 元，ROI {p['adTotal']/p['adSpend']:.2f}" if p['adSpend'] > 0 else f"- {p['name']}：净支付 {p['netPayment']:,.2f} 元（无推广花费）" for p in data['byPlatform']])}

## 各店铺核心指标（TOP15）
{chr(10).join([f"- [{s['platform']}] {s['name']}：净支付 {s['netPayment']:,.2f} 元，退款率 {s['refundRate']:.2f}%，转化率 {s['convRate']:.2f}%，客单价 {s['aov']:.2f} 元，推广花费 {s['adSpend']:,.2f} 元" for s in data['byStore']])}

## 二、单链接数据（抖店/京东/千牛）
{chr(10).join(link_lines)}

## 三、千牛单链接推广数据
{promo_line}

## 四、品类分析数据（按统一品类汇总）
{chr(10).join(cat_lines)}

## 系统预判预警
{chr(10).join([f"- {w['dim']}：{w['value']}（级别：{w['level']}）" for w in warnings]) if warnings else '无显著异常'}

## 店铺级预警
{chr(10).join([f"- {s['store']}：{s['issues']}（级别：{s['level']}）" for s in store_warnings]) if store_warnings else '各店铺指标正常'}

## 单链接消耗产出比预警
{chr(10).join([f"- [{w['platform']}] {w['dim']}：{w['value']}（级别：{w['level']}）{w['msg']}" for w in link_warnings]) if link_warnings else '单链接数据消耗产出比均正常'}

## 千牛推广预警
{chr(10).join([f"- [{w['platform']}] {w['dim']}：{w['value']}（级别：{w['level']}）{w['msg']}" for w in promo_warnings]) if promo_warnings else '千牛推广数据ROI正常'}

请为以上数据输出 JSON 分析结果（6 个字段：营销综合、单链接、千牛推广、品类、店铺、异常建议），每个字段 2-4 句，只输出一个 JSON 对象。"""

        # 调用 DeepSeek 获取各章节分析洞察（JSON），解析失败则降级为纯规则报告
        raw = call_deepseek_api(system_prompt, user_message, max_tokens=8192)
        insights = _parse_json(raw) if raw else {}
        ai_available = bool(insights and any(k in insights for k in ('营销综合', '单链接', '千牛推广', '品类', '店铺', '异常建议')))

        # 构建完整 HTML（固定头部 + 固定骨架 + AI 洞察）
        template_header = f"""<div class="da-report">
<div class="da-header">
  <div class="da-date-badge">
    <i class="fa-solid fa-calendar-check"></i>
    分析日期：{target_date}
  </div>
  <div class="da-meta">
    <span>报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}</span>
    <span>数据来源：店铺营销数据 + 抖店/京东/千牛单链接数据 + 千牛推广数据 + 品类汇总</span>
  </div>
</div>
<div class="da-kpi-row">
  <div class="da-kpi-card {'da-kpi-danger' if refund_rate > 20 else 'da-kpi-good'}">
    <div class="da-kpi-label">净支付金额</div>
    <div class="da-kpi-value">&yen;{sm['netPayment']:,.2f}</div>
    <div class="da-kpi-sub">退款率 {refund_rate:.2f}%</div>
  </div>
  <div class="da-kpi-card {'da-kpi-danger' if roi < 1.0 else 'da-kpi-good'}">
    <div class="da-kpi-label">推广ROI</div>
    <div class="da-kpi-value">{roi:.2f}</div>
    <div class="da-kpi-sub">花费 &yen;{sm['adSpend']:,.2f}</div>
  </div>
  <div class="da-kpi-card">
    <div class="da-kpi-label">访客数</div>
    <div class="da-kpi-value">{sm['visitors']:,}</div>
    <div class="da-kpi-sub">买家 {sm['payers']:,} &middot; 转化率 {conv:.2f}%</div>
  </div>
  <div class="da-kpi-card">
    <div class="da-kpi-label">客单价</div>
    <div class="da-kpi-value">&yen;{aov:.2f}</div>
    <div class="da-kpi-sub">支付金额 &yen;{sm['payment']:,.2f}</div>
  </div>
</div>
"""

        template_footer = """</div><!-- .da-report -->"""

        full_html = template_header + _build_report_html(data, refund_rate, roi, conv, aov, warnings, insights, ai_available) + template_footer
        gen_by = 'ai' if ai_available else 'template'

        # 写入数据库（先删旧记录再插新记录，实现覆盖式更新）
        db_execute(
            'DELETE FROM daily_analysis_reports WHERE report_date = %s',
            [target_date], fetch=False
        )
        new_id = db_execute_insert(
            """INSERT INTO daily_analysis_reports (report_date, html_report, metrics_json, status, error_msg)
               VALUES (%s, %s, %s, %s, %s)""",
            [target_date, full_html, json.dumps(data, ensure_ascii=False),
             'success', '' if ai_available else 'DeepSeek API不可用或解析失败，已使用规则生成报告']
        )

        return success({
            'id': new_id,
            'reportDate': str(target_date),
            'report': full_html,
            'generatedBy': gen_by,
        }, '报告生成成功')

    except Exception as e:
        return fail(str(e))


@app.route('/api/analysis/report', methods=['GET'])
def get_analysis_report():
    """按日期获取已生成的报告，不传 date 则返回最新的"""
    try:
        report_date = request.args.get('date', '').strip()
        if not report_date:
            row = db_execute(
                "SELECT * FROM daily_analysis_reports WHERE status = 'success' ORDER BY report_date DESC LIMIT 1"
            )
        else:
            row = db_execute(
                'SELECT * FROM daily_analysis_reports WHERE report_date = %s AND status = %s',
                [report_date, 'success']
            )

        if not row:
            return fail('未找到该日期的分析报告', code=404)

        r = row[0]
        return success({
            'id': r['id'],
            'reportDate': str(r['report_date']),
            'report': r['html_report'],
            'createdAt': str(r['created_at']),
        })
    except Exception as e:
        return fail(str(e))


@app.route('/api/analysis/dates', methods=['GET'])
def get_analysis_dates():
    """返回已有报告的日期列表（近 60 条）"""
    try:
        rows = db_execute(
            "SELECT report_date, status, created_at FROM daily_analysis_reports ORDER BY report_date DESC LIMIT 60"
        )
        return success([{
            'date': str(r['report_date']),
            'status': r['status'],
            'createdAt': str(r['created_at']),
        } for r in rows])
    except Exception as e:
        return fail(str(e))


_DA_STANDALONE_CSS = """
@page{size:A4;margin:14mm 12mm}
*{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}
body{font-family:"Microsoft YaHei","微软雅黑",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#fff;margin:0;padding:0;color:#334155;line-height:1.7}
.da-page{max-width:980px;margin:0 auto}
.da-report{background:#fff;border-radius:16px;border:1px solid #eef2f7;box-shadow:0 8px 30px rgba(15,23,42,.06);overflow:hidden}
.da-header{display:flex;align-items:center;justify-content:space-between;padding:22px 28px;background:linear-gradient(135deg,#0f766e,#14b8a6 55%,#6366f1 130%);flex-wrap:wrap;gap:12px}
.da-date-badge{background:rgba(255,255,255,.18);color:#fff;padding:8px 18px;border-radius:99px;font-size:.9rem;font-weight:600}
.da-meta{display:flex;gap:18px;font-size:.78rem;color:rgba(255,255,255,.85)}
.da-kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;padding:24px 28px;background:#f8fafc;border-bottom:1px solid #eef2f7}
.da-kpi-card{padding:18px;border-radius:14px;background:#fff;border:1px solid #eef2f7;box-shadow:0 1px 3px rgba(15,23,42,.05);display:flex;flex-direction:column;gap:6px}
.da-kpi-good{border-left:4px solid #10b981}
.da-kpi-danger{border-left:4px solid #ef4444}
.da-kpi-label{font-size:.78rem;color:#64748b;font-weight:600}
.da-kpi-value{font-size:1.5rem;font-weight:800;color:#0f172a;font-variant-numeric:tabular-nums;line-height:1.15}
.da-kpi-sub{font-size:.75rem;color:#94a3b8}
.analysis-section{padding:26px 28px;border-bottom:1px solid #f1f5f9}
.analysis-section:last-child{border-bottom:none}
.analysis-section h3{font-size:1.08rem;font-weight:700;color:#0f172a;margin:0 0 16px}
.analysis-section p{font-size:.9rem;color:#475569;line-height:1.7;margin-bottom:12px}
.analysis-section p:last-child{margin-bottom:0}
.highlight{background:#fef3c7;padding:2px 8px;border-radius:6px;font-weight:700;color:#92400e}
.warn{color:#e11d48;font-weight:700}
.danger{color:#dc2626;font-weight:700;background:#fee2e2;padding:2px 8px;border-radius:6px}
.da-table{width:100%;border-collapse:collapse;font-size:.85rem;margin:12px 0;border-radius:10px;overflow:hidden;box-shadow:0 1px 2px rgba(15,23,42,.04)}
.da-table th{text-align:left;padding:11px 14px;background:#f1f5f9;color:#334155;font-weight:700;font-size:.78rem;border-bottom:2px solid #e2e8f0}
.da-table td{padding:11px 14px;border-bottom:1px solid #f1f5f9;color:#334155;font-variant-numeric:tabular-nums}
.da-table tbody tr:nth-child(even){background:#fafbfc}
.da-warn-list{list-style:none;padding:0;margin:10px 0}
.da-warn-list li{padding:10px 16px;border-radius:10px;margin-bottom:8px;font-size:.85rem;font-weight:600;line-height:1.5}
.da-warn-list .da-warn-warning{background:#fff7ed;color:#c2410c;border-left:4px solid #f97316}
.da-warn-list .da-warn-danger{background:#fef2f2;color:#dc2626;border-left:4px solid #ef4444}
"""


def _build_standalone_report(report_date, content):
    """把报告正文包装成独立的 HTML 文件"""
    import re
    # 去除 Font Awesome 图标（独立 HTML 无法加载图标字体）
    content = re.sub(r'<i class="fa-[^"]*"></i>', '', content)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每日数据分析报告 {report_date}</title>
<style>{_DA_STANDALONE_CSS}</style>
</head>
<body>
<div class="da-page">
{content}
</div>
</body>
</html>"""


def _find_browser():
    """定位 Chrome / Edge 可执行文件，用于无头渲染 PDF"""
    candidates = [
        os.environ.get('CHROME_PATH', ''),
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _html_to_pdf(html_content):
    """用无头 Chrome/Edge 把 HTML 渲染成 PDF 字节"""
    browser = _find_browser()
    if not browser:
        raise RuntimeError('未检测到 Chrome/Edge 浏览器，无法生成 PDF')

    fd_html, html_path = tempfile.mkstemp(suffix='.html', prefix='da_report_')
    fd_pdf, pdf_path = tempfile.mkstemp(suffix='.pdf', prefix='da_report_')
    os.close(fd_html)
    os.close(fd_pdf)
    try:
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        file_url = 'file:///' + html_path.replace('\\', '/')
        cmd = [
            browser,
            '--headless=new',
            '--disable-gpu',
            '--no-sandbox',
            '--no-pdf-header-footer',
            '--print-to-pdf=' + pdf_path,
            file_url,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)

        if not os.path.isfile(pdf_path) or os.path.getsize(pdf_path) == 0:
            raise RuntimeError('PDF 生成失败：' + (result.stderr.decode('utf-8', 'ignore')[:500] if result.stderr else '浏览器未输出文件'))

        with open(pdf_path, 'rb') as f:
            return f.read()
    finally:
        for p in (html_path, pdf_path):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass


@app.route('/api/analysis/download', methods=['GET'])
def download_analysis_report():
    """下载指定日期的分析报告为 PDF 文件"""
    try:
        report_date = request.args.get('date', '').strip()
        if not report_date:
            row = db_execute(
                "SELECT * FROM daily_analysis_reports WHERE status = 'success' ORDER BY report_date DESC LIMIT 1"
            )
        else:
            row = db_execute(
                'SELECT * FROM daily_analysis_reports WHERE report_date = %s AND status = %s',
                [report_date, 'success']
            )

        if not row:
            return fail('未找到该日期的分析报告', code=404)

        r = row[0]
        standalone_html = _build_standalone_report(str(r['report_date']), r['html_report'])
        pdf_bytes = _html_to_pdf(standalone_html)

        from flask import Response
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'attachment; filename="daily_analysis_{r["report_date"]}.pdf"',
            }
        )

    except Exception as e:
        return fail(str(e))


# ======================== 种草监测中台 ========================

_SEEDING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools')
_SEEDING_ACCOUNTS_FILE = os.path.join(_SEEDING_DIR, 'seeding_accounts.json')
_DOUYIN_COOKIE_FILE = os.path.join(_SEEDING_DIR, 'douyin_cookie.txt')
_DOUYIN_WORKS_CSV = os.path.join(_SEEDING_DIR, '_douyin_works.csv')
_XHS_COOKIE_FILE = os.path.join(_SEEDING_DIR, 'xhs_cookie.txt')
_XHS_WORKS_FILE = os.path.join(_SEEDING_DIR, '_xhs_works.json')


def _seeding_load_accounts():
    """读取种草账号列表；文件不存在或损坏时返回空列表"""
    if not os.path.exists(_SEEDING_ACCOUNTS_FILE):
        return []
    try:
        with open(_SEEDING_ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _seeding_save_accounts(accounts):
    with open(_SEEDING_ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)


# 作品数据 mock 用的样例账号与标题（数据库尚未建立，先用虚拟数据）
_SEEDING_SAMPLE_ACCOUNTS = [
    {'name': '聚浪好物研究所', 'douyinId': 'julang_haowu'},
    {'name': '聚浪美妆种草', 'douyinId': 'julang_meizhuang'},
    {'name': '聚浪户外测评', 'douyinId': 'julang_huwai'},
]
_SEEDING_SAMPLE_TITLES = [
    '这款防晒霜也太好用了，油皮闭眼入！',
    '姐妹们冲！这个精华平价又抗打',
    '秋冬必备的保湿面霜测评来啦',
    '被问爆的显白口红色号，真的绝',
    '学生党也能入的抗老水乳',
    '回购N次的宝藏洁面，性价比拉满',
    '这个遮瑕居然能扛住暴汗',
    '办公室人手一个的养生壶推荐',
    '熬夜党救星眼霜，黑眼圈退退退',
    '减脂期也能喝的奶茶替代来了',
]


def _seeding_mock_works():
    """生成作品数据的虚拟数据（按账号逐个展开，字段与页面表格一致）"""
    accounts = _seeding_load_accounts()
    if not accounts:
        accounts = _SEEDING_SAMPLE_ACCOUNTS
    works = []
    wid = 0
    for a in accounts:
        name = a.get('name') or a.get('douyinId') or '未命名账号'
        douyin_id = a.get('douyinId') or ''
        for j in range(4):
            wid += 1
            likes = 800 + ((wid * 137 + j * 233) % 90000)
            works.append({
                'id': wid,
                'name': name,
                'account': douyin_id,
                'title': _SEEDING_SAMPLE_TITLES[(wid * 7 + j) % len(_SEEDING_SAMPLE_TITLES)],
                'link': '',
                'likes': likes,
                'comments': likes // 23,
                'collects': likes // 11,
                'shares': likes // 37,
                'publishTime': (datetime.now() - timedelta(days=(wid % 20), hours=(j * 5 + wid) % 24)).strftime('%Y-%m-%d %H:%M'),
            })
    works.sort(key=lambda x: x['publishTime'], reverse=True)
    return works


@app.route('/api/seeding/accounts', methods=['GET'])
def seeding_list_accounts():
    """种草账号列表"""
    try:
        return success(_seeding_load_accounts())
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/accounts', methods=['POST'])
def seeding_create_account():
    """新增种草账号：平台 / 账号名称 / 抖音号·小红书号 / 主页链接 / 部门"""
    try:
        data = request.get_json(force=True)
        accounts = _seeding_load_accounts()
        new_id = max([int(a.get('id', 0)) for a in accounts], default=0) + 1
        acct = {
            'id': new_id,
            'platform': (data.get('platform') or 'douyin').strip(),
            'name': (data.get('name') or '').strip(),
            'douyinId': (data.get('douyinId') or data.get('douyin_id') or '').strip(),
            'homepage': (data.get('homepage') or '').strip(),
            'redId': (data.get('redId') or data.get('red_id') or '').strip(),
            'department': (data.get('department') or '').strip(),
        }
        accounts.append(acct)
        _seeding_save_accounts(accounts)
        return success(acct, '种草账号已添加')
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/accounts/<int:aid>', methods=['PUT'])
def seeding_update_account(aid):
    """更新种草账号"""
    try:
        data = request.get_json(force=True)
        accounts = _seeding_load_accounts()
        target = next((a for a in accounts if int(a.get('id', 0)) == aid), None)
        if target is None:
            return fail('账号不存在')
        target['name'] = (data.get('name') if data.get('name') is not None else target.get('name', '')).strip()
        target['platform'] = (data.get('platform') if data.get('platform') is not None else target.get('platform', 'douyin')).strip()
        target['douyinId'] = (data.get('douyinId') if data.get('douyinId') is not None else target.get('douyinId', '')).strip()
        target['homepage'] = (data.get('homepage') if data.get('homepage') is not None else target.get('homepage', '')).strip()
        target['redId'] = (data.get('redId') if data.get('redId') is not None else target.get('redId', '')).strip()
        target['department'] = (data.get('department') if data.get('department') is not None else target.get('department', '')).strip()
        _seeding_save_accounts(accounts)
        return success(target, '种草账号已更新')
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/accounts/<int:aid>', methods=['DELETE'])
def seeding_delete_account(aid):
    """删除种草账号"""
    try:
        accounts = _seeding_load_accounts()
        accounts = [a for a in accounts if int(a.get('id', 0)) != aid]
        _seeding_save_accounts(accounts)
        return success(None, '种草账号已删除')
    except Exception as e:
        return fail(str(e))


def _seeding_load_works_csv():
    """读取抓取脚本导出的作品 CSV；不存在或无数据时返回 None"""
    import csv as _csv
    if not os.path.exists(_DOUYIN_WORKS_CSV):
        return None
    rows = []
    try:
        with open(_DOUYIN_WORKS_CSV, 'r', encoding='utf-8-sig', newline='') as f:
            for r in _csv.DictReader(f):
                rows.append({
                    'id': len(rows) + 1,
                    'name': (r.get('名称') or '').strip(),
                    'account': (r.get('账号') or '').strip(),
                    'title': (r.get('标题') or '').strip(),
                    'link': (r.get('链接') or '').strip(),
                    'likes': int(r.get('点赞') or 0),
                    'comments': int(r.get('评论') or 0),
                    'collects': int(r.get('收藏') or 0),
                    'shares': int(r.get('分享') or 0),
                    'publishTime': (r.get('发布时间') or '').strip(),
                })
    except Exception as e:
        print(f'[种草] 读取作品 CSV 失败: {e}')
        return None
    return rows if rows else None


def _seeding_load_xhs_works():
    """读取小红书作品汇总 JSON（tools/_xhs_works.json）；不存在或无数据时返回空列表"""
    if not os.path.exists(_XHS_WORKS_FILE):
        return []
    try:
        with open(_XHS_WORKS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f'[种草] 读取小红书作品失败: {e}')
        return []


@app.route('/api/seeding/works', methods=['GET'])
def seeding_list_works():
    """作品数据列表：platform=douyin 读抖音 CSV，platform=xhs 读小红书 JSON；抖音无真实数据时回退虚拟数据"""
    try:
        platform = (request.args.get('platform') or 'douyin').strip()
        if platform == 'xhs':
            return success(_seeding_load_xhs_works())
        real = _seeding_load_works_csv()
        if real is not None:
            return success(real)
        return success(_seeding_mock_works())
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/works/meta', methods=['GET'])
def seeding_works_meta():
    """作品数据来源与最后抓取时间，供前端轮询判断抓取是否完成"""
    try:
        platform = (request.args.get('platform') or 'douyin').strip()
        if platform == 'xhs':
            mtime = os.path.getmtime(_XHS_WORKS_FILE) if os.path.exists(_XHS_WORKS_FILE) else None
            real = _seeding_load_xhs_works()
            source = 'real' if real else 'empty'
        else:
            mtime = os.path.getmtime(_DOUYIN_WORKS_CSV) if os.path.exists(_DOUYIN_WORKS_CSV) else None
            real = _seeding_load_works_csv()
            source = 'real' if real is not None else 'mock'
        return success({
            'mtime': mtime,
            'rows': len(real) if real else 0,
            'source': source,
        })
    except Exception as e:
        return fail(str(e))


def _seeding_cookie_file(platform):
    return _XHS_COOKIE_FILE if platform == 'xhs' else _DOUYIN_COOKIE_FILE


@app.route('/api/seeding/cookie', methods=['GET'])
def seeding_get_cookie():
    """读取当前平台 Cookie（抖音 douyin_cookie.txt / 小红书 xhs_cookie.txt）"""
    try:
        platform = (request.args.get('platform') or 'douyin').strip()
        cookie = ''
        path = _seeding_cookie_file(platform)
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                cookie = f.read().strip()
        return success({'cookie': cookie, 'exists': bool(cookie)})
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/cookie', methods=['POST'])
def seeding_save_cookie():
    """保存平台 Cookie"""
    try:
        data = request.get_json(force=True)
        platform = (data.get('platform') or 'douyin').strip()
        cookie = (data.get('cookie') or '').strip()
        with open(_seeding_cookie_file(platform), 'w', encoding='utf-8') as f:
            f.write(cookie)
        return success({'saved': True}, 'Cookie 已保存')
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/scrape', methods=['POST'])
def seeding_trigger_scrape():
    """按钮触发：后台异步执行作品抓取脚本（platform=douyin/xhs）"""
    try:
        import sys as _sys_scrape
        data = request.get_json(force=True) or {}
        platform = (data.get('platform') or 'douyin').strip()
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if platform == 'xhs':
            scraper = os.path.join(_SEEDING_DIR, 'xhs_batch.py')
            output_file = _XHS_WORKS_FILE
            if not os.path.isdir(os.path.join(_SEEDING_DIR, 'Spider_XHS')):
                return fail('缺少小红书抓取依赖：未找到 tools/Spider_XHS 目录，请先部署 Spider_XHS 开源项目')
        else:
            scraper = os.path.join(_SEEDING_DIR, 'douyin_video_scraper.py')
            output_file = _DOUYIN_WORKS_CSV
        if not os.path.exists(scraper):
            return fail('抓取脚本不存在：' + scraper)
        before_mtime = os.path.getmtime(output_file) if os.path.exists(output_file) else None
        log_file = open(os.path.join(_SEEDING_DIR, '_scrape_%s.log' % platform), 'w', encoding='utf-8')
        try:
            subprocess.Popen(
                [_sys_scrape.executable, scraper],
                cwd=project_root,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        finally:
            log_file.close()
        return success({'triggered': True, 'mtime': before_mtime}, '已触发抓取任务，正在后台执行')
    except Exception as e:
        return fail(str(e))


# ======================== 前端页面托管 ========================

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


@app.route('/')
def serve_index():
    """托管首页 index.html"""
    return send_from_directory(FRONTEND_DIR, 'index.html')


@app.route('/css/<path:filename>')
def serve_css(filename):
    """托管 CSS 文件"""
    return send_from_directory(os.path.join(FRONTEND_DIR, 'css'), filename)


@app.route('/js/<path:filename>')
def serve_js(filename):
    """托管 JS 文件"""
    return send_from_directory(os.path.join(FRONTEND_DIR, 'js'), filename)


# ======================== PaddleOCR 图片文字识别 ========================

import os
import re as _re
import threading
import traceback
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

from PIL import Image
import numpy as np
import cv2

# ==================== 全局单例 ====================
_paddle_ocr = None
_ocr_lock = threading.Lock()
_ocr_executor = ThreadPoolExecutor(max_workers=1)

# ==================== 预处理参数 ====================
_UPSCALE_THRESHOLD_SMALL  = 1000
_UPSCALE_THRESHOLD_MEDIUM = 1800
_DOWNSCALE_MAX_SIDE = 2000
_CLAHE_CLIP_LIMIT = 2.8
_CLAHE_TILE_SIZE  = (8, 8)
_BILATERAL_D_SMALL  = 5
_BILATERAL_D_LARGE  = 7
_SHARPEN_KERNEL = np.array([[0, -0.6, 0],
                            [-0.6, 3.4, -0.6],
                            [0, -0.6, 0]], dtype=np.float32)

# ==================== 纠错字典 ====================
_CORRECTION_RULES = [
    (r'(?<![a-zA-Z])5G5(?![a-zA-Z])', 'SGS'),
    (r'(?<![a-zA-Z])5GS(?![a-zA-Z])', 'SGS'),
    (r'(?<![a-zA-Z])SG5(?![a-zA-Z])', 'SGS'),
    (r'(?<![a-zA-Z])SAS(?![a-zA-Z])', 'SGS'),
    (r'(?<![a-zA-Z])S6S(?![a-zA-Z])', 'SGS'),
    (r'(?<![a-zA-Z])S65(?![a-zA-Z])', 'SGS'),
    (r'(?<![a-zA-Z])GNAS(?![a-zA-Z])', 'CNAS'),
    (r'(?<![a-zA-Z])CNA5(?![a-zA-Z])', 'CNAS'),
    (r'(?<![a-zA-Z])6NAS(?![a-zA-Z])', 'CNAS'),
    (r'(?<![a-zA-Z])GNA5(?![a-zA-Z])', 'CNAS'),
    (r'(?<![a-zA-Z])PCC(?![a-zA-Z])', 'PICC'),
    (r'(?<![a-zA-Z])P1CC(?![a-zA-Z])', 'PICC'),
    (r'(?<![a-zA-Z])PIC(?![a-zA-Z])', 'PICC'),
    (r'(?<![a-zA-Z])CCCAS(?![a-zA-Z])', 'CCCAT'),
    (r'(?<![a-zA-Z])CCCA5(?![a-zA-Z])', 'CCCAT'),
    (r'(?<![a-zA-Z])CCCA(?![a-zA-Z])', 'CCCAT'),
    (r'(?<![a-zA-Z])1SO(?![a-zA-Z])', 'ISO'),
    (r'(?<![a-zA-Z])IS0(?![a-zA-Z])', 'ISO'),
    (r'(?<![a-zA-Z])1S0(?![a-zA-Z])', 'ISO'),
    (r"""[?？''`'"'"]*\s*900[14]\s*[:;：，;,]\s*2000""", 'ISO 9001:2000'),
    (r'IS0\s*900[14]\s*[:;：，;,]\s*2000', 'ISO 9001:2000'),
    (r'1SO\s*900[14]\s*[:;：，;,]\s*2000', 'ISO 9001:2000'),
    (r'(?<![a-zA-Z])9004(?![0-9])', '9001'),
    (r'(?<![一-鿿])际互认(?![一-鿿])', '国际互认'),
    (r'(?<![一-鿿])迗口(?![一-鿿])', '进口'),
    (r'(\d+)%(\d+)', r'\1% \2'),
]


# ==================== OCR 引擎管理 ====================
def _get_ocr_reader():
    """线程安全的懒加载 PaddleOCR 单例"""
    global _paddle_ocr
    if _paddle_ocr is not None:
        return _paddle_ocr
    with _ocr_lock:
        if _paddle_ocr is not None:
            return _paddle_ocr
        os.environ['FLAGS_use_mkldnn'] = '0'
        from paddleocr import PaddleOCR
        print('[PaddleOCR] 正在加载模型...')
        _paddle_ocr = PaddleOCR(
            lang='ch',
            use_textline_orientation=True,
            enable_mkldnn=False,
        )
        print('[PaddleOCR] 模型加载完成')
    return _paddle_ocr


# ==================== 图像预处理 ====================
def _preprocess_image(img_bgr: np.ndarray) -> np.ndarray:
    """预处理管线：自适应放大 → 双边滤波 → CLAHE → 轻微锐化"""
    h, w = img_bgr.shape[:2]
    max_side = max(h, w)
    if max_side < _UPSCALE_THRESHOLD_SMALL:
        scale = 2.0
    elif max_side < _UPSCALE_THRESHOLD_MEDIUM:
        scale = 1.5
    else:
        scale = 1.0
        # 大图降采样，避免双边滤波/CLAHE 在全分辨率下过慢
        if max_side > _DOWNSCALE_MAX_SIDE:
            scale = _DOWNSCALE_MAX_SIDE / max_side
    if scale != 1.0:
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
        img_bgr = cv2.resize(img_bgr, None, fx=scale, fy=scale,
                             interpolation=interp)
    d = _BILATERAL_D_SMALL if max(img_bgr.shape[:2]) < 1500 else _BILATERAL_D_LARGE
    img_bgr = cv2.bilateralFilter(img_bgr, d=d, sigmaColor=50, sigmaSpace=50)
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=_CLAHE_CLIP_LIMIT, tileGridSize=_CLAHE_TILE_SIZE)
    l_ch = clahe.apply(l_ch)
    lab = cv2.merge([l_ch, a_ch, b_ch])
    img_bgr = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    img_float = img_bgr.astype(np.float32)
    img_float = cv2.filter2D(img_float, -1, _SHARPEN_KERNEL)
    img_bgr = np.clip(img_float, 0, 255).astype(np.uint8)
    return img_bgr


def _decode_base64_image(data_url: str, preprocess: bool = True):
    """从 base64 data URL 解码图片，返回 BGR numpy array（PaddleOCR 格式）"""
    import base64
    header, b64 = data_url.split(',', 1)
    img_bytes = base64.b64decode(b64)
    img = Image.open(BytesIO(img_bytes))
    if img.mode != 'RGB':
        img = img.convert('RGB')
    img_bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    if preprocess:
        img_bgr = _preprocess_image(img_bgr)
    return img_bgr


def _to_grayscale_binary(img_bgr: np.ndarray) -> np.ndarray:
    """灰度 + Otsu 二值化，用于复杂背景降级识别"""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


# ==================== 后处理 ====================
def _filter_ocr_results(lines):
    """过滤噪声（PaddleOCR 噪声远少于 EasyOCR）"""
    filtered = []
    for text, conf in lines:
        text = text.strip()
        if not text:
            continue
        if conf < 0.3:
            continue
        if len(set(text)) == 1 and len(text) >= 5:
            continue
        if not _re.search(r'[一-鿿㐀-䶿a-zA-Z0-9]', text):
            if conf < 0.5:
                continue
        if _re.fullmatch(r'\d{1,2}', text) and conf < 0.5:
            continue
        filtered.append((text, conf))
    return filtered


def _apply_corrections(text: str):
    """纠错字典后处理"""
    corrected = text
    changed = False
    for pattern, replacement in _CORRECTION_RULES:
        new_text = _re.sub(pattern, replacement, corrected)
        if new_text != corrected:
            changed = True
            corrected = new_text
    return corrected, changed


# ==================== PaddleOCR 结果解析 ====================
def _parse_paddle_result(result) -> list:
    """解析 PaddleOCR predict() 返回结果 → [(text, conf), ...]

    PaddleOCR 3.x 返回 OCRResult（dict 子类），直接通过 dict key 访问
    rec_texts / rec_scores，避免通过 .json 属性做昂贵的 deepcopy+序列化往返。
    """
    lines = []
    if not result:
        return lines
    for page in result:
        # PaddleOCR 3.x: OCRResult (dict 子类)，rec_texts 是直接 key
        if isinstance(page, dict):
            rec_texts = page.get('rec_texts', [])
            rec_scores = page.get('rec_scores', [])
            for i, text in enumerate(rec_texts):
                # return_word_box 模式下 text 可能是 (str, meta) 元组
                if isinstance(text, (tuple, list)):
                    text = text[0]
                conf = rec_scores[i] if i < len(rec_scores) else 0.9
                lines.append((str(text), float(conf)))
            continue
        # PaddleOCR 2.x / 旧格式: [[[box], (text, conf)], ...]
        if isinstance(page, list):
            for item in page:
                try:
                    if isinstance(item, list) and len(item) >= 2:
                        text_conf = item[1]
                        if isinstance(text_conf, (list, tuple)) and len(text_conf) >= 2:
                            lines.append((str(text_conf[0]), float(text_conf[1])))
                except (IndexError, TypeError):
                    continue
    return lines


# ==================== 核心识别逻辑 ====================
def _do_ocr_single(img_data: str) -> dict:
    """单张图片的完整识别流程"""
    reader = _get_ocr_reader()
    if reader is None:
        return {'text': '', 'confidence': 0, 'lines': 0, 'error': 'OCR引擎未就绪'}

    # 第一轮：预处理增强图
    img_bgr = _decode_base64_image(img_data, preprocess=True)
    raw_result = reader.predict(img_bgr)
    ocr_lines = _filter_ocr_results(_parse_paddle_result(raw_result))
    print(f'[OCR] 预处理图检出 {len(ocr_lines)} 条')

    fallback_used = False
    fallback_stage = None

    # 降级1：无结果 → 原图
    if not ocr_lines:
        print('[OCR] 预处理图无有效文字，降级为原图重试...')
        img_raw = _decode_base64_image(img_data, preprocess=False)
        raw_result_fb = reader.predict(img_raw)
        ocr_lines = _filter_ocr_results(_parse_paddle_result(raw_result_fb))
        print(f'[OCR] 原图检出 {len(ocr_lines)} 条')
        fallback_used = True
        fallback_stage = 'raw'

    # 降级2：仍无结果 → 灰度二值化
    if not ocr_lines:
        print('[OCR] 原图仍无有效文字，降级为灰度二值化重试...')
        img_raw = _decode_base64_image(img_data, preprocess=False)
        binary = _to_grayscale_binary(img_raw)
        raw_result_b = reader.predict(binary)
        ocr_lines = _filter_ocr_results(_parse_paddle_result(raw_result_b))
        print(f'[OCR] 二值化图检出 {len(ocr_lines)} 条')
        fallback_used = True
        fallback_stage = 'binary'

    # 输出
    lines_text = [text for text, _conf in ocr_lines]
    total_conf = sum(conf for _text, conf in ocr_lines)
    raw_text = '\n'.join(lines_text)
    corrected_text, was_corrected = _apply_corrections(raw_text)
    if was_corrected:
        print(f'[OCR] 纠错字典已应用')

    avg_conf = round(total_conf / len(lines_text), 2) if lines_text else 0

    # 调试日志：打印实际返回的文本内容（截断过长文本）
    preview = corrected_text[:120].replace('\n', '\\n')
    if len(corrected_text) > 120:
        preview += f'... (+{len(corrected_text) - 120}字)'
    print(f'[OCR] 返回文本: lines={len(lines_text)} conf={avg_conf} fallback={fallback_used} text="{preview}"')

    return {
        'text': corrected_text,
        'confidence': avg_conf,
        'lines': len(lines_text),
        'fallback': fallback_used,
        'fallback_stage': fallback_stage,
    }


# ==================== Flask 接口 ====================
@app.route('/api/ocr/detect', methods=['POST'])
def ocr_detect():
    """OCR 文字识别接口（PaddleOCR 引擎）
    请求: { images: ["data:image/png;base64,...", ...] }
    返回: { results: [{"text": "...", "confidence": 0.95, ...}, ...] }
    接口签名与 EasyOCR 版完全一致，前端无需改动。
    """
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': '请求体解析失败，可能因图片过多导致数据被截断', 'success': False, 'results': []}), 400
        images = data.get('images', [])
        if not images:
            return jsonify({'error': '未提供图片数据', 'success': False, 'results': []}), 400

        results = []
        for idx, img_data in enumerate(images):
            try:
                future = _ocr_executor.submit(_do_ocr_single, img_data)
                result = future.result(timeout=300)
                results.append(result)
            except Exception as e:
                traceback.print_exc()
                print(f'[OCR] 图片[{idx}] 识别异常: {e}')
                results.append({
                    'text': '',
                    'confidence': 0,
                    'lines': 0,
                    'error': str(e),
                })

        # 汇总日志
        total_lines = sum(r.get('lines', 0) for r in results)
        non_empty = sum(1 for r in results if r.get('text'))
        print(f'[OCR] 批次完成: {len(images)}张, {non_empty}张有文字, 共{total_lines}行')
        return jsonify({'results': results, 'success': True})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e), 'success': False}), 500


# ======================== 违规词检测引擎 ========================

def _normalize_text(text: str) -> str:
    """归一化文本，用于模糊匹配"""
    t = text
    t = _re.sub(r'(?<=[一-鿿㐀-䶿])\s+(?=[一-鿿㐀-䶿])', '', t)
    t = _re.sub(r'\s+', '', t)
    result = []
    for ch in t:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            result.append(' ')
        else:
            result.append(ch)
    t = ''.join(result)
    t = t.replace('​', '').replace('‌', '').replace('‍', '').replace('﻿', '')
    circled = '①②③④⑤⑥⑦⑧⑨⑩'
    for i, c in enumerate(circled):
        t = t.replace(c, str(i + 1))
    return t


def _build_pattern(word: str, match_type: str) -> str:
    """根据 match_type 构建正则表达式"""
    if match_type == 'exact':
        return _re.escape(word)
    elif match_type == 'fuzzy':
        chars = list(word)
        return r'\s*'.join(_re.escape(c) for c in chars)
    elif match_type == 'pattern':
        return word
    elif match_type == 'keyword':
        return _re.escape(word)
    elif match_type == 'prefix':
        return _re.escape(word) + r'\S*'
    elif match_type == 'suffix':
        return r'\S*' + _re.escape(word)
    else:
        return _re.escape(word)


@app.route('/api/violation/detect', methods=['POST'])
def violation_detect():
    """违规词检测接口"""
    try:
        data = request.get_json(silent=True) or {}
        text = data.get('text', '')
        if not text or not text.strip():
            return jsonify({'confirmed': [], 'suspected': [], 'success': True})

        rows = db_execute(
            'SELECT id, word, category, sub_category, match_type, severity '
            'FROM violation_words WHERE is_active = 1 ORDER BY CHAR_LENGTH(word) DESC'
        )
        if not rows:
            return jsonify({'confirmed': [], 'suspected': [], 'success': True})

        normalized = _normalize_text(text)
        confirmed = []
        suspected = []
        seen_original = set()
        seen_normalized = set()

        for rule in rows:
            word = rule['word']
            match_type = rule['match_type']
            severity = rule['severity']

            if match_type in ('exact', 'fuzzy', 'keyword'):
                try:
                    pattern = _build_pattern(word, match_type)
                    for m in _re.finditer(pattern, text, _re.IGNORECASE):
                        matched = m.group()
                        key = (m.start(), matched)
                        if key in seen_original:
                            continue
                        seen_original.add(key)
                        entry = {
                            'word': word,
                            'category': rule['category'],
                            'sub_category': rule['sub_category'] or '',
                            'severity': severity,
                            'match': matched,
                            'position': m.start(),
                            'length': len(matched),
                        }
                        if match_type == 'exact' or match_type == 'fuzzy':
                            confirmed.append(entry)
                        else:
                            suspected.append(entry)
                except _re.error:
                    continue

            elif match_type in ('prefix', 'suffix'):
                try:
                    pattern = _build_pattern(word, match_type)
                    for m in _re.finditer(pattern, normalized, _re.IGNORECASE):
                        matched = m.group()
                        key = (m.start(), matched)
                        if key in seen_normalized:
                            continue
                        seen_normalized.add(key)
                        suspected.append({
                            'word': word,
                            'category': rule['category'],
                            'sub_category': rule['sub_category'] or '',
                            'severity': severity,
                            'match': matched,
                            'position': m.start(),
                            'length': len(matched),
                        })
                except _re.error:
                    continue

            elif match_type == 'pattern':
                try:
                    for m in _re.finditer(word, text, _re.IGNORECASE):
                        matched = m.group()
                        key = (m.start(), matched)
                        if key in seen_original:
                            continue
                        seen_original.add(key)
                        suspected.append({
                            'word': word,
                            'category': rule['category'],
                            'sub_category': rule['sub_category'] or '',
                            'severity': severity,
                            'match': matched,
                            'position': m.start(),
                            'length': len(matched),
                        })
                except _re.error:
                    continue

        confirmed.sort(key=lambda x: (x['position'], x['length']))
        suspected.sort(key=lambda x: (x['position'], x['length']))

        return jsonify({
            'confirmed': confirmed,
            'suspected': suspected,
            'success': True,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e), 'success': False}), 500


# ======================== 智能助手聊天 ========================

@app.route('/api/ai/chat', methods=['POST'])
def ai_chat():
    """浪小助智能对话接口"""
    try:
        data = request.get_json(silent=True) or {}
        message = data.get('message', '').strip()
        if not message:
            return fail('请输入问题')

        history = data.get('history', [])
        # 构建对话消息
        messages = [{
            'role': 'system',
            'content': '你是"浪小助"，聚浪网络科技电商后台的AI智能助手。你的职责是：1) 解答电商运营相关问题（数据分析、营销策略、商品管理等）；2) 帮助用户理解系统功能和使用方法；3) 提供专业、简洁、友好的建议。回答风格：亲切专业，用中文，适当使用表情符号，控制在300字以内。'
        }]
        for h in history[-6:]:  # 最近6轮
            messages.append({'role': h.get('role', 'user'), 'content': h.get('content', '')})
        messages.append({'role': 'user', 'content': message})

        headers = {
            'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
            'Content-Type': 'application/json',
        }
        payload = {
            'model': DEEPSEEK_MODEL,
            'messages': messages,
            'temperature': 0.7,
            'max_tokens': 1024,
            'stream': False,
        }
        resp = requests.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=60)
        if resp.status_code == 200:
            result = resp.json()
            reply = result['choices'][0]['message']['content']
            return success({'reply': reply}, 'ok')
        else:
            return fail(f'AI服务异常 ({resp.status_code})')
    except Exception as e:
        traceback.print_exc()
        return fail(f'AI服务暂时不可用: {str(e)}')


# ======================== 订单详情 - 单链接销售数据 ========================

# 各平台对应数据库中的单链接数据表（字段与真实表列完全一致）
_OD_PLATFORMS = ['抖店', '京东', '千牛']

_OD_PLATFORM_META = {
    '抖店': {'table': '抖店单链接数据表', 'date_col': '统计周期', 'store_col': '店铺名',
             'name_col': '商品名称', 'id_col': '商品编码'},
    '京东': {'table': '京东单链接数据表', 'date_col': '时间', 'store_col': '店铺名',
             'name_col': 'SPU名称', 'id_col': 'SPU'},
    '千牛': {'table': '千牛单链接数据表', 'promo_table': '千牛单链接推广数据表',
             'date_col': '统计日期', 'store_col': '店铺名', 'name_col': '商品名称', 'id_col': '商品ID'},
}

# 全部平台聚合视图的“共同字段”（各平台列名不同，此处做语义映射）
_OD_COMMON_FIELDS = [
    {'key': 'date',           'label': '日期',     'type': 'date'},
    {'key': 'store_name',     'label': '店铺名',   'type': 'text'},
    {'key': 'link_id',        'label': '商品ID',   'type': 'text'},
    {'key': 'product_name',   'label': '商品名称', 'type': 'text'},
    {'key': 'payment_amount', 'label': '成交金额', 'type': 'money'},
    {'key': 'order_count',    'label': '订单数',   'type': 'int'},
    {'key': 'buyer_count',    'label': '买家数',   'type': 'int'},
    {'key': 'refund_amount',  'label': '退款金额', 'type': 'money'},
]

_OD_COMMON_MAP = {
    '抖店': {'date': '统计周期', 'store_name': '店铺名', 'link_id': '商品编码', 'product_name': '商品名称',
             'payment_amount': '成交金额', 'order_count': '成交订单数', 'buyer_count': '成交人数', 'refund_amount': '成交退款金额'},
    '京东': {'date': '时间', 'store_name': '店铺名', 'link_id': 'SPU', 'product_name': 'SPU名称',
             'payment_amount': '成交金额', 'order_count': '成交单量', 'buyer_count': '成交客户数', 'refund_amount': '取消及售后退款金额'},
    '千牛': {'date': '统计日期', 'store_name': '店铺名', 'link_id': '商品ID', 'product_name': '商品名称',
             'payment_amount': '支付金额', 'order_count': '支付件数', 'buyer_count': '支付买家数', 'refund_amount': '成功退款金额'},
}

# 比率/均摊类字段：多日区间聚合时取「单日数据相加 ÷ 天数」（平均），不直接求和。
_OD_AVERAGE_KEYWORDS = ('率', '占比', '转化', '费比', '价值', '单价', '成本', 'ROI', '人均', '平均')


def _od_is_average_field(name):
    """判断字段是否为比率/均摊类字段（聚合时求平均而非求和）"""
    return any(k in name for k in _OD_AVERAGE_KEYWORDS)


def _od_classify(name, ctype):
    """依据列名 + MySQL 列类型推断展示类型"""
    c = (ctype or '').lower()
    if 'int' in c:
        return 'int'
    if c.startswith('date') or c.startswith('datetime') or c.startswith('timestamp') or c.startswith('year'):
        return 'date'
    if 'decimal' in c or 'double' in c or 'float' in c:
        if any(k in name for k in ('率', '占比', '转化', '费比')):
            return 'pct'
        if any(k in name for k in ('金额', '支出', '价值', '客单价', '件单价', '花费', '补贴', '佣金', '消耗', '成本')):
            return 'money'
        return 'decimal'
    return 'text'


_od_fields_cache = {}


def _od_fields_for_table(table):
    """动态读取指定数据表字段，保证与数据库表结构完全一致（带缓存）"""
    if table in _od_fields_cache:
        return _od_fields_cache[table]
    fields = []
    try:
        cols = db_execute(f'SHOW FULL COLUMNS FROM `{table}`')
    except Exception:
        cols = []
    for c in cols:
        name = c.get('Field', '')
        if not name:
            continue
        fields.append({'key': name, 'label': name, 'type': _od_classify(name, c.get('Type', ''))})
    _od_fields_cache[table] = fields
    return fields


def _od_table_for(plat, link_type='all'):
    """根据平台与商品类型返回对应数据表名（千牛支持“推广商品”切到推广数据表）"""
    meta = _OD_PLATFORM_META[plat]
    if link_type == 'promo' and meta.get('promo_table'):
        return meta['promo_table']
    return meta['table']


def _od_fetch_rows(plat, start_date, end_date, store, search, link_type='all'):
    """查询某平台数据表，返回筛选后的行（字典列表）"""
    meta = _OD_PLATFORM_META[plat]
    table = _od_table_for(plat, link_type)
    where = []
    params = []
    if start_date:
        where.append(f"`{meta['date_col']}` >= %s")
        params.append(start_date)
    if end_date:
        where.append(f"`{meta['date_col']}` <= %s")
        params.append(end_date)
    if store:
        where.append(f"`{meta['store_col']}` = %s")
        params.append(store)
    if search:
        where.append(f"(`{meta['name_col']}` LIKE %s OR `{meta['id_col']}` LIKE %s)")
        params.append(f'%{search}%')
        params.append(f'%{search}%')
    sql = f"SELECT * FROM `{table}`"
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    return db_execute(sql, params)


def _od_to_common(plat, row):
    """将平台原始行映射为“全部平台”共同字段视图"""
    m = _OD_COMMON_MAP[plat]
    out = {'platform': plat}
    for ckey, col in m.items():
        out[ckey] = row.get(col)
    return out


def _od_promo_only_fields():
    """千牛推广表相对全部商品表独有的列（这些推广字段将合并到全部商品列表）"""
    meta = _OD_PLATFORM_META['千牛']
    all_cols = {f['key'] for f in _od_fields_for_table(meta['table'])}
    return [f for f in _od_fields_for_table(meta['promo_table']) if f['key'] not in all_cols]


def _od_qianniu_promo_map(start_date, end_date, store):
    """千牛推广数据按商品ID聚合，返回 {商品ID: {推广字段: 值}}。
    数值字段求和（比率/小数取均值），文本取首个，用于合并到全部商品列表。"""
    meta = _OD_PLATFORM_META['千牛']
    promo_fields = _od_fields_for_table(meta['promo_table'])
    id_col = meta['id_col']
    where, params = [], []
    if start_date:
        where.append(f"`{meta['date_col']}` >= %s")
        params.append(start_date)
    if end_date:
        where.append(f"`{meta['date_col']}` <= %s")
        params.append(end_date)
    if store:
        where.append(f"`{meta['store_col']}` = %s")
        params.append(store)
    sql = f"SELECT * FROM `{meta['promo_table']}`"
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    try:
        rows = db_execute(sql, params)
    except Exception:
        return {}

    ftype = {f['key']: f['type'] for f in promo_fields}
    groups = {}
    for r in rows:
        pid = str(r.get(id_col)) if r.get(id_col) is not None else ''
        g = groups.get(pid)
        if g is None:
            g = {'first': r, 'acc': {}, 'cnt': {}}
            groups[pid] = g
        for f in promo_fields:
            k = f['key']
            v = r.get(k)
            if v is None:
                continue
            if ftype[k] in ('money', 'int', 'pct', 'decimal'):
                try:
                    nv = float(v)
                except (TypeError, ValueError):
                    continue
                g['acc'][k] = g['acc'].get(k, 0.0) + nv
                g['cnt'][k] = g['cnt'].get(k, 0) + 1

    out = {}
    for pid, g in groups.items():
        row = {}
        for f in promo_fields:
            k = f['key']
            if k in g['acc']:
                t = ftype[k]
                if _od_is_average_field(k):
                    # 比率/均摊字段：单日数据相加 ÷ 天数
                    row[k] = round(g['acc'][k] / g['cnt'][k], 2)
                elif t == 'money':
                    row[k] = round(g['acc'][k], 2)
                elif t == 'int':
                    row[k] = int(round(g['acc'][k]))
                else:  # pct / decimal 取均值
                    row[k] = round(g['acc'][k] / g['cnt'][k], 2)
            else:
                row[k] = g['first'].get(k)
        out[pid] = row
    return out


def _od_serialize(row):
    """把 Decimal/date/datetime 转成可 JSON 序列化的类型"""
    out = {}
    for k, v in row.items():
        if v is None:
            out[k] = None
        elif isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (date, datetime)):
            out[k] = str(v)
        elif isinstance(v, bytes):
            out[k] = v.decode('utf-8', 'ignore')
        else:
            out[k] = v
    return out


def _od_sort_value(v):
    """排序键：数字按数值，其余按字符串；None 排最后"""
    if v is None:
        return (1, '')
    if isinstance(v, Decimal):
        return (0, float(v))
    if isinstance(v, (int, float)):
        return (0, v)
    return (1, str(v))


def _od_aggregate(rows, fields, start_date, end_date):
    """多日区间聚合：每条链接合并为一行，金额/整数求和，比率/均摊字段求平均（单日相加÷天数），
    文本取首个，日期显示区间。"""
    if not rows:
        return rows
    ftype = {f['key']: f['type'] for f in fields}
    date_keys = [f['key'] for f in fields if f['type'] == 'date']
    range_label = f'{start_date} ~ {end_date}'

    def _link_key(r):
        # 稳定标识：平台 + 链接ID（单平台视图用商品ID列，全部平台视图用 link_id）
        # 不能把所有文本字段都当分组键——跳出率/转化率等每天变化，会把同一链接拆成多行，
        # 导致按商品ID预汇总的推广字段被重复累加。
        plat = r.get('platform')
        if r.get('link_id') is not None:
            return (plat, str(r.get('link_id')))
        id_col = _OD_PLATFORM_META.get(plat, {}).get('id_col')
        return (plat, str(r.get(id_col) if r.get(id_col) is not None else ''))

    groups = {}  # 分组键 -> 聚合中间态
    for r in rows:
        key = _link_key(r)
        g = groups.get(key)
        if g is None:
            g = {'first': r, 'acc': {}, 'cnt': {}}
            groups[key] = g
        for f in fields:
            k = f['key']
            v = r.get(k)
            if v is None:
                continue
            t = ftype[k]
            if t in ('money', 'int', 'pct', 'decimal'):
                try:
                    nv = float(v)
                except (TypeError, ValueError):
                    continue
                g['acc'][k] = g['acc'].get(k, 0.0) + nv
                g['cnt'][k] = g['cnt'].get(k, 0) + 1

    out = []
    for g in groups.values():
        row = dict(g['first'])
        for f in fields:
            k = f['key']
            if k in date_keys:
                row[k] = range_label
                continue
            if k not in g['acc']:
                continue
            t = ftype[k]
            if _od_is_average_field(k):
                # 比率/均摊字段：单日数据相加 ÷ 天数
                row[k] = round(g['acc'][k] / g['cnt'][k], 2)
            elif t == 'money':
                row[k] = round(g['acc'][k], 2)
            elif t == 'int':
                row[k] = int(round(g['acc'][k]))
            else:  # pct / decimal：取均值
                row[k] = round(g['acc'][k] / g['cnt'][k], 2)
        out.append(row)
    return out


@app.route('/api/order-details/data', methods=['GET'])
def order_details_data():
    """单链接销售数据（读取真实数据表），支持分页/筛选/排序 + 自定义统计字段"""
    try:
        page = max(1, int(request.args.get('page', 1)))
        page_size = min(100, max(10, int(request.args.get('pageSize', 20))))
        start_date = request.args.get('start', '').strip()
        end_date = request.args.get('end', '').strip()
        platform = request.args.get('platform', '').strip()
        store = request.args.get('store', '').strip()
        search = request.args.get('search', '').strip()
        sort_by = request.args.get('sortBy', '')
        sort_dir = request.args.get('sortDir', 'desc')
        link_type = request.args.get('linkType', 'all').strip()
        if platform != '千牛':
            link_type = 'all'
        card_fields = [f for f in request.args.get('cardFields', '').split(',') if f]

        is_all = platform not in _OD_PLATFORMS

        # 千牛「全部商品」需要把推广数据表的推广字段合并进来（未推广的链接补 0）
        merge_promo = (platform == '千牛' and link_type == 'all')
        promo_fields = _od_promo_only_fields() if merge_promo else []

        if is_all:
            # 全部平台：聚合视图（共同字段）
            fields = _OD_COMMON_FIELDS
            rows = []
            for plat in _OD_PLATFORMS:
                for r in _od_fetch_rows(plat, start_date, end_date, store, search):
                    rows.append(_od_to_common(plat, r))
        else:
            # 指定平台：字段与数据表列完全一致
            fields = _od_fields_for_table(_od_table_for(platform, link_type))
            rows = []
            for r in _od_fetch_rows(platform, start_date, end_date, store, search, link_type):
                r['platform'] = platform
                rows.append(r)
            if merge_promo and promo_fields:
                fields = fields + promo_fields

        # 多日区间：按链接聚合，每条链接一行，避免「近7天/近30天」把同一链接拆成多行不同日期
        if start_date and end_date and start_date != end_date:
            rows = _od_aggregate(rows, fields, start_date, end_date)

        # 千牛「全部商品」：聚合后按商品ID左连接推广字段（先聚合再合并，避免多日期重复累计）
        if merge_promo and promo_fields:
            promo_map = _od_qianniu_promo_map(start_date, end_date, store)
            id_col = _OD_PLATFORM_META['千牛']['id_col']
            for r in rows:
                pid = str(r.get(id_col)) if r.get(id_col) is not None else ''
                promo = promo_map.get(pid) or {}
                for pf in promo_fields:
                    r[pf['key']] = promo.get(pf['key'], 0)

        total = len(rows)

        # 排序
        sortable = {f['key'] for f in fields}
        if sort_by not in sortable:
            sort_by = fields[0]['key'] if fields else ''
        if sort_by:
            rows.sort(key=lambda r: _od_sort_value(r.get(sort_by)), reverse=(sort_dir == 'desc'))

        # 卡片字段聚合（前端选中最多 4 个字段）
        card_summaries = []
        _field_map = {f['key']: f for f in fields}
        for key in card_fields:
            fdef = _field_map.get(key)
            if not fdef:
                continue
            vals = [r.get(key) for r in rows if r.get(key) is not None]
            if not vals:
                continue
            t = fdef['type']
            if t in ('money', 'int'):
                value = round(sum(float(v) for v in vals), 2)
                agg = 'sum'
            elif t in ('pct', 'decimal'):
                value = round(sum(float(v) for v in vals) / len(vals), 2)
                agg = 'avg'
            else:
                value = len(set(vals))
                agg = 'count'
            card_summaries.append({'key': key, 'label': fdef['label'], 'type': t, 'value': value, 'agg': agg})

        # 分页
        start_idx = (page - 1) * page_size
        page_data = [_od_serialize(r) for r in rows[start_idx:start_idx + page_size]]

        return success({
            'rows': page_data,
            'total': total,
            'page': page,
            'pageSize': page_size,
            'totalPages': max(1, (total + page_size - 1) // page_size),
            'cardSummaries': card_summaries,
            'fields': fields,
            'platforms': _OD_PLATFORMS,
        })
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


@app.route('/api/order-details/stores', methods=['GET'])
def order_details_stores():
    """返回指定平台的店铺列表（读取真实数据表）"""
    try:
        platform = request.args.get('platform', '').strip()
        link_type = request.args.get('linkType', 'all').strip()
        if platform != '千牛':
            link_type = 'all'
        if platform in _OD_PLATFORMS:
            meta = _OD_PLATFORM_META[platform]
            table = _od_table_for(platform, link_type)
            rows = db_execute(f"SELECT DISTINCT `{meta['store_col']}` AS s FROM `{table}` ORDER BY `{meta['store_col']}`")
            return success([r['s'] for r in rows])
        seen = set()
        stores = []
        for plat in _OD_PLATFORMS:
            meta = _OD_PLATFORM_META[plat]
            try:
                rows = db_execute(f"SELECT DISTINCT `{meta['store_col']}` AS s FROM `{meta['table']}`")
            except Exception:
                rows = []
            for r in rows:
                s = r['s']
                if s not in seen:
                    seen.add(s)
                    stores.append(s)
        return success(sorted(stores))
    except Exception as e:
        return fail(str(e))


# ======================== 选品助手 ========================

def _ps_parse_float(s):
    """把字符串解析为 float，失败返回 None"""
    if s is None or s == '':
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


@app.route('/api/product-selection/tmall', methods=['GET'])
def product_selection_tmall():
    """天猫榜单：读取「天猫榜单表」，支持价格区间筛选"""
    try:
        min_price = _ps_parse_float(request.args.get('minPrice', '').strip())
        max_price = _ps_parse_float(request.args.get('maxPrice', '').strip())

        conditions = []
        params = []
        if min_price is not None:
            conditions.append('价格 >= %s')
            params.append(min_price)
        if max_price is not None:
            conditions.append('价格 <= %s')
            params.append(max_price)

        where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

        rows = db_execute(f"""
            SELECT 类别名, 排行榜名, 产品名, 价格, 日期
            FROM 天猫榜单表
            {where_clause}
            ORDER BY 日期 DESC, 价格 ASC
        """, params)
        _serialize_rows(rows)

        # 返回全表价格区间，供前端展示 / 占位提示
        stats = db_execute("SELECT MIN(价格) AS min_p, MAX(价格) AS max_p FROM 天猫榜单表")
        price_range = None
        if stats and stats[0].get('min_p') is not None:
            price_range = {'min': float(stats[0]['min_p']), 'max': float(stats[0]['max_p'])}

        return success({'items': rows, 'total': len(rows), 'priceRange': price_range})
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


@app.route('/api/product-selection/douyin', methods=['GET'])
def product_selection_douyin():
    """抖音热搜榜：读取「抖音热搜品类表」（筛选后的电商热搜），支持日期选择"""
    try:
        selected_date = request.args.get('date', '').strip()

        date_rows = db_execute("SELECT DISTINCT 日期 FROM 抖音热搜品类表 ORDER BY 日期 DESC")
        dates = [str(r['日期']) for r in date_rows]

        conditions = []
        params = []
        if selected_date:
            conditions.append('日期 = %s')
            params.append(selected_date)
        elif dates:
            # 未指定日期时默认展示最新一天
            conditions.append('日期 = %s')
            params.append(dates[0])

        where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

        rows = db_execute(f"""
            SELECT 热搜名, 热搜值, 品类, 日期
            FROM 抖音热搜品类表
            {where_clause}
        """, params)
        # 按热度高低排序（把「热搜值」字符串解析为数值后降序）
        rows.sort(key=lambda r: _dy_parse_heat(r['热搜值']), reverse=True)
        # 去重：同一「热搜名+日期」可能因重复导入出现多行，榜单只保留一条
        seen = set()
        unique_rows = []
        for r in rows:
            key = (r['热搜名'], str(r['日期']))
            if key in seen:
                continue
            seen.add(key)
            unique_rows.append(r)
        _serialize_rows(unique_rows)

        return success({'items': unique_rows, 'total': len(unique_rows), 'dates': dates})
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


@app.route('/api/product-selection/douyin/raw-dates', methods=['GET'])
def product_selection_douyin_raw_dates():
    """返回「抖音热搜榜单表」（原始热搜）的日期列表，供筛选按钮选择要筛选的日期"""
    try:
        date_rows = db_execute("SELECT DISTINCT 日期 FROM 抖音热搜榜单表 ORDER BY 日期 DESC")
        dates = [str(r['日期']) for r in date_rows]
        return success({'dates': dates})
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


def _as_parse_num(s):
    """解析爱搜人次字符串（如 '8081.47w'、'平均:10.76w'）为数值，仅用于排序"""
    if s is None:
        return 0.0
    m = re.search(r'([\d.]+)\s*([wW万亿]?)', str(s))
    if not m:
        return 0.0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0.0
    unit = m.group(2)
    if unit == '亿':
        return n * 1e8
    if unit in ('w', 'W', '万'):
        return n * 1e4
    return n


@app.route('/api/product-selection/aisou', methods=['GET'])
def product_selection_aisou():
    """爱搜数据：读取「爱搜数据表」，支持日期选择 + 搜索词关键词模糊搜索"""
    try:
        selected_date = request.args.get('date', '').strip()
        keyword = request.args.get('keyword', '').strip()

        date_rows = db_execute("SELECT DISTINCT 日期 FROM 爱搜数据表 ORDER BY 日期 DESC")
        dates = [str(r['日期']) for r in date_rows]

        conditions = []
        params = []
        if selected_date and selected_date != 'all':
            conditions.append('日期 = %s')
            params.append(selected_date)
        elif not selected_date and dates:
            # 未指定日期时默认展示最新一天
            selected_date = dates[0]
            conditions.append('日期 = %s')
            params.append(selected_date)
        if keyword:
            conditions.append('搜索词关键词 LIKE %s')
            params.append('%' + keyword + '%')

        where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

        rows = db_execute(f"""
            SELECT 日期, 搜索词关键词, 搜索词月覆盖人次, 搜索词七日搜索人次,
                   电商词关键词, 电商词月覆盖人次, 电商词七日搜索人次
            FROM 爱搜数据表
            {where_clause}
        """, params)
        # 注意：fetchall() 在 0 行时返回空元组，包一层 list 统一成 list
        rows = list(rows)

        # 链式稳定排序（由次要键到主要键）：日期倒序 → 搜索词热度降序 → 搜索词名 → 电商词热度降序，
        # 使同一搜索词下的电商词连续排列，便于查看
        rows.sort(key=lambda r: -_as_parse_num(r['电商词月覆盖人次']))
        rows.sort(key=lambda r: r['搜索词关键词'])
        rows.sort(key=lambda r: -_as_parse_num(r['搜索词月覆盖人次']))
        rows.sort(key=lambda r: str(r['日期']), reverse=True)
        _serialize_rows(rows)

        return success({
            'items': rows,
            'total': len(rows),
            'dates': dates,
            'date': selected_date,
            'keyword': keyword,
        })
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


# ======================== 选品助手智能体（RAG） ========================
# 知识库：抖音热搜品类表（筛选后的电商热搜）→ 天猫榜单表 → 爱搜数据表。
# 流程：从抖音已筛品类表取品类/热搜词 → 用词在天猫榜单与爱搜表中检索对应数据
# → 拼成上下文交给 DeepSeek 生成「分析文字 + 推荐卡片」。


def _ps_agent_parse_cards(raw):
    """从 DeepSeek 返回文本中提取推荐卡片 JSON 数组；失败返回 []"""
    if not raw:
        return []
    s = raw.strip()
    # 去掉 markdown 代码围栏
    s = re.sub(r'```(?:json)?', '', s).strip()
    candidates = [s]
    # 截取第一个 '[' 到最后一个 ']' 之间的内容
    i = s.find('[')
    j = s.rfind(']')
    if i != -1 and j > i:
        candidates.append(s[i:j + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, list):
                return [c for c in data if isinstance(c, dict)]
            if isinstance(data, dict) and isinstance(data.get('cards'), list):
                return [c for c in data['cards'] if isinstance(c, dict)]
        except Exception:
            continue
    return []


# 检索停用词：热搜短语中剔除这些弱语义词，避免切出大量无效关键词
_PS_AGENT_STOP = {
    '高级感', '2026', '新款', '爆款', '必备', '推荐', '专用', '神器', '网红',
    '大容量', '超轻', '加厚', '防', '最', '好物', '好吃', '排行', '热门',
    '最新', '流行', '百搭', '高端', '小众', '炸街', '大方得体', '轻奢',
    '女装', '男装', '童装', '品牌', '官方', '旗舰', '同款',
}

_PS_AGENT_STOP_4 = {'高级感', '大方得体', '轻奢', '小众', '炸街'}


def _ps_agent_keywords(terms, max_kw=80):
    """把抖音热搜短语切分成检索关键词片段（优先完整词与 3 字片段，不足再补 2 字片段）。
    返回按长度降序、去重后的关键词列表，控制数量避免 SQL 过长。"""
    kws = []
    seen = set()

    def add(k):
        k = k.strip()
        if not k or k in seen:
            return
        seen.add(k)
        kws.append(k)

    # 先放完整热搜词（短词可直接匹配）
    for t in sorted(terms, key=lambda x: -len(x)):
        if len(t) <= 6:
            add(t)

    # 切分：按停用词切割，取子片段；再补 3 字、2 字滑窗
    for t in terms:
        # 用停用词切分短语
        parts = [t]
        for sw in sorted(_PS_AGENT_STOP, key=lambda x: -len(x)):
            new_parts = []
            for p in parts:
                new_parts.extend(p.split(sw))
            parts = [x for x in new_parts if x]
        for p in parts:
            p = p.strip()
            if 2 <= len(p) <= 8:
                add(p)
            elif len(p) > 8:
                # 长片段继续切 3 字滑窗
                for i in range(len(p) - 2):
                    seg = p[i:i + 3]
                    if seg not in _PS_AGENT_STOP:
                        add(seg)
        # 补充 3 字滑窗
        if len(t) >= 3:
            for i in range(len(t) - 2):
                seg = t[i:i + 3]
                if seg not in _PS_AGENT_STOP:
                    add(seg)

    # 数量足够则去掉 2 字片段，减少噪音
    result = sorted(kws, key=lambda x: -len(x))
    if len([k for k in result if len(k) >= 3]) >= 40:
        result = [k for k in result if len(k) >= 3]
    return result[:max_kw]


@app.route('/api/product-selection/agent', methods=['POST'])
def product_selection_agent():
    """选品助手智能体：检索三张知识库表 + DeepSeek 分析"""
    try:
        payload = request.get_json(silent=True) or {}
        question = (payload.get('question') or '').strip()
        if not question:
            return fail('请输入分析需求')

        # ---- 1. 抖音已筛选电商热搜（知识库入口，读「抖音热搜品类表」） ----
        dy_rows = db_execute(
            "SELECT 热搜名, 热搜值, 品类, 日期 FROM `抖音热搜品类表` WHERE `是否电商` = 1 ORDER BY `热度数值` DESC LIMIT 200")
        dy_rows = list(dy_rows)
        dy_items = []
        seen = set()
        for r in dy_rows:
            term = (r['热搜名'] or '').strip()
            if not term or term in seen:
                continue
            seen.add(term)
            dy_items.append({
                'term': term,
                'heat': r['热搜值'],
                'category': r['品类'],
                'date': str(r['日期']) if r['日期'] else '',
            })

        # 检索关键词：把热搜短语切分成短关键词片段（完整短语直接 LIKE 命中率太低）
        terms = [d['term'] for d in dy_items]
        keywords = _ps_agent_keywords(terms)

        # ---- 2. 天猫榜单：按关键词匹配 产品名/类别名/排行榜名；同产品名只保留价格最低那条 ----
        tmall = []
        if keywords:
            ors = []
            params = []
            for k in keywords:
                like = '%' + k + '%'
                ors.append('(产品名 LIKE %s OR 类别名 LIKE %s OR 排行榜名 LIKE %s)')
                params.extend([like, like, like])
            where = ' OR '.join(ors)
            tmall_rows = db_execute(
                f"SELECT 类别名, 排行榜名, 产品名, 价格, 日期 FROM `天猫榜单表` WHERE {where} LIMIT 400",
                params)
            tmall_rows = list(tmall_rows)
            # 按产品名去重，保留价格最低的一条（价格缺省视为最高，优先被替换）
            best = {}
            for r in tmall_rows:
                name = r['产品名']
                price = float(r['价格']) if r['价格'] is not None else None
                if name not in best or (price is not None and (best[name]['价格'] is None or price < best[name]['价格'])):
                    best[name] = {
                        '类别名': r['类别名'], '排行榜名': r['排行榜名'], '产品名': r['产品名'],
                        '价格': price,
                        '日期': str(r['日期']) if r['日期'] else '',
                    }
            tmall = list(best.values())

        # ---- 3. 爱搜数据：按关键词匹配 搜索词关键词/电商词关键词 ----
        aisou = []
        aisou_seen = set()
        if keywords:
            ors = []
            params = []
            for k in keywords:
                like = '%' + k + '%'
                ors.append('(搜索词关键词 LIKE %s OR 电商词关键词 LIKE %s)')
                params.extend([like, like])
            where = ' OR '.join(ors)
            aisou_rows = db_execute(
                f"SELECT 日期, 搜索词关键词, 搜索词月覆盖人次, 搜索词七日搜索人次, "
                f"电商词关键词, 电商词月覆盖人次, 电商词七日搜索人次 FROM `爱搜数据表` WHERE {where} LIMIT 200",
                params)
            aisou_rows = list(aisou_rows)
            for r in aisou_rows:
                key = (r['搜索词关键词'], r['电商词关键词'], str(r['日期']))
                if key in aisou_seen:
                    continue
                aisou_seen.add(key)
                aisou.append({
                    '日期': str(r['日期']) if r['日期'] else '',
                    '搜索词关键词': r['搜索词关键词'],
                    '搜索词月覆盖人次': r['搜索词月覆盖人次'],
                    '搜索词七日搜索人次': r['搜索词七日搜索人次'],
                    '电商词关键词': r['电商词关键词'],
                    '电商词月覆盖人次': r['电商词月覆盖人次'],
                    '电商词七日搜索人次': r['电商词七日搜索人次'],
                })

        # ---- 4. 拼接上下文 → DeepSeek ----
        dy_dates = sorted({d['date'] for d in dy_items if d['date']}, reverse=True)
        date_label = dy_dates[0] if dy_dates else '（无）'

        context = {
            '日期': date_label,
            '抖音电商热搜榜单': dy_items[:80],
            '天猫榜单匹配': tmall[:80],
            '爱搜数据匹配': aisou[:60],
        }
        ctx_json = json.dumps(context, ensure_ascii=False)

        sys_p = (
            '# 角色定义\n'
            '你是"选品助手智能体"，一名专业的电商选品分析师，服务于同时经营抖音和天猫店铺的商家。'
            '你只基于给定的知识库数据做分析，绝不编造。\n\n'
            '# 数据来源与结构\n'
            '你会收到一份结构化的知识库检索结果（JSON），包含以下三部分，它们通过同一批电商关键词串联：\n\n'
            '1. 抖音电商热搜（数组，字段：term热搜词、heat热搜值、category品类、date日期）\n'
            '   - 含义：反映需求侧趋势与热度\n'
            '   - 注意：本数据无"排名"字段，热度高低仅用 heat（热搜值）判断，值越大热度越高\n'
            '   - 注意：若某词在多条数据中出现（当前多为单日），仅作为热度参考，不必强判"持续热度"\n\n'
            '2. 爱搜搜索数据（数组，字段：日期、搜索词关键词、搜索词月覆盖人次、搜索词七日搜索人次、电商词关键词、电商词月覆盖人次、电商词七日搜索人次）\n'
            '   - 含义：反映用户主动搜索该词的真实购买意图强度\n'
            '   - 核心指标是"搜索词月覆盖人次"（如 4161.77w），越大购买意图越真实\n'
            '   - 注意：爱搜数据无排名信息，禁止在输出中编造排名\n\n'
            '3. 天猫榜单匹配数据（数组，字段：类别名、排行榜名、产品名、价格、日期）\n'
            '   - 含义：反映该词对应商品在天猫的供给侧竞争格局\n'
            '   - 注意：同一"产品名"可能出现在多个排行榜中，请按产品名去重后再统计\n\n'
            '# 数据匹配规则\n'
            '- 三张表通过同一电商关键词串联，不需要做品类映射\n'
            '- 天猫匹配方式：电商关键词出现在"排行榜名"或"产品名"中即视为匹配\n'
            '- 若某关键词在三张表中只命中部分数据源，正常分析，并在结论中说明哪部分数据缺失\n'
            '- 若用户询问的产品/关键词完全不在知识库中（三张表均无匹配），不要拒绝回答或只说"无数据"：\n'
            '  改为基于你的电商市场分析能力，给出该产品的市场选品分析（需求趋势、目标人群、竞争格局判断、切入机会、裂变产品等），\n'
            '  并在分析开头明确标注「该产品不在当前知识库，以下为基于市场认知的分析，建议后续抓取数据验证」\n\n'
            '# 分析框架（严格按此顺序执行）\n\n'
            '## 第一步：热度评估\n'
            '基于抖音电商热搜数据判断每个词的热度：\n'
            '- 热搜值越高 = 曝光量越大、热度越高\n'
            '- 同义词簇可合并看热度\n\n'
            '## 第二步：搜索意图验证\n'
            '基于爱搜数据判断用户真实购买意图：\n'
            '- 搜索词月覆盖人次越高 = 主动搜索购买的用户越多 = 需求越真实\n'
            '- 同一词同时出现在抖音热搜和爱搜中 = 强信号（有曝光 + 有购买意图）\n'
            '- 仅在抖音热搜出现而爱搜无数据 = 有热度但购买意图待验证，标注提醒\n\n'
            '## 第三步：竞争格局分析\n'
            '基于天猫匹配数据分析供给侧：\n'
            '- 匹配到的产品数量越多 = 该词对应品类竞争越激烈\n'
            '- 从原始"价格"字段统计价格分布（最低价、最高价、大致价位段），只引用原始价格，不做任何计算或估算\n'
            '- 指出价格空档：哪个价位段几乎没有产品覆盖\n'
            '- 匹配到的排行榜数量越多 = 该品类在天猫越成熟\n\n'
            '## 第四步：综合排序\n'
            '对所有关键词按以下逻辑排序：\n'
            '1. 三张表全部命中 > 命中两张 > 仅命中一张\n'
            '2. 抖音热搜值高 + 爱搜月覆盖人次高 = 优先\n'
            '3. 天猫匹配产品数少（竞争低）+ 有价格空档 = 加分\n'
            '4. 天猫匹配产品数多但价格分布分散 = 仍有切入机会\n\n'
            '## 第五步：裂变产品衍生\n'
            '针对每个值得关注的主品/主关键词，衍生出 1~2 个靠谱的裂变产品，衍生方向包括：\n'
            '- 配套耗材：如「拼豆」→「拼豆板」「拼豆镊子」「拼豆图纸」\n'
            '- 工具配件：如「空气炸锅」→「空气炸锅专用锡纸/纸托」\n'
            '- 场景延伸：如「露营车」→「露营桌板」「车顶行李架」\n'
            '- 细分人群/功能：如「内衣」→「聚拢防下垂内衣」「运动无痕内衣」\n'
            '裂变规则：\n'
            '- 不依赖知识库已有数据，由你基于电商市场分析能力独立判断裂变方向（从品类特性、使用场景、配套关系、耗材复购、人群细分等角度）\n'
            '- 裂变品须是市场上已被验证、消费者普遍认可的品类或形态，不要编造虚构的规格或不存在的新概念\n'
            '- 每个主品尽量给出 1~2 个靠谱裂变品，宁缺毋滥\n\n'
            '# 输出格式（严格遵守）\n\n'
            '## 第一部分：分析结论\n'
            '标题用"## 分析结论"，中文自然语言，分要点，150~300字，包含：\n'
            '- 当前最值得关注的选品方向（1~2句）\n'
            '- 竞争格局与价格机会判断（1~2句）\n'
            '- 具体行动建议（1~2句）\n'
            '- 若某数据源缺失，明确说明"该词在XX平台暂无匹配数据"\n\n'
            '## 第二部分：推荐卡片\n'
            '紧接一个 JSON 数组（用 ```json 代码块包裹），每个元素字段如下：\n'
            '{"type":"tmall"|"aisou","title":"卡片标题","subtitle":"说明","metric":"关键数值","tags":"逗号分隔标签","reason":"一句话推荐理由","derived":"裂变产品，逗号分隔"}\n\n'
            '卡片生成规则：\n'
            '- tmall 卡片必须来自天猫榜单匹配数据：title=产品名，subtitle=类别名+排行榜名，metric=价格(带¥)，type="tmall"\n'
            '- aisou 卡片必须来自爱搜数据：title=搜索词关键词，subtitle=电商词关键词，metric=月覆盖人次(带单位)，type="aisou"\n'
            '- 只挑数据中真实存在、最有选品价值的 3~8 条\n'
            '- 两类卡片尽量均衡；若某类无匹配数据则只输出另一类，绝不编造\n'
            '- 同一产品名只出现一次（去重）\n'
            '- tags 须包含关键标签，如：蓝海/红海、趋势上升/平稳、价格空档等\n'
            '- derived 列出该主品/关键词的裂变产品（1~2 个，逗号分隔，中文），没有靠谱裂变方向可留空字符串\n\n'
            '## 第三部分：后续引导\n'
            '在 JSON 代码块之后，用一句话引导用户下一步操作，例如：\n'
            '- "想看这个词在天猫的完整价格带分布，可以继续问我"\n'
            '- "需要我对比这几个方向的竞争情况吗？"\n\n'
            '# 硬性约束\n'
            '- 知识库命中的数据必须真实引用，严禁编造不存在的商品、数字或关键词\n'
            '- 裂变产品（derived）基于市场分析独立产出，不属于知识库数据，但仍是市场上真实存在的品类，不得虚构\n'
            '- 知识库外的产品分析属市场认知输出：可给出合理的市场判断与品类级建议，但必须明确标注，且不得虚构具体销量/价格等硬数据\n'
            '- 若某数据源无匹配结果，在分析结论中明确说明"该词在XX平台暂无匹配数据"\n'
            '- 涉及具体品牌名时仅作为竞争格局参考展示，不做品牌推荐或贬低\n'
            '- 价格数据直接引用原始数据，不做计算或估算\n'
            '- 爱搜数据中不存在排名信息，不要在输出中编造排名'
        )
        user_msg = f'知识库检索结果（JSON）：\n{ctx_json}\n\n用户需求：{question}\n\n请按要求输出分析结论和推荐卡片。'

        raw = call_deepseek_api(sys_p, user_msg, temperature=0.4, max_tokens=4096,
                                api_key=DEEPSEEK_SELECTION_API_KEY)

        # 分离分析文字与卡片 JSON
        cards = _ps_agent_parse_cards(raw)
        # 分析文字：去掉 JSON 数组块和代码围栏，保留「分析结论」与「后续引导」两部分正文
        analysis = raw or ''
        if raw:
            # 去掉 ```json ... ``` 代码围栏（连同内部的 JSON 数组）
            analysis = re.sub(r'```json\s*\[.*?\]\s*```', '', analysis, flags=re.DOTALL)
            # 去掉残留的裸 JSON 数组
            analysis = re.sub(r'\[\s*\{.*?\}\s*(?:,\s*\{.*?\}\s*)*\]', '', analysis, flags=re.DOTALL)
            # 去掉残留的 markdown 围栏
            analysis = re.sub(r'```[a-zA-Z]*', '', analysis)
            analysis = analysis.strip()

        return success({
            'analysis': analysis,
            'cards': cards,
            'meta': {
                'date': date_label,
                'douyin_count': len(dy_items),
                'tmall_count': len(tmall),
                'aisou_count': len(aisou),
            },
            'raw_available': bool(raw),
        }, 'ok')
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


# ======================== 抖音热搜 → 电商热搜筛选 ========================
# 筛选逻辑与「抖音热搜词筛选 skill」保持一致：命中品类关键词即保留，
# 命中黑名单 / 超长 / 网红非商品则删除，其余（未命中品类）视为非电商。

# 精确匹配黑名单：整个热搜词完全等于才删除（避免短词误杀）
_DY_BLACKLIST_EXACT = {"声明", "后果", "吗"}

# 子串匹配黑名单：热搜词中包含即删除
_DY_BLACKLIST_SUBSTR = [
    "明星", "电视剧", "综艺", "电影", "短剧", "剧集", "主演",
    "代言人", "代言", "演唱会", "选秀", "爱豆", "追星", "恋情",
    "离婚", "绯闻", "番剧",
    "游戏", "王者荣耀", "手游", "网游", "端游",
    "团购", "拼团", "主播", "股票", "股价", "酒店", "事件",
    "是什么意思", "仅退款", "人民锐评",
    "金价", "黄金价", "黄金价格", "今日黄金", "黄金走势",
    "黄金今日价", "黄金大盘价", "黄金多少钱", "5斤黄金",
    "大盘价", "黄金鸟窝", "黄金大盘", "黄金回收",
    "华为", "小米", "苹果", "oppo", "vivo", "iphone",
    "救援", "搭电", "广场舞", "音乐种草", "沙鹰", "donk",
    "寡人", "放冰箱", "坦克", "女扮男装", "外卖", "速运",
    "代驾", "京东健康", "鞋厂", "失火", "附近美食", "美团来电",
    "钟丽缇", "的做法",
]

# 正向关键词：命中即保留
_DY_CATS = {
    "服饰鞋包": ["衣服", "服装", "服饰", "女装", "男装", "童装", "裤子",
                "裙子", "外套", "卫衣", "t恤", "衬衫", "鞋", "运动鞋",
                "凉鞋", "靴子", "包包", "背包", "帽子", "围巾", "内衣", "袜子"],
    "食品饮料": ["食品", "饮料", "咖啡", "水果", "坚果",
                "辣条", "巧克力", "饼干", "方便面", "螺蛳粉", "自热",
                "白酒", "啤酒", "红酒", "葡萄酒", "喝酒", "酒馆", "茶叶",
                "大米", "糖", "饮品", "小吃", "美食"],
    "家电家居": ["家电", "冰箱", "洗衣机", "空调", "电视机", "电视柜",
                "电视盒", "扫地机器人", "空气净化", "加湿", "电饭煲",
                "空气炸锅", "炸锅", "微波炉", "净水", "热水器", "家具",
                "沙发", "床", "床垫", "窗帘", "收纳", "灯具", "家居"],
    "母婴玩具": ["母婴", "婴幼儿", "奶粉", "纸尿裤", "尿不湿", "玩具",
                "童车", "辅食", "孕婴", "拼豆", "拼图", "积木", "乐高",
                "手工", "diy", "粘土", "彩泥", "橡皮泥", "串珠", "钻石画", "盲盒", "手办"],
    "汽车用品": ["脚垫", "座套", "坐垫", "后备箱垫", "车衣", "车载",
                "方向盘", "行车记录仪", "车膜", "机油", "汽车"],
    "健康保健": ["保健品", "维生素", "瘦身", "瑜伽", "体检", "牙齿"],
    "黄金珠宝": ["黄金", "金店", "珠宝", "钻石", "翡翠", "银饰", "项链",
                "戒指", "手镯"],
    "宠物": ["猫粮", "狗粮", "修狗", "宠物用品", "撸猫", "吸猫"],
}


def _dy_parse_heat(s):
    """解析热度字符串（如 '8222.7万'）为数值"""
    if s is None:
        return 0.0
    s = str(s).strip()
    m = re.search(r"([\d.]+)", s)
    if not m:
        return 0.0
    n = float(m.group(1))
    if "亿" in s:
        return n * 1e8
    if "万" in s:
        return n * 1e4
    return n


def _dy_build_kw_index(cats):
    """预构建关键词索引：{lower_kw: [cat_name, ...]}"""
    index = {}
    for cat_name, kws in cats.items():
        for kw in kws:
            index.setdefault(kw.lower(), []).append(cat_name)
    return index


def _dy_match_categories(term_lower, kw_index):
    """返回该词命中的所有类别列表"""
    hit_cats = set()
    for kw_lower, cat_names in kw_index.items():
        if kw_lower in term_lower:
            hit_cats.update(cat_names)
    return sorted(hit_cats)


def _dy_split_categories(cat_str):
    """把品类串（如 '食品饮料/健康保健'）拆成原子品类集合，兼容 / 逗号 顿号分隔"""
    if not cat_str:
        return set()
    return {c for c in re.split(r'[/,，、]', str(cat_str)) if c}


def _dy_prev_day_categories(target_date):
    """返回「抖音热搜品类表」中 target_date 之前最近一天已出现的原子品类集合。

    用于「前一天去重」：前一天已经出现的品类，今天筛选时自动剔除。
    返回 (prev_date, prev_cats)；没有更早日期时返回 (None, set())。"""
    if not target_date:
        return None, set()
    rows = db_execute(
        "SELECT MAX(`日期`) AS d FROM `抖音热搜品类表` WHERE `日期` < %s", [target_date])
    prev_date = rows[0].get('d') if rows else None
    if prev_date is None:
        return None, set()
    cats = set()
    for r in db_execute("SELECT `品类` FROM `抖音热搜品类表` WHERE `日期` = %s", [prev_date]):
        cats |= _dy_split_categories(r.get('品类'))
    return str(prev_date), cats


def _ensure_douyin_hot_columns():
    """确保「抖音热搜榜单表」具备筛选结果相关列（品类 / 是否电商 / 热度数值）"""
    existing = {r['Field'] for r in db_execute("SHOW COLUMNS FROM `抖音热搜榜单表`")}
    specs = {
        '品类': "VARCHAR(255) NULL",
        '是否电商': "TINYINT(1) NOT NULL DEFAULT 0",
        '热度数值': "DECIMAL(20,2) NULL",
    }
    for col, ddl in specs.items():
        if col not in existing:
            db_execute(f"ALTER TABLE `抖音热搜榜单表` ADD COLUMN `{col}` {ddl}", fetch=False)


def _ensure_douyin_category_columns():
    """确保「抖音热搜品类表」结构与「抖音热搜榜单表」一致（热搜名/热搜值/日期/品类/是否电商/热度数值）"""
    existing = {r['Field'] for r in db_execute("SHOW COLUMNS FROM `抖音热搜品类表`")}
    specs = {
        '热搜名': "VARCHAR(255) NOT NULL",
        '热搜值': "VARCHAR(255) NOT NULL",
        '日期': "DATE NOT NULL",
        '品类': "VARCHAR(255) NULL",
        '是否电商': "TINYINT(1) NOT NULL DEFAULT 0",
        '热度数值': "DECIMAL(20,2) NULL",
    }
    for col, ddl in specs.items():
        if col not in existing:
            db_execute(f"ALTER TABLE `抖音热搜品类表` ADD COLUMN `{col}` {ddl}", fetch=False)


def _summarize_douyin_categories(matched):
    """调用 DeepSeek 总结筛选结果中电商会涉及的品类/单品名，返回列表；失败返回 None"""
    if not matched:
        return []

    ordered = sorted(matched, key=lambda m: m.get('heat_num', 0), reverse=True)
    groups = {}
    for m in ordered:
        groups.setdefault(m['categories'], []).append(m['term'])
    sample = '\n'.join(f"{cat}：{'、'.join(terms)}" for cat, terms in groups.items())

    sys_p = ('你是电商选品分析助手。请根据给定的抖音电商相关热搜词（按品类分组），'
             '总结出其中电商会涉及的品类名或具体单品名。只输出结果，每行一个，'
             '不要编号、不要解释、不要标点符号。')
    user = (f'以下是筛选出的抖音电商相关热搜词（按品类分组）：\n{sample}\n\n'
            f'请总结出电商会涉及的品类或单品名，每行输出一个。')
    raw = call_deepseek_api(sys_p, user, temperature=0.3, max_tokens=2048)
    if not raw:
        return None

    items = []
    for line in raw.splitlines():
        s = re.sub(r'^(?:\d+[.、)）]\s*|[-*•·]\s*)+', '', line).strip()
        if s and s not in items:
            items.append(s)
    return items


def _export_douyin_summary_excel(items, target_date, base_dir=r"Z:\抖音搜索榜"):
    """把总结出的电商品类/单品名导出为一列 Excel。
    成功返回文件完整路径，失败返回 None（不中断筛选主流程）。"""
    try:
        import openpyxl
        from pathlib import Path
    except Exception:
        return None

    try:
        date_label = (target_date or '全表').strip().replace('/', '-')
        folder = Path(base_dir) / f"{date_label} 品类关联词"
        folder.mkdir(parents=True, exist_ok=True)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "电商品类单品名"
        ws.append(["电商品类/单品名"])
        for it in items:
            ws.append([it])
        ws.column_dimensions['A'].width = 40

        out = folder / "电商相关热搜词.xlsx"
        wb.save(out)
        return str(out)
    except Exception:
        traceback.print_exc()
        return None


@app.route('/api/product-selection/douyin/filter', methods=['POST'])
def product_selection_douyin_filter():
    """触发筛选：读取「抖音热搜榜单表」原始热搜词运行品类筛选，
    筛选结果写入「抖音热搜品类表」（榜单表保留原始数据不被覆盖）"""
    try:
        _ensure_douyin_category_columns()

        # 日期：优先从 JSON body 取，兼容 query 参数；为空则筛选全表
        _payload = request.get_json(silent=True) or {}
        target_date = (_payload.get('date') or request.args.get('date') or '').strip()

        if target_date:
            rows = db_execute(
                "SELECT 热搜名, 热搜值, 日期 FROM `抖音热搜榜单表` WHERE `日期` = %s", [target_date])
        else:
            rows = db_execute("SELECT 热搜名, 热搜值, 日期 FROM `抖音热搜榜单表`")

        # 按 (热搜名, 日期) 去重后逐条筛选
        seen = set()
        entries = []
        for r in rows:
            term = (r['热搜名'] or '').strip()
            if not term:
                continue
            dt = str(r['日期']) if r['日期'] else ''
            key = (term, dt)
            if key in seen:
                continue
            seen.add(key)
            entries.append({'term': term, 'heat_raw': (r['热搜值'] or '').strip(), 'date': dt})

        kw_index = _dy_build_kw_index(_DY_CATS)
        # 前一天去重：读取 target_date 之前最近一天已出现的品类，今天筛选时自动剔除
        prev_date, prev_cats = _dy_prev_day_categories(target_date)
        deduped = 0
        matched = []
        stats = {
            "超长(>10字)": 0,
            "黑名单精确匹配(删)": 0,
            "黑名单子串匹配(删)": 0,
            "网红非商品(删)": 0,
            "网红商品(保留)": 0,
            "前一天已出现(去重)": 0,
        }
        for e in entries:
            term = e['term']
            tl = term.lower()

            if len(term) > 10:
                stats["超长(>10字)"] += 1
                continue
            if term in _DY_BLACKLIST_EXACT:
                stats["黑名单精确匹配(删)"] += 1
                continue
            if any(b.lower() in tl for b in _DY_BLACKLIST_SUBSTR):
                stats["黑名单子串匹配(删)"] += 1
                continue

            hit_cats = _dy_match_categories(tl, kw_index)
            is_wh = ("网红" in tl or "达人" in tl)
            if is_wh and not hit_cats:
                stats["网红非商品(删)"] += 1
                continue

            # 前一天去重：把前一天已出现的品类从该词命中品类里剔除，
            # 全部被剔除则该词整体丢弃（前一天已出现，今天不再重复）。
            if hit_cats and prev_cats:
                kept = [c for c in hit_cats if c not in prev_cats]
                if not kept:
                    stats["前一天已出现(去重)"] += 1
                    deduped += 1
                    continue
                hit_cats = kept

            if hit_cats:
                if is_wh:
                    stats["网红商品(保留)"] += 1
                matched.append({
                    'term': term,
                    'heat_raw': e['heat_raw'],
                    'date': e['date'],
                    'categories': '/'.join(hit_cats),
                    'heat_num': _dy_parse_heat(e['heat_raw']),
                })

        # 覆盖写入「抖音热搜品类表」：先删除目标日期的旧筛选结果，再插入本次筛选出的电商热搜（幂等，可重复点击）
        if target_date:
            db_execute("DELETE FROM `抖音热搜品类表` WHERE `日期` = %s", [target_date], fetch=False)
        else:
            db_execute("DELETE FROM `抖音热搜品类表`", fetch=False)

        for m in matched:
            db_execute(
                "INSERT INTO `抖音热搜品类表` (`热搜名`, `热搜值`, `日期`, `品类`, `是否电商`, `热度数值`) "
                "VALUES (%s, %s, %s, %s, 1, %s)",
                [m['term'], m['heat_raw'], m['date'], m['categories'], m['heat_num']], fetch=False)

        summary = _summarize_douyin_categories(matched)
        if not summary:
            # 大模型不可用时，退回用代码筛选出的品类去重列表
            cats = []
            for m in matched:
                for c in m['categories'].split('/'):
                    if c and c not in cats:
                        cats.append(c)
            summary = cats
        export_path = _export_douyin_summary_excel(summary, target_date)
        return success({
            'matched': len(matched), 'total': len(entries), 'stats': stats,
            'deduped': deduped, 'prev_date': prev_date,
            'date': target_date, 'export': export_path,
        }, '筛选完成')
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


# ======================== 启动 ========================

if __name__ == '__main__':
    print('=' * 55)
    print('  电商后台管理系统 - Flask API 服务')
    print(f'  数据库：{DB_CONFIG["host"]}:{DB_CONFIG["port"]}/{DB_CONFIG["database"]}')
    print(f'  连接池：{DB_POOL_SIZE} 个连接')
    print('=' * 55)

    # 启动时检测数据库连通性
    print()
    conn = None
    try:
        conn = pool.get()
        conn.ping()
        print('  [OK] 数据库连接成功！')
        # 自动建表：每日数据分析报告
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_analysis_reports (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    report_date DATE NOT NULL UNIQUE,
                    html_report MEDIUMTEXT NOT NULL,
                    metrics_json TEXT,
                    status VARCHAR(20) DEFAULT 'success',
                    error_msg VARCHAR(500),
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            conn.commit()
        print('  [OK] 数据表 daily_analysis_reports 就绪')
        pool.put(conn)
    except Exception as e:
        print(f'  [FAIL] 数据库连接失败: {e}')
        print('  请检查：1) MySQL 服务是否启动  2) 网络是否通  3) 账号密码是否正确')
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    print()
    print('  前端页面（本机访问）：http://127.0.0.1:5000')
    print('  前端页面（局域网访问）：http://<本机IP>:5000')
    print()
    print('  API 端点：')
    print('    GET  /api/health             健康检查 / 数据库连通测试')
    print('    GET  /api/products           商品列表')
    print('    POST /api/products           新增商品')
    print('    PUT  /api/products/<id>      更新商品')
    print('    DELETE /api/products/<id>    删除商品')
    print('    GET  /api/orders             订单列表')
    print('    POST /api/orders             新增订单')
    print('    PUT  /api/orders/<id>        更新订单')
    print('    DELETE /api/orders/<id>      删除订单')
    print('    GET  /api/customers          客户列表')
    print('    POST /api/customers          新增客户')
    print('    PUT  /api/customers/<id>     更新客户')
    print('    DELETE /api/customers/<id>    删除客户')
    print('    GET  /api/dashboard/stats    数据看板统计')
    print('    GET  /api/marketing/overview 营销数据总览')
    print('    POST /api/analysis/generate  每日数据分析-生成报告')
    print('    GET  /api/analysis/report    每日数据分析-查询报告')
    print('    GET  /api/analysis/dates     每日数据分析-报告日期列表')
    print('    GET  /api/analysis/download  每日数据分析-下载PDF报告')
    print('    GET  /api/db/tables          列出可管理的数据库表')
    print('    GET  /api/db/tables/<表>/rows  查询表数据')
    print('    POST /api/db/tables/<表>/rows  插入行')
    print('    PUT  /api/db/tables/<表>/rows/<id> 更新行')
    print('    DELETE /api/db/tables/<表>/rows/<id> 删除行')
    print()
    app.run(host='0.0.0.0', port=5000, debug=False)
