# -*- coding: utf-8 -*-
"""抓取淘宝「天猫榜单」当前页面可见的全部榜单商品。

数据通过页面自身的 MTop 请求加载。脚本只在已存在的合法淘宝登录态下工作，
不会尝试绕过验证码、滑块或其他访问控制；登录态失效或榜单分页不完整时会失败，
由调用方决定是否重试。

输出：tools/_tmall_ranklist_output.json
进度：tools/_tmall_ranklist_progress.json
"""
from __future__ import annotations

import json
import hashlib
import os
import sys
import time
import traceback
import urllib.parse
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from playwright.sync_api import Page, Response, sync_playwright


BASE_DIR = Path(__file__).resolve().parent
COOKIE_FILE = BASE_DIR / 'taobao_cookie.txt'
OUTPUT_FILE = BASE_DIR / '_tmall_ranklist_output.json'
PROGRESS_FILE = BASE_DIR / '_tmall_ranklist_progress.json'
RANKLIST_URL = 'https://huodong.taobao.com/wow/z/tbhome/tbpc-venue/ranklist'

# 页面当前公开的顶层类别；三类按业务要求不采集。
EXCLUDED_CATEGORIES = {'天猫进口', '食品生鲜', '医药健康'}
CATEGORIES = [
    '为你精选', '服饰时尚', '美妆护肤', '品质家电', '手机数码',
    '天猫进口', '个护家清', '医药健康', '食品生鲜', '母婴亲子',
    '运动户外', '萌宠潮玩', '家居家装', '图书音像', '酷车出行',
]
RANK_TABS = ['热销榜', '好价榜', '好评榜', '回购榜']
RANK_TAB_BY_TYPE = {'18': '热销榜', '32': '好价榜', '20': '好评榜', '9': '回购榜'}
RANK_TYPE_BY_TAB = {value: key for key, value in RANK_TAB_BY_TYPE.items()}

HEADLESS = os.environ.get('TMALL_RANKLIST_HEADLESS', '1').strip().lower() not in ('0', 'false', 'no')
TAB_WAIT_SECONDS = float(os.environ.get('TMALL_RANKLIST_TAB_WAIT', '12'))
PAGE_WAIT_SECONDS = float(os.environ.get('TMALL_RANKLIST_PAGE_WAIT', '10'))
MAX_RANK_PAGES = int(os.environ.get('TMALL_RANKLIST_MAX_PAGES', '50'))
# 榜单接口对连续请求有短时保护；4 秒间隔可让周任务在一小时窗口内温和完成。
REQUEST_INTERVAL_SECONDS = float(os.environ.get('TMALL_RANKLIST_REQUEST_INTERVAL', '4'))
DIRECT_PAGE_SIZE = int(os.environ.get('TMALL_RANKLIST_PAGE_SIZE', '100'))


class CrawlError(RuntimeError):
    """可预期的采集失败：调用方不应把不完整结果写入数据库。"""


class EmptyRankPage(CrawlError):
    """接口成功但指定页无榜单内容；仅在上一页已满时可判定为自然末页。"""


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子替换状态文件，避免前端轮询读到半截 JSON。"""
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def write_progress(status: str, message: str, done: int = 0, total: int = 0, **extra: Any) -> None:
    _write_json(PROGRESS_FILE, {
        'status': status,
        'message': message,
        'done': done,
        'total': total,
        'updatedAt': _now(),
        **extra,
    })


def parse_cookies(raw: str) -> list[dict[str, str]]:
    cookies: list[dict[str, str]] = []
    for piece in raw.split(';'):
        name, sep, value = piece.strip().partition('=')
        if sep and name:
            cookies.append({'name': name, 'value': value, 'domain': '.taobao.com', 'path': '/'})
    return cookies


def load_cookies() -> list[dict[str, str]]:
    if not COOKIE_FILE.is_file():
        raise CrawlError('未找到淘宝登录态：请先在系统中保存天猫 Cookie')
    cookies = parse_cookies(COOKIE_FILE.read_text(encoding='utf-8').strip())
    if not cookies:
        raise CrawlError('淘宝登录态为空：请重新保存天猫 Cookie')
    return cookies


def _rank_payloads(value: Any) -> Iterable[dict[str, Any]]:
    """从 MTop 的嵌套响应中找出真正的榜单页数据块。"""
    if isinstance(value, dict):
        # ``rankType`` 曾经是必填字段，但近期接口在部分类别的回包中
        # 不再返回它。rankList + 分页字段已经足够唯一地识别榜单数据块。
        ranks = value.get('rankList')
        if (isinstance(ranks, list) and
                ('rankType' in value or 'hasMore' in value or
                 'currentPage' in value or any(isinstance(x, dict) and
                                               'itemList' in x for x in ranks))):
            yield value
        for child in value.values():
            yield from _rank_payloads(child)
    elif isinstance(value, list):
        for child in value:
            yield from _rank_payloads(child)


def _response_rank_payloads(responses: Iterable[Response]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for response in responses:
        # 接口域名/路径会随淘宝前端发布变化（route.aldlampservice、
        # mtop/aldlampservice 均曾出现）。以 JSON 结构识别，避免硬编码
        # endpoint 导致首屏误报“未拿到数据”。
        try:
            payload = response.json()
        except Exception:
            continue
        out.extend(_rank_payloads(payload))
    return out


def _wait_for_rank_payload(page: Page, responses: list[Response], start_at: int, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payloads = _response_rank_payloads(responses[start_at:])
        if payloads:
            return payloads[-1]
        # 让 Playwright 调度网络 response 回调，且不会像固定 sleep 一样
        # 在接口已返回时无谓地增加每一页的采集时间。
        page.wait_for_timeout(200)
    return None


def _has_more(payload: dict[str, Any]) -> bool:
    return str(payload.get('hasMore') or '').strip().lower() == 'true'


def _payload_key(payload: dict[str, Any]) -> tuple[Any, tuple[str, ...]]:
    ranks = payload.get('rankList') or []
    ids = tuple(str(rank.get('rankId') or rank.get('distinctId') or rank.get('title') or '')
                for rank in ranks if isinstance(rank, dict))
    return payload.get('currentPage'), ids


def _click_exact(page: Page, label: str) -> None:
    locator = page.get_by_text(label, exact=True)
    count = locator.count()
    if count != 1:
        raise CrawlError('页面元素「%s」数量异常（%d），页面可能已改版' % (label, count))
    locator.click()


def _last_rank_response(responses: Iterable[Response]) -> tuple[Response, dict[str, Any]] | None:
    for response in reversed(list(responses)):
        try:
            payloads = list(_rank_payloads(response.json()))
        except Exception:
            continue
        if payloads:
            return response, payloads[-1]
    return None


def _rank_tab_from_response(response: Response) -> str | None:
    try:
        data = json.loads(urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)['data'][0])
    except (KeyError, ValueError, TypeError):
        return None
    return RANK_TAB_BY_TYPE.get(str(data.get('type') or ''))


def _rank_template_for_tab(template_url: str, rank_tab: str) -> str:
    """在类别请求模板上切换榜单类型，供接口分页直接使用。"""
    parsed = urllib.parse.urlparse(template_url)
    query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
    try:
        data = json.loads(query['data'])
        rank_type = RANK_TYPE_BY_TAB[rank_tab]
        data['type'] = rank_type
        if data.get('params'):
            params = json.loads(data['params'])
            params['type'] = rank_type
            data['params'] = json.dumps(params, ensure_ascii=False, separators=(',', ':'))
        query['data'] = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    except (KeyError, ValueError, TypeError) as exc:
        raise CrawlError('无法构造「%s」榜单接口模板' % rank_tab) from exc
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', urllib.parse.urlencode(query), ''))


def _m_h5_token(context: Any) -> str:
    for cookie in context.cookies('https://h5api.m.taobao.com'):
        if cookie.get('name') == '_m_h5_tk':
            return str(cookie.get('value') or '').split('_', 1)[0]
    raise CrawlError('淘宝登录态中缺少榜单接口令牌，请重新保存 Cookie')


def _direct_rank_page(page: Page, context: Any, template_url: str, page_number: int) -> dict[str, Any]:
    """沿用页面自己发出的接口模板和登录会话，稳定拉取指定榜单页。"""
    parsed = urllib.parse.urlparse(template_url)
    query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
    try:
        data = json.loads(query['data'])
        app_key = query['appKey']
    except (KeyError, ValueError, TypeError) as exc:
        raise CrawlError('榜单接口请求格式已变化，无法安全翻页') from exc
    data['page'] = page_number
    data['pageSize'] = DIRECT_PAGE_SIZE
    data_text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))

    last_result = '未收到响应'
    for _ in range(3):
        timestamp = str(int(time.time() * 1000))
        sign_source = '%s&%s&%s&%s' % (_m_h5_token(context), timestamp, app_key, data_text)
        query.update({
            't': timestamp,
            'data': data_text,
            'sign': hashlib.md5(sign_source.encode('utf-8')).hexdigest(),
        })
        url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', urllib.parse.urlencode(query), ''))
        # 由已打开的浏览器页面请求，沿用页面的连接、Referer 与 Cookie 策略；
        # 不使用独立 APIRequestContext，避免其在网络超时时中断会话。
        try:
            result = page.evaluate('''async (url) => {
                try {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 25000);
                    const response = await fetch(url, { credentials: 'include', signal: controller.signal });
                    clearTimeout(timer);
                    return { ok: response.ok, status: response.status, text: await response.text() };
                } catch (_) {
                    return { ok: false, status: 0, text: '' };
                }
            }''', url)
        except Exception:
            result = {'ok': False, 'status': 0, 'text': ''}
        if result.get('ok'):
            reply: dict[str, Any] = {}
            try:
                reply = json.loads(str(result.get('text') or ''))
                payloads = list(_rank_payloads(reply))
            except Exception:
                payloads = []
            if payloads:
                return payloads[-1]
            last_result = '；'.join(str(value) for value in (reply.get('ret') or ['响应没有榜单数据']))[:180]
        else:
            last_result = 'HTTP %s 或网络超时' % result.get('status', 0)
        # 访问保护时不要立即重试，给服务端和当前登录会话足够冷却时间。
        time.sleep(max(REQUEST_INTERVAL_SECONDS, 5))
    if last_result.startswith('SUCCESS'):
        raise EmptyRankPage('榜单第 %d 页返回成功但没有榜单内容' % (page_number + 1))
    raise CrawlError('榜单第 %d 页接口未返回有效数据（%s）' % (page_number + 1, last_result))


def _collect_rank_pages(page: Page, context: Any, template_url: str, label: str) -> list[dict[str, Any]]:
    """使用页面原始接口模板逐页抓取；分页失败时拒绝不完整结果。"""
    try:
        first_page = int(json.loads(urllib.parse.parse_qs(urllib.parse.urlparse(template_url).query)['data'][0]).get('page', 0))
    except (KeyError, ValueError, TypeError) as exc:
        raise CrawlError('%s 的首屏接口未包含有效页码' % label) from exc
    # 首屏 UI 固定为 10 个榜单；从第 0 页用已验证支持的 100 条 pageSize
    # 重新读取，避免下一页直接跳到第 101 条而遗漏首屏之后的数据。
    try:
        initial = _direct_rank_page(page, context, template_url, first_page)
    except EmptyRankPage:
        # 某些类别确实没有该榜单类型的数据；接口明确成功但为空时，
        # 记录为 0 条并继续其他榜单，不把空榜误判为网络失败。
        return []
    pages = [initial]
    seen = {_payload_key(initial)}
    next_page = first_page + 1
    while _has_more(pages[-1]):
        if len(pages) >= MAX_RANK_PAGES:
            raise CrawlError('%s 的榜单分页超过上限 %d，已停止以避免不完整入库' % (label, MAX_RANK_PAGES))
        try:
            payload = _direct_rank_page(page, context, template_url, next_page)
        except EmptyRankPage:
            if len(pages[-1].get('rankList') or []) >= DIRECT_PAGE_SIZE:
                pages[-1]['hasMore'] = 'false'
                break
            raise
        key = _payload_key(payload)
        if key in seen:
            raise CrawlError('%s 的下一页榜单重复，拒绝以不完整数据入库' % label)
        seen.add(key)
        pages.append(payload)
        next_page += 1
        # 保持温和请求频率，减少长期定时任务触发站点短时保护的概率。
        time.sleep(REQUEST_INTERVAL_SECONDS)
    return pages


def _open_ranklist(page: Page, responses: list[Response]) -> tuple[Response, dict[str, Any]]:
    """首屏遇到站点短时保护时低频刷新；绝不把空响应当作正常数据。"""
    for attempt in range(3):
        before = len(responses)
        if attempt:
            write_progress('running', '首屏榜单暂不可用，等待后第 %d 次重试' % (attempt + 1))
            page.wait_for_timeout(60000)
            try:
                page.reload(wait_until='domcontentloaded', timeout=60000)
            except Exception as exc:
                raise CrawlError('刷新天猫榜单页面失败：%s' % exc) from exc
        else:
            try:
                page.goto(RANKLIST_URL, wait_until='domcontentloaded', timeout=60000)
            except Exception as exc:
                # 统一转成可重试的采集错误，保留原始原因供告警和进度页展示。
                raise CrawlError('打开天猫榜单页面失败：%s' % exc) from exc
        page.wait_for_timeout(3500)
        if 'login.taobao.com' in page.url:
            raise CrawlError('淘宝登录态已失效，请重新保存 Cookie 后再运行')
        if _wait_for_rank_payload(page, responses, before, TAB_WAIT_SECONDS) is not None:
            current_rank = _last_rank_response(responses[before:])
            if current_rank is not None:
                return current_rank
    # 留下少量上下文，便于区分访问验证和接口改版，而不是每次都只看到
    # 一条无法行动的通用告警。
    seen_urls = []
    for response in responses[-80:]:
        url = response.url
        if any(token in url for token in ('mtop', 'aldlamp', 'h5api', 'taobao')):
            seen_urls.append('%s:%s' % (response.status, urllib.parse.urlparse(url).path[-80:]))
    detail = '；'.join(seen_urls[-5:])
    suffix = '（相关响应：%s）' % detail if detail else ''
    raise CrawlError('未拿到天猫榜单数据，可能触发访问验证或页面接口已变更%s' % suffix)


def _price(value: Any) -> float:
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        raise CrawlError('榜单商品缺少可用价格：%r' % (value,))


def rows_from_payloads(category: str, rank_tab: str, payloads: Iterable[dict[str, Any]], capture_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for payload in payloads:
        for rank in payload.get('rankList') or []:
            if not isinstance(rank, dict):
                continue
            rank_name = str(rank.get('title') or '').strip()
            if not rank_name:
                raise CrawlError('%s / %s 存在没有榜单名的数据块' % (category, rank_tab))
            for item in rank.get('itemList') or []:
                if not isinstance(item, dict):
                    continue
                product_name = str(item.get('title') or '').strip()
                if not product_name:
                    raise CrawlError('%s / %s / %s 存在没有产品名的链接' % (category, rank_tab, rank_name))
                rows.append({
                    '类别名': category,
                    '排行榜名': rank_name,
                    '产品名': product_name,
                    '价格': _price(item.get('wapFinalPrice')),
                    '日期': capture_date,
                    # 不落现有窄表，保留在运行结果里便于排障与后续扩展。
                    '产品链接': 'https:' + str(item.get('url') or '') if str(item.get('url') or '').startswith('//') else str(item.get('url') or ''),
                    '榜单类型': rank_tab,
                    '榜单ID': str(rank.get('rankId') or rank.get('distinctId') or ''),
                })
    return rows


def dedupe_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """同页的重复 MTop 回包只保留一条，键与数据库唯一键一致。"""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        key = (row['类别名'], row['排行榜名'], row['产品名'], row['日期'])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def crawl(capture_date: str) -> dict[str, Any]:
    cookies = load_cookies()
    included_categories = [name for name in CATEGORIES if name not in EXCLUDED_CATEGORIES]
    total_steps = len(included_categories) * len(RANK_TABS)
    responses: list[Response] = []
    all_rows: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=HEADLESS)
        context = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='zh-CN')
        context.add_cookies(cookies)
        try:
            for category in included_categories:
                category_rows: list[dict[str, Any]] = []
                for category_attempt in range(1, 4):
                    page = context.new_page()
                    page.on('response', lambda response: responses.append(response))
                    try:
                        current_response, current_payload = _open_ranklist(page, responses)
                        current_category = '为你精选'
                        current_tab = _rank_tab_from_response(current_response) or '热销榜'
                        if category != current_category:
                            before = len(responses)
                            _click_exact(page, category)
                            if _wait_for_rank_payload(page, responses, before, TAB_WAIT_SECONDS) is None:
                                raise CrawlError('切换类别「%s」后未收到榜单数据' % category)
                            current_rank = _last_rank_response(responses[before:])
                            if current_rank is None:
                                raise CrawlError('切换类别「%s」后未找到榜单接口' % category)
                            current_response, current_payload = current_rank
                        current_tab = _rank_tab_from_response(current_response) or '热销榜'
                        page.wait_for_timeout(1500)
                        category_rows = []
                        for rank_tab in RANK_TABS:
                            step = included_categories.index(category) * len(RANK_TABS) + RANK_TABS.index(rank_tab) + 1
                            write_progress('running', '抓取 %s · %s' % (category, rank_tab), step - 1, total_steps,
                                           category=category, rankTab=rank_tab, rowCount=len(all_rows) + len(category_rows))
                            template_url = _rank_template_for_tab(current_response.url, rank_tab)
                            pages = _collect_rank_pages(page, context, template_url, '%s / %s' % (category, rank_tab))
                            category_rows.extend(rows_from_payloads(category, rank_tab, pages, capture_date))
                        all_rows.extend(category_rows)
                        write_progress('running', '已完成类别 %s' % category,
                                       (included_categories.index(category) + 1) * len(RANK_TABS), total_steps,
                                       category=category, rowCount=len(all_rows))
                        break
                    except CrawlError as exc:
                        if category_attempt == 3:
                            raise
                        write_progress('running', '%s 第 %d 次失败，正在更换页面重试' % (category, category_attempt),
                                       included_categories.index(category) * len(RANK_TABS), total_steps,
                                       category=category, rowCount=len(all_rows), retry=str(exc))
                        page.wait_for_timeout(3000)
                    finally:
                        page.close()
        finally:
            context.close()
            browser.close()

    rows = dedupe_rows(all_rows)
    if not rows:
        raise CrawlError('本轮没有得到任何可入库的天猫榜单商品')
    return {
        'status': 'done',
        'message': '抓取完成',
        'date': capture_date,
        'excludedCategories': sorted(EXCLUDED_CATEGORIES),
        'categories': included_categories,
        'rankTabs': RANK_TABS,
        'rowCount': len(rows),
        'rows': rows,
        'finishedAt': _now(),
    }


def main() -> int:
    sys.stdout.reconfigure(encoding='utf-8')
    capture_date = os.environ.get('TMALL_RANKLIST_DATE') or datetime.now().strftime('%Y-%m-%d')
    write_progress('running', '正在启动天猫榜单采集', 0, len([x for x in CATEGORIES if x not in EXCLUDED_CATEGORIES]) * len(RANK_TABS))
    try:
        result = crawl(capture_date)
        _write_json(OUTPUT_FILE, result)
        write_progress('done', '抓取完成：%d 条待入库数据' % result['rowCount'],
                       result['rowCount'], result['rowCount'], rowCount=result['rowCount'])
        print('[天猫榜单] 抓取完成：%d 条' % result['rowCount'], flush=True)
        return 0
    except Exception as exc:
        traceback.print_exc()
        error = {'status': 'error', 'message': str(exc), 'date': capture_date, 'finishedAt': _now(), 'rows': []}
        _write_json(OUTPUT_FILE, error)
        write_progress('error', '抓取失败：' + str(exc))
        print('[天猫榜单] 抓取失败：%s' % exc, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
