# -*- coding: utf-8 -*-
"""爱搜（dso.aidso.com）搜索词数据采集（登录态 + 接口取数）

用法：python tools/aisou_scraper.py
- 关键词列表从 tools/_aisou_input.json 读取（{keywords: [...]}，由后端智能体写入）
- 登录态从 tools/aisou_localstorage.json 读取（JSON.stringify(localStorage)，核心是
  token(JWT) + uid + d_m_p）。登录态失效时接口会返回 {"code":800,"msg":"请先登录账号"}，
  页面静默降级为「游客态」并展示示例榜单 —— 此时 10 个关键词会抓到完全一样的词
  （典型症状：「搜索词/什么」「下拉词/在抖音、抖音号、本人抖音」）。
  ★ 重新登录：本机跑 tools/aisou_login.py，再把 aisou_localstorage.json 传到服务器。
- 结果导出 tools/_aisou_output.json，由后端写入「爱搜数据表」
- 全程写 tools/_aisou_progress.json 供进度轮询

取数方式（2026-09-17 实测，登录态正常时）：
  页面 https://dso.aidso.com/KeywordDouyin/searchWord?keyword=<kw>
  首屏即「搜索词」tab，其余 tab 需点击；每次点击触发对应接口（POST，body 同构）：
    搜索词  keyword/library_v2/list          {"orders":[{"column":"month_cover_count","asc":false}],"page_no":1,"page_size":20,"params_data":{"keyword":"<kw>"}}
    相关词  keyword/library_v2/list_relation  同上
    下拉词  keyword/library_v2/list_down      同上
    电商词  keyword/library_v2/e_commerce     同上（排序字段 gmv_index）
  返回 data.result[]：词名在 keyword（相关词在 relation_keyword），月覆盖在 month_cover_count。

表格列（用于取「7日搜索人次」= 单元格里的「平均:xxx」，接口未直接返回）：
    搜索词 tab：0词名 / 1月覆盖人次 / 2  7日搜索人次 / 3字数 / 4下拉词数量 / 5下拉词月覆盖
    相关词 tab：0词名 / 1  7日搜索人次 / 2字数 / 3相关词月覆盖 / 4类型
    下拉词 tab：0词名 / 1月覆盖人次 / 2  7日搜索人次 / 3字数
    电商词 tab：0词名 / 1月覆盖人次 / 2  7日搜索人次 / 3搜索点击率 ...

设计要点：
- 词名/月覆盖 一律取**接口**字段（原始整数，不受表格列序变动影响）；
- 「7日搜索人次」接口不返回，只能从表格单元格取；
- 启动先做**登录态自检**（user/info），失效则直接报错退出，绝不静默写垃圾数据；
- 若多个关键词抓到的词完全一致，判定为示例数据并告警；
- ★ 等接口响应必须用 `page.wait_for_timeout()` 轮询，**不能用 `time.sleep()`**：
  playwright 同步 API 只在自身调用期间派发 response 事件，纯 sleep 期间 handler 不触发，
  会白等满超时（实测 4 分钟才跑 1 个关键词 → 改正后约 25 秒/词）。
"""
import json
import os
import re
import sys
from urllib.parse import quote

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCALSTORAGE_FILE = os.path.join(BASE_DIR, "aisou_localstorage.json")
INPUT_FILE = os.path.join(BASE_DIR, "_aisou_input.json")
OUTPUT_FILE = os.path.join(BASE_DIR, "_aisou_output.json")
PROGRESS_FILE = os.path.join(BASE_DIR, "_aisou_progress.json")
PROFILE_DIR = os.path.join(BASE_DIR, "aisou_profile")

SEARCH_URL = "https://dso.aidso.com/KeywordDouyin/searchWord?keyword="
HOME_URL = "https://dso.aidso.com/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 每个类型取前 N 个词（搜索词取第1个，其余取前5）
TOP_N = {"搜索词": 1, "相关词": 5, "下拉词": 5, "电商词": 5}

# (tab 名称, 接口尾名, 接口里词名字段)
# 首项「搜索词」是页面默认激活的 tab，页面加载时即触发，不需要点击
TABS = [
    ("搜索词", "list", "keyword"),
    ("相关词", "list_relation", "relation_keyword"),
    ("下拉词", "list_down", "keyword"),
    ("电商词", "e_commerce", "keyword"),
]
API_TABS = ("list", "list_relation", "list_down", "e_commerce")

# 各 tab 的列索引：词名 / 月覆盖人次 / 7日搜索人次（仅用于兜底与取 seven）
COLS = {
    "搜索词": (0, 1, 2),
    "相关词": (0, 3, 1),   # 相关词 tab：月覆盖在列3（相关词月覆盖），7日在列1
    "下拉词": (0, 1, 2),
    "电商词": (0, 1, 2),
}

_UNIT = {"亿": 10 ** 8, "万": 10 ** 4, "w": 10 ** 4, "W": 10 ** 4,
         "k": 10 ** 3, "K": 10 ** 3}

JS_ROWS = """() => {
  const t = document.querySelector('table.el-table__body');
  if (!t) return [];
  return Array.from(t.querySelectorAll('tbody tr')).map(tr =>
    Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim())
  );
}"""

JS_CLICK_TAB = """(name) => {
  const cands = Array.from(document.querySelectorAll('span,div,li,a'))
    .filter(e => (e.innerText || '').trim() === name && e.offsetParent !== null);
  if (!cands.length) return 'NOT_FOUND';
  let best = cands[0];
  for (const e of cands) {
    if (e.querySelectorAll('*').length < best.querySelectorAll('*').length) best = e;
  }
  best.click();
  return 'clicked';
}"""


def log(*a):
    print(*a, flush=True)


def write_progress(status, done, total, msg=""):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump({"status": status, "done": done, "total": total, "message": msg},
                      f, ensure_ascii=False)
    except Exception:
        pass


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


def _num(txt):
    """'平均:111' / '1.97w' / '1214.37w' / '29.67亿' -> int；无法解析返回 ''"""
    if txt is None:
        return ""
    s = str(txt).strip().replace("平均:", "").replace("平均：", "").replace(",", "")
    if not s or s in ("-", "--", "—"):
        return ""
    m = re.match(r"^([\d.]+)\s*(亿|万|w|W|k|K)?", s)
    if not m:
        return ""
    try:
        v = float(m.group(1))
    except ValueError:
        return ""
    return int(round(v * _UNIT.get(m.group(2) or "", 1)))


def _to_int(txt):
    """接口里形如 "19654" 的整数字符串 -> int；失败返回 ''"""
    if txt is None or txt == "":
        return ""
    try:
        return int(float(str(txt).strip()))
    except (TypeError, ValueError):
        return ""


def _new_capture():
    """返回 (box, handler)：捕获 4 个取数接口与 user/info 的响应"""
    box = {k: None for k in API_TABS}
    box["user_info"] = None
    box["user_info_code"] = None
    box["find_word"] = None

    def handler(r):
        try:
            u = r.url
            if "aidso" not in u:
                return
            if "user/info" in u:      # 实际路径可能是 /api/user/info 或 /dso/api/user/info
                j = r.json()
                box["user_info"] = j
                box["user_info_code"] = j.get("code")
                return
            if "/library_v2/find_word" in u:
                box["find_word"] = r.json()
                return
            if "/library_v2/" in u:
                tail = u.split("/library_v2/")[-1].split("?")[0]
                if tail in API_TABS:
                    box[tail] = r.json()
        except Exception:
            pass

    return box, handler


def _wait_box(page, box, key, timeout=20):
    """轮询等待某个接口响应到达；返回是否拿到。

    ★ 必须用 page.wait_for_timeout() 而不是 time.sleep()：playwright 同步 API 只在
    它自己的调用期间派发 response 事件，纯 time.sleep() 期间 handler 不会被触发，
    会导致每次都白等满超时（曾因此 4 分钟才跑 1 个关键词）。
    """
    waited = 0
    step = 300
    while waited < timeout * 1000:
        if box.get(key) is not None:
            return True
        page.wait_for_timeout(step)
        waited += step
    return box.get(key) is not None


def _table_rows(page):
    try:
        return page.evaluate(JS_ROWS) or []
    except Exception:
        return []


def _click_tab(page, name):
    try:
        return page.evaluate(JS_CLICK_TAB, name)
    except Exception as e:
        return "ERR:%r" % (e,)


def _collect(page, data, tab, name_field):
    """把某个 tab 的接口数据 + 表格里的 7日搜索人次 合并成 words 列表"""
    name_col, month_col, seven_col = COLS[tab]
    rows = _table_rows(page)

    dom_seven, dom_month = {}, {}
    for r in rows:
        if not r:
            continue
        nm = (r[name_col] if len(r) > name_col else "").strip()
        if not nm:
            continue
        if len(r) > seven_col:
            v = _num(r[seven_col])
            if v != "":
                dom_seven.setdefault(nm, v)
        if len(r) > month_col:
            v = _num(r[month_col])
            if v != "":
                dom_month.setdefault(nm, v)

    res = ((data or {}).get("data") or {}).get("result") or []
    by_name = {}
    for rec in res:
        if not isinstance(rec, dict):
            continue
        nm = (rec.get(name_field) or rec.get("keyword") or "").strip()
        if nm:
            by_name.setdefault(nm, rec)

    names = list(by_name.keys()) or list(dom_month.keys())
    out = []
    for nm in names[:TOP_N[tab]]:
        rec = by_name.get(nm) or {}
        month = _to_int(rec.get("month_cover_count"))
        if month == "":
            month = dom_month.get(nm, "")
        out.append({"type": tab, "name": nm, "month": month,
                    "seven": dom_seven.get(nm, "")})
    return out


def scrape_keyword(page, keyword, box):
    """采集单个关键词，返回 (words, meta)"""
    for k in ["user_info", "user_info_code", "find_word"] + list(API_TABS):
        box[k] = None

    page.goto(SEARCH_URL + quote(keyword), timeout=60000, wait_until="domcontentloaded")
    _wait_box(page, box, "list", 30)          # 首屏「搜索词」tab 的接口
    _wait_box(page, box, "user_info", 6)      # 登录态自检用
    page.wait_for_timeout(1500)

    words = []
    # 1) 首屏即「搜索词」tab，直接读
    words.extend(_collect(page, box.get("list"), "搜索词", "keyword"))
    # 2) 其余 3 个 tab：点击触发接口
    for tab, ep, name_field in TABS[1:]:
        got = False
        for attempt in (1, 2):
            box[ep] = None
            clicked = _click_tab(page, tab)
            got = _wait_box(page, box, ep, 20)
            if got:
                break
            if clicked == "NOT_FOUND":
                log("[爱搜]   %s 未找到「%s」tab（第%d次）" % (keyword, tab, attempt))
            page.wait_for_timeout(1000)
        page.wait_for_timeout(1200)     # 等表格渲染，取 7日搜索人次
        if not got:
            log("[爱搜]   %s 的「%s」接口无响应，该类型可能缺数据" % (keyword, tab))
        words.extend(_collect(page, box.get(ep), tab, name_field))

    meta = {"login_code": box.get("user_info_code")}
    return words, meta


def main():
    keywords = load_keywords()
    if not keywords:
        log("[爱搜] 无关键词，请先写入 _aisou_input.json")
        write_progress("error", 0, 0, "无关键词")
        return
    ls = load_localstorage()
    if not ls:
        log("[爱搜] 未找到登录态，请先跑 tools/aisou_login.py 生成 aisou_localstorage.json。")
        write_progress("error", 0, len(keywords), "缺少登录态")
        return

    write_progress("running", 0, len(keywords), "启动浏览器")
    results = []
    sig_map = {}
    dup = 0
    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR, channel="chrome", headless=True,
                args=["--disable-blink-features=AutomationControlled"],
                user_agent=UA, viewport={"width": 1440, "height": 900}, locale="zh-CN")
        except Exception as e:
            log("[爱搜] 启动 Chrome 失败（%r），回退内置 Chromium" % (e,))
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR, headless=True,
                args=["--disable-blink-features=AutomationControlled"],
                user_agent=UA, viewport={"width": 1440, "height": 900}, locale="zh-CN")

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        box, handler = _new_capture()
        page.on("response", handler)
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        # 先打开站点，注入 localStorage，再访问业务页让登录态生效
        page.goto(HOME_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        page.evaluate("(kv) => { for (const k in kv) localStorage.setItem(k, kv[k]); }", ls)
        page.wait_for_timeout(500)

        for i, kw in enumerate(keywords):
            write_progress("running", i, len(keywords), "采集 " + kw)
            try:
                words, meta = scrape_keyword(page, kw, box)
            except Exception as e:
                log("[爱搜] %s 采集失败: %r" % (kw, e))
                words, meta = [], {}

            # 首个关键词做登录态硬校验：失效就立刻停，不写垃圾数据
            if i == 0:
                code = meta.get("login_code")
                if code is not None and code != 200:
                    log("[爱搜] ❌ 登录态已失效（user/info code=%s）。" % code)
                    log("       请在本机跑 tools/aisou_login.py 重新登录，"
                        "再把 aisou_localstorage.json 传到服务器。")
                    write_progress("error", 0, len(keywords), "登录态失效(code=%s)" % code)
                    ctx.close()
                    return

            sig = json.dumps([(w["type"], w["name"]) for w in words], ensure_ascii=False)
            if words and sig in sig_map:
                dup += 1
                log("[爱搜] ⚠️ %s 抓到的词与「%s」完全一致，疑似示例数据" % (kw, sig_map[sig]))
            else:
                sig_map[sig] = kw

            cnt = {}
            for w in words:
                cnt[w["type"]] = cnt.get(w["type"], 0) + 1
            log("[爱搜] %s 采集到 %d 个词 %s" % (kw, len(words), cnt))
            results.append({"keyword": kw, "words": words})
            page.wait_for_timeout(600)
        ctx.close()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"results": results}, f, ensure_ascii=False, indent=2)

    if dup:
        write_progress("done", len(keywords), len(keywords),
                       "采集完成，但有 %d 个关键词的词重复（疑似登录态问题）" % dup)
        log("[爱搜] ⚠️ 有 %d/%d 个关键词抓到重复词，登录态或账号权限可能有问题。"
            % (dup, len(keywords)))
    else:
        write_progress("done", len(keywords), len(keywords), "采集完成")
    log("[爱搜] 完成 -> %s" % OUTPUT_FILE)


if __name__ == "__main__":
    main()
