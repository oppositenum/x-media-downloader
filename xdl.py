#!/usr/bin/env python3
"""Download videos from X / Twitter, including sensitive posts.

yt-dlp's guest GraphQL often returns TweetTombstone for NSFW tweets.
This tool resolves media via the FxTwitter / VxTwitter embed APIs, then
downloads the chosen MP4 with yt-dlp (or urllib if yt-dlp is missing).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TWEET_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|mobile\.)?"
    r"(?:twitter\.com|x\.com|vxtwitter\.com|fxtwitter\.com|fixupx\.com)/"
    r"(?:i/(?:web/)?status|(?:[A-Za-z0-9_]+)/status)/(\d+)",
    re.I,
)
STATUS_ID_RE = re.compile(r"\b(\d{15,25})\b")
VIDEO_CDN_RE = re.compile(
    r"https?://video\.twimg\.com/[^\s\"'<>]+",
    re.I,
)
DIM_RE = re.compile(r"/(\d+)x(\d+)/")

API_ENDPOINTS = (
    "https://api.fxtwitter.com/status/{id}",
    "https://api.vxtwitter.com/Twitter/status/{id}",
)

ProgressCb = Callable[[int, int, float], None]
CancelFn = Callable[[], bool]

_proxy_lock = threading.Lock()
_proxy_mode = "system"  # system | direct | explicit
_proxy_url = ""
_opener = urllib.request.build_opener()


class Cancelled(Exception):
    """Raised when a download is cancelled by the caller."""


@dataclass
class VideoFormat:
    url: str
    bitrate: int = 0
    width: int = 0
    height: int = 0
    container: str = "mp4"

    @property
    def is_mp4(self) -> bool:
        return "m3u8" not in self.url.lower() and (
            self.container == "mp4" or ".mp4" in self.url.lower()
        )

    @property
    def label(self) -> str:
        dim = f"{self.width}x{self.height}" if self.width and self.height else "?"
        br = f"{self.bitrate // 1000}k" if self.bitrate else "?"
        kind = "mp4" if self.is_mp4 else "hls"
        return f"{dim} {br} {kind}"

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "bitrate": self.bitrate,
            "width": self.width,
            "height": self.height,
            "container": self.container,
            "label": self.label,
        }


@dataclass
class TweetVideo:
    tweet_id: str
    media_id: str
    author: str
    text: str
    duration: float
    thumbnail: str
    formats: list[VideoFormat] = field(default_factory=list)

    @property
    def best_mp4(self) -> Optional[VideoFormat]:
        mp4s = [f for f in self.formats if f.is_mp4]
        if not mp4s:
            return None
        return max(mp4s, key=lambda f: (f.height, f.width, f.bitrate))

    @property
    def tweet_url(self) -> str:
        return f"https://x.com/{self.author}/status/{self.tweet_id}"


@dataclass
class PreparedDownload:
    tweet_id: str
    media_id: str
    author: str
    text: str
    duration: float
    thumbnail: str
    fmt: VideoFormat
    dest: Path
    source: str
    skipped: bool = False

    def to_meta(self, size: int = 0) -> dict:
        return {
            "id": self.dest.stem,
            "tweet_id": self.tweet_id,
            "media_id": self.media_id,
            "author": self.author,
            "text": self.text,
            "duration": self.duration,
            "thumbnail_url": self.thumbnail,
            "quality": f"{self.fmt.height}p" if self.fmt.height else "video",
            "width": self.fmt.width,
            "height": self.fmt.height,
            "bitrate": self.fmt.bitrate,
            "size": size,
            "filename": self.dest.name,
            "url": self.source,
        }


def eprint(*args: object, **kwargs: object) -> None:
    print(*args, file=sys.stderr, flush=True, **kwargs)


def env_proxy() -> str:
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "ALL_PROXY", "all_proxy"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def normalize_proxy(value: Optional[str]) -> tuple[str, str]:
    raw = (value or "").strip()
    if not raw or raw.lower() in {"system", "env", "auto", "default"}:
        return "system", ""
    if raw.lower() in {"direct", "none", "off", "no", "disabled", "disable"}:
        return "direct", ""
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https", "socks", "socks4", "socks5", "socks5h"}:
        raise ValueError("代理只支持 http / https / socks5，例如 http://127.0.0.1:7890")
    if not parsed.hostname:
        raise ValueError("代理地址缺少主机名")
    if parsed.port is None and scheme in {"http", "https"}:
        raise ValueError("代理地址缺少端口，例如 http://127.0.0.1:7890")
    return "explicit", raw


def proxy_status() -> dict:
    with _proxy_lock:
        mode = _proxy_mode
        url = _proxy_url
    return {
        "mode": mode,
        "url": url,
        "env": env_proxy(),
        "label": proxy_label(mode, url),
    }


def proxy_label(mode: str, url: str) -> str:
    if mode == "direct":
        return "直连"
    if mode == "explicit":
        return url
    env = env_proxy()
    return f"系统环境（{env}）" if env else "系统环境（未设置）"


def _socks_opener(url: str) -> urllib.request.OpenerDirector:
    parsed = urllib.parse.urlparse(url)
    try:
        import socks
        from sockshandler import SocksiPyHandler
    except ImportError as err:
        raise RuntimeError("SOCKS 代理需要先安装 PySocks：pip3 install PySocks") from err
    scheme = (parsed.scheme or "socks5").lower()
    sock_type = socks.SOCKS4 if scheme.startswith("socks4") else socks.SOCKS5
    rdns = scheme in {"socks5h", "socks4a"}
    handler = SocksiPyHandler(
        sock_type,
        parsed.hostname,
        parsed.port or 1080,
        rdns=rdns,
        username=parsed.username,
        password=parsed.password,
    )
    return urllib.request.build_opener(handler)


def _make_opener(mode: str, url: str) -> urllib.request.OpenerDirector:
    if mode == "direct":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if mode == "explicit":
        if url.lower().startswith("socks"):
            return _socks_opener(url)
        proxies = {"http": url, "https": url}
        return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    return urllib.request.build_opener()


def set_proxy(value: Optional[str]) -> dict:
    global _proxy_mode, _proxy_url, _opener
    mode, url = normalize_proxy(value)
    opener = _make_opener(mode, url)
    with _proxy_lock:
        _proxy_mode = mode
        _proxy_url = url
        _opener = opener
        urllib.request.install_opener(opener)
    return proxy_status()


def current_opener() -> urllib.request.OpenerDirector:
    with _proxy_lock:
        return _opener


def urlopen(req: urllib.request.Request, timeout: float = 20.0):
    return current_opener().open(req, timeout=timeout)


def yt_dlp_proxy_args() -> list[str]:
    with _proxy_lock:
        mode = _proxy_mode
        url = _proxy_url
    if mode == "direct":
        return ["--proxy", ""]
    if mode == "explicit":
        return ["--proxy", url]
    return []


def test_proxy(timeout: float = 12.0) -> dict:
    started = time.time()
    status = proxy_status()
    probes = (
        ("https://api.ipify.org?format=json", "ipify"),
        ("https://api.fxtwitter.com/", "fxtwitter"),
    )
    last_err = ""
    for url, name in probes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read(800)
                code = getattr(resp, "status", None) or resp.getcode()
            elapsed = round(time.time() - started, 2)
            ip = ""
            try:
                data = json.loads(body.decode("utf-8", "replace"))
                if isinstance(data, dict):
                    ip = str(data.get("ip") or "")
            except json.JSONDecodeError:
                pass
            return {
                "ok": True,
                "via": status["label"],
                "probe": name,
                "status": code,
                "ip": ip,
                "elapsed": elapsed,
            }
        except Exception as err:
            last_err = str(err)
    return {
        "ok": False,
        "via": status["label"],
        "error": last_err or "代理测试失败",
        "elapsed": round(time.time() - started, 2),
    }


def say(*args: object) -> None:
    print(*args, flush=True)


def parse_tweet_ids(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return []
    if VIDEO_CDN_RE.search(raw) and "status/" not in raw:
        return [raw.strip()]
    ids = TWEET_URL_RE.findall(raw)
    if ids:
        return ids
    if re.fullmatch(r"\d{15,25}", raw):
        return [raw]
    return []


def extract_all_inputs(text: str) -> list[str]:
    """Pull every tweet id / CDN url out of a pasted blob."""
    items: list[str] = []
    seen: set[str] = set()

    def add(item: str) -> None:
        if item and item not in seen:
            seen.add(item)
            items.append(item)

    for m in TWEET_URL_RE.finditer(text or ""):
        add(m.group(1))
    for m in VIDEO_CDN_RE.finditer(text or ""):
        add(m.group(0).rstrip(".,);]"))
    if not items:
        for m in STATUS_ID_RE.finditer(text or ""):
            add(m.group(1))
    return items


def collect_inputs(values: list[str], file_path: Optional[str], allow_stdin: bool) -> list[str]:
    chunks: list[str] = list(values)
    if file_path:
        text = Path(file_path).read_text(encoding="utf-8")
        chunks.extend(text.splitlines())
    if allow_stdin and not sys.stdin.isatty():
        chunks.extend(sys.stdin.read().splitlines())
    items: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        found = extract_all_inputs(chunk) or parse_tweet_ids(chunk)
        for item in found:
            if item not in seen:
                seen.add(item)
                items.append(item)
    return items


def http_get_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            return json.loads(body.decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_err = err
            time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"请求失败: {url} ({last_err})")


def _dim_from_url(url: str) -> tuple[int, int]:
    m = DIM_RE.search(url)
    if not m:
        return 0, 0
    return int(m.group(1)), int(m.group(2))


def _formats_from_media(media: dict) -> list[VideoFormat]:
    out: list[VideoFormat] = []
    seen: set[str] = set()
    raw_list = media.get("formats") or media.get("variants") or []
    if media.get("url"):
        raw_list = list(raw_list) + [
            {
                "url": media["url"],
                "bitrate": 0,
                "container": media.get("format", "video/mp4").split("/")[-1],
            }
        ]
    for item in raw_list:
        url = (item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        w, h = _dim_from_url(url)
        if not w:
            w = int(item.get("width") or media.get("width") or 0)
            h = int(item.get("height") or media.get("height") or 0)
        container = (
            item.get("container")
            or (item.get("content_type") or item.get("type") or "mp4").split("/")[-1]
        )
        if container in {"x-mpegURL", "mpegurl"}:
            container = "m3u8"
        out.append(
            VideoFormat(
                url=url,
                bitrate=int(item.get("bitrate") or 0),
                width=w,
                height=h,
                container=container,
            )
        )
    return out


def fetch_tweet_videos(tweet_id: str) -> list[TweetVideo]:
    last_err = "未知错误"
    data: Optional[dict] = None
    for tmpl in API_ENDPOINTS:
        url = tmpl.format(id=tweet_id)
        try:
            payload = http_get_json(url)
        except RuntimeError as err:
            last_err = str(err)
            continue
        code = payload.get("code")
        tweet = payload.get("tweet") or payload
        if code not in (None, 200) and not tweet:
            last_err = payload.get("message") or f"HTTP {code}"
            continue
        if not tweet or not isinstance(tweet, dict):
            last_err = "接口没有返回推文"
            continue
        data = tweet
        break
    if data is None:
        raise RuntimeError(f"无法读取推文 {tweet_id}: {last_err}")

    media = data.get("media") or {}
    items = []
    if isinstance(media, dict):
        items = media.get("videos") or [
            m
            for m in (media.get("all") or [])
            if (m.get("type") or "").lower() in {"video", "gif", "animated_gif"}
        ]
    elif isinstance(media, list):
        items = [m for m in media if (m.get("type") or "").lower() in {"video", "gif", "animated_gif"}]

    videos: list[TweetVideo] = []
    author = (
        (data.get("author") or {}).get("screen_name")
        or data.get("user_name")
        or data.get("user_screen_name")
        or "unknown"
    )
    text = (data.get("text") or data.get("full_text") or "").strip()
    for media_item in items:
        formats = _formats_from_media(media_item)
        if not formats:
            continue
        videos.append(
            TweetVideo(
                tweet_id=str(data.get("id") or tweet_id),
                media_id=str(media_item.get("id") or ""),
                author=str(author),
                text=text,
                duration=float(media_item.get("duration") or data.get("duration") or 0),
                thumbnail=str(media_item.get("thumbnail_url") or media_item.get("thumbnail") or ""),
                formats=formats,
            )
        )
    return videos


def pick_format(video: TweetVideo, quality: str) -> VideoFormat:
    mp4s = [f for f in video.formats if f.is_mp4]
    pool = mp4s or video.formats
    if not pool:
        raise RuntimeError("这条视频没有可用格式")
    q = quality.lower().strip()
    if q in {"best", "max", "highest"}:
        return max(pool, key=lambda f: (f.height, f.width, f.bitrate))
    if q in {"worst", "min", "lowest"}:
        return min(pool, key=lambda f: (f.height or 10**9, f.width, f.bitrate))
    m = re.match(r"^(\d{3,4})p?$", q)
    if not m:
        raise RuntimeError(f"无法识别清晰度: {quality}（例如 best / 720 / 1080p）")
    target = int(m.group(1))
    not_above = [f for f in pool if f.height and f.height <= target]
    if not_above:
        return max(not_above, key=lambda f: (f.height, f.width, f.bitrate))
    return min(pool, key=lambda f: (abs((f.height or target) - target), -(f.bitrate)))


def safe_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "video"


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "?"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def build_filename(video: TweetVideo, fmt: VideoFormat, index: int, total: int) -> str:
    height = f"{fmt.height}p" if fmt.height else "video"
    parts = [video.author, video.tweet_id, height]
    if total > 1:
        parts.insert(2, str(index))
    return safe_filename("-".join(parts)) + ".mp4"


def yt_dlp_path() -> Optional[str]:
    return shutil.which("yt-dlp")


def download_with_ytdlp(url: str, dest: Path) -> None:
    binary = yt_dlp_path()
    if not binary:
        raise FileNotFoundError("yt-dlp")
    cmd = [binary, "--no-mtime", *yt_dlp_proxy_args(), "-o", str(dest), url]
    if not sys.stderr.isatty():
        cmd.insert(2, "--no-progress")
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp 退出码 {proc.returncode}")


def download_with_urllib(
    url: str,
    dest: Path,
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelFn] = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            started = time.time()
            last_emit = 0.0
            with open(part, "wb") as fh:
                while True:
                    if should_cancel and should_cancel():
                        raise Cancelled("已取消")
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    elapsed = max(now - started, 0.001)
                    speed = downloaded / elapsed
                    if on_progress and (now - last_emit >= 0.2 or downloaded == total):
                        on_progress(downloaded, total, speed)
                        last_emit = now
                    if sys.stderr.isatty() and total and now - last_emit >= 0.2:
                        pct = downloaded * 100 / total if total else 0
                        eta = (total - downloaded) / speed if speed else 0
                        eprint(
                            f"\r[{pct:5.1f}%] {downloaded / 1048576:7.1f}/{total / 1048576:.1f} MiB  "
                            f"{speed / 1048576:5.1f} MiB/s  ETA {format_duration(eta)}",
                            end="",
                        )
        if sys.stderr.isatty() and total:
            eprint("")
        if on_progress:
            on_progress(downloaded, total or downloaded, 0.0)
        part.replace(dest)
    except Cancelled:
        if part.exists():
            part.unlink()
        raise
    except Exception:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        raise


def save_url_to_file(url: str, dest: Path, timeout: float = 20.0) -> bool:
    if not url:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            dest.write_bytes(resp.read())
        return dest.exists() and dest.stat().st_size > 0
    except Exception:
        return False


def file_complete(dest: Path, url: str = "") -> bool:
    if not dest.exists() or dest.stat().st_size == 0:
        return False
    if not url:
        return True
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=15) as resp:
            length = int(resp.headers.get("Content-Length") or 0)
        if length and dest.stat().st_size == length:
            return True
        if length and dest.stat().st_size < length:
            return False
    except Exception:
        pass
    return dest.exists() and dest.stat().st_size > 0


def download_url(
    url: str,
    dest: Path,
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelFn] = None,
    prefer_urllib: bool = False,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if prefer_urllib or on_progress or should_cancel or not yt_dlp_path():
        download_with_urllib(url, dest, on_progress=on_progress, should_cancel=should_cancel)
        return
    download_with_ytdlp(url, dest)


def print_info(video: TweetVideo, chosen: Optional[VideoFormat] = None) -> None:
    preview = video.text.replace("\n", " ")
    if len(preview) > 80:
        preview = preview[:80] + "…"
    say(f"{video.author}/{video.tweet_id}  {format_duration(video.duration)}  {preview}")
    for i, fmt in enumerate(sorted(video.formats, key=lambda f: (f.height, f.bitrate)), 1):
        mark = "  <-" if chosen and fmt.url == chosen.url else ""
        say(f"  {i:>2}. {fmt.label}{mark}")


def prepare_tweet_downloads(
    tweet_id: str,
    out_dir: Path,
    quality: str,
    skip_existing: bool = True,
) -> list[PreparedDownload]:
    videos = fetch_tweet_videos(tweet_id)
    if not videos:
        raise RuntimeError("这条帖没有视频")
    prepared: list[PreparedDownload] = []
    for i, video in enumerate(videos, 1):
        fmt = pick_format(video, quality)
        dest = out_dir / build_filename(video, fmt, i, len(videos))
        prepared.append(
            PreparedDownload(
                tweet_id=video.tweet_id,
                media_id=video.media_id,
                author=video.author,
                text=video.text,
                duration=video.duration,
                thumbnail=video.thumbnail,
                fmt=fmt,
                dest=dest,
                source=video.tweet_url,
                skipped=bool(skip_existing and dest.exists() and dest.stat().st_size > 0),
            )
        )
    return prepared


def prepare_cdn_download(url: str, out_dir: Path, skip_existing: bool = True) -> PreparedDownload:
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name or "video.mp4"
    dest = out_dir / safe_filename(name)
    w, h = _dim_from_url(url)
    return PreparedDownload(
        tweet_id="",
        media_id="",
        author="cdn",
        text="",
        duration=0,
        thumbnail="",
        fmt=VideoFormat(url=url, width=w, height=h, container="mp4"),
        dest=dest,
        source=url,
        skipped=bool(skip_existing and dest.exists() and dest.stat().st_size > 0),
    )


def download_direct_cdn(url: str, out_dir: Path) -> Path:
    item = prepare_cdn_download(url, out_dir)
    if item.skipped:
        say(f"已存在，跳过  {item.dest}")
        return item.dest
    say(f"下载直链  {item.dest.name}")
    download_url(url, item.dest)
    return item.dest


def process_tweet(
    tweet_id: str,
    out_dir: Path,
    quality: str,
    info_only: bool,
    skip_existing: bool,
) -> int:
    videos = fetch_tweet_videos(tweet_id)
    if not videos:
        eprint(f"✗ {tweet_id}: 这条帖没有视频")
        return 1
    if info_only:
        for video in videos:
            print_info(video, pick_format(video, quality))
        return 0
    prepared = prepare_tweet_downloads(tweet_id, out_dir, quality, skip_existing)
    errors = 0
    for item in prepared:
        if item.skipped:
            say(f"已存在，跳过  {item.dest.name}")
            continue
        say(
            f"↓ @{item.author}  {item.tweet_id}  {item.fmt.label}  "
            f"{format_duration(item.duration)}  -> {item.dest.name}"
        )
        try:
            download_url(item.fmt.url, item.dest)
            size_mb = item.dest.stat().st_size / 1048576
            say(f"✓ {item.dest}  {size_mb:.1f} MB")
        except Exception as err:
            errors += 1
            eprint(f"✗ {tweet_id}: {err}")
    return errors


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xdl",
        description="从 X/Twitter 下载视频（含敏感内容）。支持帖子链接、纯 ID、批量文件。",
    )
    p.add_argument("urls", nargs="*", help="推文链接或数字 ID，可多个")
    p.add_argument("-f", "--file", help="从文本文件读取链接，一行一条")
    p.add_argument("-o", "--output", default="downloads", help="保存目录（默认 ./downloads）")
    p.add_argument(
        "-q",
        "--quality",
        default="best",
        help="清晰度: best / worst / 270 / 360 / 720 / 1080 / 1440（默认 best）",
    )
    p.add_argument("--info", action="store_true", help="只显示信息，不下载")
    p.add_argument("--json", action="store_true", help="以 JSON 打印解析结果")
    p.add_argument("--no-skip", action="store_true", help="已存在的文件也重新下载")
    p.add_argument("--open", action="store_true", help="完成后打开保存目录（macOS）")
    p.add_argument("--serve", action="store_true", help="打开可视化界面")
    p.add_argument("--port", type=int, default=8787, help="可视化界面端口（默认 8787）")
    p.add_argument(
        "--proxy",
        default="system",
        help="代理: system（默认跟环境变量）/ direct（直连）/ http://127.0.0.1:7890",
    )
    return p


def dump_json(tweet_ids: Iterable[str], quality: str) -> int:
    payload = []
    errors = 0
    for tweet_id in tweet_ids:
        try:
            videos = fetch_tweet_videos(tweet_id)
            chosen = [pick_format(v, quality) for v in videos]
            payload.append(
                {
                    "id": tweet_id,
                    "videos": [
                        {
                            "author": v.author,
                            "tweet_id": v.tweet_id,
                            "media_id": v.media_id,
                            "text": v.text,
                            "duration": v.duration,
                            "thumbnail": v.thumbnail,
                            "chosen": {
                                "url": c.url,
                                "width": c.width,
                                "height": c.height,
                                "bitrate": c.bitrate,
                            },
                            "formats": [
                                {
                                    "url": f.url,
                                    "width": f.width,
                                    "height": f.height,
                                    "bitrate": f.bitrate,
                                    "container": f.container,
                                }
                                for f in v.formats
                            ],
                        }
                        for v, c in zip(videos, chosen)
                    ],
                }
            )
        except Exception as err:
            errors += 1
            payload.append({"id": tweet_id, "error": str(err)})
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return errors


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        set_proxy(args.proxy)
    except ValueError as err:
        eprint(str(err))
        return 2
    if args.serve:
        from app import serve

        out = args.output if args.output != "downloads" else str(Path(__file__).resolve().parent / "downloads")
        return serve(port=args.port, out_dir_value=out, proxy=args.proxy)

    items = collect_inputs(args.urls, args.file, allow_stdin=not args.serve)
    if not items:
        build_parser().print_help()
        eprint("\n示例: xdl.py https://x.com/user/status/123")
        eprint("界面:  xdl.py --serve")
        return 2

    out_dir = Path(args.output).expanduser()
    if args.json:
        return dump_json(items, args.quality)

    errors = 0
    for item in items:
        try:
            if item.startswith("http") and "video.twimg.com" in item:
                if args.info:
                    say(item)
                    continue
                download_direct_cdn(item, out_dir)
                continue
            errors += process_tweet(
                item,
                out_dir=out_dir,
                quality=args.quality,
                info_only=args.info,
                skip_existing=not args.no_skip,
            )
        except Exception as err:
            errors += 1
            eprint(f"✗ {item}: {err}")

    if args.open and not args.info:
        subprocess.run(["open", str(out_dir)], check=False)
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        eprint("已中断")
        raise SystemExit(130)
