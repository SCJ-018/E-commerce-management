"""
电商后台管理系统 - Flask API 服务
连接远程 MySQL 数据库，提供 RESTful API
启动方式：python app.py  （默认监听 0.0.0.0:5000）
"""
import os
import json
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
import pymysql

from config import DB_CONFIG, DB_POOL_SIZE, DB_PING_BEFORE_QUERY, DEEPSEEK_API_KEY, DEEPSEEK_API_URL, DEEPSEEK_MODEL

app = Flask(__name__)
CORS(app)


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

def call_deepseek_api(system_prompt, user_message, temperature=0.3, max_tokens=4096):
    """调用 DeepSeek API 生成分析报告，返回文本内容；失败返回 None"""
    headers = {
        'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
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
    }


def _build_fallback_template(data, refund_rate, roi, conv, aov, warnings):
    """当 DeepSeek API 不可用时使用的纯模板分析报告"""
    sm = data['summary']
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

    return f"""
<div class="analysis-section">
  <h3><i class="fa-solid fa-coins"></i> 营销数据综合分析</h3>
  <p>净支付金额 <span class="highlight">&yen;{sm['netPayment']:,.2f}</span>，退款金额 <span class="highlight">&yen;{sm['refundAmount']:,.2f}</span>，退款率 <span class="{'warn' if refund_rate > 20 else 'highlight'}">{refund_rate:.2f}%</span>。</p>
  <p>推广花费 <span class="highlight">&yen;{sm['adSpend']:,.2f}</span>，推广总成交 <span class="highlight">&yen;{sm['adTotal']:,.2f}</span>，ROI <span class="{'danger' if roi < 1.0 else 'highlight'}">{roi:.2f}</span>。</p>
  <p>访客数 <span class="highlight">{sm['visitors']:,}</span>，支付买家数 <span class="highlight">{sm['payers']:,}</span>，支付转化率 <span class="{'warn' if conv < 2 else 'highlight'}">{conv:.2f}%</span>，客单价 <span class="highlight">&yen;{aov:.2f}</span>。</p>
  {platforms_html}
</div>
<div class="analysis-section">
  <h3><i class="fa-solid fa-chart-simple"></i> 单链接数据综合分析</h3>
  <p>下表为抖店/京东/千牛单链接数据（按日期汇总）。<span class="highlight">综合消耗产出比 = 成交金额 ÷ (投放消耗+佣金+补贴)</span>，低于 1 表示投入产出倒挂、整体亏损。</p>
  {links_html}
</div>
<div class="analysis-section">
  <h3><i class="fa-solid fa-bullhorn"></i> 千牛单链接推广数据分析</h3>
  <p>千牛推广数据：推广消耗与直接/总引导成交的产出比。<span class="highlight">总ROI = 总引导成交金额 ÷ 推广消耗</span>，低于 1 表示推广投入亏损。</p>
  {promo_html}
</div>
<div class="analysis-section">
  <h3><i class="fa-solid fa-shop"></i> 店铺经营分析</h3>
  {stores_html}
  {"<ul class=\"da-warn-list\">" + ''.join(store_warnings) + "</ul>" if store_warnings else "<p style=\"color:#16a34a\">各店铺核心指标均处于正常范围。</p>"}
</div>
<div class="analysis-section">
  <h3><i class="fa-solid fa-triangle-exclamation"></i> 异常预警与建议</h3>
  <ul class="da-warn-list">{warn_html}{link_warn_html}{promo_warn_html}</ul>
  <p style="margin-top:12px;color:#94a3b8;font-size:0.85rem">注：AI 分析服务暂不可用，以上为模板规则生成的简化版报告。</p>
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
            ad_spend = float(trow['ad_spend'])
            pay = int(trow['pay'])
            vis = int(trow['vis'])
            cart = int(trow['cart'])
            day_payment = float(trow['payment'])
            conv = float(trow['conv_rate'])
            order_rr = float(trow['order_refund_rate'])
            aov = float(trow['aov'])
            ad_total = float(trow['ad_total'])

            trends.append({
                'date': str(trow['日期']),
                'netPayment': net,
                'refundAmount': refund,
                'refundRate': round(refund / net * 100, 2) if net > 0 else 0,
                'adSpend': ad_spend,
                'roi': round(ad_total / ad_spend, 4) if ad_spend > 0 else 0,
                'visitors': vis,
                'payers': pay,
                'payment': day_payment,
                'cart': cart,
                'convRate': round(conv * 100, 2),
                'orderRefundRate': round(order_rr * 100, 2),
                'aov': round(aov, 2),
                'adTotal': ad_total,
            })
            agg['totalVisitors'] += vis
            agg['totalCart'] += cart
            agg['totalPayers'] += pay
            agg['totalPayment'] += day_payment
            agg['totalRefund'] += refund
            agg['totalAdSpend'] += ad_spend
            agg['totalAdRev'] += ad_total
            agg['totalAdTotal'] += ad_total

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
        system_prompt = """你是一位资深的电商数据分析师。请根据提供的店铺营销数据、抖店/京东/千牛单链接销售数据与千牛单链接推广数据，撰写一份专业、有洞察力的每日数据分析报告。

报告必须包含以下部分：
1. 【营销数据综合分析】- 分析店铺营销数据整体表现：净支付金额、退款金额、退款率、推广花费、ROI、访客数、支付转化率、客单价；对比各平台表现差异
2. 【单链接数据综合分析】- 汇总抖店/京东/千牛三平台单链接数据的成交金额、订单数、买家数、退款、客单价，做横向对比，找出表现最好和最差的平台
3. 【各平台单链接数据单独分析】- 分别深入分析抖店、京东、千牛各自的成交、退款、客单价、流量表现，点名异常并给出针对性建议
4. 【千牛单链接推广数据分析】- 分析千牛推广数据的推广消耗、直接引导成交、总引导成交、直接ROI、总ROI、点击率、单次点击成本等，评估推广投放效率
5. 【推广花费/消耗与产出比专项分析】- 这是本报告的重点。重点分析推广花费、投放消耗、佣金、补贴等成本与产出的比例（投放产出比、综合消耗产出比、千牛推广ROI），识别亏损或低效投放，给出具体可行的优化建议
6. 【店铺经营分析】- 对比各店铺的核心指标（净支付、退款率、转化率、客单价），找出表现优异和需要关注的店铺
7. 【异常预警与建议】- 汇总整体、各平台、各店铺的突出异常指标（尤其退款率偏高、消耗产出比/推广ROI低于1的亏损情况），给出具体可执行的优化建议

输出格式要求：
- 直接输出HTML代码片段（不含```html标记，不含<!DOCTYPE>、<html>、<head>、<body>标签），只输出<body>内部的内容
- 使用 <div class="analysis-section"> 包裹每个大段
- 使用 <h3> 作段落标题并用 <i> 标签加Font Awesome图标
- 数据对比用 <table class="da-table"> 展示，各对比表务必包含所有提供的数据
- 关键数据用 <span class="highlight"> 标亮，预警用 <span class="warn"> 或 <span class="danger"> 标记
- 对问题平台、亏损投放和问题店铺要给出点名分析和具体建议
- 风格专业、简洁，数据准确"""

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

## 系统预判预警
{chr(10).join([f"- {w['dim']}：{w['value']}（级别：{w['level']}）" for w in warnings]) if warnings else '无显著异常'}

## 店铺级预警
{chr(10).join([f"- {s['store']}：{s['issues']}（级别：{s['level']}）" for s in store_warnings]) if store_warnings else '各店铺指标正常'}

## 单链接消耗产出比预警
{chr(10).join([f"- [{w['platform']}] {w['dim']}：{w['value']}（级别：{w['level']}）{w['msg']}" for w in link_warnings]) if link_warnings else '单链接数据消耗产出比均正常'}

## 千牛推广预警
{chr(10).join([f"- [{w['platform']}] {w['dim']}：{w['value']}（级别：{w['level']}）{w['msg']}" for w in promo_warnings]) if promo_warnings else '千牛推广数据ROI正常'}

请为以上所有数据生成专业的 HTML 分析报告。重点：①推广花费/投放消耗/佣金/补贴等成本与产出的比例，亏损或低效投放必须点名分析并给出建议；②千牛单链接推广数据的 ROI 表现；③对各平台单链接数据做综合+单独分析。"""

        # 调用 DeepSeek
        ai_html = call_deepseek_api(system_prompt, user_message)

        # 构建完整 HTML（模板头部 + AI 内容）
        template_header = f"""<div class="da-report">
<div class="da-header">
  <div class="da-date-badge">
    <i class="fa-solid fa-calendar-check"></i>
    分析日期：{target_date}
  </div>
  <div class="da-meta">
    <span>报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}</span>
    <span>数据来源：店铺营销数据 + 抖店/京东/千牛单链接数据 + 千牛推广数据</span>
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

        if ai_html:
            full_html = template_header + ai_html + template_footer
            gen_by = 'ai'
        else:
            full_html = template_header + _build_fallback_template(data, refund_rate, roi, conv, aov, warnings) + template_footer
            gen_by = 'template'

        # 写入数据库（先删旧记录再插新记录，实现覆盖式更新）
        db_execute(
            'DELETE FROM daily_analysis_reports WHERE report_date = %s',
            [target_date], fetch=False
        )
        new_id = db_execute_insert(
            """INSERT INTO daily_analysis_reports (report_date, html_report, metrics_json, status, error_msg)
               VALUES (%s, %s, %s, %s, %s)""",
            [target_date, full_html, json.dumps(data, ensure_ascii=False),
             'success', '' if ai_html else 'DeepSeek API不可用，已使用模板生成基本报告']
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
        data = request.get_json(silent=True) or {}
        images = data.get('images', [])
        if not images:
            return jsonify({'error': '未提供图片数据'}), 400

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
    """多日区间聚合：每条链接合并为一行，金额/整数求和，比率/小数求均值，文本取首个，日期显示区间。"""
    if not rows:
        return rows
    ftype = {f['key']: f['type'] for f in fields}
    text_keys = [f['key'] for f in fields if f['type'] == 'text']
    date_keys = [f['key'] for f in fields if f['type'] == 'date']
    range_label = f'{start_date} ~ {end_date}'

    groups = {}  # 分组键 -> 聚合中间态
    for r in rows:
        key = tuple([r.get('platform')] + [r.get(k) for k in text_keys])
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
            if t == 'money':
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

        # 多日区间：按链接聚合，每条链接一行，避免「近7天/近30天」把同一链接拆成多行不同日期
        if start_date and end_date and start_date != end_date:
            rows = _od_aggregate(rows, fields, start_date, end_date)

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
