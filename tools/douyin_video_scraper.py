# -*- coding: utf-8 -*-
"""
抖音「视频作品」数据采集（登录态方案）

功能：分页拉取指定抖音账号的全部作品，导出 名称/账号/标题/点赞/评论/收藏/分享/发布时间 到 CSV。
用法：python tools/douyin_video_scraper.py

账号来源：优先读取 tools/seeding_accounts.json（由「种草监测中台」账号管理维护），
         文件不存在或为空时，回退到脚本内硬编码的 SEC_USER_ID。

依赖：pip install requests

注意：
  - 需先获取登录态 Cookie，保存到 tools/douyin_cookie.txt（已被 .gitignore 排除，勿提交）。
  - 采集他人账号的公开数据，仅建议用于个人学习研究。

获取 Cookie 方法：
  浏览器登录抖音网页版 -> F12 -> Network -> 刷新 -> 点任意 aweme 开头的请求 ->
  Headers -> Request Headers -> 复制整段 cookie 值，粘贴保存到 douyin_cookie.txt。
"""
import csv
import json
import os
import re
import sys
import time
import datetime

import requests

# 让 Windows 控制台能正确输出中文和 emoji
sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, "douyin_cookie.txt")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "seeding_accounts.json")
OUTPUT_CSV = os.path.join(BASE_DIR, "_douyin_works.csv")

# 回退账号 sec_user_id：打开对方主页，网址 https://www.douyin.com/user/ 后面的那串（MS4wLjAB...）
FALLBACK_SEC_USER_ID = "MS4wLjABAAAAmNgVBI7dikJ3OmLbDqK7G3eIF54FIURwJl0Qa8MOfCg"
FALLBACK_NAME = "默认账号"

# 以下三项来自浏览器开发者工具里任意一次 aweme 请求的 URL 参数，随设备指纹，一般可长期复用
WEBID = "7632160257455687231"
MS_TOKEN = ("dNckAiwMbpkT_I90UBeZPXIYF5qgQPi6kWJCAAT52Ydqk_F1P8T7k4weyw2AG8gtmF9PzUxti7"
            "XkeFNvq8Qn6wBwIXLPWE9vzeqgXxM7Sq-fKHbxicP7xNshKowRzVYG2l8G676hJVnIsswl1v8PeBNbhMec"
            "a4_hIA_M19burYEYqw==")
VERIFY_FP = "verify_mt2de1jh_LuRlJLSR_S87n_4byK_Alnk_uHPlt29uluUi"


def load_cookie():
    if not os.path.exists(COOKIE_FILE):
        sys.exit(f"未找到 Cookie 文件：{COOKIE_FILE}\n请按脚本顶部说明获取并保存 Cookie。")
    with open(COOKIE_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_accounts():
    """读取种草账号列表；不存在或为空时回退到硬编码账号。返回 [{name, douyin_id, sec_user_id}]"""
    accounts = []
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                accounts = raw
        except Exception as e:
            print(f"[警告] 读取账号文件失败，回退到默认账号：{e}")

    result = []
    for a in accounts:
        name = (a.get("name") or a.get("douyinId") or "").strip() or "未命名账号"
        douyin_id = (a.get("douyinId") or "").strip()
        sec = extract_sec_user_id(a.get("homepage") or "")
        if not sec:
            continue
        result.append({"name": name, "douyin_id": douyin_id, "sec_user_id": sec})

    if not result:
        result.append({
            "name": FALLBACK_NAME,
            "douyin_id": "",
            "sec_user_id": FALLBACK_SEC_USER_ID,
        })
    return result


def _extract_url(text):
    """从分享文案中提取 URL（如「长按复制此条消息… https://v.douyin.com/xxx/」）"""
    if not text:
        return ""
    s = str(text).strip()
    m = re.search(r'https?://\S+', s)
    return m.group(0).rstrip('，。；、,.;') if m else s


def _resolve_short_link(url):
    """把抖音短链（v.douyin.com/xxx）跟随重定向解析成最终 URL，失败返回原 URL"""
    if 'v.douyin.com' not in url:
        return url
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            },
            allow_redirects=True,
            timeout=15,
        )
        return resp.url
    except Exception as e:
        print(f"[警告] 短链解析失败 {url}: {e}")
        return url


def extract_sec_user_id(homepage):
    """从主页链接提取 sec_user_id；兼容分享文案/短链/完整主页链接/直接 sec_user_id"""
    if not homepage:
        return ""
    url = _extract_url(homepage)
    url = _resolve_short_link(url)
    h = str(url).strip().rstrip("/")
    if "/user/" in h:
        return h.split("/user/")[-1].split("?")[0].split("/")[0]
    if h.startswith("MS4wLjAB"):
        return h
    return ""


def build_headers():
    return {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Cookie": load_cookie(),
    }


def fetch_page(sec_user_id, max_cursor="0", count=18):
    url = "https://www.douyin.com/aweme/v1/web/aweme/post/"
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "sec_user_id": sec_user_id,
        "max_cursor": max_cursor,
        "count": str(count),
        "cookie_enabled": "true",
        "platform": "PC",
        "webid": WEBID,
        "msToken": MS_TOKEN,
        "verifyFp": VERIFY_FP,
        "fp": VERIFY_FP,
    }
    resp = requests.get(url, params=params, headers=build_headers(), timeout=20)
    resp.raise_for_status()
    return resp.json()


def ts_to_str(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def fetch_account_works(account):
    """拉取单个账号的全部作品，返回 [{名称,账号,标题,点赞,评论,收藏,分享,发布时间}]"""
    rows = []
    max_cursor = "0"
    page = 0
    while True:
        page += 1
        data = fetch_page(account["sec_user_id"], max_cursor=max_cursor)

        if data.get("status_code") != 0:
            print(f"[{account['name']} 第{page}页] 接口异常 status_code={data.get('status_code')}，可能 Cookie 失效或触发风控")
            break

        aweme_list = data.get("aweme_list", [])
        has_more = data.get("has_more", 0)
        max_cursor = str(data.get("max_cursor", 0))

        for aweme in aweme_list:
            stat = aweme.get("statistics", {})
            rows.append({
                "名称": account["name"],
                "账号": account["douyin_id"],
                "标题": aweme.get("desc", "").replace("\n", " "),
                "链接": ("https://www.douyin.com/video/" + str(aweme.get("aweme_id", ""))) if aweme.get("aweme_id") else "",
                "发布时间": ts_to_str(aweme.get("create_time")),
                "点赞": stat.get("digg_count", 0),
                "评论": stat.get("comment_count", 0),
                "收藏": stat.get("collect_count", 0),
                "分享": stat.get("share_count", 0),
            })

        print(f"[{account['name']} 第{page}页] 本页 {len(aweme_list)} 条，累计 {len(rows)} 条，has_more={has_more}")

        if not has_more or not aweme_list:
            break
        time.sleep(2)  # 分页间隔，降低风控概率
    return rows


def main():
    accounts = load_accounts()
    all_rows = []

    for account in accounts:
        all_rows.extend(fetch_account_works(account))

    if all_rows:
        fieldnames = ["名称", "账号", "标题", "链接", "点赞", "评论", "收藏", "分享", "发布时间"]
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\n✅ 共采集 {len(all_rows)} 条作品，已导出到：{OUTPUT_CSV}")
    else:
        print("\n未采集到任何作品，请检查 Cookie 是否有效")


if __name__ == "__main__":
    main()
