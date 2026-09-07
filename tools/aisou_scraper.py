# -*- coding: utf-8 -*-
"""
爱搜（dso.aidso.com）搜索词数据采集（登录态 + 浏览器渲染）
用法：python tools/aisou_scraper.py
- 关键词列表从 tools/_aisou_input.json 读取（{keywords: [...]}，由后端智能体写入）
- 登录态从 tools/aisou_localstorage.json 读取（前端「爱搜数据 → Cookie」保存的
  JSON.stringify(localStorage) 结果），核心是 token(JWT) + uid + d_m_p
- 每个关键词打开 https://dso.aidso.com/KeywordDouyin/searchWord?keyword=<kw>
  抓取：搜索词下第1个、相关词/下拉词/电商词各前5个（词名称/月覆盖人次/7日搜索人次）
- 结果导出到 tools/_aisou_output.json，由后端读取后写入「爱搜数据表」
- 全程写 tools/_aisou_progress.json 供进度轮询

实测（2026-09-01）页面结构：Element-UI el-table，登录后各 tab 列序不同：
  搜索词 tab：0词名 / 1月覆盖人次 / 2 7日搜索人次 / 3字数 ...
  相关词 tab：0词名 / 1 7日搜索人次 / 2字数 / 3相关词月覆盖 / 4类型 ...
  下拉词 tab：0词名 / 1月覆盖人次 / 2 7日搜索人次 / 3字数 ...
  电商词 tab：0词名 / 1月覆盖人次 / 2 7日搜索人次 / 3搜索点击率 ...
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
LOCALSTORAGE_FILE = os.path.join(BASE_DIR, "aisou_localstorage.json")
INPUT_FILE = os.path.join(BASE_DIR, "_aisou_input.json")
OUTPUT_FILE = os.path.join(BASE_DIR, "_aisou_output.json")
PROGRESS_FILE = os.path.join(BASE_DIR, "_aisou_progress.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 每个类型取前 N 个词（搜索词取第1个，其余取前5）
TOP_N = {"搜索词": 1, "相关词": 5, "下拉词": 5, "电商词": 5}

# 各 tab 的列索引：词名 / 月覆盖人次 / 7日搜索人次
COLS = {
    "搜索词": (0, 1, 2),
    "相关词": (0, 3, 1),   # 相关词 tab：月覆盖在列3（相关词月覆盖），7日在列1
    "下拉词": (0, 1, 2),
    "电商词": (0, 1, 2),
}


def load_localstorage():
    """读取 localStorage JSON（dict）；文件不存在或不是 dict 时返回 None"""
    if not os.path.exists(LOCALSTORAGE_FILE):
        return None
    try:
        with open(LOCALSTORAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_keywords():
    if not os.path.exists(INPUT_FILE):
        return []
    try:
        with open(INPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("keywords") or []
    except Exception:
        return []


def write_progress(status, done, total, msg=""):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump({"status": status, "done": done, "total": total, "message": msg},
                      f, ensure_ascii=False)
    except Exception:
        pass


def log(*a):
    print(*a, flush=True)


def _table_rows(page):
    """当前 tab 的表体行，每行是 td 文本列表"""
    return page.evaluate(
        """() => {
          const t = document.querySelector('table.el-table__body');
          if (!t) return [];
          return Array.from(t.querySelectorAll('tbody tr')).map(tr =>
            Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim())
          );
        }""")


def _click_tab(page, name):
    page.evaluate(
        """(name) => {
          const spans = Array.from(document.querySelectorAll('span'));
          const el = spans.find(e => (e.innerText || '').trim() === name);
          if (el) el.click();
        }""", name)
    page.wait_for_timeout(2500)


def _pick(row, name_col, month_col, seven_col, tab):
    name = row[name_col] if len(row) > name_col else ""
    month = row[month_col] if len(row) > month_col else ""
    seven = row[seven_col] if len(row) > seven_col else ""
    return {"type": tab, "name": name.strip(), "month": month, "seven": seven}


def scrape_keyword(page, keyword):
    url = "https://dso.aidso.com/KeywordDouyin/searchWord?keyword=" + quote(keyword)
    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)

    words = []
    for tab in ("搜索词", "相关词", "下拉词", "电商词"):
        _click_tab(page, tab)  # 每个 tab 都点击（含搜索词），否则 tab 切换会错位
        rows = _table_rows(page)
        name_col, month_col, seven_col = COLS[tab]
        for r in rows[:TOP_N[tab]]:
            if not r:
                continue
            w = _pick(r, name_col, month_col, seven_col, tab)
            if w["name"]:
                words.append(w)
    return words


def main():
    keywords = load_keywords()
    if not keywords:
        log("[爱搜] 无关键词，请先写入 _aisou_input.json")
        write_progress("error", 0, 0, "无关键词")
        return
    ls = load_localstorage()
    if not ls:
        log("[爱搜] 未找到登录态，请在前端「爱搜数据 → Cookie」保存 JSON.stringify(localStorage) 结果。")
        write_progress("error", 0, len(keywords), "缺少登录态")
        return

    write_progress("running", 0, len(keywords), "开始采集")
    results = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            os.path.join(BASE_DIR, "aisou_profile"),
            channel="chrome",
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # 先打开站点，注入 localStorage，再刷新让登录态生效
        page.goto("https://dso.aidso.com/", timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        page.evaluate("(kv) => { for (const k in kv) localStorage.setItem(k, kv[k]); }", ls)

        for i, kw in enumerate(keywords):
            write_progress("running", i, len(keywords), "采集 " + kw)
            try:
                words = scrape_keyword(page, kw)
            except Exception as e:
                log("[爱搜] %s 采集失败: %r" % (kw, e))
                words = []
            results.append({"keyword": kw, "words": words})
            log("[爱搜] %s 采集到 %d 个词" % (kw, len(words)))
            time.sleep(0.6)
        ctx.close()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"results": results}, f, ensure_ascii=False, indent=2)
    write_progress("done", len(keywords), len(keywords), "采集完成")
    log("[爱搜] 完成 -> %s" % OUTPUT_FILE)


if __name__ == "__main__":
    main()
