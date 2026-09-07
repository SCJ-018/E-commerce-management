# -*- coding: utf-8 -*-
"""
淘宝「天猫市场」销量前50抓取（登录态方案）v5
用法：python tools/taobao_scraper.py <关键词>
- 关键词从命令行参数读取，缺省时读 TB_KEYWORD 环境变量，再缺省用默认值
- 首次运行弹出 Chrome，扫码登录一次；cookie 持久化到 tools/taobao_profile/
- 登录后自动按「销量」排序，分页抓取前50的 标题/封面图/价格/销量/店铺名/链接
  -> tools/_taobao_top50.json（哪个字段抓不到就置 null，不落库）
"""
import json
import os
import re
import sys
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright

DEFAULT_KEYWORD = "拼豆智能板"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "taobao_profile")
COOKIE_FILE = os.path.join(BASE_DIR, "taobao_cookie.txt")
OUT_JSON = os.path.join(BASE_DIR, "_taobao_top50.json")
OUT_HTML = os.path.join(BASE_DIR, "_taobao_page.html")
OUT_SHOT = os.path.join(BASE_DIR, "_taobao_shot.png")

LOGIN_WAIT = int(os.environ.get("TB_LOGIN_WAIT", "300"))
TOP_N = 50  # 淘宝搜索每页约 44 条，取 50 需翻到第二页
PROGRESS_FILE = os.path.join(BASE_DIR, "_taobao_progress.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")

LOGIN_COOKIES = {"tracknick", "unb", "_nk_"}


def resolve_keyword():
    """抓取关键词：优先命令行参数，其次 TB_KEYWORD 环境变量，最后默认值"""
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    return (os.environ.get("TB_KEYWORD") or DEFAULT_KEYWORD).strip()


def log(*a):
    print(*a, flush=True)


def write_progress(status, done, total, keyword):
    """写进度文件，供后端 /status 接口读取"""
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump({"status": status, "keyword": keyword, "done": done, "total": total},
                      f, ensure_ascii=False)
    except Exception:
        pass


def cookie_state(page):
    try:
        return {c["name"] for c in page.context.cookies("https://www.taobao.com")}
    except Exception:
        return set()


def is_logged_in(page):
    return bool(cookie_state(page) & LOGIN_COOKIES)


def load_cookie():
    """读取淘宝 Cookie 字符串；文件不存在或为空返回 ''"""
    if not os.path.exists(COOKIE_FILE):
        return ''
    try:
        with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


def parse_cookies(cookie_str):
    """把 'k=v; k2=v2' 形式的 Cookie 字符串解析成 Playwright add_cookies 所需对象列表"""
    cookies = []
    for part in cookie_str.split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        name, _, value = part.partition('=')
        name, value = name.strip(), value.strip()
        if not name:
            continue
        cookies.append({'name': name, 'value': value, 'domain': '.taobao.com', 'path': '/'})
    return cookies


SCRAPE_JS = """
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll("a[href*='item.htm']")) {
    const href = a.href || "";
    const m = href.match(/[?&]id=(\\d+)/);
    const pid = m ? m[1] : "";
    if (!pid || seen.has(pid)) continue;

    // 卡片容器：这个 <a> 通常是整卡；缺关键字段时向上找
    let card = a;
    if (!card.querySelector("[class*='title--']") && !card.querySelector("[class*='realSales--']")) {
      for (let i = 0; i < 4 && card; i++) card = card.parentElement;
    }

    const titleEl = card.querySelector("[class*='title--']");
    const unitEl = card.querySelector("[class*='unit--']");
    const intEl = card.querySelector("[class*='priceInt--']");
    const floatEl = card.querySelector("[class*='priceFloat--']");
    const salesEl = card.querySelector("[class*='realSales--']");
    const shopEl = card.querySelector("[class*='shopName--'], [class*='ShopName--'], [class*='shop--'], [class*='ShopInfo--']");

    const unit = unitEl ? unitEl.innerText.trim() : "¥";
    const intT = intEl ? intEl.innerText.trim() : "";
    const floatT = floatEl ? floatEl.innerText.trim() : "";
    const price = intT ? unit + intT + floatT : "";

    let imgEl = null;
    for (const im of card.querySelectorAll("img")) {
      const s = im.getAttribute("data-src") || im.src || "";
      if (s.includes("alicdn") && (s.includes("uploaded") || s.includes("item_pic") || s.includes("_580"))) {
        imgEl = im; break;
      }
    }
    if (!imgEl) imgEl = card.querySelector("img");

    seen.add(pid);
    out.push({
      id: pid,
      title: titleEl ? titleEl.innerText.trim() : null,
      price: price || null,
      sales: salesEl ? salesEl.innerText.trim() : null,
      image: imgEl ? (imgEl.getAttribute("data-src") || imgEl.src || null) : null,
      shop: shopEl ? shopEl.innerText.trim() : null,
      link: href || null
    });
  }
  return out;
}
"""


def click_sales_sort(page):
    """点击「销量」排序 tab，等待 URL 生效"""
    try:
        page.locator("li.next-tabs-tab").filter(has_text="销量").first.click()
        log("[排序] 已点击「销量」tab")
    except Exception as e:
        log("[排序] 文本点击失败，尝试按索引:", repr(e))
        try:
            page.locator("li.next-tabs-tab").nth(1).click()
        except Exception as e2:
            log("[排序] 索引点击也失败:", repr(e2))
    for _ in range(15):
        if "sort=sale-desc" in page.url or "sale-desc" in page.url:
            log("[排序] URL 已更新:", page.url)
            return
        time.sleep(1)
    log("[排序] 警告：URL 未出现 sale-desc，当前:", page.url)


def main():
    keyword = resolve_keyword()
    write_progress("running", 0, TOP_N, keyword)
    search_url = "https://s.taobao.com/search?q=" + quote(keyword)
    log(f"[抓取] 关键词 = {keyword}，目标销量前 {TOP_N}")

    # 有 Cookie 则无头运行（不弹浏览器窗口）；没 Cookie 才弹窗扫码登录
    cookie_str = load_cookie()
    headless = bool(cookie_str)
    log(f"[登录] 模式 = {'无头(复用 Cookie)' if headless else '有头(扫码登录)'}")

    os.makedirs(PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel="chrome",
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # 注入 Cookie（若有）
        if cookie_str:
            try:
                ctx.add_cookies(parse_cookies(cookie_str))
                log("[登录] 已注入淘宝 Cookie，尝试复用登录态")
            except Exception as e:
                log("[登录] Cookie 注入失败:", repr(e))

        page.goto(search_url, timeout=60000, wait_until="domcontentloaded")

        # --- 登录处理 ---
        if is_logged_in(page):
            log("[登录] 已登录（复用本地会话）")
        elif headless:
            log("[登录] Cookie 无效或已过期，且无头模式无法扫码。请更新淘宝 Cookie 后重试。")
            write_progress("error", 0, TOP_N, keyword)
            ctx.close()
            return
        else:
            log(f"[登录] 未登录 —— 请在弹出的 Chrome 窗口里【扫码】登录，等待最多 {LOGIN_WAIT}s ...")
            deadline = time.time() + LOGIN_WAIT
            while time.time() < deadline:
                time.sleep(3)
                if is_logged_in(page):
                    log("[登录] 检测到登录成功")
                    break
            else:
                log("[登录] 等待超时，仍未登录。请重新运行。")
                write_progress("error", 0, TOP_N, keyword)
                ctx.close()
                return

        # --- 重新加载 + 销量排序（URL 参数为主，点击兜底） ---
        base_url = "https://s.taobao.com/search?q=" + quote(keyword) + "&sort=sale-desc"
        page.goto(base_url, timeout=60000, wait_until="domcontentloaded")
        time.sleep(3)
        click_sales_sort(page)

        # 分页抓取：淘宝搜索每页约 44 条且不无限滚动，滚动加载不出下一页，
        # 需用 s= 偏移翻页才能凑够 TOP_N
        products = []
        seen = set()
        offset = 0
        while len(products) < TOP_N:
            if offset:
                page.goto(base_url + "&s=" + str(offset), timeout=60000, wait_until="domcontentloaded")
                time.sleep(2)
            # 页内滚动触发图片懒加载
            for _ in range(3):
                page.mouse.wheel(0, 1500)
                time.sleep(1)
            batch = page.evaluate(SCRAPE_JS)
            added = 0
            for p in batch:
                if p['id'] in seen:
                    continue
                seen.add(p['id'])
                products.append(p)
                added += 1
                if len(products) >= TOP_N:
                    break
            write_progress("running", len(products), TOP_N, keyword)
            log(f"[分页] s={offset} 本页新增 {added}，累计 {len(products)}")
            if added == 0:
                break
            offset += 44

        # 落盘快照（最后一页）
        try:
            open(OUT_HTML, "w", encoding="utf-8").write(page.content())
            page.screenshot(path=OUT_SHOT, full_page=True)
        except Exception as e:
            log("[快照] 失败:", repr(e))

        products = products[:TOP_N]
        log(f"[结果] 抓取到 {len(products)} 个商品")

        result = {
            "keyword": keyword,
            "url": page.url,
            "count": len(products),
            "products": [dict(rank=i + 1, **d) for i, d in enumerate(products)],
        }
        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        write_progress("done", len(products), TOP_N, keyword)
        log("[保存]", OUT_JSON)
        ctx.close()


if __name__ == "__main__":
    main()
