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
HEADLESS = os.environ.get("TAOBAO_HEADLESS", "1").strip().lower() not in ("0", "false", "no")
VERIFY_WAIT = int(os.environ.get("TAOBAO_VERIFY_WAIT", "300"))
KEYWORD_DELAY = float(os.environ.get("TAOBAO_KEYWORD_DELAY", "8"))
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


def load_request():
    if not os.path.exists(INPUT_FILE):
        return [], ""
    try:
        with open(INPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("keywords") or [], str(data.get("request_id") or "")
    except Exception:
        return [], ""


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


def access_block_reason(page):
    """识别淘宝访问验证，避免把风控页误记成“0 个销量样本”。"""
    try:
        blockers = page.locator("iframe[src*='/punish'], iframe[src*='_____tmd_____'], .baxia-dialog, .J_MIDDLEWARE_FRAME_WIDGET")
        for i in range(min(blockers.count(), 20)):
            if blockers.nth(i).is_visible():
                return "淘宝触发滑块访问验证"
        if "_____tmd_____" in page.url or "/punish" in page.url:
            return "淘宝触发访问验证"
    except Exception:
        pass
    return ""


def dismiss_access_dialogs(page, rounds=12):
    """按淘宝验证层自带的关闭按钮逐层关闭，直到页面不再出现遮罩。"""
    closed = 0
    for _ in range(rounds):
        clicked = False
        for selector in (".baxia-dialog-close", ".J_MIDDLEWARE_FRAME_WIDGET > img"):
            buttons = page.locator(selector)
            for i in range(min(buttons.count(), 20)):
                button = buttons.nth(i)
                try:
                    if button.is_visible():
                        button.click(force=True, timeout=3000)
                        closed += 1
                        clicked = True
                        time.sleep(0.4)
                except Exception:
                    continue
        if not clicked:
            break
        time.sleep(0.8)
    if closed:
        log("[天猫批量] 已连续关闭 %d 个访问验证弹层" % closed)
    return closed


def wait_for_access(page):
    dismiss_access_dialogs(page)
    reason = access_block_reason(page)
    if not reason:
        return
    if HEADLESS:
        raise RuntimeError(reason)
    log("[天猫批量] 检测到滑块验证，请在可见浏览器中完成验证，最长等待 %d 秒" % VERIFY_WAIT)
    deadline = time.time() + VERIFY_WAIT
    while time.time() < deadline:
        time.sleep(2)
        dismiss_access_dialogs(page)
        if not access_block_reason(page):
            time.sleep(3)
            log("[天猫批量] 访问验证已通过，继续抓取")
            return
    raise RuntimeError("淘宝滑块验证等待超时")


def scrape_keyword(page, keyword):
    base = "https://s.taobao.com/search?q=" + quote(keyword) + "&sort=sale-desc"
    page.goto(base, timeout=60000, wait_until="domcontentloaded")
    time.sleep(3)
    wait_for_access(page)
    try:
        page.wait_for_selector("a[href*='item.htm']", timeout=15000)
    except Exception:
        dismiss_access_dialogs(page)
    products = []
    seen = set()
    offset = 0
    while len(products) < TOP_N and offset < TOP_N * 2:
        if offset:
            page.goto(base + "&s=" + str(offset), timeout=60000, wait_until="domcontentloaded")
            time.sleep(2)
            wait_for_access(page)
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
    keywords, request_id = load_request()
    if not keywords:
        log("[天猫批量] 无关键词，请先写入 _tmall_input.json")
        write_progress("error", 0, 0, "无关键词")
        return 2
    cookie = load_cookie()
    if not cookie:
        log("[天猫批量] 未找到 Cookie，请在前端「天猫市场 → Cookie」先保存。")
        write_progress("error", 0, len(keywords), "缺少 Cookie")
        return 2

    write_progress("running", 0, len(keywords), "开始批量抓取")
    results = []
    failures = []
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel="chrome",
            headless=HEADLESS,
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
                failures.append({"keyword": kw, "message": str(e)})
            results.append({"keyword": kw, "count": len(products), "products": products})
            log("[天猫批量] %s 抓取到 %d 个商品" % (kw, len(products)))
            time.sleep(KEYWORD_DELAY)
        ctx.close()

    success_count = sum(1 for r in results if r.get("products"))
    status = "done" if success_count == len(results) else "partial" if success_count else "error"
    message = "抓取完成" if status == "done" else (failures[0]["message"] if failures else "未抓到有效商品")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"request_id": request_id, "status": status, "message": message,
                   "success_count": success_count, "failures": failures, "results": results},
                  f, ensure_ascii=False, indent=2)
    write_progress(status, len(keywords), len(keywords), message)
    log("[天猫批量] 完成 -> %s" % OUTPUT_FILE)
    return 0 if success_count else 2


if __name__ == "__main__":
    sys.exit(main() or 0)
