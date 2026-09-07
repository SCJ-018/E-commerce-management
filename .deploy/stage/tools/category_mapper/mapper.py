# -*- coding: utf-8 -*-
"""
商品 → 统一品类 映射脚本
====================================
功能：
  1. 从 抖店/京东/千牛 三张单链接表，按 (平台, 平台ID) 去重，取代表标题
  2. 关键词规则优先分类；规则撞车（0 命中 或 ≥2 命中）交给 DeepSeek
  3. 写入「商品品类映射表」，支持增量（已存在 ID 不动，新 ID 补标）

用法：
  python mapper.py --dry-run     # 只跑规则，看覆盖率，不写库、不调 LLM
  python mapper.py               # 完整跑：规则 + LLM + 写库
  python mapper.py --only-new    # 增量：只处理映射表里还不存在的 ID（默认即增量）

依赖：pymysql, requests（与 backend 一致）
"""

import sys, os, re, json, argparse, datetime
import pymysql
import requests

# ---------------------------------------------------------------------------
# 配置（与 backend/config.py 保持一致，源：backend/config.py）
# ---------------------------------------------------------------------------
# 密钥统一从 backend/config.py 读取（该文件已被 .gitignore 排除，不会进入版本库）
_BACKEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'backend')
sys.path.insert(0, _BACKEND_DIR)
from config import DB_CONFIG, DEEPSEEK_API_KEY, DEEPSEEK_API_URL, DEEPSEEK_MODEL  # noqa: E402

# 平台 -> (表名, 商品ID字段, 标题字段, 日期字段)
PLATFORMS = [
    ('抖店', '抖店单链接数据表', '商品编码', '商品名称', '统计周期'),
    ('京东', '京东单链接数据表', 'SPU', 'SPU名称', '时间'),
    ('千牛', '千牛单链接数据表', '商品ID', '商品名称', '统计日期'),
]

MAPPING_TABLE = '商品品类映射表'

# 品类清单（完整，含特殊与兜底）——LLM 也按这份选
CATEGORY_LIST = [
    # 车品-车内
    '脚垫', '后备箱垫', '座椅垫/座套', '头枕/腰靠', '方向盘套', '手机支架',
    '安全带配件', '车内收纳', '香薰/香水', '摆件/装饰',
    # 车品-车外/养护
    '遮阳/防晒', '车身防护', '洗车清洁', '养护/修复', '车衣/车罩', '玻璃贴膜', '轮胎/应急',
    # 车品-电子
    '车用电子',
    # 非车品
    '保温杯/水杯', '内衣/服饰', '安防/探测', '个护/小家电',
    # 特殊 + 兜底
    '补差价链接', '其他',
]

# 关键词规则：品类 -> 强关键词列表（命中任一即视为该品类候选）
# 只放“高精准”关键词；标题命中 0 个或 ≥2 个品类时交给 LLM 裁决
RULES = {
    # 车品-车内
    '脚垫': ['脚垫', '地垫', '包门槛', '门槛条', '丝圈'],
    '后备箱垫': ['后备箱垫', '尾箱垫'],
    '座椅垫/座套': ['坐垫', '座套', '座垫', '椅套', '汽车座垫'],
    '头枕/腰靠': ['头枕', '颈枕', '腰靠', '靠垫', '腰垫', '护颈枕', '抱枕', '靠枕'],
    '方向盘套': ['方向盘套', '把套', '盘套'],
    '手机支架': ['手机支架', '手机架', '导航支架', '车载支架'],
    '安全带配件': ['安全带', '护肩套', '限位器'],
    '车内收纳': ['收纳', '置物', '储物', '纸巾盒', '垃圾桶', '杯架', '眼镜盒',
                '烟灰缸', '驾驶证', '行驶证', '驾照'],
    '香薰/香水': ['香薰', '香水', '香氛', '香片', '香膏', '车载香', '香座', '除味', '除臭',
                '空气清新', '竹炭包', '除甲醛', '活性炭'],
    '摆件/装饰': ['摆件', '挂件', '车贴', '贴纸', '拉花', '车标', '财神',
                '钥匙扣', '钥匙套', '钥匙链', '钥匙圈'],
    # 车品-车外/养护
    '遮阳/防晒': ['遮阳', '防晒', '遮光', '遮阳挡', '遮阳帘', '遮阳伞', '防蚊', '蚊帐',
                '遮雪挡', '太阳挡'],
    '车身防护': ['防撞条', '密封条', '防刮', '门碗', '门把手', '保险杠', '防撞贴', '门角',
                '防开门杀', '隔音', '防虫网', '防护网', '防飞絮'],
    '洗车清洁': ['洗车', '擦车', '毛巾', '抹布', '拖把', '掸子', '泡沫', '刷子', '海绵',
                '水枪', '喷壶', '除胶剂', '清洁剂', '刮水器', '玻璃水', '洗车液'],
    '养护/修复': ['护理', '镀膜', '表板蜡', '补漆', '划痕修复', '抛光', '打蜡', '车窗润滑',
                '抽油', '内饰清洁', '内饰护理', '内饰镀膜', '雨刮', '雨刷', '雨刮器', '雨刷器'],
    '车衣/车罩': ['车衣', '车罩', '防冻罩'],
    '玻璃贴膜': ['贴膜', '玻璃膜', '防爆膜', '车窗膜'],
    '轮胎/应急': ['轮胎', '补胎', '充气泵', '打气泵', '胎压', '气门嘴', '安全锤', '灭火器',
                '搭电', '电瓶', '拖车绳', '警示', '应急', '破窗器', '酒精测试', '防火罩',
                '三角架', '三角警示'],
    # 车品-电子
    '车用电子': ['充电', '数据线', '点烟器', '记录仪', '倒车影像', '车载风扇', '氛围灯',
                '指南针', '车载时钟', '投影', '车载充电'],
    # 非车品
    '保温杯/水杯': ['保温杯', '水杯', '钛杯', '吸管杯', '焖茶', '茶杯', '保温壶'],
    '内衣/服饰': ['内衣', '文胸', '抹胸', '吊带', '无钢圈', '聚拢', '桑蚕丝'],
    '安防/探测': ['探测仪', '防偷拍', '反偷拍', '探测器', '防窥'],
    '个护/小家电': ['剃须刀', '刮胡刀', '吹风机', '电动牙刷'],
}

# 补差价/虚拟链接关键词：命中即直接判为「补差价链接」，优先级最高
SPECIAL_LINK_KEYWORDS = ['补差价', '补运费', '补余款', '补邮费', '荭包']

# 排除词：标题命中这些词时，跳过对应品类（解决组合套装的主品类归属）
# 例：「脚垫+后备箱垫套装」主品类是脚垫 → 后备箱垫被排除
# 例：「车载香薰摆件」主品类是香薰 → 摆件被排除
EXCLUDE = {
    '后备箱垫': ['脚垫', '地垫'],
    '摆件/装饰': ['香薰', '香水', '香氛', '车载香'],
}

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def get_conn():
    return pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)


def ensure_table(conn):
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{MAPPING_TABLE}` (
      `平台` VARCHAR(16) NOT NULL,
      `平台商品ID` VARCHAR(64) NOT NULL,
      `统一品类` VARCHAR(32) NOT NULL,
      `商品名称快照` VARCHAR(255) NOT NULL,
      `归类方式` VARCHAR(16) NOT NULL COMMENT '规则/LLM/人工',
      `归类时间` DATETIME NOT NULL,
      `更新时间` DATETIME NOT NULL,
      PRIMARY KEY (`平台`, `平台商品ID`),
      KEY `idx_品类` (`统一品类`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品→统一品类映射表';
    """
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def load_unique_products(conn):
    """按 (平台, 平台ID) 去重，取每 ID 代表标题（最新日期，同日多标题取最长）。
    返回 dict[(平台, 平台ID)] = 标题"""
    result = {}
    with conn.cursor() as cur:
        for platform, table, id_col, title_col, date_col in PLATFORMS:
            sql = f"""
            SELECT t.`{id_col}` AS pid, t.`{title_col}` AS title
            FROM `{table}` t
            INNER JOIN (
                SELECT `{id_col}` AS pid, MAX(`{date_col}`) AS md
                FROM `{table}` GROUP BY `{id_col}`
            ) m ON t.`{id_col}` = m.pid AND t.`{date_col}` = m.md
            """
            cur.execute(sql)
            for row in cur.fetchall():
                key = (platform, str(row['pid']))
                title = row['title'] or ''
                if key not in result or len(title) > len(result[key]):
                    result[key] = title
    return result


def load_existing_ids(conn):
    """已映射的 (平台, 平台ID) 集合"""
    with conn.cursor() as cur:
        cur.execute(f"SELECT `平台`, `平台商品ID` FROM `{MAPPING_TABLE}`")
        return {(r['平台'], str(r['平台商品ID'])) for r in cur.fetchall()}


def classify_by_rules(title):
    """返回 品类名；若命中 0 或 ≥2 个品类则返回 None（交给 LLM）"""
    tl = title.lower()
    # 补差价链接优先级最高
    if any(k in tl for k in SPECIAL_LINK_KEYWORDS):
        return '补差价链接'
    hits = []
    for cat, kws in RULES.items():
        # 排除词：命中则跳过该品类（解决组合套装的主品类归属）
        if any(e.lower() in tl for e in EXCLUDE.get(cat, [])):
            continue
        for kw in kws:
            if kw.lower() in tl:
                hits.append(cat)
                break
    if len(hits) == 1:
        return hits[0]
    return None


# ---------------------------------------------------------------------------
# DeepSeek 兜底分类
# ---------------------------------------------------------------------------
def _call_deepseek(system_prompt, user_message):
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
        'temperature': 0.0,
        'max_tokens': 4096,
    }
    for attempt in range(3):
        try:
            resp = requests.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=120)
            if resp.status_code == 200:
                return resp.json()['choices'][0]['message']['content']
            print(f'  [LLM] HTTP {resp.status_code} retry {attempt+1}')
        except Exception as e:
            print(f'  [LLM] error retry {attempt+1}: {e}')
        time.sleep(2 * (attempt + 1))
    return None


def classify_by_llm(titles):
    """批量分类。titles: list[(index, title)]，返回 {index: 品类名}"""
    if not titles:
        return {}
    cat_lines = '\n'.join(f'{i} {c}' for i, c in enumerate(CATEGORY_LIST))
    sys_prompt = (
        '你是电商商品分类助手。请把每个商品标题归类到【唯一】一个品类。\n'
        '品类列表（只能从中选一个，用编号对应）：\n' + cat_lines + '\n\n'
        '规则：\n'
        '1. 每个标题只归【一个】品类，选最贴切、最能代表主卖点的那个，禁止重复归类。\n'
        '2. 若标题是「补差价/补运费/补余款/拍多少」等虚拟链接，归「补差价链接」。\n'
        '3. 车品优先判断为车品类；保温杯/内衣/探测仪/剃须刀等非车品按其本身归入对应非车品类。\n'
        '4. 实在无法判断的归「其他」。\n'
        '只输出一个 JSON 对象，格式 {"0":"脚垫","1":"后备箱垫"}；必须用上面列表里的确切品类名，'
        '编号与标题一一对应、不要漏、不要加额外文字、不要用 markdown 代码块。'
    )
    # 分块，每块最多 20 条（更稳）
    batch = 20
    result = {}
    items = list(titles)
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        lines = '\n'.join(f'{i}: {t}' for i, t in chunk)
        user_msg = '商品标题列表（编号: 标题）：\n' + lines
        raw = _call_deepseek(sys_prompt, user_msg)
        parsed = _parse_json(raw)
        if not parsed:
            # 解析失败：记录原始返回，重试一次
            with open(os.path.join(OUT_DIR, 'LLM解析失败.log'), 'a', encoding='utf-8') as f:
                f.write(f'--- batch start={start} ---\n{raw}\n\n')
            raw = _call_deepseek(sys_prompt, user_msg)
            parsed = _parse_json(raw)
        for idx, title in chunk:
            cat = _match_cat(parsed.get(str(idx)))
            result[idx] = cat if cat else '其他'
        print(f'  [LLM] 已处理 {min(start + batch, len(items))}/{len(items)} 条')
    return result


def _norm_cat(s):
    """归一化品类名，便于模糊匹配"""
    return str(s).replace('／', '/').replace(' ', '').replace('　', '').strip()

_NORM_MAP = {_norm_cat(c): c for c in CATEGORY_LIST}


def _match_cat(raw):
    """把 LLM 返回的品类值（可能是名字/编号/带空格或全角斜杠）映射到标准品类名，失败返回 None"""
    if raw is None:
        return None
    s = str(raw).strip()
    if s in CATEGORY_LIST:
        return s
    # 数字编号
    if s.isdigit() and 0 <= int(s) < len(CATEGORY_LIST):
        return CATEGORY_LIST[int(s)]
    n = _norm_cat(s)
    if n in _NORM_MAP:
        return _NORM_MAP[n]
    # 子串模糊：谁包含谁
    for c in CATEGORY_LIST:
        nc = _norm_cat(c)
        if nc and (nc in n or n in nc):
            return c
    return None


def _parse_json(raw):
    if not raw:
        return {}
    raw = raw.strip()
    raw = re.sub(r'```(?:json)?', '', raw)
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        frag = m.group(0)
        try:
            return json.loads(frag)
        except Exception:
            try:
                # 去掉尾逗号再试
                return json.loads(re.sub(r',\s*([}\]])', r'\1', frag))
            except Exception:
                return {}
    return {}


# ---------------------------------------------------------------------------
# 重分类「其他」行
# ---------------------------------------------------------------------------
def reclassify_uncategorized(conn):
    """把映射表中「其他」的行重新走一遍（规则优先，LLM 兜底），并 UPDATE 回库"""
    with conn.cursor() as cur:
        cur.execute(f"SELECT `平台`,`平台商品ID`,`商品名称快照` FROM `{MAPPING_TABLE}` WHERE `统一品类`='其他'")
        rows = cur.fetchall()
    items = {(r['平台'], str(r['平台商品ID'])): r['商品名称快照'] for r in rows}
    print(f'其他待重分类={len(items)}')
    if not items:
        return

    rule_hit = {}
    unresolved = {}
    for key, title in items.items():
        cat = classify_by_rules(title)
        if cat and cat != '其他':
            rule_hit[key] = cat
        else:
            unresolved[key] = title

    llm_result = {}
    if unresolved:
        idx_map = {i: key for i, key in enumerate(unresolved.keys())}
        titles = [(i, unresolved[key]) for i, key in enumerate(unresolved.keys())]
        llm_index = classify_by_llm(titles)
        for i, key in idx_map.items():
            llm_result[key] = llm_index.get(i, '其他')

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with conn.cursor() as cur:
        sql = (f"UPDATE `{MAPPING_TABLE}` SET `统一品类`=%s, `归类方式`=%s, `更新时间`=%s "
               f"WHERE `平台`=%s AND `平台商品ID`=%s")
        for key, title in items.items():
            plat, pid = key
            cat = rule_hit.get(key) or llm_result.get(key) or '其他'
            method = '规则' if key in rule_hit else 'LLM'
            cur.execute(sql, (cat, method, now, plat, pid))
    conn.commit()
    print(f'重分类完成，更新 {len(items)} 条')


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='只跑规则看覆盖率，不写库不调 LLM')
    ap.add_argument('--reclassify', action='store_true', help='重分类映射表中「其他」的行')
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    conn = get_conn()
    ensure_table(conn)

    if args.reclassify:
        reclassify_uncategorized(conn)
        conn.close()
        return

    products = load_unique_products(conn)
    existing = load_existing_ids(conn)
    new_products = {k: v for k, v in products.items() if k not in existing}
    print(f'唯一商品总数={len(products)}  已映射={len(existing)}  待处理={len(new_products)}')

    # 1) 规则分类
    rule_matched = {}   # key -> 品类
    unresolved = {}     # key -> 标题
    for key, title in new_products.items():
        cat = classify_by_rules(title)
        if cat:
            rule_matched[key] = cat
        else:
            unresolved[key] = title

    print(f'规则命中={len(rule_matched)}  LLM待分类={len(unresolved)}')

    # 品类分布（规则部分）
    from collections import Counter
    dist = Counter(rule_matched.values())
    with open(os.path.join(OUT_DIR, '规则分布.txt'), 'w', encoding='utf-8') as f:
        f.write('规则命中品类分布：\n')
        for cat, n in dist.most_common():
            f.write(f'{cat}\t{n}\n')

    # 未决标题落盘，供检查
    with open(os.path.join(OUT_DIR, '未决标题.txt'), 'w', encoding='utf-8') as f:
        f.write(f'待 LLM 分类的标题数={len(unresolved)}\n平台\tID\t标题\n')
        for (plat, pid), title in sorted(unresolved.items()):
            f.write(f'{plat}\t{pid}\t{title}\n')

    if args.dry_run:
        print('dry-run 结束，未写库、未调 LLM。详见 output/ 目录')
        conn.close()
        return

    # 2) LLM 兜底
    llm_result = {}
    if unresolved:
        idx_map = {i: key for i, key in enumerate(unresolved.keys())}
        titles = [(i, unresolved[key]) for i, key in enumerate(unresolved.keys())]
        llm_index = classify_by_llm(titles)
        for i, key in idx_map.items():
            llm_result[key] = llm_index.get(i, '其他')
    print(f'LLM 完成，兜底 {len(llm_result)} 条')

    # 3) 写库
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    inserted = 0
    with conn.cursor() as cur:
        sql = (f"INSERT INTO `{MAPPING_TABLE}` "
               f"(`平台`,`平台商品ID`,`统一品类`,`商品名称快照`,`归类方式`,`归类时间`,`更新时间`) "
               f"VALUES (%s,%s,%s,%s,%s,%s,%s) "
               f"ON DUPLICATE KEY UPDATE `统一品类`=VALUES(`统一品类`),"
               f"`商品名称快照`=VALUES(`商品名称快照`),`归类方式`=VALUES(`归类方式`),"
               f"`更新时间`=VALUES(`更新时间`)")
        for key, title in new_products.items():
            plat, pid = key
            cat = rule_matched.get(key) or llm_result.get(key) or '其他'
            method = '规则' if key in rule_matched else 'LLM'
            cur.execute(sql, (plat, pid, cat, title[:255], method, now, now))
            inserted += 1
    conn.commit()
    print(f'写入完成，共 {inserted} 条')

    # 汇总
    final_dist = Counter(list(rule_matched.values()) + list(llm_result.values()))
    with open(os.path.join(OUT_DIR, '映射汇总.txt'), 'w', encoding='utf-8') as f:
        f.write(f'本次新增映射={inserted}\n')
        f.write('最终品类分布：\n')
        for cat in CATEGORY_LIST:
            f.write(f'{cat}\t{final_dist.get(cat, 0)}\n')
    # 抽样
    with open(os.path.join(OUT_DIR, '映射抽样.txt'), 'w', encoding='utf-8') as f:
        f.write('抽样 200 条映射结果：\n平台\tID\t品类\t标题\n')
        for key, title in list(new_products.items())[:200]:
            plat, pid = key
            cat = rule_matched.get(key) or llm_result.get(key) or '其他'
            f.write(f'{plat}\t{pid}\t{cat}\t{title}\n')
    conn.close()
    print('全部完成，结果见 tools/category_mapper/output/')


if __name__ == '__main__':
    main()
