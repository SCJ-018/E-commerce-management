# -*- coding: utf-8 -*-
"""
抖音「视频作品」数据采集 —— Playwright 真实浏览器方案（2026-09-17 重写）

为什么必须用浏览器（不是 cookie 的问题）
--------------------------------------
旧版是纯 requests 直打 `/aweme/v1/web/aweme/post/`，被 Argus 拦：

    HTTP 403 Blocked by ArgusSecurityPlugin Uifid Not Found

2026-09-17 用三步只读探针（服务器 `/opt/pw/_dy_probe{1,2,3}.py`）实测定性：

  1) 真正缺的是 **`a_bogus` 动态签名** 与 **浏览器指纹（uifid/ttwid）**，
     **与服务器机房 IP 无关** —— 实测出口 119.45.187.154 畅通；
  2) 换成「真实 Chrome + 页面内 fetch」后，匿名就能拿到 aweme_list（HTTP 200 / status_code=0）；
  3) ★ **匿名只给第 1 页 18 条**：响应里 `not_login_module.guide_login_tip_exist=true`
     （文案「看更多最新作品」），第 2 页直接返回空列表；
     **带 cookie 才能翻页取全量**（实测 21 / 25 条，`has_more=0`、`not_login_module=None`）。

所以本版：Playwright 起真实浏览器 → 注入 douyin_cookie.txt → 在页面上下文里 fetch 翻页。

运行环境
--------
服务器（由后端 `_seeding_launch()` 自动用这个组合拉起）：

    xvfb-run -a /opt/pw/venv/bin/python /opt/ecom/tools/douyin_video_scraper.py

本地 Windows 调试：直接 `python tools/douyin_video_scraper.py`（会弹出可见浏览器窗口）。

依赖：playwright（服务器在 `/opt/pw/venv`，与抖店链路同源配置）

输出口径（2026-09-17 用户确认）
------------------------------
  CSV **只写本轮真实抓取到的作品**，不回填任何历史数据：
    · 账号无公开作品（被清空/设为私密/注销/废除）→ 不写它的行，作品列表里自然消失；
    · 消失的作品由后端 `_seeding_reconcile_guard()` 对比快照后记入「被删作品」，
      在那里可见、可单条清除；账号以后又有作品了，抓到了就自然重新出现。

对外接口（与旧版完全一致，前端 / 对账 / 体检都无需改动）
----------------------------------------------------
  _douyin_works.csv                九列，utf-8-sig
  _seeding_progress_douyin.json    {status,done,total,ts,msg,accounts,ok}
退出码：0 = 全部账号成功；3 = 有账号失败或无数据；1 = 异常
"""
import csv
import datetime
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, "douyin_cookie.txt")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "seeding_accounts.json")
OUTPUT_CSV = os.path.join(BASE_DIR, "_douyin_works.csv")
PROGRESS_FILE = os.path.join(BASE_DIR, "_seeding_progress_douyin.json")

# 与 /opt/pw/doudian/fetch_daily.py 完全一致的启动配置（那条链路已验证可用）
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled",
               "--no-first-run", "--no-default-browser-check", "--no-sandbox"]
INIT_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
HOME_URL = "https://www.douyin.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

PAGE_COUNT = 18        # 每页条数（实测 18 有效）
MAX_PAGES = 12         # 单账号最大翻页（18*12=216 条，远超实际；中位数仅 8 条）
PAGE_DELAY_MS = 900    # 翻页间隔

# ★ 2026-09-18 起删除了「回退账号」：
#   账号文件缺失/为空时不再拿一个硬编码账号去抓（那会把别人的作品混进数据里，
#   用户也会误以为"还有数据在更新"）。改为直接跳过本轮、进度写 skipped，见 main()。

FIELDNAMES = ["名称", "账号", "标题", "链接", "点赞", "评论", "收藏", "分享", "发布时间"]


# ============================ 进度 / 日志 ============================

def write_progress(status, done, total, msg="", accounts=None, ok=None):
    """写进度文件，供后端 /status 接口读取。

    accounts/ok = 账号总数/成功数，供后端判断「本轮是否完整」，
    不完整时后端会跳过「被删作品」对比，避免把抓取失败误判成作品被删。
    """
    data = {"status": status, "done": done, "total": total, "ts": time.time(), "msg": msg}
    if accounts is not None:
        data["accounts"] = accounts
    if ok is not None:
        data["ok"] = ok
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def log(msg):
    print(msg, flush=True)


# ============================ 账号 / Cookie ============================

def load_cookie_list():
    """把分号分隔的 cookie 字符串转成 Playwright cookie 对象列表。

    缺失/为空不算致命：能匿名跑，但只能取第 1 页 18 条（见文件头说明），
    脚本会在末尾把它标成 error 而非 done，交给体检告警。
    """
    if not os.path.exists(COOKIE_FILE):
        return [], "未找到 Cookie 文件：%s" % COOKIE_FILE
    try:
        raw = open(COOKIE_FILE, "r", encoding="utf-8", errors="replace").read().strip()
    except Exception as e:
        return [], "读取 Cookie 文件失败：%s" % e
    out = []
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out.append({"name": k, "value": v, "domain": ".douyin.com", "path": "/"})
    if not out:
        return [], "Cookie 文件内容为空或格式不对：%s" % COOKIE_FILE
    return out, ""


def _extract_url(text):
    """从分享文案中提取 URL（如「长按复制此条消息… https://v.douyin.com/xxx/」）"""
    if not text:
        return ""
    s = str(text).strip()
    m = re.search(r'https?://\S+', s)
    return m.group(0).rstrip('，。；、,.;') if m else s


def _resolve_short_link(url):
    """短链跟随重定向解析（用标准库 urllib，零第三方依赖）"""
    if 'v.douyin.com' not in url:
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.geturl()
    except Exception as e:
        log("[警告] 短链解析失败 %s: %s" % (url, str(e)[:120]))
        return url


def extract_sec_user_id(homepage):
    """从主页链接提取 sec_user_id；兼容分享文案/短链/完整主页链接/直接 sec_user_id"""
    if not homepage:
        return ""
    url = _resolve_short_link(_extract_url(homepage))
    h = str(url).strip().rstrip("/")
    if "/user/" in h:
        return h.split("/user/")[-1].split("?")[0].split("/")[0]
    if h.startswith("MS4wLjAB"):
        return h
    return ""


def load_accounts():
    """读取种草账号列表；不存在 / 为空 / 全部解析失败时返回 []（由 main 优雅跳过，不报错）。

    返回 [{name, douyin_id, sec_user_id}]
    """
    accounts = []
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                accounts = raw
        except Exception as e:
            log("[警告] 读取账号文件失败，本轮按「无账号」处理：%s" % e)

    result = []
    seen = {}   # sec_user_id -> 首次出现的账号名（同一账号配了多条时只抓一次）
    skipped = []
    for a in accounts:
        # ★ 只处理抖音账号：seeding_accounts.json 是「抖音 + 小红书」混放的一张表，
        #   旧版会把小红书账号（platform='xhs'）也拿去解析抖音短链 → 白跑一次 + 刷警告
        if (a.get("platform") or "douyin").strip().lower() != "douyin":
            continue
        name = (a.get("name") or a.get("douyinId") or "").strip() or "未命名账号"
        douyin_id = (a.get("douyinId") or "").strip()
        sec = extract_sec_user_id(a.get("homepage") or "")
        if not sec:
            skipped.append(name)
            continue
        # ★ 按 sec_user_id 去重：同一账号被配两条时重复抓只会白跑一轮、并在 CSV 里出重复行
        if sec in seen:
            log("[跳过重复账号] %s（与「%s」是同一账号）" % (name, seen[sec]))
            continue
        seen[sec] = name
        result.append({"name": name, "douyin_id": douyin_id, "sec_user_id": sec})

    if skipped:
        log("[警告] %d 个账号主页链接解析不出 sec_user_id，已跳过：%s"
            % (len(skipped), "、".join(skipped[:8])))

    return result


# ============================ 浏览器取数 ============================

# 单账号翻页取数：在页面上下文里 fetch，签名/指纹由页面自身提供
FETCH_ACCOUNT_JS = r"""
async (args) => {
  const {sec, count, maxPages, delayMs} = args;
  const sleep = ms => new Promise(s => setTimeout(s, ms));
  let cursor = '0', page = 0, rows = [], errs = [];
  let loginTip = null, lastStatus = null, statusCode = null;
  while (page < maxPages) {
    const u = '/aweme/v1/web/aweme/post/?device_platform=webapp&aid=6383'
      + '&channel=channel_pc_web&sec_user_id=' + sec
      + '&max_cursor=' + cursor + '&count=' + count
      + '&cookie_enabled=true&platform=PC';
    let j = null;
    try {
      const r = await fetch(u, {credentials: 'include'});
      lastStatus = r.status;
      j = await r.json();
    } catch (e) {
      errs.push('第' + (page + 1) + '页请求失败: ' + String(e).slice(0, 90));
      break;
    }
    statusCode = (typeof j.status_code === 'undefined') ? null : j.status_code;
    if (statusCode !== null && statusCode !== 0) {
      errs.push('第' + (page + 1) + '页 status_code=' + statusCode
                + ' ' + (j.status_msg || ''));
    }
    if (page === 0) {
      // ★ 登录态判据：匿名访问时这里会带「看更多最新作品」引导，且后续页拿不到数据
      loginTip = j.not_login_module || null;
    }
    const list = j.aweme_list || [];
    for (const it of list) {
      const st = it.statistics || {};
      rows.push({
        aweme_id: it.aweme_id,
        desc: (it.desc || '').replace(/\s+/g, ' '),
        create_time: it.create_time,
        digg: st.digg_count, comment: st.comment_count,
        collect: st.collect_count, share: st.share_count
      });
    }
    page++;
    if (!j.has_more || !list.length) break;
    if (j.max_cursor) cursor = String(j.max_cursor);
    await sleep(delayMs);
  }
  return {page: page, rows: rows, errs: errs, loginTip: loginTip,
          lastStatus: lastStatus, statusCode: statusCode};
}
"""


def ts_to_str(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def fetch_account(page, account):
    """拉取单个账号的全部作品。返回 (九列 rows, 详情 dict)"""
    res = page.evaluate(FETCH_ACCOUNT_JS, {
        "sec": account["sec_user_id"], "count": PAGE_COUNT,
        "maxPages": MAX_PAGES, "delayMs": PAGE_DELAY_MS,
    }) or {}
    rows = []
    for it in res.get("rows") or []:
        aweme_id = it.get("aweme_id")
        rows.append({
            "名称": account["name"],
            "账号": account["douyin_id"],
            "标题": it.get("desc") or "",
            "链接": ("https://www.douyin.com/video/" + str(aweme_id)) if aweme_id else "",
            "点赞": it.get("digg", 0),
            "评论": it.get("comment", 0),
            "收藏": it.get("collect", 0),
            "分享": it.get("share", 0),
            "发布时间": ts_to_str(it.get("create_time")),
        })
    return rows, res


def _watchdog(limit_sec, total):
    """兜底看门狗：浏览器卡死时不让进度永远停在 running"""
    def _kill():
        log("\n[看门狗] 超过 %d 秒未完成，强制退出" % limit_sec)
        write_progress("error", 0, total, "抓取超时（看门狗 %d 秒触发）" % limit_sec,
                       accounts=total, ok=0)
        os._exit(2)
    t = threading.Timer(limit_sec, _kill)
    t.daemon = True
    t.start()
    return t


def main():
    from playwright.sync_api import sync_playwright

    headless = "--headless" in sys.argv
    limit = 0
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            try:
                limit = int(sys.argv[i + 1])
            except Exception:
                pass

    accounts = load_accounts()
    # ★ 账号为空 = 没东西可抓，不是错误（2026-09-18）：
    #   原来会回退到硬编码账号去抓，把无关作品混进 CSV；现在直接跳过本轮，
    #   进度写 skipped（体检脚本视作健康，不推钉钉告警，也不判「停更」），
    #   不触碰 _douyin_works.csv，上一次的数据保持原样。
    if not accounts:
        msg = "未配置可抓取的抖音账号（需要在「种草账号」里添加抖音账号并填主页链接），本轮跳过"
        log("[跳过] " + msg)
        write_progress("skipped", 0, 0, msg)
        return
    if limit > 0:
        accounts = accounts[:limit]
        log("[调试] --limit %d，只抓前 %d 个账号" % (limit, len(accounts)))
    total = len(accounts)

    cookie_list, cookie_err = load_cookie_list()
    if cookie_err:
        log("[警告] %s" % cookie_err)
        log("[警告] 将匿名抓取 —— 每账号只能拿到第 1 页 %d 条，本轮记为 error" % PAGE_COUNT)
    else:
        log("已载入 Cookie：%d 条字段" % len(cookie_list))

    write_progress("running", 0, total, accounts=total, ok=0)
    _watchdog(600 + total * 20, total)

    all_rows = []
    failed_accounts = []
    empty_accounts = []     # 接口正常但 0 条 = 账号当前无公开作品（非抓取失败）
    login_missing = False
    t_start = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=headless, args=LAUNCH_ARGS)
        try:
            ctx = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai",
                                      viewport={"width": 1600, "height": 950},
                                      user_agent=UA)
            ctx.set_default_timeout(120000)
            ctx.add_init_script(INIT_JS)

            if cookie_list:
                try:
                    ctx.add_cookies(cookie_list)
                except Exception as e:
                    log("[警告] Cookie 批量注入失败，逐条重试：%s" % str(e)[:140])
                    n = 0
                    for c in cookie_list:
                        try:
                            ctx.add_cookies([c])
                            n += 1
                        except Exception:
                            pass
                    log("[警告] 逐条注入成功 %d/%d 条" % (n, len(cookie_list)))

            page = ctx.new_page()

            # 先开首页，让页面把 ttwid / 指纹环境建立起来（签名由页面 JS 提供）
            log("[1/2] 打开抖音首页建立会话 ...")
            try:
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log("[警告] 首页加载异常（继续尝试抓取）：%s" % str(e)[:160])
            time.sleep(6)

            # 兜底：cookie 里若有 douyin 域之外的字段，补一次
            log("[2/2] 开始按账号抓取（共 %d 个）..." % total)
            for i, account in enumerate(accounts):
                tag = "[%d/%d] %s" % (i + 1, total, account["name"])
                try:
                    rows, res = fetch_account(page, account)
                except Exception as e:
                    log("%s 抓取异常: %s" % (tag, str(e)[:200]))
                    failed_accounts.append(account["name"])
                    write_progress("running", i + 1, total, accounts=total,
                                   ok=total - len(failed_accounts))
                    continue

                if res.get("loginTip"):
                    login_missing = True
                errs = res.get("errs") or []
                status_code = res.get("statusCode")
                if rows:
                    all_rows.extend(rows)
                    if errs:
                        failed_accounts.append(account["name"])
                        log("%s 取到 %d 条，但有 %d 处异常：%s"
                            % (tag, len(rows), len(errs), errs[:2]))
                    else:
                        log("%s 取到 %d 条（%s 页）" % (tag, len(rows), res.get("page")))
                elif not errs and status_code == 0:
                    # ★ 接口 200 + status_code=0 + 空列表 = 账号当前无公开作品
                    #   （实测：作品被设私密/清空/账号注销/账号废除）。这不是抓取失败 → 不告警；
                    #   也不回填历史行 —— 消失的作品由后端转入「被删作品」列表。
                    empty_accounts.append(account["name"])
                    log("%s 接口正常但无作品（账号当前无公开作品 → 历史作品转入「被删作品」）" % tag)
                else:
                    log("%s 未取到作品（页数=%s HTTP=%s status_code=%s errs=%s）"
                        % (tag, res.get("page"), res.get("lastStatus"),
                           status_code, errs or "无"))
                    failed_accounts.append(account["name"])

                write_progress("running", i + 1, total, accounts=total,
                               ok=total - len(failed_accounts))
        finally:
            try:
                browser.close()
            except Exception:
                pass

    elapsed = time.time() - t_start
    ok_cnt = total - len(failed_accounts)

    # ---- 收尾：三种结局，分开处理 ----
    # A) 登录态缺失 → 数据是「缩水版」（每账号仅第 1 页），绝不能覆盖完整旧数据，
    #    否则后端对比会把它判成「大量作品被删」（2026-09-17 那 61 条误报的同源坑）。
    if login_missing and all_rows:
        msg = ("登录态失效：每账号仅取到第 1 页 %d 条，已放弃覆盖数据文件"
               "（共 %d 条候选、%d/%d 个账号）。请更新 tools/douyin_cookie.txt"
               % (PAGE_COUNT, len(all_rows), ok_cnt, total))
        log("\n❌ %s" % msg)
        write_progress("error", 0, total, msg, accounts=total, ok=0)
        sys.exit(3)

    # C) 账号当前无公开作品 → 不回填历史行（作品数据只展示「本轮真实抓取」结果）
    #    ★ 口径（2026-09-17 用户确认）：废除/清空/设为私密的账号就**不返回作品**；
    #      它们消失的作品由后端对比上一次快照后记入「被删作品」列表
    #      （可见、可单条清除）；抓到了就自然重新出现在作品列表里。
    #      所以这里既不告警（不算抓取失败），也不把旧行塞回 CSV 伪造"还在"。
    if empty_accounts:
        log("[无公开作品] %d 个账号当前无公开作品，其历史作品将转入「被删作品」：%s"
            % (len(empty_accounts), "、".join(empty_accounts[:6])))

    if all_rows:
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(all_rows)
        msg = "共采集 %d 条作品（成功 %d/%d 个账号，耗时 %.0fs）" % (
            len(all_rows), ok_cnt, total, elapsed)
        if empty_accounts:
            msg += "；无公开作品：%s" % "、".join(empty_accounts[:6])
        if failed_accounts:
            msg += "；失败账号：%s" % "、".join(failed_accounts[:8])
        log("\n✅ %s，已导出到：%s" % (msg, OUTPUT_CSV))
        write_progress("done", total, total, msg, accounts=total, ok=ok_cnt)
        if failed_accounts:
            sys.exit(3)
        return

    # B) 一条都没取到 → 不覆盖旧 CSV
    msg = "未采集到任何作品（成功 0/%d 个账号），请检查 Cookie 是否有效" % total
    log("\n" + msg)
    write_progress("error", 0, total, msg, accounts=total, ok=0)
    sys.exit(3)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        main()
    except SystemExit as e:
        # main() 之外的 sys.exit（例如 import 失败）会让进度停在 running，
        # 补写一次 error，避免被误判成「卡死」（症状是数据永不再更新）
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if code != 0:
            try:
                p = json.load(open(PROGRESS_FILE, encoding="utf-8"))
            except Exception:
                p = {}
            if (p.get("status") or "") == "running":
                write_progress("error", p.get("done", 0), p.get("total", 0),
                               "脚本提前退出（exit %s）" % code)
        raise
