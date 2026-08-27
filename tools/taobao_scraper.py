# -*- coding: utf-8 -*-
"""
淘宝「拼豆智能板」销量前十抓取（登录态方案）v3
用法：python tools/taobao_scraper.py
- 首次运行弹出 Chrome，扫码登录一次；cookie 持久化到 tools/taobao_profile/
- 登录后自动点击「销量」排序，抓取前十的 标题/价格/销量/封面图 -> tools/_taobao_top10.json
"""
import json
import os
import re
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright

KEYWORD = "拼豆智能板"
SEARCH_URL = "https://s.taobao.com/search?q=" + quote(KEYWORD)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "taobao_profile")
OUT_JSON = os.path.join(BASE_DIR, "_taobao_top10.json")
OUT_HTML = os.path.join(BASE_DIR, "_taobao_page.html")
OUT_SHOT = os.path.join(BASE_DIR, "_taobao_shot.png")

LOGIN_WAIT = int(os.environ.get("TB_LOGIN_WAIT", "300"))
TOP_N = 10

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")

LOGIN_COOKIES = {"tracknick", "unb", "_nk_"}


def log(*a):
    print(*a, flush=True)


def cookie_state(page):
    try:
        return {c["name"] for c in page.context.cookies("https://www.taobao.com")}
    except Exception:
        return set()


def is_logged_in(page):
    return bool(cookie_state(page) & LOGIN_COOKIES)


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
      image: imgEl ? (imgEl.getAttribute("data-src") || imgEl.src || null) : null
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
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(SEARCH_URL, timeout=60000, wait_until="domcontentloaded")

        # --- 登录处理 ---
        if is_logged_in(page):
            log("[登录] 已登录（复用本地会话）")
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
                ctx.close()
                return

        # --- 重新加载 + 点击「销量」排序 ---
        page.goto(SEARCH_URL, timeout=60000, wait_until="domcontentloaded")
        time.sleep(3)
        click_sales_sort(page)

        # 等待结果重渲染 + 滚动触发图片懒加载
        for i in range(6):
            time.sleep(2)
            page.mouse.wheel(0, 1500)
        time.sleep(2)

        # 落盘快照
        try:
            open(OUT_HTML, "w", encoding="utf-8").write(page.content())
            page.screenshot(path=OUT_SHOT, full_page=True)
        except Exception as e:
            log("[快照] 失败:", repr(e))

        products = page.evaluate(SCRAPE_JS)[:TOP_N]
        log(f"[结果] 抓取到 {len(products)} 个商品")

        result = {
            "keyword": KEYWORD,
            "url": page.url,
            "count": len(products),
            "products": [dict(rank=i + 1, **d) for i, d in enumerate(products)],
        }
        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        log("[保存]", OUT_JSON)
        ctx.close()


if __name__ == "__main__":
    main()
