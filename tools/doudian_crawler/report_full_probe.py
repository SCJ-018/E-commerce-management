# -*- coding: utf-8 -*-
"""复刻影刀「指标配置 -> 下载当前明细」的单店验证探针。

只下载并检查 Excel，不写数据库。默认测试 2026-09-23 的御车宝店。
重点验证商品列表在打开「指标配置」并选中全部可用指标后，导出的 sheet1
是否包含完整字段和完整商品行。
"""
import sys
import os
import re
import time
import json
import zipfile
import datetime
from xml.etree import ElementTree as ET

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import shops  # noqa: E402
from login_fetch_all import WORKBENCH, switch_shop, current_shop  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

DATE = sys.argv[1] if len(sys.argv) > 1 else '2026-09-23'
TARGET = sys.argv[2] if len(sys.argv) > 2 else '御车宝周口驰为网络科技有限公司专卖店'
DATE_DEBUG = '--date-debug' in sys.argv
PRODUCT_LIST_MODE = '--product-list' in sys.argv
DATE_VALUE = str(int(datetime.datetime.strptime(DATE, '%Y-%m-%d').replace(
    tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp()))
DL_DIR = os.path.join(BASE_DIR, '_downloads')
os.makedirs(DL_DIR, exist_ok=True)

TRAFFIC_URL = (
    'https://compass.jinritemai.com/shop/merchandise-traffic'
    '?from_page=%%2Fshop%%2Fbusiness-part&date_type=20&date_value=%s%%2C%s'
    % (DATE_VALUE, DATE_VALUE)
)
PRODUCT_LIST_URL = (
    'https://compass.jinritemai.com/shop/commodity/product-list'
    '?from_page=%%2Fshop%%2Fsettlement-analysis'
)
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'pr': 'http://schemas.openxmlformats.org/package/2006/relationships'}


def read_xlsx(path):
    """标准库读取 xlsx，返回每个 sheet 的行列表。"""
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read('xl/workbook.xml'))
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        relmap = {x.attrib['Id']: x.attrib['Target'].lstrip('/')
                  for x in rels.findall('pr:Relationship', NS)}
        out = []
        for sheet in wb.findall('m:sheets/m:sheet', NS):
            name = sheet.attrib.get('name', '')
            target = relmap.get(sheet.attrib.get('{%s}id' % NS['r']), '')
            if target and not target.startswith('xl/'):
                target = 'xl/' + target
            if not target or target not in z.namelist():
                continue
            root = ET.fromstring(z.read(target))
            rows = []
            for row in root.findall('.//m:sheetData/m:row', NS):
                vals = []
                for cell in row.findall('m:c', NS):
                    v = cell.find('m:v', NS)
                    if v is not None:
                        vals.append(v.text or '')
                    else:
                        inline = cell.find('m:is', NS)
                        vals.append(''.join(t.text or '' for t in inline.findall('.//m:t', NS))
                                    if inline is not None else '')
                rows.append(vals)
            out.append((name, rows))
        return out


def visible_text_count(page, text):
    n = 0
    loc = page.get_by_text(text, exact=True)
    for i in range(loc.count()):
        try:
            if loc.nth(i).is_visible():
                n += 1
        except Exception:
            pass
    return n


def click_near_one_day(page):
    loc = page.get_by_text('近1天', exact=True)
    for i in range(loc.count()):
        try:
            if loc.nth(i).is_visible():
                loc.nth(i).click()
                time.sleep(3)
                print('  已点击「近1天」')
                return True
        except Exception:
            pass
    print('  [warn] 未找到「近1天」，继续使用当前日期状态')
    return False


def date_debug(page):
    """点击商品列表的「自然日」，保存弹层截图并打印控件信息。"""
    loc = page.get_by_text('自然日', exact=True)
    print('  自然日控件数量:', loc.count())
    for i in range(loc.count()):
        try:
            if loc.nth(i).is_visible():
                loc.nth(i).click()
                break
        except Exception:
            pass
    time.sleep(2)
    page.screenshot(path=os.path.join(BASE_DIR, '_report_date_debug.png'))
    info = page.evaluate(r'''() => ({
      url: location.href,
      text: (document.body.innerText || '').slice(0, 5000),
      inputs: Array.from(document.querySelectorAll('input')).map(x => ({value:x.value, placeholder:x.placeholder, cls:x.className})).slice(0, 30),
      buttons: Array.from(document.querySelectorAll('button')).map(x => (x.innerText || '').trim()).filter(Boolean).slice(0, 80),
      dateCells: Array.from(document.querySelectorAll('td')).map(x => ({title:x.getAttribute('title'), cls:x.className, text:(x.innerText||'').trim()})).filter(x => x.title || x.text).slice(-100)
    })''')
    print('  日期弹层信息:', json.dumps(info, ensure_ascii=False))


def click_exact_natural_day(page, date_text):
    """切到「自然日」并点击指定日期，避免“近1天”受服务器当前日期影响。"""
    loc = page.get_by_text('自然日', exact=True)
    opened = False
    for i in range(loc.count()):
        try:
            if loc.nth(i).is_visible():
                loc.nth(i).click()
                opened = True
                break
        except Exception:
            pass
    if not opened:
        print('  [warn] 未找到「自然日」控件')
        return False
    time.sleep(1)
    # Aurora 日历单元格带 title=YYYY-MM-DD；只点可见且属于当前面板的单元格。
    candidates = page.locator('td[title="%s"]' % date_text)
    for i in range(candidates.count()):
        try:
            if candidates.nth(i).is_visible():
                candidates.nth(i).click()
                time.sleep(3)
                print('  已选择自然日:', date_text)
                return True
        except Exception:
            pass
    # 备用：有些版本使用 data-date 或按钮文本。
    fallback = page.locator('[data-date="%s"], [data-value="%s"]' % (date_text, date_text))
    for i in range(fallback.count()):
        try:
            if fallback.nth(i).is_visible():
                fallback.nth(i).click()
                time.sleep(3)
                print('  已通过备用属性选择自然日:', date_text)
                return True
        except Exception:
            pass
    print('  [warn] 日历中未找到目标日期:', date_text)
    return False


def checkbox_snapshot(page):
    return page.evaluate(r'''() => {
      const els = Array.from(document.querySelectorAll('label, [role="checkbox"], span'))
        .filter(e => ((e.className || '').toString().toLowerCase().includes('checkbox')));
      return els.map(e => {
        const c = (e.className || '').toString();
        const txt = (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
        return {tag:e.tagName, cls:c.slice(0,120), text:txt.slice(0,80),
                checked:e.getAttribute('aria-checked'), disabled:e.getAttribute('aria-disabled')};
      }).filter(x => x.text || x.checked || x.disabled).slice(0, 500);
    }''')


def select_all_metrics(page):
    """优先点可见「全选」，否则逐个点未选中的 checkbox label。"""
    print('  打开指标配置...')
    cfg = page.get_by_text('指标配置', exact=True)
    if cfg.count() == 0:
        raise RuntimeError('未找到指标配置按钮')
    cfg.first.click()
    time.sleep(2)
    page.screenshot(path=os.path.join(BASE_DIR, '_report_full_config_before.png'))
    snap = checkbox_snapshot(page)
    print('  指标配置 checkbox 快照数量:', len(snap))
    print('  快照样例:', json.dumps(snap[:25], ensure_ascii=False))

    # 某些版本提供全选，先使用页面原生按钮，避免遗漏虚拟列表中的项。
    select = page.get_by_text('全选', exact=True)
    clicked_select = 0
    for i in range(select.count()):
        try:
            if select.nth(i).is_visible():
                select.nth(i).click()
                clicked_select += 1
                time.sleep(1)
        except Exception:
            pass

    if not clicked_select:
        # 兼容没有全选按钮的版本：只点击可见、未禁用、当前未选中的 label。
        labels = page.locator('label')
        clicked = 0
        for i in range(labels.count()):
            label = labels.nth(i)
            try:
                if not label.is_visible():
                    continue
                text = (label.inner_text() or '').strip()
                cls = (label.get_attribute('class') or '').lower()
                if 'checkbox' not in cls and label.locator('[class*="checkbox"]').count() == 0:
                    continue
                if 'disabled' in cls or 'checked' in cls:
                    continue
                label.click()
                clicked += 1
            except Exception:
                pass
        print('  逐项点击未选指标:', clicked)
    else:
        print('  已点击全选按钮:', clicked_select)

    time.sleep(2)
    snap_after = checkbox_snapshot(page)
    print('  选中后 checkbox 快照数量:', len(snap_after))
    print('  选中后样例:', json.dumps(snap_after[:25], ensure_ascii=False))
    page.screenshot(path=os.path.join(BASE_DIR, '_report_full_config_after.png'))

    ok = page.get_by_text('确定', exact=True)
    if ok.count() == 0:
        raise RuntimeError('指标配置抽屉中未找到确定按钮')
    ok.first.click()
    time.sleep(4)
    print('  指标配置已确认')


def download(page):
    btn = page.get_by_text('下载明细', exact=True)
    if btn.count() == 0:
        raise RuntimeError('未找到商品列表下载明细按钮')
    # 商品列表的按钮先展开菜单；不要直接点击 DOM 中不可见的同名菜单项。
    # 某些版本则是第一次点击就直接下载，因此先兼容两种行为。
    direct = None
    try:
        with page.expect_download(timeout=8000) as di:
            btn.first.click()
        direct = di.value
    except Exception:
        # expect_download 超时通常表示第一次点击只是展开了下拉菜单，菜单仍保持打开。
        pass
    if direct is not None:
        dl = direct
    else:
        time.sleep(1)
        current = page.get_by_text('下载当前明细', exact=True)
        visible = []
        for i in range(current.count()):
            try:
                if current.nth(i).is_visible():
                    visible.append(current.nth(i))
            except Exception:
                pass
        page.screenshot(path=os.path.join(BASE_DIR, '_report_product_list_download_menu.png'))
        if not visible:
            raise RuntimeError('下载菜单已展开但未找到可见「下载当前明细」')
        with page.expect_download(timeout=120000) as di:
            visible[0].click()
        dl = di.value
    safe = re.sub(r'[^0-9A-Za-z_.-]+', '_', dl.suggested_filename)
    path = os.path.join(DL_DIR, 'traffic_full_%s_%s' % (DATE, safe))
    dl.save_as(path)
    return path


acc = shops.get_email_account() or {}
if not acc.get('state'):
    print('[FAIL] DB 中没有抖店登录态')
    sys.exit(2)
state_path = os.path.join(BASE_DIR, '_report_full_probe_state.json')
with open(state_path, 'w', encoding='utf-8') as f:
    json.dump(acc['state'], f, ensure_ascii=False)

with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome', headless=False,
                                args=['--disable-blink-features=AutomationControlled',
                                      '--no-first-run', '--no-default-browser-check', '--no-sandbox'])
    ctx = browser.new_context(storage_state=state_path, locale='zh-CN',
                              timezone_id='Asia/Shanghai', viewport={'width': 1600, 'height': 950},
                              accept_downloads=True)
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    page = ctx.new_page()
    page.goto(WORKBENCH, wait_until='domcontentloaded', timeout=60000)
    time.sleep(12)
    cur = current_shop(page) or ''
    print('[登录] 当前店:', cur or '<读不到>', '| URL:', page.url)
    if '/login' in page.url or 'passport' in page.url:
        raise RuntimeError('登录态失效，探针不尝试重新登录')
    if TARGET[:6] not in cur and not switch_shop(page, TARGET, cur):
        raise RuntimeError('切店失败: %s' % TARGET)
    print('[切店] 当前店:', current_shop(page) or '<读不到>')

    target_url = PRODUCT_LIST_URL if PRODUCT_LIST_MODE else TRAFFIC_URL
    page.goto(target_url, wait_until='domcontentloaded', timeout=60000)
    time.sleep(10)
    if DATE_DEBUG:
        date_debug(page)
        browser.close()
        print('[完成] 日期调试，不下载。')
        sys.exit(0)
    # URL 参数在该页面只作为初始状态，实际导出日期以页面控件为准。
    click_exact_natural_day(page, DATE)
    print('[商品列表] URL:', page.url)
    select_all_metrics(page)
    path = download(page)
    browser.close()

print('\n[下载成功]', path, 'size=', os.path.getsize(path))
for sheet, rows in read_xlsx(path):
    print('SHEET=%s ROWS=%d COLS=%d' % (sheet, len(rows), len(rows[0]) if rows else 0))
    if rows:
        print('  HEADER=', json.dumps(rows[0], ensure_ascii=False))
    if len(rows) > 1:
        print('  ROW2=', json.dumps(rows[1], ensure_ascii=False))
print('[完成] 只下载、配置、解析，未写数据库。')
