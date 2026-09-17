# -*- coding: utf-8 -*-
"""
小红书账号作品数据爬虫（基于 Spider_XHS 开源项目封装）

用法:
    python xhs_crawler.py --red-id 18930360363                  # 按小红书号爬取（自动解析 user_id）
    python xhs_crawler.py --user-id 5ff0e6410000000001005f1a    # 按 24 位 user_id 爬取
    python xhs_crawler.py --home https://www.xiaohongshu.com/user/profile/xxx?xsec_token=...  # 按主页链接
    python xhs_crawler.py --red-id 18930360363 --fast           # 只拿列表(标题/封面/链接)，不逐条拉详情，速度快
    python xhs_crawler.py --red-id 18930360363 --limit 50       # 最多爬 50 条
    python xhs_crawler.py --red-id 18930360363 --cookie "a1=...;web_session=..."  # 指定 Cookie
    python xhs_crawler.py --red-id 18930360363 --qrcode         # 扫码登录（二维码存为 qrcode.png）

登录优先级: --cookie > cookie.txt(上次保存) > --qrcode
输出: ./output/{账号名}_{user_id前8位}_notes.xlsx 和同名 .json
"""
import argparse
import json
import os
import sys
import time
import random

# 项目根目录 = Spider_XHS 仓库目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPIDER_DIR = os.path.join(BASE_DIR, 'Spider_XHS')
sys.path.insert(0, SPIDER_DIR)
os.chdir(SPIDER_DIR)  # Node 签名脚本依赖 cwd

from loguru import logger
from xhs_utils.xhs_pc import XHSPcAuth

COOKIE_FILE = os.path.join(BASE_DIR, 'cookie.txt')
QR_FILE = os.path.join(BASE_DIR, 'qrcode.png')
OUTPUT_DIR = os.path.join(BASE_DIR, 'output')


def build_auth(cookie_str: str = None, use_qrcode: bool = False):
    from xhs_utils.xhs_pc import XHSPcAuth
    if cookie_str:
        logger.info('使用指定 Cookie 登录')
        return XHSPcAuth.from_cookie(cookie_str)
    if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 10:
        with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
            saved = f.read().strip()
        if saved:
            logger.info(f'复用已保存 Cookie: {COOKIE_FILE}')
            return XHSPcAuth.from_cookie(saved)
    if not use_qrcode:
        raise SystemExit(
            '\n[缺少登录凭证] 三种方式任选:\n'
            '  1) --cookie "完整Cookie串"  (浏览器F12 -> 网络 -> 任意请求 -> 复制Cookie)\n'
            '  2) --qrcode  生成二维码, 用小红书APP扫码后自动继续\n'
            '  3) 把 Cookie 写入 cookie.txt 后重跑\n'
        )
    # 扫码登录：把二维码存成 png 供扫码，而不是打印在终端
    logger.info('扫码登录：二维码将保存为 qrcode.png，请用小红书 APP 扫描')
    return _qrcode_login_save_png()


def _qrcode_login_save_png():
    """改造版扫码登录：二维码保存为 PNG 文件而非终端打印。"""
    import qrcode
    from apis.xhs_pc_login_apis import XHSLoginApi

    login_api = XHSLoginApi()
    cookies = login_api.generate_init_cookies()
    success, msg, qr_data = login_api.generate_qrcode(cookies)
    if not success:
        raise SystemExit(f'获取二维码失败: {msg}')
    cookies = qr_data['cookies']
    # 预检 + webprofile（与官方流程一致）
    success, msg, cookies = login_api.check_qrcode_status(qr_data['qr_id'], qr_data['code'], cookies)
    if msg != '请扫描二维码':
        raise SystemExit(f'二维码预检查状态异常: {msg}')
    login_api.ensure_webprofile(cookies)

    qr_url = qr_data['qr_url']
    img = qrcode.QRCode(box_size=10, border=4)
    img.add_data(qr_url)
    img.make(fit=True)
    img.make_image(fill_color='black', back_color='white').save(QR_FILE)
    logger.info(f'二维码已保存: {QR_FILE}  （180 秒内有效，请用小红书APP扫描并确认）')

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        success, msg, cookies = login_api.check_qrcode_status(qr_data['qr_id'], qr_data['code'], cookies)
        if success:
            break
        if msg == '二维码已过期':
            raise SystemExit('二维码已过期，请重新运行')
        time.sleep(2)
    else:
        raise SystemExit('等待扫码超时，请重新运行')

    success, user_info, cookies = login_api.get_user_info(cookies)
    if not success or user_info.get('guest') is not False:
        raise SystemExit('登录会话验证失败: success=%s guest=%s info=%s'
                         % (success, (user_info or {}).get('guest'), str(user_info)[:220]))
    cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())
    with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
        f.write(cookie_str)
    logger.info(f'扫码登录成功: {user_info.get("nickname")} (RedID: {user_info.get("red_id")})，Cookie 已存入 cookie.txt')
    return XHSPcAuth.from_cookie(cookie_str)


def resolve_user(api, red_id: str):
    """小红书号 -> user_id（通过用户搜索接口）。返回字段为扁平结构。"""
    logger.info(f'搜索小红书号: {red_id}')
    success, msg, res = api.search_user(red_id)
    if not success:
        raise SystemExit(f'搜索用户失败: {msg}')
    users = (res.get('data') or {}).get('users') or []
    target = None
    for u in users:
        if str(u.get('red_id', '')) == str(red_id):
            target = u
            break
    if target is None and users:
        target = users[0]  # 兜底取第一个
        logger.warning(f'未精确匹配到小红书号 {red_id}，取第一个结果: {target.get("name")} (red_id={target.get("red_id")})')
    if target is None:
        raise SystemExit(f'搜索不到用户: {red_id}（注意：该账号可能未公开或小红书号有误）')
    uid = target.get('id')
    return {
        'user_id': uid,
        'nickname': target.get('name'),
        'red_id': target.get('red_id'),
        'xsec_token': target.get('xsec_token', ''),
        'home_url': f'https://www.xiaohongshu.com/user/profile/{uid}',
    }


def crawl_user_notes(api, user_id: str, xsec_token: str, limit: int = 0, fast: bool = False):
    """拉取用户全部作品。fast=True 只拿列表字段；否则逐条拉详情（含点赞/收藏/评论数）。"""
    notes = []
    cursor = ''
    while True:
        success, msg, res = api.get_user_note_info(user_id, cursor, xsec_token, 'pc_search')
        if not success:
            raise SystemExit(f'获取作品列表失败: {msg}')
        data = res.get('data') or {}
        batch = data.get('notes') or []
        notes.extend(batch)
        logger.info(f'已获取 {len(notes)} 条作品...')
        if not data.get('has_more') or not batch:
            break
        cursor = str(data.get('cursor', ''))
        time.sleep(random.uniform(1.0, 2.0))
    if limit > 0:
        notes = notes[:limit]

    results = []
    total = len(notes)
    if fast:
        for i, n in enumerate(notes, 1):
            results.append({
                'note_id': n.get('note_id'),
                '标题': n.get('display_title', ''),
                '类型': '视频' if n.get('type') == 'video' else '图集',
                '置顶': '是' if n.get('is_top') else '否',
                '封面': (n.get('cover') or {}).get('url', ''),
                '链接': f"https://www.xiaohongshu.com/explore/{n.get('note_id')}?xsec_token={n.get('xsec_token')}",
                'xsec_token': n.get('xsec_token'),
            })
    else:
        for i, n in enumerate(notes, 1):
            note_id = n.get('note_id')
            token = n.get('xsec_token', '')
            url = f'https://www.xiaohongshu.com/explore/{note_id}?xsec_token={token}&xsec_source=pc_user'
            try:
                success, msg, res = api.get_note_info(url)
                if success:
                    item = (res.get('data') or {}).get('items') or [{}]
                    note_card = (item[0].get('note_card') or {}) if item else {}
                    interact = note_card.get('interact_info') or {}
                    results.append({
                        'note_id': note_id,
                        '标题': note_card.get('display_title') or note_card.get('title') or '',
                        '类型': '视频' if note_card.get('type') == 'video' else '图集',
                        '发布时间': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(note_card.get('time', 0) / 1000)) if note_card.get('time') else '',
                        '点赞数': interact.get('liked_count', ''),
                        '收藏数': interact.get('collected_count', ''),
                        '评论数': interact.get('comment_count', ''),
                        '分享数': interact.get('share_count', ''),
                        '正文': (note_card.get('desc') or '')[:500],
                        '话题标签': ','.join(t.get('name', '') for t in (note_card.get('tag_list') or [])),
                        'IP属地': note_card.get('ip_location', ''),
                        '链接': f'https://www.xiaohongshu.com/explore/{note_id}?xsec_token={token}',
                    })
                else:
                    logger.warning(f'[{i}/{total}] 笔记 {note_id} 详情失败: {msg}')
            except Exception as e:
                logger.warning(f'[{i}/{total}] 笔记 {note_id} 异常: {e}')
            if i % 5 == 0:
                logger.info(f'详情进度: {i}/{total}')
            time.sleep(random.uniform(1.5, 3.0))
    return results


def save_outputs(rows, user_info):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    name = (user_info.get('nickname') or 'user').replace('\\/:*?"<>|', '_')
    base = os.path.join(OUTPUT_DIR, f"{name}_{str(user_info.get('user_id'))[:8]}_notes")
    # JSON
    with open(base + '.json', 'w', encoding='utf-8') as f:
        json.dump({'user': user_info, 'notes': rows}, f, ensure_ascii=False, indent=2)
    # Excel
    if rows:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = '作品数据'
        cols = list(rows[0].keys())
        ws.append(cols)
        for r in rows:
            ws.append([str(r.get(c, '')) for c in cols])
        wb.save(base + '.xlsx')
    logger.info(f'已保存: {base}.xlsx / {base}.json')
    return base


def get_user_profile(api, user_id):
    success, msg, res = api.get_user_info(user_id)
    if success:
        d = (res.get('data') or {})
        basic = d.get('basic_info') or {}
        inter = d.get('interactions') or []
        def cnt(i):
            try:
                return inter[i]['count']
            except Exception:
                return ''
        return {
            'user_id': user_id,
            'nickname': basic.get('nickname', ''),
            'red_id': basic.get('red_id', ''),
            '粉丝数': cnt(1),
            '关注数': cnt(0),
            '获赞收藏': cnt(2),
            'IP属地': basic.get('ip_location', ''),
            '简介': basic.get('desc', ''),
        }
    return {'user_id': user_id, 'nickname': '', 'red_id': ''}


def main():
    p = argparse.ArgumentParser(description='小红书账号作品爬虫')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--red-id', help='小红书号（如 18930360363），自动搜索解析 user_id')
    g.add_argument('--user-id', help='24位十六进制 user_id')
    g.add_argument('--home', help='用户主页完整链接（含 xsec_token 更佳）')
    p.add_argument('--limit', type=int, default=0, help='最多爬取条数，0=全部')
    p.add_argument('--fast', action='store_true', help='只拿列表数据，不逐条拉详情')
    p.add_argument('--cookie', help='完整 Cookie 串')
    p.add_argument('--qrcode', action='store_true', help='扫码登录')
    args = p.parse_args()

    from apis.xhs_pc_apis import XHS_Apis
    auth = build_auth(args.cookie, args.qrcode)
    api = XHS_Apis(auth)
    api.bootstrap()

    # 目标账号解析
    if args.red_id:
        info = resolve_user(api, args.red_id)
        user_id, xsec_token = info['user_id'], info['xsec_token']
    elif args.user_id:
        user_id, xsec_token = args.user_id, ''
    else:
        import urllib.parse as up
        q = up.parse_qs(up.urlparse(args.home).query)
        user_id = up.urlparse(args.home).path.rstrip('/').split('/')[-1]
        xsec_token = (q.get('xsec_token') or [''])[0]

    profile = get_user_profile(api, user_id)
    logger.info(f'目标账号: {profile.get("nickname")} user_id={user_id} 粉丝={profile.get("粉丝数")}')

    rows = crawl_user_notes(api, user_id, xsec_token, args.limit, args.fast)
    profile['作品总数'] = len(rows)
    base = save_outputs(rows, profile)
    print(f'\n===== 完成 =====\n账号: {profile.get("nickname")} (小红书号 {profile.get("red_id")})\n作品数: {len(rows)}\n输出: {base}.xlsx')


if __name__ == '__main__':
    main()
