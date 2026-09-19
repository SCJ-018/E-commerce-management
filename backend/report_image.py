# -*- coding: utf-8 -*-
"""日报「长图」渲染 —— 把报告 HTML 截成整页 PNG，供钉钉图片消息推送。

【为什么不再发 PDF】
钉钉单聊的「文件消息」对 PDF 只能先下载、再用外部应用打开，手机上基本等于打不开。
换成图片消息（msgKey=sampleImageMsg）后点一下就在钉钉内全屏查看，可双指缩放，
不依赖任何外部程序 —— 这是「能直接在钉钉打开」最稳的格式。

【为什么不沿用 _html_to_pdf 的 --print-to-pdf】
那是 A4 分页版式，图片要的是「一整张长图」，必须按 CSS 像素高度整页截图。
⚠ 实测（Chrome 152）：`chrome --headless=new --screenshot --window-size=1000,600`
   只截 **视口**（输出恰好 1000x600），不是全页；旧 headless 的「全页截图」行为已移除。
   所以主路径用 Playwright 的 full_page 截图 —— 高度精确、2 倍图更清晰。

【降级链】
 ① Playwright（优先用系统 Chrome，其次自带 Chromium）—— 精确高度、2 倍分辨率
 ② 无头 Chrome 大窗口截图 + Pillow 裁掉底部空白 —— 只依赖 Chrome 和 Pillow
两条都失败才抛错，避免浏览器环境一变整条日报推送就瘫掉。

本模块**无副作用**（不在导入时起线程/连库），可被测试脚本单独 import 验证。
"""
import io
import os
import re
import subprocess
import tempfile

# 视口宽 = 报告卡片 980px（见 app.py 的 .da-page{max-width:980px}）+ 左右各 20px 留白
IMAGE_WIDTH = 1020
# 设备像素比：2 倍图在手机放大后依然清晰
IMAGE_SCALE = 2
# 单张位图高度上限（Chromium 纹理上限约 16384，留余量）；超出则降低像素比
MAX_BITMAP_PX = 16000
# 降级方案用的窗口高度：给足即可，多出来的白边由 Pillow 裁掉
CLI_WINDOW_HEIGHT = 20000
# 钉钉图片上传上限 10MB，超过就转 JPEG
MAX_UPLOAD_BYTES = 9 * 1024 * 1024

# 截图版额外样式：灰底 + 留白，白卡片才有边距（A4 纸面版不需要这层）
IMAGE_EXTRA_CSS = """
body{background:#eef2f7;padding:20px}
.da-report{box-shadow:0 6px 24px rgba(15,23,42,.10)}
"""

# 可执行浏览器候选（与 app.py _find_browser 同源，这里独立一份避免循环依赖）
_BROWSER_CANDIDATES = (
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/lib/chromium/chromium',
    '/snap/bin/chromium',
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
)


def build_html(report_date, content, css):
    """把报告正文包成「适合整页截图」的独立 HTML"""
    # Font Awesome 图标在离线 HTML 里加载不出来，先剔掉
    content = re.sub(r'<i class="fa-[^"]*"></i>', '', content or '')
    return ('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            '<title>每日数据分析报告 %s</title>\n<style>%s\n%s</style>\n</head>\n'
            '<body>\n<div class="da-page">\n%s\n</div>\n</body>\n</html>'
            % (report_date, css, IMAGE_EXTRA_CSS, content))


def _find_browser():
    """定位 Chrome / Edge，可用环境变量 CHROME_PATH 覆盖"""
    for path in (os.environ.get('CHROME_PATH', ''),) + _BROWSER_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


# ---------------- 主路径：Playwright 全页截图 ----------------

def _measure_height(browser, html):
    """先用 1 倍像素比量一次页面高度，据此决定最终设备像素比"""
    ctx = browser.new_context(viewport={'width': IMAGE_WIDTH, 'height': 900})
    try:
        page = ctx.new_page()
        page.set_content(html, wait_until='load')
        page.wait_for_timeout(300)
        return int(page.evaluate(
            'Math.max(document.body.scrollHeight, '
            'document.documentElement.scrollHeight)') or 0)
    finally:
        ctx.close()


def _render_playwright(html):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, last_err = None, None
        # channel=chrome 用系统 Chrome；失败再退回 Playwright 自带 Chromium
        for kwargs in ({'channel': 'chrome'}, {}, {'channel': 'chromium'}):
            try:
                browser = p.chromium.launch(**kwargs)
                break
            except Exception as e:      # noqa: BLE001 - 依次试错，最后统一报错
                last_err = e
        if browser is None:
            raise RuntimeError('启动浏览器失败：%s' % last_err)

        try:
            height = _measure_height(browser, html)
            scale = IMAGE_SCALE
            if height and height * scale > MAX_BITMAP_PX:
                scale = max(1, int(MAX_BITMAP_PX / height))
                print('[报告长图] 内容高 %dpx，设备像素比降为 %d' % (height, scale))

            ctx = browser.new_context(viewport={'width': IMAGE_WIDTH, 'height': 900},
                                      device_scale_factor=scale)
            try:
                page = ctx.new_page()
                page.set_content(html, wait_until='load')
                page.wait_for_timeout(400)
                return page.screenshot(full_page=True)
            finally:
                ctx.close()
        finally:
            browser.close()


# ---------------- 降级：无头 Chrome 大窗口 + 裁白边 ----------------

def _render_chrome_cli(html):
    browser = _find_browser()
    if not browser:
        raise RuntimeError('未检测到 Chrome/Edge 浏览器')

    from PIL import Image, ImageChops

    fd_html, html_path = tempfile.mkstemp(suffix='.html', prefix='da_img_')
    fd_png, png_path = tempfile.mkstemp(suffix='.png', prefix='da_img_')
    os.close(fd_html)
    os.close(fd_png)
    try:
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html)

        # ⚠ 关掉滚动条：否则视口宽会被滚动条吃掉 15px，换行位置与量高时不一致
        cmd = [browser, '--headless=new', '--disable-gpu', '--no-sandbox',
               '--hide-scrollbars', '--force-device-scale-factor=1',
               '--window-size=%d,%d' % (IMAGE_WIDTH, CLI_WINDOW_HEIGHT),
               '--screenshot=' + png_path,
               'file:///' + html_path.replace('\\', '/')]
        result = subprocess.run(cmd, capture_output=True, timeout=120)

        if not os.path.isfile(png_path) or os.path.getsize(png_path) == 0:
            raise RuntimeError('截图无输出：%s'
                               % (result.stderr or b'').decode('utf-8', 'ignore')[:300])

        im = Image.open(png_path).convert('RGB')   # convert 会 load，之后即可删文件
        # 窗口给了 20000px 高，底下全是背景色；以右下角像素为背景色求非背景区域
        bg = Image.new('RGB', im.size, im.getpixel((im.width - 2, im.height - 2)))
        bbox = ImageChops.difference(im, bg).getbbox()
        if bbox:
            im = im.crop((0, 0, im.width, min(im.height, bbox[3] + 20)))

        buf = io.BytesIO()
        im.save(buf, 'PNG', optimize=True)
        return buf.getvalue()
    finally:
        for path in (html_path, png_path):
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass


# ---------------- 对外入口 ----------------

def render_png(html):
    """渲染整页长图，返回 PNG 字节；主路径失败自动降级"""
    errors = []
    for name, func in (('Playwright', _render_playwright), ('ChromeCLI', _render_chrome_cli)):
        try:
            data = func(html)
            if data:
                return data
            errors.append('%s：输出为空' % name)
        except Exception as e:      # noqa: BLE001 - 任一方案失败都继续试下一条
            errors.append('%s：%s' % (name, e))
            print('[报告长图] %s 渲染失败：%s' % (name, e))
    raise RuntimeError('报告长图渲染失败（' + '；'.join(errors) + '）')


def _shrink_if_needed(data):
    """超过钉钉 10MB 上传上限就转 JPEG（长报告 PNG 可能十几 MB，JPEG 通常只剩 1/3）"""
    if len(data) <= MAX_UPLOAD_BYTES:
        return data, 'png'
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert('RGB')
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=88, optimize=True)
        out = buf.getvalue()
        print('[报告长图] PNG %.2fMB 超限，转 JPEG %.2fMB'
              % (len(data) / 1048576.0, len(out) / 1048576.0))
        if len(out) < len(data):
            return out, 'jpg'
    except Exception as e:          # noqa: BLE001 - 转码失败不影响出图
        print('[报告长图] 转 JPEG 失败，仍发 PNG：%s' % e)
    return data, 'png'


def render_report_image(report_date, content, css):
    """日报长图唯一入口。

    :param report_date: 报告日期（写进 <title>）
    :param content:     报告正文 HTML 片段（daily_analysis_reports.html_report）
    :param css:         独立报告 CSS（app.py 的 _DA_STANDALONE_CSS）
    :return: (图片字节, 扩展名 'png' | 'jpg')
    """
    data = render_png(build_html(report_date, content, css))
    return _shrink_if_needed(data)
