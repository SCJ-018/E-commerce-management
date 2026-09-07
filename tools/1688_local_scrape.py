# -*- coding: utf-8 -*-
"""
1688 本地抓取 + 回传服务器（解决服务器无头无法过滑块的问题）
用法：python tools/1688_local_scrape.py <关键词>

流程：
  1. 设置 1688_HEADED=1，弹出 Chrome（有头）打开 1688 搜索页
     -> 你手动拖过滑块验证码后，脚本自动抓前 10 页
  2. 抓取成功后，把 tools/_1688_top10.json 上传到服务器 /opt/ecom/tools/_1688_top10.json
     -> 服务器前端的「1688市场」面板与选品智能体即可读到新数据

依赖：本机已装 Chrome + playwright；服务器信息复用 .deploy/ssx.py 里的配置。
"""
import json
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRAPER = os.path.join(BASE_DIR, '1688_scraper.py')
PROGRESS = os.path.join(BASE_DIR, '_1688_progress.json')
OUT = os.path.join(BASE_DIR, '_1688_top10.json')
SSX = os.path.abspath(os.path.join(BASE_DIR, '..', '.deploy', 'ssx.py'))
REMOTE = '/opt/ecom/tools/_1688_top10.json'


def main():
    kw = sys.argv[1].strip() if len(sys.argv) > 1 else ''
    if not kw:
        kw = input('输入要抓取的商品关键词: ').strip()
    if not kw:
        print('未输入关键词，退出')
        return

    # 1) 本地有头抓取（手动过滑块）
    os.environ['1688_HEADED'] = '1'
    print('→ 开始本地抓取「%s」，请在弹出的 Chrome 里手动滑过滑块验证码…' % kw)
    subprocess.call([sys.executable, SCRAPER, kw])

    # 2) 校验结果，成功才上传
    status = ''
    if os.path.exists(PROGRESS):
        try:
            with open(PROGRESS, 'r', encoding='utf-8') as f:
                status = json.load(f).get('status', '')
        except Exception:
            pass
    if status != 'done':
        print('→ 抓取未成功（状态=%s），未上传。请重试并确保已手动通过滑块验证码。' % (status or '未知'))
        return

    print('→ 抓取成功，上传结果到服务器…')
    subprocess.call([sys.executable, SSX, 'up', OUT, REMOTE])
    print('→ 完成：服务器「1688市场」已可读到最新数据')


if __name__ == '__main__':
    main()
