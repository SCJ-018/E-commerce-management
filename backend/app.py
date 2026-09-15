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
from threading import Lock, RLock

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
        self._lock = RLock()  # RLock 允许 _create_conn/_discard 内再次加锁
        self._size = pool_size
        self._count = 0  # 存活连接数（已借出 + 在池中）

    def _create_conn(self):
        """创建一个新连接并计数 +1"""
        conn = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        conn.ping()
        with self._lock:
            self._count += 1
        return conn

    def _discard(self, conn):
        """关闭连接并递减存活连接计数（连接被判定失效时调用）"""
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        with self._lock:
            if self._count > 0:
                self._count -= 1

    def get(self):
        """从池中获取一个可用连接"""
        # 1) 优先取空闲连接
        try:
            conn = self._pool.get(block=False)
        except Exception:
            conn = None
        if conn is not None and DB_PING_BEFORE_QUERY:
            try:
                conn.ping()
            except Exception:
                self._discard(conn)  # 失效：关闭并递减计数
                conn = None
        if conn is not None:
            return conn
        # 2) 池空，额度内新建（连接失败则计数不变，不会泄漏）
        with self._lock:
            if self._count < self._size:
                return self._create_conn()
        # 3) 池满，阻塞等待归还
        conn = self._pool.get(block=True, timeout=5)
        if DB_PING_BEFORE_QUERY:
            try:
                conn.ping()
            except Exception:
                self._discard(conn)
                return self._create_conn()
        return conn

    def put(self, conn):
        """归还连接到池中"""
        if conn:
            try:
                self._pool.put_nowait(conn)
            except Exception:
                self._discard(conn)

    def close_all(self):
        """关闭池中所有连接"""
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                self._discard(conn)
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


def discard_db(conn):
    """丢弃失效连接（关闭并递减池计数），供 db_execute 在连接级错误时使用"""
    pool._discard(conn)


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
                discard_db(conn)
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
                discard_db(conn)
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

def call_deepseek_api(system_prompt, user_message, temperature=0.3, max_tokens=4096, api_key=None, model=None):
    """调用 DeepSeek API 生成分析报告，返回文本内容；失败返回 None。api_key 可指定专用 key，model 可指定模型，均默认用全局配置。"""
    headers = {
        'Authorization': f'Bearer {api_key or DEEPSEEK_API_KEY}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': model or DEEPSEEK_MODEL,
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


def _gather_link_data(start_date, end_date=None):
    """收集抖店/京东/千牛单链接数据（按日期或日期范围聚合）"""
    end_date = end_date or start_date
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
            WHERE 统计周期 >= %s AND 统计周期 <= %s
        """, [start_date, end_date])[0]
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
            WHERE 时间 >= %s AND 时间 <= %s
        """, [start_date, end_date])[0]
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
            WHERE 统计日期 >= %s AND 统计日期 <= %s
        """, [start_date, end_date])[0]
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


def _gather_qianniu_promo(start_date, end_date=None):
    """收集千牛单链接推广数据（推广消耗 vs 产出，支持日期范围）"""
    end_date = end_date or start_date
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
            WHERE 统计日期 >= %s AND 统计日期 <= %s
        """, [start_date, end_date])[0]
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


def gather_daily_data(start_date, end_date=None):
    """收集指定日期（或日期范围）的全维度数据：店铺营销数据 + 抖店/京东/千牛单链接数据"""
    end_date = end_date or start_date
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
        WHERE 日期 >= %s AND 日期 <= %s
    """, [start_date, end_date])[0]

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
        WHERE 日期 >= %s AND 日期 <= %s
        GROUP BY 平台
        ORDER BY net_payment DESC
    """, [start_date, end_date])

    # 分品牌数据 (TOP10)
    brand_rows = db_execute("""
        SELECT
            品牌,
            COALESCE(SUM(净支付金额), 0) AS net_payment,
            COALESCE(SUM(访客数), 0)      AS visitors
        FROM 店铺营销数据
        WHERE 日期 >= %s AND 日期 <= %s
        GROUP BY 品牌
        ORDER BY net_payment DESC
        LIMIT 10
    """, [start_date, end_date])

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
        WHERE 日期 >= %s AND 日期 <= %s
        GROUP BY 平台, 店铺名
        ORDER BY net_payment DESC
        LIMIT 15
    """, [start_date, end_date])

    # 单链接数据（抖店/京东/千牛）+ 消耗产出比分析
    links = _gather_link_data(start_date, end_date)
    link_issues = _analyze_links(links)

    # 千牛单链接推广数据（推广消耗 vs 产出）
    qianniu_promo = _gather_qianniu_promo(start_date, end_date)
    promo_issues = _analyze_qianniu_promo(qianniu_promo)

    # 品类维度汇总（复用品类营销聚合逻辑）
    try:
        category = _gather_category_data(str(start_date), str(end_date))
    except Exception as e:
        category = {'totals': {}, 'categories': [], 'error': str(e)}

    date_label = str(start_date) if str(start_date) == str(end_date) else f'{start_date} ~ {end_date}'
    return {
        'date': date_label,
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


def _build_analysis_context(start_date, end_date=None):
    """把 gather_daily_data 结果转成中文 key 的知识库上下文，供每日数据分析智能体使用"""
    end_date = end_date or start_date
    data = gather_daily_data(start_date, end_date)
    sm = data['summary']
    date_label = str(start_date) if str(start_date) == str(end_date) else f'{start_date} ~ {end_date}'
    return {
        '日期': date_label,
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


# ======================== 人事数据中心（5 张人事表通用 CRUD） ========================
# 独立模块 backend/hr_api.py，技术框架与本文件完全一致（Flask Blueprint + 同一连接池）
try:
    from hr_api import hr_bp, init_hr_api
    init_hr_api(db_execute, db_execute_insert, success, fail)
    app.register_blueprint(hr_bp)
    print('[HR] 人事数据中心接口已注册: /api/hr/meta, /api/hr/<key>/rows')
except Exception as _hr_err:
    print('[HR] 人事接口注册失败:', _hr_err)


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
            discard_db(conn)
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


# ======================== 登录鉴权（token 会话） ========================

import secrets

# token -> {'account':..., 'name':..., 'role':..., 'expires': 时间戳}
# ponytail: 内存存储，单 worker（systemd 里 --workers 1）够用；若以后多 worker 需换 Redis/DB
_AUTH_TOKENS = {}
_AUTH_TOKEN_TTL = 24 * 3600  # 24 小时，滑动续期

# 无需登录即可访问的接口（登录本身、退出、健康检查）
_AUTH_PUBLIC_PATHS = {'/api/auth/login', '/api/auth/logout', '/api/health'}


@app.before_request
def _require_auth():
    """对所有 /api/* 接口做登录态校验（白名单除外），未登录返回 401"""
    if not request.path.startswith('/api/'):
        return None
    if request.path in _AUTH_PUBLIC_PATHS:
        return None
    token = request.cookies.get('token', '')
    rec = _AUTH_TOKENS.get(token)
    now = time.time()
    if not rec or rec.get('expires', 0) < now:
        if rec:
            _AUTH_TOKENS.pop(token, None)
        return jsonify({'code': 401, 'msg': '未登录或登录已过期', 'data': None}), 401
    rec['expires'] = now + _AUTH_TOKEN_TTL  # 滑动续期
    return None


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

        token = secrets.token_hex(32)
        _AUTH_TOKENS[token] = {
            'account': admin['account'],
            'name': admin['name'],
            'role': admin['role'],
            'expires': time.time() + _AUTH_TOKEN_TTL,
        }
        resp = success({
            'id': admin['id'],
            'name': admin['name'],
            'account': admin['account'],
            'role': admin['role'],
            'status': admin['status'],
            'lastLogin': now_str,
            'permissions': permissions,
        }, '登录成功')
        resp.set_cookie('token', token, max_age=_AUTH_TOKEN_TTL,
                        httponly=True, samesite='Lax', path='/')
        return resp
    except Exception as e:
        return fail(str(e))


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    """退出登录 - 清除服务端 token 会话"""
    token = request.cookies.get('token', '')
    _AUTH_TOKENS.pop(token, None)
    resp = success(None, '已退出登录')
    resp.set_cookie('token', '', max_age=0, httponly=True, samesite='Lax', path='/')
    return resp


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


# ======================== Excel/CSV 网页导入 ========================

# 允许网页上传导入的业务数据表
IMPORT_TABLES = {
    '抖店单链接数据表', '京东单链接数据表', '千牛单链接数据表',
    '千牛单链接推广数据表', '店铺营销数据',
}


def _parse_upload_file(filename, stream, sheet_name=None):
    """解析上传的 xlsx/xls/csv，返回 (列名列表, 行数据列表[{列:值}])。sheet_name 指定工作表，缺省取第一个。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext in ('.xlsx', '.xls'):
        import io
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(stream.read()), read_only=True, data_only=True)
        ws = wb[sheet_name] if (sheet_name and sheet_name in wb.sheetnames) else wb[wb.sheetnames[0]]
        cols, rows = [], []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                cols = [str(c).strip() if c is not None else f'Column_{j}' for j, c in enumerate(row)]
            elif row and any(v is not None and str(v).strip() != '' for v in row):
                rows.append(dict(zip(cols, row)))
        return cols, rows
    if ext == '.csv':
        import io
        import csv as _csv
        data = stream.read()
        for enc in ('utf-8-sig', 'utf-8', 'gbk', 'gb18030'):
            try:
                text = data.decode(enc)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        else:
            text = data.decode('utf-8', errors='replace')
        reader = _csv.DictReader(io.StringIO(text))
        cols = [c.strip() for c in (reader.fieldnames or [])]
        return cols, [dict(r) for r in reader]
    raise ValueError('仅支持 .xlsx / .xls / .csv 文件')


def _import_convert(value, col_type):
    """按数据库列类型转换单元格值，无法转换返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, date):
        return value.strftime('%Y-%m-%d')
    s = str(value).strip()
    if s in ('', 'NULL', 'N/A', '-', 'null', 'None', 'nan', 'NaN'):
        return None
    tl = col_type.lower()
    if any(t in tl for t in ('int', 'decimal', 'float', 'double', 'numeric')):
        s2 = s.replace(',', '').replace('￥', '').replace('¥', '').replace('元', '').replace('%', '').replace(' ', '')
        try:
            return float(s2) if '.' in s2 else int(s2)
        except ValueError:
            return None
    if 'date' in tl or 'time' in tl:
        for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y%m%d', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S', '%Y年%m月%d日'):
            try:
                return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
            except ValueError:
                continue
        return s
    return s


@app.route('/api/import/sheets', methods=['POST'])
def import_sheets():
    """返回上传的 xlsx/xls 的工作表名列表，供前端选择要导入的 sheet。"""
    try:
        file = request.files.get('file')
        if not file or not file.filename:
            return fail('请选择要导入的文件')
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ('.xlsx', '.xls'):
            return success({'sheets': []})
        import io
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file.read()), read_only=True, data_only=True)
        return success({'sheets': wb.sheetnames})
    except Exception as e:
        return fail(str(e))


@app.route('/api/import/excel', methods=['POST'])
def import_excel():
    """上传 Excel/CSV 导入到业务数据表（按表主键 upsert，已存在则更新）。"""
    try:
        table = (request.form.get('table') or '').strip()
        sheet = (request.form.get('sheet') or '').strip()
        file = request.files.get('file')
        if table not in IMPORT_TABLES:
            return fail('该表不支持网页导入')
        if not file or not file.filename:
            return fail('请选择要导入的文件')

        cols, rows = _parse_upload_file(file.filename, file, sheet)
        if not rows:
            return fail('文件里没有数据行')

        col_types = {c['Field']: c['Type'] for c in db_execute(f'SHOW FULL COLUMNS FROM `{table}`')}
        table_fields = list(col_types.keys())
        insert_cols = [c for c in cols if c in table_fields]
        if not insert_cols:
            return fail('文件列与表字段无交集，请检查表头是否与目标表字段一致')

        pk_cols = [r['Column_name'] for r in db_execute(f"SHOW KEYS FROM `{table}` WHERE Key_name='PRIMARY'")]

        cols_sql = ', '.join([f'`{c}`' for c in insert_cols])
        placeholders = ', '.join(['%s'] * len(insert_cols))
        if pk_cols:
            upd = ', '.join([f'`{c}`=VALUES(`{c}`)' for c in insert_cols if c not in pk_cols])
            sql = (f'INSERT INTO `{table}` ({cols_sql}) VALUES ({placeholders})'
                   + (f' ON DUPLICATE KEY UPDATE {upd}' if upd else ''))
        else:
            sql = f'INSERT INTO `{table}` ({cols_sql}) VALUES ({placeholders})'

        ok, failed, first_err = 0, 0, ''
        conn = get_db()
        try:
            with conn.cursor() as cur:
                for row in rows:
                    vals = [_import_convert(row.get(c), col_types[c]) for c in insert_cols]
                    try:
                        cur.execute(sql, vals)
                        ok += 1
                    except Exception as e:
                        failed += 1
                        if not first_err:
                            first_err = str(e)
                conn.commit()
        finally:
            return_db(conn)

        return success({'inserted': ok, 'failed': failed, 'error': first_err},
                       f'导入完成：成功 {ok} 行，失败 {failed} 行')
    except Exception as e:
        return fail(str(e))


# ======================== 员工花名册 ========================

# 员工花名册全部列（与数据库表结构一致）
HR_COLS = ['工号', '姓名', '入职时间', '一级部门', '二级部门', '直带人', '岗级', '手机号',
           '身份证号', '紧急联系人电话', '状态', '薪资待遇', '转正日期', '银行卡号', '开户行', '主人事']


@app.route('/api/hr/employees', methods=['GET'])
def list_employees():
    """员工花名册列表"""
    try:
        rows = db_execute('SELECT * FROM `员工花名册` ORDER BY `工号`')
        return success(_serialize_rows(rows))
    except Exception as e:
        return fail(str(e))


@app.route('/api/hr/employees', methods=['POST'])
def add_employee():
    """新增员工"""
    try:
        data = request.get_json(force=True) or {}
        emp_no = (data.get('工号') or '').strip()
        name = (data.get('姓名') or '').strip()
        phone = str(data.get('手机号') or '').strip()
        if not emp_no or not name or not phone:
            return fail('工号、姓名、手机号为必填项')

        def val(k):
            v = str(data.get(k) or '').strip()
            if not v:
                return None if k == '入职时间' else ''
            return v

        sql = 'INSERT INTO `员工花名册` (`' + '`,`'.join(HR_COLS) + '`) VALUES (' + ','.join(['%s'] * len(HR_COLS)) + ')'
        db_execute_insert(sql, [val(k) for k in HR_COLS])
        return success({'工号': emp_no, '姓名': name, '手机号': phone}, '添加成功')
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

        start_str = (payload.get('start') or '').strip()
        end_str = (payload.get('end') or '').strip()
        date_str = (payload.get('date') or '').strip()
        try:
            if start_str and end_str:
                start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_str, '%Y-%m-%d').date()
                if end_date < start_date:
                    start_date, end_date = end_date, start_date
            elif date_str:
                start_date = end_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            else:
                start_date = end_date = date.today() - timedelta(days=1)
        except ValueError:
            start_date = end_date = date.today() - timedelta(days=1)

        context = _build_analysis_context(start_date, end_date)
        ctx_json = json.dumps(context, ensure_ascii=False, default=str)

        sys_p = (
            '# 角色定义\n'
            '你是"数据分析智能体"，一名资深电商经营数据分析师，服务于同时经营多平台（淘宝/京东/拼多多/抖音/快手等）的商家。'
            '你只基于给定的知识库数据做分析，绝不编造。\n\n'
            '# 数据来源与结构\n'
            '你会收到一份结构化的知识库检索结果（JSON），包含该商家的四类经营数据，均为指定日期（或日期范围）内的汇总：\n\n'
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
        date_label = str(start_date) if start_date == end_date else f'{start_date} ~ {end_date}'
        return success({
            'analysis': analysis,
            'cards': cards,
            'meta': {
                'date': date_label,
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
_SEEDING_STATE_FILE = os.path.join(_SEEDING_DIR, '_seeding_state.json')


def _seeding_progress_file(platform):
    return os.path.join(_SEEDING_DIR, '_seeding_progress_%s.json' % platform)


def _seeding_progress_write(platform, status, done, total):
    """写抓取进度文件，供前端进度条轮询"""
    try:
        with open(_seeding_progress_file(platform), 'w', encoding='utf-8') as f:
            json.dump({'status': status, 'done': done, 'total': total}, f, ensure_ascii=False)
    except Exception:
        pass


def _seeding_progress_read(platform):
    """读抓取进度：{status, done, total, progress, finished}"""
    info = {'status': 'idle', 'done': 0, 'total': 0, 'progress': 0, 'finished': False}
    path = _seeding_progress_file(platform)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                p = json.load(f)
            info['status'] = p.get('status', 'idle')
            info['done'] = int(p.get('done', 0) or 0)
            info['total'] = int(p.get('total', 0) or 0)
        except Exception:
            pass
    if info['total']:
        info['progress'] = min(100, round(info['done'] / info['total'] * 100))
    if info['status'] == 'done':
        info['progress'] = 100
        info['finished'] = True
    return info


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
            works = _seeding_load_xhs_works()
            if os.path.exists(_XHS_WORKS_FILE):
                _seeding_reconcile_deleted('xhs', works)
            return success(works)
        real = _seeding_load_works_csv()
        if real is not None:
            _seeding_reconcile_deleted('douyin', real)
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


def _seeding_launch(platform):
    """后台异步启动指定平台的作品抓取脚本，写进度初始状态。返回 (error_msg or None)"""
    import sys as _sys_scrape
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if platform == 'xhs':
        scraper = os.path.join(_SEEDING_DIR, 'seeding_xhs.py')
        if not os.path.isdir(os.path.join(_SEEDING_DIR, 'Spider_XHS')):
            return '缺少小红书抓取依赖：未找到 tools/Spider_XHS 目录，请先部署 Spider_XHS 开源项目'
    else:
        scraper = os.path.join(_SEEDING_DIR, 'douyin_video_scraper.py')
    if not os.path.exists(scraper):
        return '抓取脚本不存在：' + scraper
    _seeding_progress_write(platform, 'running', 0, 0)
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
    return None


@app.route('/api/seeding/scrape', methods=['POST'])
def seeding_trigger_scrape():
    """按钮触发：后台异步执行作品抓取脚本（platform=douyin/xhs）"""
    try:
        data = request.get_json(force=True) or {}
        platform = (data.get('platform') or 'douyin').strip()
        if platform == 'xhs':
            output_file = _XHS_WORKS_FILE
        else:
            output_file = _DOUYIN_WORKS_CSV
        before_mtime = os.path.getmtime(output_file) if os.path.exists(output_file) else None
        err = _seeding_launch(platform)
        if err:
            return fail(err)
        return success({'triggered': True, 'mtime': before_mtime}, '已触发抓取任务，正在后台执行')
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/scrape/status', methods=['GET'])
def seeding_scrape_status():
    """读取作品抓取进度（供前端进度条轮询）"""
    try:
        platform = (request.args.get('platform') or 'douyin').strip()
        return success(_seeding_progress_read(platform))
    except Exception as e:
        return fail(str(e))


# ======================== 被删作品检测 ========================

def _seeding_work_key(w):
    """作品唯一键：优先用链接，无链接退回 账号+标题"""
    link = (w.get('link') or w.get('url') or '').strip()
    if link:
        return 'link:' + link
    return 't:' + (w.get('account') or '').strip() + '|' + (w.get('title') or '').strip()


def _seeding_load_state():
    """读取被删作品状态（快照 + 被删列表 + 自增 id）；文件不存在或损坏时返回空状态"""
    if not os.path.exists(_SEEDING_STATE_FILE):
        return {'snapshots': {}, 'deleted': [], '_seq': 0}
    try:
        with open(_SEEDING_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {'snapshots': {}, 'deleted': [], '_seq': 0}
        data.setdefault('snapshots', {})
        data.setdefault('deleted', [])
        data.setdefault('_seq', 0)
        return data
    except Exception:
        return {'snapshots': {}, 'deleted': [], '_seq': 0}


def _seeding_save_state(state):
    try:
        with open(_SEEDING_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'[种草] 保存被删作品状态失败: {e}')


def _seeding_reconcile_deleted(platform, current_works):
    """对比上一次快照，把消失的作品记入被删列表（持久化，随数据更新累积不丢失）"""
    state = _seeding_load_state()
    snapshots = state['snapshots']
    deleted = state['deleted']
    seq = state['_seq']

    prev = snapshots.get(platform) or []
    snapshots[platform] = current_works

    existing = {d.get('key', '') for d in deleted}
    cur_keys = {_seeding_work_key(w) for w in current_works}
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    for w in prev:
        k = _seeding_work_key(w)
        if k in cur_keys or k in existing:
            continue
        seq += 1
        existing.add(k)
        deleted.append({
            'id': seq,
            'key': k,
            'platform': platform,
            'name': w.get('name') or '',
            'account': w.get('account') or '',
            'title': w.get('title') or '',
            'link': w.get('link') or '',
            'publishTime': w.get('publishTime') or '',   # 原作品发布时间
            'deletedAt': now_str,                          # 检查出被删除的时间
        })

    state['snapshots'] = snapshots
    state['deleted'] = deleted
    state['_seq'] = seq
    _seeding_save_state(state)


@app.route('/api/seeding/deleted', methods=['GET'])
def seeding_list_deleted():
    """被删作品列表（按检测时间倒序）"""
    try:
        deleted = _seeding_load_state().get('deleted', [])
        deleted = sorted(deleted, key=lambda d: d.get('deletedAt', ''), reverse=True)
        return success(deleted)
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/deleted', methods=['DELETE'])
def seeding_clear_deleted():
    """清空全部被删作品记录"""
    try:
        state = _seeding_load_state()
        state['deleted'] = []
        _seeding_save_state(state)
        return success(None, '已清空全部被删作品记录')
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/deleted/<int:did>', methods=['DELETE'])
def seeding_delete_deleted(did):
    """删除单条被删作品记录"""
    try:
        state = _seeding_load_state()
        before = len(state['deleted'])
        state['deleted'] = [d for d in state['deleted'] if d.get('id') != did]
        if len(state['deleted']) == before:
            return fail('记录不存在')
        _seeding_save_state(state)
        return success(None, '记录已清除')
    except Exception as e:
        return fail(str(e))


# ======================== 种草智能体（文案生成） ========================
# 知识库：已上传文案库（单列 `文案`，存用户上传的种草文案样本）
# 系统提示词：backend/seeding_agent_prompt.txt（「种草君」人设）

_SEEDING_AGENT_PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seeding_agent_prompt.txt')
_SEEDING_AGENT_MODEL = 'deepseek-v4-flash'   # 种草智能体专用模型（默认全局为 deepseek-chat）
_SEEDING_KB_LIMIT = 200        # 知识库最多带入条数
_SEEDING_KB_ITEM_MAX = 2000    # 单条文案最多带入字符数


def _load_seeding_agent_prompt():
    """读取「种草君」系统提示词；文件缺失时回退到一段简短默认提示"""
    try:
        with open(_SEEDING_AGENT_PROMPT_FILE, 'r', encoding='utf-8') as f:
            p = f.read().strip()
            if p:
                return p
    except Exception as e:
        print(f'[种草] 读取系统提示词失败: {e}')
    return ('你是"种草君"，一名资深的小红书/社交平台种草文案创作专家，擅长撰写种草图文正文、'
            '评论区互动文案与口播种草视频脚本。请基于知识库中的优秀文案学习风格，输出真实接地气的原创文案。'
            '信息不足时先输出信息填写模板引导用户补充。')


def _seeding_load_kb():
    """读取「已上传文案库」知识库，返回文案列表；表不存在或为空时返回 []"""
    try:
        rows = db_execute('SELECT `文案` FROM `已上传文案库` LIMIT %s', [_SEEDING_KB_LIMIT])
    except Exception as e:
        print(f'[种草] 读取文案库失败: {e}')
        return []
    samples = []
    for r in rows:
        t = (r.get('文案') or '').strip()
        if t:
            samples.append(t[:_SEEDING_KB_ITEM_MAX])
    return samples


@app.route('/api/seeding/agent', methods=['POST'])
def seeding_agent():
    """种草智能体：检索「已上传文案库」知识库 + 「种草君」系统提示词生成种草文案"""
    try:
        payload = request.get_json(silent=True) or {}
        question = (payload.get('question') or '').strip()
        if not question:
            return fail('请输入种草文案需求')

        samples = _seeding_load_kb()
        if samples:
            kb_block = '\n\n'.join(f'【样本文案 {i + 1}】\n{s}' for i, s in enumerate(samples))
        else:
            kb_block = '（当前「已上传文案库」为空，暂无参考样本，请按你自己的风格原创）'

        sys_p = _load_seeding_agent_prompt()
        user_msg = (f'已上传文案库（知识库，用于学习风格与去重，若为空则忽略）：\n{kb_block}\n\n'
                    f'用户需求：{question}\n\n'
                    f'（直接输出最终文案，不要输出任何风格学习、知识库分析、去重说明、切入角度差异等过程性文字）')

        raw = call_deepseek_api(sys_p, user_msg, temperature=0.8, max_tokens=4096, model=_SEEDING_AGENT_MODEL)

        return success({
            'analysis': raw or '',
            'meta': {
                'kb_count': len(samples),
                'raw_available': bool(raw),
            },
        }, 'ok')
    except Exception as e:
        traceback.print_exc()
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
            use_textline_orientation=False,  # 关闭方向分类，显著提速（商品图文字基本为正方向）
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
    """天猫榜单：读取「天猫榜单表」，支持价格区间筛选 + 分页"""
    try:
        min_price = _ps_parse_float(request.args.get('minPrice', '').strip())
        max_price = _ps_parse_float(request.args.get('maxPrice', '').strip())
        try:
            page = max(1, int(request.args.get('page', '1') or 1))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = max(1, min(200, int(request.args.get('pageSize', '50') or 50)))
        except (TypeError, ValueError):
            page_size = 50

        conditions = []
        params = []
        if min_price is not None:
            conditions.append('价格 >= %s')
            params.append(min_price)
        if max_price is not None:
            conditions.append('价格 <= %s')
            params.append(max_price)

        where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

        total_rows = db_execute(f"SELECT COUNT(*) AS total FROM 天猫榜单表 {where_clause}", params)
        total = int(total_rows[0]['total']) if total_rows else 0

        rows = db_execute(f"""
            SELECT 类别名, 排行榜名, 产品名, 价格, 日期
            FROM 天猫榜单表
            {where_clause}
            ORDER BY 日期 DESC, 价格 ASC
            LIMIT %s OFFSET %s
        """, params + [page_size, (page - 1) * page_size])
        _serialize_rows(rows)

        # 返回全表价格区间，供前端展示 / 占位提示
        stats = db_execute("SELECT MIN(价格) AS min_p, MAX(价格) AS max_p FROM 天猫榜单表")
        price_range = None
        if stats and stats[0].get('min_p') is not None:
            price_range = {'min': float(stats[0]['min_p']), 'max': float(stats[0]['max_p'])}

        return success({'items': rows, 'total': total, 'page': page, 'pageSize': page_size, 'priceRange': price_range})
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
    """爱搜数据：读取「爱搜数据表」（智能体抓取写入），支持日期选择 + 词名称模糊搜索"""
    try:
        _ensure_aisou_columns()
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
            selected_date = dates[0]
            conditions.append('日期 = %s')
            params.append(selected_date)
        if keyword:
            conditions.append('(词名称 LIKE %s OR 来源词 LIKE %s)')
            params.extend(['%' + keyword + '%', '%' + keyword + '%'])

        where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

        rows = list(db_execute(f"""
            SELECT 日期, 来源词, 词类型, 词名称, 月覆盖人次, 七日搜索人次
            FROM 爱搜数据表
            {where_clause}
        """, params))
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


# ======================== 天猫市场（淘宝销量抓取） ========================
# 由前端「选品助手 → 天猫市场」输入关键词触发 taobao_scraper.py 抓销量前50，
# 结果封存到 tools/_taobao_top50.json（不落库），供选品智能体读取分析。

_TAOBAO_MARKET_FILE = os.path.join(_SEEDING_DIR, '_taobao_top50.json')
_TAOBAO_COOKIE_FILE = os.path.join(_SEEDING_DIR, 'taobao_cookie.txt')
_TAOBAO_PROGRESS_FILE = os.path.join(_SEEDING_DIR, '_taobao_progress.json')

_DOUYIN_MARKET_FILE = os.path.join(_SEEDING_DIR, '_douyin_top50.json')
_DOUYIN_PROGRESS_FILE = os.path.join(_SEEDING_DIR, '_douyin_progress.json')

_1688_MARKET_FILE = os.path.join(_SEEDING_DIR, '_1688_top10.json')
_1688_COOKIE_FILE = os.path.join(_SEEDING_DIR, '1688_cookie.txt')
_1688_PROGRESS_FILE = os.path.join(_SEEDING_DIR, '_1688_progress.json')


def _ps_load_market(file_path):
    """读取市场抓取结果 JSON（销量前50）；不存在或损坏时返回 None"""
    if not os.path.exists(file_path):
        return None
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _ps_load_taobao_market():
    """读取「天猫市场」抓取结果 JSON（销量前50）；不存在或损坏时返回 None"""
    return _ps_load_market(_TAOBAO_MARKET_FILE)


def _ps_load_douyin_market():
    """读取「抖音市场」抓取结果 JSON（销量前50）；不存在或损坏时返回 None"""
    return _ps_load_market(_DOUYIN_MARKET_FILE)


def _ps_load_1688_market():
    """读取「1688 市场」抓取结果 JSON（前10页）；不存在或损坏时返回 None"""
    return _ps_load_market(_1688_MARKET_FILE)


@app.route('/api/product-selection/tmall-market/cookie', methods=['GET'])
def product_selection_tmall_market_cookie_get():
    """读取淘宝 Cookie（tools/taobao_cookie.txt）"""
    try:
        cookie = ''
        if os.path.exists(_TAOBAO_COOKIE_FILE):
            with open(_TAOBAO_COOKIE_FILE, 'r', encoding='utf-8') as f:
                cookie = f.read().strip()
        return success({'cookie': cookie, 'exists': bool(cookie)})
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/tmall-market/cookie', methods=['POST'])
def product_selection_tmall_market_cookie_save():
    """保存淘宝 Cookie，供 taobao_scraper.py 注入登录态（留空则回退扫码登录）"""
    try:
        data = request.get_json(silent=True) or {}
        cookie = (data.get('cookie') or '').strip()
        with open(_TAOBAO_COOKIE_FILE, 'w', encoding='utf-8') as f:
            f.write(cookie)
        return success({'saved': True}, '淘宝 Cookie 已保存')
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/tmall-market/scrape', methods=['POST'])
def product_selection_tmall_market_scrape():
    """触发「天猫市场」抓取：后台异步运行 taobao_scraper.py，按关键词抓销量前50"""
    try:
        import sys as _sys_scrape
        data = request.get_json(silent=True) or {}
        keyword = (data.get('keyword') or '').strip()
        if not keyword:
            return fail('请输入要抓取的商品关键词')
        scraper = os.path.join(_SEEDING_DIR, 'taobao_scraper.py')
        if not os.path.exists(scraper):
            return fail('抓取脚本不存在：' + scraper)
        before_mtime = os.path.getmtime(_TAOBAO_MARKET_FILE) if os.path.exists(_TAOBAO_MARKET_FILE) else None
        log_file = open(os.path.join(_SEEDING_DIR, '_scrape_taobao.log'), 'w', encoding='utf-8')
        try:
            subprocess.Popen(
                [_sys_scrape.executable, scraper, keyword],
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        finally:
            log_file.close()
        return success({'triggered': True, 'keyword': keyword, 'mtime': before_mtime},
                       '已触发天猫市场抓取，正在后台执行（首次需在弹窗扫码登录淘宝）')
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


@app.route('/api/product-selection/tmall-market/data', methods=['GET'])
def product_selection_tmall_market_data():
    """读取最近一次「天猫市场」抓取结果（不落库，直接读文件封存的变量）"""
    try:
        return success(_ps_load_taobao_market())
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


def _market_status(platform):
    """读取抓取进度：返回 {status, keyword, done, total, progress, finished}"""
    if platform == 'douyin':
        progress_file = _DOUYIN_PROGRESS_FILE
        default_total = 50
    elif platform == '1688':
        progress_file = _1688_PROGRESS_FILE
        default_total = 10
    else:
        progress_file = _TAOBAO_PROGRESS_FILE
        default_total = 50
    info = {'status': 'idle', 'keyword': '', 'done': 0, 'total': default_total, 'progress': 0, 'finished': False}
    if os.path.exists(progress_file):
        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                p = json.load(f)
            info['status'] = p.get('status', 'running')
            info['keyword'] = p.get('keyword', '')
            info['done'] = int(p.get('done', 0) or 0)
            info['total'] = int(p.get('total', 50) or 50)
            info['error'] = p.get('error', '')
        except Exception:
            pass
    info['progress'] = min(100, round(info['done'] / info['total'] * 100)) if info['total'] else 0
    if info['status'] == 'done':
        info['progress'] = 100
        info['finished'] = True
    elif info['status'] == 'error':
        info['finished'] = True  # 失败也视为终态，前端据此停止轮询并提示
    return info


@app.route('/api/product-selection/tmall-market/status', methods=['GET'])
def product_selection_tmall_market_status():
    """读取「天猫市场」抓取进度（供前端进度条轮询）"""
    try:
        return success(_market_status('tmall'))
    except Exception as e:
        return fail(str(e))


# ======================== 抖音市场（抖音商城销量抓取） ========================

@app.route('/api/product-selection/douyin-market/scrape', methods=['POST'])
def product_selection_douyin_market_scrape():
    """触发「抖音市场」抓取：后台异步运行 douyin_scraper.py，按关键词抓销量前50"""
    try:
        import sys as _sys_scrape
        data = request.get_json(silent=True) or {}
        keyword = (data.get('keyword') or '').strip()
        if not keyword:
            return fail('请输入要抓取的商品关键词')
        scraper = os.path.join(_SEEDING_DIR, 'douyin_scraper.py')
        if not os.path.exists(scraper):
            return fail('抓取脚本不存在：' + scraper)
        log_file = open(os.path.join(_SEEDING_DIR, '_scrape_douyin_market.log'), 'w', encoding='utf-8')
        try:
            subprocess.Popen(
                [_sys_scrape.executable, scraper, keyword],
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        finally:
            log_file.close()
        return success({'triggered': True, 'keyword': keyword}, '已触发抖音市场抓取，正在后台执行')
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


@app.route('/api/product-selection/douyin-market/data', methods=['GET'])
def product_selection_douyin_market_data():
    """读取最近一次「抖音市场」抓取结果（不落库，直接读文件封存的变量）"""
    try:
        return success(_ps_load_douyin_market())
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


@app.route('/api/product-selection/douyin-market/status', methods=['GET'])
def product_selection_douyin_market_status():
    """读取「抖音市场」抓取进度（供前端进度条轮询）"""
    try:
        return success(_market_status('douyin'))
    except Exception as e:
        return fail(str(e))


# ======================== 1688 市场（1688 前10页抓取） ========================

@app.route('/api/product-selection/1688-market/cookie', methods=['GET'])
def product_selection_1688_market_cookie_get():
    """读取 1688 Cookie（tools/1688_cookie.txt）"""
    try:
        cookie = ''
        if os.path.exists(_1688_COOKIE_FILE):
            with open(_1688_COOKIE_FILE, 'r', encoding='utf-8') as f:
                cookie = f.read().strip()
        return success({'cookie': cookie, 'exists': bool(cookie)})
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/1688-market/cookie', methods=['POST'])
def product_selection_1688_market_cookie_save():
    """保存 1688 Cookie，供 1688_scraper.py 注入登录态"""
    try:
        data = request.get_json(silent=True) or {}
        cookie = (data.get('cookie') or '').strip()
        with open(_1688_COOKIE_FILE, 'w', encoding='utf-8') as f:
            f.write(cookie)
        return success({'saved': True}, '1688 Cookie 已保存')
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/1688-market/scrape', methods=['POST'])
def product_selection_1688_market_scrape():
    """触发「1688 市场」抓取：后台异步运行 1688_scraper.py，按关键词抓前10页"""
    try:
        import sys as _sys_scrape
        data = request.get_json(silent=True) or {}
        keyword = (data.get('keyword') or '').strip()
        if not keyword:
            return fail('请输入要抓取的商品关键词')
        scraper = os.path.join(_SEEDING_DIR, '1688_scraper.py')
        if not os.path.exists(scraper):
            return fail('抓取脚本不存在：' + scraper)
        log_file = open(os.path.join(_SEEDING_DIR, '_scrape_1688.log'), 'w', encoding='utf-8')
        try:
            subprocess.Popen(
                [_sys_scrape.executable, scraper, keyword],
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        finally:
            log_file.close()
        return success({'triggered': True, 'keyword': keyword}, '已触发1688市场抓取，正在后台执行（需先在 Cookie 面板保存登录态）')
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


@app.route('/api/product-selection/1688-market/data', methods=['GET'])
def product_selection_1688_market_data():
    """读取最近一次「1688 市场」抓取结果（不落库，直接读文件封存的变量）"""
    try:
        return success(_ps_load_1688_market())
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


@app.route('/api/product-selection/1688-market/status', methods=['GET'])
def product_selection_1688_market_status():
    """读取「1688 市场」抓取进度（供前端进度条轮询）"""
    try:
        return success(_market_status('1688'))
    except Exception as e:
        return fail(str(e))


# ======================== 选品助手智能体 ========================
# 流程：
#   每日 8 点自动抓取抖音热点宝（搜索总榜 + 热度飙升榜，近一天，去重，不落库）
#   → 品类筛选写入「抖音热搜品类表」→ 前端抖音热搜榜自动展示。
#   用户点击「当日热搜选品分析」→ 输入价格区间 → 智能体对比天猫榜单价格与市场客单价
#   → 返回约 50 个具体商品卡片 → 用户勾选 10 个 → 智能体过爱搜 + 天猫销量前100
#   → 输出 3 可用品 + 9 裂变品 + 300 字当日建议 → 存档可回看。

_DOUYIN_HOT_COOKIE_FILE = os.path.join(_SEEDING_DIR, 'douyin_hot_cookie.txt')
_DOUYIN_HOT_FILE = os.path.join(_SEEDING_DIR, '_douyin_hot.json')
_DOUYIN_HOT_PROGRESS = os.path.join(_SEEDING_DIR, '_douyin_hot_progress.json')
_DOUYIN_HOT_SCRAPER = os.path.join(_SEEDING_DIR, 'douyin_hot_scraper.py')

_AISOU_LOCALSTORAGE_FILE = os.path.join(_SEEDING_DIR, 'aisou_localstorage.json')
_AISOU_INPUT_FILE = os.path.join(_SEEDING_DIR, '_aisou_input.json')
_AISOU_OUTPUT_FILE = os.path.join(_SEEDING_DIR, '_aisou_output.json')
_AISOU_SCRAPER = os.path.join(_SEEDING_DIR, 'aisou_scraper.py')

_TMALL_BATCH_INPUT = os.path.join(_SEEDING_DIR, '_tmall_input.json')
_TMALL_BATCH_OUTPUT = os.path.join(_SEEDING_DIR, '_tmall_output.json')
_TMALL_BATCH_SCRAPER = os.path.join(_SEEDING_DIR, 'tmall_batch_scraper.py')

_SELECTION_JOB_FILE = os.path.join(_SEEDING_DIR, '_selection_job.json')
_SELECTION_RESULT_FILE = os.path.join(_SEEDING_DIR, '_selection_result.json')


# ======================== 表结构保障 ========================

def _ensure_aisou_columns():
    """确保「爱搜数据表」具备智能体写入所需的列，并让旧字段（非主键）允许 NULL，
    否则只写新字段会因旧字段 NOT NULL 无默认值而报错。"""
    info = {r['Field']: r for r in db_execute("SHOW COLUMNS FROM `爱搜数据表`")}
    specs = {
        '来源词': "VARCHAR(255) NULL",
        '词类型': "VARCHAR(20) NULL",
        '词名称': "VARCHAR(255) NULL",
        '月覆盖人次': "VARCHAR(50) NULL",
        '七日搜索人次': "VARCHAR(50) NULL",
    }
    for col, ddl in specs.items():
        if col not in info:
            db_execute(f"ALTER TABLE `爱搜数据表` ADD COLUMN `{col}` {ddl}", fetch=False)
    # 旧字段（非主键）改成允许 NULL，避免只插新字段时报 1364
    for col in ('搜索词关键词', '搜索词月覆盖人次', '搜索词七日搜索人次',
                '电商词月覆盖人次', '电商词七日搜索人次'):
        r = info.get(col)
        if r and r.get('Null') == 'NO' and r.get('Key') != 'PRI':
            db_execute(f"ALTER TABLE `爱搜数据表` MODIFY COLUMN `{col}` VARCHAR(255) NULL", fetch=False)


def _ensure_selection_record_table():
    """选品记录表：每日选品最终结果存档，供前端「历史选品记录」回看"""
    db_execute(
        "CREATE TABLE IF NOT EXISTS `选品记录表` ("
        "id INT AUTO_INCREMENT PRIMARY KEY, "
        "`日期` DATE NOT NULL, "
        "`价格区间` VARCHAR(50) NULL, "
        "`结果` MEDIUMTEXT NOT NULL, "
        "`创建时间` DATETIME DEFAULT CURRENT_TIMESTAMP"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4", fetch=False)


def _ensure_douyin_category_columns():
    """确保「抖音热搜品类表」具备筛选结果相关列"""
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


# ======================== 抖音热搜 → 电商热搜筛选 ========================
# 筛选优先级：边界拦截 → 精确黑名单 → 子串黑名单 → 正向品类关键词 → 兜底丢弃
MAX_HOT_WORD_LEN = 20

_BLACKLIST_EXACT = {
    "声明", "后果", "吗", "怎么样", "什么意思", "是真的吗",
    "官方回应", "最新进展", "最新消息", "网友热议",
}

_BLACKLIST_SUBSTR = [
    "明星", "电视剧", "综艺", "电影", "短剧", "剧集", "主演",
    "代言人", "代言", "演唱会", "选秀", "爱豆", "追星", "恋情",
    "离婚", "绯闻", "番剧", "八卦", "塌房", "出轨",
    "游戏", "王者荣耀", "手游", "网游", "端游", "原神",
    "团购", "拼团", "外卖", "速运", "代驾", "酒店",
    "附近美食", "美团来电", "京东健康",
    "金价", "黄金价", "黄金价格", "今日黄金", "黄金走势",
    "黄金今日价", "黄金大盘价", "黄金多少钱", "5斤黄金",
    "大盘价", "黄金鸟窝", "黄金大盘", "黄金回收",
    "股票", "股价", "基金", "理财", "涨幅",
    "华为", "小米", "苹果", "oppo", "vivo", "iphone",
    "特斯拉", "比亚迪", "奔驰", "宝马",
    "救援", "失火", "事件", "通报", "警方", "事故",
    "人民锐评", "仅退款",
    "的做法", "是什么意思", "如何评价", "怎么看待",
    "主播", "广场舞", "音乐种草", "沙鹰", "donk",
    "寡人", "放冰箱", "坦克", "女扮男装", "鞋厂", "钟丽缇",
]

_CATS = {
    "服饰鞋包": [
        "衣服", "服装", "服饰", "女装", "男装", "童装", "裤子",
        "裙子", "外套", "卫衣", "t恤", "衬衫", "鞋", "运动鞋",
        "凉鞋", "靴子", "包包", "背包", "帽子", "围巾", "内衣",
        "袜子", "风衣", "羽绒服", "棉服", "西装", "西装裤",
        "牛仔裤", "阔腿裤", "打底裤", "半身裙", "连衣裙",
        "拖鞋", "帆布鞋", "皮鞋", "靴子", "雪地靴",
        "行李箱", "钱包", "腰带", "墨镜", "手套",
    ],
    "食品饮料": [
        "食品", "饮料", "咖啡", "水果", "坚果", "辣条",
        "巧克力", "饼干", "方便面", "螺蛳粉", "自热",
        "白酒", "啤酒", "红酒", "葡萄酒", "喝酒", "酒馆",
        "茶叶", "大米", "糖", "饮品", "小吃",
        "牛奶", "酸奶", "奶粉", "蜂蜜", "麦片",
        "蛋糕", "面包", "月饼", "粽子", "汤圆",
        "辣酱", "火锅底料", "调味料", "橄榄油",
    ],
    "美妆个护": [
        "口红", "粉底", "眉笔", "眼影", "腮红", "遮瑕",
        "防晒", "面膜", "精华", "水乳", "面霜", "洗面奶",
        "洗发水", "护发素", "沐浴露", "牙膏", "香水",
        "美甲", "化妆", "护肤", "美妆", "卸妆",
        "防晒霜", "隔离霜", "散粉", "定妆",
    ],
    "家电家居": [
        "家电", "冰箱", "洗衣机", "空调", "电视机", "电视柜",
        "电视盒", "扫地机器人", "空气净化", "加湿", "电饭煲",
        "空气炸锅", "炸锅", "微波炉", "净水", "热水器",
        "家具", "沙发", "床", "床垫", "窗帘", "收纳",
        "灯具", "家居", "四件套", "被子", "枕头",
        "吹风机", "电动牙刷", "吸尘器", "挂烫机",
        "置物架", "衣架", "垃圾桶", "保鲜膜",
    ],
    "母婴玩具": [
        "母婴", "婴幼儿", "纸尿裤", "尿不湿", "玩具",
        "童车", "辅食", "孕婴", "拼豆", "拼图", "积木",
        "乐高", "手工", "diy", "粘土", "彩泥", "橡皮泥",
        "串珠", "钻石画", "盲盒", "手办",
        "婴儿车", "安全座椅", "学步鞋", "儿童餐",
        "奶瓶", "安抚奶嘴", "温奶器", "吸奶器",
    ],
    "数码配件": [
        "耳机", "充电宝", "数据线", "手机壳", "贴膜",
        "键盘", "鼠标", "音箱", "麦克风", "摄像头",
        "平板", "笔记本", "显示器", "路由器",
    ],
    "运动户外": [
        "瑜伽垫", "瑜伽", "跑步", "健身", "哑铃",
        "跳绳", "拉力器", "泳衣", "泳镜", "登山",
        "帐篷", "露营", "钓鱼", "自行车", "骑行",
        "滑板", "轮滑", "羽毛球", "乒乓球",
    ],
    "汽车用品": [
        "脚垫", "座套", "坐垫", "后备箱垫", "车衣",
        "车载", "方向盘套", "行车记录仪", "车膜",
        "机油", "汽车", "雨刮器", "车灯",
    ],
    "健康保健": [
        "保健品", "维生素", "瘦身", "体检", "牙齿",
        "蛋白粉", "益生菌", "鱼油", "钙片",
        "血压计", "体温计", "按摩仪", "泡脚桶",
    ],
    "黄金珠宝": [
        "金店", "珠宝", "钻石", "翡翠", "银饰",
        "项链", "戒指", "手镯", "耳钉", "耳环",
        "金饰", "铂金", "水晶", "珍珠",
    ],
    "宠物": [
        "猫粮", "狗粮", "修狗", "宠物用品", "撸猫", "吸猫",
        "猫砂", "猫窝", "狗窝", "宠物", "猫罐头",
        "猫条", "冻干", "驱虫",
    ],
    "文具办公": [
        "笔记本", "中性笔", "钢笔", "书包", "文具",
        "打印纸", "文件夹", "便签", "手账", "胶带",
    ],
    "厨房用品": [
        "锅", "炒锅", "砂锅", "刀具", "砧板",
        "保鲜盒", "饭盒", "水杯", "保温杯", "水壶",
    ],
}

_ALL_CAT_KEYWORDS = set()
for _kws in _CATS.values():
    _ALL_CAT_KEYWORDS.update(_kws)


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
    index = {}
    for cat_name, kws in cats.items():
        for kw in kws:
            index.setdefault(kw.lower(), []).append(cat_name)
    return index


def _dy_match_categories(term_lower, kw_index):
    hit_cats = set()
    for kw_lower, cat_names in kw_index.items():
        if kw_lower in term_lower:
            hit_cats.update(cat_names)
    return sorted(hit_cats)


def is_ecommerce_hot(word):
    """判断热搜词是否电商相关，返回 (是否保留, 原因, 品类)"""
    if not word or not isinstance(word, str):
        return False, "空值或非字符串", None
    word = word.strip()
    if not word:
        return False, "去除空白后为空", None
    if len(word) > MAX_HOT_WORD_LEN:
        return False, "超长", None
    if word.isdigit() or all(not c.isalnum() for c in word):
        return False, "纯数字或纯符号", None
    if word in _BLACKLIST_EXACT:
        return False, "命中精确黑名单", None
    word_lower = word.lower()
    for bw in _BLACKLIST_SUBSTR:
        if bw in word_lower:
            return False, "命中子串黑名单", None
    for cat_name, keywords in _CATS.items():
        for kw in keywords:
            if kw in word_lower:
                return True, "命中品类", cat_name
    return False, "未命中品类", None


# ======================== 通用小工具 ========================

def _run_py_script(script_path, timeout=1200):
    """同步运行一个 Python 脚本（用于爬虫），成功返回 True"""
    import sys as _sys
    try:
        subprocess.run([_sys.executable, script_path], check=False, timeout=timeout)
        return True
    except Exception as e:
        print(f'[选品] 脚本运行异常 {script_path}: {e}')
        return False


def _read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def _write_douyin_hot_progress(status, msg='', extra=None):
    data = {'status': status, 'message': msg}
    if extra:
        data.update(extra)
    _write_json(_DOUYIN_HOT_PROGRESS, data)


# ======================== 抖音热点宝：Cookie ========================

@app.route('/api/product-selection/douyin-hot/cookie', methods=['GET'])
def ps_douyin_hot_cookie_get():
    try:
        cookie = ''
        if os.path.exists(_DOUYIN_HOT_COOKIE_FILE):
            with open(_DOUYIN_HOT_COOKIE_FILE, 'r', encoding='utf-8') as f:
                cookie = f.read().strip()
        return success({'cookie': cookie, 'exists': bool(cookie)})
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/douyin-hot/cookie', methods=['POST'])
def ps_douyin_hot_cookie_save():
    try:
        data = request.get_json(silent=True) or {}
        cookie = (data.get('cookie') or '').strip()
        with open(_DOUYIN_HOT_COOKIE_FILE, 'w', encoding='utf-8') as f:
            f.write(cookie)
        return success({'saved': True}, '抖音热点宝 Cookie 已保存')
    except Exception as e:
        return fail(str(e))


# ======================== 抖音热点宝：抓取 + 自动筛选 ========================

def _run_douyin_hot_filter():
    """读取热点宝抓取结果，跑品类筛选，写入「抖音热搜品类表」（覆盖当天）"""
    _ensure_douyin_category_columns()
    hot = _read_json(_DOUYIN_HOT_FILE)
    if not hot:
        return {'total': 0, 'matched': 0, 'error': '暂无热点宝数据，请先抓取'}
    words = hot.get('words') or []
    date_str = (hot.get('date') or time.strftime('%Y-%m-%d')).strip()
    kw_index = _dy_build_kw_index(_CATS)
    matched = []
    for w in words:
        term = (w.get('word') or '').strip()
        if not term:
            continue
        tl = term.lower()
        if len(term) > MAX_HOT_WORD_LEN:
            continue
        if term in _BLACKLIST_EXACT:
            continue
        if any(b.lower() in tl for b in _BLACKLIST_SUBSTR):
            continue
        cats = _dy_match_categories(tl, kw_index)
        if not cats:
            continue
        heat_raw = str(w.get('hot_value') or '')
        matched.append({'term': term, 'heat_raw': heat_raw,
                        'categories': '/'.join(cats), 'heat_num': _dy_parse_heat(heat_raw)})
    db_execute("DELETE FROM `抖音热搜品类表` WHERE `日期` = %s", [date_str], fetch=False)
    for m in matched:
        db_execute(
            "INSERT INTO `抖音热搜品类表` (`热搜名`, `热搜值`, `日期`, `品类`, `是否电商`, `热度数值`) "
            "VALUES (%s, %s, %s, %s, 1, %s)",
            [m['term'], m['heat_raw'], date_str, m['categories'], m['heat_num']], fetch=False)
    return {'total': len(words), 'matched': len(matched), 'date': date_str}


def _run_scrape_and_filter():
    """抓取热点宝 → 品类筛选，写进度文件。手动触发与每日 8 点定时共用。"""
    try:
        _write_douyin_hot_progress('scraping', '正在抓取抖音热点宝热搜...')
        ok = _run_py_script(_DOUYIN_HOT_SCRAPER, timeout=600)
        if not ok:
            _write_douyin_hot_progress('error', '热点宝抓取脚本运行失败')
            return
        _write_douyin_hot_progress('filtering', '正在筛选电商品类...')
        result = _run_douyin_hot_filter()
        _write_douyin_hot_progress('done', '抓取并筛选完成',
                                   {'total': result.get('total'), 'matched': result.get('matched'),
                                    'date': result.get('date')})
    except Exception as e:
        traceback.print_exc()
        _write_douyin_hot_progress('error', '抓取筛选异常: ' + str(e))


_selection_scrape_thread = None


def _trigger_scrape_and_filter():
    global _selection_scrape_thread
    if _selection_scrape_thread and _selection_scrape_thread.is_alive():
        return False
    _selection_scrape_thread = threading.Thread(target=_run_scrape_and_filter, daemon=True,
                                                name='douyin-hot-scrape')
    _selection_scrape_thread.start()
    return True


@app.route('/api/product-selection/douyin-hot/scrape', methods=['POST'])
def ps_douyin_hot_scrape():
    """手动触发：抓取热点宝 + 自动筛选（后台执行）"""
    try:
        started = _trigger_scrape_and_filter()
        if not started:
            return fail('已有抓取任务在运行中')
        return success({'triggered': True}, '已触发热点宝抓取，完成后自动筛选')
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/douyin-hot/status', methods=['GET'])
def ps_douyin_hot_status():
    try:
        return success(_read_json(_DOUYIN_HOT_PROGRESS, {'status': 'idle', 'message': ''}))
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/douyin/filter', methods=['POST'])
def ps_douyin_filter():
    """对已抓取的热点宝数据重新跑一遍品类筛选（不重新抓取）"""
    try:
        result = _run_douyin_hot_filter()
        return success(result, '筛选完成')
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


# ======================== 爱搜：Cookie ========================

@app.route('/api/product-selection/aisou/cookie', methods=['GET'])
def ps_aisou_cookie_get():
    """读取爱搜登录态（localStorage JSON，核心 token）"""
    try:
        ls = _read_json(_AISOU_LOCALSTORAGE_FILE)
        return success({'cookie': json.dumps(ls, ensure_ascii=False) if ls else '',
                        'exists': bool(ls)})
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/aisou/cookie', methods=['POST'])
def ps_aisou_cookie_save():
    """保存爱搜登录态：入参为 JSON.stringify(localStorage) 的输出字符串"""
    try:
        data = request.get_json(silent=True) or {}
        cookie = (data.get('cookie') or '').strip()
        try:
            ls = json.loads(cookie)
        except Exception:
            return fail('请粘贴 JSON.stringify(localStorage) 的输出（登录态在 localStorage，不是 cookie）')
        if not isinstance(ls, dict):
            return fail('登录态格式错误，应为 JSON 对象')
        _write_json(_AISOU_LOCALSTORAGE_FILE, ls)
        return success({'saved': True}, '爱搜登录态已保存')
    except Exception as e:
        return fail(str(e))


# ======================== 选品流程：当日热搜选品分析（第一轮 50 卡片） ========================

def _ps_latest_douyin_categories():
    """读取「抖音热搜品类表」最新一天筛选结果，返回 [{term, category, heat}]"""
    rows = list(db_execute(
        "SELECT 热搜名, 热搜值, 品类, 日期 FROM `抖音热搜品类表` ORDER BY `日期` DESC, `热度数值` DESC LIMIT 500"))
    seen = set()
    items = []
    for r in rows:
        term = (r['热搜名'] or '').strip()
        if not term or term in seen:
            continue
        seen.add(term)
        items.append({'term': term, 'heat': r['热搜值'], 'category': r['品类'],
                      'date': str(r['日期']) if r['日期'] else ''})
    return items


def _ps_tmall_by_price_range(min_p, max_p):
    """按价格区间读天猫榜单，返回去重产品 [{name, price, category}]"""
    conds, params = [], []
    if min_p is not None:
        conds.append('价格 >= %s'); params.append(min_p)
    if max_p is not None:
        conds.append('价格 <= %s'); params.append(max_p)
    where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
    rows = list(db_execute(
        f"SELECT 类别名, 产品名, 价格 FROM `天猫榜单表` {where} LIMIT 2000", params))
    best = {}
    for r in rows:
        name = r['产品名']
        price = float(r['价格']) if r['价格'] is not None else None
        if name not in best or (price is not None and (best[name]['price'] is None or price < best[name]['price'])):
            best[name] = {'name': name, 'price': price, 'category': r['类别名']}
    return list(best.values())


def _ps_parse_json_array(raw):
    """从 DeepSeek 返回文本提取 JSON 数组"""
    if not raw:
        return []
    s = raw.strip()
    s = re.sub(r'```(?:json)?', '', s).strip()
    i, j = s.find('['), s.rfind(']')
    if i == -1 or j <= i:
        return []
    try:
        data = json.loads(s[i:j + 1])
        return [c for c in data if isinstance(c, dict)]
    except Exception:
        return []


def _ps_agent_parse_cards(raw):
    """从 DeepSeek 返回文本提取推荐卡片 JSON 数组（兼容 {'cards':[...]} 结构）；失败返回 []"""
    if not raw:
        return []
    s = raw.strip()
    s = re.sub(r'```(?:json)?', '', s).strip()
    candidates = [s]
    i, j = s.find('['), s.rfind(']')
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


@app.route('/api/product-selection/selection', methods=['POST'])
def ps_selection_round1():
    """第一轮：当日热搜选品分析，按价格区间返回约 50 个具体商品卡片"""
    try:
        payload = request.get_json(silent=True) or {}
        min_p = _ps_parse_float(payload.get('minPrice'))
        max_p = _ps_parse_float(payload.get('maxPrice'))

        dy_items = _ps_latest_douyin_categories()
        tmall_items = _ps_tmall_by_price_range(min_p, max_p)
        price_label = f"{min_p if min_p is not None else '不限'} ~ {max_p if max_p is not None else '不限'}"

        ctx = {
            '日期': dy_items[0]['date'] if dy_items else '',
            '抖音电商热搜品类': dy_items[:150],
            '天猫榜单商品(已按价格区间过滤)': tmall_items[:300],
            '价格区间': price_label,
        }
        sys_p = (
            '你是电商选品智能体，服务同时经营抖音和天猫的商家。请基于给定数据，选出约 50 个有潜力的具体商品。\n'
            '硬性要求：\n'
            '1. 必须是具体单品，例如「拼豆智能板」「车载香薰」「宠物冻干」，绝不要返回「衣服」「汽车用品」这类大品类名；\n'
            '2. 若某个抖音热搜品类过大（如「家居」），请自行裂变出具体小品（如「收纳盒」「懒人沙发」「香薰」）；\n'
            '3. 每个商品价格区间要贴合用户给出的价格区间和市场客单价（大众能接受的价格段）；\n'
            '4. 只基于给定数据推断，不编造具体销量数字。\n'
            '输出一个 JSON 数组（不要任何解释文字），每项字段：\n'
            '{"name":"商品名","category":"品类","price_range":"如 ¥39-89","reason":"一句话选品理由"}\n'
            '数组长度 45~55，商品名不要重复。'
        )
        user_msg = f'知识库数据（JSON）：\n{json.dumps(ctx, ensure_ascii=False)}\n\n请按要求输出约 50 个商品的 JSON 数组。'
        raw = call_deepseek_api(sys_p, user_msg, temperature=0.5, max_tokens=6000,
                                api_key=DEEPSEEK_SELECTION_API_KEY)
        cards = _ps_parse_json_array(raw)
        for i, c in enumerate(cards):
            c['id'] = i
        return success({'cards': cards, 'priceRange': {'min': min_p, 'max': max_p},
                        'meta': {'douyin_count': len(dy_items), 'tmall_count': len(tmall_items)}}, 'ok')
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


# ======================== 选品流程：第二轮（勾选10品 → 爱搜+天猫 → 3+9 → 存档） ========================

def _aisou_enrich(products):
    """对给定商品名跑爱搜采集，结果写入「爱搜数据表」"""
    _ensure_aisou_columns()
    _write_json(_AISOU_INPUT_FILE, {'keywords': products})
    if not _run_py_script(_AISOU_SCRAPER, timeout=1800):
        return 0
    out = _read_json(_AISOU_OUTPUT_FILE)
    if not out:
        return 0
    today = time.strftime('%Y-%m-%d')
    inserted = 0
    for res in out.get('results') or []:
        src = res.get('keyword') or ''
        # 先删除当天该来源词的旧数据，再插入新数据（幂等，重复抓取不冲突）
        db_execute("DELETE FROM `爱搜数据表` WHERE `日期` = %s AND `来源词` = %s", [today, src], fetch=False)
        for w in res.get('words') or []:
            name = (w.get('name') or '').strip()
            if not name:
                continue
            wtype = w.get('type') or ''
            # 电商词关键词 是复合主键的一部分（NOT NULL），填「类型_词名」保证唯一
            ek = f"{wtype}_{name}"
            db_execute(
                "INSERT INTO `爱搜数据表` (`日期`, `电商词关键词`, `来源词`, `词类型`, `词名称`, `月覆盖人次`, `七日搜索人次`) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [today, ek, src, wtype, name, w.get('month') or '', w.get('seven') or ''], fetch=False)
            inserted += 1
    return inserted


def _select_3_plus_9(products):
    """把爱搜数据交给 DeepSeek，按「3 可用品 + 9 裂变品」逻辑筛选"""
    rows = list(db_execute(
        "SELECT 来源词, 词类型, 词名称, 月覆盖人次, 七日搜索人次 FROM `爱搜数据表` "
        "ORDER BY `日期` DESC LIMIT 2000"))
    groups = {}
    for r in rows:
        groups.setdefault(r['词类型'], []).append({
            'name': r['词名称'], 'month': r['月覆盖人次'], 'seven': r['七日搜索人次'],
            'source': r['来源词']})
    sys_p = (
        '你是电商选品智能体。基于给定的爱搜搜索词数据（按 搜索词/相关词/下拉词/电商词 分组，'
        '每个词含 name 词名、month 月覆盖人次、seven 七日搜索人次），完成两件事：\n'
        '第一步：数据清洗与标准化\n'
        '- 去掉热度全为 0 的词、重复词、明显非商品词\n'
        '- 对四类热度分别做 min-max 归一化（缩放 0~1）\n'
        '- 综合热度分 = 电商词热度×0.4 + 搜索词热度×0.3 + 下拉词热度×0.15 + 相关词热度×0.15\n'
        '第二步：选出 3 个「可用品」\n'
        '- 综合分排序取 Top 候选池，品类尽量分散（不同品类），同品类只留综合分最高的 1 个\n'
        '- 优先「热度高但竞争中等」，避开红海词\n'
        '第三步：每个可用品裂变出 3 个「裂变品」\n'
        '- 只看该可用品的下拉词/相关词：裂变分 = 下拉词热度×0.5 + 相关词热度×0.5\n'
        '- 裂变品不能与可用品重复、去掉品牌词、语义去重、必须仍指向可购买商品\n'
        '输出 JSON 数组（不要解释文字）：\n'
        '[{"可用品":"名称","品类":"xx","裂变品":["a","b","c"]}]  （3 个元素）'
    )
    user_msg = '爱搜词数据（JSON）：\n' + json.dumps(groups, ensure_ascii=False) + \
        '\n\n待选商品：' + '、'.join(products) + '\n请输出 3 个可用品及其各 3 个裂变品的 JSON 数组。'
    raw = call_deepseek_api(sys_p, user_msg, temperature=0.4, max_tokens=3000,
                            api_key=DEEPSEEK_SELECTION_API_KEY)
    parsed = _ps_parse_json_array(raw)
    result = []
    for p in parsed:
        name = p.get('可用品') or p.get('name') or ''
        variants = p.get('裂变品') or p.get('variants') or []
        if name:
            result.append({'name': name, 'category': p.get('品类') or '', 'variants': variants[:3]})
    return result


def _tmall_enrich(products):
    """对 3 可用品 + 9 裂变品跑天猫销量前100，返回 {name: products}"""
    _write_json(_TMALL_BATCH_INPUT, {'keywords': products})
    if not _run_py_script(_TMALL_BATCH_SCRAPER, timeout=2400):
        return {}
    out = _read_json(_TMALL_BATCH_OUTPUT)
    if not out:
        return {}
    return {r.get('keyword'): r.get('products') or [] for r in out.get('results') or []}


def _final_analyze(selection, tmall_map, price_label):
    """把天猫 top100 数据交给 DeepSeek，产出 3 大卡 + 9 小卡 + 300 字建议"""
    for s in selection:
        s['tmall_products'] = tmall_map.get(s['name'], [])
        s['variants'] = [{'name': v, 'tmall_products': tmall_map.get(v, [])}
                         for v in s.get('variants', [])]
    sys_p = (
        '你是电商选品智能体。基于给定的天猫销量前100数据（每个商品含 rank/title/price/sales/shop），'
        '对每个可用品及其裂变品做分析：\n'
        '- 把每个商品的天猫前100按价格归到 3 个价格段，统计每段销量，做利润与成本分析；\n'
        '- 每个可用品和每个裂变品各写一句简短小结（一句话，点明机会点/价格带/竞争）；\n'
        '- 最后综合所有商品给一段 300 字左右的当日选品分析建议。\n'
        '输出 JSON 对象（不要解释文字）：\n'
        '{"products":[{"name":"可用品","category":"xx","summary":"一句话小结",'
        '"price_segments":[{"range":"如 0-50","sales":"如 12.3万"}],'
        '"profit":"利润成本分析","variants":[{"name":"裂变品","summary":"一句话小结"}]}],'
        '"advice":"300字当日选品建议"}'
    )
    user_msg = ('天猫销量前100数据（JSON）：\n' + json.dumps(selection, ensure_ascii=False) +
                '\n价格区间：' + price_label + '\n请输出上述 JSON 对象。')
    raw = call_deepseek_api(sys_p, user_msg, temperature=0.4, max_tokens=5000,
                            api_key=DEEPSEEK_SELECTION_API_KEY)
    s = (raw or '').strip()
    s = re.sub(r'```(?:json)?', '', s).strip()
    i, j = s.find('{'), s.rfind('}')
    if i != -1 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            return None
    return None


def _run_selection_analyze(products, min_p, max_p):
    """后台任务：勾选10品 → 爱搜 → 3+9 → 天猫 → 分析 → 存档"""
    price_label = f"{min_p if min_p is not None else '不限'} ~ {max_p if max_p is not None else '不限'}"

    def _progress(status, msg, done=0, total=0):
        _write_json(_SELECTION_JOB_FILE, {'status': status, 'message': msg, 'done': done, 'total': total})

    try:
        _progress('aisou', '正在爱搜抓取 10 个商品搜索词...')
        _aisou_enrich(products)

        _progress('select', '正在筛选 3 可用品 + 9 裂变品...')
        selection = _select_3_plus_9(products)
        if not selection:
            _progress('error', '智能体未能筛选出可用品')
            return

        all_names = [s['name'] for s in selection]
        for s in selection:
            all_names += [v for v in s.get('variants', [])]
        _progress('tmall', '正在天猫抓取销量前100（{} 个商品）...'.format(len(all_names)))
        tmall_map = _tmall_enrich(all_names)

        _progress('analyze', '正在生成最终选品分析...')
        final = _final_analyze(selection, tmall_map, price_label)
        if not final:
            _progress('error', '最终分析生成失败')
            return

        today = time.strftime('%Y-%m-%d')
        result = {'date': today, 'priceRange': {'min': min_p, 'max': max_p},
                  'priceLabel': price_label, 'data': final}
        _write_json(_SELECTION_RESULT_FILE, result)

        _ensure_selection_record_table()
        db_execute(
            "INSERT INTO `选品记录表` (`日期`, `价格区间`, `结果`) VALUES (%s, %s, %s)",
            [today, price_label, json.dumps(result, ensure_ascii=False)], fetch=False)

        _progress('done', '选品分析完成', done=1, total=1)
    except Exception as e:
        traceback.print_exc()
        _progress('error', '选品分析异常: ' + str(e))


_selection_job_thread = None


@app.route('/api/product-selection/selection/analyze', methods=['POST'])
def ps_selection_analyze():
    """第二轮：提交勾选的 10 个商品，后台执行完整选品流程"""
    global _selection_job_thread
    try:
        payload = request.get_json(silent=True) or {}
        products = payload.get('products') or []
        if not products:
            return fail('请先勾选商品')
        products = [str(p).strip() for p in products if str(p).strip()][:10]
        if _selection_job_thread and _selection_job_thread.is_alive():
            return fail('已有选品任务在运行中')
        min_p = _ps_parse_float(payload.get('minPrice'))
        max_p = _ps_parse_float(payload.get('maxPrice'))
        _selection_job_thread = threading.Thread(
            target=_run_selection_analyze, args=(products, min_p, max_p),
            daemon=True, name='selection-analyze')
        _selection_job_thread.start()
        return success({'started': True}, '已开始选品分析，请稍候')
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/selection/status', methods=['GET'])
def ps_selection_status():
    try:
        return success(_read_json(_SELECTION_JOB_FILE, {'status': 'idle', 'message': ''}))
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/selection/result', methods=['GET'])
def ps_selection_result():
    try:
        return success(_read_json(_SELECTION_RESULT_FILE))
    except Exception as e:
        return fail(str(e))


# ======================== 爱搜上升词 ========================

@app.route('/api/product-selection/rising', methods=['POST'])
def ps_rising():
    """爱搜上升词：基于爱搜数据表分析，返回 5 个热度上升的产品词"""
    try:
        rows = list(db_execute(
            "SELECT 词名称, 词类型, 月覆盖人次, 七日搜索人次 FROM `爱搜数据表` ORDER BY `日期` DESC LIMIT 3000"))
        # 按词名称聚合，取七日搜索人次最大代表热度
        agg = {}
        for r in rows:
            name = (r['词名称'] or '').strip()
            if not name:
                continue
            seven = _as_parse_num(r['七日搜索人次'])
            month = _as_parse_num(r['月覆盖人次'])
            if name not in agg or seven > agg[name]['seven']:
                agg[name] = {'name': name, 'type': r['词类型'], 'month': r['月覆盖人次'],
                             'seven': r['七日搜索人次'], 'seven_num': seven, 'month_num': month}
        words = sorted(agg.values(), key=lambda x: -x['seven_num'])[:40]
        sys_p = (
            '你是电商选品智能体。基于给定的爱搜词数据（词名、类型、月覆盖人次、七日搜索人次），'
            '找出近期热度上升最明显、最值得关注的 5 个产品词（不一定是电商词，相关词条都可以）。\n'
            '输出 JSON 数组（不要解释文字）：[{"word":"词","type":"类型","reason":"一句话理由"}]（5 个）'
        )
        user_msg = '爱搜词数据（JSON）：\n' + json.dumps(words, ensure_ascii=False) + '\n请输出 5 个上升词。'
        raw = call_deepseek_api(sys_p, user_msg, temperature=0.4, max_tokens=1500,
                                api_key=DEEPSEEK_SELECTION_API_KEY)
        cards = _ps_parse_json_array(raw)
        return success({'cards': cards, 'total': len(agg)})
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


# ======================== 历史选品记录 ========================

@app.route('/api/product-selection/history/dates', methods=['GET'])
def ps_history_dates():
    try:
        _ensure_selection_record_table()
        rows = db_execute("SELECT DISTINCT `日期` FROM `选品记录表` ORDER BY `日期` DESC")
        return success({'dates': [str(r['日期']) for r in rows]})
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/history', methods=['GET'])
def ps_history():
    try:
        _ensure_selection_record_table()
        d = request.args.get('date', '').strip()
        if d:
            rows = list(db_execute("SELECT `日期`, `价格区间`, `结果` FROM `选品记录表` WHERE `日期` = %s ORDER BY `创建时间` DESC LIMIT 1", [d]))
        else:
            rows = list(db_execute("SELECT `日期`, `价格区间`, `结果` FROM `选品记录表` ORDER BY `创建时间` DESC LIMIT 1"))
        if not rows:
            return success(None)
        r = rows[0]
        result = None
        try:
            result = json.loads(r['结果'])
        except Exception:
            result = {'date': str(r['日期']), 'priceLabel': r['价格区间'], 'data': r['结果']}
        return success(result)
    except Exception as e:
        return fail(str(e))


# ======================== 每日 8 点自动抓取 + 筛选 ========================

def _douyin_hot_auto_loop():
    """后台线程：每天 8 点自动抓取抖音热点宝并筛选"""
    while True:
        try:
            now = datetime.now()
            next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            time.sleep((next_run - now).total_seconds())
            print('[选品][定时] 开始每日热点宝抓取')
            _run_scrape_and_filter()
        except Exception as e:
            print(f'[选品][定时] 异常: {e}')


_douyin_hot_auto_thread = threading.Thread(target=_douyin_hot_auto_loop, daemon=True,
                                           name='douyin-hot-auto')
_douyin_hot_auto_thread.start()



# ======================== 启动 ========================

def _seeding_auto_update_loop():
    """后台线程：每小时自动触发一次作品抓取（抖音 + 小红书），随进程存活"""
    while True:
        time.sleep(3600)
        try:
            for platform in ('douyin', 'xhs'):
                err = _seeding_launch(platform)
                if err:
                    print(f'[种草][自动更新] {platform} 触发失败: {err}')
                time.sleep(5)  # 两个平台错开，避免同时打满
        except Exception as e:
            print(f'[种草][自动更新] 触发异常: {e}')


# daemon 线程，gunicorn 单 worker 下只启动一次；随进程退出自动结束
_seeding_auto_thread = threading.Thread(target=_seeding_auto_update_loop, daemon=True, name='seeding-auto-update')
_seeding_auto_thread.start()


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
