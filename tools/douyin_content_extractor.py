#!/usr/bin/env python3
"""Render one public Douyin share URL and emit a small, JSON-safe content record.

This deliberately uses a real browser instead of undocumented HTTP endpoints. It
extracts only page-visible text and media hints; audio transcription is a separate
step because it requires ffmpeg/ASR and must not be confused with page metadata.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import base64
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from playwright.async_api import async_playwright


ALLOWED_HOSTS = {"douyin.com", "iesdouyin.com"}


def allowed(url):
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        return p.scheme == "https" and any(host == x or host.endswith("." + x) for x in ALLOWED_HOSTS)
    except ValueError:
        return False


def clean_text(value, limit=12000):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:limit]


def transcribe_bytes(data):
    # Tencent Cloud egress cannot reach hf.co directly; use the reachable mirror.
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from faster_whisper import WhisperModel
    with tempfile.TemporaryDirectory(prefix="douyin_asr_") as work:
        source = os.path.join(work, "source.mp4")
        audio = os.path.join(work, "audio.wav")
        if len(data) > 80 * 1024 * 1024:
            raise ValueError("video too large")
        with open(source, "wb") as out:
            out.write(data)
        ff = subprocess.run(["ffmpeg", "-y", "-i", source, "-vn", "-ac", "1", "-ar", "16000", audio],
                            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            timeout=180)
        if ff.returncode:
            detail = (ff.stderr or b"").decode("utf-8", "replace")[-500:]
            raise RuntimeError("ffmpeg failed: " + detail)
        model = WhisperModel(os.environ.get("DOUYIN_WHISPER_MODEL", "base"), device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio, language="zh", vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments if seg.text.strip())[:12000]


async def extract(url, do_transcribe=False):
    if not allowed(url):
        raise ValueError("unsupported host")
    async with async_playwright() as p:
        chrome = os.environ.get("DOUYIN_CHROME_PATH") or "/usr/bin/google-chrome"
        browser = await p.chromium.launch(headless=True,
                                          executable_path=chrome if os.path.isfile(chrome) else None,
                                          args=["--no-sandbox"])
        try:
            cookie_file = os.environ.get("DOUYIN_COOKIE_FILE", "/opt/ecom/tools/douyin_cookie.txt")
            extra_headers = {}
            if os.path.isfile(cookie_file):
                # Cookie is supplied as a browser Cookie header. Never include it
                # in the JSON result or logs.
                with open(cookie_file, "r", encoding="utf-8", errors="ignore") as fh:
                    cookie = fh.read().replace("\r", " ").replace("\n", " ").strip()
                if cookie:
                    extra_headers["Cookie"] = cookie
            page = await browser.new_page(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                extra_http_headers=extra_headers,
            )
            media_responses = []
            async def remember_media(response):
                ctype = (response.headers.get("content-type") or "").lower()
                url_text = response.url.lower()
                if "video/" in ctype or "audio/" in ctype or ".mp4" in url_text or "mime_type=video" in url_text:
                    media_responses.append((response, ctype))
            page.on("response", lambda response: asyncio.create_task(remember_media(response)))
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
            body = await page.locator("body").inner_text()
            title = await page.title()
            final_url = page.url
            title_text = re.sub(r"\s*-\s*抖音\s*$", "", title).strip()
            # The page contains a long navigation/footer plus recommendations.
            # For a note, retain the author/content area only.
            main = body.split("打开声音", 1)[-1] if "打开声音" in body else body
            main = main.split("相关推荐", 1)[0]
            text = clean_text(main)
            if len(text) < 35:
                text = ""
            image_records = await page.locator("img").evaluate_all(
                "els => els.map(e => { const r=e.getBoundingClientRect(); const p=e.closest('[class],[id]'); return {src:e.currentSrc || e.src || e.getAttribute('data-src') || '', width:e.naturalWidth || e.width || 0, height:e.naturalHeight || e.height || 0, displayWidth:r.width || 0, displayHeight:r.height || 0, context:p ? ((p.className || '') + ' ' + (p.id || '')).slice(0,240) : '', alt:e.alt || ''}; })"
            )
            image_urls = []
            seen_images = set()
            for item in image_records:
                src = str(item.get("src") or "").strip()
                if not src.startswith(("https://", "http://")):
                    continue
                # 抖音作品页会把评论区图片也渲染成 img；这些 URL 明确带有
                # aweme_comment/sc=thumb 标记，不能作为母本作品图。
                src_lower = src.lower()
                if "biz_tag=aweme_comment" in src_lower or "aweme_comment" in src_lower or "sc=thumb" in src_lower:
                    continue
                width = int(item.get("width") or 0)
                height = int(item.get("height") or 0)
                display_width = float(item.get("displayWidth") or 0)
                display_height = float(item.get("displayHeight") or 0)
                context = str(item.get("context") or "")
                # 头像、图标和推荐区缩略图通常很小；保留作品页中的大图并按页面顺序去重。
                if width > 0 and height > 0 and (max(width, height) < 240 or min(width, height) < 160):
                    continue
                if display_width > 0 and display_height > 0 and (display_width < 320 or display_height < 220):
                    continue
                if re.search(r"recommend|related|相关推荐|评论|comment|avatar|author|header", context, re.I):
                    continue
                if src in seen_images:
                    continue
                seen_images.add(src)
                image_urls.append(src)
                if len(image_urls) >= 6:
                    break
            images = len(image_urls)
            videos = await page.locator("video").count()
            video_urls = await page.locator("video").evaluate_all(
                "els => els.map(e => e.currentSrc || e.src).filter(Boolean)"
            )
            content_type = "image_text" if "/note/" in final_url or (images > 1 and videos == 0) else "video"
            page_text_lower = (body or "").lower()
            access_blocked = any(token in page_text_lower for token in ("安全验证", "人机验证", "请完成验证", "访问频繁", "验证后继续", "captcha"))
            page_screenshot = ""
            # 图片 URL 被 canvas/懒加载隐藏，或页面出现验证提示时，保留当前可见页面给视觉模型诊断。
            if not image_urls or access_blocked:
                try:
                    shot = await page.screenshot(type="jpeg", quality=70, full_page=False)
                    page_screenshot = "data:image/jpeg;base64," + base64.b64encode(shot).decode("ascii")
                except Exception:
                    page_screenshot = ""
            # Keep only aggregate signals which are useful to the analyst.
            stats = {}
            lines = [x.strip() for x in main.splitlines() if x.strip()]
            leading_numbers = [x for x in lines[:10] if re.fullmatch(r"[0-9.万亿]+", x)]
            if len(leading_numbers) >= 3:
                stats = dict(zip(("likes", "comments", "favorites", "shares"), leading_numbers[:4]))
            result = {
                "sourceUrl": final_url,
                "contentType": content_type,
                "title": clean_text(title_text, 300),
                "text": text,
                "imageCount": images,
                "imageUrls": image_urls,
                "pageScreenshot": page_screenshot,
                "imageAccessBlocked": access_blocked,
                "videoCount": videos,
                "videoUrl": video_urls[0] if video_urls else "",
                "stats": stats,
            }
            if do_transcribe and content_type == "video":
                try:
                    if result["videoUrl"].startswith("blob:"):
                        captured = None
                        ordered = sorted(media_responses,
                                         key=lambda item: 0 if "audio/" in item[1] else 1)
                        for response, _ctype in ordered:
                            try:
                                candidate = await response.body()
                                if candidate and len(candidate) > 4096:
                                    captured = candidate
                                    break
                            except Exception:
                                continue
                        if captured is None:
                            raise ValueError("browser media response unavailable")
                        result["transcript"] = transcribe_bytes(captured)
                    else:
                        result["transcript"] = transcribe(result["videoUrl"])
                except Exception as exc:
                    result["transcriptError"] = str(exc)[:300]
            return result
        finally:
            await browser.close()


def transcribe(video_url):
    """Download one browser-exposed stream and transcribe it locally."""
    if not video_url:
        return ""
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from faster_whisper import WhisperModel
    with tempfile.TemporaryDirectory(prefix="douyin_asr_") as work:
        source = os.path.join(work, "source.mp4")
        audio = os.path.join(work, "audio.wav")
        req = Request(video_url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"})
        with urlopen(req, timeout=90) as response, open(source, "wb") as out:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 80 * 1024 * 1024:
                    raise ValueError("video too large")
                out.write(chunk)
        subprocess.run(["ffmpeg", "-y", "-i", source, "-vn", "-ac", "1", "-ar", "16000", audio],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        model = WhisperModel(os.environ.get("DOUYIN_WHISPER_MODEL", "base"), device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio, language="zh", vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments if seg.text.strip())[:12000]


async def main():
    result = await extract(sys.argv[1], do_transcribe="--transcribe" in sys.argv[2:])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
