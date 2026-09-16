# -*- coding: utf-8 -*-
"""把浏览器扩展导出的 cookies JSON 转成 Playwright storage_state 并存入 _states/。

支持两种输入格式：
1. EditThisCookie / Cookie-Editor 扩展导出的格式（数组，含 domain/expirationDate/httpOnly/secure/sameSite 等）
2. 已符合 Playwright storage_state 的格式（含 cookies 数组）

用法：
  python import_cookies.py <账号> <cookies.json路径>
  # 转换后存到 _states/<账号>.json，接着跑 upload_state.py 写库
"""
import sys
import os
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, '_states')

SAMESITE_MAP = {
    'no_restriction': 'None',
    'unspecified': 'Lax',
    'lax': 'Lax',
    'strict': 'Strict',
    'Lax': 'Lax',
    'Strict': 'Strict',
    'None': 'None',
}


def _to_pw_cookie(c):
    """EditThisCookie 单条 cookie → Playwright storage_state cookie。"""
    expires = -1
    if c.get('expirationDate'):
        try:
            expires = int(float(c['expirationDate']))
        except Exception:
            expires = -1
    if c.get('session'):
        expires = -1
    ss = c.get('sameSite') or 'unspecified'
    return {
        'name': c.get('name', ''),
        'value': c.get('value', ''),
        'domain': c.get('domain', ''),
        'path': c.get('path', '/'),
        'expires': expires,
        'httpOnly': bool(c.get('httpOnly')),
        'secure': bool(c.get('secure')),
        'sameSite': SAMESITE_MAP.get(ss, 'Lax'),
    }


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    account = sys.argv[1]
    cookie_path = sys.argv[2]

    with open(cookie_path, encoding='utf-8') as f:
        raw = json.load(f)

    # 判断输入格式
    if isinstance(raw, dict) and 'cookies' in raw:
        state = raw
    elif isinstance(raw, list):
        state = {
            'cookies': [_to_pw_cookie(c) for c in raw],
            'origins': [],
        }
    else:
        print('[FAIL] 无法识别的 JSON 格式')
        return

    n = len(state.get('cookies', []))
    print('转换 cookies 条数:', n)

    os.makedirs(STATE_DIR, exist_ok=True)
    out = os.path.join(STATE_DIR, account.replace(':', '_').replace('/', '_') + '.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False)
    print('[OK] storage_state 已存:', out)
    print('接下来跑：python upload_state.py "%s"' % account)


if __name__ == '__main__':
    main()
