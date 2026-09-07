# -*- coding: utf-8 -*-
"""
1688「1688 市场」前10页抓取（浏览器方案，v2）
用法：python tools/1688_scraper.py <关键词>
- 关键词从命令行参数读取，缺省用默认值
- 用 Playwright 启动 Chrome 打开 1688 搜索页 s.1688.com，注入登录态 Cookie 后抓取渲染后的 DOM
- 原 mtop 接口 (h5api.m.1688.com) 已被 1688 风控（RGV587/被挤爆 → 滑块验证），纯请求无法通过，
  改走真实浏览器绕过风控，抓「综合」排序前 10 页货源
- Cookie 读取：tools/1688_cookie.txt（k=v; k2=v2 字符串）
- 输出字段：排名、标题、商品超链接、店铺名、封面图、价格 -> tools/_1688_top10.json
- 全程写 tools/_1688_progress.json 供前端进度条轮询
"""
import json
import os
import re
import sys
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_KEYWORD = "汽车脚垫"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(BASE_DIR, "_1688_top10.json")
PROGRESS_FILE = os.path.join(BASE_DIR, "_1688_progress.json")
COOKIE_FILE = os.path.join(BASE_DIR, "1688_cookie.txt")

MAX_PAGES = 10  # 固定抓前 10 页
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")

LOGIN_COOKIES = {"_nk_", "lid", "unb"}


def resolve_keyword():
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    return (os.environ.get("1688_KEYWORD") or DEFAULT_KEYWORD).strip()


def log(*a):
    print(*a, flush=True)


def write_progress(status, done, total, keyword, error=''):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump({"status": status, "keyword": keyword, "done": done, "total": total, "error": error},
                      f, ensure_ascii=False)
    except Exception:
        pass


def load_cookie():
    if not os.path.exists(COOKIE_FILE):
        return ''
    try:
        with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


def parse_cookies(cookie_str):
    cookies = []
    for part in cookie_str.split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        name, _, value = part.partition('=')
        name, value = name.strip(), value.strip()
        if not name:
            continue
        cookies.append({'name': name, 'value': value, 'domain': '.1688.com', 'path': '/'})
    return cookies


def cookie_state(page):
    try:
        return {c["name"] for c in page.context.cookies("https://s.1688.com")}
    except Exception:
        return set()


def is_logged_in(page):
    return bool(cookie_state(page) & LOGIN_COOKIES)


SCRAPE_JS = """
() => {
  const out = [];
  const seen = new Set();
  const cards = document.querySelectorAll('.search-offer-item');
  for (const card of cards) {
    // offerId：优先从卡片内链接的 offerId=xxx 参数取，其次从 detail 链接取
    let oid = '';
    for (const a of card.querySelectorAll('a')) {
      const m = (a.href || '').match(/offerId=(\\d+)/);
      if (m) { oid = m[1]; break; }
    }
    if (!oid) {
      for (const a of card.querySelectorAll('a')) {
        const m = (a.href || '').match(/detail\\.1688\\.com\\/offer\\/(\\d+)/);
        if (m) { oid = m[1]; break; }
      }
    }
    if (!oid || seen.has(oid)) continue;
    const titleEl = card.querySelector('.offer-title-row, [class*="title-text"]');
    const title = titleEl ? titleEl.innerText.trim() : '';
    const priceEl = card.querySelector('[class*="offer-price"]');
    const price = priceEl ? priceEl.innerText.trim() : '';
    const imgEl = card.querySelector('[class*="offer-img"] img, img.main-img');
    const image = imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '';
    const shopEl = card.querySelector('[class*="offer-shop"]');
    const shop = shopEl ? shopEl.innerText.trim() : '';
    seen.add(oid);
    out.push({ id: oid, title: title || null, price: price || null, image: image || null, shop: shop || null, link: 'https://detail.1688.com/offer/' + oid + '.html' });
  }
  return out;
}
"""


def clean_price(raw):
    """把 DOM 里抓到的价格文本规整成 '¥12.5' 或 '¥1.2-3.4'，失败返回 None"""
    if not raw:
        return None
    s = str(raw)
    # 价格在「运费」之前，之后是运费/销量等杂项，不参与价格
    s = s.split('运费')[0]
    # 去掉 ¥/￥/空白/换行，只留数字、小数点、-、~
    s = s.replace('¥', '').replace('￥', '').replace(' ', '').replace('\n', '').replace('\r', '').replace('\t', '')
    s = re.sub(r'[^\d.\-~]', '', s)
    nums = re.findall(r'\d+(?:\.\d+)?', s)
    if not nums:
        return None
    if len(nums) >= 2 and ('-' in s or '~' in s):
        return '¥' + nums[0] + '-' + nums[1]
    return '¥' + nums[0]


def main():
    keyword = resolve_keyword()
    write_progress("running", 0, MAX_PAGES, keyword)

    cookie_str = load_cookie()
    # 默认无头；设 1688_HEADED=1 可弹窗手动滑过 1688 滑块验证码
    headless = not bool(os.environ.get('1688_HEADED'))
    log(f"[启动] 关键词={keyword} 目标页数={MAX_PAGES} 模式={'无头' if headless else '有头(可手动过验证码)'}")

    search_url = "https://s.1688.com/selloffer/offer_search.htm?keywords=" + quote(keyword.encode('gbk'))

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            BASE_DIR + os.sep + "1688_profile",
            channel="chrome",
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if cookie_str:
            try:
                ctx.add_cookies(parse_cookies(cookie_str))
                log("[登录] 已注入 1688 Cookie")
            except Exception as e:
                log("[登录] Cookie 注入失败:", repr(e))

        all_items = []
        per_page = []
        seen = set()

        for pg in range(1, MAX_PAGES + 1):
            url = search_url if pg == 1 else search_url + "&beginPage=" + str(pg)
            try:
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                log(f"[页{pg}] 页面加载失败: {e}")
                break
            # 等待商品卡片渲染 + 滚动触发懒加载
            try:
                page.wait_for_selector("a[href*='detail.1688.com/offer/']", timeout=20000)
            except Exception:
                pass
            for _ in range(3):
                page.mouse.wheel(0, 2000)
                time.sleep(1)

            batch = page.evaluate(SCRAPE_JS)
            added = 0
            for it in batch:
                oid = it.get("id")
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                it["page"] = pg
                all_items.append(it)
                added += 1
            per_page.append(added)
            log(f"[页{pg}/{MAX_PAGES}] 本页新增 {added}，累计 {len(all_items)}")
            write_progress("running", pg, MAX_PAGES, keyword)

            if added == 0:
                log("  -> 本页无新商品，视为已到最后一页，停止")
                break
            time.sleep(2)

        # 若一页都没抓到，通常是 1688 触发了滑块验证 / 未登录 / 账号被风控
        if not all_items:
            try:
                page.screenshot(path=os.path.join(BASE_DIR, "_1688_shot.png"), full_page=False)
            except Exception:
                pass
            body = ""
            try:
                body = (page.inner_text("body") or "")[:200]
            except Exception:
                pass
            log("[失败] 未抓到商品。页面开头文本:", body.replace("\n", " "))
            msg = "未抓到商品（1688 可能触发了滑块验证或账号被风控），请更新 Cookie 后重试；本地可设 1688_HEADED=1 弹窗手动滑过验证码"
            write_progress("error", 0, MAX_PAGES, keyword, error=msg)
            ctx.close()
            return

        products = [
            dict(rank=i + 1,
                 title=it["title"] or None,
                 price=clean_price(it.get("price")),
                 image=it["image"] or None,
                 shop=it["shop"] or None,
                 link=it["link"] or None)
            for i, it in enumerate(all_items)
        ]

        with_img = sum(1 for p in products if p.get("image"))
        log(f"[结果] 共 {len(products)} 条，其中 {with_img} 条有封面图")

        result = {
            "keyword": keyword,
            "count": len(products),
            "pages_crawled": len(per_page),
            "total_deduped": len(products),
            "per_page_counts": per_page,
            "products": products,
        }
        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        write_progress("done", len(per_page), MAX_PAGES, keyword)
        log("[保存]", OUT_JSON)
        ctx.close()


if __name__ == "__main__":
    main()
