# -*- coding: utf-8 -*-
"""
抖音「热点宝」热搜抓取（登录态）
用法：python tools/douyin_hot_scraper.py
- 抓取「搜索总榜」+「热度飙升榜」近一天热搜词，两榜合并去重（重复词只留一个）
- 数据不落数据库，只导出到 tools/_douyin_hot.json，供后端筛选接口读取
- Cookie 从 tools/douyin_hot_cookie.txt 读取（由前端「抖音热搜榜 → Cookie」保存）
- 全程写 tools/_douyin_hot_progress.json 供前端进度条轮询

接口实测（2026-09-01）：
  搜索总榜  -> board_type=0 的 data.word_list（49 条，有 hot_value）
  热度飙升榜 -> board_type=3 的 data.trending_list（50 条，hot_value=0，只有上升趋势）
"""
import json
import os
import re
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, "douyin_hot_cookie.txt")
OUT_JSON = os.path.join(BASE_DIR, "_douyin_hot.json")
PROGRESS_FILE = os.path.join(BASE_DIR, "_douyin_hot_progress.json")

API_URL = "https://www.douyin.com/aweme/v1/web/hot/search/list/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 榜单定义：board_type + 返回字段 + 中文名
BOARDS = [
    ("搜索总榜", 0, "word_list"),
    ("热度飙升榜", 3, "trending_list"),
]


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


def fetch_words(cookie, board_type, field):
    """请求一次热搜榜，返回 [{word, hot_value, position}]"""
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "detail_list": "1",
        "source": "6",
        "board_type": str(board_type),
        "pc_client_type": "1",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "platform": "PC",
    }
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.douyin.com/hot",
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie,
    }
    resp = requests.get(API_URL, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    inner = resp.json().get("data") or {}
    words = []
    for it in inner.get(field) or []:
        word = (it.get("word") or "").strip()
        if not word:
            continue
        hv = it.get("hot_value")
        # 飙升榜 hot_value 恒为 0，存空表示「无绝对热度、只有上升趋势」
        hv = hv if hv not in (None, "", 0) else ""
        words.append({"word": word, "hot_value": hv, "position": it.get("position") or ""})
    return words


def main():
    cookie = load_cookie()
    if not cookie:
        log("[热点宝] 未找到 Cookie，请在前端「抖音热搜榜 → Cookie」先保存。")
        write_progress("error", 0, len(BOARDS), "缺少 Cookie")
        return

    total_words = []
    seen = set()

    for i, (label, board_type, field) in enumerate(BOARDS):
        write_progress("running", i, len(BOARDS), "抓取" + label)
        try:
            words = fetch_words(cookie, board_type, field)
        except Exception as e:
            log("[热点宝] %s 抓取失败: %r" % (label, e))
            words = []
        added = 0
        for w in words:
            if w["word"] in seen:
                continue  # 两榜重复只留一个
            seen.add(w["word"])
            w["board"] = label
            total_words.append(w)
            added += 1
        log("[热点宝] %s 抓取到 %d 条（去重后新增 %d）" % (label, len(words), added))
        time.sleep(0.5)

    write_progress("running", len(BOARDS), len(BOARDS), "写入结果")
    today = time.strftime("%Y-%m-%d")
    result = {
        "date": today,
        "count": len(total_words),
        "words": total_words,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    write_progress("done", len(total_words), len(total_words), "抓取完成")
    log("[热点宝] 完成：去重后共 %d 条热搜词 -> %s" % (len(total_words), OUT_JSON))


if __name__ == "__main__":
    main()
