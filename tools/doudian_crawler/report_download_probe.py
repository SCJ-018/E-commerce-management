# -*- coding: utf-8 -*-
"""单店单日报表下载探针。

只验证罗盘两个「下载明细」入口，不写数据库：
  1. 全店成交分析 -> 成交概览第二行
  2. 商品卡列表（用户称营销/单链接表）-> 下载文件各 sheet 前两行

服务器没有 openpyxl 时，用标准库直接读取 xlsx XML，避免为了探针安装依赖。
"""
import sys
import os
import re
import time
import json
import zipfile
from xml.etree import ElementTree as ET

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import shops  # noqa: E402
from login_fetch_all import WORKBENCH, switch_shop, current_shop  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

DATE = sys.argv[1] if len(sys.argv) > 1 else '2026-09-23'
TARGET = sys.argv[2] if len(sys.argv) > 2 else '御车宝周口驰为网络科技有限公司专卖店'
DL_DIR = os.path.join(BASE_DIR, '_downloads')
os.makedirs(DL_DIR, exist_ok=True)

DATE_VALUE = '1790092800'  # 2026-09-23 00:00:00 Asia/Shanghai
TRADE_URL = (
    'https://compass.jinritemai.com/shop/business-part'
    '?defaultVisualType=all&date_type=20&date_value=%s%%2C%s'
    '&from_page=%%2Fshop' % (DATE_VALUE, DATE_VALUE)
)
TRAFFIC_URL = (
    'https://compass.jinritemai.com/shop/merchandise-traffic'
    '?from_page=%2Fshop%2Fbusiness-part'
)

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'pr': 'http://schemas.openxmlformats.org/package/2006/relationships'}


def _cell_value(cell):
    # 抖店导出的文件通常是 t="str" + <v>，也兼容 inlineStr 和数字。
    v = cell.find('m:v', NS)
    if v is not None:
        return v.text or ''
    inline = cell.find('m:is', NS)
    if inline is not None:
        return ''.join(t.text or '' for t in inline.findall('.//m:t', NS))
    return ''


def read_xlsx_rows(path, max_rows=3):
    """返回 [(sheet_name, [[row...], ...]), ...]。"""
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read('xl/workbook.xml'))
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        relmap = {}
        for rel in rels.findall('pr:Relationship', NS):
            relmap[rel.attrib['Id']] = rel.attrib['Target'].lstrip('/')
        out = []
        for sheet in wb.findall('m:sheets/m:sheet', NS):
            name = sheet.attrib.get('name', '')
            rid = sheet.attrib.get('{%s}id' % NS['r'])
            target = relmap.get(rid, '')
            if target and not target.startswith('xl/'):
                target = 'xl/' + target
            if not target or target not in z.namelist():
                continue
            root = ET.fromstring(z.read(target))
            rows = []
            for row in root.findall('.//m:sheetData/m:row', NS)[:max_rows]:
                cells = row.findall('m:c', NS)
                values = [_cell_value(c) for c in cells]
                rows.append(values)
            out.append((name, rows))
        return out


def _visible_count(page, text):
    n = 0
    for loc in page.locator('text=%s' % text).all():
        try:
            if loc.is_visible():
                n += 1
        except Exception:
            pass
    return n


def click_near_one_day(page):
    loc = page.locator('text=近1天')
    count = loc.count()
    for i in range(count):
        try:
            item = loc.nth(i)
            if item.is_visible():
                item.click()
                time.sleep(3)
                print('  已点击「近1天」')
                return True
        except Exception:
            continue
    print('  [warn] 页面未找到可见「近1天」，继续使用 URL/默认日期')
    return False


def download_detail(page, url, tag):
    print('\n[%s] 打开 %s' % (tag, url))
    page.goto(url, wait_until='domcontentloaded', timeout=60000)
    time.sleep(10)
    click_near_one_day(page)
    print('  页面:', page.url)
    btn = page.get_by_text('下载明细', exact=True)
    print('  下载明细按钮:', btn.count())
    if btn.count() == 0:
        page.screenshot(path=os.path.join(BASE_DIR, '_report_%s_no_button.png' % tag))
        return None
    try:
        with page.expect_download(timeout=120000) as di:
            btn.first.click()
            # 全店成交分析的「下载明细」是下拉入口，第一次点击只展开菜单；
            # 真正触发导出的是「下载当前明细」。商品卡列表通常直接下载，
            # 所以仅在菜单项出现时补点一次。
            current = page.get_by_text('下载当前明细', exact=True)
            if current.count() > 0:
                current.first.click()
        dl = di.value
        safe = re.sub(r'[^0-9A-Za-z_.-]+', '_', dl.suggested_filename)
        path = os.path.join(DL_DIR, '%s_%s' % (tag, safe))
        dl.save_as(path)
        print('  已下载:', path, '大小=', os.path.getsize(path))
        return path
    except Exception as e:
        print('  [FAIL] 下载异常:', str(e)[:300])
        page.screenshot(path=os.path.join(BASE_DIR, '_report_%s_download_fail.png' % tag))
        return None


def dump(path):
    if not path:
        return
    print('\n===== 解析 %s =====' % os.path.basename(path))
    for name, rows in read_xlsx_rows(path):
        print('SHEET=%s rows=%d' % (name, len(rows)))
        for i, row in enumerate(rows, 1):
            print('  ROW%d=%s' % (i, json.dumps(row, ensure_ascii=False)))


acc = shops.get_email_account() or {}
state = acc.get('state')
if not state:
    print('[FAIL] DB 中没有抖店登录态')
    sys.exit(2)
tmp_state = os.path.join(BASE_DIR, '_report_probe_state.json')
with open(tmp_state, 'w', encoding='utf-8') as f:
    json.dump(state, f, ensure_ascii=False)

with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome', headless=False,
                                args=['--disable-blink-features=AutomationControlled',
                                      '--no-first-run', '--no-default-browser-check', '--no-sandbox'])
    ctx = browser.new_context(storage_state=tmp_state, locale='zh-CN',
                              timezone_id='Asia/Shanghai', viewport={'width': 1600, 'height': 950},
                              accept_downloads=True)
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    page = ctx.new_page()
    page.goto(WORKBENCH, wait_until='domcontentloaded', timeout=60000)
    time.sleep(12)
    print('[登录] 当前页面:', page.url, '| 当前店:', current_shop(page) or '<读不到>')
    if '/login' in page.url or 'passport' in page.url:
        print('[FAIL] 登录态失效，探针不尝试重新登录')
        browser.close()
        sys.exit(3)
    cur = current_shop(page) or ''
    if TARGET[:6] not in cur:
        print('[切店] %s -> %s' % (cur or '<读不到>', TARGET))
        if not switch_shop(page, TARGET, cur):
            print('[FAIL] 切店失败')
            browser.close()
            sys.exit(4)
    print('[切店] 成功，当前店:', current_shop(page) or '<读不到>')
    f_trade = download_detail(page, TRADE_URL, 'trade_%s' % DATE)
    f_traffic = download_detail(page, TRAFFIC_URL, 'traffic_%s' % DATE)
    browser.close()

dump(f_trade)
dump(f_traffic)
print('\n[完成] 仅下载和解析，未写数据库。')
