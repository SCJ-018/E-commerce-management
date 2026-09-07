# -*- coding: utf-8 -*-
import app

print('=== 开始爱搜抓取 + 写库 ===')
n = app._aisou_enrich(['拼豆'])
print('INSERTED', n)

print('=== 爱搜数据表最新数据（词名称非空，按日期倒序）===')
rows = app.db_execute(
    "SELECT `日期`, `来源词`, `词类型`, `词名称`, `月覆盖人次`, `七日搜索人次` "
    "FROM `爱搜数据表` WHERE `词名称` IS NOT NULL ORDER BY `日期` DESC, `词类型` LIMIT 50")
print('共', len(rows), '条')
for r in rows:
    print(r['日期'], '|', r['来源词'], '|', r['词类型'], '|', r['词名称'], '|', r['月覆盖人次'], '|', r['七日搜索人次'])
