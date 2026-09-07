# -*- coding: utf-8 -*-
"""
天猫「销量前100」批量抓取（选品智能体专用）
用法：python tools/tmall_batch_scraper.py
- 关键词列表从 tools/_tmall_input.json 读取（{keywords: [...]}，由后端智能体写入）
- Cookie 复用 tools/taobao_cookie.txt（前端「天猫市场 → Cookie」保存）
- 单个浏览器会话顺序抓取每个关键词销量排序前100（标题/价格/销量/店铺名/链接）
- 结果导出到 tools/_tmall_output.json，不落库
- 全程写 tools/_tmall_progress.json 供进度轮询
"""
import json
import os
import re
import sys
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, "taobao_cookie.txt")
PROFILE_DIR = os.path.join(BASE_DIR, "taobao_profile")
INPUT_FILE = os.path.join(BASE_DIR, "_tmall_input.json")
OUTPUT_FILE = os.path.join(BASE_DIR, "_tmall_output.json")
PROGRESS_FILE = os.path.join(BASE_DIR, "_tmall_progress.json")

TOP_N = 100
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SCRAPE_JS = """
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll("a[href*='item.htm']")) {
    const href = a.href || "";
    const m = href.match(/[?&]id=(\\d+)/);
    const pid = m ? m[1] : "";
    if (!pid || seen.has(pid)) continue;
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
    seen.add(pid);
    out.push({
      id: pid,
      title: titleEl ? titleEl.innerText.trim() : null,
      price: price || null,
      sales: salesEl ? salesEl.innerText.trim() : null,
      shop: shopEl ? shopEl.innerText.trim() : null,
      link: href || null
    });
  }
  return out;
}
"""


def load_cookie():
    if not os.path.exists(COOKIE_FILE):
        return ""
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def load_keywords():
    if not os.path.exists(INPUT_FILE):
        return []
    try:
        with open(INPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("keywords") or []
    except Exception:
        return []


def parse_cookies(cookie_str):
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        if name.strip():
            cookies.append({"name": name.strip(), "value": value.strip(),
                            "domain": ".taobao.com", "path": "/"})
    return cookies


def write_progress(status, done, total, msg=""):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump({"status": status, "done": done, "total": total, "message": msg},
                      f, ensure_ascii=False)
    except Exception:
        pass


def log(*a):
    print(*a, flush=True)


def sales_num(s):
    m = re.search(r"([\d.]+)\s*([万wW亿]?)", str(s or ""))
    if not m:
        return 0.0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0.0
    unit = m.group(2)
    if unit == "亿":
        return n * 1e8
    if unit in ("万", "w", "W"):
        return n * 1e4
    return n


def price_num(s):
    m = re.search(r"([\d.]+)", str(s or ""))
    return float(m.group(1)) if m else None


def scrape_keyword(page, keyword):
    base = "https://s.taobao.com/search?q=" + quote(keyword) + "&sort=sale-desc"
    page.goto(base, timeout=60000, wait_until="domcontentloaded")
    time.sleep(3)
    products = []
    seen = set()
    offset = 0
    while len(products) < TOP_N and offset < TOP_N * 2:
        if offset:
            page.goto(base + "&s=" + str(offset), timeout=60000, wait_until="domcontentloaded")
            time.sleep(2)
        for _ in range(2):
            page.mouse.wheel(0, 1500)
            time.sleep(1)
        batch = page.evaluate(SCRAPE_JS)
        added = 0
        for p in batch:
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            products.append(p)
            added += 1
            if len(products) >= TOP_N:
                break
        if added == 0:
            break
        offset += 44
    products = products[:TOP_N]
    products.sort(key=lambda p: sales_num(p.get("sales")), reverse=True)
    return [dict(rank=i + 1, **p) for i, p in enumerate(products)]


def main():
    keywords = load_keywords()
    if not keywords:
        log("[天猫批量] 无关键词，请先写入 _tmall_input.json")
        write_progress("error", 0, 0, "无关键词")
        return
    cookie = load_cookie()
    if not cookie:
        log("[天猫批量] 未找到 Cookie，请在前端「天猫市场 → Cookie」先保存。")
        write_progress("error", 0, len(keywords), "缺少 Cookie")
        return

    write_progress("running", 0, len(keywords), "开始批量抓取")
    results = []
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel="chrome",
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ctx.add_cookies(parse_cookies(cookie))
        except Exception as e:
            log("[天猫批量] Cookie 注入失败: %r" % e)

        for i, kw in enumerate(keywords):
            write_progress("running", i, len(keywords), "抓取 " + kw)
            try:
                products = scrape_keyword(page, kw)
            except Exception as e:
                log("[天猫批量] %s 抓取失败: %r" % (kw, e))
                products = []
            results.append({"keyword": kw, "count": len(products), "products": products})
            log("[天猫批量] %s 抓取到 %d 个商品" % (kw, len(products)))
            time.sleep(1.5)
        ctx.close()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"results": results}, f, ensure_ascii=False, indent=2)
    write_progress("done", len(keywords), len(keywords), "抓取完成")
    log("[天猫批量] 完成 -> %s" % OUTPUT_FILE)


if __name__ == "__main__":
    main()
