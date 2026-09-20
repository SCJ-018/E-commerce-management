"""
电商后台管理系统 - Flask API 服务
连接远程 MySQL 数据库，提供 RESTful API
启动方式：python app.py  （默认监听 0.0.0.0:5000）
"""
import os
import json
import re
import math
import traceback
import time
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from queue import Queue
from threading import Lock, RLock
import threading  # ★ 必须在顶层导入：模块级（约 4200 行）的 Event/Lock 会用到，晚导入即 NameError
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import RequestEntityTooLarge
import pymysql

from config import DB_CONFIG, DB_POOL_SIZE, DB_PING_BEFORE_QUERY, DEEPSEEK_API_KEY, DEEPSEEK_API_URL, DEEPSEEK_MODEL, DEEPSEEK_SELECTION_API_KEY

# 通告正文美化可使用独立 Key，避免与日报 / 选品等任务共用配额；
# 未单独配置时兼容回退到全局 DeepSeek Key。
_ANNOUNCE_AI_API_KEY = os.environ.get('ANNOUNCE_AI_API_KEY', '').strip()

# 品类分类规则（供「品类营销数据」返回各品类的命中关键词）
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), 'tools', 'category_mapper'))
    import mapper as _category_mapper
except Exception:
    _category_mapper = None

# 钉钉推送（每日分析报告自动发到指定成员）
try:
    from dingtalk import DingTalkClient, DingTalkError
except Exception as _dt_err:  # 缺文件/缺依赖时不影响主服务启动，推送功能不可用
    DingTalkClient = None
    print('[钉钉推送] 模块加载失败: %s' % _dt_err)

    class DingTalkError(Exception):
        pass

# 日报「长图」渲染（钉钉图片消息用；重依赖 Playwright 放在独立模块里，便于单独验证）
# ⚠ 模块内不做任何副作用动作，导入失败只降级为「日报退回不发长图」，不影响主服务
try:
    from report_image import render_report_image as _render_report_image
except Exception as _ri_err:
    _render_report_image = None
    print('[报告长图] 模块加载失败: %s' % _ri_err)

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


# ======================== 开发告警：安全入口 ========================
# 「后台任何报错都直接钉钉给李自豪」的统一入口，实现在 backend/dev_alert.py。
# 这里统一走 globals() 取函数（模块未加载完就被调用也不会 NameError），
# 并且告警自身出任何问题都必须静默 —— 绝不能把主流程带崩。

def _dev_alert(title, detail='', signature=None, source='', force=False):
    try:
        fn = globals().get('notify_dev')
        if fn is None:
            print('[开发告警][未加载] %s' % title)
            return False
        return fn(title, detail, signature=signature, source=source, force=force)
    except Exception:
        return False


def _dev_alert_exc(title, exc, extra='', signature=None, source=None):
    """异常告警。

    title  : 异常标题（如「接口未捕获异常」），传给 notify_dev_exception 当 source
    source : 子系统标签（如「种草定时任务」），可选，拼进标题便于在钉钉里归堆

    ★ 2026-09-17 修：原签名是 (source, exc, extra, signature)，但调用方有 9 处按
      _dev_alert() 的习惯传了 source= 关键字 → 参数绑定阶段即抛
      TypeError: _dev_alert_exc() got multiple values for argument 'source'。
      该异常发生在**进入函数体之前**，函数内 try/except 拦不住，于是这些告警路径
      （后台线程/主线程兜底、每日报告 ×2、脚本调度、抖音热点、种草 ×3）全部是哑弹、
      从未真正发出。现把 source 收成合法关键字参数并与 title 合并。
    """
    try:
        label = ('%s / %s' % (source, title)) if source else title
        fn = globals().get('notify_dev_exception')
        if fn is None:
            print('[开发告警][未加载] %s: %s' % (label, exc))
            return False
        return fn(label, exc, extra=extra, signature=signature)
    except Exception:
        return False


def _sql_brief(sql, limit=120):
    """SQL 摘要 —— 告警正文只放片段，避免报文过长/条件泄露"""
    try:
        s = ' '.join(str(sql).split())
        return s[:limit] + ('…' if len(s) > limit else '')
    except Exception:
        return '(无法解析 SQL)'


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
            # 重试仍失败 = 数据库真出问题了，直接告警开发（同签名 5 分钟冷却）
            _dev_alert_exc('数据库连接异常', e, extra='SQL: %s' % _sql_brief(sql),
                           signature='db:operational')
            raise
        except Exception as e:
            if conn:
                return_db(conn)
            traceback.print_exc()
            _dev_alert_exc('SQL 执行异常', e, extra='SQL: %s' % _sql_brief(sql),
                           signature='db:sql:%s' % _sql_brief(sql, 60))
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
            _dev_alert_exc('数据库连接异常（INSERT）', e, extra='SQL: %s' % _sql_brief(sql),
                           signature='db:operational')
            raise
        except Exception as e:
            if conn:
                return_db(conn)
            traceback.print_exc()
            _dev_alert_exc('SQL 执行异常（INSERT）', e, extra='SQL: %s' % _sql_brief(sql),
                           signature='db:sql:%s' % _sql_brief(sql, 60))
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
_AUTH_PUBLIC_PATHS = {
    '/api/auth/login', '/api/auth/logout', '/api/health',
    # ★ 本机滑块助手（tools/doudian_crawler/slider_agent.py）没有浏览器登录态，
    #   这两个接口在路由内用共享密钥 X-Slider-Key 自校验，比依赖 token 简单且不受
    #   「重启 ecom 全员掉线」影响（token 存内存，见 token 说明）。
    '/api/fetch/doudian/slider/pending', '/api/fetch/doudian/slider/report',
}


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


def _current_session():
    """取当前请求的登录会话记录（无则返回 {}）"""
    token = request.cookies.get('token', '')
    return _AUTH_TOKENS.get(token) or {}


# ★★ 「管理员与权限」页面权限模型（2026-09-18 调整）
#
# 三层结构：
#   第 1 层  开发人员 / 超级管理员 —— 全库账号 + 角色权限，无限制
#   第 2 层  部门主管             —— 只能管【本部门】账号；看不到「角色与权限」；
#                                   给账号赋的权限必须 ⊆ 自己的权限
#   第 3 层  普通员工             —— 无账号管理入口
#
# 部门主管的识别口径：admin_accounts.leader == 自己的 name（不新增角色、不改数据）。
#   现状命中 6 人：刘颖/和鹏伟/马湘湘/路颜欣/冯照辉（种草部 5 个业务部）+ 陈子轩（人事行政部）。
#
# ★ 与原行为的差异：ACCOUNT_MANAGER_ROLES 由 {开发人员,超级管理员,人事行政部} 收窄为
#   前两者 —— 人事行政部不再能进「账号列表」。这是需求明确要求的
#   「角色与权限界面仅有开发人员和超级管理员有权限」，一并收窄后语义才自洽。
ACCOUNT_MANAGER_ROLES = {'开发人员', '超级管理员'}

# ★ 「角色与权限」Tab 的硬门槛（与账号列表同集合；独立常量便于将来再拆）
ROLE_PERM_MANAGER_ROLES = {'开发人员', '超级管理员'}

# ★ 主管管辖判定的兜底口径：当「本人姓名」为空或部门为空时不做主管判定。
#   （leader 字段历史上有过填「上级姓名」而非自己的写法，故必须 leader==name 才算。）


def _can_manage_accounts(sess=None):
    """能否查看/编辑账号列表（第 1 层，全库）"""
    sess = sess if sess is not None else _current_session()
    return (sess.get('role') or '') in ACCOUNT_MANAGER_ROLES


def _can_manage_roles(sess=None):
    """能否查看/编辑「角色与权限」（仅第 1 层）"""
    sess = sess if sess is not None else _current_session()
    return (sess.get('role') or '') in ROLE_PERM_MANAGER_ROLES


def _role_permissions(role_name):
    """取某角色的权限列表；返回 (perms, 是否存在该角色)

    perms 为 list；'*' 以 ['*'] 表示（调用方自行判断全权）。
    """
    if not role_name:
        return [], False
    rows = db_execute('SELECT permissions FROM admin_roles WHERE name = %s', [role_name])
    if not rows:
        return [], False
    raw = rows[0]['permissions']
    if isinstance(raw, str):
        try:
            perms = json.loads(raw)
        except Exception:
            perms = []
    else:
        perms = raw or []
    return (perms if isinstance(perms, list) else []), True


def _is_all_perm(perms):
    """['*'] 或 '*' 视为全权"""
    if perms == '*':
        return True
    return bool(isinstance(perms, list) and perms and perms[0] == '*')


def _load_self_account(sess=None):
    """按登录会话取本人 admin_accounts 记录（无则 None）"""
    sess = sess if sess is not None else _current_session()
    acct = (sess.get('account') or '').strip()
    if not acct:
        return None
    rows = db_execute(
        'SELECT id, name, account, role, department, sub_dept, leader, status '
        'FROM admin_accounts WHERE account = %s', [acct])
    return rows[0] if rows else None


def _my_scope(sess=None):
    """当前登录者的管辖范围描述。

    {
      'level': 'super' | 'lead' | 'none',
      'role':      角色名,
      'department': 管辖部门（lead 时为本人部门；super 时为空表示不限）,
      'isLead':    是否为部门主管,
      'perms':     list —— 当前者可授权的权限上限（super 为 ['*']）,
      'allPerm':   bool —— 是否不受权限子集限制,
    }

    level 语义：
      super = 开发人员/超级管理员（全库 + 角色权限）
      lead  = 部门主管（本部门账号，权限 ⊆ 自己）
      none  = 无权进入账号管理页
    """
    sess = sess if sess is not None else _current_session()
    role = (sess.get('role') or '').strip()
    if role in ACCOUNT_MANAGER_ROLES:
        perms, _ = _role_permissions(role)
        return {'level': 'super', 'role': role, 'department': '', 'isLead': False,
                'perms': perms if perms else ['*'], 'allPerm': True}

    me = _load_self_account(sess)
    if not me:
        return {'level': 'none', 'role': role, 'department': '', 'isLead': False,
                'perms': [], 'allPerm': False}

    name = (me.get('name') or '').strip()
    dept = (me.get('department') or '').strip()
    # ★ leader == 本人姓名 且 部门非空 才判定为主管
    is_lead = bool(name) and bool(dept) and (me.get('leader') or '').strip() == name
    if is_lead:
        perms, _ = _role_permissions(role)
        return {'level': 'lead', 'role': role, 'department': dept, 'isLead': True,
                'perms': perms, 'allPerm': _is_all_perm(perms)}

    return {'level': 'none', 'role': role, 'department': dept, 'isLead': False,
            'perms': [], 'allPerm': False}


def _scope_can_touch(scope, target_row):
    """scope 是否有权查看/修改 target_row 这条账号记录"""
    if scope.get('level') == 'super':
        return True
    if scope.get('level') != 'lead':
        return False
    tgt_dept = (target_row.get('department') or '').strip()
    return bool(tgt_dept) and tgt_dept == scope.get('department')


def _perm_subset_of(sub, upper, upper_all=False):
    """sub 是否 ⊆ upper（upper_all=True 时无条件通过）。全部返回 (ok, 越权项)"""
    if upper_all:
        return True, []
    upper_set = set(upper or [])
    # 自己就是全权时，任何勾选都合法
    if '*' in upper_set:
        return True, []
    extra = [p for p in (sub or []) if p not in upper_set]
    return (not extra), extra


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
#
# ★★ 全部 4 个接口共用硬门槛：仅 开发人员 / 超级管理员（_can_manage_roles）。
#    部门主管与普通员工一律 403 语义（返回 code!=0 的 fail），
#    前端同样隐藏该 Tab —— 两层都拦，防止直接调接口。

@app.route('/api/admin/roles', methods=['GET'])
def admin_roles_list():
    """获取所有角色及其权限（仅开发人员 / 超级管理员）"""
    try:
        if not _can_manage_roles():
            return fail('无权查看角色与权限')
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
    """新增角色（仅开发人员 / 超级管理员）"""
    try:
        if not _can_manage_roles():
            return fail('无权新增角色')
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
    """更新角色权限（仅开发人员 / 超级管理员）"""
    try:
        if not _can_manage_roles():
            return fail('无权修改角色权限')
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
    """删除角色（仅开发人员 / 超级管理员）"""
    try:
        if not _can_manage_roles():
            return fail('无权删除角色')
        db_execute('DELETE FROM admin_roles WHERE id = %s', [role_id], fetch=False)
        return success(None, '角色已删除')
    except Exception as e:
        return fail(str(e))


@app.route('/api/admin/accounts', methods=['GET'])
def admin_list():
    """账号列表

    权限分两层（2026-09-18）：
      · 开发人员 / 超级管理员 —— 全库，可按部门筛选
      · 部门主管             —— **只返回本部门账号**（服务端强制，前端传什么都无效）
    """
    try:
        scope = _my_scope()
        if scope['level'] == 'none':
            return fail('无权查看账号列表')

        dept = (request.args.get('department') or '').strip()
        kw = (request.args.get('keyword') or '').strip()

        # ★ 列表刻意不返回 password：避免一次请求就把全库明文密码下发到前端。
        # 需要看某一条时走 GET /api/admin/accounts/<id>/password 单条读取。
        sql = ('SELECT id, name, account, role, status, last_login, created_at, '
               'department, sub_dept, leader, avatar, gender FROM admin_accounts')
        conds, params = [], []
        # ★ 主管：无条件追加本部门条件（不能靠前端传参决定，否则可越权拉全库）
        if scope['level'] == 'lead':
            conds.append('department = %s')
            params.append(scope['department'])
        if dept and dept not in ('全部', 'all'):
            conds.append('role = %s')
            params.append(dept)
        if kw:
            conds.append('(name LIKE %s OR account LIKE %s OR department LIKE %s)')
            params.extend(['%' + kw + '%'] * 3)
        if conds:
            sql += ' WHERE ' + ' AND '.join(conds)
        sql += ' ORDER BY id'

        rows = db_execute(sql, params)
        admins = []
        my_name = ''
        me = _load_self_account()
        if me:
            my_name = (me.get('name') or '').strip()
        for r in rows:
            admins.append({
                'id': r['id'],
                'name': r['name'],
                'account': r['account'],
                'role': r['role'],
                'department': r.get('department') or '',
                'subDept': r.get('sub_dept') or '',
                'leader': r.get('leader') or '',
                'avatar': r.get('avatar') or '',
                'gender': r.get('gender') or '',
                'status': r['status'],
                'lastLogin': r['last_login'] or '',
                'createdAt': str(r['created_at']) if r['created_at'] else '',
                # ★ 供前端置灰「编辑/删除」按钮：主管只能动本部门，且不能动自己
                'manageable': True if scope['level'] == 'super' else (
                    (r.get('department') or '').strip() == scope['department']),
                'isSelf': bool(my_name) and (r['name'] or '').strip() == my_name,
            })
        return success(admins)
    except Exception as e:
        return fail(str(e))


@app.route('/api/admin/my-scope', methods=['GET'])
def admin_my_scope():
    """当前登录者的管辖范围 —— 前端据此决定页面显示/锁定/置灰

    {
      level: 'super' | 'lead' | 'none',
      role, department, isLead, allPerm,
      perms:  可授权的权限上限（super 为 ['*']，代表全部）
      canManageRoles: 能否进「角色与权限」
    }
    """
    try:
        scope = _my_scope()
        out = dict(scope)
        out['canManageRoles'] = _can_manage_roles()
        out['canManageAccounts'] = scope['level'] != 'none'
        return success(out)
    except Exception as e:
        return fail(str(e))


@app.route('/api/admin/accounts/<int:aid>/password', methods=['GET'])
def admin_get_password(aid):
    """单条读取账号明文密码（点「眼睛」时才调用）

    ★ 与列表接口分离：列表不下发 password，避免一次请求就把全库明文密码
    送到前端；这里一次只返回一条，把风险从「全库」收窄为「单条」。
    权限：开发人员 / 超级管理员（全库）；部门主管（限本部门）。
    """
    try:
        scope = _my_scope()
        if scope['level'] == 'none':
            return fail('无权查看密码')
        rows = db_execute(
            'SELECT password, department FROM admin_accounts WHERE id = %s', [aid])
        if not rows:
            return fail('账号不存在')
        if not _scope_can_touch(scope, rows[0]):
            return fail('无权查看其他部门账号的密码')
        return success({'id': aid, 'password': rows[0]['password'] or ''})
    except Exception as e:
        return fail(str(e))


@app.route('/api/admin/accounts', methods=['POST'])
def admin_create():
    """新增账号

    权限分两层（2026-09-18）：
      · 开发人员 / 超级管理员 —— 任意角色、任意部门
      · 部门主管 —— 三条硬约束：
          ① role 强制为本人角色（不接受前端传值）
          ② department 强制为本人部门（不接受前端传值）
          ③ 新账号继承该角色的权限，必须 ⊆ 主管自己的权限（防越权提权）
    """
    try:
        scope = _my_scope()
        if scope['level'] == 'none':
            return fail('无权新增账号')
        data = request.get_json(force=True)
        name = data.get('name', '').strip()
        account = data.get('account', '').strip()
        password = data.get('password', '').strip()
        role = data.get('role', '').strip()
        status = data.get('status', 'enabled').strip()
        department = (data.get('department') or '').strip()
        sub_dept = (data.get('subDept') or data.get('sub_dept') or '').strip()
        leader = (data.get('leader') or '').strip()
        gender = (data.get('gender') or '').strip()

        if not name or not account or not password:
            return fail('请填写完整信息')

        if scope['level'] == 'lead':
            # ① 部门锁定：强制本部门，忽略前端传值
            if department and department != scope['department']:
                return fail('只能在本部门「%s」下新增账号' % scope['department'])
            department = scope['department']
            # ② 角色锁定：只能建自己那个角色
            if role and role != scope['role']:
                return fail('只能新增「%s」角色的账号' % scope['role'])
            role = scope['role']
            # ③ 权限子集：新账号继承本角色权限，但不得超出主管自己的权限
            want_perms = data.get('permissions')
            if isinstance(want_perms, list):
                base_perms = want_perms
            else:
                base_perms, _ = _role_permissions(role)
            ok, extra = _perm_subset_of(base_perms, scope['perms'], scope['allPerm'])
            if not ok:
                return fail('无权授予超出自身范围的权限：%s' % '、'.join(extra))
            # ★ 主管不得把新账号挂到别人名下：leader 只能填自己，或留空
            me = _load_self_account()
            my_name = (me.get('name') or '').strip() if me else ''
            if leader and leader != my_name:
                return fail('部门主管字段只能填写本人')

        # 检查账号是否已存在
        existing = db_execute('SELECT id FROM admin_accounts WHERE account = %s', [account])
        if existing:
            return fail('账号已存在')

        new_id = db_execute_insert(
            'INSERT INTO admin_accounts '
            '(name, account, password, role, status, department, sub_dept, leader, gender) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            [name, account, password, role, status, department, sub_dept, leader, gender]
        )
        return success({'id': new_id}, '账号已创建')
    except Exception as e:
        return fail(str(e))


@app.route('/api/admin/accounts/<int:aid>', methods=['PUT'])
def admin_update(aid):
    """更新账号信息（姓名、账号、密码、角色、状态、部门、主管、性别）

    权限分两层（2026-09-18）：
      · 开发人员 / 超级管理员 —— 任意账号
      · 部门主管 —— 仅本部门账号；且
          · 不能改 role（防把自己/部门升级成开发人员）
          · 不能改 department 到别的部门
          · 不能改 leader 为他人
          · 不能被自己提拔为「开发人员/超级管理员」等管理角色
    """
    try:
        scope = _my_scope()
        if scope['level'] == 'none':
            return fail('无权限修改账号')
        data = request.get_json(force=True)

        # 目标账号必须存在，且在当前者管辖范围内
        tgt = db_execute(
            'SELECT id, name, account, role, department FROM admin_accounts WHERE id = %s', [aid])
        if not tgt:
            return fail('账号不存在')
        tgt = tgt[0]
        if not _scope_can_touch(scope, tgt):
            return fail('只能管理本部门「%s」的账号' % scope['department'])

        me = _load_self_account()
        my_name = (me.get('name') or '').strip() if me else ''

        updates = []
        params = []

        def _set(col, val):
            updates.append(col + ' = %s')
            params.append(val)

        if 'name' in data:
            new_name = str(data['name']).strip()
            # ★ 主管不能改自己姓名（避免 leader 判定错乱）
            if scope['level'] == 'lead' and aid == (me or {}).get('id') and new_name != my_name:
                return fail('不能修改本人姓名，请联系管理员')
            _set('name', new_name)
        if 'account' in data and str(data['account']).strip():
            new_acc = str(data['account']).strip()
            dup = db_execute('SELECT id FROM admin_accounts WHERE account = %s AND id <> %s',
                             [new_acc, aid])
            if dup:
                return fail('该账号（手机号）已被占用')
            _set('account', new_acc)
        if 'password' in data and str(data['password']).strip():
            _set('password', str(data['password']).strip())
        if 'role' in data:
            new_role = str(data['role']).strip()
            if scope['level'] == 'lead':
                # ★ 主管不得改角色（防止把自己部门的人提权到开发人员/超级管理员）
                if new_role != (tgt.get('role') or ''):
                    return fail('无权修改账号角色')
            else:
                # 连超级管理员也不能把别人挂成超管角色以外的越权组合时留白；
                # 这里只做「必须存在该角色」的校验，保持原行为
                pass
            _set('role', new_role)
        if 'status' in data:
            new_status = str(data['status']).strip()
            # ★ 主管不能禁用/启用自己，避免把自己锁死
            if scope['level'] == 'lead' and aid == (me or {}).get('id'):
                return fail('不能修改本人账号状态')
            _set('status', new_status)
        if 'department' in data:
            new_dept = str(data['department']).strip()
            if scope['level'] == 'lead':
                if new_dept != scope['department']:
                    return fail('只能把账号留在本部门「%s」' % scope['department'])
            _set('department', new_dept)
        if 'subDept' in data or 'sub_dept' in data:
            _set('sub_dept', str(data.get('subDept', data.get('sub_dept'))).strip())
        if 'leader' in data:
            new_leader = str(data['leader']).strip()
            if scope['level'] == 'lead' and new_leader and new_leader != my_name:
                return fail('部门主管字段只能填写本人')
            _set('leader', new_leader)
        if 'gender' in data:
            _set('gender', str(data['gender']).strip())

        if not updates:
            return fail('没有要更新的数据')

        # 不允许修改 admin 账号的角色和状态（保护超级管理员）
        if tgt['account'] == 'admin':
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
    """删除账号

    权限分两层：全库（第 1 层）；仅本部门（部门主管），且不能删自己。
    """
    try:
        scope = _my_scope()
        if scope['level'] == 'none':
            return fail('无权限删除账号')
        row = db_execute(
            'SELECT id, account, department FROM admin_accounts WHERE id = %s', [aid])
        if not row:
            return fail('账号不存在')
        row = row[0]
        if row['account'] == 'admin':
            return fail('不能删除超级管理员账号')
        if not _scope_can_touch(scope, row):
            return fail('只能管理本部门「%s」的账号' % scope['department'])
        me = _load_self_account()
        if me and aid == me.get('id'):
            return fail('不能删除本人账号')
        db_execute('DELETE FROM admin_accounts WHERE id = %s', [aid], fetch=False)
        return success(None, '已删除')
    except Exception as e:
        return fail(str(e))


# ======================== 个人中心（个人信息 / 修改密码 / 头像上传） ========================

# 头像存放目录（线上：/opt/ecom/uploads/avatar）
AVATAR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'uploads', 'avatar')
_ALLOWED_AVATAR_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}


def _profile_row(account):
    """按登录账号取本人记录（含 password，供当前密码校验）"""
    rows = db_execute(
        'SELECT id, name, account, password, role, status, avatar, gender, department, '
        'sub_dept, leader, created_at, last_login FROM admin_accounts WHERE account = %s',
        [account])
    return rows[0] if rows else None


@app.route('/api/profile/me', methods=['GET'])
def profile_me():
    """当前登录者的个人资料"""
    try:
        sess = _current_session()
        if not sess:
            return fail('未登录')
        a = _profile_row(sess.get('account'))
        if not a:
            return fail('账号不存在')
        return success({
            'id': a['id'],
            'name': a['name'],
            'account': a['account'],
            'role': a['role'],
            'avatar': a['avatar'] or '',
            'gender': a['gender'] or '',
            'department': a['department'] or '',
            'subDept': a['sub_dept'] or '',
            'leader': a['leader'] or '',
            'status': a['status'],
            'createdAt': str(a['created_at']) if a['created_at'] else '',
            'lastLogin': a['last_login'] or '',
        })
    except Exception as e:
        return fail(str(e))


@app.route('/api/profile/update', methods=['POST'])
def profile_update():
    """更新个人信息

    ★ 改手机号（=登录账号）必须带 current_password 且校验通过；
      改完后 admin_accounts 就是账号列表读的同一张表，天然同步。
    """
    try:
        sess = _current_session()
        if not sess:
            return fail('未登录')
        a = _profile_row(sess.get('account'))
        if not a:
            return fail('账号不存在')
        data = request.get_json(force=True) or {}

        name = str(data.get('name', a['name'])).strip()
        gender = str(data.get('gender', a['gender'] or '')).strip()
        new_acc = str(data.get('account', a['account'])).strip() or a['account']
        if not name:
            return fail('姓名不能为空')

        changed_account = (new_acc != a['account'])
        if changed_account:
            cur_pwd = str(data.get('current_password', '')).strip()
            if not cur_pwd:
                return fail('修改手机号需要先验证当前密码')
            if cur_pwd != (a['password'] or ''):
                return fail('当前密码不正确')
            dup = db_execute('SELECT id FROM admin_accounts WHERE account = %s AND id <> %s',
                             [new_acc, a['id']])
            if dup:
                return fail('该手机号已被其他账号使用')

        db_execute('UPDATE admin_accounts SET name = %s, account = %s, gender = %s WHERE id = %s',
                   [name, new_acc, gender, a['id']], fetch=False)

        # 同步内存会话，避免改完手机号把自己踢下线
        for rec in list(_AUTH_TOKENS.values()):
            if rec.get('account') == a['account']:
                rec['account'] = new_acc
                rec['name'] = name

        return success({'account': new_acc, 'name': name, 'gender': gender}, '个人信息已保存')
    except Exception as e:
        return fail(str(e))


@app.route('/api/profile/password', methods=['POST'])
def profile_password():
    """修改密码 — 当前密码校验不通过一律拒绝"""
    try:
        sess = _current_session()
        if not sess:
            return fail('未登录')
        a = _profile_row(sess.get('account'))
        if not a:
            return fail('账号不存在')
        data = request.get_json(force=True) or {}
        cur_pwd = str(data.get('current_password', '')).strip()
        new_pwd = str(data.get('new_password', '')).strip()

        if not cur_pwd:
            return fail('请输入当前密码')
        if cur_pwd != (a['password'] or ''):
            return fail('当前密码不正确')
        if not new_pwd:
            return fail('请输入新密码')
        if len(new_pwd) < 6:
            return fail('新密码至少 6 位')
        if new_pwd == cur_pwd:
            return fail('新密码不能与当前密码相同')

        db_execute('UPDATE admin_accounts SET password = %s WHERE id = %s',
                   [new_pwd, a['id']], fetch=False)
        return success(None, '密码修改成功')
    except Exception as e:
        return fail(str(e))


@app.route('/api/profile/avatar', methods=['POST'])
def profile_avatar():
    """头像上传 — 落盘到 uploads/avatar/，库里只存相对路径"""
    try:
        sess = _current_session()
        if not sess:
            return fail('未登录')
        a = _profile_row(sess.get('account'))
        if not a:
            return fail('账号不存在')
        f = request.files.get('file')
        if f is None or not f.filename:
            return fail('未选择图片')
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in _ALLOWED_AVATAR_EXT:
            return fail('只支持 png / jpg / jpeg / webp / gif 格式')
        os.makedirs(AVATAR_DIR, exist_ok=True)
        fname = 'u%d_%d%s' % (a['id'], int(time.time() * 1000), ext)
        f.save(os.path.join(AVATAR_DIR, fname))
        url = '/uploads/avatar/' + fname
        db_execute('UPDATE admin_accounts SET avatar = %s WHERE id = %s',
                   [url, a['id']], fetch=False)
        return success({'avatar': url}, '头像已更新')
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


class ReportNoData(Exception):
    """目标日期没有任何营销数据，无法生成报告"""
    pass


def _generate_analysis_report(target_date):
    """生成指定日期的数据分析报告 — 模板规则 + DeepSeek AI 洞见，落库后返回结果。

    供 /api/analysis/generate 路由与「每日 11:00 定时推送」后台任务共用。
    返回 {'id', 'reportDate', 'report', 'generatedBy'}；无数据时抛 ReportNoData。
    """
    try:
        # 收集指定日期数据
        data = gather_daily_data(target_date)
        sm = data['summary']

        if sm['netPayment'] == 0 and sm['visitors'] == 0:
            raise ReportNoData(f'{target_date} 暂无营销数据，无法生成分析报告')

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

        return {
            'id': new_id,
            'reportDate': str(target_date),
            'report': full_html,
            'generatedBy': gen_by,
        }

    except ReportNoData:
        raise
    except Exception as e:
        traceback.print_exc()
        raise


@app.route('/api/analysis/generate', methods=['POST'])
def generate_analysis():
    """生成指定日期数据分析报告 — 模板规则 + DeepSeek AI 洞见，默认分析昨日"""
    target_str = (request.get_json(silent=True) or {}).get('date', '').strip()
    if target_str:
        try:
            target_date = datetime.strptime(target_str, '%Y-%m-%d').date()
        except ValueError:
            return fail('日期格式不正确，应为 YYYY-MM-DD')
    else:
        target_date = date.today() - timedelta(days=1)

    try:
        return success(_generate_analysis_report(target_date), '报告生成成功')
    except ReportNoData as e:
        return fail(str(e), code=404)
    except Exception as e:
        print('[每日分析] 报告生成失败:', e)
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
body{font-family:"Microsoft YaHei","微软雅黑","Noto Sans CJK SC","Source Han Sans SC","WenQuanYi Micro Hei",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#fff;margin:0;padding:0;color:#334155;line-height:1.7}
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
    """定位 Chrome / Edge / Chromium 可执行文件，用于无头渲染 PDF

    兼顾本地 Windows 开发与线上 Linux（服务器为 /usr/bin/google-chrome），
    可用环境变量 CHROME_PATH 覆盖。
    """
    candidates = [
        os.environ.get('CHROME_PATH', ''),
        # Linux（线上服务器）
        '/usr/bin/google-chrome',
        '/usr/bin/google-chrome-stable',
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
        '/usr/lib/chromium/chromium',
        '/snap/bin/chromium',
        # Windows（本地开发）
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


# ======================== 钉钉推送（每日分析报告） ========================
# 需求：每天定时生成昨日数据分析报告 → 渲染 PDF → 通过企业机器人发给指定成员，
#      推送人名单 / 应用凭证 / 开关 / 推送时间都在「每日数据分析」页面维护。
# 数据表：dingtalk_push_config（key-value 配置）/ dingtalk_push_users（推送人）/ dingtalk_push_logs（推送记录）
# 注意：线上由 gunicorn 启动（app:app）不会执行 __main__ 块，所以建表必须在模块导入阶段完成。

# 钉钉新版控制台「凭证与基础信息」有三个值：App ID（UnifiedAppId，UUID 格式，即旧版 AgentId）、
# Client ID（原 AppKey）、Client Secret（原 AppSecret）。换取 accessToken 必须用 Client ID + Client Secret，
# 所以这里不给默认值，由页面填写；「小钉」的 App ID 为 616ebaf8-08bc-4c48-85bf-115da040c1c8（填 agent_id 那格）。
_PUSH_DEFAULT_APP_KEY = ''
_PUSH_SECRET_MASK = '********'
_PUSH_CFG_DEFAULTS = {
    'app_key': _PUSH_DEFAULT_APP_KEY,
    'app_secret': '',
    'robot_code': '',
    'agent_id': '',
    'enabled': '1',
    'push_hour': '11',
    'push_minute': '0',
}
_PUSH_RETRY_INTERVAL = 900      # 昨日数据未落库时的重试间隔（秒）
_PUSH_RETRY_DEADLINE = (12, 30)  # 最晚重试到 12:30，仍无数据则跳过当日


def _push_ensure_tables():
    """建表：推送配置 / 推送人 / 推送记录（模块导入时执行）"""
    try:
        db_execute("""
            CREATE TABLE IF NOT EXISTS dingtalk_push_config (
                id INT AUTO_INCREMENT PRIMARY KEY,
                cfg_key VARCHAR(64) NOT NULL UNIQUE,
                cfg_value TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """, fetch=False)
        db_execute("""
            CREATE TABLE IF NOT EXISTS dingtalk_push_users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(64) NOT NULL,
                mobile VARCHAR(32) DEFAULT '',
                user_id VARCHAR(128) DEFAULT '',
                enabled TINYINT DEFAULT 1,
                remark VARCHAR(255) DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """, fetch=False)
        db_execute("""
            CREATE TABLE IF NOT EXISTS dingtalk_push_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                report_date DATE,
                push_type VARCHAR(20) DEFAULT 'auto',
                status VARCHAR(20) DEFAULT '',
                total INT DEFAULT 0,
                ok_count INT DEFAULT 0,
                detail TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """, fetch=False)
        print('[钉钉推送] 数据表就绪（配置 / 推送人 / 推送记录）')
    except Exception as e:
        print('[钉钉推送] 建表失败: %s' % e)


def _push_config():
    """读取推送配置（缺失项回落到默认值）"""
    cfg = dict(_PUSH_CFG_DEFAULTS)
    try:
        rows = db_execute('SELECT cfg_key, cfg_value FROM dingtalk_push_config') or []
        for r in rows:
            k = r.get('cfg_key')
            if k in cfg and r.get('cfg_value') is not None:
                cfg[k] = r.get('cfg_value')
    except Exception as e:
        print('[钉钉推送] 读取配置失败: %s' % e)
    return cfg


def _push_config_save(items):
    """保存推送配置，返回 (ok, msg)"""
    if not items:
        return True, ''
    try:
        for k, v in items.items():
            db_execute(
                'INSERT INTO dingtalk_push_config (cfg_key, cfg_value) VALUES (%s, %s) '
                'ON DUPLICATE KEY UPDATE cfg_value = VALUES(cfg_value)',
                [k, '' if v is None else str(v)], fetch=False)
        return True, ''
    except Exception as e:
        return False, str(e)


def _push_client(cfg=None):
    """构造钉钉客户端；凭证不全时抛 DingTalkError"""
    if DingTalkClient is None:
        raise DingTalkError('钉钉模块未加载（backend/dingtalk.py 缺失或依赖异常）')
    cfg = cfg or _push_config()
    if not (cfg.get('app_secret') or '').strip():
        raise DingTalkError('未配置 AppSecret，请先在「钉钉推送」设置里填写')
    return DingTalkClient(cfg.get('app_key'), cfg.get('app_secret'),
                          cfg.get('robot_code'), cfg.get('agent_id'))


def _push_users(only_enabled=False):
    """推送人列表"""
    sql = 'SELECT id, name, mobile, user_id, enabled, remark FROM dingtalk_push_users'
    if only_enabled:
        sql += ' WHERE enabled = 1'
    return db_execute(sql + ' ORDER BY id') or []


def _push_resolve_userid(client, row, write_back=True):
    """取成员 userid：已有则直接用；只填了手机号则调接口换取并回写缓存"""
    user_id = (row.get('user_id') or '').strip()
    if user_id:
        return user_id, ''
    mobile = (row.get('mobile') or '').strip()
    if not mobile:
        return '', '未填写 userId 或手机号'
    try:
        user_id = client.get_userid_by_mobile(mobile)
    except DingTalkError as e:
        return '', str(e)
    if write_back:
        try:
            db_execute('UPDATE dingtalk_push_users SET user_id = %s WHERE id = %s',
                       [user_id, row.get('id')], fetch=False)
        except Exception:
            pass
    return user_id, ''


# ======================== 开发告警：初始化 + 全局兜底 ========================
# 规则：后台任何报错 → 钉钉单聊「李自豪」（收件人自动从 dingtalk_push_users 里按名字匹配，
# 页面上改手机号/userId 即可生效；查不到时回落 .env 的 DEV_ALERT_USER_ID / DEV_ALERT_MOBILE）。
# 实现与冷却/限流策略见 backend/dev_alert.py。
try:
    from dev_alert import (init_dev_alert, notify_dev, notify_dev_exception,  # noqa: F401
                           dev_recipient as _dev_recipient)
    init_dev_alert(db_execute, _push_client, _push_resolve_userid)
except Exception as _dev_alert_init_err:
    print('[开发告警] 模块加载失败: %s' % _dev_alert_init_err)

    def _dev_recipient(force=False):
        return None


@app.errorhandler(Exception)
def _handle_uncaught_exception(e):
    """未捕获异常统一兜底：告警开发 + 返回统一 JSON（不再向前端吐 HTML 错误页）

    注意：Flask 的 Exception 处理器也会接到 HTTPException（404/405/413 等），
    这类是正常语义的响应，必须原样放行，不能当成故障告警。
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    try:
        traceback.print_exc()
    except Exception:
        pass
    _dev_alert_exc('接口未捕获异常', e,
                   extra='%s %s' % (request.method, request.path),
                   signature='http:%s:%s' % (request.path, type(e).__name__))
    return jsonify({'code': 1, 'msg': '服务器内部错误：%s' % e, 'data': None}), 500


def _push_apply_robot_code(cfg, robot_code):
    """机器人接口实际生效的 robotCode 与配置不一致时回写，减少后续试错"""
    robot_code = (robot_code or '').strip()
    if not robot_code or robot_code == (cfg.get('robot_code') or '').strip():
        return
    ok, _ = _push_config_save({'robot_code': robot_code})
    if ok:
        cfg['robot_code'] = robot_code
        print('[钉钉推送] robotCode 已自动记录为 %s' % robot_code)


def _load_report_metrics(target_date):
    """读取报告落库时保存的原始指标，用于拼摘要"""
    try:
        rows = db_execute('SELECT metrics_json FROM daily_analysis_reports WHERE report_date = %s',
                          [target_date])
        if rows and rows[0].get('metrics_json'):
            return json.loads(rows[0]['metrics_json'])
    except Exception as e:
        print('[钉钉推送] 读取报告指标失败: %s' % e)
    return {}


def _num(v, digits=2):
    """千分位格式化（钉钉 markdown 里可读性更好）"""
    try:
        return format(float(v or 0), ',.%df' % digits)
    except Exception:
        return '0'


def _int(v):
    try:
        return format(int(v or 0), ',d')
    except Exception:
        return '0'


def _push_summary_md(target_date, metrics):
    """把报告指标拼成钉钉 markdown 摘要（控制长度，平台取 TOP3、预警最多 4 条）"""
    sm = (metrics or {}).get('summary') or {}
    net = float(sm.get('netPayment') or 0)
    refund_amt = float(sm.get('refundAmount') or 0)
    ad_spend = float(sm.get('adSpend') or 0)
    ad_total = float(sm.get('adTotal') or 0)
    conv = float(sm.get('convRate') or 0)
    refund_rate = round(refund_amt / net * 100, 2) if net > 0 else 0
    roi = round(ad_total / ad_spend, 2) if ad_spend > 0 else 0

    lines = [
        '### 每日经营数据分析 · %s' % target_date,
        '',
        '**核心指标**',
        '- 净支付金额：**¥%s**' % _num(net),
        '- 退款率：%s%%（订单退款率 %s%%）' % (refund_rate, _num(sm.get('orderRefundRate'))),
        '- 推广 ROI：**%s**（花费 ¥%s / 产出 ¥%s）' % (roi, _num(ad_spend), _num(ad_total)),
        '- 访客 %s ｜ 买家 %s ｜ 转化率 %s%%' % (_int(sm.get('visitors')), _int(sm.get('payers')), _num(conv)),
        '- 客单价：¥%s' % _num(sm.get('aov')),
    ]

    plats = sorted((metrics or {}).get('byPlatform') or [],
                   key=lambda p: -(p.get('netPayment') or 0))[:3]
    if plats:
        lines += ['', '**平台表现**']
        for p in plats:
            lines.append('- %s：净支付 ¥%s' % (p.get('name') or '', _num(p.get('netPayment'))))

    warns = []
    if refund_rate > 20:
        warns.append('整体退款率 %s%%%s' % (refund_rate, '（严重）' if refund_rate > 30 else '（偏高）'))
    if ad_spend > 0 and roi < 1:
        warns.append('推广 ROI %s 低于 1，投放处于亏损' % roi)
    if conv and conv < 2:
        warns.append('支付转化率 %s%% 偏低' % _num(conv))
    bad_stores = [s for s in ((metrics or {}).get('byStore') or [])
                  if (s.get('refundRate') or 0) > 20]
    bad_stores.sort(key=lambda s: -(s.get('refundRate') or 0))
    for s in bad_stores[:2]:
        warns.append('[%s] %s 退款率 %s%%' % (s.get('platform') or '', s.get('name') or '',
                                            round(s.get('refundRate') or 0, 1)))
    if warns:
        lines += ['', '**预警**'] + ['- %s' % w for w in warns[:4]]

    lines += ['', '> 完整报告见下方长图']
    return '\n'.join(lines)


def _push_log_write(report_date, push_type, status, total, ok_count, detail):
    """写推送记录（失败不影响主流程）"""
    try:
        db_execute(
            'INSERT INTO dingtalk_push_logs (report_date, push_type, status, total, ok_count, detail) '
            'VALUES (%s, %s, %s, %s, %s, %s)',
            [report_date, push_type, status, total, ok_count, (detail or '')[:2000]], fetch=False)
    except Exception as e:
        print('[钉钉推送] 写推送记录失败: %s' % e)


def _push_daily_report(target_date, result, push_type='auto'):
    """把报告渲染成整页长图并推送给所有启用成员，返回 (status, detail)

    为什么是图片不是 PDF：钉钉单聊的「文件消息」里 PDF 只能下载后另找应用打开，
    手机上等于打不开；图片消息点一下就在钉钉内全屏看、可缩放，不依赖外部程序。

    status: success（全部送达）/ partial（部分送达）/ fail（未送达）
    """
    users = _push_users(only_enabled=True)
    if not users:
        msg = '没有启用中的推送人，请先在「钉钉推送」里添加'
        _push_log_write(target_date, push_type, 'fail', 0, 0, msg)
        return 'fail', msg

    # 1) 渲染长图 + 上传拿 media_id（同一张图发给所有人，只上传一次）
    try:
        if _render_report_image is None:
            raise RuntimeError('report_image 模块未加载（缺 backend/report_image.py 或 Playwright）')
        client = _push_client()
        img_bytes, ext = _render_report_image(str(target_date), result['report'],
                                              _DA_STANDALONE_CSS)
        filename = '每日数据分析报告_%s.%s' % (target_date, ext)
        media_id = client.upload_image(
            filename, img_bytes, 'image/jpeg' if ext == 'jpg' else 'image/png')
        print('[钉钉推送] %s 报告长图 %.2f MB（%s）' % (target_date, len(img_bytes) / 1048576.0, ext))
    except Exception as e:
        msg = '生成或上传报告长图失败：%s' % e
        print('[钉钉推送] ' + msg)
        _push_log_write(target_date, push_type, 'fail', len(users), 0, msg)
        return 'fail', msg

    # 2) 逐人推送：先发指标摘要，再发报告长图
    summary = _push_summary_md(target_date, _load_report_metrics(target_date))
    title = '每日经营数据分析 · %s' % target_date
    ok_count = 0
    details = []
    for u in users:
        name = u.get('name') or ('id=%s' % u.get('id'))
        uid, err = _push_resolve_userid(client, u)
        if err:
            details.append('%s：%s' % (name, err))
            continue
        try:
            r1 = client.send_markdown([uid], title, summary)
            bad = (r1 or {}).get('invalidStaffIdList') or []
            if bad:
                raise DingTalkError('该成员不在应用可见范围内')
            r2 = client.send_image([uid], media_id)
            bad = (r2 or {}).get('invalidStaffIdList') or []
            if bad:
                raise DingTalkError('该成员不在应用可见范围内')
            _push_apply_robot_code(_push_config(), (r2 or {}).get('robotCode'))
            ok_count += 1
            details.append('%s：已送达' % name)
        except Exception as e:
            details.append('%s：%s' % (name, e))

    status = 'success' if ok_count == len(users) else ('partial' if ok_count else 'fail')
    detail = '；'.join(details)
    _push_log_write(target_date, push_type, status, len(users), ok_count, detail)
    return status, detail


def _push_data_ready(target_date):
    """昨日数据是否已落库（净支付与访客都为 0 视为未就绪）"""
    try:
        sm = (gather_daily_data(target_date) or {}).get('summary') or {}
        return (sm.get('netPayment') or 0) != 0 or (sm.get('visitors') or 0) != 0
    except Exception as e:
        print('[钉钉推送] 数据就绪检查失败: %s' % e)
        return False


def _push_log_has_success(report_date):
    """该报告日期今天是否已成功推送过（用于服务重启后的补跑判断）"""
    try:
        today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = db_execute(
            "SELECT COUNT(*) AS c FROM dingtalk_push_logs "
            "WHERE report_date = %s AND status = 'success' AND created_at >= %s",
            [report_date, today0])
        return bool(rows and rows[0]['c'])
    except Exception as e:
        # 查询异常时按「已推送」处理，避免重复打扰
        print('[钉钉推送] 推送记录检查失败: %s' % e)
        return True


def _run_daily_report_push(push_type='auto'):
    """自动任务：等昨日数据落库 → 生成报告 → 推送钉钉"""
    target_date = date.today() - timedelta(days=1)
    cfg = _push_config()
    if push_type == 'auto' and cfg.get('enabled') != '1':
        print('[钉钉推送] 自动推送已关闭，跳过')
        return

    # 1) 等昨日数据落库（三平台抓取 9:00 起跑，可能 10 点多才完）
    deadline = datetime.now().replace(hour=_PUSH_RETRY_DEADLINE[0],
                                      minute=_PUSH_RETRY_DEADLINE[1], second=0, microsecond=0)
    while not _push_data_ready(target_date):
        if push_type != 'auto' or datetime.now() >= deadline:
            msg = '%s 数据尚未落库，本次跳过' % target_date
            print('[钉钉推送] ' + msg)
            _push_log_write(target_date, push_type, 'skipped', 0, 0, msg)
            return
        print('[钉钉推送] %s 数据未就绪，%d 秒后重试' % (target_date, _PUSH_RETRY_INTERVAL))
        time.sleep(_PUSH_RETRY_INTERVAL)

    # 2) 生成报告（覆盖式更新，保证推送的是最新数据）
    print('[钉钉推送] 开始生成 %s 报告' % target_date)
    result = _generate_analysis_report(target_date)

    # 3) 推送
    status, detail = _push_daily_report(target_date, result, push_type)
    print('[钉钉推送] %s 推送结果：%s | %s' % (target_date, status, detail))
    if status != 'success':
        # 日报是有人等着看的产物：部分送达/未送达都直接告警开发（同一日期同一状态只报一次）
        _dev_alert('每日数据分析报告推送%s（%s）'
                   % ('部分失败' if status == 'partial' else '失败', target_date),
                   detail=detail, signature='push:daily:%s:%s' % (target_date, status),
                   source='每日报告推送')


def _daily_report_push_loop():
    """后台线程：每天到配置时间点自动生成昨日报告并推送钉钉；服务重启后自动补跑

    每分钟检查一次（而非一觉睡到时间点），这样页面改推送时间/开关后 1 分钟内生效。
    """
    time.sleep(20)  # 等数据库连接池与服务初始化

    # ---- 启动补跑：服务恰在时间点之后重启时，当天容易漏推 ----
    try:
        cfg = _push_config()
        now = datetime.now()
        run_at = now.replace(hour=int(cfg.get('push_hour') or 11),
                             minute=int(cfg.get('push_minute') or 0), second=0, microsecond=0)
        target_date = date.today() - timedelta(days=1)
        if cfg.get('enabled') == '1' and now > run_at and not _push_log_has_success(target_date):
            print('[钉钉推送][补跑] 今日尚未推送成功，立即补跑一次')
            _run_daily_report_push('auto')
        else:
            print('[钉钉推送][补跑] 无需补跑')
    except Exception as e:
        print('[钉钉推送][补跑] 异常: %s' % e)
        _dev_alert_exc('每日报告启动补跑异常', e, signature='push:catchup',
                       source='每日报告推送')

    # ---- 主循环 ----
    last_fired = ''
    while True:
        try:
            cfg = _push_config()
            now = datetime.now()
            hh = int(cfg.get('push_hour') or 11)
            mm = int(cfg.get('push_minute') or 0)
            stamp = '%s %02d:%02d' % (now.strftime('%Y-%m-%d'), hh, mm)
            if cfg.get('enabled') == '1' and now.hour == hh and now.minute == mm and last_fired != stamp:
                last_fired = stamp
                print('[钉钉推送][定时] 触发每日报告生成与推送')
                _run_daily_report_push('auto')
        except Exception as e:
            print('[钉钉推送][定时] 异常: %s' % e)
            _dev_alert_exc('每日报告定时循环异常', e, signature='push:loop',
                           source='每日报告推送')
        time.sleep(60)


@app.route('/api/analysis/push/config', methods=['GET'])
def push_config_get():
    """读取钉钉推送配置 + 推送人 + 最近推送记录"""
    try:
        cfg = _push_config()
        users = _push_users()
        logs = db_execute(
            'SELECT id, report_date, push_type, status, total, ok_count, detail, created_at '
            'FROM dingtalk_push_logs ORDER BY id DESC LIMIT 10') or []
        return success({
            'appKey': cfg.get('app_key', ''),
            'appSecret': _PUSH_SECRET_MASK if cfg.get('app_secret') else '',
            'hasAppSecret': bool(cfg.get('app_secret')),
            'robotCode': cfg.get('robot_code', ''),
            'agentId': cfg.get('agent_id', ''),
            'enabled': cfg.get('enabled') == '1',
            'pushHour': int(cfg.get('push_hour') or 11),
            'pushMinute': int(cfg.get('push_minute') or 0),
            'users': [{
                'id': u['id'], 'name': u['name'], 'mobile': u.get('mobile') or '',
                'userId': u.get('user_id') or '', 'enabled': bool(u.get('enabled')),
                'remark': u.get('remark') or '',
            } for u in users],
            'logs': [{
                'id': l['id'],
                'reportDate': str(l['report_date']) if l.get('report_date') else '',
                'pushType': l.get('push_type') or '', 'status': l.get('status') or '',
                'total': l.get('total') or 0, 'okCount': l.get('ok_count') or 0,
                'detail': l.get('detail') or '', 'createdAt': str(l.get('created_at') or ''),
            } for l in logs],
        })
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


@app.route('/api/analysis/push/config', methods=['POST'])
def push_config_save():
    """保存钉钉推送配置（AppSecret 回传掩码时保持原值不变）"""
    try:
        d = request.get_json(force=True) or {}
        items = {}
        if 'appKey' in d:
            items['app_key'] = (d.get('appKey') or '').strip()
        if 'appSecret' in d:
            v = (d.get('appSecret') or '').strip()
            if v and v != _PUSH_SECRET_MASK:
                items['app_secret'] = v
        if 'robotCode' in d:
            items['robot_code'] = (d.get('robotCode') or '').strip()
        if 'agentId' in d:
            items['agent_id'] = (d.get('agentId') or '').strip()
        if 'enabled' in d:
            items['enabled'] = '1' if d.get('enabled') else '0'
        if 'pushHour' in d:
            items['push_hour'] = str(max(0, min(23, int(d.get('pushHour') or 0))))
        if 'pushMinute' in d:
            items['push_minute'] = str(max(0, min(59, int(d.get('pushMinute') or 0))))
        ok, msg = _push_config_save(items)
        if not ok:
            return fail(msg)
        return success(None, '设置已保存')
    except Exception as e:
        return fail(str(e))


@app.route('/api/analysis/push/users', methods=['POST'])
def push_user_create():
    """新增推送人（手机号或 userId 至少填一个）"""
    try:
        d = request.get_json(force=True) or {}
        name = (d.get('name') or '').strip()
        mobile = (d.get('mobile') or '').strip()
        user_id = (d.get('userId') or '').strip()
        if not name:
            return fail('请填写成员姓名')
        if not mobile and not user_id:
            return fail('请填写手机号或钉钉 userId（至少一个）')
        new_id = db_execute_insert(
            'INSERT INTO dingtalk_push_users (name, mobile, user_id, enabled, remark) '
            'VALUES (%s, %s, %s, %s, %s)',
            [name, mobile, user_id, 1 if d.get('enabled', True) else 0,
             (d.get('remark') or '').strip()])
        return success({'id': new_id}, '已添加推送人')
    except Exception as e:
        return fail(str(e))


@app.route('/api/analysis/push/users/<int:uid>', methods=['PUT'])
def push_user_update(uid):
    """修改推送人（可改姓名/手机号/userId/启用状态/备注）"""
    try:
        d = request.get_json(force=True) or {}
        sets, params = [], []
        if 'name' in d:
            name = (d.get('name') or '').strip()
            if not name:
                return fail('姓名不能为空')
            sets.append('name = %s')
            params.append(name)
        if 'mobile' in d:
            sets.append('mobile = %s')
            params.append((d.get('mobile') or '').strip())
        if 'userId' in d:
            sets.append('user_id = %s')
            params.append((d.get('userId') or '').strip())
        if 'enabled' in d:
            sets.append('enabled = %s')
            params.append(1 if d.get('enabled') else 0)
        if 'remark' in d:
            sets.append('remark = %s')
            params.append((d.get('remark') or '').strip())
        if not sets:
            return fail('没有需要更新的字段')
        params.append(uid)
        rows = db_execute('UPDATE dingtalk_push_users SET %s WHERE id = %%s' % ', '.join(sets),
                          params, fetch=False)
        if not rows:
            return fail('推送人不存在')
        return success(None, '已更新')
    except Exception as e:
        return fail(str(e))


@app.route('/api/analysis/push/users/<int:uid>', methods=['DELETE'])
def push_user_delete(uid):
    """删除推送人"""
    try:
        db_execute('DELETE FROM dingtalk_push_users WHERE id = %s', [uid], fetch=False)
        return success(None, '已删除')
    except Exception as e:
        return fail(str(e))


@app.route('/api/analysis/push/resolve', methods=['POST'])
def push_resolve():
    """用手机号解析钉钉 userId（添加成员前可先验证是否匹配得到人）"""
    try:
        mobile = ((request.get_json(force=True) or {}).get('mobile') or '').strip()
        if not mobile:
            return fail('请输入手机号')
        user_id = _push_client().get_userid_by_mobile(mobile)
        return success({'mobile': mobile, 'userId': user_id}, '已匹配到成员')
    except DingTalkError as e:
        return fail(str(e))
    except Exception as e:
        return fail(str(e))


@app.route('/api/analysis/push/test', methods=['POST'])
def push_test():
    """连通性测试：给所有启用成员发一条文本消息，验证凭证与可见范围"""
    try:
        users = _push_users(only_enabled=True)
        if not users:
            return fail('请先添加并启用推送人')
        client = _push_client()
        cfg = _push_config()
        custom = ((request.get_json(silent=True) or {}).get('content') or '').strip()
        ok_count, details = 0, []
        test_text = custom or ('【测试】每日数据分析报告推送通道正常，每天 %02d:%02d 将自动推送昨日报告。'
                               % (int(cfg.get('push_hour') or 11), int(cfg.get('push_minute') or 0)))
        for u in users:
            name = u.get('name') or ('id=%s' % u.get('id'))
            uid, err = _push_resolve_userid(client, u)
            if err:
                details.append('%s：%s' % (name, err))
                continue
            try:
                r = client.send_text([uid], test_text)
                if (r or {}).get('invalidStaffIdList'):
                    raise DingTalkError('该成员不在应用可见范围内')
                _push_apply_robot_code(cfg, (r or {}).get('robotCode'))
                ok_count += 1
                details.append('%s：测试消息已发送' % name)
            except Exception as e:
                details.append('%s：%s' % (name, e))
        detail = '；'.join(details)
        if ok_count == 0:
            _push_log_write(date.today(), 'test', 'fail', len(users), 0, detail)
            return fail(detail)
        status = 'success' if ok_count == len(users) else 'partial'
        _push_log_write(date.today(), 'test', status, len(users), ok_count, detail)
        return success({'okCount': ok_count, 'total': len(users), 'detail': detail, 'status': status},
                       '测试完成（%d/%d）' % (ok_count, len(users)))
    except DingTalkError as e:
        return fail(str(e))
    except Exception as e:
        return fail(str(e))


@app.route('/api/analysis/push/now', methods=['POST'])
def push_now():
    """立即生成并推送指定日期的报告（默认昨日），用于验证与临时补发"""
    try:
        d = request.get_json(silent=True) or {}
        target_str = (d.get('date') or '').strip()
        if target_str:
            try:
                target_date = datetime.strptime(target_str, '%Y-%m-%d').date()
            except ValueError:
                return fail('日期格式不正确，应为 YYYY-MM-DD')
        else:
            target_date = date.today() - timedelta(days=1)

        if not _push_users(only_enabled=True):
            return fail('请先添加并启用推送人')

        try:
            result = _generate_analysis_report(target_date)
        except ReportNoData as e:
            return fail(str(e), code=404)

        status, detail = _push_daily_report(target_date, result, 'manual')
        if status == 'fail':
            return fail(detail)
        return success({'status': status, 'detail': detail, 'reportDate': str(target_date)},
                       '推送完成' if status == 'success' else '部分成员推送失败')
    except DingTalkError as e:
        return fail(str(e))
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


# ======================== 工具箱 · 通告发放 ========================
# 需求：通告内容支持 文本 / 图片（可粘贴）/ 办公文件，通过钉钉机器人单聊发送；
#       接收人支持 网站账号列表 / 网站部门 / 钉钉组织架构 / 钉钉联系人（姓名匹配）。
# ★ 凭证配置完全独立（announce_config 表），在本页面「发送设置」里维护，
#   不复用「每日数据分析 → 钉钉推送」的 dingtalk_push_config，互不影响。
#
# 接口：
#   GET  /api/announce/options             网站账号列表 + 部门 + 已维护的钉钉推送人
#   GET  /api/announce/dingtalk/contacts   同步钉钉组织架构（部门树 + 成员，含缓存）
#   POST /api/announce/send                发送通告（multipart：payload JSON + files 附件）
#   GET  /api/announce/config              读取本页钉钉应用凭证（secret 掩码）
#   POST /api/announce/config              保存本页钉钉应用凭证
#   POST /api/announce/config/test         连通性测试（取 token / 可选发测试消息）

# 钉钉组织架构 / 联系人名单缓存（三级：进程内存 → announce_contacts_cache 落库 → 钉钉接口）
# ★ 2026-09-19：名单改为服务端定时生成并落库，前端不再有任何「同步/加载」按钮，
#   页面加载只读缓存，不再因为读通讯录而等待或需要人工点击。
_ANNOUNCE_CONTACTS_CACHE = {'ts': 0.0, 'rosterTs': 0.0, 'departments': [], 'users': []}
_ANNOUNCE_CONTACTS_TTL = 600                  # 进程内复用 10 分钟
_ANNOUNCE_ROSTER_MAX_AGE = 12 * 3600          # 落库名单超过 12h → 后台异步补一次
_ANNOUNCE_ROSTER_HOUR, _ANNOUNCE_ROSTER_MINUTE = 8, 30   # 每天 08:30 刷新（保证 9 点前是最新）
_ANNOUNCE_ROSTER_REFRESHING = threading.Event()
_ANNOUNCE_ROSTER_LOCK = threading.Lock()      # 刷新串行化，避免定时/页面并发重复拉取
# 钉钉单条 markdown 消息安全长度（超长自动分段发送）
_ANNOUNCE_MD_CHUNK = 1800
# 单个附件上限（media/upload 的硬限制：图片 10MB / 文件 20MB）
_ANNOUNCE_IMG_MAX = 10 * 1024 * 1024
_ANNOUNCE_FILE_MAX = 20 * 1024 * 1024
# 通告发放独立配置（与「钉钉推送」的 dingtalk_push_config 完全隔离）
# ★★ 2026-09-19 起：凭证由系统统一下发（下表默认值 + 首次启动落库），
#   前端「发送设置」配置页已整体下线，DB 里已有值以 DB 为准（覆盖默认值）。
_ANNOUNCE_CFG_DEFAULTS = {
    'app_key': 'dingjxvfpxfrgbgxrbyq',
    'app_secret': '8DYTyv8Ge70ehgARHoIzr0E_VZvliKORd0nhc1W-y1W6JWncbYNaiCWt94Ox_JTt',
    'robot_code': '',           # 留空 = 自动取 app_key；发送成功后按实际生效值回写
    'agent_id': '4872122118',
}


def _announce_ensure_table():
    """建表：通告发放独立配置 + 发送记录（模块导入时执行，gunicorn 下也生效）"""
    try:
        db_execute("""
            CREATE TABLE IF NOT EXISTS announce_config (
                id INT AUTO_INCREMENT PRIMARY KEY,
                cfg_key VARCHAR(64) NOT NULL UNIQUE,
                cfg_value TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """, fetch=False)
        db_execute("""
            CREATE TABLE IF NOT EXISTS announce_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                send_date DATE,
                status VARCHAR(20) DEFAULT '',
                total INT DEFAULT 0,
                ok_count INT DEFAULT 0,
                detail TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """, fetch=False)
        # 钉钉组织架构/联系人名单落库（单行 id=1，服务端定时刷新，前端读这份）
        db_execute("""
            CREATE TABLE IF NOT EXISTS announce_contacts_cache (
                id TINYINT PRIMARY KEY,
                payload LONGTEXT,
                synced_at DATETIME,
                user_count INT DEFAULT 0,
                dept_count INT DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """, fetch=False)
        print('[通告发放] 数据表就绪（announce_config / announce_logs / announce_contacts_cache）')
    except Exception as e:
        print('[通告发放] 建表失败: %s' % e)


def _announce_config():
    """读取通告发放自己的钉钉配置（缺失项回落空值）"""
    cfg = dict(_ANNOUNCE_CFG_DEFAULTS)
    try:
        rows = db_execute('SELECT cfg_key, cfg_value FROM announce_config') or []
        for r in rows:
            k = r.get('cfg_key')
            if k in cfg and r.get('cfg_value') is not None:
                cfg[k] = r.get('cfg_value')
    except Exception as e:
        print('[通告发放] 读取配置失败: %s' % e)
    return cfg


def _announce_config_save(items):
    """保存通告发放配置，返回 (ok, msg)"""
    if not items:
        return True, ''
    try:
        for k, v in items.items():
            db_execute(
                'INSERT INTO announce_config (cfg_key, cfg_value) VALUES (%s, %s) '
                'ON DUPLICATE KEY UPDATE cfg_value = VALUES(cfg_value)',
                [k, '' if v is None else str(v)], fetch=False)
        return True, ''
    except Exception as e:
        return False, str(e)


def _announce_client(cfg=None):
    """构造通告发放专用的钉钉客户端；凭证不全时抛 DingTalkError"""
    if DingTalkClient is None:
        raise DingTalkError('钉钉模块未加载（backend/dingtalk.py 缺失或依赖异常）')
    cfg = cfg or _announce_config()
    if not (cfg.get('app_key') or '').strip():
        raise DingTalkError('钉钉通道未配置（缺少 Client ID），请联系管理员在服务端补齐')
    if not (cfg.get('app_secret') or '').strip():
        raise DingTalkError('钉钉通道未配置（缺少 Client Secret），请联系管理员在服务端补齐')
    return DingTalkClient(cfg.get('app_key'), cfg.get('app_secret'),
                          cfg.get('robot_code'), cfg.get('agent_id'))


def _announce_apply_robot_code(cfg, robot_code):
    """机器人接口实际生效的 robotCode 与配置不一致时回写 announce_config"""
    robot_code = (robot_code or '').strip()
    if not robot_code or robot_code == (cfg.get('robot_code') or '').strip():
        return
    ok, _ = _announce_config_save({'robot_code': robot_code})
    if ok:
        cfg['robot_code'] = robot_code
        print('[通告发放] robotCode 已自动记录为 %s' % robot_code)


def _announce_roster_load():
    """读取落库名单 → (departments, users, synced_ts)；没有/解析失败返回 None"""
    try:
        rows = db_execute('SELECT payload, synced_at FROM announce_contacts_cache WHERE id = 1') or []
    except Exception as e:
        print('[通告发放] 读取落库名单失败: %s' % e)
        return None
    if not rows:
        return None
    row = rows[0]
    try:
        payload = json.loads(row.get('payload') or '{}')
    except Exception:
        return None
    users = payload.get('users') or []
    if not users:
        return None
    synced_ts = 0.0
    sa = row.get('synced_at')
    if sa is not None:
        try:
            synced_ts = time.mktime(sa.timetuple())
        except Exception:
            synced_ts = 0.0
    return (payload.get('departments') or []), users, synced_ts


def _announce_roster_save(departments, users):
    """把名单写库（单行 upsert），供前端与发送解析共用"""
    payload = json.dumps({'departments': departments, 'users': users}, ensure_ascii=False)
    db_execute(
        'INSERT INTO announce_contacts_cache (id, payload, synced_at, user_count, dept_count) '
        'VALUES (1, %s, NOW(), %s, %s) '
        'ON DUPLICATE KEY UPDATE payload = VALUES(payload), synced_at = NOW(), '
        'user_count = VALUES(user_count), dept_count = VALUES(dept_count)',
        [payload, len(users), len(departments)], fetch=False)


def _announce_roster_status():
    """名单状态（不触发任何钉钉请求）：{ready, userCount, deptCount, syncedAt, ageSeconds}"""
    cache = _ANNOUNCE_CONTACTS_CACHE
    if not cache['users']:
        stored = _announce_roster_load()
        if stored:
            departments, users, synced_ts = stored
            cache.update(ts=time.time(), rosterTs=synced_ts, departments=departments, users=users)
    if not cache['users']:
        return {'ready': False, 'userCount': 0, 'deptCount': 0, 'syncedAt': '', 'ageSeconds': None}
    ts = cache.get('rosterTs') or cache.get('ts') or 0
    return {
        'ready': True,
        'userCount': len(cache['users']),
        'deptCount': len(cache.get('departments') or []),
        'syncedAt': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts else '',
        'ageSeconds': int(time.time() - ts) if ts else None,
    }


def _announce_contacts_refresh():
    """真正访问钉钉拉全量名单 → 写内存 + 落库；失败抛 DingTalkError"""
    with _ANNOUNCE_ROSTER_LOCK:
        client = _announce_client()
        departments, users = client.fetch_all_contacts()
        if not users:
            raise DingTalkError('钉钉组织架构返回 0 位成员（请检查应用可见范围与通讯录读权限）')
        now = time.time()
        _ANNOUNCE_CONTACTS_CACHE.update(ts=now, rosterTs=now,
                                        departments=departments, users=users)
        try:
            _announce_roster_save(departments, users)
        except Exception as e:
            print('[通告发放] 名单落库失败（本次仅驻留内存）: %s' % e)
        return departments, users


def _announce_roster_refresh_async():
    """后台异步刷新名单（去重，失败只打日志，不影响当前请求）"""
    if _ANNOUNCE_ROSTER_REFRESHING.is_set():
        return

    def _work():
        _ANNOUNCE_ROSTER_REFRESHING.set()
        try:
            departments, users = _announce_contacts_refresh()
            print('[通告发放][名单] 后台刷新完成：部门 %d / 成员 %d' % (len(departments), len(users)))
        except Exception as e:
            print('[通告发放][名单] 后台刷新失败: %s' % e)
        finally:
            _ANNOUNCE_ROSTER_REFRESHING.clear()

    threading.Thread(target=_work, daemon=True, name='announce-roster-refresh').start()


def _announce_contacts(force=False):
    """读取钉钉组织架构/联系人名单：内存 → 落库 → 实时拉取（force=True 强制拉取）

    ★ 前端只走前两级，所以页面加载是毫秒级、且不依赖钉钉接口当时是否可用。
    """
    now = time.time()
    if (not force and _ANNOUNCE_CONTACTS_CACHE['users']
            and now - _ANNOUNCE_CONTACTS_CACHE['ts'] < _ANNOUNCE_CONTACTS_TTL):
        return _ANNOUNCE_CONTACTS_CACHE

    if not force:
        stored = _announce_roster_load()
        if stored:
            departments, users, synced_ts = stored
            _ANNOUNCE_CONTACTS_CACHE.update(ts=now, rosterTs=synced_ts,
                                            departments=departments, users=users)
            if now - synced_ts > _ANNOUNCE_ROSTER_MAX_AGE:
                print('[通告发放][名单] 落库名单已超过 %dh，触发后台刷新'
                      % int(_ANNOUNCE_ROSTER_MAX_AGE / 3600))
                _announce_roster_refresh_async()
            return _ANNOUNCE_CONTACTS_CACHE

    _announce_contacts_refresh()
    return _ANNOUNCE_CONTACTS_CACHE


def _announce_roster_loop():
    """后台线程：每天 08:30 自动刷新钉钉名单并落库（保证 9 点前是最新列表）

    条件式触发（不是睡到点）：只要「已过今天的 08:30」且「今天还没刷新成功」，就刷新一次。
    这样服务在 08:30 之后重启也会自动补跑，名单不会因为重启而整天空缺。
    """
    time.sleep(25)  # 等数据库/服务初始化
    while True:
        try:
            now = datetime.now()
            due = now.replace(hour=_ANNOUNCE_ROSTER_HOUR,
                              minute=_ANNOUNCE_ROSTER_MINUTE, second=0, microsecond=0)
            st = _announce_roster_status()
            synced_date = ''
            if st.get('syncedAt'):
                synced_date = st['syncedAt'][:10]
            if now >= due and synced_date != now.strftime('%Y-%m-%d'):
                print('[通告发放][名单] 触发每日刷新（目标：%02d:%02d 前就绪）'
                      % (_ANNOUNCE_ROSTER_HOUR + 1, 0))
                try:
                    departments, users = _announce_contacts_refresh()
                    print('[通告发放][名单] 每日刷新完成：部门 %d / 成员 %d'
                          % (len(departments), len(users)))
                except Exception as e:
                    print('[通告发放][名单] 每日刷新失败（1 分钟后再试）: %s' % e)
        except Exception as e:
            print('[通告发放][名单] 定时循环异常: %s' % e)
        time.sleep(60)


def _announce_match_by_name(name):
    """按姓名在钉钉组织架构中匹配 userid，返回 [userid...]；组织架构不可用时抛 DingTalkError"""
    cache = _announce_contacts()
    return [u['userid'] for u in cache['users'] if u.get('name') == name]


def _announce_resolve_recipient(client, r):
    """把前端传来的接收人解析成钉钉 userid 列表，返回 (user_ids, err_msg)"""
    name = (r.get('name') or '').strip()
    user_id = (r.get('userId') or '').strip()
    mobile = (r.get('mobile') or '').strip()

    if user_id:
        return [user_id], ''
    if mobile:
        try:
            return [client.get_userid_by_mobile(mobile)], ''
        except DingTalkError as e:
            return [], str(e)
    if not name:
        return [], '姓名 / 手机号 / userId 均为空，无法匹配'

    # 钉钉组织架构按姓名匹配（重名会全部命中，由发送明细区分）
    try:
        hits = _announce_match_by_name(name)
    except DingTalkError as e:
        return [], '无法通过姓名匹配：%s' % e
    if hits:
        return hits, ''
    return [], '未找到姓名为「%s」的钉钉成员（可先在「钉钉组织架构 / 钉钉联系人」页签同步）' % name


@app.route('/api/announce/options', methods=['GET'])
def announce_options():
    """通告发放的可选数据源：网站账号（按部门聚合）+ 本页钉钉配置状态"""
    try:
        rows = db_execute('SELECT id, name, account, role, status, department, sub_dept '
                          'FROM admin_accounts ORDER BY department, id') or []
        accounts = []
        departments = []
        dept_set = set()
        for r in rows:
            dept = (r.get('department') or '').strip()
            if dept and dept not in dept_set:
                dept_set.add(dept)
                departments.append(dept)
            accounts.append({
                'id': r['id'], 'name': r['name'], 'account': r['account'],
                'role': r.get('role') or '', 'department': dept,
                'subDept': r.get('sub_dept') or '', 'status': r.get('status') or '',
            })
        cfg = _announce_config()
        return success({'accounts': accounts, 'departments': departments,
                        'roster': _announce_roster_status(),
                        'configReady': bool((cfg.get('app_key') or '').strip()
                                            and (cfg.get('app_secret') or '').strip())})
    except Exception as e:
        return fail(str(e))


@app.route('/api/announce/dingtalk/contacts', methods=['GET'])
def announce_dingtalk_contacts():
    """同步钉钉组织架构到前端（部门树 + 成员列表）

    ★ 2026-09-19 起前端不再有手动同步按钮，本接口在页面加载时被自动调用；
      优先复用 10 分钟进程级缓存（预热线程启动时已拉好一次），只在过期时才真正访问钉钉。
    失败时明确返回「不能实现该功能」+ 原因（凭证缺失 / 无通讯录权限等）。
    """
    try:
        cache = _announce_contacts(force=False)
        st = _announce_roster_status()
        return success({
            'departments': cache['departments'],
            'users': [{'userid': u['userid'], 'name': u.get('name') or '',
                       'title': u.get('title') or '', 'deptIds': u.get('deptIds') or []}
                      for u in cache['users']],
            'syncedAt': st.get('syncedAt') or '',
            'userCount': len(cache['users']),
            'deptCount': len(cache.get('departments') or []),
        })
    except DingTalkError as e:
        return fail('不能实现该功能：无法读取钉钉组织架构 —— %s' % e)
    except Exception as e:
        return fail('不能实现该功能：无法读取钉钉组织架构 —— %s' % e)


@app.route('/api/announce/beautify', methods=['POST'])
def announce_beautify():
    """美化通告正文：保留原始事实与格式，只优化措辞和结构。"""
    try:
        data = request.get_json(silent=True) or {}
        text = str(data.get('text') or '').strip()
        if not text:
            return fail('请先填写需要美化的通告正文')
        if len(text) > 6000:
            return fail('通告正文过长（最多 6000 字），请拆分后再美化')

        api_key = _ANNOUNCE_AI_API_KEY or DEEPSEEK_API_KEY
        if not api_key:
            return fail('AI 美化服务尚未配置，请联系管理员设置 ANNOUNCE_AI_API_KEY')

        system_prompt = (
            '你是企业内部通告编辑。你的工作是把用户提供的中文通告润色得清晰、正式、友好、'
            '便于员工快速阅读。必须严格遵守：\n'
            '1. 只优化措辞、语序、分段和标题层级，不得新增、删除、猜测或修改任何事实、日期、'
            '金额、地点、联系人、制度要求或行动指令。\n'
            '2. 保留原文已有的数字、专有名词、链接、联系方式与明确格式；信息不完整时不要补写。\n'
            '3. 可使用简洁标题、小标题、项目符号与强调，但不要使用表格、代码块、寒暄、解释、'
            '署名或“以下是美化后的内容”等前缀。\n'
            '4. 仅输出可直接发送的最终通告正文。'
        )
        polished = call_deepseek_api(system_prompt, text, temperature=0.35, max_tokens=2200,
                                     api_key=api_key)
        polished = (polished or '').strip()
        if not polished:
            return fail('AI 美化暂时不可用，请稍后重试')
        return success({'text': polished}, '通告内容已美化')
    except Exception as e:
        print('[通告发放][AI美化] 异常: %s' % e)
        return fail('AI 美化失败，请稍后重试')


@app.route('/api/announce/send', methods=['POST'])
def announce_send():
    """发送通告：multipart 表单（payload=JSON 字符串 + files=附件）

    payload: { title, text, recipients: [{source, name, userId, mobile}] }
    附件按 content-type 自动区分图片（sampleImage）与文件（sampleFile）。
    """
    try:
        try:
            payload = json.loads(request.form.get('payload') or '{}')
        except Exception:
            return fail('请求数据格式不正确（payload 解析失败）')

        title = (payload.get('title') or '').strip()
        text = (payload.get('text') or '').strip()
        recipients = payload.get('recipients') or []
        files = request.files.getlist('files')

        if not title and not text and not files:
            return fail('通告内容为空：请填写文字、粘贴图片或添加附件')
        if not recipients:
            return fail('请先选择接收人')
        for f in files:
            if f.filename and f.content_type and f.content_type.startswith('image/') \
                    and f.content_length and f.content_length > _ANNOUNCE_IMG_MAX:
                return fail('图片「%s」超过 10MB 上限，请压缩后再发' % f.filename)
            elif f.content_length and f.content_length > _ANNOUNCE_FILE_MAX:
                return fail('附件「%s」超过 20MB 上限，请拆分后再发' % f.filename)

        client = _announce_client()
        cfg = _announce_config()

        # ---- 解析接收人 → 钉钉 userid ----
        resolved, resolve_fails = {}, []
        for r in recipients:
            ids, err = _announce_resolve_recipient(client, r)
            if err:
                resolve_fails.append('%s：%s' % (r.get('name') or r.get('userId') or '(未命名)', err))
            for uid in ids:
                resolved.setdefault(uid, (r.get('name') or uid))
        user_ids = list(resolved.keys())
        if not user_ids:
            return fail('所有接收人均无法匹配钉钉账号 —— ' + '；'.join(resolve_fails))

        # ---- 附件分类 ----
        images, docs = [], []
        for f in files:
            fname = f.filename or ('附件_%d' % (len(images) + len(docs) + 1))
            ct = (f.content_type or '').lower()
            content = f.read()
            if ct.startswith('image/'):
                images.append((fname, content, ct))
            else:
                docs.append((fname, content, ct))

        # ---- 组装文本消息（markdown，超长分段）----
        body = (('### %s\n\n' % title) if title else '') + text
        chunks = [body[i:i + _ANNOUNCE_MD_CHUNK] for i in range(0, len(body), _ANNOUNCE_MD_CHUNK)] \
            if body else []

        invalid_users, send_ok, send_details = set(), 0, []

        def _send_one(desc, fn):
            """执行一次批量发送并统计，返回是否全部成功"""
            nonlocal send_ok
            try:
                result = fn()
                bad = (result or {}).get('invalidStaffIdList') or []
                if bad:
                    for uid in bad:
                        invalid_users.add(uid)
                        send_details.append('%s：成员 %s 不在机器人应用可见范围内'
                                            % (desc, resolved.get(uid, uid)))
                    return False
                _announce_apply_robot_code(cfg, (result or {}).get('robotCode'))
                send_ok += 1
                return True
            except Exception as e:
                send_details.append('%s：发送失败 - %s' % (desc, e))
                return False

        all_ok = True
        for idx, chunk in enumerate(chunks):
            label = '文字内容' + ('(第%d段)' % (idx + 1) if len(chunks) > 1 else '')
            all_ok = _send_one(label, lambda c=chunk: client.send_markdown(
                user_ids, title or '通告发放', c)) and all_ok
        for fname, content, ct in images:
            all_ok = _send_one('图片「%s」' % fname, (lambda fn=fname, co=content, t=ct: client.send_image(
                user_ids, client.upload_image(fn, co, t or 'image/jpeg')))) and all_ok
        for fname, content, ct in docs:
            all_ok = _send_one('附件「%s」' % fname, (lambda fn=fname, co=content, t=ct: client.send_file(
                user_ids, client.upload_file(fn, co, t or 'application/octet-stream'), fn))) and all_ok

        status = 'success' if (all_ok and not invalid_users and not resolve_fails) else (
            'partial' if send_ok else 'fail')
        detail = '；'.join(resolve_fails + send_details) or (
            '已发送给 %d 位成员' % len(user_ids))
        try:
            db_execute(
                'INSERT INTO announce_logs (send_date, status, total, ok_count, detail) '
                'VALUES (%s, %s, %s, %s, %s)',
                [date.today(), status, len(user_ids) + len(resolve_fails),
                 len(user_ids) - len(invalid_users), detail], fetch=False)
        except Exception:
            pass

        result = {
            'totalUsers': len(user_ids), 'okUsers': len(user_ids) - len(invalid_users),
            'resolveFails': resolve_fails, 'sendDetails': send_details,
            'status': status,
        }
        if status == 'fail':
            return fail('通告发送失败：' + detail, code=1)
        return success(result, '通告已发送给 %d/%d 位成员' % (result['okUsers'], result['totalUsers']))
    except DingTalkError as e:
        return fail(str(e))
    except Exception as e:
        traceback.print_exc()
        return fail(str(e))


@app.route('/api/announce/config', methods=['GET'])
def announce_config_get():
    """读取通告发放自己的钉钉应用凭证（secret 掩码，不回传明文）"""
    try:
        _announce_ensure_table()
        cfg = _announce_config()
        return success({
            'appKey': cfg.get('app_key', ''),
            'appSecret': _PUSH_SECRET_MASK if cfg.get('app_secret') else '',
            'hasAppSecret': bool(cfg.get('app_secret')),
            'robotCode': cfg.get('robot_code', ''),
            'agentId': cfg.get('agent_id', ''),
        })
    except Exception as e:
        return fail(str(e))


@app.route('/api/announce/config', methods=['POST'])
def announce_config_save():
    """保存通告发放自己的钉钉应用凭证（secret 回传掩码时保持原值不变）"""
    try:
        _announce_ensure_table()
        d = request.get_json(force=True) or {}
        items = {}
        if 'appKey' in d:
            items['app_key'] = (d.get('appKey') or '').strip()
        if 'appSecret' in d:
            v = (d.get('appSecret') or '').strip()
            if v and v != _PUSH_SECRET_MASK:
                items['app_secret'] = v
        if 'robotCode' in d:
            items['robot_code'] = (d.get('robotCode') or '').strip()
        if 'agentId' in d:
            items['agent_id'] = (d.get('agentId') or '').strip()
        ok, msg = _announce_config_save(items)
        if not ok:
            return fail(msg)
        return success(None, '发送设置已保存')
    except Exception as e:
        return fail(str(e))


@app.route('/api/announce/config/test', methods=['POST'])
def announce_config_test():
    """连通性测试：验证凭证可换取 accessToken；传入 userId 时补发一条测试消息"""
    try:
        client = _announce_client()
        client.get_token(force=True)
        sent = False
        user_id = ((request.get_json(silent=True) or {}).get('userId') or '').strip()
        if user_id:
            client.send_text([user_id], '【测试】「通告发放」钉钉通道连通正常。')
            sent = True
        return success({'tokenOk': True, 'testSent': sent},
                       '凭证有效，可正常获取 accessToken' if not sent else '凭证有效，测试消息已发送')
    except DingTalkError as e:
        return fail(str(e))
    except Exception as e:
        return fail(str(e))



_SEEDING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools')
_SEEDING_ACCOUNTS_FILE = os.path.join(_SEEDING_DIR, 'seeding_accounts.json')
_DOUYIN_COOKIE_FILE = os.path.join(_SEEDING_DIR, 'douyin_cookie.txt')
_DOUYIN_WORKS_CSV = os.path.join(_SEEDING_DIR, '_douyin_works.csv')
_XHS_COOKIE_FILE = os.path.join(_SEEDING_DIR, 'xhs_cookie.txt')
_XHS_WORKS_FILE = os.path.join(_SEEDING_DIR, '_xhs_works.json')
_SEEDING_STATE_FILE = os.path.join(_SEEDING_DIR, '_seeding_state.json')
# 作品数据自动更新间隔（秒）：每半小时
_SEEDING_AUTO_INTERVAL = 1800
# ★ 两次「成功抓取」之间的最小间隔（秒）：自动循环触发前检查，不够就跳过本轮。
#   为什么需要（2026-09-17 17:36 那次告警的根因）：循环末尾是固定 sleep(1800)，
#   但服务每次重启都会把节拍从 0 重排 —— 当天 ecom 重启 26 次，导致同一账号
#   在 17:04 / 17:06 / 17:36 / 17:44 被连抓 4 轮（最短间隔只有 105 秒），
#   小红书随即对主页作品接口限流、返回 0 条作品 → 误报「登录态可能已失效」。
#   只作用于自动循环；「数据更新」按钮的手工触发不受此限制。
_SEEDING_MIN_GAP = 20 * 60
# 抓取日志单文件上限，超过滚动成 _scrape_<平台>.log.1（只留 1 份历史）
_SEEDING_LOG_MAX_BYTES = 2 * 1024 * 1024
# ★★ 作品数据「停更」判定阈值（小时）：前端顶部时间染色 + meta 接口 level 分级共用。
#   ⚠ 必须与 tools/seeding_health.py 的 STALE_HOURS 保持一致（那边用于钉钉告警），
#     两处不一致会出现「页面标红但没告警」或反之的错位。
_SEEDING_STALE_HOURS = 3.0
# 「将要停更」的提醒阈值（小时）：1 小时 ≈ 漏了 2 轮自动抓取，页面转橙提示留意
_SEEDING_WARN_HOURS = 1.0
# 抓取健康体检脚本：每轮自动更新前先体检上一轮，异常时它自己推钉钉（见 tools/seeding_health.py）
_SEEDING_HEALTH_SCRIPT = os.path.join(_SEEDING_DIR, 'seeding_health.py')
# ★★ 自动抓取时间窗（2026-09-18 改）：只在 09:00~19:00 之间每半小时抓一轮，夜间不抓。
#   背景：原实现是「启动即抓一轮 + 死循环 sleep 30 分钟」，凌晨也在打接口，
#   既容易被平台限流/风控，也没人在夜里处置告警。抖音与小红书共用同一时间表。
_SEEDING_WINDOW_START = 9
_SEEDING_WINDOW_END = 19
# ★★ 新鲜度判定的「参照点缓冲」（秒）。
#   为什么需要：有了时间窗之后，夜间（19:00 → 次日 09:00）本来就不会有新数据，
#   若还按「距今超过 N 小时」判停更，每天 09:00 开跑前必然误报一次「停更 14 小时」。
#   所以新鲜度改为对比「最近一次本应完成的抓取时刻」，缓冲用于遮住「刚触发、还在跑」的本轮。
#   取 40 分钟（> 1 个抓取周期）> 单轮耗时，避免长轮次被误判。
_SEEDING_FRESH_GRACE = _SEEDING_AUTO_INTERVAL * 4 // 3


def _seeding_log_rotate(path, max_bytes=None):
    """日志超过上限时滚动一份 .1 备份，避免长年追加把磁盘写满"""
    max_bytes = max_bytes or _SEEDING_LOG_MAX_BYTES
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            bak = path + '.1'
            if os.path.exists(bak):
                os.remove(bak)
            os.rename(path, bak)
    except Exception as e:
        print('[种草] 日志滚动失败: %s' % e)


def _seeding_health_check(dry_run=False):
    """调 tools/seeding_health.py 体检上一轮抓取；异常时由该脚本推钉钉告警。

    单独做成脚本（而不是写在 app.py 里）是为了能手动复跑：
        python3 tools/seeding_health.py --dry-run
    永不抛异常，返回 (退出码 or None, 输出文本)。
    """
    try:
        if not os.path.exists(_SEEDING_HEALTH_SCRIPT):
            return None, '未找到 %s' % _SEEDING_HEALTH_SCRIPT
        import sys as _sys_hc
        py = '/opt/ecom/venv/bin/python'
        if not os.path.exists(py):
            py = _sys_hc.executable
        cmd = [py, _SEEDING_HEALTH_SCRIPT] + (['--dry-run'] if dry_run else [])
        r = subprocess.run(cmd, cwd=_SEEDING_DIR, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=180)
        out = (r.stdout or b'').decode('utf-8', 'replace').strip()
        for line in out.splitlines():
            print('[种草][体检] %s' % line)
        return r.returncode, out
    except Exception as e:
        print('[种草][体检] 执行失败: %s' % e)
        return None, str(e)


def _seeding_progress_file(platform):
    return os.path.join(_SEEDING_DIR, '_seeding_progress_%s.json' % platform)


def _seeding_progress_write(platform, status, done, total, msg=''):
    """写抓取进度文件，供前端进度条轮询"""
    try:
        with open(_seeding_progress_file(platform), 'w', encoding='utf-8') as f:
            json.dump({'status': status, 'done': done, 'total': total, 'ts': time.time(), 'msg': msg}, f, ensure_ascii=False)
    except Exception:
        pass


def _seeding_progress_read(platform):
    """读抓取进度：{status, done, total, ts, msg, ok, accounts, progress, finished}

    ok/accounts = 本轮成功的账号数 / 账号总数（抓取脚本写入），
    用于判断「本轮抓取是否完整」—— 不完整时不做被删作品对比，避免误报。
    """
    info = {'status': 'idle', 'done': 0, 'total': 0, 'ts': 0, 'msg': '',
            'ok': None, 'accounts': None, 'progress': 0, 'finished': False}
    path = _seeding_progress_file(platform)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                p = json.load(f)
            info['status'] = p.get('status', 'idle')
            info['done'] = int(p.get('done', 0) or 0)
            info['total'] = int(p.get('total', 0) or 0)
            info['ts'] = float(p.get('ts', 0) or 0)
            info['msg'] = p.get('msg', '') or ''
            if p.get('ok') is not None:
                info['ok'] = int(p.get('ok') or 0)
            if p.get('accounts') is not None:
                info['accounts'] = int(p.get('accounts') or 0)
        except Exception:
            pass
    if info['total']:
        info['progress'] = min(100, round(info['done'] / info['total'] * 100))
    if info['status'] == 'done':
        info['progress'] = 100
        info['finished'] = True
    return info


# 平台 -> 当前抓取子进程（进程内有效；与 _AUTH_TOKENS 同一约束：单 worker）
# 只靠进度文件的 30 分钟时间窗判断「是否在跑」会误判（2026-09-17 当天误拦 3 次），
# 抓到的 Popen 对象能直接问「进程还活着吗」。
_SEEDING_PROCS = {}


def _seeding_reap(platform, proc, log_path):
    """后台回收种草抓取子进程，避免 xvfb-run 包装进程变 <defunct> 僵尸（2026-09-17 补）。

    原先只把 Popen 存进 _SEEDING_PROCS，靠 _seeding_is_running() 里的 poll()
    顺手回收 → 进程退出后到下一次被 poll 之间的空窗期始终是僵尸。
    线上实测每轮抖音抓取都会留下一个僵尸 xvfb-run（PPID = gunicorn worker），
    因为 _seeding_is_running() 只在有人点按钮/轮询该平台时才会被调用。

    这里单独起线程 wait()，进程一退出立即回收，顺带把退出码落进抓取日志。
    不影响 _seeding_is_running()：wait() 与 poll() 设置的是同一个 returncode，
    已退出的 proc 仍能被它 poll() 到并补写终态，故这里**不**从 _SEEDING_PROCS 摘除。
    """
    try:
        rc = proc.wait()
    except Exception:
        return
    try:
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write('[%s] 进程已退出 exit=%s\n'
                    % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), rc))
    except Exception:
        pass


def _seeding_is_running(platform):
    """判断指定平台是否仍在抓取。

    ① 本轮由本进程拉起的 → 直接看子进程是否还活着（最准）；
       进程已退出却仍停在 running（脚本静默退出没写状态）→ 补写终态并放行重触发。
    ② 否则（进程重启后 / 手工触发）退回旧判据：running 且时间戳在一个更新周期内。
    """
    proc = _SEEDING_PROCS.get(platform)
    if proc is not None:
        if proc.poll() is None:
            return True
        _SEEDING_PROCS.pop(platform, None)
        p = _seeding_progress_read(platform)
        if p.get('status') == 'running':
            rc = proc.returncode
            msg = '抓取进程已退出（exit %s）但未写入进度' % rc
            _seeding_progress_write(platform, 'done' if rc == 0 else 'error',
                                    p.get('done', 0), p.get('total', 0), msg)
            print('[种草] %s' % msg)
        return False

    p = _seeding_progress_read(platform)
    if p.get('status') != 'running':
        return False
    ts = p.get('ts') or 0
    return (time.time() - ts) < _SEEDING_AUTO_INTERVAL


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
        # 该账号曾被删过（作品被级联清空）→ 解除标记，重新抓到作品后能正常回到列表
        _seeding_unpurge_account(acct)
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
        old_identity = dict(target)   # 改名/换号前后的标识都要解除「已删除」标记
        target['name'] = (data.get('name') if data.get('name') is not None else target.get('name', '')).strip()
        target['platform'] = (data.get('platform') if data.get('platform') is not None else target.get('platform', 'douyin')).strip()
        target['douyinId'] = (data.get('douyinId') if data.get('douyinId') is not None else target.get('douyinId', '')).strip()
        target['homepage'] = (data.get('homepage') if data.get('homepage') is not None else target.get('homepage', '')).strip()
        target['redId'] = (data.get('redId') if data.get('redId') is not None else target.get('redId', '')).strip()
        target['department'] = (data.get('department') if data.get('department') is not None else target.get('department', '')).strip()
        _seeding_save_accounts(accounts)
        # 改回某个曾被删掉的账号标识 → 解除「已删除」标记
        _seeding_unpurge_account(old_identity)
        _seeding_unpurge_account(target)
        return success(target, '种草账号已更新')
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/accounts/<int:aid>', methods=['DELETE'])
def seeding_delete_account(aid):
    """删除种草账号：同步清空该账号的作品数据与被删作品记录（见 _seeding_purge_account_works）"""
    try:
        accounts = _seeding_load_accounts()
        target = next((a for a in accounts if int(a.get('id', 0) or 0) == aid), None)
        kept = [a for a in accounts if int(a.get('id', 0) or 0) != aid]
        _seeding_save_accounts(kept)
        report = _seeding_purge_account_works([target] if target else [], kept)
        n_works, n_deleted = _seeding_purge_counts(report)
        msg = '种草账号已删除'
        if n_works or n_deleted:
            msg += '（同步清空 %d 条作品数据、%d 条被删作品记录）' % (n_works, n_deleted)
        return success({'works': n_works, 'deletedWorks': n_deleted}, msg)
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/accounts/batch-delete', methods=['POST'])
def seeding_batch_delete_accounts():
    """批量删除种草账号。

    body: {ids: [1,2,3]}
    ★ 用 POST 而不是 DELETE：DELETE 带 body 在部分网关/代理上会被丢掉，
      且 /api/seeding/accounts/<int:aid> 的 DELETE 已占用该路径。
    """
    try:
        body = request.get_json(force=True) or {}
        raw = body.get('ids')
        if not isinstance(raw, list) or not raw:
            return fail('请选择要删除的种草账号')
        ids = set()
        for x in raw:
            try:
                ids.add(int(x))
            except Exception:
                pass
        if not ids:
            return fail('请选择要删除的种草账号')
        accounts = _seeding_load_accounts()
        before = len(accounts)
        removed_accs = [a for a in accounts if int(a.get('id', 0) or 0) in ids]
        kept = [a for a in accounts if int(a.get('id', 0) or 0) not in ids]
        removed = before - len(kept)
        if not removed:
            return fail('所选种草账号不存在（可能已被删除）')
        _seeding_save_accounts(kept)
        # 被删账号的作品数据 / 被删作品记录一并清空（见 _seeding_purge_account_works）
        report = _seeding_purge_account_works(removed_accs, kept)
        n_works, n_deleted = _seeding_purge_counts(report)
        msg = '已删除 %d 个种草账号' % removed
        if n_works or n_deleted:
            msg += '（同步清空 %d 条作品数据、%d 条被删作品记录）' % (n_works, n_deleted)
        return success({'deleted': removed, 'works': n_works, 'deletedWorks': n_deleted}, msg)
    except Exception as e:
        return fail(str(e))


def _seeding_platform_accounts(platform):
    """某平台「真正能抓」的账号数：抖音=有主页链接，小红书=有小红书号。

    用于「账号为空时跳过抓取」的判定，口径与两个抓取脚本的筛选保持一致。
    """
    n = 0
    for a in _seeding_load_accounts():
        p = (a.get('platform') or 'douyin').strip().lower()
        if platform == 'xhs':
            if p == 'xhs' and str(a.get('redId') or '').strip():
                n += 1
        else:
            if p != 'xhs' and str(a.get('homepage') or '').strip():
                n += 1
    return n


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
    """作品数据列表：platform=douyin 读抖音 CSV，platform=xhs 读小红书 JSON；抖音无真实数据时回退虚拟数据。

    ★ 手动删除的作品（hidden 名单）在返回前过滤掉：
      抓取脚本每轮都会重写数据文件，直接改文件删不掉（下一轮又回来）。
      所以「删除」记在 _seeding_state.json 的 hidden 名单里，读列表时过滤 —— 删了就一直是删的。
      ⚠ 对账（被删作品）必须用未过滤的原始列表，否则会把用户手动删掉的作品误判成「被平台删除」。
    """
    try:
        platform = (request.args.get('platform') or 'douyin').strip()
        if platform == 'xhs':
            works = _seeding_load_xhs_works()
            if os.path.exists(_XHS_WORKS_FILE):
                # 走 guard：本轮抓取不完整时不对比，避免误报「作品被删」
                _seeding_reconcile_guard('xhs', works)
            return success(_seeding_filter_hidden('xhs', works))
        real = _seeding_load_works_csv()
        if real is not None:
            _seeding_reconcile_guard('douyin', real)
            return success(_seeding_filter_hidden('douyin', real))
        # 无真实数据时的虚拟数据同样支持删除（否则删了又"复活"，看着像 bug）
        return success(_seeding_filter_hidden('douyin', _seeding_mock_works()))
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/works', methods=['DELETE'])
def seeding_delete_works():
    """删除作品数据：单条 / 批量（前端把选中的作品行整条传上来）。

    body: {platform: 'douyin'|'xhs', items: [{link, url, account, title, ...}, ...]}
    不物理改数据文件，而是把作品唯一键写入 hidden 名单 —— 理由见 seeding_list_works 注释。
    """
    try:
        body = request.get_json(force=True) or {}
        platform = (body.get('platform') or 'douyin').strip()
        if platform not in ('douyin', 'xhs'):
            platform = 'douyin'
        items = body.get('items')
        if not isinstance(items, list) or not items:
            return fail('请选择要删除的作品数据')
        keys = []
        for it in items:
            if not isinstance(it, dict):
                continue
            k = _seeding_work_key(it)
            if k and k not in keys:
                keys.append(k)
        if not keys:
            return fail('未能识别要删除的作品数据')
        state = _seeding_load_state()
        hidden = state.get('hidden')
        if not isinstance(hidden, dict):
            hidden = {}
        cur = hidden.get(platform)
        if not isinstance(cur, list):
            cur = []
        added = 0
        for k in keys:
            if k not in cur:
                cur.append(k)
                added += 1
        hidden[platform] = cur
        state['hidden'] = hidden
        _seeding_save_state(state)
        if added:
            return success({'deleted': added}, '已删除 %d 条作品数据' % added)
        return success({'deleted': 0}, '所选作品数据已删除过')
    except Exception as e:
        return fail(str(e))


def _seeding_scheduled_slots(day):
    """某天时间窗内的全部抓取时刻：09:00 起每 30 分钟一次，含 19:00 收尾"""
    slots = []
    t = day.replace(hour=_SEEDING_WINDOW_START, minute=0, second=0, microsecond=0)
    end = day.replace(hour=_SEEDING_WINDOW_END, minute=0, second=0, microsecond=0)
    step = timedelta(seconds=_SEEDING_AUTO_INTERVAL)
    while t <= end:
        slots.append(t)
        t = t + step
    return slots


def _seeding_next_run(now=None):
    """下一次自动抓取时刻（严格晚于 now）；窗口外返回次日 09:00"""
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for day in (today, today + timedelta(days=1)):
        for s in _seeding_scheduled_slots(day):
            if s > now:
                return s
    return _seeding_scheduled_slots(today + timedelta(days=1))[0]


def _seeding_last_expected_run(now=None):
    """最近一次「本应已完成」的抓取时刻，用于新鲜度分级与停更判定。

    ★ 为什么不能直接用「现在 - 3 小时」（2026-09-18）：
      自动抓取只在 09:00~19:00 进行，夜间 19:00 → 次日 09:00 本来就没有新数据。
      按老口径判停更，每天 09:00 开跑前必然误报一次「停更 14 小时」。
      改为以「最近一次应抓取时刻」为参照，夜间空档自然被排除。
    缓冲 _SEEDING_FRESH_GRACE：给刚触发、还在跑的本轮留出时间，避免它没写完就被判停更。
    """
    now = now or datetime.now()
    ref = now - timedelta(seconds=_SEEDING_FRESH_GRACE)
    today = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    for day in (today, today - timedelta(days=1)):
        slots = [s for s in _seeding_scheduled_slots(day) if s <= ref]
        if slots:
            return slots[-1]
    return (today - timedelta(days=1)).replace(hour=_SEEDING_WINDOW_END, minute=0,
                                               second=0, microsecond=0)


def _seeding_freshness_level(mtime):
    """按「落后最近一次应抓取时刻多久」分级：ok / warn / stale / none"""
    if not mtime:
        return 'none'
    expected = _seeding_last_expected_run().timestamp()
    lag_hours = (expected - float(mtime)) / 3600.0
    if lag_hours < _SEEDING_WARN_HOURS:
        return 'ok'
    if lag_hours <= _SEEDING_STALE_HOURS:
        return 'warn'
    return 'stale'


def _seeding_platform_meta(platform):
    """单平台的作品数据元信息：mtime / rows / source / 新鲜度。

    ★★ 为什么要有 age_hours + level（2026-09-18）：
      前端顶部原来只有一个「数据更新时间」，值取的是当前选中平台的 mtime。
      09-18 早上小红书 cookie 失效停更 4.9 小时，但抖音正常抓取刷新了 CSV，
      页面顶部照样显示 08:24 → 看起来两个平台都新鲜，实际小红书早已停更。
      现在后端直接给出「距今多少小时」和分级，前端不必再猜哪个时间代表谁。
    ★ level 由 _seeding_freshness_level 给出（对比「最近一次应抓取时刻」而非「现在」），
      这样夜间空档不会把两个平台都染红。age_hours 仍是原始距今小时数，供展示用。
      ⚠ 阈值必须与 tools/seeding_health.py 的 STALE_HOURS / 时间窗保持一致。
    """
    if platform == 'xhs':
        path = _XHS_WORKS_FILE
        real = _seeding_load_xhs_works()
        source = 'real' if real else 'empty'
    else:
        path = _DOUYIN_WORKS_CSV
        real = _seeding_load_works_csv()
        source = 'real' if real is not None else 'mock'
    if os.path.exists(path):
        mtime = os.path.getmtime(path)
        age_hours = max(0.0, (time.time() - mtime) / 3600.0)
    else:
        mtime = None
        age_hours = None
    level = _seeding_freshness_level(mtime)
    return {
        'mtime': mtime,
        'rows': len(real) if real else 0,
        'source': source,
        'age_hours': round(age_hours, 2) if age_hours is not None else None,
        'level': level,
    }


@app.route('/api/seeding/works/meta', methods=['GET'])
def seeding_works_meta():
    """作品数据来源与最后抓取时间，供前端轮询判断抓取是否完成。

    默认返回「两个平台各自」的元信息（platforms 字段），顶层 mtime/rows/source
    仍保持向后兼容 = platform 参数指定平台（不传则 douyin）的值。
    """
    try:
        platform = (request.args.get('platform') or 'douyin').strip()
        if platform not in ('douyin', 'xhs'):
            platform = 'douyin'
        info = _seeding_platform_meta(platform)
        resp = {
            'mtime': info['mtime'],
            'rows': info['rows'],
            'source': info['source'],
            'age_hours': info['age_hours'],
            'level': info['level'],
        }
        # 两平台都给出：前端顶部要并列展示，避免"只显示一个平台的假新鲜"
        resp['platforms'] = {
            'douyin': _seeding_platform_meta('douyin'),
            'xhs': _seeding_platform_meta('xhs'),
        }
        return success(resp)
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
    """后台异步启动指定平台的作品抓取脚本，写进度初始状态。

    返回 (err, skip)：
      err  = 硬错误（Cookie 未配置 / 脚本缺失 / 正在跑），需要提示用户；
      skip = 「本轮无需抓取」的原因（该平台还没有可用账号），**不是错误**，只做提示。
    """
    import sys as _sys_scrape
    # 上次抓取仍在进行时不再重复触发，避免任务堆积
    if _seeding_is_running(platform):
        return '该平台抓取正在进行中，请稍后再试', None
    # ★ 账号为空时直接跳过（2026-09-18）：不再拉起脚本空跑。
    #   进度写 'skipped' 而非 'error' —— 体检脚本把它当「健康」处理，不会误推钉钉告警，
    #   也不会因为数据文件不更新而报「停更」。脚本里同样有兜底（防手工直接执行脚本时报错）。
    if _seeding_platform_accounts(platform) <= 0:
        label = '小红书' if platform == 'xhs' else '抖音'
        msg = '未配置%s账号，本轮跳过抓取（请先在「种草账号」页添加）' % label
        _seeding_progress_write(platform, 'skipped', 0, 0, msg)
        print('[种草][跳过] %s: %s' % (platform, msg), flush=True)
        return None, msg
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if platform == 'xhs':
        scraper = os.path.join(_SEEDING_DIR, 'seeding_xhs.py')
        if not os.path.isdir(os.path.join(_SEEDING_DIR, 'Spider_XHS')):
            return '缺少小红书抓取依赖：未找到 tools/Spider_XHS 目录，请先部署 Spider_XHS 开源项目', None
    else:
        scraper = os.path.join(_SEEDING_DIR, 'douyin_video_scraper.py')
    if not os.path.exists(scraper):
        return '抓取脚本不存在：' + scraper, None
    # Cookie 校验：缺失时直接给出明确提示，避免后台无谓触发
    if platform == 'douyin':
        if not (os.path.exists(_DOUYIN_COOKIE_FILE) and os.path.getsize(_DOUYIN_COOKIE_FILE) > 0):
            return '抖音 Cookie 未配置，请先在「数据更新」面板保存 Cookie', None
    else:
        fallback_cookie = os.path.join(_SEEDING_DIR, 'cookie.txt')
        if not (os.path.exists(_XHS_COOKIE_FILE) and os.path.getsize(_XHS_COOKIE_FILE) > 0) and \
           not (os.path.exists(fallback_cookie) and os.path.getsize(fallback_cookie) > 0):
            return '小红书 Cookie 未配置，请先在「数据更新」面板保存 Cookie', None
    _seeding_progress_write(platform, 'running', 0, 0)
    log_path = os.path.join(_SEEDING_DIR, '_scrape_%s.log' % platform)
    try:
        _seeding_log_rotate(log_path)
    except Exception:
        pass
    # ★ 追加而非覆盖（原来用 'w' 每次都清空，历史失败轨迹全丢，查不了原因）
    log_file = open(log_path, 'a', encoding='utf-8')
    try:
        log_file.write('\n%s\n[%s] 触发抓取 platform=%s\n%s\n'
                       % ('=' * 60, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                          platform, '=' * 60))
        log_file.flush()
        # ★ 抖音改走 Playwright 真实浏览器（2026-09-17）：必须用 /opt/pw/venv 的解释器
        #   （只有它装了 playwright）+ xvfb 虚拟屏（浏览器以 headless=False 启动，需要显示环境）。
        #   与抖店抓取链路同源配置。其它平台（小红书等）仍用后端自身解释器。
        cmd = [_sys_scrape.executable, scraper]
        if platform == 'douyin':
            _pw_py = '/opt/pw/venv/bin/python'
            _xvfb = '/usr/bin/xvfb-run'
            if os.path.exists(_pw_py) and os.path.exists(_xvfb):
                cmd = [_xvfb, '-a', _pw_py, scraper]
            else:
                warn = ('[启动告警] 抖音需要 %s + %s（未找到），已回退后端解释器；'
                        'Playwright 缺失会让本轮直接失败' % (_pw_py, _xvfb))
                print(warn, flush=True)
                log_file.write(warn + '\n')
                log_file.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=project_root,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            # ★ 独立会话：让 xvfb-run → Xvfb / python / Chrome 落在同一进程组，
            #   需要时可整组清理（与抓取链路 _fetch_kill_tree 同一口径）。
            #   systemd 按 cgroup 停服务，不受 setsid 影响，重启 ecom 仍能带走子进程。
            start_new_session=True,
        )
        _SEEDING_PROCS[platform] = proc
        # ★ 单独线程 wait()：进程退出即回收，杜绝 xvfb-run 僵尸
        threading.Thread(target=_seeding_reap, args=(platform, proc, log_path),
                         daemon=True).start()
    finally:
        log_file.close()
    return None, None


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
        err, skip = _seeding_launch(platform)
        if err:
            return fail(err)
        # 没账号可抓不是错误：返回成功但 triggered=false，前端据此提示而不是报错、也不轮询进度
        if skip:
            return success({'triggered': False, 'reason': skip}, skip)
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


@app.route('/api/seeding/health', methods=['GET'])
def seeding_health_api():
    """抓取体检（只读、不发消息）。

    healthy=false 表示有平台抓取失败或作品数据停更。
    真正的告警由定时线程每轮自动触发（tools/seeding_health.py → 钉钉）。
    """
    try:
        code, out = _seeding_health_check(dry_run=True)
        return success({'healthy': code == 0, 'exit': code, 'detail': out})
    except Exception as e:
        return fail(str(e))


# ======================== 种草：部门配置 + 点赞阈值钉钉推送 ========================
# 三份 JSON 配置（都在 tools/ 下，随 _SEEDING_DIR 走）：
#   seeding_departments.json  部门列表  {"departments": ["三部", "四部", "五部"]}
#   seeding_push_rules.json   推送规则  {"三部": {"userId": "...", "userName": "张三", "threshold": 1000, "enabled": true}}
#   seeding_push_log.json     推送账本  {"pushed": {"link:https://...": {"ts": ..., "likes": ..., "dept": "三部"}}}
# ★ 推送通道沿用「企业内部应用机器人单聊」（backend/dingtalk.py），与每日报告、抓取告警同一条链路。
# ★★ 2026-09-18 改：收件人名单**独立**成表 seeding_push_users，不再复用「每日数据分析 → 钉钉推送」的
#   dingtalk_push_users —— 两个场景的人本来就不是一拨人（种草是各业务部门对接人，日报是全员/管理层），
#   共用一份会互相污染（在这边"匹配并绑定"的人会凭空出现在日报收件人里）。现在两边各管各的，
#   只有钉钉应用凭证（AppKey/Secret）继续共用。
#   ⚠️ 系统告警（dev_alert.py 找「李自豪」）仍走 dingtalk_push_users，那是开发告警，与种草无关。

_SEEDING_DEPT_FILE = os.path.join(_SEEDING_DIR, 'seeding_departments.json')
_SEEDING_PUSH_RULE_FILE = os.path.join(_SEEDING_DIR, 'seeding_push_rules.json')
_SEEDING_PUSH_LOG_FILE = os.path.join(_SEEDING_DIR, 'seeding_push_log.json')
_SEEDING_DEPT_DEFAULT = ['三部', '四部', '五部']   # 迁移兜底：文件缺失时页面不至于没有部门

# 种草智能体：爆文库 / 优化建议
_SEEDING_HOT_KB_LIMIT = 100      # 爆文带入模型的条数上限
_SEEDING_HOT_ITEM_MAX = 3000     # 单条爆文带入模型的字符上限
_SEEDING_FB_MAX = 2000           # 单条优化建议的字符上限


def _seeding_ensure_tables():
    """建表（模块导入时执行）：种草推送人名单 / 爆文库 / 优化建议。

    线上由 gunicorn 启动不会跑 __main__，所以建表必须在导入阶段完成
    （与 _push_ensure_tables 同一套路）。
    """
    try:
        db_execute("""
            CREATE TABLE IF NOT EXISTS seeding_push_users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(64) NOT NULL,
                mobile VARCHAR(32) DEFAULT '',
                user_id VARCHAR(128) DEFAULT '',
                enabled TINYINT(1) DEFAULT 1,
                remark VARCHAR(255) DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """, fetch=False)
        db_execute("""
            CREATE TABLE IF NOT EXISTS `已上传爆文库` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                `标题` VARCHAR(128) DEFAULT '',
                `爆文` TEXT,
                `来源` VARCHAR(64) DEFAULT '',
                `创建时间` DATETIME DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """, fetch=False)
        db_execute("""
            CREATE TABLE IF NOT EXISTS `种草优化建议` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                `内容` TEXT,
                `提交人` VARCHAR(64) DEFAULT '',
                `角色` VARCHAR(64) DEFAULT '',
                `状态` VARCHAR(32) DEFAULT '',
                `创建时间` DATETIME DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """, fetch=False)
        print('[种草] 数据表就绪（推送人 / 爆文库 / 优化建议）')
    except Exception as e:
        print('[种草] 建表失败: %s' % e)
        return

    # ★ 一次性迁移（只在名单为空时做）：把已经绑在部门规则里的联系人补进新名单。
    #   为什么需要：名单从「共用 dingtalk_push_users」切成独立表之后，老配置里绑好的
    #   userId 还在 rules 里（点赞推送照常），但页面上「已登记联系人」会是空的，
    #   看起来像"绑定丢了"。这里把规则里的人补进去，页面观感与数据一致。
    try:
        if not db_execute('SELECT id FROM seeding_push_users LIMIT 1'):
            seen = set()
            for _d, _r in (_seeding_push_rules() or {}).items():
                nr = _seeding_norm_rule(_r)
                if nr['userId'] and nr['userId'] not in seen:
                    seen.add(nr['userId'])
                    db_execute('INSERT INTO seeding_push_users (name, mobile, user_id, enabled) '
                               'VALUES (%s, %s, %s, %s)',
                               [nr['userName'] or '未命名', '', nr['userId'], 1], fetch=False)
            if seen:
                print('[种草] 已从部门推送规则迁移 %d 位联系人到种草推送名单（独立名单首次建立）'
                      % len(seen))
    except Exception as e:
        print('[种草] 联系人迁移跳过: %s' % e)


def _seeding_cfg_load(path, default):
    """读 JSON 配置；缺失/损坏/类型不符一律回退默认值（配置文件不值得抛 500）"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _seeding_cfg_save(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _seeding_dept_list():
    """部门列表（去重 + 去空）"""
    raw = _seeding_cfg_load(_SEEDING_DEPT_FILE, {})
    depts = raw.get('departments') if isinstance(raw, dict) else None
    if not isinstance(depts, list) or not depts:
        depts = list(_SEEDING_DEPT_DEFAULT)
    out, seen = [], set()
    for d in depts:
        d = str(d or '').strip()
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _seeding_push_rules():
    raw = _seeding_cfg_load(_SEEDING_PUSH_RULE_FILE, {})
    return raw if isinstance(raw, dict) else {}


def _seeding_push_log():
    raw = _seeding_cfg_load(_SEEDING_PUSH_LOG_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    if not isinstance(raw.get('pushed'), dict):
        raw['pushed'] = {}
    return raw


def _seeding_norm_rule(r):
    """规则字段归一化：阈值转 int、启用转 bool（前端可能传字符串，直接比会出错）"""
    r = r if isinstance(r, dict) else {}
    try:
        thr = int(r.get('threshold') or 0)
    except Exception:
        thr = 0
    return {
        'userId': str(r.get('userId') or '').strip(),
        'userName': str(r.get('userName') or '').strip(),
        'threshold': max(0, thr),
        'enabled': bool(r.get('enabled')),
    }


# ---------- 种草推送人名单（独立于「每日数据分析 → 钉钉推送」） ----------

def _seeding_push_users(only_enabled=False):
    """种草推送人名单"""
    sql = 'SELECT id, name, mobile, user_id, enabled, remark FROM seeding_push_users'
    if only_enabled:
        sql += ' WHERE enabled = 1'
    return db_execute(sql + ' ORDER BY id') or []


@app.route('/api/seeding/push-users', methods=['GET'])
def seeding_push_user_list():
    """种草推送人名单"""
    try:
        users = [{
            'id': u.get('id'),
            'name': u.get('name') or '',
            'mobile': (u.get('mobile') or '').strip(),
            'userId': (u.get('user_id') or '').strip(),
            'enabled': 1 if u.get('enabled') in (1, '1', True) else 0,
        } for u in _seeding_push_users()]
        return success(users)
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/push-users', methods=['POST'])
def seeding_push_user_create():
    """新增种草推送人（手机号或 userId 至少填一个）"""
    try:
        d = request.get_json(force=True) or {}
        name = (d.get('name') or '').strip()
        mobile = (d.get('mobile') or '').strip()
        user_id = (d.get('userId') or '').strip()
        if not name:
            return fail('请填写成员姓名')
        if not mobile and not user_id:
            return fail('请填写手机号或钉钉 userId（至少一个）')
        new_id = db_execute_insert(
            'INSERT INTO seeding_push_users (name, mobile, user_id, enabled, remark) '
            'VALUES (%s, %s, %s, %s, %s)',
            [name, mobile, user_id, 1 if d.get('enabled', True) else 0,
             (d.get('remark') or '').strip()])
        return success({'id': new_id}, '已添加推送人')
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/push-users/<int:uid>', methods=['PUT'])
def seeding_push_user_update(uid):
    """修改种草推送人（姓名 / 手机号 / userId / 启用 / 备注）"""
    try:
        d = request.get_json(force=True) or {}
        sets, params = [], []
        if 'name' in d:
            name = (d.get('name') or '').strip()
            if not name:
                return fail('姓名不能为空')
            sets.append('name = %s')
            params.append(name)
        if 'mobile' in d:
            sets.append('mobile = %s')
            params.append((d.get('mobile') or '').strip())
        if 'userId' in d:
            sets.append('user_id = %s')
            params.append((d.get('userId') or '').strip())
        if 'enabled' in d:
            sets.append('enabled = %s')
            params.append(1 if d.get('enabled') else 0)
        if 'remark' in d:
            sets.append('remark = %s')
            params.append((d.get('remark') or '').strip())
        if not sets:
            return fail('没有需要更新的字段')
        params.append(uid)
        rows = db_execute('UPDATE seeding_push_users SET %s WHERE id = %%s' % ', '.join(sets),
                          params, fetch=False)
        if not rows:
            return fail('推送人不存在')
        return success(None, '已更新')
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/push-users/<int:uid>', methods=['DELETE'])
def seeding_push_user_delete(uid):
    """删除种草推送人"""
    try:
        db_execute('DELETE FROM seeding_push_users WHERE id = %s', [uid], fetch=False)
        return success(None, '已删除')
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/push-users/resolve', methods=['POST'])
def seeding_push_user_resolve():
    """手机号 → 钉钉 userId（只换取，不写库；写库由前端决定）"""
    try:
        d = request.get_json(force=True) or {}
        mobile = (d.get('mobile') or '').strip()
        if not mobile:
            return fail('请填写手机号')
        client = _push_client()
        user_id = client.get_userid_by_mobile(mobile)
        if not user_id:
            return fail('手机号未匹配到钉钉成员，请确认号码正确且在该应用可见范围内')
        return success({'userId': user_id})
    except DingTalkError as e:
        return fail(str(e))
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/dept-config', methods=['GET'])
def seeding_dept_config_get():
    """部门列表 + 推送规则 + 可选钉钉联系人（一次取全，前端弹窗直接用）

    ★ 候选人来自**种草专有**名单 seeding_push_users（与日报推送名单互不影响）。
    """
    try:
        depts = _seeding_dept_list()
        rules = _seeding_push_rules()
        merged = {}
        for d in depts:
            merged[d] = _seeding_norm_rule(rules.get(d))
        cands = []
        for u in (_seeding_push_users() or []):
            cands.append({
                'id': u.get('id'),
                'name': u.get('name') or '',
                'userId': (u.get('user_id') or '').strip(),
                'mobile': (u.get('mobile') or '').strip(),
                'enabled': 1 if u.get('enabled') in (1, '1', True) else 0,
            })
        return success({'departments': depts, 'rules': merged, 'candidates': cands})
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/dept-config', methods=['POST'])
def seeding_dept_config_save():
    """保存部门列表与推送规则（整份覆盖；前端一次提交，避免多次写文件互相踩）"""
    try:
        body = request.get_json(silent=True) or {}
        depts, seen = [], set()
        for d in (body.get('departments') or []):
            d = str(d or '').strip()
            if d and d not in seen:
                seen.add(d)
                depts.append(d)
        if not depts:
            return fail('至少保留一个部门')
        rules_in = body.get('rules') or {}
        rules = {}
        for d in depts:
            rules[d] = _seeding_norm_rule(rules_in.get(d))
        _seeding_cfg_save(_SEEDING_DEPT_FILE, {'departments': depts})
        _seeding_cfg_save(_SEEDING_PUSH_RULE_FILE, rules)
        return success({'departments': depts, 'rules': rules})
    except Exception as e:
        return fail(str(e))


def _seeding_all_works():
    """两个平台的当前作品（抖音 CSV + 小红书 JSON），用于点赞阈值判定。

    ★ 剔除用户手动删除（hidden）的作品：既然在页面上删掉了，就不该再被点赞推送捞出来。
    """
    out = []
    try:
        out.extend(_seeding_filter_hidden('douyin', _seeding_load_works_csv() or []))
    except Exception as e:
        print('[种草][点赞推送] 读抖音作品失败: %s' % e)
    try:
        out.extend(_seeding_filter_hidden('xhs', _seeding_load_xhs_works() or []))
    except Exception as e:
        print('[种草][点赞推送] 读小红书作品失败: %s' % e)
    return out


def _seeding_like_push_check(dry_run=False):
    """按「部门 → 点赞阈值 N」把达标作品推给该部门的钉钉联系人。

    规则
    ----
    · 每个部门各配一个 N（seeding_push_rules.json）；enabled=false 或没配联系人的跳过；
    · 作品归属部门：作品里的账号名 → seeding_accounts.json 的 department 字段；
    · ★ 同一作品**只推一次**：账本 seeding_push_log.json 按 _seeding_work_key 记账
      （该 key 已剔除链接里的 query，小红书 xsec_token 每轮变化不会造成重复推送）；
    · 一个部门本批多个达标作品合并成一条 markdown 发出，避免刷屏。

    返回 (推送作品条数, 说明)。
    """
    rules = _seeding_push_rules()
    active = {}
    for d, r in rules.items():
        nr = _seeding_norm_rule(r)
        if nr['enabled'] and nr['userId'] and nr['threshold'] > 0:
            active[d] = nr
    if not active:
        return 0, '没有已启用且配好联系人与阈值的部门'

    dept_of = {}
    for a in (_seeding_load_accounts() or []):
        nm = (a.get('name') or '').strip()
        if nm:
            dept_of[nm] = (a.get('department') or '').strip()

    log = _seeding_push_log()
    pushed = log['pushed']

    buckets = {}
    for w in _seeding_all_works():
        dept = dept_of.get((w.get('name') or '').strip(), '')
        rule = active.get(dept)
        if not rule:
            continue
        try:
            likes = int(w.get('likes') or 0)
        except Exception:
            likes = 0
        if likes < rule['threshold']:
            continue
        if _seeding_work_key(w) in pushed:
            continue
        buckets.setdefault(dept, []).append((likes, w))

    if not buckets:
        return 0, '没有新达标作品'
    if dry_run:
        cnt = sum(len(v) for v in buckets.values())
        detail = '; '.join('%s %d 条(阈值%d)' % (d, len(v), active[d]['threshold'])
                           for d, v in sorted(buckets.items()))
        return cnt, '[dry-run] %s' % detail

    try:
        client = _push_client()
    except DingTalkError as e:
        return 0, '钉钉不可用：%s' % e

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sent = 0
    for dept, items in sorted(buckets.items()):
        rule = active[dept]
        items.sort(key=lambda x: -x[0])
        lines = ['### 🔥 种草作品点赞达标 · %s' % dept, '',
                 '阈值 **%d** 赞，本批 **%d** 条：' % (rule['threshold'], len(items)), '']
        for likes, w in items[:20]:
            title = (w.get('title') or '(无标题)').strip()
            link = (w.get('link') or w.get('url') or '').strip()
            acct = (w.get('account') or w.get('name') or '').strip()
            lines.append('- [%s](%s) — %s · 点赞 **%s**'
                         % (title[:40], link, acct, format(likes, ',')))
        if len(items) > 20:
            lines.append('- ……另有 %d 条（详见种草监测中台）' % (len(items) - 20))
        lines += ['', '> %s' % now_str]
        try:
            client.send_markdown([rule['userId']],
                                 '🔥 种草点赞达标 · %s' % dept, '\n'.join(lines))
        except DingTalkError as e:
            print('[种草][点赞推送] %s 发送失败: %s' % (dept, e))
            continue
        for likes, w in items:
            pushed[_seeding_work_key(w)] = {
                'ts': int(time.time()), 'likes': likes,
                'dept': dept, 'title': (w.get('title') or '')[:60],
            }
        sent += len(items)
        print('[种草][点赞推送] %s → %s，已推送 %d 条'
              % (dept, rule['userName'] or rule['userId'], len(items)), flush=True)

    if sent:
        # 账本只留最近 5000 条，避免文件无限膨胀
        if len(pushed) > 5000:
            for k in sorted(pushed, key=lambda x: pushed[x].get('ts', 0))[:len(pushed) - 5000]:
                pushed.pop(k, None)
        _seeding_cfg_save(_SEEDING_PUSH_LOG_FILE, log)
    return sent, '已推送 %d 条' % sent


# ======================== 被删作品检测 ========================

def _seeding_normalize_link(link):
    """作品链接归一化：去掉 query 与 fragment，只保留 scheme://host/path。

    ★ 为什么必须做（2026-09-17 实测的误报根因）：
    小红书作品链接形如
        https://www.xiaohongshu.com/explore/<note_id>?xsec_token=<一次性令牌>
    这个 `xsec_token` **每次抓取都不一样**。若直接用整条 link 当作品唯一键，
    同一个作品在快照对比时会被判成「旧的没了 + 来个新的」→ 整批作品被误报为
    「已删除」（实测一次刷出 59 条假记录）。去掉 query 后 note_id 稳定，比较才成立。
    """
    s = (link or '').strip()
    if not s:
        return ''
    try:
        from urllib.parse import urlsplit
        p = urlsplit(s)
        if p.scheme and p.netloc:
            return '%s://%s%s' % (p.scheme, p.netloc, p.path.rstrip('/'))
    except Exception:
        pass
    return s.split('?')[0].split('#')[0].rstrip('/')


def _seeding_work_key(w):
    """作品唯一键：优先用「去掉一次性参数」的链接，无链接退回 账号+标题"""
    link = (w.get('link') or w.get('url') or '').strip()
    if link:
        return 'link:' + _seeding_normalize_link(link)
    return 't:' + (w.get('account') or '').strip() + '|' + (w.get('title') or '').strip()


def _seeding_load_state():
    """读取被删作品状态（快照 + 被删列表 + 手动删除名单 + 自增 id）；文件不存在或损坏时返回空状态

    hidden = 用户在页面上手动删掉的作品（{平台: [作品唯一键]}）。抓取脚本每轮重写数据文件，
    所以手动删除只能记在这里、读列表时过滤，见 seeding_list_works。
    """
    empty = {'snapshots': {}, 'deleted': [], 'hidden': {}, 'purged_accounts': [], '_seq': 0}
    if not os.path.exists(_SEEDING_STATE_FILE):
        return dict(empty)
    try:
        with open(_SEEDING_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(empty)
        data.setdefault('snapshots', {})
        data.setdefault('deleted', [])
        data.setdefault('hidden', {})
        # 已删除种草账号的标记（展示过滤 + 对账跳过兜底），见 _seeding_purge_account_works
        data.setdefault('purged_accounts', [])
        data.setdefault('_seq', 0)
        if not isinstance(data.get('hidden'), dict):
            data['hidden'] = {}
        if not isinstance(data.get('purged_accounts'), list):
            data['purged_accounts'] = []
        return data
    except Exception:
        return dict(empty)


def _seeding_filter_hidden(platform, works):
    """过滤掉用户手动删除的作品（hidden 名单）与已删除账号的作品（purged 名单）。

    ⚠ 只用于「展示」链路。对账 / 点赞推送若也用过滤后的列表，会把手动删除的作品
      误判成又被删了一次或被重新推送；所以那边要么用原始列表，要么单独过滤。
    """
    keys = set(_seeding_load_state().get('hidden', {}).get(platform) or [])
    out = works if not keys else [w for w in works if _seeding_work_key(w) not in keys]
    # 已删除的种草账号：它的作品可能被「删除时正在跑的那轮抓取」又写回文件，这里兜底过滤
    purged = _seeding_purged_work_check(platform)
    if purged:
        out = [w for w in out if not purged(w)]
    return out


def _seeding_save_state(state):
    try:
        with open(_SEEDING_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'[种草] 保存被删作品状态失败: {e}')


# ---- 种草账号 → 作品数据的归属判定 / 级联清理 ----
# 作品数据里没有账号主键（accounts 的 id），抓取脚本只写了「账号名 + 平台内账号号」，
# 所以「删除账号 → 清空它的作品」只能靠这两个值去作品里认领，详见 _seeding_purge_account_works。

def _seeding_account_platform(a):
    """账号所属平台：只有明确 platform=xhs 才算小红书，其余（含未填）按抖音 —— 与两个抓取脚本口径一致"""
    return 'xhs' if str((a or {}).get('platform') or '').strip().lower() == 'xhs' else 'douyin'


def _seeding_account_identity(a):
    """账号的作品归属标识 (平台, 平台内账号号, 账号名)。

    抓取脚本写进作品数据的正是这两个值：
      · 抖音   _douyin_works.csv ：名称 = 账号名，账号 = douyinId
      · 小红书 _xhs_works.json   ：name = 账号名，account = redId
    两个都带上：账号号兜住「账号名后来改过」的历史数据，账号名兜住老数据里没写账号号的行。
    """
    a = a or {}
    platform = _seeding_account_platform(a)
    code = str((a.get('redId') if platform == 'xhs' else a.get('douyinId')) or '').strip()
    return platform, code, str(a.get('name') or '').strip()


def _seeding_work_owned_by(work, code, name, survivor_names=None):
    """判断一条作品是否属于某个（正在被删的）种草账号。

    survivor_names = 同平台「没被删」的账号名集合：命中它说明这条作品还挂在留下的
    账号名下（同一账号配了多条 / 两个账号填了同一个抖音号），不能算给被删账号，
    否则会把还留着的账号的作品一起清掉。
    """
    wname = str((work or {}).get('name') or '').strip()
    wcode = str((work or {}).get('account') or '').strip()
    if wname and survivor_names and wname in survivor_names:
        return False
    if name and wname and wname == name:
        return True
    if code and wcode and wcode == code:
        return True
    return False


_SEEDING_PURGED_MAX = 200   # 已删账号标记上限（只用于兜底过滤，超了丢最老的）


def _seeding_purged_work_check(platform):
    """返回判定函数 work -> bool（该作品是否属于「已被删除的种草账号」）；无标记时返回 None。

    为什么还需要它：作品数据文件每轮都是整份重写的。若删除账号时刚好有一轮抓取在跑
    （它启动时读到的账号列表里还有这个账号），收尾重写会把它的作品又写回文件。
    展示过滤 + 对账跳过都用这个判定兜底，才不会「删了又复活」或冒出假的「被删作品」。
    """
    entries = [e for e in (_seeding_load_state().get('purged_accounts') or [])
               if isinstance(e, dict) and str(e.get('platform') or '') == platform]
    if not entries:
        return None
    survivors = set()
    for a in _seeding_load_accounts():
        if _seeding_account_platform(a) == platform:
            n = str(a.get('name') or '').strip()
            if n:
                survivors.add(n)
    pairs = [(str(e.get('code') or '').strip(), str(e.get('name') or '').strip()) for e in entries]

    def _hit(w):
        return any(_seeding_work_owned_by(w, c, n, survivors) for c, n in pairs)

    return _hit


def _seeding_purge_works_csv(owned):
    """从抖音作品 CSV 里物理删掉匹配的行，返回删除条数（文件不存在 / 出错都返回 0）。

    ★ 回写后用 os.utime 还原原来的 mtime：顶部「数据更新时间」取的就是文件 mtime，
      清理动作不该把它刷成「刚刚更新过」，否则会掩盖真正停更的平台。
    """
    import csv as _csv
    if not os.path.exists(_DOUYIN_WORKS_CSV):
        return 0
    try:
        st = os.stat(_DOUYIN_WORKS_CSV)
        with open(_DOUYIN_WORKS_CSV, 'r', encoding='utf-8-sig', newline='') as f:
            reader = _csv.DictReader(f)
            fnames = list(reader.fieldnames or [])
            rows = list(reader)
    except Exception as e:
        print('[种草] 读取抖音作品 CSV 失败: %s' % e)
        return 0
    kept, removed = [], 0
    for r in rows:
        if owned({'name': (r.get('名称') or '').strip(),
                  'account': (r.get('账号') or '').strip()}):
            removed += 1
        else:
            kept.append(r)
    if not removed:
        return 0
    try:
        with open(_DOUYIN_WORKS_CSV, 'w', encoding='utf-8-sig', newline='') as f:
            wr = _csv.DictWriter(f, fieldnames=fnames, extrasaction='ignore')
            wr.writeheader()
            wr.writerows(kept)
        os.utime(_DOUYIN_WORKS_CSV, (st.st_atime, st.st_mtime))
    except Exception as e:
        print('[种草] 回写抖音作品 CSV 失败: %s' % e)
        return 0
    return removed


def _seeding_purge_xhs_works(owned):
    """从小红书作品 JSON 里物理删掉匹配的作品，返回删除条数（顺带把 id 重排成 1..N）"""
    works = _seeding_load_xhs_works()
    if not works:
        return 0
    kept = [w for w in works if not owned(w)]
    removed = len(works) - len(kept)
    if not removed:
        return 0
    for i, w in enumerate(kept):
        w['id'] = i + 1
    try:
        st = os.stat(_XHS_WORKS_FILE)
        with open(_XHS_WORKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(kept, f, ensure_ascii=False, indent=2)
        os.utime(_XHS_WORKS_FILE, (st.st_atime, st.st_mtime))
    except Exception as e:
        print('[种草] 回写小红书作品失败: %s' % e)
        return 0
    return removed


def _seeding_purge_account_works(removed_accounts, kept_accounts):
    """删除种草账号时，把该账号的作品数据 / 被删作品记录一并清空。

    为什么必须做（而不是等下一轮抓取自然消失）：数据文件是整份重写的，在下一轮抓取
    之前（最长 30 分钟）页面照样显示它的作品；这期间一旦触发对账，这些作品还会被判成
    「被平台删除」写进被删作品列表 —— 账号都删了还不断冒它的记录，纯脏数据。
    所以一次做四件事：
      ① 数据文件里属于该账号的行 → 物理删除（页面立即清空这批作品）；
      ② 对账快照里的这些作品 → 剔除（后续对账不会误报「平台删除」）；
      ③ 被删作品列表里属于该账号的记录 → 删除；
      ④ 记一条 purged_accounts 标记 → 展示过滤 + 对账跳过兜底
         （挡住「删除时正在跑的那轮抓取把作品写回来」；账号重新添加时自动解除）。

    removed_accounts：被删的账号对象列表；kept_accounts：删完后剩下的账号列表
    （用来排除「同名/同账号号还挂在别的账号上」的作品，避免误伤）。
    返回 {平台: {'works': n, 'snapshots': n, 'deleted': n}}
    """
    state = _seeding_load_state()
    snapshots = state.get('snapshots') if isinstance(state.get('snapshots'), dict) else {}
    deleted = state.get('deleted') if isinstance(state.get('deleted'), list) else []
    purged = state.get('purged_accounts')
    if not isinstance(purged, list):
        purged = []

    by_platform, survivors = {}, {}
    for a in removed_accounts or []:
        platform, code, name = _seeding_account_identity(a)
        if not code and not name:
            continue
        by_platform.setdefault(platform, []).append((code, name))
    for a in kept_accounts or []:
        platform, _c, name = _seeding_account_identity(a)
        if name:
            survivors.setdefault(platform, set()).add(name)

    report = {}
    for platform, idents in by_platform.items():
        sur = survivors.get(platform) or set()

        def _owned(work, _idents=tuple(idents), _sur=sur):
            return any(_seeding_work_owned_by(work, c, n, _sur) for c, n in _idents)

        info = {'works': 0, 'snapshots': 0, 'deleted': 0}
        # ① 数据文件
        info['works'] = (_seeding_purge_xhs_works(_owned) if platform == 'xhs'
                         else _seeding_purge_works_csv(_owned))
        # ② 对账快照
        snap = snapshots.get(platform)
        if isinstance(snap, list):
            left = [w for w in snap if not _owned(w)]
            info['snapshots'] = len(snap) - len(left)
            snapshots[platform] = left
        # ③ 被删作品记录
        before = len(deleted)
        deleted = [d for d in deleted
                   if not (str((d or {}).get('platform') or '') == platform and _owned(d))]
        info['deleted'] = before - len(deleted)
        # ④ 已删账号标记（先按标识去重，再追加）
        for code, name in idents:
            purged = [e for e in purged
                      if not (isinstance(e, dict) and str(e.get('platform') or '') == platform
                              and str(e.get('code') or '').strip() == code
                              and str(e.get('name') or '').strip() == name)]
            purged.append({'platform': platform, 'code': code, 'name': name})
        report[platform] = info

    state['snapshots'] = snapshots
    state['deleted'] = deleted
    state['purged_accounts'] = purged[-_SEEDING_PURGED_MAX:]
    _seeding_save_state(state)
    return report


def _seeding_purge_counts(report):
    """(同步清空的作品数, 同步删除的被删作品记录数) —— 用于给用户看的提示文案"""
    return (sum(int(v.get('works') or 0) for v in (report or {}).values()),
            sum(int(v.get('deleted') or 0) for v in (report or {}).values()))


def _seeding_unpurge_account(a):
    """账号重新添加（或改回某个标识）时解除 purged 标记，让它的作品能正常回到列表"""
    platform, code, name = _seeding_account_identity(a)
    state = _seeding_load_state()
    purged = state.get('purged_accounts')
    if not isinstance(purged, list) or not purged:
        return
    left = []
    for e in purged:
        if not isinstance(e, dict):
            continue
        same = (str(e.get('platform') or '') == platform
                and str(e.get('code') or '').strip() == code
                and str(e.get('name') or '').strip() == name)
        if not same:
            left.append(e)
    if len(left) != len(purged):
        state['purged_accounts'] = left
        _seeding_save_state(state)


def _seeding_reconcile_guard(platform, current_works):
    """只在「本轮抓取完整」时才做被删作品对比，否则跳过。

    抓取失败 / 部分失败时数据文件天然缺内容，直接对比会把大批作品误判成
    「已删除」（2026-09-17 出现过 61 条误报）。返回 (是否已对比, 原因)。
    """
    if not current_works:
        return False, '本轮作品数为 0'
    p = _seeding_progress_read(platform)
    if p.get('status') != 'done':
        return False, '本轮抓取未成功（status=%s）' % p.get('status')
    accounts, ok = p.get('accounts'), p.get('ok')
    if accounts and ok is not None and ok < accounts:
        return False, '本轮 %d/%d 个账号成功，数据不完整' % (ok, accounts)
    _seeding_reconcile_deleted(platform, current_works)
    return True, ''


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

    # 已删除账号的作品不算「被平台删除」（账号都删了，不该再冒出它的被删记录），
    # 见 _seeding_purge_account_works ④。
    purged = _seeding_purged_work_check(platform)

    for w in prev:
        if purged and purged(w):
            continue
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
# 知识库（两套，生成时一起喂给模型）：
#   1. 已上传文案库（单列 `文案`）—— 历史种草文案样本，学风格、做去重
#   2. 已上传爆文库（`标题`/`爆文`）—— 用户「投喂爆文」上传的爆款文案，学爆款结构与钩子（严禁照抄）
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


def _seeding_load_hot_kb():
    """读取「已上传爆文库」，返回 [{title, content}]；表不存在或为空时返回 []"""
    try:
        rows = db_execute('SELECT `标题`, `爆文` FROM `已上传爆文库` '
                          'ORDER BY id DESC LIMIT %s', [_SEEDING_HOT_KB_LIMIT])
    except Exception as e:
        print(f'[种草] 读取爆文库失败: {e}')
        return []
    out = []
    for r in rows:
        t = (r.get('爆文') or '').strip()
        if t:
            out.append({'title': (r.get('标题') or '').strip(), 'content': t[:_SEEDING_HOT_ITEM_MAX]})
    return out


@app.route('/api/seeding/hot-articles', methods=['GET'])
def seeding_hot_articles_list():
    """爆文库列表（供「投喂爆文」弹窗展示已投喂内容）"""
    try:
        rows = db_execute('SELECT id, `标题`, `来源`, `创建时间`, CHAR_LENGTH(`爆文`) AS `字数` '
                          'FROM `已上传爆文库` ORDER BY id DESC LIMIT 200') or []
        items = [{
            'id': r.get('id'),
            'title': r.get('标题') or '',
            'source': r.get('来源') or '',
            'words': int(r.get('字数') or 0),
            'createdAt': str(r.get('创建时间') or ''),
        } for r in rows]
        return success({'count': len(items), 'items': items})
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/hot-articles', methods=['POST'])
def seeding_hot_article_create():
    """投喂爆文：把用户粘贴的爆款文案存进「已上传爆文库」"""
    try:
        d = request.get_json(force=True) or {}
        content = (d.get('content') or '').strip()
        if not content:
            return fail('请先粘贴爆文内容')
        if len(content) > 20000:
            content = content[:20000]
        title = (d.get('title') or '').strip()[:128]
        source = (d.get('source') or '').strip()[:64]
        new_id = db_execute_insert(
            'INSERT INTO `已上传爆文库` (`标题`, `爆文`, `来源`) VALUES (%s, %s, %s)',
            [title, content, source])
        return success({'id': new_id, 'words': len(content)},
                       '爆文已上传，智能体下次生成时就会参考它')
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/hot-articles/<int:aid>', methods=['DELETE'])
def seeding_hot_article_delete(aid):
    """删除一条爆文（投喂错了可以删）"""
    try:
        rows = db_execute('DELETE FROM `已上传爆文库` WHERE id = %s', [aid], fetch=False)
        if not rows:
            return fail('该爆文不存在')
        return success(None, '已删除该爆文')
    except Exception as e:
        return fail(str(e))


def _seeding_feedback_push(content, who, role):
    """把优化建议钉钉单聊发给开发人员；返回 (ok, msg)

    收件人沿用「开发告警」的口径（dev_alert 从 dingtalk_push_users 里找「李自豪」，
    查不到再回落 .env 的 DEV_ALERT_USER_ID / DEV_ALERT_MOBILE）——
    这里要的是"开发人员"，与种草推送名单是两回事。
    """
    try:
        row = _dev_recipient(force=True) if callable(_dev_recipient) else None
    except Exception as e:
        print(f'[种草][优化建议] 取开发人员失败: {e}')
        row = None
    if not row:
        return False, '未找到开发人员钉钉账号（请在「每日数据分析 → 钉钉推送」里维护）'
    try:
        client = _push_client()
    except DingTalkError as e:
        return False, '钉钉不可用：%s' % e
    uid, err = _push_resolve_userid(client, row)
    if err or not uid:
        return False, '解析开发人员 userId 失败：%s' % (err or '空')
    md = '\n'.join([
        '### 💡 种草智能体 · 优化建议',
        '',
        '- **提交人**：%s' % (who or '未署名'),
        '- **角色**：%s' % (role or '-'),
        '- **时间**：%s' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        '',
        '**建议内容**',
        '',
        content,
    ])
    try:
        r = client.send_markdown([uid], '💡 种草智能体优化建议', md)
        if (r or {}).get('invalidStaffIdList'):
            return False, '开发人员不在该钉钉应用的可见范围内'
    except DingTalkError as e:
        return False, str(e)
    return True, '已发送给开发人员'


@app.route('/api/seeding/feedback', methods=['POST'])
def seeding_feedback():
    """种草智能体「优化建议」：落库 + 直接钉钉单聊发给开发人员"""
    try:
        d = request.get_json(force=True) or {}
        content = (d.get('content') or '').strip()
        if not content:
            return fail('请输入优化建议内容')
        if len(content) > _SEEDING_FB_MAX:
            content = content[:_SEEDING_FB_MAX]
        who = (d.get('userName') or '').strip()[:64]
        role = (d.get('role') or '').strip()[:64]
        fb_id = None
        try:
            fb_id = db_execute_insert(
                'INSERT INTO `种草优化建议` (`内容`, `提交人`, `角色`, `状态`) VALUES (%s, %s, %s, %s)',
                [content, who, role, '待发送'])
        except Exception as e:
            # 落库失败不阻塞发送：建议本身比留痕更重要
            print(f'[种草][优化建议] 落库失败: {e}')
        ok, msg = _seeding_feedback_push(content, who, role)
        try:
            if fb_id:
                db_execute('UPDATE `种草优化建议` SET `状态` = %s WHERE id = %s',
                           ['已发送' if ok else ('发送失败：' + msg)[:32], fb_id], fetch=False)
        except Exception:
            pass
        if ok:
            return success({'sent': True}, msg)
        # 已落库但没发出去：如实告诉用户，别让人以为白写了
        print(f'[种草][优化建议] {msg}')
        return fail('建议已记录，但%s' % msg)
    except Exception as e:
        return fail(str(e))


@app.route('/api/seeding/agent', methods=['POST'])
def seeding_agent():
    """种草智能体：检索「已上传文案库」+「爆文库」+「种草君」系统提示词生成种草文案"""
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

        hots = _seeding_load_hot_kb()
        if hots:
            hot_block = '\n\n'.join(
                '【爆文 %d%s】\n%s' % (i + 1, ('·' + h['title']) if h.get('title') else '', h['content'])
                for i, h in enumerate(hots))
        else:
            hot_block = '（当前爆文库为空，暂无爆款参考；可让用户在「投喂爆文」里上传）'

        sys_p = _load_seeding_agent_prompt()
        user_msg = (f'已上传文案库（用于学习风格与去重，若为空则忽略）：\n{kb_block}\n\n'
                    f'爆文库（已认可的爆款文案，用于学习爆款结构/开头钩子/节奏，'
                    f'严禁照抄句子，若为空则忽略）：\n{hot_block}\n\n'
                    f'用户需求：{question}\n\n'
                    f'（直接输出最终文案，不要输出任何风格学习、知识库分析、去重说明、切入角度差异等过程性文字）')

        raw = call_deepseek_api(sys_p, user_msg, temperature=0.8, max_tokens=4096, model=_SEEDING_AGENT_MODEL)

        return success({
            'analysis': raw or '',
            'meta': {
                'kb_count': len(samples),
                'hot_count': len(hots),
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


@app.route('/uploads/avatar/<path:filename>')
def serve_avatar(filename):
    """托管头像文件（无需登录态，路径带随机串）"""
    return send_from_directory(AVATAR_DIR, filename)


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


# ======================== 选品助手智能体 ========================
# 流程：
#   每日 07:00 抓取并清洗抖音热点宝 → 写入「抖音热搜品类表」。
#   → V2 按四个价格带从热搜和天猫榜单生成 70 个候选品。
#   → 低速采集爱搜数据，按需求/利润/竞争/售后/内容五维评分。
#   → 输出 7 个原品、每品 3 个关联裂变品和选品建议，并按日期存档。
_DOUYIN_HOT_FILE = os.path.join(_SEEDING_DIR, '_douyin_hot.json')
_DOUYIN_HOT_PROGRESS = os.path.join(_SEEDING_DIR, '_douyin_hot_progress.json')
_DOUYIN_HOT_SCRAPER = os.path.join(_SEEDING_DIR, 'douyin_hot_scraper.py')

_AISOU_INPUT_FILE = os.path.join(_SEEDING_DIR, '_aisou_input.json')
_AISOU_OUTPUT_FILE = os.path.join(_SEEDING_DIR, '_aisou_output.json')
_AISOU_SCRAPER = os.path.join(_SEEDING_DIR, 'aisou_scraper.py')

# ======================== 表结构保障 ========================

def _ensure_aisou_columns():
    """「爱搜数据表」结构保障（窄表：一行一个词）。

    历史遗留：该表原本是宽表——主键 (日期, 电商词关键词, 下拉词关键词, 相关词关联词)，
    且下拉词/相关词的月覆盖人次、七日搜索人次都是 NOT NULL 无默认值，
    智能体只插业务字段会直接报 1364。2026-09-17 已把线上表改成窄表，
    这里保留一段自愈逻辑，保证其它库（内网库等）第一次跑也不会挂。
    """
    info = {r['Field']: r for r in db_execute("SHOW COLUMNS FROM `爱搜数据表`")}
    specs = {
        '来源词': "VARCHAR(255) NOT NULL DEFAULT ''",
        '词类型': "VARCHAR(20) NOT NULL DEFAULT ''",
        '词名称': "VARCHAR(255) NOT NULL DEFAULT ''",
        '月覆盖人次': "VARCHAR(50) NULL",
        '七日搜索人次': "VARCHAR(50) NULL",
    }
    for col, ddl in specs.items():
        if col not in info:
            db_execute(f"ALTER TABLE `爱搜数据表` ADD COLUMN `{col}` {ddl}", fetch=False)
    # 遗留宽表列（NOT NULL 且无默认值）放宽为 NULL，避免插入时报 1364
    legacy = ('搜索词关键词', '搜索词月覆盖人次', '搜索词七日搜索人次',
              '电商词关键词', '电商词月覆盖人次', '电商词七日搜索人次',
              '下拉词关键词', '下拉词月覆盖人次', '下拉词七日搜索人次',
              '相关词关联词', '相关词月覆盖人次', '相关词七日搜索人次')
    for col in legacy:
        r = info.get(col)
        if r and r.get('Null') == 'NO' and r.get('Key') != 'PRI' and r.get('Default') is None:
            try:
                db_execute(f"ALTER TABLE `爱搜数据表` MODIFY COLUMN `{col}` VARCHAR(255) NULL", fetch=False)
            except Exception:
                pass
    # 主键对齐为 (日期, 来源词, 词类型, 词名称)：空表自动迁移，有数据则打日志提示人工处理
    idx = sorted((r for r in db_execute("SHOW INDEX FROM `爱搜数据表`") if r.get('Key_name') == 'PRIMARY'),
                 key=lambda r: r.get('Seq_in_index') or 0)
    pk = [r['Column_name'] for r in idx]
    if pk != ['日期', '来源词', '词类型', '词名称']:
        cnt_row = (db_execute("SELECT COUNT(*) AS c FROM `爱搜数据表`") or [{}])[0]
        if (cnt_row.get('c') or 0) > 0:
            print('[爱搜] 警告：爱搜数据表主键仍是旧结构且表内有数据，需人工迁移')
        else:
            try:
                for col, typ in (('来源词', 'VARCHAR(255)'), ('词类型', 'VARCHAR(20)'),
                                 ('词名称', 'VARCHAR(255)')):
                    db_execute(
                        f"ALTER TABLE `爱搜数据表` MODIFY COLUMN `{col}` {typ} NOT NULL DEFAULT ''",
                        fetch=False)
                if pk:
                    db_execute("ALTER TABLE `爱搜数据表` DROP PRIMARY KEY", fetch=False)
                db_execute(
                    "ALTER TABLE `爱搜数据表` ADD PRIMARY KEY (`日期`, `来源词`, `词类型`, `词名称`)",
                    fetch=False)
            except Exception as e:
                print('[爱搜] 主键自动迁移失败：', e)


def _ensure_selection_record_table():
    """选品记录表：每次选品运行存一行（id 自增），供前端「历史选品记录」按运行翻页回看"""
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
    """同步运行一个 Python 脚本（用于爬虫），成功返回 True

    ★ 退出码也算失败（2026-09-17 修正）：此前只看有没有抛异常，
    脚本以非 0 退出（Cookie 失效、登录失败等）会被当成成功静默略过，故障无人知晓。
    现在「非 0 退出码」= 失败 → 钉钉告警开发人员。
    子进程 stdout/stderr 仍直接写进服务日志（不 capture），排查细节看 journalctl -u ecom。
    """
    import sys as _sys
    name = os.path.basename(script_path)
    try:
        proc = subprocess.run([_sys.executable, script_path], check=False, timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        print(f'[选品] 脚本超时 {script_path}: {timeout}s')
        _dev_alert('脚本执行超时（%ds）：%s' % (timeout, name),
                   detail='脚本：%s\n详见 journalctl -u ecom' % script_path,
                   signature='script:timeout:%s' % name, source='脚本调度')
        return False
    except Exception as e:
        print(f'[选品] 脚本运行异常 {script_path}: {e}')
        _dev_alert_exc('脚本启动失败', e, extra='脚本：%s' % script_path,
                       signature='script:spawn:%s' % name, source='脚本调度')
        return False
    if code != 0:
        print(f'[选品] 脚本非零退出 {script_path}: code={code}')
        _dev_alert('脚本失败（退出码 %d）：%s' % (code, name),
                   detail='脚本：%s\n详见 journalctl -u ecom' % script_path,
                   signature='script:exit:%s:%s' % (name, code), source='脚本调度')
        return False
    return True


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


# 天猫榜单：独立于淘宝关键词搜索。每周一 00:00（中国标准时间）采集榜单页，
# 排除指定三类，数据库仅保留最近四个采集周。
_TMALL_RANKLIST_SCRIPT = os.path.join(_SEEDING_DIR, 'tmall_ranklist_scraper.py')
_TMALL_RANKLIST_OUTPUT = os.path.join(_SEEDING_DIR, '_tmall_ranklist_output.json')
_TMALL_RANKLIST_PROGRESS = os.path.join(_SEEDING_DIR, '_tmall_ranklist_progress.json')
_TMALL_RANKLIST_JOB = os.path.join(_SEEDING_DIR, '_tmall_ranklist_job.json')
_TMALL_RANKLIST_TZ = ZoneInfo('Asia/Shanghai')
_TMALL_RANKLIST_EXCLUDED = ('天猫进口', '食品生鲜', '医药健康')
_TMALL_RANKLIST_KEEP_WEEKS = 4
_tmall_ranklist_lock = Lock()
_tmall_ranklist_job_thread = None


# ======================== 天猫榜单：采集、入库、四周轮换 ========================

def _tmall_ranklist_now():
    """调度和日期落库统一使用中国标准时间，不依赖服务器的系统时区。"""
    return datetime.now(_TMALL_RANKLIST_TZ)


def _tmall_ranklist_write_job(status, message, **extra):
    data = {
        'status': status,
        'message': message,
        'updatedAt': _tmall_ranklist_now().strftime('%Y-%m-%d %H:%M:%S'),
        'excludedCategories': list(_TMALL_RANKLIST_EXCLUDED),
        'keepWeeks': _TMALL_RANKLIST_KEEP_WEEKS,
    }
    data.update(extra)
    _write_json(_TMALL_RANKLIST_JOB, data)


def _tmall_ranklist_ensure_table():
    """确保榜单快照可按周共存；旧主键会覆盖历史日期，因此只做一次无损主键迁移。"""
    db_execute("""
        CREATE TABLE IF NOT EXISTS `天猫榜单表` (
            `类别名` VARCHAR(255) NOT NULL,
            `排行榜名` VARCHAR(255) NOT NULL,
            `产品名` VARCHAR(255) NOT NULL,
            `价格` FLOAT(8,2) NOT NULL,
            `日期` DATE NOT NULL,
            PRIMARY KEY (`类别名`, `排行榜名`, `产品名`, `日期`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """, fetch=False)
    fields = {r.get('Field') for r in db_execute("SHOW COLUMNS FROM `天猫榜单表`")}
    required = {'类别名', '排行榜名', '产品名', '价格', '日期'}
    missing = required - fields
    if missing:
        raise RuntimeError('天猫榜单表缺少字段：%s' % '、'.join(sorted(missing)))
    primary_rows = sorted((r for r in db_execute("SHOW INDEX FROM `天猫榜单表`")
                           if r.get('Key_name') == 'PRIMARY'),
                          key=lambda r: r.get('Seq_in_index') or 0)
    primary = [r.get('Column_name') for r in primary_rows]
    expected = ['类别名', '排行榜名', '产品名', '日期']
    if primary != expected:
        # 旧表主键是「排行榜名 + 产品名」，会令下一周覆盖上一周的快照；
        # 只改索引，不删改任何既有行。
        db_execute("ALTER TABLE `天猫榜单表` DROP PRIMARY KEY, "
                   "ADD PRIMARY KEY (`类别名`, `排行榜名`, `产品名`, `日期`)", fetch=False)
        print('[天猫榜单] 已将主键迁移为 类别名+排行榜名+产品名+日期（支持四周快照）')


def _tmall_ranklist_normalize_rows(raw_rows, capture_date):
    """校验爬虫输出；任何缺字段或异常价格都会拒绝整批写入。"""
    if not isinstance(raw_rows, list):
        raise RuntimeError('天猫榜单爬虫输出格式异常：rows 不是数组')
    rows, seen = [], set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise RuntimeError('天猫榜单爬虫输出包含非对象行')
        category = str(raw.get('类别名') or '').strip()
        rank_name = str(raw.get('排行榜名') or '').strip()
        product_name = str(raw.get('产品名') or '').strip()
        if not category or not rank_name or not product_name:
            raise RuntimeError('天猫榜单爬虫输出缺少类别名、排行榜名或产品名')
        if category in _TMALL_RANKLIST_EXCLUDED:
            raise RuntimeError('爬虫输出含排除类别：%s' % category)
        try:
            price = float(raw.get('价格'))
        except (TypeError, ValueError):
            raise RuntimeError('天猫榜单产品「%s」价格无效' % product_name)
        if price < 0:
            raise RuntimeError('天猫榜单产品「%s」价格不能为负数' % product_name)
        key = (category, rank_name, product_name, capture_date)
        if key in seen:
            continue
        seen.add(key)
        rows.append((category, rank_name, product_name, price, capture_date))
    if not rows:
        raise RuntimeError('天猫榜单爬虫没有返回可入库的数据')
    return rows


def _tmall_ranklist_upsert(rows):
    """单连接批量写入，避免每行独立事务造成四位数 SQL 往返。"""
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO `天猫榜单表` (`类别名`, `排行榜名`, `产品名`, `价格`, `日期`) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE `价格` = VALUES(`价格`)",
                rows)
        conn.commit()
        return_db(conn)
        return len(rows)
    except Exception:
        if conn:
            discard_db(conn)
        raise


def _tmall_ranklist_prune_weeks():
    """保留最近四个采集日期；第五次成功写入后才删除最早一批。"""
    date_rows = list(db_execute("SELECT DISTINCT `日期` AS d FROM `天猫榜单表` "
                                "ORDER BY `日期` DESC"))
    keep_dates = [r.get('d') for r in date_rows[:_TMALL_RANKLIST_KEEP_WEEKS]]
    if len(date_rows) <= _TMALL_RANKLIST_KEEP_WEEKS:
        return 0, [str(d) for d in keep_dates]
    placeholders = ', '.join(['%s'] * len(keep_dates))
    removed = db_execute("DELETE FROM `天猫榜单表` WHERE `日期` NOT IN (%s)" % placeholders,
                         keep_dates, fetch=False)
    return int(removed or 0), [str(d) for d in keep_dates]


def _tmall_ranklist_db_summary():
    try:
        rows = list(db_execute("SELECT `日期` AS d, COUNT(*) AS c FROM `天猫榜单表` "
                               "GROUP BY `日期` ORDER BY `日期` DESC LIMIT %s",
                               [_TMALL_RANKLIST_KEEP_WEEKS]))
        return [{'date': str(r.get('d')), 'rows': int(r.get('c') or 0)} for r in rows]
    except Exception:
        return []


def _tmall_ranklist_next_run(now=None):
    now = now or _tmall_ranklist_now()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_until_monday = (7 - now.weekday()) % 7
    target = today_midnight + timedelta(days=days_until_monday)
    if target <= now:
        target += timedelta(days=7)
    return target


def _tmall_ranklist_run(capture_date=None, trigger='manual'):
    """运行独立爬虫；仅当其完整成功后才开始数据库事务。"""
    import sys as _sys
    capture_date = capture_date or _tmall_ranklist_now().strftime('%Y-%m-%d')
    _tmall_ranklist_write_job('running', '正在启动天猫榜单采集', date=capture_date, trigger=trigger)
    try:
        env = os.environ.copy()
        env['TMALL_RANKLIST_DATE'] = capture_date
        proc = subprocess.run([_sys.executable, _TMALL_RANKLIST_SCRIPT], check=False,
                              timeout=3600, env=env)
        result = _read_json(_TMALL_RANKLIST_OUTPUT, {}) or {}
        if proc.returncode != 0 or result.get('status') != 'done':
            raise RuntimeError(result.get('message') or '天猫榜单爬虫异常退出（code=%s）' % proc.returncode)
        if str(result.get('date') or '') != capture_date:
            raise RuntimeError('天猫榜单输出日期不一致，拒绝入库')
        _tmall_ranklist_write_job('running', '抓取完成，正在校验并入库', date=capture_date,
                                  trigger=trigger, crawledRows=int(result.get('rowCount') or 0))
        _tmall_ranklist_ensure_table()
        rows = _tmall_ranklist_normalize_rows(result.get('rows'), capture_date)
        stored = _tmall_ranklist_upsert(rows)
        removed, kept_dates = _tmall_ranklist_prune_weeks()
        _tmall_ranklist_write_job('done', '已入库 %d 条；保留最近 %d 周%s' %
                                  (stored, _TMALL_RANKLIST_KEEP_WEEKS,
                                   ('，清理 %d 条旧数据' % removed) if removed else ''),
                                  date=capture_date, trigger=trigger, crawledRows=int(result.get('rowCount') or 0),
                                  storedRows=stored, prunedRows=removed, keptDates=kept_dates)
        print('[天猫榜单] %s：入库 %d 条，清理 %d 条旧数据' % (capture_date, stored, removed))
    except subprocess.TimeoutExpired:
        message = '天猫榜单采集超过 60 分钟，已终止且未入库'
        _tmall_ranklist_write_job('error', message, date=capture_date, trigger=trigger)
        _dev_alert(message, signature='tmall-ranklist:timeout', source='天猫榜单')
    except Exception as e:
        traceback.print_exc()
        message = '天猫榜单采集失败：' + str(e)
        _tmall_ranklist_write_job('error', message, date=capture_date, trigger=trigger)
        _dev_alert_exc('天猫榜单采集失败', e, signature='tmall-ranklist:%s' % capture_date,
                       source='天猫榜单')


def _start_tmall_ranklist(capture_date=None, trigger='manual'):
    global _tmall_ranklist_job_thread
    with _tmall_ranklist_lock:
        if _tmall_ranklist_job_thread and _tmall_ranklist_job_thread.is_alive():
            return False
        _tmall_ranklist_job_thread = threading.Thread(
            target=_tmall_ranklist_run, args=(capture_date, trigger), daemon=True,
            name='tmall-ranklist-%s' % (capture_date or 'current'))
        _tmall_ranklist_job_thread.start()
        return True


def _tmall_ranklist_auto_loop():
    """中国标准时间每周一 00:00 触发一次；进程内失败不高频重试，避免触发平台风控。"""
    attempted_dates = set()
    time.sleep(30)
    while True:
        try:
            now = _tmall_ranklist_now()
            date_str = now.strftime('%Y-%m-%d')
            if now.weekday() == 0 and date_str not in attempted_dates:
                attempted_dates.add(date_str)
                if _start_tmall_ranklist(date_str, trigger='schedule'):
                    print('[天猫榜单][定时] 已触发周一 00:00 采集：%s' % date_str)
        except Exception as e:
            print('[天猫榜单][定时] 异常：%s' % e)
            _dev_alert_exc('天猫榜单定时任务异常', e, signature='tmall-ranklist:loop', source='天猫榜单')
        time.sleep(30)


def _write_douyin_hot_progress(status, msg='', extra=None):
    data = {'status': status, 'message': msg}
    if extra:
        data.update(extra)
    _write_json(_DOUYIN_HOT_PROGRESS, data)


# ======================== 抖音热点宝：Cookie ========================

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
    return {'total': len(words), 'matched': len(matched), 'date': date_str, 'terms': matched}


_DOUYIN_HOT_AI_MAX_WORDS = 1000  # 送 DeepSeek 的词数上限（按热度截断），控制输入 token


def _douyin_hot_ai_filter(filter_result):
    """DeepSeek 二次过滤：只剔除非电商词，不再改写产品名称。"""
    terms = (filter_result or {}).get('terms') or []
    date_str = (filter_result or {}).get('date') or time.strftime('%Y-%m-%d')
    if not terms:
        return {'kept': 0, 'removed': 0, 'skipped': True}
    if not DEEPSEEK_SELECTION_API_KEY:
        print('[选品][AI筛选] 未配置 DEEPSEEK_SELECTION_API_KEY，跳过二次过滤（保留词典结果）')
        return {'kept': len(terms), 'removed': 0, 'skipped': True}

    # 只返回少数需要剔除的词，避免模型在这一阶段改写后续候选商品名。
    terms_sorted = sorted(terms, key=lambda m: m.get('heat_num') or 0, reverse=True)
    if len(terms_sorted) > _DOUYIN_HOT_AI_MAX_WORDS:
        terms_sorted = terms_sorted[:_DOUYIN_HOT_AI_MAX_WORDS]
    word_list = [{'word': m['term'], 'category': m.get('categories') or ''} for m in terms_sorted]
    sys_p = (
        '你是电商选品数据清洗助手。给你一批已通过词典初筛的抖音热搜词，只挑出不是电商购物意图的词。'
        '影视综艺、明星八卦、游戏、社会新闻、品牌事件、行情和抽象梗应剔除；拿不准时宁可保留。'
        '不要生成、规范化或改写任何产品名。只返回要剔除的原词，JSON 字符串数组格式：'
        '["词1","词2"]，不要任何其他文字。'
    )
    raw = call_deepseek_api(sys_p, '热搜词列表：\n' + json.dumps(word_list, ensure_ascii=False),
                            temperature=0.1, max_tokens=4000,
                            api_key=DEEPSEEK_SELECTION_API_KEY)
    if not raw:
        return {'kept': len(terms_sorted), 'removed': 0, 'skipped': True}
    removed_words = set()
    text = re.sub(r'```(?:json)?', '', str(raw)).strip()
    i, j = text.find('['), text.rfind(']')
    if i != -1 and j > i:
        try:
            removed_words = {str(x).strip() for x in json.loads(text[i:j + 1]) if str(x).strip()}
        except Exception:
            removed_words = set()
    allowed = {m['term'] for m in terms_sorted}
    removed_words &= allowed
    for word in removed_words:
        db_execute("DELETE FROM `抖音热搜品类表` WHERE `日期` = %s AND `热搜名` = %s",
                   [date_str, word], fetch=False)
    kept = len(terms_sorted) - len(removed_words)
    print('[选品][AI筛选] 词典 %d 词 -> 保留 %d，剔除 %d' %
          (len(terms_sorted), kept, len(removed_words)))
    return {'kept': kept, 'removed': len(removed_words), 'skipped': False}


def _run_scrape_and_filter():
    """抓取热点宝 → 品类筛选 → 启动当日分层选品。手动触发与每日 7 点定时共用。"""
    try:
        _write_douyin_hot_progress('scraping', '正在抓取抖音热点宝热搜...')
        ok = _run_py_script(_DOUYIN_HOT_SCRAPER, timeout=900)
        if not ok:
            _write_douyin_hot_progress('error', '热点宝抓取脚本运行失败')
            return
        _write_douyin_hot_progress('filtering', '正在筛选电商品类...')
        result = _run_douyin_hot_filter()
        _write_douyin_hot_progress('ai_filtering', 'DeepSeek 二次过滤中...')
        ai = _douyin_hot_ai_filter(result)
        _write_douyin_hot_progress('done', '抓取并筛选完成',
                                   {'total': result.get('total'), 'matched': result.get('matched'),
                                    'ai_kept': ai.get('kept'), 'ai_removed': ai.get('removed'),
                                    'date': result.get('date')})
        # V2 同时支持「热搜关联」和「仅用天猫榜单补位」：即使当天没有命中热搜词，
        # 也要运行，才能按四个价格带生成完整候选池。
        _start_selection_v2(result.get('date'))
    except Exception as e:
        traceback.print_exc()
        _write_douyin_hot_progress('error', '抓取筛选异常: ' + str(e))


_selection_scrape_thread = None


def _ps_latest_douyin_categories():
    """读取最新一天二次筛选后的原始抖音热搜词，不在这里改写产品名。"""
    rows = list(db_execute(
        "SELECT 热搜名, 热搜值, 品类, 日期 FROM `抖音热搜品类表` "
        "WHERE `日期` = (SELECT MAX(`日期`) FROM `抖音热搜品类表`) "
        "ORDER BY `热度数值` DESC LIMIT 500"))
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


def _aisou_enrich(products):
    """对给定商品名跑爱搜采集，结果写入「爱搜数据表」"""
    _ensure_aisou_columns()
    _write_json(_AISOU_INPUT_FILE, {'keywords': products})
    # 70 个候选品按低速顺序采集，给出 65 分钟窗口，满足 45~60 分钟目标并保留故障余量。
    if not _run_py_script(_AISOU_SCRAPER, timeout=3900):
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
            # 主键 = (日期, 来源词, 词类型, 词名称)；同一来源词下爱搜可能返回重复词，
            # 用 ON DUPLICATE KEY UPDATE 保证重复抓取幂等（否则报 1062）
            db_execute(
                "INSERT INTO `爱搜数据表` (`日期`, `来源词`, `词类型`, `词名称`, `月覆盖人次`, `七日搜索人次`) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE `月覆盖人次` = VALUES(`月覆盖人次`), "
                "`七日搜索人次` = VALUES(`七日搜索人次`)",
                [today, src, wtype, name, w.get('month') or '', w.get('seven') or ''], fetch=False)
            inserted += 1
    return inserted


# ======================== 选品助手 V2：每日分层选品看板 ========================
#
# 每天自动产出一份完整的 70 品分层候选、评分、7 个原品和 21 个裂变品，
# 并以 version 标识写入选品记录表。

_SELECTION_V2_BANDS = [
    {'key': 'volume', 'name': '走量款', 'min': 29, 'max': 89, 'quota': 10, 'final_quota': 1,
     'decision': '货比三家，看评价和主图', 'audience': '大多数中小卖家的主力盘，广告能扛得住',
     'accent': 'mint'},
    {'key': 'profit', 'name': '利润款', 'min': 90, 'max': 199, 'quota': 30, 'final_quota': 3,
     'decision': '需要被说服，详情页定生死', 'audience': '有品牌叙事或差异化卖点的人',
     'accent': 'violet'},
    {'key': 'trust', 'name': '信任款', 'min': 200, 'max': 499, 'quota': 20, 'final_quota': 2,
     'decision': '看资质、客服、售后承诺', 'audience': '有工厂背书、认证齐全、能做售后兜底',
     'accent': 'peach'},
    {'key': 'image', 'name': '形象款', 'min': 500, 'max': 1199, 'quota': 10, 'final_quota': 1,
     'decision': '长决策周期，复购低但客单高', 'audience': '品牌方、专业类目、中高端市场',
     'accent': 'rose'},
]
_SELECTION_V2_JOB_FILE = os.path.join(_SEEDING_DIR, '_selection_v2_job.json')
_selection_v2_job_thread = None


def _selection_v2_key(name):
    """用于跨价格带去重的宽松商品键，避免同一品仅凭规格词重复入选。"""
    s = re.sub(r'[\s\-_/（）()【】\[\]·]', '', str(name or '').lower())
    s = re.sub(r'(pro|max|plus|升级版|大容量|小号|中号|大号|\d+(?:ml|l|g|kg|寸|件|只|个))$', '', s)
    return s[:80]


def _selection_v2_parse_object(raw):
    if not raw:
        return None
    s = re.sub(r'```(?:json)?', '', str(raw)).strip()
    i, j = s.find('{'), s.rfind('}')
    if i == -1 or j <= i:
        return None
    try:
        obj = json.loads(s[i:j + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _selection_v2_price_label(lo, hi):
    return '¥%s-%s' % (int(lo) if float(lo).is_integer() else lo,
                         int(hi) if float(hi).is_integer() else hi)


def _selection_v2_market_price_label(price):
    return '市场均价 ¥%s' % (int(price) if float(price).is_integer() else ('%.2f' % price))


def _selection_v2_market_from_rows(rows, display_name, match_source, fallback_category=''):
    """同类商品去重后计算市场均价；display_name 原样返回，不参与改名。"""
    prices, categories = {}, {}
    for row in rows:
        raw_name = str(row.get('产品名') or '').strip()
        raw_key = _selection_v2_key(raw_name)
        if not raw_key or raw_key in prices:
            continue
        try:
            price = float(row.get('价格'))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        prices[raw_key] = price
        category = str(row.get('类别名') or '').strip()
        if category:
            categories[category] = categories.get(category, 0) + 1
    if not prices:
        return None
    avg_price = round(sum(prices.values()) / len(prices), 2)
    category = max(categories, key=categories.get) if categories else fallback_category
    return {'name': display_name, 'category': category, 'market_avg_price': avg_price,
            'sample_count': len(prices), 'match_source': match_source}


def _selection_v2_tmall_market(search_term, display_name=None):
    """用内部同类词搜索价格样本，但最终产品名保持候选生成阶段的原名称。"""
    query = str(search_term or '').strip()
    display_name = str(display_name or search_term or '').strip()
    if not query or not display_name:
        return None
    like = '%' + query + '%'
    rows = list(db_execute(
        "SELECT 类别名, 排行榜名, 产品名, 价格, 日期 FROM `天猫榜单表` "
        "WHERE `排行榜名` LIKE %s AND `价格` > 0 ORDER BY `日期` DESC", [like]))
    match_source = '排行榜名'
    if not rows:
        rows = list(db_execute(
            "SELECT 类别名, 排行榜名, 产品名, 价格, 日期 FROM `天猫榜单表` "
            "WHERE `产品名` LIKE %s AND `价格` > 0 ORDER BY `日期` DESC", [like]))
        match_source = '产品名'
    return _selection_v2_market_from_rows(rows, display_name, match_source) if rows else None


def _selection_v2_tmall_pool(band):
    """恢复原候选名来源：把该单品价格段里的天猫原始商品交给模型选名。"""
    rows = list(db_execute(
        "SELECT 类别名, 排行榜名, 产品名, 价格 FROM `天猫榜单表` "
        "WHERE `价格` >= %s AND `价格` <= %s ORDER BY `日期` DESC, `价格` ASC LIMIT 800",
        [band['min'], band['max']]))
    out, seen = [], set()
    for row in rows:
        name = str(row.get('产品名') or '').strip()
        key = _selection_v2_key(name)
        if not name or not key or key in seen:
            continue
        seen.add(key)
        out.append({'name': name, 'category': str(row.get('类别名') or ''),
                    'rank_name': str(row.get('排行榜名') or ''),
                    'price': float(row.get('价格') or 0)})
    return out


def _selection_v2_candidates():
    """恢复原产品名生成逻辑；只用同类市场均价重新决定所属价格段。"""
    hot = _ps_latest_douyin_categories()[:150]
    pools = {band['key']: _selection_v2_tmall_pool(band) for band in _SELECTION_V2_BANDS}
    bands_ctx = []
    for band in _SELECTION_V2_BANDS:
        bands_ctx.append({'key': band['key'], 'name': band['name'],
                          'price_range': _selection_v2_price_label(band['min'], band['max']),
                          'quota': band['quota'], 'decision': band['decision'], 'audience': band['audience'],
                          'tmall_products': pools[band['key']][:180]})
    prompt = (
        '你是严谨的电商选品研究员。沿用原有候选商品命名方式：根据抖音热搜和天猫商品生成具体可购买单品名称，'
        'name 是最终展示并送爱搜的产品名，不要把 name 改成价格查询词；不可为品牌词或过宽大类，不能用颜色、规格、Pro/Plus 制造重复。'
        '每组按 quota 返回。另给每个商品一个 market_query，仅供后台搜索同类商品并计算平均客单价，必须是简短通用品类词；'
        'market_query 不会替换 name。只引用输入，不编造销量或成本。返回 JSON：'
        '{"bands":[{"key":"volume","items":[{"name":"原逻辑具体商品名","category":"品类",'
        '"market_query":"同类价格查询词","reason":"十字内机会说明"}]}]}。'
    )
    raw = call_deepseek_api(prompt, json.dumps({'hot_words': hot, 'bands': bands_ctx}, ensure_ascii=False),
                            temperature=0.25, max_tokens=12000, api_key=DEEPSEEK_SELECTION_API_KEY)
    parsed = _selection_v2_parse_object(raw) or {}
    by_key = {x.get('key'): x.get('items') for x in (parsed.get('bands') or []) if isinstance(x, dict)}
    buckets = {band['key']: [] for band in _SELECTION_V2_BANDS}
    proposed_seen = set()
    for source_band in _SELECTION_V2_BANDS:
        for item in by_key.get(source_band['key']) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            key = _selection_v2_key(name)
            market_query = str(item.get('market_query') or '').strip()
            if not name or not key or key in proposed_seen or not market_query:
                continue
            found = _selection_v2_tmall_market(market_query, display_name=name)
            if not found:
                continue
            actual_band = next((b for b in _SELECTION_V2_BANDS
                                if b['min'] <= found['market_avg_price'] <= b['max']), None)
            if not actual_band:
                continue
            found.update({'source': 'douyin_tmall', 'market_query': market_query,
                          'reason': str(item.get('reason') or '')[:80],
                          'category': str(item.get('category') or found.get('category') or '')})
            buckets[actual_band['key']].append(found)
            proposed_seen.add(key)

    # 模型候选因市场均价被重分层后若有缺口，沿用旧逻辑的天猫原始产品名补足；
    # 仅价格取该产品所属排行榜全部商品的平均值，不改写补位产品名。
    rank_rows_cache = {}
    fallback_rows = [row for band in _SELECTION_V2_BANDS for row in pools[band['key']]]
    for row in fallback_rows:
        rank_name = row.get('rank_name') or ''
        if rank_name not in rank_rows_cache:
            rank_rows_cache[rank_name] = list(db_execute(
                "SELECT 类别名, 排行榜名, 产品名, 价格, 日期 FROM `天猫榜单表` "
                "WHERE `排行榜名` = %s AND `价格` > 0 ORDER BY `日期` DESC", [rank_name]))
        found = _selection_v2_market_from_rows(
            rank_rows_cache[rank_name], row['name'], '排行榜名', row.get('category') or '')
        if not found:
            continue
        actual_band = next((b for b in _SELECTION_V2_BANDS
                            if b['min'] <= found['market_avg_price'] <= b['max']), None)
        if not actual_band or len(buckets[actual_band['key']]) >= actual_band['quota']:
            continue
        key = _selection_v2_key(found['name'])
        if key in proposed_seen:
            continue
        found.update({'source': 'tmall', 'reason': '天猫榜单均价补位'})
        buckets[actual_band['key']].append(found)
        proposed_seen.add(key)

    global_seen, result = set(), []
    for band in _SELECTION_V2_BANDS:
        items = []
        for found in buckets[band['key']]:
            price = found['market_avg_price']
            key = _selection_v2_key(found['name'])
            if not key or key in global_seen:
                continue
            items.append({'name': found['name'], 'category': found.get('category') or '',
                          'price_range': _selection_v2_market_price_label(price),
                          'market_avg_price': price, 'sample_count': found.get('sample_count') or 0,
                          'match_source': found.get('match_source'), 'source': found.get('source'),
                          'reason': str(found.get('reason') or '')[:80]})
            global_seen.add(key)
            if len(items) >= band['quota']:
                break
        result.append(dict(band, candidates=items))
    return result


def _selection_v2_aisou_snapshot(names, date_str):
    """读取当天爱搜采集，给每个候选整理四类词的月覆盖/七日搜索摘要。"""
    if not names:
        return {}
    placeholders = ','.join(['%s'] * len(names))
    rows = list(db_execute(
        "SELECT 来源词, 词类型, 词名称, 月覆盖人次, 七日搜索人次 FROM `爱搜数据表` "
        "WHERE `日期` = %s AND `来源词` IN (" + placeholders + ")", [date_str] + names))
    result = {n: {'types': {}, 'word_count': 0, 'month_total': 0.0, 'seven_total': 0.0} for n in names}
    for r in rows:
        source = str(r.get('来源词') or '')
        if source not in result:
            continue
        typ = str(r.get('词类型') or '其他')
        month, seven = _as_parse_num(r.get('月覆盖人次')), _as_parse_num(r.get('七日搜索人次'))
        cell = result[source]['types'].setdefault(typ, {'words': 0, 'month': 0.0, 'seven': 0.0})
        cell['words'] += 1; cell['month'] += month; cell['seven'] += seven
        result[source]['word_count'] += 1; result[source]['month_total'] += month; result[source]['seven_total'] += seven
    return result


def _selection_v2_default_score(candidate, aisou):
    seven = (aisou or {}).get('seven_total') or 0
    words = (aisou or {}).get('word_count') or 0
    demand = 0.5 if not words else min(3.0, round(0.8 + min(1.6, math.log10(max(seven, 10)) / 4) + min(0.6, words / 20), 1))
    return {'aisou_score': demand, 'profit_score': 1.5, 'competition_score': 1.0,
            'after_sales_score': 0.5, 'content_score': 0.5,
            'total_score': round(demand + 3.5, 1), 'comment': '待进一步验证',
            'data_confidence': '低', 'risk_note': '评分模型未返回，需人工复核',
            'profit_note': '无供应链实采，仅作待验证预估'}


def _selection_v2_score(bands, aisou_map):
    """把 70 品结构化数据交给模型，用固定五维权重评分；缺失结果降级而非编造。"""
    entries = []
    for band in bands:
        for c in band['candidates']:
            entries.append({'name': c['name'], 'band': band['key'], 'price_range': c['price_range'],
                            'market_avg_price': c.get('market_avg_price'),
                            'market_sample_count': c.get('sample_count'),
                            'category': c['category'], 'aisou': aisou_map.get(c['name'], {})})
    sys_p = (
        '你是电商选品评分引擎。只根据输入数据评分，不能编造货源价、销量、大品牌份额或外部研究结论。'
        '总分严格为 10：爱搜需求 0-3、利润空间 0-3、市场饱和度/竞争压力 0-2、售后风险 0-1、内容创作空间 0-1。'
        '爱搜分优先看七日搜索、月覆盖、四类词完整度与购买意图；利润数据没有实采时，profit_note 必须说明“无供应链实采”。'
        '竞争分越高表示越值得进入；售后分越高表示风险越低。comment 必须十个字以内。'
        '返回 JSON 对象：{"scores":[{"name":"","aisou_score":0,"profit_score":0,"competition_score":0,'
        '"after_sales_score":0,"content_score":0,"total_score":0,"comment":"","data_confidence":"高/中/低",'
        '"risk_note":"","profit_note":""}]}；分项与 total_score 必须相加一致。'
    )
    raw = call_deepseek_api(sys_p, json.dumps({'candidates': entries}, ensure_ascii=False),
                            temperature=0.15, max_tokens=11500, api_key=DEEPSEEK_SELECTION_API_KEY)
    parsed = _selection_v2_parse_object(raw) or {}
    scores = {str(x.get('name') or ''): x for x in (parsed.get('scores') or []) if isinstance(x, dict)}
    for band in bands:
        for c in band['candidates']:
            score = scores.get(c['name']) or _selection_v2_default_score(c, aisou_map.get(c['name']))
            clean = _selection_v2_default_score(c, aisou_map.get(c['name']))
            for field, cap in (('aisou_score', 3), ('profit_score', 3), ('competition_score', 2),
                               ('after_sales_score', 1), ('content_score', 1)):
                try:
                    clean[field] = max(0, min(cap, round(float(score.get(field, clean[field])), 1)))
                except (TypeError, ValueError):
                    pass
            clean['total_score'] = round(sum(clean[k] for k in ('aisou_score', 'profit_score', 'competition_score', 'after_sales_score', 'content_score')), 1)
            clean['comment'] = str(score.get('comment') or clean['comment'])[:10]
            clean['data_confidence'] = str(score.get('data_confidence') or clean['data_confidence'])
            clean['risk_note'] = str(score.get('risk_note') or clean['risk_note'])[:100]
            clean['profit_note'] = str(score.get('profit_note') or clean['profit_note'])[:120]
            c['aisou'] = aisou_map.get(c['name'], {})
            c['score'] = clean
    return bands


def _selection_v2_finalists(bands):
    selected = []
    for band in bands:
        ranked = sorted(band['candidates'], key=lambda x: (x.get('score', {}).get('total_score', 0), x.get('aisou', {}).get('seven_total', 0)), reverse=True)
        band['finalists'] = [dict(x) for x in ranked[:band['final_quota']]]
        for x in band['finalists']:
            x['band_key'], x['band_name'] = band['key'], band['name']
            selected.append(x)
    return selected


def _selection_v2_variants(finalists):
    """每个原品只生成关联的不同单品，禁止规格型伪裂变。"""
    sys_p = (
        '为每个原品生成恰好 3 个裂变品。裂变品必须是同类目中相关但不同的具体单品，'
        '不能是原品加型号、颜色、容量、尺寸、Pro/Plus，不能重复、不能是品牌词；要有创新性和相邻场景关联。'
        '返回 JSON：{"items":[{"name":"原品","variants":[{"name":"具体裂变品","category":"","reason":"十字内关联理由"}]}]}。'
    )
    raw = call_deepseek_api(sys_p, json.dumps({'finalists': [{'name': x['name'], 'category': x['category'], 'band': x['band_name']} for x in finalists]}, ensure_ascii=False),
                            temperature=0.35, max_tokens=4500, api_key=DEEPSEEK_SELECTION_API_KEY)
    parsed = _selection_v2_parse_object(raw) or {}
    got = {str(x.get('name') or ''): x.get('variants') for x in (parsed.get('items') or []) if isinstance(x, dict)}
    all_keys = {_selection_v2_key(x['name']) for x in finalists}
    for item in finalists:
        variants = []
        for v in got.get(item['name']) or []:
            if not isinstance(v, dict):
                continue
            name, key = str(v.get('name') or '').strip(), _selection_v2_key(v.get('name'))
            if not name or not key or key in all_keys:
                continue
            variants.append({'name': name, 'category': str(v.get('category') or item['category']), 'reason': str(v.get('reason') or '')[:20]})
            all_keys.add(key)
            if len(variants) == 3:
                break
        if len(variants) != 3:
            # 不用“原品 + 型号”的伪裂变来凑数；缺少合格裂变品让任务失败，下一轮可重试。
            raise RuntimeError('%s 未生成 3 个合格的关联裂变品' % item['name'])
        item['variants'] = variants
    return finalists


def _selection_v2_recommendations(finalists):
    sys_p = (
        '基于每个原品的固定评分、风险、爱搜摘要和天猫同类商品市场均价，写一条不超过 200 字的选品建议。'
        '不要编造事实；说明切入客单价、机会、风险、内容或主图方向。只为原品写建议。'
        '返回 JSON：{"items":[{"name":"原品","advice":""}]}。'
    )
    raw = call_deepseek_api(sys_p, json.dumps({'finalists': finalists}, ensure_ascii=False),
                            temperature=0.25, max_tokens=3500, api_key=DEEPSEEK_SELECTION_API_KEY)
    parsed = _selection_v2_parse_object(raw) or {}
    advice = {str(x.get('name') or ''): str(x.get('advice') or '')[:200]
              for x in (parsed.get('items') or []) if isinstance(x, dict)}
    for item in finalists:
        item['advice'] = advice.get(item['name']) or '建议先以小规模素材测试验证转化，再根据主销价格带和售后反馈决定是否放量。'


def _selection_v2_progress(status, message, done=0, total=0):
    _write_json(_SELECTION_V2_JOB_FILE, {'status': status, 'message': message, 'done': done, 'total': total,
                                         'updatedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})


def _run_selection_v2(date_str=None):
    """V2 每日后台任务。爱搜采集为低速串行脚本，以任务状态文件便于恢复和观察。"""
    date_str = date_str or time.strftime('%Y-%m-%d')
    try:
        _selection_v2_progress('candidates', '正在生成四个价格带的 70 个候选品', 0, 70)
        bands = _selection_v2_candidates()
        names = [c['name'] for b in bands for c in b['candidates']]
        if len(names) < 70:
            raise RuntimeError('天猫榜单不足，未能补足 70 个去重候选品')
        _selection_v2_progress('aisou', '正在低速采集 70 个商品的爱搜需求数据', 0, len(names))
        _aisou_enrich(names)  # 采集脚本本身为顺序低速访问，避免对爱搜高并发。
        aisou_map = _selection_v2_aisou_snapshot(names, date_str)
        _selection_v2_progress('scoring', '正在按五维固定规则评分', len(names), len(names))
        bands = _selection_v2_score(bands, aisou_map)
        finalists = _selection_v2_variants(_selection_v2_finalists(bands))
        _selection_v2_progress('advice', '正在生成原品选品建议', len(finalists), len(finalists))
        _selection_v2_recommendations(finalists)
        result = {'version': 'selection-v2', 'date': date_str, 'createdAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                  'bands': bands, 'finalists': finalists,
                  'summary': {'candidateCount': len(names), 'finalistCount': len(finalists),
                              'variantCount': sum(len(x.get('variants', [])) for x in finalists)}}
        _ensure_selection_record_table()
        db_execute("INSERT INTO `选品记录表` (`日期`, `价格区间`, `结果`) VALUES (%s, %s, %s)",
                   [date_str, '四档自动选品', json.dumps(result, ensure_ascii=False)], fetch=False)
        _selection_v2_progress('done', '当日分层选品已完成', 1, 1)
    except Exception as e:
        traceback.print_exc()
        _selection_v2_progress('error', '当日分层选品失败：' + str(e))


def _start_selection_v2(date_str=None):
    global _selection_v2_job_thread
    if _selection_v2_job_thread and _selection_v2_job_thread.is_alive():
        return False
    _selection_v2_job_thread = threading.Thread(target=_run_selection_v2, args=(date_str,), daemon=True,
                                                 name='selection-v2-daily')
    _selection_v2_job_thread.start()
    return True


def _selection_v2_record_for_date(date_str):
    _ensure_selection_record_table()
    rows = list(db_execute("SELECT `结果` FROM `选品记录表` WHERE `日期` = %s ORDER BY `创建时间` DESC, `id` DESC", [date_str]))
    for row in rows:
        try:
            data = json.loads(row.get('结果') or '{}')
        except Exception:
            continue
        if isinstance(data, dict) and data.get('version') == 'selection-v2':
            return data
    return None


def _selection_v2_has_today():
    try:
        return bool(_selection_v2_record_for_date(time.strftime('%Y-%m-%d')))
    except Exception as e:
        print('[选品][V2补跑] 当日结果检查失败: %s' % e)
        return True


@app.route('/api/product-selection/dashboard/dates', methods=['GET'])
def ps_v2_dashboard_dates():
    try:
        _ensure_selection_record_table()
        rows = list(db_execute("SELECT DISTINCT `日期` FROM `选品记录表` ORDER BY `日期` DESC LIMIT 180"))
        dates = [str(r.get('日期')) for r in rows if _selection_v2_record_for_date(str(r.get('日期')))]
        return success({'dates': dates})
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/dashboard', methods=['GET'])
def ps_v2_dashboard():
    try:
        date_str = (request.args.get('date') or time.strftime('%Y-%m-%d')).strip()
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            return fail('日期格式应为 YYYY-MM-DD')
        return success(_selection_v2_record_for_date(date_str))
    except Exception as e:
        return fail(str(e))


@app.route('/api/product-selection/dashboard/status', methods=['GET'])
def ps_v2_dashboard_status():
    return success(_read_json(_SELECTION_V2_JOB_FILE, {'status': 'idle', 'message': ''}))


@app.route('/api/product-selection/dashboard/run', methods=['POST'])
def ps_v2_dashboard_run():
    try:
        payload = request.get_json(silent=True) or {}
        date_str = (payload.get('date') or time.strftime('%Y-%m-%d')).strip()
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            return fail('日期格式应为 YYYY-MM-DD')
        if not _start_selection_v2(date_str):
            return fail('已有当日分层选品任务在运行中')
        return success({'started': True, 'date': date_str}, '已开始生成当日分层选品')
    except Exception as e:
        return fail(str(e))


# ======================== 选品助手内的天猫榜单采集入口 ========================

@app.route('/api/tmall-ranklist/status', methods=['GET'])
def tmall_ranklist_status():
    """前端只读取任务状态和近四周数据量，不把含链接的原始爬虫输出暴露给浏览器。"""
    try:
        job = _read_json(_TMALL_RANKLIST_JOB, {
            'status': 'idle', 'message': '等待每周一 00:00 自动采集',
            'excludedCategories': list(_TMALL_RANKLIST_EXCLUDED),
            'keepWeeks': _TMALL_RANKLIST_KEEP_WEEKS,
        }) or {}
        crawl = _read_json(_TMALL_RANKLIST_PROGRESS, {}) or {}
        if job.get('status') == 'running' and crawl.get('message'):
            job['crawlerMessage'] = crawl.get('message')
            job['crawlerDone'] = int(crawl.get('done') or 0)
            job['crawlerTotal'] = int(crawl.get('total') or 0)
            job['crawlerRows'] = int(crawl.get('rowCount') or 0)
        job['nextRun'] = _tmall_ranklist_next_run().strftime('%Y-%m-%d %H:%M:%S')
        job['weeks'] = _tmall_ranklist_db_summary()
        return success(job)
    except Exception as e:
        return fail(str(e))


@app.route('/api/tmall-ranklist/run', methods=['POST'])
def tmall_ranklist_run_api():
    """手动验证入口；与定时任务共用同一把锁，避免同一登录态并发访问淘宝。"""
    try:
        if not _start_tmall_ranklist(trigger='manual'):
            return fail('已有天猫榜单采集任务在运行中')
        return success({'started': True, 'date': _tmall_ranklist_now().strftime('%Y-%m-%d')},
                       '已开始天猫榜单采集')
    except Exception as e:
        return fail(str(e))


# ======================== 每日 7 点自动抓取 + 筛选 ========================

def _douyin_hot_has_today():
    """「抖音热搜品类表」当天是否已有筛选结果（用于判断启动时要不要补跑）"""
    try:
        rows = db_execute("SELECT COUNT(*) AS c FROM `抖音热搜品类表` WHERE `日期` = %s",
                          [time.strftime('%Y-%m-%d')])
        return bool(rows and rows[0]['c'])
    except Exception as e:
        # 查询本身异常时按「已有数据」处理，避免失败循环反复灌库
        print(f'[选品][补跑] 当天数据检查失败: {e}')
        return True


def _douyin_hot_auto_loop():
    """后台线程：每天 7 点自动抓取抖音热点宝并筛选；启动时当天缺数据则立即补跑"""
    # ---- 启动补跑 ----
    # 覆盖两种会让当天数据永久丢失的情况：
    #   ① 服务恰在 07:00 重启 → 主循环 next_run <= now 会顺延到次日，当天被跳过且无补跑；
    #   ② 覆盖式整库同步（sync_db.py）把当天已落库的数据冲掉。
    # 只在 07:00 之后判断：若服务在 7 点前启动，交给下面的主循环正常触发，避免重复抓取。
    try:
        time.sleep(15)  # 等 gunicorn worker 与数据库连接池就绪
        now = datetime.now()
        if now.hour >= 7 and not _douyin_hot_has_today():
            print('[选品][补跑] 当天无热搜数据，立即补跑一次抓取+筛选')
            _run_scrape_and_filter()
        elif now.hour >= 7 and not _selection_v2_has_today():
            # 热搜已在，但服务可能在旧版任务完成后重启；补跑 V2 不必重新抓热搜。
            print('[选品][V2补跑] 当天热搜已存在，补跑分层选品任务')
            _start_selection_v2(time.strftime('%Y-%m-%d'))
        else:
            print('[选品][补跑] 当天数据已存在或未到 07:00，跳过补跑')
    except Exception as e:
        print(f'[选品][补跑] 异常: {e}')

    # ---- 每日 07:00 定时 ----
    while True:
        try:
            now = datetime.now()
            next_run = now.replace(hour=7, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            time.sleep((next_run - now).total_seconds())
            print('[选品][定时] 开始每日热点宝抓取')
            _run_scrape_and_filter()
        except Exception as e:
            print(f'[选品][定时] 异常: {e}')
            _dev_alert_exc('抖音热点定时任务异常', e, signature='douyin:hot:loop',
                           source='短视频热词定时任务')


_douyin_hot_auto_thread = threading.Thread(target=_douyin_hot_auto_loop, daemon=True,
                                           name='douyin-hot-auto')
_douyin_hot_auto_thread.start()



# ======================== 启动 ========================

def _seeding_reconcile_now():
    """主动对账：把「上一轮抓取后消失的作品」记入被删列表。

    为什么不能只靠前端 GET 触发（`/api/seeding/works` 里也调了 guard）：
    没人打开种草页面时对账永远不发生 → 明明作品删了/隐藏了，被删列表却一直是空的。
    用户口径是「删除的或隐藏的都放到被删作品里」，所以每轮抓取后由定时线程主动跑一次。
    幂等：同一个 key 入列后不会重复（`_seeding_reconcile_deleted` 里有 existing 去重）。
    """
    for platform, loader in (('douyin', _seeding_load_works_csv),
                             ('xhs', _seeding_load_xhs_works)):
        try:
            works = loader() or []
            if not works:
                print(f'[种草][对账] {platform}: 无数据文件，跳过', flush=True)
                continue
            done, reason = _seeding_reconcile_guard(platform, works)
            if done:
                print(f'[种草][对账] {platform}: 已对比快照（当前 {len(works)} 条）', flush=True)
            else:
                print(f'[种草][对账] {platform}: 跳过（{reason}）', flush=True)
        except Exception as e:
            print(f'[种草][对账] {platform}: 异常 {e}', flush=True)
            _dev_alert_exc('种草作品对账异常', e, extra='平台：%s' % platform,
                           signature='seeding:reconcile:%s' % platform, source='种草定时任务')


def _seeding_sleep_until(target):
    """睡到指定时刻；分段睡（每分钟醒一次），长时间等待也不会卡住进程退出"""
    while True:
        remain = (target - datetime.now()).total_seconds()
        if remain <= 0:
            return
        time.sleep(min(remain, 60))


def _seeding_auto_update_loop():
    """后台线程：每天 09:00~19:00 之间每半小时自动抓取一轮（抖音 + 小红书同步）。

    ★ 2026-09-18 改：原实现是「启动即抓一轮，之后死循环 sleep 30 分钟」，夜里也在打接口。
      现在改为按时间表触发（09:00、09:30、…、19:00），夜间只睡眠等待：
        · 少打一半的接口，降低被平台限流/风控的概率；
        · 夜里没人处置告警，也不必在夜里刷数据。
      ⚠ 时间窗常量、新鲜度判定（_seeding_last_expected_run）与 tools/seeding_health.py 三处必须一致。
    """
    time.sleep(10)  # 等服务完成初始化，避免与启动过程竞争

    # ★ 启动自愈：服务重启时 systemd 会把正在跑的抓取子进程一起清掉，
    #   但进度文件会停在 running —— 旧的「30 分钟时间窗」判据会因此误拦一整轮
    #   （2026-09-17 实测：重启后 xhs 被误判为「正在进行中」）。
    #   此刻不可能真有抓取在跑（cgroup 已清空），直接把残留的 running 重置为 idle。
    for _p in ('douyin', 'xhs'):
        _st = _seeding_progress_read(_p)
        if _st.get('status') == 'running':
            _seeding_progress_write(_p, 'idle', _st.get('done', 0), _st.get('total', 0),
                                    '服务重启中断了本轮抓取')
            print('[种草] %s: 上次抓取被服务重启中断，已重置进度（下一轮可正常触发）' % _p,
                  flush=True)

    print('[种草][自动更新] 时间表：每天 %02d:00-%02d:00 每 %d 分钟一轮（抖音/小红书同步）'
          % (_SEEDING_WINDOW_START, _SEEDING_WINDOW_END, _SEEDING_AUTO_INTERVAL // 60),
          flush=True)

    while True:
        # ★ 先按时间表睡到下一个抓取点（窗口外会一直睡到次日 09:00）
        nxt = _seeding_next_run()
        print('[种草][自动更新] 下一次自动抓取：%s' % nxt.strftime('%Y-%m-%d %H:%M'), flush=True)
        _seeding_sleep_until(nxt)

        try:
            # ★ 先体检「上一轮」再触发新一轮（顺序不能反：触发会把进度覆写成 running）
            #   异常时 tools/seeding_health.py 会推钉钉给李自豪，见该文件头部说明
            _seeding_health_check()
            # ★ 体检之后、触发之前对账：此时上一轮数据文件刚写完、status=done，时机正确。
            #   顺序反了会因为新进度还是 running 而被 guard 跳过。
            _seeding_reconcile_now()
            # ★ 点赞阈值推送与对账共用同一个时间窗（上一轮数据刚落盘），
            #   单独 try 包住：推送失败不能连累本轮抓取触发。
            try:
                n, why = _seeding_like_push_check()
                print('[种草][点赞推送] %s' % why, flush=True)
            except Exception as e:
                print('[种草][点赞推送] 检查异常: %s' % e)
                _dev_alert_exc('种草点赞推送检查异常', e, signature='seeding:like-push',
                               source='种草定时任务')
            for platform in ('douyin', 'xhs'):
                # ★ 距上次「成功抓取」不足 _SEEDING_MIN_GAP 就跳过本轮：
                #   服务重启会把循环节拍归零（说明见 _SEEDING_MIN_GAP 定义处）
                _st = _seeding_progress_read(platform)
                _gap = time.time() - float(_st.get('ts') or 0)
                if _st.get('status') == 'done' and _st.get('ts') and _gap < _SEEDING_MIN_GAP:
                    print('[种草][自动更新] %s: 距上次成功抓取仅 %.1f 分钟（< %d 分钟），'
                          '本轮跳过，避免平台限流' % (platform, _gap / 60.0, _SEEDING_MIN_GAP // 60))
                    time.sleep(5)
                    continue
                err, skip = _seeding_launch(platform)
                if err:
                    print(f'[种草][自动更新] {platform}: {err}')
                elif skip:
                    print(f'[种草][自动更新] {platform}: {skip}')
                else:
                    print(f'[种草][自动更新] 已触发 {platform} 作品抓取（每 {_SEEDING_AUTO_INTERVAL // 60} 分钟）')
                time.sleep(5)  # 两个平台错开，避免同时打满
        except Exception as e:
            print(f'[种草][自动更新] 触发异常: {e}')
            _dev_alert_exc('种草自动更新循环异常', e, signature='seeding:loop',
                           source='种草定时任务')


# daemon 线程，gunicorn 单 worker 下只启动一次；随进程退出自动结束
_seeding_auto_thread = threading.Thread(target=_seeding_auto_update_loop, daemon=True, name='seeding-auto-update')
_seeding_auto_thread.start()


# 天猫榜单独立按周采集；服务使用单 worker，故本线程在一个进程内只会启动一次。
_tmall_ranklist_auto_thread = threading.Thread(target=_tmall_ranklist_auto_loop, daemon=True,
                                                name='tmall-ranklist-auto')
_tmall_ranklist_auto_thread.start()


# ======================== 每日分析报告 → 钉钉推送 ========================
# 建表放这里（模块导入即执行）：线上用 gunicorn 启动不会跑 __main__ 里的建表逻辑
_push_ensure_tables()
# 通告发放独立配置表（独立于钉钉推送的 dingtalk_push_config）
_announce_ensure_table()

# daemon 线程：启动时校验/补齐「通告发放」配置 + 名单（页面直接读这份，无需人工同步）。
# 名单三级兜底：内存 → announce_contacts_cache 落库 → 实时拉钉钉；
# 每日 08:30 由 _announce_roster_loop 自动刷新，保证 9 点前是最新列表。
def _announce_prewarm():
    try:
        cfg = _announce_config()
        missing = {k: v for k, v in _ANNOUNCE_CFG_DEFAULTS.items()
                   if v and not (cfg.get(k) or '').strip()}
        if missing:
            _announce_config_save(missing)
            print('[通告发放] 已从内置默认值补齐配置项: %s' % ','.join(missing))
    except Exception as e:
        print('[通告发放] 配置自愈失败: %s' % e)
    try:
        st = _announce_roster_status()
        if st.get('ready'):
            print('[通告发放] 名单已就绪（落库缓存）：部门 %d / 成员 %d，更新于 %s'
                  % (st['deptCount'], st['userCount'], st['syncedAt']))
        else:
            _announce_contacts_refresh()
            print('[通告发放] 名单首次生成完成（落库 + 内存）')
    except Exception as e:
        print('[通告发放] 名单初始化失败（定时任务会自动重试）: %s' % e)


threading.Thread(target=_announce_prewarm, daemon=True, name='announce-prewarm').start()
# 每日名单刷新线程（08:30 前就绪，含重启补跑）
threading.Thread(target=_announce_roster_loop, daemon=True, name='announce-roster-loop').start()
# 种草专有表：推送人名单 / 爆文库 / 优化建议（同样必须在导入阶段建好）
_seeding_ensure_tables()

# daemon 线程：默认每天 11:00 生成昨日报告并推送到钉钉
_daily_report_push_thread = threading.Thread(target=_daily_report_push_loop, daemon=True,
                                             name='daily-report-push')
_daily_report_push_thread.start()
print('[钉钉推送] 每日报告推送线程已启动（时间与开关可在「每日数据分析」页配置）')


# ======================== 店铺账号管理 API（千牛/抖店/抖店邮箱/京东） ========================
# 表：千牛账号表 / 抖店账号表 / 抖店邮箱账号表 / 京东账号表
# 字段映射：前端 camelCase -> 数据库中文列名
_ACCOUNT_CFG = {
    'qianniu': {
        'table': '千牛账号表',
        'fields': {'account': '账号', 'password': '密码', 'shopId': '店铺ID', 'brand': '品牌',
                   'active': '是否运营', 'remark': '备注'},
        'required': ['account'],
    },
    'doudian': {
        'table': '抖店账号表',
        'fields': {'shopName': '店铺名', 'shopId': '店铺ID', 'brand': '品牌',
                   'active': '是否运营', 'remark': '备注'},
        'required': ['shopName'],
    },
    'doudian-email': {
        'table': '抖店邮箱账号表',
        'fields': {'email': '邮箱', 'password': '密码', 'active': '是否运营', 'remark': '备注'},
        'required': ['email'],
    },
    'jd': {
        'table': '京东账号表',
        'fields': {'account': '账号', 'password': '密码', 'shopId': '店铺ID', 'shopName': '店铺名',
                   'active': '是否运营', 'remark': '备注'},
        'required': ['account'],
    },
}


def _acct_to_front(r, fields):
    """数据库行（中文列名）-> 前端 camelCase 对象"""
    rev = {col: key for key, col in fields.items()}
    item = {'id': r.get('id')}
    for col, key in rev.items():
        item[key] = r.get(col)
    item['createdAt'] = str(r.get('创建时间')) if r.get('创建时间') else ''
    item['updatedAt'] = str(r.get('更新时间')) if r.get('更新时间') else ''
    return item


def _build_account_views(prefix, cfg):
    table = cfg['table']
    fields = cfg['fields']
    required = cfg['required']

    def list_view():
        try:
            rows = db_execute('SELECT * FROM `%s` ORDER BY id' % table)
            return success([_acct_to_front(r, fields) for r in rows])
        except Exception as e:
            return fail(str(e))

    def create_view():
        try:
            data = request.get_json(force=True) or {}
            for req in required:
                if not str(data.get(req, '')).strip():
                    return fail('请填写必填字段')
            cols, vals = [], []
            for key, col in fields.items():
                if key not in data:
                    continue
                v = 1 if (key == 'active' and data[key]) else (0 if key == 'active' else data[key])
                cols.append('`%s`' % col)
                vals.append(v)
            if not cols:
                return fail('没有可写入的字段')
            sql = 'INSERT INTO `%s` (%s) VALUES (%s)' % (table, ', '.join(cols), ', '.join(['%s'] * len(vals)))
            new_id = db_execute_insert(sql, vals)
            return success({'id': new_id}, '已新增')
        except Exception as e:
            return fail(str(e))

    def update_view(acct_id):
        try:
            data = request.get_json(force=True) or {}
            cols, vals = [], []
            for key, col in fields.items():
                if key not in data:
                    continue
                v = 1 if (key == 'active' and data[key]) else (0 if key == 'active' else data[key])
                cols.append('`%s` = %s' % (col, '%s'))
                vals.append(v)
            if not cols:
                return fail('没有可更新的字段')
            vals.append(acct_id)
            sql = 'UPDATE `%s` SET %s WHERE id = %s' % (table, ', '.join(cols), '%s')
            db_execute(sql, vals, fetch=False)
            return success(None, '已更新')
        except Exception as e:
            return fail(str(e))

    def delete_view(acct_id):
        try:
            db_execute('DELETE FROM `%s` WHERE id = %s' % (table, '%s'), [acct_id], fetch=False)
            return success(None, '已删除')
        except Exception as e:
            return fail(str(e))

    def toggle_view(acct_id):
        try:
            data = request.get_json(force=True) or {}
            if 'active' in data:
                active = 1 if data['active'] else 0
            else:
                row = db_execute('SELECT `是否运营` FROM `%s` WHERE id = %s' % (table, '%s'), [acct_id])
                if not row:
                    return fail('记录不存在')
                active = 0 if row[0]['是否运营'] else 1
            db_execute('UPDATE `%s` SET `是否运营` = %s WHERE id = %s' % (table, '%s', '%s'),
                       [active, acct_id], fetch=False)
            return success({'active': active}, '已切换')
        except Exception as e:
            return fail(str(e))

    return list_view, create_view, update_view, delete_view, toggle_view


for _prefix, _cfg in _ACCOUNT_CFG.items():
    _lv, _cv, _uv, _dv, _tv = _build_account_views(_prefix, _cfg)
    _base = '/api/store-accounts/%s' % _prefix
    _ep = _prefix.replace('-', '_')
    app.add_url_rule(_base, 'acct_%s_list' % _ep, _lv, methods=['GET'])
    app.add_url_rule(_base, 'acct_%s_create' % _ep, _cv, methods=['POST'])
    app.add_url_rule(_base + '/<int:acct_id>', 'acct_%s_update' % _ep, _uv, methods=['PUT'])
    app.add_url_rule(_base + '/<int:acct_id>', 'acct_%s_delete' % _ep, _dv, methods=['DELETE'])
    app.add_url_rule(_base + '/<int:acct_id>/toggle', 'acct_%s_toggle' % _ep, _tv, methods=['PUT'])

print('[账号API] 店铺账号管理路由已注册（千牛/抖店/抖店邮箱/京东）')


# ==================== 抓取任务 API（店铺账号管理「更新数据」） ====================
# 站点与抓取程序同机（/opt/pw），gunicorn 以 root 运行 → 可直接 subprocess 唤起抓取。
# 平台映射与 tools/fetch_reconcile.py 严格一致：
#   千牛账号表 → 平台「千牛」 / 抖店账号表 → 平台「抖音」 / 京东账号表 → 平台「京东」
# 账号列表来源就是店铺账号管理在编辑的那三张表，`是否运营=1` 即「运营中」。
import threading  # noqa: E402  （下文线程用；模块前面只 import 了 Lock/RLock）
import signal     # noqa: E402  （超时强杀进程组用，见 _fetch_kill_tree）

_FETCH_PW = '/opt/pw'
_FETCH_PY = '/opt/pw/venv/bin/python'
_FETCH_XVFB = '/usr/bin/xvfb-run'
_FETCH_CFG = {
    'qianniu': {
        'label': '千牛', 'db_platform': '千牛', 'table': '千牛账号表',
        'cli_col': '账号', 'script': '/opt/pw/fetch_daily.py',
        'style': 'per_shop', 'timeout': 1800,
    },
    'doudian': {
        'label': '抖店', 'db_platform': '抖音', 'table': '抖店账号表',
        'cli_col': '店铺名', 'script': '/opt/pw/doudian/login_fetch_all.py',
        'style': 'batch', 'batch_size': 4, 'timeout': 3600,
    },
    'jd': {
        'label': '京东', 'db_platform': '京东', 'table': '京东账号表',
        'cli_col': '店铺名', 'script': '/opt/pw/jd/fetch_main.py',
        'style': 'whole', 'timeout': 1800,
    },
}
_FETCH_READY = os.path.isdir(_FETCH_PW) and os.path.isfile(_FETCH_PY)
_FETCH_LOG_MAX = 600
_fetch_jobs = {}
_fetch_seq = [0]
_fetch_lock = threading.Lock()


def _fetch_now():
    return datetime.now().strftime('%F %T')


def _fetch_log(job, line):
    line = (line or '').rstrip()
    if not line:
        return
    log = job['log']
    log.append(line)
    if len(log) > _FETCH_LOG_MAX:
        del log[:len(log) - _FETCH_LOG_MAX]


def _fetch_parse_dates(start, end):
    """起止日期 → ['YYYY-MM-DD', ...]（缺 end 视为单日；上限 31 天）"""
    def _d(s):
        return datetime.strptime(str(s).strip(), '%Y-%m-%d').date()
    s = _d(start)
    e = _d(end) if (end or '').strip() else s
    if e < s:
        raise ValueError('结束日期不能早于开始日期')
    out, cur = [], s
    while cur <= e:
        out.append(cur.strftime('%Y-%m-%d'))
        cur += timedelta(days=1)
    if len(out) > 31:
        raise ValueError('单次最多抓取 31 天')
    return out


def _fetch_shops(cfg, ids=None):
    """该平台待抓店铺（是否运营=1）。ids 为账号表 id 列表，None/空 = 全部运营中。"""
    sql = 'SELECT id, `店铺ID`, `%s` AS cli_value FROM `%s` WHERE `是否运营` = 1' % (
        cfg['cli_col'], cfg['table'])
    params = []
    if ids:
        sql += ' AND id IN (%s)' % ', '.join(['%s'] * len(ids))
        params = list(ids)
    rows = db_execute(sql + ' ORDER BY id', params) or []
    return [{'id': r['id'], 'shop_id': str(r.get('店铺ID') or '').strip(),
             'cli_value': (r.get('cli_value') or '').strip()} for r in rows]


def _fetch_build_cmds(platforms, dates, sel_ids):
    """展开成待执行命令：[{platform,label,date,target,argv,shops:[...]}]"""
    cmds = []
    for pkey in platforms:
        cfg = _FETCH_CFG.get(pkey)
        if not cfg:
            continue
        shops = _fetch_shops(cfg, sel_ids.get(pkey))
        if not shops:
            continue
        for d in dates:
            if cfg['style'] == 'per_shop':
                for s in shops:
                    cmds.append({
                        'platform': pkey, 'label': cfg['label'], 'date': d,
                        'target': s['cli_value'],
                        'argv': [_FETCH_PY, cfg['script'], s['cli_value'], d],
                        'shops': [s],
                    })
            elif cfg['style'] == 'batch':
                size = cfg.get('batch_size') or 4
                for i in range(0, len(shops), size):
                    chunk = shops[i:i + size]
                    cmds.append({
                        'platform': pkey, 'label': cfg['label'], 'date': d,
                        'target': '、'.join(s['cli_value'] for s in chunk),
                        'argv': [_FETCH_PY, cfg['script'], d,
                                 ','.join(s['cli_value'] for s in chunk)],
                        'shops': chunk,
                    })
            else:  # whole：单店平台，全量重跑
                cmds.append({
                    'platform': pkey, 'label': cfg['label'], 'date': d,
                    'target': '、'.join(s['cli_value'] for s in shops),
                    'argv': [_FETCH_PY, cfg['script'], '--date', d],
                    'shops': shops,
                })
    return cmds


# 抖店登录态时效预检阈值（分钟）。
# 抖店 state 是**账号级**（14 家店共用一个邮箱）且寿命很短：实测 10:05 刷新 →
# 10:11 可用 → 10:44 已失效，约 40 分钟。一旦过期，login_fetch_all.py 会退到邮箱登录，
# 而服务器（IDC IP + xvfb 虚拟屏）既过不了拼图滑块、也没有任何窗口可供人工拖拽 ——
# 结果是每条命令干等 240s 后失败。batch(4家/条) × N 天会把这笔账乘上去，
# 用户在前端只会看到「卡住」。
# 取 25 分钟：给「启动 → 抓完 14 家店」留余量（一轮实测 20~30 分钟）。
_FETCH_DD_STATE_MAX_MIN = 25


def _fetch_dd_state_check():
    """抖店登录态时效预检。返回 None = 可用；否则返回给前端的中文拒绝理由。"""
    rows = db_execute(
        'SELECT `邮箱`, `状态更新时间`, '
        'TIMESTAMPDIFF(MINUTE, `状态更新时间`, NOW()) AS mins '
        'FROM `抖店邮箱账号表` WHERE `是否运营` = 1 ORDER BY `id` LIMIT 1') or []
    if not rows:
        return '抖店邮箱账号表里没有「运营中」的邮箱账号，无法抓取'
    r = rows[0]
    email = r.get('邮箱') or '?'
    if not r.get('状态更新时间'):
        return ('抖店（%s）从未保存过登录态，抓取必然失败。\n'
                '请在本机双击「启动抖店登录.bat」登录一次后重试。' % email)
    mins = int(r.get('mins') or 0)
    if mins > _FETCH_DD_STATE_MAX_MIN:
        return ('抖店登录态已过期：%s 最后一次有效在 %d 分钟前（实测寿命约 40 分钟）。\n'
                '服务器上没有窗口、也没人能帮它过拼图滑块，本次抓取必然失败，'
                '已提前拦下以免白等。\n'
                '请先在本机双击「启动抖店登录.bat」恢复登录态，再重新触发抓取。'
                % (email, mins))
    return None


def _fetch_kill_tree(proc, grace=3):
    """超时强杀：连**整个进程组**一起清，不要只杀最外层。

    ★ 2026-09-17 修（线上实测残留 7 个孤儿 Xvfb，:99~:105，横跨 6 小时）：
      抓取命令是 `xvfb-run -a python xxx.py`，而 xvfb-run 是 shell 脚本，
      它自己再去启 Xvfb 和 python。原来的 proc.kill() 只杀掉 xvfb-run 这一层 ——
      下面的 Xvfb / python / Chrome 全部变孤儿被 init 收养，继续跑到底：
        · Xvfb 没有「父进程断开就自退」的机制 → 只增不减（占显示号 + 内存）
        · Chrome 若没退干净会持续吃 CPU（实测单渲染进程 156%），
          叠几次之后整个 4 核机器就被拖垮 —— 网页端随之卡成「未响应」。
      配合 Popen(start_new_session=True)，子进程自成一个进程组，这里 killpg 一锅端。
      注意：start_new_session 只改会话、不改 cgroup —— systemd 的
      KillMode=control-group 依然能在重启 ecom 时把这些进程一起带走，不影响运维。
    """
    if proc is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=grace)
        return
    except Exception:
        pass
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        pass


def _fetch_exec(argv, job, timeout):
    """执行单条抓取命令，stdout 实时灌进 job['log']。返回退出码。"""
    cmd = ([_FETCH_XVFB, '-a'] + argv) if os.path.isfile(_FETCH_XVFB) else argv
    _fetch_log(job, '$ ' + ' '.join(cmd))
    try:
        # PW_UNATTENDED=1：告诉抓取脚本「本机是 xvfb 虚拟屏，没人能拖滑块」，
        # 让它遇到拼图验证立刻失败退出（否则会干等 240s，见 login_fetch_all.UNATTENDED）
        env = dict(os.environ)
        env['PW_UNATTENDED'] = '1'
        # start_new_session=True：子进程自成进程组，超时才杀得干净（见 _fetch_kill_tree）
        proc = subprocess.Popen(cmd, cwd=_FETCH_PW, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding='utf-8', errors='replace', bufsize=1,
                                start_new_session=True, env=env)
    except Exception as e:
        _fetch_log(job, '[err] 启动失败: %s' % e)
        return -1
    killer = threading.Timer(timeout, lambda: _fetch_kill_tree(proc))
    killer.start()
    try:
        for line in proc.stdout:
            _fetch_log(job, line)
        proc.wait()
    finally:
        killer.cancel()
        try:
            proc.stdout.close()
        except Exception:
            pass
    return proc.returncode


def _fetch_verify(cmds, job):
    """抓完对账：所选店铺是否已写进「店铺营销数据」。返回缺失清单文案行。"""
    seen = {}
    for c in cmds:
        for s in c['shops']:
            if not s['shop_id']:
                continue
            seen[(c['platform'], c['date'], s['shop_id'], s['cli_value'])] = True
    lines, miss_n = [], 0
    for (pkey, d, shop_id, label) in seen:
        cfg = _FETCH_CFG[pkey]
        row = db_execute(
            'SELECT COUNT(*) AS n FROM `店铺营销数据` WHERE `平台`=%s AND `日期`=%s AND `店铺ID`=%s',
            [cfg['db_platform'], d, shop_id])
        n = (row[0]['n'] if row else 0) or 0
        if n:
            lines.append('  ✅ %s %s %s' % (d, cfg['label'], label))
        else:
            miss_n += 1
            lines.append('  ❌ %s %s %s（未落库）' % (d, cfg['label'], label))
    return lines, miss_n


def _fetch_job_thread(job_id):
    job = _fetch_jobs.get(job_id)
    if not job:
        return
    cmds = job['_cmds']
    ok = fail = 0
    try:
        for i, c in enumerate(cmds, 1):
            if job.get('_stop'):
                _fetch_log(job, '[stop] 已请求停止，中断剩余任务')
                break
            job['current'] = '%s / %s / %s' % (c['date'], c['label'], c['target'])
            job['index'] = i
            _fetch_log(job, '\n===== [%d/%d] %s %s · %s ====='
                       % (i, len(cmds), c['date'], c['label'], c['target']))
            rc = _fetch_exec(c['argv'], job, _FETCH_CFG[c['platform']]['timeout'])
            _fetch_log(job, '  → rc=%s' % rc)
            if rc in (0, 3):
                # 0 = 全部落库；3 = 有店铺未落库（明细交给下面的对账段判断）
                ok += 1
            else:
                fail += 1
        job['done'] = len(cmds) if not job.get('_stop') else job.get('index', 0)

        _fetch_log(job, '\n===== 对账：所选店铺是否落库 =====')
        try:
            lines, miss_n = _fetch_verify(cmds, job)
            for ln in lines:
                _fetch_log(job, ln)
            job['missing'] = miss_n
            _fetch_log(job, '对账结论：%s' % ('全部落库 ✅' if not miss_n
                                        else '有 %d 项未落库 ⚠️' % miss_n))
        except Exception as e:
            _fetch_log(job, '[warn] 对账失败: %s' % e)

        job['ok'], job['fail'] = ok, fail
        job['status'] = 'done'
    except Exception as e:
        _fetch_log(job, '[err] 任务异常: %s' % e)
        job['status'] = 'fail'
    finally:
        job['current'] = ''
        job['finishedAt'] = _fetch_now()
        job.pop('_cmds', None)


@app.route('/api/fetch/status')
def api_fetch_status():
    """抓取环境是否就绪 + 当前是否有任务在跑"""
    with _fetch_lock:
        running = next((j for j in _fetch_jobs.values() if j['status'] == 'running'), None)
    return success({
        'ready': _FETCH_READY,
        'running': bool(running),
        'jobId': running['id'] if running else '',
        'platforms': [{'key': k, 'label': v['label'], 'style': v['style']}
                      for k, v in _FETCH_CFG.items()],
    })


@app.route('/api/fetch/trigger', methods=['POST'])
def api_fetch_trigger():
    """触发抓取：{platforms:[], itemKeys:['qianniu:3'], start:'', end:''}"""
    if not _FETCH_READY:
        return fail('本机未找到抓取程序（%s），无法触发' % _FETCH_PW)
    try:
        data = request.get_json(force=True) or {}
        platforms = [p for p in (data.get('platforms') or []) if p in _FETCH_CFG]
        if not platforms:
            return fail('请先选择要抓取的平台')
        dates = _fetch_parse_dates(data.get('start'), data.get('end'))
        sel_ids = {}
        for k in (data.get('itemKeys') or []):
            s = str(k)
            if ':' in s:
                pkey, _, sid = s.partition(':')
                if pkey in _FETCH_CFG and sid.isdigit():
                    sel_ids.setdefault(pkey, []).append(int(sid))
        cmds = _fetch_build_cmds(platforms, dates, sel_ids)
        if not cmds:
            return fail('所选平台下没有「运营中」的店铺')
        # 抖店这条链路依赖「抖店邮箱账号表.登录状态」免登录，而该 state 只有约 40 分钟
        # 寿命、且服务器无法自助登录（见 _fetch_dd_state_check）。过期就别启动了。
        if 'doudian' in platforms:
            why = _fetch_dd_state_check()
            if why:
                # data 里带结构化标记：前端据此在错误提示旁挂一个「手动拖滑块」按钮，
                # 让用户不用退出弹窗、跑去桌面找 bat（见 api_slider_request）
                return jsonify({'code': 1, 'msg': why,
                                'data': {'sliderNeeded': True}})
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        return fail('参数错误：%s' % e)

    with _fetch_lock:
        if any(j['status'] == 'running' for j in _fetch_jobs.values()):
            return fail('已有抓取任务在执行，请等它跑完')
        _fetch_seq[0] += 1
        job_id = 'f%d' % _fetch_seq[0]
        _fetch_jobs[job_id] = {
            'id': job_id, 'status': 'running', 'total': len(cmds), 'done': 0,
            'index': 0, 'current': '', 'ok': 0, 'fail': 0, 'missing': 0,
            'log': [], 'startedAt': _fetch_now(), 'finishedAt': '',
            'dates': dates, 'platforms': platforms, 'shopCount': sum(len(c['shops']) for c in cmds),
            'cmdCount': len(cmds), '_cmds': cmds, '_stop': False,
        }
    threading.Thread(target=_fetch_job_thread, args=(job_id,), daemon=True,
                     name='fetch-job-%s' % job_id).start()
    print('[抓取任务] %s 已启动：%d 条命令 / %d 个日期'
          % (job_id, len(cmds), len(dates)))
    return success({'jobId': job_id, 'total': len(cmds)}, '抓取任务已启动')


def _fetch_job_view(job):
    return {
        'jobId': job['id'], 'status': job['status'],
        'total': job['total'], 'done': job['done'], 'index': job.get('index', 0),
        'current': job.get('current', ''),
        'okCount': job.get('ok', 0), 'failCount': job.get('fail', 0),
        'missing': job.get('missing', 0),
        'dates': job.get('dates', []), 'platforms': job.get('platforms', []),
        'shopCount': job.get('shopCount', 0),
        'startedAt': job['startedAt'], 'finishedAt': job['finishedAt'],
        'log': job['log'][-200:],
    }


@app.route('/api/fetch/job/<job_id>')
def api_fetch_job(job_id):
    job = _fetch_jobs.get(job_id)
    if not job:
        return fail('任务不存在或已过期')
    return success(_fetch_job_view(job))


@app.route('/api/fetch/job/latest')
def api_fetch_job_latest():
    """最近一次任务（刷新页面后仍可拿回进度）"""
    if not _fetch_jobs:
        return success(None)
    job = sorted(_fetch_jobs.values(), key=lambda j: j['startedAt'])[-1]
    return success(_fetch_job_view(job))


@app.route('/api/fetch/stop', methods=['POST'])
def api_fetch_stop():
    """请求停止：当前命令跑完后不再执行后续命令"""
    data = request.get_json(force=True) or {}
    job = _fetch_jobs.get(str(data.get('jobId') or ''))
    if not job or job['status'] != 'running':
        return fail('没有正在执行的抓取任务')
    job['_stop'] = True
    return success(None, '已请求停止，当前步骤跑完后中断')


@app.route('/api/fetch/reconcile')
def api_fetch_reconcile():
    """每日对账结果（fetch_reconcile_logs 最近记录，按日期倒序）"""
    try:
        limit = min(int(request.args.get('limit') or 60), 300)
    except Exception:
        limit = 60
    try:
        rows = db_execute(
            'SELECT target_date, round_no, platform, active_count, ok_count, '
            'missing_shops, action, final_status, alert_status, created_at '
            'FROM fetch_reconcile_logs ORDER BY id DESC LIMIT %s', [limit]) or []
        out = []
        for r in rows:
            # 库里抖店落在「抖音」平台（沿用早期影刀口径），界面上仍叫抖店
            plat = r.get('platform')
            out.append({
                'date': str(r.get('target_date') or ''),
                'round': r.get('round_no'),
                'platform': '抖店' if plat == '抖音' else plat,
                'activeCount': r.get('active_count'), 'okCount': r.get('ok_count'),
                'missingShops': r.get('missing_shops') or '',
                'action': r.get('action'), 'status': r.get('final_status'),
                'alertStatus': r.get('alert_status'),
                'createdAt': str(r.get('created_at') or ''),
            })
        return success(out)
    except Exception as e:
        return fail('对账记录读取失败：%s' % e)


# ==================== 抖店「手动拖滑块」：本机助手任务中继 ====================
# 为什么要有这层中继（2026-09-18 加）：
#   抖店登录态是**账号级**的短效凭证（14 家店共用 1 个邮箱，实测约 40 分钟失效）。
#   服务器是 IDC IP + Xvfb 虚拟屏（640x480）：拼图滑块过不去，而且**没有任何窗口
#   存在于人的屏幕上** —— 脚本里那句「请在浏览器窗口人工拖拽」在服务器上物理上做不到。
#   所以登录 + 拖滑块只能在本机（李自豪的 Windows 电脑）完成。原流程要人跑去桌面
#   双击「启动抖店登录.bat」，痛点是「人得离开后台页面去找那个 bat」。
#   现在：后台点「手动拖滑块」→ 后端记一条任务 → 本机常驻助手
#   （tools/doudian_crawler/slider_agent.py）每 3 秒轮询领走 → 自动拉起本机 Chrome
#   → 人工拖滑块完成登录 → 助手把 state 传回云库 → 页面回显结果。
#
# ★ 助手没有浏览器登录态 → pending/report 走共享密钥 X-Slider-Key（已进白名单）。
# ★ 任务状态落 json 文件而不是内存：本项目一天要重启 ecom 几十次，重启不该把用户
#   刚点下的任务弄丢，否则页面会一直转圈。助手心跳则放内存（30 秒就过期，无需持久化）。
_SLIDER_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'tools', '_doudian_slider.json')
# 与 tools/doudian_crawler/slider_agent.py 的 KEY 保持一致；线上可用环境变量覆盖
_SLIDER_KEY = os.environ.get('SLIDER_AGENT_KEY', 'julang-doudian-slider-2026')
# 单条任务总时限（秒）：超时未完成即降级为 timeout，避免页面无限转圈
_SLIDER_TASK_TTL = 600
# 本机助手离线判定（秒）：助手每 3 秒轮询一次，30 秒没动静即认为没在跑
_SLIDER_ALIVE_GAP = 30
# 「进行中」的三个状态；其余（idle/done/fail/timeout）都算终态
_SLIDER_BUSY = ('pending', 'claimed', 'running')
_slider_lock = threading.Lock()
# 本机助手心跳（内存即可）：[lastSeen, host, pid]
_slider_agent_seen = [0.0, '', '']


def _slider_read():
    """读任务状态文件；不存在/损坏一律当空状态（不抛异常、不影响接口）"""
    try:
        with open(_SLIDER_STATE_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _slider_write(st):
    try:
        d = os.path.dirname(_SLIDER_STATE_FILE)
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        # 先写临时文件再原子替换：status 接口不加锁读，避免读到写了一半的 json
        tmp = _SLIDER_STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _SLIDER_STATE_FILE)
    except Exception as e:
        print('[滑块] 状态落盘失败: %s' % e)


def _slider_eff_status(t):
    """任务的有效状态：pending/claimed/running 超过 TTL 一律降级为 timeout"""
    if not t:
        return 'idle'
    s = t.get('status') or 'idle'
    if s in _SLIDER_BUSY and (time.time() - float(t.get('ts') or 0)) > _SLIDER_TASK_TTL:
        return 'timeout'
    return s


def _slider_dd_login_state():
    """抖店邮箱账号表的登录态时效（前端徽章 + 「是不是该去拖滑块了」的判断依据）"""
    rows = db_execute(
        'SELECT `邮箱`, `状态更新时间`, '
        'TIMESTAMPDIFF(MINUTE, `状态更新时间`, NOW()) AS mins '
        'FROM `抖店邮箱账号表` WHERE `是否运营` = 1 ORDER BY `id` LIMIT 1') or []
    if not rows:
        return {'email': '', 'updatedAt': '', 'ageMinutes': None,
                'hasState': False, 'expired': True, 'maxMinutes': _FETCH_DD_STATE_MAX_MIN}
    r = rows[0]
    has = bool(r.get('状态更新时间'))
    mins = r.get('mins')
    age = int(mins) if mins is not None else None
    return {
        'email': r.get('邮箱') or '',
        'updatedAt': str(r.get('状态更新时间') or ''),
        'ageMinutes': age,
        'hasState': has,
        'expired': (not has) or (age is None) or (age > _FETCH_DD_STATE_MAX_MIN),
        'maxMinutes': _FETCH_DD_STATE_MAX_MIN,
    }


def _slider_view(st):
    """给前端的状态视图（status / request / report 共用）"""
    st = st or {}
    t = st.get('task') or {}
    seen = _slider_agent_seen
    return {
        'taskId': t.get('id') or '',
        'status': _slider_eff_status(t),
        'step': t.get('step') or '',
        'message': t.get('message') or '',
        'requestedAt': t.get('requestedAt') or '',
        'requestedBy': t.get('requestedBy') or '',
        'finishedAt': t.get('finishedAt') or '',
        'agentOnline': (time.time() - float(seen[0] or 0)) < _SLIDER_ALIVE_GAP,
        'agentHost': seen[1] or '',
        'login': _slider_dd_login_state(),
    }


def _slider_key_ok():
    """本机助手接口的鉴权：共享密钥放 header，避免公开一个能拉起本机浏览器的入口"""
    return request.headers.get('X-Slider-Key', '') == _SLIDER_KEY


@app.route('/api/fetch/doudian/slider/status')
def api_slider_status():
    """页面轮询：当前滑块任务状态 + 抖店登录态时效

    只读 + 原子写落盘，所以这里不加锁（避免 DB 慢时拖住本机助手的轮询）。
    """
    return success(_slider_view(_slider_read()))


@app.route('/api/fetch/doudian/slider/request', methods=['POST'])
def api_slider_request():
    """页面点「手动拖滑块」：下发一条任务给本机助手（幂等，重复点不会叠加）"""
    try:
        tok = request.cookies.get('token', '')
        who = (_AUTH_TOKENS.get(tok) or {}).get('name') or ''
        with _slider_lock:
            st = _slider_read()
            cur = st.get('task') or {}
            if _slider_eff_status(cur) in _SLIDER_BUSY:
                return success(_slider_view(st), '已有滑块任务在执行，未重复下发')
            seq = int(st.get('seq') or 0) + 1
            st['seq'] = seq
            st['task'] = {
                'id': 's%d' % seq, 'status': 'pending',
                'step': '等待本机滑块助手领取任务…',
                'message': '', 'requestedAt': _fetch_now(), 'requestedBy': who,
                'claimedAt': '', 'finishedAt': '', 'ts': time.time(),
            }
            _slider_write(st)
            print('[滑块] 收到拖滑块请求：%s（来自 %s）' % (st['task']['id'], who or '?'))
            return success(_slider_view(st), '已下发滑块任务，请留意本机弹出的 Chrome')
    except Exception as e:
        traceback.print_exc()
        return fail('下发滑块任务失败：%s' % e)


@app.route('/api/fetch/doudian/slider/pending')
def api_slider_pending():
    """本机助手轮询：顺便当心跳；有 pending 任务就领走并标 claimed

    没有任务时也每 3 秒被调一次，所以心跳放在这里更新最省事。
    """
    if not _slider_key_ok():
        return jsonify({'code': 403, 'msg': '滑块助手密钥无效', 'data': None}), 403
    _slider_agent_seen[0] = time.time()
    _slider_agent_seen[1] = request.args.get('host') or request.headers.get('X-Slider-Host') or ''
    _slider_agent_seen[2] = request.args.get('pid') or ''
    with _slider_lock:
        st = _slider_read()
        t = st.get('task') or {}
        take = None
        if t and _slider_eff_status(t) == 'pending':
            t['status'] = 'claimed'
            t['claimedAt'] = _fetch_now()
            t['step'] = '本机助手已领取，正在打开浏览器…'
            t['ts'] = time.time()
            st['task'] = t
            _slider_write(st)
            take = {'taskId': t['id'], 'action': 'login'}
            print('[滑块] %s 已被本机助手领取' % t['id'])
    return success({'task': take})


@app.route('/api/fetch/doudian/slider/report', methods=['POST'])
def api_slider_report():
    """本机助手回报进度：{taskId, status: running|done|fail, step, message}"""
    if not _slider_key_ok():
        return jsonify({'code': 403, 'msg': '滑块助手密钥无效', 'data': None}), 403
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        data = {}
    want = str(data.get('taskId') or '')
    status = str(data.get('status') or '')
    if status not in ('running', 'done', 'fail'):
        return fail('status 只支持 running / done / fail')
    with _slider_lock:
        st = _slider_read()
        t = st.get('task') or {}
        if not t or (want and t.get('id') != want):
            return fail('任务不存在或已被替换')
        t['status'] = status
        t['step'] = str(data.get('step') or t.get('step') or '')
        t['message'] = str(data.get('message') or '')
        t['ts'] = time.time()
        if status in ('done', 'fail'):
            t['finishedAt'] = _fetch_now()
        st['task'] = t
        _slider_write(st)
        print('[滑块] %s → %s %s' % (t.get('id'), status, t.get('message') or ''))
    return success(_slider_view(st), '已记录')


# ======================== 开发告警：接口与全局异常钩子 ========================
# 三个运维自用接口：
#   POST /api/dev/report-error  前端未捕获 JS 异常上报（Vue 整页白屏那类问题以前没人知道）
#   POST /api/dev/alert/test    自检：立刻给李自豪发一条测试消息，验证告警链路是否通
#   GET  /api/dev/alert/status  查看收件人解析结果（排查「为什么没收到告警」）

@app.route('/api/dev/report-error', methods=['POST'])
def dev_report_error():
    """前端 JS 未捕获异常上报 → 转钉钉告警开发"""
    try:
        d = request.get_json(silent=True) or {}
        msg = (d.get('message') or '').strip()[:500]
        if not msg:
            return success(None, 'ignored')
        detail = ('页面：%s\n位置：%s\n浏览器：%s\n\n%s'
                  % (d.get('page') or '-', d.get('location') or '-',
                     (request.headers.get('User-Agent') or '')[:200],
                     (d.get('stack') or '')[:1000]))
        _dev_alert('前端 JS 异常：%s' % msg[:80], detail=detail,
                   signature='js:%s:%s' % ((d.get('page') or '-'), msg[:80]),
                   source='前端页面')
        return success(None, 'reported')
    except Exception as e:
        return fail(str(e))


@app.route('/api/dev/alert/test', methods=['POST'])
def dev_alert_test():
    """给开发（李自豪）发一条测试告警，验证链路"""
    try:
        row = _dev_recipient(force=True) if _dev_recipient else None
        if not row:
            return fail('没有找到收件人：请先在「每日数据分析 → 钉钉推送」里添加名字含「李自豪」的成员，'
                        '或在服务器 .env 配置 DEV_ALERT_USER_ID')
        queued = _dev_alert('开发告警链路测试',
                            detail='这是后台手动触发的测试消息，收到即代表告警通道正常。',
                            signature='manual:test', source='告警自检', force=True)
        return success({'recipient': row.get('name'),
                        'userId': row.get('user_id') or '(待匹配)',
                        'queued': bool(queued)}, '测试告警已发送')
    except Exception as e:
        return fail(str(e))


@app.route('/api/dev/alert/status', methods=['GET'])
def dev_alert_status():
    """查看告警开关状态与收件人解析结果"""
    try:
        try:
            from dev_alert import DEV_NAME as _dev_name
        except Exception:
            _dev_name = '李自豪'
        row = _dev_recipient(force=True) if _dev_recipient else None
        return success({
            'enabled': bool(globals().get('notify_dev')),
            'devName': _dev_name,
            'recipient': ({'name': row.get('name'), 'userId': row.get('user_id') or '',
                           'mobile': row.get('mobile') or ''} if row else None),
        })
    except Exception as e:
        return fail(str(e))


def _install_dev_alert_hooks():
    """安装线程/主线程未捕获异常钩子

    定时任务全是模块级 daemon 线程，异常平时只写进 journalctl，没人翻等于没有 ——
    这里统一转成钉钉告警。原钩子照常调用，不改变原有行为。
    """
    import sys as _sysmod
    try:
        _prev_thread_hook = getattr(threading, 'excepthook', None)

        def _thread_hook(args):
            try:
                if args.exc_type is not SystemExit and args.exc_value is not None:
                    name = getattr(args.thread, 'name', 'unknown') if args.thread else 'unknown'
                    _dev_alert_exc('后台线程未捕获异常（%s）' % name, args.exc_value,
                                   extra='线程：%s' % name, signature='thread:%s' % name,
                                   source='后台线程')
            except Exception:
                pass
            try:
                if _prev_thread_hook:
                    _prev_thread_hook(args)
            except Exception:
                pass

        threading.excepthook = _thread_hook

        _prev_sys_hook = _sysmod.excepthook

        def _sys_hook(etype, value, tb):
            try:
                if etype is not SystemExit and value is not None:
                    _dev_alert_exc('主线程未捕获异常', value,
                                   signature='main:%s' % getattr(etype, '__name__', 'Error'),
                                   source='主进程')
            except Exception:
                pass
            try:
                _prev_sys_hook(etype, value, tb)
            except Exception:
                pass

        _sysmod.excepthook = _sys_hook
        print('[开发告警] 异常钩子已安装（接口 / 线程 / 主进程 未捕获异常均会告警）')
    except Exception as e:
        print('[开发告警] 安装异常钩子失败: %s' % e)


_install_dev_alert_hooks()


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
