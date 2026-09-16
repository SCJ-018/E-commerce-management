# -*- coding: utf-8 -*-
"""
抖音热点宝「聚合搜索榜」全量抓取（登录态 + 翻页）
用法：python tools/douyin_hot_scraper.py
- 数据源：https://douhot.douyin.com/square/hotspot?active_tab=hotspot_search&date_window=24
- 抓两个榜（近 1 天 date_window=24）：
    搜索总榜         sub_type=3001   total=10000
    热度飙升搜索榜    sub_type=3002   total=10000
- 接口：POST /douhot/v1/dashboard/hot_search/query_list
        body: {"page_num": n, "page_size": 50, "date_window": 24, "sub_type": 300x}
        无需 X-Bogus/_signature 签名（实测 2026-09-15），纯 requests 直连即可
- page_size 上限实测 50（传更大也只返回 50）→ 每榜 200 页，两榜合计约 2 万条
- 两榜按词去重（同词保留搜索总榜的热度值）
- 数据不落数据库，只导出到 tools/_douyin_hot.json，供后端筛选接口读取
- Cookie 从 tools/douyin_hot_cookie.txt 读取（douhot.douyin.com 域的登录态，
  由前端「抖音热搜榜 → Cookie」保存）
- 全程写 tools/_douyin_hot_progress.json 供前端进度条轮询

接口字段（实测 2026-09-15）：
  data.total_count   该榜总数
  data.search_list[] 每项: key_word（词）, search_score（热度值）, trends[]（趋势，不用）
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, "douyin_hot_cookie.txt")
OUT_JSON = os.path.join(BASE_DIR, "_douyin_hot.json")
PROGRESS_FILE = os.path.join(BASE_DIR, "_douyin_hot_progress.json")

API_URL = "https://douhot.douyin.com/douhot/v1/dashboard/hot_search/query_list"
PAGE_SIZE = 50          # 实测上限
MAX_PAGES = 260         # 单榜保护上限（200 页够 1 万条）
WORKERS = 6             # 并发请求数
DATE_WINDOW = 24        # 近 1 天
PAGES_PER_BOARD = 210   # 进度展示用的单榜粗略页数

# 榜单定义：名称 + sub_type
BOARDS = [
    ("搜索总榜", 3001),
    ("热度飙升搜索榜", 3002),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def load_cookie():
    if not os.path.exists(COOKIE_FILE):
        return ""
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def write_progress(status, done, total, msg=""):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump({"status": status, "done": done, "total": total, "message": msg},
                      f, ensure_ascii=False)
    except Exception:
        pass


def log(*a):
    print(*a, flush=True)


def make_session(cookie):
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": "https://douhot.douyin.com/square/hotspot?active_tab=hotspot_search&date_window=24&sub_type=3001",
        "Origin": "https://douhot.douyin.com",
        "Content-Type": "application/json",
        "Cookie": cookie,
    })
    return s


def fetch_page(session, sub_type, page_num, retries=3):
    """抓一页，返回 search_list（可能为空列表）。失败抛异常。"""
    body = {"page_num": page_num, "page_size": PAGE_SIZE,
            "date_window": DATE_WINDOW, "sub_type": sub_type}
    last_err = None
    for attempt in range(retries):
        try:
            r = session.post(API_URL, json=body, timeout=20)
            j = r.json()
            if j.get("code") != 0:
                raise RuntimeError("code=%s msg=%s" % (j.get("code"), j.get("message")))
            return (j.get("data") or {}).get("search_list") or []
        except Exception as e:
            last_err = e
            time.sleep(0.8 * (attempt + 1))
    raise last_err


def fetch_total(session, sub_type):
    """拿该榜 total_count，失败返回 None"""
    try:
        r = session.post(API_URL, json={"page_num": 1, "page_size": PAGE_SIZE,
                                        "date_window": DATE_WINDOW, "sub_type": sub_type},
                         timeout=20)
        return ((r.json().get("data") or {}).get("total_count")) or None
    except Exception:
        return None


def fetch_board(session, label, sub_type, progress_base, progress_total):
    """抓一个榜的全量，返回 [{word, hot_value, position}]。失败抛异常。"""
    first = fetch_page(session, sub_type, 1)
    total = fetch_total(session, sub_type) or 10000  # 实测近 1 天两榜都是 10000
    total_pages = min((total + PAGE_SIZE - 1) // PAGE_SIZE, MAX_PAGES)

    pages = {1: first}
    lock_done = [1]

    def worker(p):
        lst = fetch_page(session, sub_type, p)
        lock_done.append(1)
        write_progress("running", progress_base + len(lock_done), progress_total,
                       "抓取%s %d/%d 页" % (label, len(lock_done), total_pages))
        return p, lst

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(worker, p): p for p in range(2, total_pages + 1)}
        for fut in as_completed(futs):
            p, lst = fut.result()
            pages[p] = lst

    words = []
    for p in range(1, total_pages + 1):
        for i, it in enumerate(pages.get(p) or []):
            word = (it.get("key_word") or "").strip()
            if not word:
                continue
            words.append({"word": word,
                          "hot_value": it.get("search_score") or "",
                          "position": (p - 1) * PAGE_SIZE + i + 1})
    log("[热点宝] %s sub_type=%s total=%s 实抓=%d 条（%d 页）" % (label, sub_type, total, len(words), total_pages))
    return words


def main():
    cookie = load_cookie()
    if not cookie:
        log("[热点宝] 未找到 Cookie，请在前端「抖音热搜榜 → Cookie」先保存 douhot.douyin.com 的登录态。")
        write_progress("error", 0, 1, "缺少 Cookie")
        return

    session = make_session(cookie)
    progress_total = len(BOARDS) * PAGES_PER_BOARD

    all_words = []
    seen = set()
    dup = 0
    for bi, (label, sub_type) in enumerate(BOARDS):
        progress_base = bi * PAGES_PER_BOARD
        write_progress("running", progress_base, progress_total, "抓取" + label)
        try:
            words = fetch_board(session, label, sub_type, progress_base, progress_total)
        except Exception as e:
            log("[热点宝] %s 抓取失败: %r" % (label, e))
            write_progress("error", progress_base, progress_total, "抓取%s失败: %s" % (label, e))
            return
        added = 0
        for w in words:
            if w["word"] in seen:
                dup += 1
                continue
            seen.add(w["word"])
            w["board"] = label
            all_words.append(w)
            added += 1
        log("[热点宝] %s 去重后新增 %d 条" % (label, added))

    write_progress("running", progress_total, progress_total, "写入结果")
    today = time.strftime("%Y-%m-%d")
    result = {"date": today, "count": len(all_words), "dup_removed": dup, "words": all_words}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    write_progress("done", len(all_words), len(all_words), "抓取完成")
    log("[热点宝] 完成：两榜去重后共 %d 条（重复剔除 %d）-> %s" % (len(all_words), dup, OUT_JSON))


if __name__ == "__main__":
    main()
