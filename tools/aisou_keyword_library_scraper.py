# -*- coding: utf-8 -*-
"""内容创作中心专用爱搜产品词库采集器。

本程序与选品助手的 ``aisou_scraper.py`` 完全隔离：使用独立的输入、输出、进度文件，
只走“搜索词精确匹配行 → 详情 → 下拉词模块”，每个产品词抓取下拉词前五页。
它不写「爱搜数据表」，由内容创作中心后端读取本程序输出后写入自己的知识库表。
"""
import json
import os
import re
import sys
from urllib.parse import quote

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_FILE = os.path.join(BASE_DIR, '_aisou_keyword_library_input.json')
OUTPUT_FILE = os.path.join(BASE_DIR, '_aisou_keyword_library_output.json')
PROGRESS_FILE = os.path.join(BASE_DIR, '_aisou_keyword_library_progress.json')
LOCALSTORAGE_FILE = os.path.join(BASE_DIR, 'aisou_localstorage.json')
PROFILE_DIR = os.path.join(BASE_DIR, 'aisou_keyword_library_profile')
SEARCH_URL = 'https://dso.aidso.com/KeywordDouyin/searchWord?keyword='
HOME_URL = 'https://dso.aidso.com/'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

JS_EXACT_DETAIL = """(kw) => {
  const rows = Array.from(document.querySelectorAll('table tbody tr')).filter(x => x.offsetParent !== null);
  let exact = rows.find(row => Array.from(row.querySelectorAll('td')).some(td => (td.innerText || '').trim() === kw));
  if (!exact) return 'NOT_FOUND';
  // 爱搜当前版本的详情入口在第 9 列，文本常由图标/空白 p 节点承载，
  // 不能只依赖“详情”文字。先按参考 DOM 结构定位，再按文字/title/aria-label 兜底。
  const detail = exact.querySelector('td:nth-child(9) div div div p:nth-child(2)') ||
    exact.querySelector('td:nth-child(9) [title*="详情"],td:nth-child(9) [aria-label*="详情"]') ||
    Array.from(exact.querySelectorAll('button,a,[role="button"],span,div,p')).find(x =>
      x.offsetParent !== null && (/详情|查看|明细/.test((x.innerText || '').trim()) ||
        /详情|查看|明细/.test(x.getAttribute('title') || '') || /详情|查看|明细/.test(x.getAttribute('aria-label') || '')));
  if (!detail) return 'NO_DETAIL';
  detail.click(); return 'CLICKED';
}"""
JS_OPEN_DOWN = """() => {
  const nodes = Array.from(document.querySelectorAll('#content-container *,body *')).filter(x =>
    x.offsetParent !== null && (x.innerText || '').trim() === '下拉词');
  if (!nodes.length) return 'NOT_FOUND';
  nodes[nodes.length - 1].click(); return 'CLICKED';
}"""
JS_ROWS = """() => {
  const all = Array.from(document.querySelectorAll('#content-container *,body *'))
    .filter(x => x.offsetParent !== null);
  const heading = all.find(x => (x.innerText || '').trim() === '下拉词');
  // 真实页面是四列卡片而非 table。使用整个内容容器，避免停在只包含前两列的
  // CSS 子网格；详情页中其它卡片会被下面的“关键词+数值”行规则过滤掉。
  const root = document.querySelector('#content-container') || document.body;
  const rows = [];
  const seen = new Set();
  const numeric = s => /^[0-9,.]+(?:亿|万|w|W|k|K)?$/.test(s.replace(/平均[:：]/g, '').trim());
  for (const el of Array.from(root.querySelectorAll('*'))) {
    if (el.offsetParent === null || el.children.length > 5) continue;
    const lines = (el.innerText || '').split(String.fromCharCode(10)).map(x => x.trim()).filter(Boolean);
    if (lines.length < 2 || lines.length > 4 || lines.length > 80) continue;
    let n = -1;
    for (let i = lines.length - 1; i >= 0; i--) if (numeric(lines[i])) { n = i; break; }
    if (n < 1) continue;
    let word = lines[n - 1];
    if (/^[0-9]+$/.test(word) && n > 1) word = lines[n - 2];
    if (!word || /关键词|月覆盖人次|全部导出|下拉词/.test(word) || /^[0-9]+$/.test(word)) continue;
    const key = word + '|' + lines[n];
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push([word, lines[n], '']);
  }
  return rows;
}"""
JS_NEXT = """() => {
  const selectors = ['.el-pagination .btn-next','.el-pagination button[class*="next"]',
    'button[aria-label="下一页"]','button[title="下一页"]','a[aria-label="下一页"]'];
  for (const selector of selectors) {
    const el = document.querySelector(selector);
    if (el && el.offsetParent !== null && !el.disabled && !el.classList.contains('is-disabled')) { el.click(); return true; }
  }
  const el = Array.from(document.querySelectorAll('button,a,span')).find(x =>
    x.offsetParent !== null && /^(下一页|>|›|»)$/.test((x.innerText || '').trim()));
  if (el) { el.click(); return true; }
  const pagers = Array.from(document.querySelectorAll('[class*="pagination"],[class*="Pagination"],.el-pagination'))
    .filter(x => x.offsetParent !== null);
  for (const pager of pagers) {
    const controls = Array.from(pager.querySelectorAll('button,a,li,[role="button"]'))
      .filter(x => x.offsetParent !== null && !x.disabled && !x.classList.contains('disabled') && !x.classList.contains('is-disabled'));
    if (!controls.length) continue;
    const next = controls.slice().reverse().find(x => /^(下一页|>|›|»)$/.test((x.innerText || '').trim()) ||
      /next|下一页/.test((x.className || '') + ' ' + (x.getAttribute('aria-label') || '') + ' ' + (x.getAttribute('title') || '')));
    if (next) { next.click(); return true; }
    // 截图所示分页没有文本/aria 标识时，分页控件最后一个可用按钮就是“>”。
    if (controls.length >= 3) { controls[controls.length - 1].click(); return true; }
  }
  return false;
}"""


def log(*args):
    print(*args, flush=True)


def progress(status, done, total, message=''):
    try:
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as fh:
            json.dump({'status': status, 'done': done, 'total': total, 'message': message}, fh, ensure_ascii=False)
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            value = json.load(fh)
        return value
    except Exception:
        return default


def parse_num(text):
    text = str(text or '').replace('平均:', '').replace('平均：', '').replace(',', '').strip()
    m = re.match(r'([\d.]+)\s*(亿|万|w|W|k|K)?', text)
    if not m:
        return ''
    unit = {'亿': 10 ** 8, '万': 10 ** 4, 'w': 10 ** 4, 'W': 10 ** 4, 'k': 10 ** 3, 'K': 10 ** 3}
    return int(round(float(m.group(1)) * unit.get(m.group(2) or '', 1)))


def collect_page(page, page_no, api_payloads=None):
    rows = page.evaluate(JS_ROWS) or []
    result = []
    seen = set()
    for row in rows:
        if not row:
            continue
        name = str(row[0] or '').strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append({'type': '下拉词', 'name': name,
                       'month': parse_num(row[1]) if len(row) > 1 else '',
                       'seven': parse_num(row[2]) if len(row) > 2 else '',
                       'page': page_no})
    if result or not api_payloads:
        return result
    # 详情模块的表格在部分版本由虚拟列表渲染，DOM 没有 tbody；从同一次点击触发的
    # JSON 响应兜底读取 result/list/data 数组，字段名兼容 keyword/name。
    def walk(value):
        if isinstance(value, dict):
            for key in ('result', 'list', 'rows', 'records', 'items'):
                child = value.get(key)
                if isinstance(child, list):
                    yield from child
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)
    for payload in reversed(api_payloads):
        for item in walk(payload):
            if not isinstance(item, dict):
                continue
            name = str(item.get('keyword') or item.get('name') or item.get('word') or '').strip()
            if not name or name in seen:
                continue
            seen.add(name)
            result.append({'type': '下拉词', 'name': name,
                           'month': item.get('month_cover_count') or item.get('month') or '',
                           'seven': item.get('seven_search_count') or item.get('seven') or '',
                           'page': page_no})
        if result:
            break
    return result


def scrape_one(page, keyword, api_payloads=None):
    page.goto(SEARCH_URL + quote(keyword), wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(5000)
    detail = page.evaluate(JS_EXACT_DETAIL, keyword)
    if detail != 'CLICKED':
        log('[词库] %s 未找到精确匹配详情按钮：%s' % (keyword, detail))
        return []
    page.wait_for_timeout(1500)
    opened = page.evaluate(JS_OPEN_DOWN)
    log('[词库] %s 详情页下拉词入口：%s，URL：%s' % (keyword, opened, page.url))
    page.wait_for_timeout(1200)
    words = []
    for page_no in range(1, 6):
        current = collect_page(page, page_no, api_payloads)
        words.extend(current)
        log('[词库] %s 下拉词第%d页读取 %d 条' % (keyword, page_no, len(current)))
        if page_no == 1 and not current:
            try:
                body_text = (page.locator('body').inner_text(timeout=2000) or '').replace('\n', ' | ')
                log('[词库] %s 详情页未读到表格，页面文本：%s' % (keyword, body_text[:1200]))
            except Exception:
                pass
        next_clicked = page.evaluate(JS_NEXT)
        log('[词库] %s 下拉词第%d页翻页：%s' % (keyword, page_no, next_clicked))
        if page_no == 5 or not next_clicked:
            break
        page.wait_for_timeout(1200)
    # 同一词在不同页重复时只保留首次出现。
    unique, seen = [], set()
    for item in words:
        if item['name'] in seen:
            continue
        seen.add(item['name'])
        unique.append(item)
    return unique


def main():
    payload = load_json(INPUT_FILE, {})
    keywords = [str(x).strip() for x in payload.get('keywords', []) if str(x).strip()]
    ls = load_json(LOCALSTORAGE_FILE, {})
    if not keywords:
        progress('error', 0, 0, '无产品词')
        return 2
    if not isinstance(ls, dict) or not ls:
        progress('error', 0, len(keywords), '缺少爱搜登录态')
        return 2
    progress('running', 0, len(keywords), '启动内容创作中心专用采集器')
    results = []
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                PROFILE_DIR, channel='chrome', headless=True,
                args=['--disable-blink-features=AutomationControlled'],
                user_agent=UA, viewport={'width': 1440, 'height': 900}, locale='zh-CN')
        except Exception:
            context = playwright.chromium.launch_persistent_context(
                PROFILE_DIR, headless=True,
                args=['--disable-blink-features=AutomationControlled'],
                user_agent=UA, viewport={'width': 1440, 'height': 900}, locale='zh-CN')
        page = context.pages[0] if context.pages else context.new_page()
        api_payloads = []
        def capture_response(response):
            try:
                if 'aidso' not in response.url:
                    return
                payload = response.json()
                if isinstance(payload, (dict, list)):
                    api_payloads.append(payload)
                    if len(api_payloads) > 120:
                        del api_payloads[:-120]
            except Exception:
                pass
        page.on('response', capture_response)
        login_codes = []
        def on_response(response):
            try:
                if 'aidso' in response.url and ('user/info' in response.url or 'sub_account_info' in response.url):
                    login_codes.append((response.json() or {}).get('code'))
            except Exception:
                pass
        page.on('response', on_response)
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page.goto(HOME_URL, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(1200)
        page.evaluate('(kv) => { for (const k in kv) localStorage.setItem(k, kv[k]); }', ls)
        page.wait_for_timeout(1500)
        if login_codes and login_codes[-1] != 200:
            progress('error', 0, len(keywords), '爱搜登录态失效，Cookie 需要重新登录')
            log('[词库] 登录态失效：user/info code=%s' % login_codes[-1])
            context.close()
            return 3
        for index, keyword in enumerate(keywords):
            progress('running', index, len(keywords), '采集 ' + keyword)
            try:
                api_payloads.clear()
                words = scrape_one(page, keyword, api_payloads)
            except Exception as exc:
                log('[词库] %s 采集失败：%r' % (keyword, exc))
                words = []
            results.append({'keyword': keyword, 'words': words})
            log('[词库] %s 完成，%d 条下拉词' % (keyword, len(words)))
        context.close()
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as fh:
        json.dump({'results': results}, fh, ensure_ascii=False, indent=2)
    progress('done', len(keywords), len(keywords), '采集完成')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
