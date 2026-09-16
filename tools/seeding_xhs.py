# -*- coding: utf-8 -*-
"""
小红书账号批量抓取（供「种草监测中台」按钮触发）

- 读取 tools/seeding_accounts.json 里 platform=xhs 的账号
- 逐个调用 xhs_crawler.py 的抓取函数
- 汇总为统一格式写入 tools/_xhs_works.json（名称/账号/标题/点赞/评论/收藏/分享/发布时间）

登录凭证优先级：tools/xhs_cookie.txt > tools/cookie.txt（xhs_crawler 默认）
"""
import json
import os
import sys
import time
import random

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPIDER_DIR = os.path.join(BASE_DIR, 'Spider_XHS')
sys.path.insert(0, BASE_DIR)

ACCOUNTS_FILE = os.path.join(BASE_DIR, 'seeding_accounts.json')
XHS_COOKIE_FILE = os.path.join(BASE_DIR, 'xhs_cookie.txt')
OUTPUT_FILE = os.path.join(BASE_DIR, '_xhs_works.json')
PROGRESS_FILE = os.path.join(BASE_DIR, '_seeding_progress_xhs.json')


def write_progress(status, done, total, msg=''):
    try:
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'status': status, 'done': done, 'total': total, 'ts': time.time(), 'msg': msg}, f, ensure_ascii=False)
    except Exception:
        pass


def load_xhs_accounts():
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
    except Exception:
        return []
    return [a for a in data if (a.get('platform') or 'douyin') == 'xhs' and str(a.get('redId') or '').strip()]


def _cookie_str():
    for f in (XHS_COOKIE_FILE, os.path.join(BASE_DIR, 'cookie.txt')):
        if os.path.exists(f) and os.path.getsize(f) > 10:
            with open(f, 'r', encoding='utf-8') as fp:
                s = fp.read().strip()
            if s:
                return s
    return None


def _to_int(v):
    try:
        return int(v or 0)
    except Exception:
        return 0


def main():
    if not os.path.isdir(SPIDER_DIR):
        print('[xhs_batch] 缺少依赖：未找到 ' + SPIDER_DIR)
        print('[xhs_batch] 请将 Spider_XHS 开源项目完整放到 tools/Spider_XHS/ 目录（含 xhs_utils、apis 等子目录）')
        print('[xhs_batch] 并安装依赖：pip install loguru qrcode openpyxl requests')
        write_progress('error', 0, 0, '缺少小红书抓取依赖 Spider_XHS')
        sys.exit(1)

    try:
        import xhs_crawler as crawler  # 内部会 os.chdir 到 Spider_XHS
    except Exception as e:
        print('[xhs_batch] 导入 xhs_crawler 失败: ' + str(e))
        write_progress('error', 0, 0, '导入 xhs_crawler 失败')
        sys.exit(1)

    accounts = load_xhs_accounts()
    if not accounts:
        print('[xhs_batch] 未找到小红书账号（需要 platform=xhs 且填写 redId）')
        write_progress('error', 0, 0, '未找到小红书账号')
        return

    cookie = _cookie_str()
    write_progress('running', 0, len(accounts))
    try:
        auth = crawler.build_auth(cookie, use_qrcode=False)
        from apis.xhs_pc_apis import XHS_Apis  # noqa: E402
        api = XHS_Apis(auth)
        api.bootstrap()

        all_rows = []
        for i, acc in enumerate(accounts):
            red_id = str(acc.get('redId') or '').strip()
            name = (acc.get('name') or '').strip() or red_id
            try:
                info = crawler.resolve_user(api, red_id)
                notes = crawler.crawl_user_notes(api, info['user_id'], info.get('xsec_token', ''), limit=0, fast=False)
            except Exception as e:
                print(f'[xhs_batch] 账号 {name}({red_id}) 抓取失败: {e}')
                write_progress('running', i + 1, len(accounts))
                continue
            for n in notes:
                all_rows.append({
                    'id': len(all_rows) + 1,
                    'name': name,
                    'account': red_id,
                    'title': n.get('标题', ''),
                    'link': n.get('链接', ''),
                    'likes': _to_int(n.get('点赞数')),
                    'comments': _to_int(n.get('评论数')),
                    'collects': _to_int(n.get('收藏数')),
                    'shares': _to_int(n.get('分享数')),
                    'publishTime': n.get('发布时间', ''),
                })
            write_progress('running', i + 1, len(accounts))
            time.sleep(random.uniform(0.5, 1.0))

        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_rows, f, ensure_ascii=False, indent=2)
        msg = '共采集 %d 条作品' % len(all_rows)
        write_progress('done', len(accounts), len(accounts), msg)
        print(f'[xhs_batch] {msg} -> {OUTPUT_FILE}')
    except Exception as e:
        msg = '小红书抓取失败: %s' % e
        print('[xhs_batch] ' + msg)
        write_progress('error', 0, len(accounts), msg)


if __name__ == '__main__':
    main()
