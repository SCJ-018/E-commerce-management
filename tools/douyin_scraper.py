# -*- coding: utf-8 -*-
"""
抖音「抖音市场」商城销量前50抓取
用法：python tools/douyin_scraper.py <关键词>
- 关键词从命令行参数读取，缺省时读 DY_KEYWORD 环境变量，再缺省用默认值
- 数据源默认 mock（本地仿真数据，直接可跑通）；设 DY_DATA_SOURCE=onebound 走 OneBound 真实接口
- 抓取后按「销量」降序取前50，导出 标题/封面图/价格/销量/店铺名/链接
  -> tools/_douyin_top50.json（哪个字段抓不到就置 null，不落库）
- 全程写 tools/_douyin_progress.json 供前端进度条轮询
"""
import json
import os
import re
import sys
import time
from urllib.parse import quote

# 让 Windows 控制台和日志保持中文输出
sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_KEYWORD = "汽车脚垫"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(BASE_DIR, "_douyin_top50.json")
PROGRESS_FILE = os.path.join(BASE_DIR, "_douyin_progress.json")

TOP_N = 50
DATA_SOURCE = os.environ.get("DY_DATA_SOURCE", "mock").strip().lower()


def resolve_keyword():
    """抓取关键词：优先命令行参数，其次 DY_KEYWORD 环境变量，最后默认值"""
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    return (os.environ.get("DY_KEYWORD") or DEFAULT_KEYWORD).strip()


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


def sales_num(s):
    """把销量字符串（如 '已售 3.5万'、'1.2w'、'5000+'）解析为数值，仅用于排序"""
    m = re.search(r'([\d.]+)\s*([万wW亿]?)', str(s or ""))
    if not m:
        return 0.0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0.0
    unit = m.group(2)
    if unit == '亿':
        return n * 1e8
    if unit in ('万', 'w', 'W'):
        return n * 1e4
    return n


# ======================== 数据源 ========================

class MockSource:
    """本地仿真数据源：生成与关键词相关的、销量降序的抖音商品，便于直接跑通全流程"""
    max_pages = 3

    _ADJ = ["升级款", "旗舰款", "Pro增强版", "家用款", "便携款", "新款", "热销款", "多功能款"]
    _SHOP = ["优品甄选旗舰店", "好物严选专卖店", "潮品馆官方旗舰店", "严选好物店",
             "品质生活馆", "超值购旗舰店", "臻选百货店", "明星同款优选店"]

    def fetch(self, keyword, page):
        items = []
        base = page * 20
        for i in range(20):
            idx = base + i
            sold = max(800, (60 - idx) * 1870)  # 销量整体降序
            items.append({
                "id": str(3600000000 + idx),
                "title": f"{self._ADJ[idx % len(self._ADJ)]} {keyword} 抖音同款 高性价比 多规格可选",
                "price": f"¥{(idx % 28) * 9.9 + 9.9:.1f}",
                "sales": ("已售 %.1f万" % (sold / 10000)) if sold >= 10000 else ("已售 %d" % sold),
                "image": "https://picsum.photos/seed/douyin%d/200/200" % idx,
                "shop": self._SHOP[idx % len(self._SHOP)],
                "link": "https://haohuo.jinritemai.com/views/product/item2?id=%d" % (3600000000 + idx),
            })
        return items


class OneBoundSource:
    """OneBound 抖音商品搜索数据源（按环境变量配置）。
    ponytail: OneBound 真实请求/返回结构未在本仓库验证，字段映射为最佳猜测；
    若与原 datasource.py 不一致，替换此类的 fetch/_normalize 即可。"""
    def __init__(self):
        self.api_key = os.environ.get("DY_ONEBOUN_KEY", "")
        self.api_secret = os.environ.get("DY_ONEBOUN_SECRET", "")
        self.base_url = os.environ.get("DY_ONEBOUN_BASE", "https://api-gw.onebound.cn/douyin")
        self.page_size = int(os.environ.get("DY_ONEBOUN_PAGE_SIZE", "100"))
        self.max_pages = int(os.environ.get("DY_ONEBOUN_MAX_PAGES", "5"))

    def fetch(self, keyword, page):
        if not self.api_key or not self.api_secret:
            raise RuntimeError("已选择 onebound 数据源，但未配置 DY_ONEBOUN_KEY/DY_ONEBOUN_SECRET")
        import requests
        resp = requests.get(
            self.base_url,
            params={"key": self.api_key, "secret": self.api_secret, "q": keyword,
                    "page": page, "page_size": self.page_size, "sort": "sales"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items") or data.get("data") or data.get("result") or {}
        if isinstance(items, dict):
            items = items.get("item") or items.get("list") or items.get("result") or []
        return [self._normalize(it) for it in items if isinstance(it, dict)]

    @staticmethod
    def _normalize(it):
        return {
            "id": str(it.get("num_iid") or it.get("item_id") or it.get("id") or ""),
            "title": it.get("title") or it.get("name") or None,
            "price": it.get("price") or it.get("promotion_price") or None,
            "sales": it.get("sold") or it.get("sales") or it.get("sale_count") or None,
            "image": it.get("pic_url") or it.get("image") or it.get("img") or None,
            "shop": it.get("shop_title") or it.get("shop_name") or it.get("nick") or None,
            "link": it.get("detail_url") or it.get("item_url") or it.get("link") or None,
        }


def build_source():
    if DATA_SOURCE == "onebound":
        return OneBoundSource()
    return MockSource()


# ======================== 抓取主流程 ========================

def main():
    keyword = resolve_keyword()
    source = build_source()
    log(f"[抓取] 关键词 = {keyword}，数据源 = {source.__class__.__name__}，目标销量前 {TOP_N}")

    write_progress("running", 0, TOP_N, keyword)

    products = []
    seen = set()
    page = 0
    try:
        while len(products) < TOP_N and page < source.max_pages:
            batch = source.fetch(keyword, page)
            added = 0
            for p in batch:
                pid = p.get("id")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                products.append(p)
                added += 1
                if len(products) >= TOP_N:
                    break
            write_progress("running", len(products), TOP_N, keyword)
            log(f"[分页] page={page} 本页新增 {added}，累计 {len(products)}")
            if added == 0:
                break
            page += 1
            time.sleep(0.4)
    except Exception as e:
        log("[抓取] 异常:", repr(e))
        write_progress("error", len(products), TOP_N, keyword)
        raise

    products = sorted(products, key=lambda p: sales_num(p.get("sales")), reverse=True)[:TOP_N]
    log(f"[结果] 抓取到 {len(products)} 个商品")

    result = {
        "keyword": keyword,
        "url": "https://www.douyin.com/search/" + quote(keyword),
        "count": len(products),
        "products": [dict(rank=i + 1, **p) for i, p in enumerate(products)],
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    write_progress("done", len(products), TOP_N, keyword)
    log("[保存]", OUT_JSON)


if __name__ == "__main__":
    main()
