# -*- coding: utf-8 -*-
"""
临时脚本：把 tab 分隔的抖店单链接数据导入「抖店单链接数据表」
数据文件：_tmp_douyin_data.tsv（每行 49 个字段，与表字段顺序一致）
处理规则：
  - '数据暂未更新' -> 0（对应第 38~44 列数值字段）
  - 统计周期 '2026/8/20' -> '2026-08-20'
  - '全部' 原样保留
  - 使用 INSERT ... ON DUPLICATE KEY UPDATE 幂等写入
"""
import sys
from datetime import datetime
import pymysql

COLUMNS = [
    '店铺名', '统计周期', '商品名称', '商品编码', '载体', '品类', '自卖/合作',
    '成交金额', '结算金额', '用户支付金额', '成交订单数', '成交人数', '成交件数', '成交客单价',
    '商品结算金额（结算时间）', '实际佣金支出', '净成交金额', '净成交订单数', '成交退款金额',
    '退款金额(退款时间)', '退款订单数(退款时间)', '退款人数(退款时间)', '退款件数(退款时间)',
    '商品曝光人数', '商品曝光次数', '商品点击人数', '商品点击次数', '曝光点击率（人数）',
    '投放消耗（店铺被投）', '投放消耗（推商品）', '投放贡献成交金额', '投放贡献成交退款金额',
    '投放费比（剔除退款、店铺被投）', '加购人数', '收藏人数', '评价好评率', '好评数',
    '商品差评订单数', '商品差评率', '商品品质退货单量', '商品品质退货率', '投诉工单量', '投诉率',
    '客服不满意会话量', '商详页曝光人数', '商详页曝光点击率', '商详页成交转化率', '商详页跳失率',
    '平台消费券补贴金额',
]
assert len(COLUMNS) == 49, f"列数错误: {len(COLUMNS)}"

DATA_FILE = r'D:\代码\电商后台管理\backend\_tmp_douyin_data.tsv'

DB = dict(host='192.168.2.10', port=3306, user='root', password='123456',
          database='数据', charset='utf8mb4')


def transform(field, idx):
    s = field.strip()
    if s == '数据暂未更新':
        return '0'
    if idx == 1:  # 统计周期
        for fmt in ('%Y/%m/%d', '%Y-%m-%d'):
            try:
                return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
            except ValueError:
                continue
        return s
    return s


def main():
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        lines = [ln for ln in f.read().split('\n') if ln.strip()]

    rows = []
    bad = []
    for i, ln in enumerate(lines, 1):
        fields = ln.split('\t')
        if len(fields) != 49:
            bad.append((i, len(fields), ln[:80]))
            continue
        rows.append([transform(f, j) for j, f in enumerate(fields)])

    print(f'共 {len(lines)} 行，有效 {len(rows)} 行，字段数异常 {len(bad)} 行')
    for b in bad[:20]:
        print('  异常行:', b)

    if bad:
        print('存在字段数异常的行，先修复再导入。')
        sys.exit(1)

    cols = ', '.join(f'`{c}`' for c in COLUMNS)
    ph = ', '.join(['%s'] * 49)
    upd = ', '.join(f'`{c}`=VALUES(`{c}`)' for c in COLUMNS)
    sql = f'INSERT INTO `抖店单链接数据表` ({cols}) VALUES ({ph}) ON DUPLICATE KEY UPDATE {upd}'

    conn = pymysql.connect(**DB, connect_timeout=5, read_timeout=60, write_timeout=60)
    cur = conn.cursor()
    inserted = 0
    try:
        for r in rows:
            cur.execute(sql, r)
            inserted += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        print('导入出错:', repr(e))
        raise
    finally:
        cur.close()
        conn.close()
    print(f'已写入 {inserted} 行到 抖店单链接数据表')


if __name__ == '__main__':
    main()
