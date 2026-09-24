#!/usr/bin/env python3
"""Download videos from X / Twitter, Heiliao/Haijiao /archives/, and eve568.

yt-dlp's guest GraphQL often returns TweetTombstone for NSFW tweets.
This tool resolves media via the FxTwitter / VxTwitter embed APIs, then
downloads the chosen MP4 with yt-dlp (or urllib if yt-dlp is missing).

Archives pages embed AES-128 HLS in DPlayer; those playlists are decrypted
and remuxed to MP4. Haijiao permanent hosts need a browser TLS fingerprint
and usually a local proxy (Cloudflare RST on direct connect).

eve568 play pages need a portal login (u/n/s). The API then returns a
COS-signed MP4 that expires in about 15 minutes.
"""

from __future__ import annotations

import argparse
import html as html_lib
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

XH_GET_VIDEO_URL = Path("/Volumes/xinba/10_Projects/Active/oppositenum/xh/get_video_url.py")
XH_DEFAULT_LOGIN_ID = "a10006"
XH_DEFAULT_PASSWORD = "Aa11221122"

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
ARCHIVES_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?([a-zA-Z0-9.-]+)/archives/(\d+)/?",
    re.I,
)
HAIJIAO_HOST_RE = re.compile(
    r"(?:^|\.)(?:hjw\d+|hjwang\d+|haijiao(?:wang)?|haijw)\.(?:com|cc|net|app)$",
    re.I,
)
EVE568_PLAY_RE = re.compile(
    r"(?:https?://)?(?:www\.)?([a-zA-Z0-9.-]+)/v/"
    r"(adult-long-video|adult-short-video|movie|tv)/play/"
    r"([a-fA-F0-9]{24})(?:\?[^\s\"'<>]*)?",
    re.I,
)
EVE568_SEARCH_RE = re.compile(
    r"(?:https?://)?(?:www\.)?([a-zA-Z0-9.-]+)/v/"
    r"(?:(adult-long-video|adult-short-video|movie|tv)/)?search(?:/[^\s\"'<>]*)?"
    r"(\?[^\s\"'<>]*)?",
    re.I,
)
EVE568_PORTAL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?([a-zA-Z0-9.-]+)/v(?:/|\?(?:[^\s\"'<>]*))?",
    re.I,
)
EVE568_HOST_RE = re.compile(r"(?:^|\.)eve568\.com$", re.I)
EVE568_CATEGORY_IDS = {
    "adult-long-video": "5b73eb8886d00574944a9dc1",
    "adult-short-video": "5b73eb8886d00574944a9dc2",
    "movie": "5b73eb8886d00574944a9dc3",
    "tv": "5b73eb8886d00574944a9dc4",
}
EVE568_CATEGORY_KINDS = {value: key for key, value in EVE568_CATEGORY_IDS.items()}
EVE568_SEARCH_PAGE_SIZE = 30
DIM_RE = re.compile(r"/(\d+)x(\d+)/")
HLS_WORKERS = 4
HLS_SEGMENT_RETRIES = 6
HAIJIAO_MIRRORS = (
    "hjw01.com",
    "hjw2026.com",
    "hjwang35.com",
    "hjwang34.com",
    "hjwang33.com",
)
HAIJIAO_GITLAB_README = (
    "https://gitlab.com/api/v4/projects/84517394/repository/files/README.md/raw?ref=main"
)
BROWSER_IMPERSONATES = ("chrome124", "chrome131", "chrome120", "safari17_0")
LOCAL_HTTP_PROXIES = ("http://127.0.0.1:7890",)

API_ENDPOINTS = (
    "https://api.fxtwitter.com/status/{id}",
    "https://api.vxtwitter.com/Twitter/status/{id}",
)

ProgressCb = Callable[[int, int, float], None]
CancelFn = Callable[[], bool]
StageCb = Callable[[str], None]

_proxy_lock = threading.Lock()
_proxy_mode = "system"  # system | direct | explicit
_proxy_url = ""
_opener = urllib.request.build_opener()
_eve568_lock = threading.Lock()
_eve568_jwt = ""
_eve568_jwt_exp = 0.0
_eve568_creds: dict[str, str] = {}
_eve568_account: dict[str, str] = {
    "login_id": XH_DEFAULT_LOGIN_ID,
    "password": XH_DEFAULT_PASSWORD,
    "script": str(XH_GET_VIDEO_URL),
}
_eve568_refresh_lock = threading.Lock()
_eve568_cred_listener: Optional[Callable[[dict], None]] = None


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
    alt_urls: list[str] = field(default_factory=list)

    def to_meta(self, size: int = 0) -> dict:
        return {
            "id": self.dest.stem,
            "tweet_id": self.tweet_id,
            "media_id": self.media_id,
            "author": self.author,
            "text": self.text,
            "duration": self.duration,
            "thumbnail_url": self.thumbnail,
            "quality": (
                f"{self.fmt.height}p"
                if self.fmt.height
                else ("HLS" if "m3u8" in self.fmt.url.lower() else "video")
            ),
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


def normalize_archives_url(host: str, archive_id: str) -> str:
    host = (host or "").strip().lower().lstrip(".")
    return f"https://{host}/archives/{archive_id}/"


def is_archives_url(value: str) -> bool:
    return bool(ARCHIVES_URL_RE.search(value or ""))


def is_haijiao_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return bool(HAIJIAO_HOST_RE.search(host))


def is_eve568_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return bool(EVE568_HOST_RE.search(host))


def normalize_eve568_play_url(raw: str) -> str:
    m = EVE568_PLAY_RE.search(raw or "")
    if not m:
        return (raw or "").strip()
    host, kind, video_id = m.group(1), m.group(2), m.group(3)
    scheme = "https"
    if (raw or "").lower().startswith("http://"):
        scheme = "http"
    return f"{scheme}://{host}/v/{kind}/play/{video_id}"


def is_eve568_play_url(value: str) -> bool:
    return bool(EVE568_PLAY_RE.search(value or ""))


def normalize_eve568_search_url(raw: str) -> str:
    m = EVE568_SEARCH_RE.search(raw or "")
    if not m:
        return (raw or "").strip()
    host, kind, query = m.group(1), m.group(2), m.group(3) or ""
    scheme = "http"
    if (raw or "").lower().startswith("https://"):
        scheme = "https"
    if kind:
        return f"{scheme}://{host}/v/{kind}/search{query}"
    return f"{scheme}://{host}/v/search{query}"


def is_eve568_search_url(value: str) -> bool:
    return bool(EVE568_SEARCH_RE.search(value or "")) and not is_eve568_play_url(value or "")


def is_eve568_global_search_url(value: str) -> bool:
    m = EVE568_SEARCH_RE.search(value or "")
    return bool(m and not m.group(2)) and not is_eve568_play_url(value or "")


def eve568_search_folder_name(page_url: str) -> str:
    parsed = urllib.parse.urlparse(page_url if "://" in (page_url or "") else "http://" + (page_url or ""))
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("keyword", "actorName", "manufacturer"):
        values = query.get(key) or []
        if values and values[0].strip():
            return safe_filename(values[0].strip())
    return ""


def eve568_keyword_search_url(keyword: str) -> str:
    keyword = (keyword or "").strip()
    origin = eve568_origin() or "http://pk.eve568.com"
    return f"{origin.rstrip('/')}/v/search?keyword={urllib.parse.quote(keyword)}"


def eve568_query_creds(url: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(url if "://" in (url or "") else "https://" + (url or ""))
    query = urllib.parse.parse_qs(parsed.query)
    out: dict[str, str] = {}
    for key in ("u", "n", "s"):
        values = query.get(key) or []
        if values and values[0]:
            out[key] = values[0]
    return out


def remember_eve568_creds_from_text(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in re.findall(r"https?://[^\s\"'<>]+", text or ""):
        creds = eve568_query_creds(raw)
        if {"u", "n", "s"} <= set(creds):
            found = creds
            host = urllib.parse.urlparse(raw).hostname or ""
            if is_eve568_host(host):
                found["origin"] = f"{urllib.parse.urlparse(raw).scheme or 'https'}://{host}"
    if found:
        set_eve568_creds(found)
    return found


def extract_all_inputs(text: str) -> list[str]:
    """Pull tweet ids, CDN urls, archives pages, and eve568 play urls."""
    remember_eve568_creds_from_text(text)
    items: list[str] = []
    seen: set[str] = set()

    def add(item: str) -> None:
        if item and item not in seen:
            seen.add(item)
            items.append(item)

    for m in EVE568_PLAY_RE.finditer(text or ""):
        add(normalize_eve568_play_url(m.group(0)))
    for m in EVE568_SEARCH_RE.finditer(text or ""):
        add(normalize_eve568_search_url(m.group(0)))
    for m in ARCHIVES_URL_RE.finditer(text or ""):
        add(normalize_archives_url(m.group(1), m.group(2)))
    for m in TWEET_URL_RE.finditer(text or ""):
        add(m.group(1))
    for m in VIDEO_CDN_RE.finditer(text or ""):
        add(m.group(0).rstrip(".,);]"))
    leftover: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or re.search(r"https?://", line, re.I):
            continue
        leftover.append(line)
    if leftover:
        for line in leftover:
            if STATUS_ID_RE.fullmatch(line):
                if not items:
                    add(line)
                continue
            add(eve568_keyword_search_url(line))
    elif not items:
        blob = (text or "").strip()
        if blob and not re.search(r"https?://", blob, re.I) and not STATUS_ID_RE.fullmatch(blob):
            add(eve568_keyword_search_url(blob))
        elif not items:
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
    referer: str = "",
    refresh_url: Optional[Callable[[], str]] = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    downloaded = part.stat().st_size if part.exists() else 0
    started = time.time()
    last_emit = 0.0
    last_refresh = time.time()
    current = url
    total = 0
    attempts = 0
    while True:
        if should_cancel and should_cancel():
            raise Cancelled("已取消")
        headers = _request_headers(referer)
        if downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"
        req = urllib.request.Request(current, headers=headers)
        try:
            resp = urlopen(req, timeout=60)
        except urllib.error.HTTPError as err:
            if err.code in {401, 403, 404} and refresh_url and attempts < 8:
                current = refresh_url()
                last_refresh = time.time()
                attempts += 1
                time.sleep(0.4)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
            attempts += 1
            if attempts > 30:
                raise RuntimeError("下载中断次数过多")
            time.sleep(0.6)
            if refresh_url:
                current = refresh_url()
                last_refresh = time.time()
            continue
        restart = False
        with resp:
            code = getattr(resp, "status", None) or resp.getcode()
            length = int(resp.headers.get("Content-Length") or 0)
            content_range = resp.headers.get("Content-Range") or ""
            if code == 206:
                if content_range and "/" in content_range:
                    try:
                        total = int(content_range.rsplit("/", 1)[1])
                    except ValueError:
                        total = downloaded + length
                else:
                    total = downloaded + length
                mode = "ab"
            else:
                downloaded = 0
                total = length
                mode = "wb"
            with open(part, mode) as fh:
                while True:
                    if should_cancel and should_cancel():
                        raise Cancelled("已取消")
                    if refresh_url and time.time() - last_refresh > 480:
                        current = refresh_url()
                        last_refresh = time.time()
                        restart = True
                        break
                    try:
                        chunk = resp.read(1024 * 256)
                    except (urllib.error.URLError, TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
                        restart = True
                        break
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    elapsed = max(now - started, 0.001)
                    speed = downloaded / elapsed
                    if on_progress and (now - last_emit >= 0.2 or (total and downloaded >= total)):
                        on_progress(downloaded, total, speed)
                        last_emit = now
        if restart:
            continue
        if total and downloaded >= total:
            part.replace(dest)
            if on_progress:
                on_progress(downloaded, total, 0.0)
            return
        if downloaded > 0 and not total:
            part.replace(dest)
            if on_progress:
                on_progress(downloaded, downloaded, 0.0)
            return
        attempts += 1
        if attempts > 30:
            raise RuntimeError("下载中断次数过多")
        time.sleep(0.6)
        if refresh_url:
            current = refresh_url()
            last_refresh = time.time()


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
    referer: str = "",
    on_stage: Optional[StageCb] = None,
    alt_urls: Optional[list[str]] = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if is_hls_url(url):
        download_hls(
            url,
            dest,
            referer=referer,
            on_progress=on_progress,
            should_cancel=should_cancel,
            on_stage=on_stage,
            alt_urls=alt_urls,
        )
        return
    if prefer_urllib or on_progress or should_cancel or not yt_dlp_path():
        download_with_urllib(
            url,
            dest,
            on_progress=on_progress,
            should_cancel=should_cancel,
            referer=referer,
        )
        return
    download_with_ytdlp(url, dest)


def is_hls_url(url: str) -> bool:
    return ".m3u8" in (url or "").lower()


def _request_headers(referer: str = "", accept: str = "*/*") -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
        parsed = urllib.parse.urlparse(referer)
        if parsed.scheme and parsed.netloc:
            headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    return headers


def _configured_proxy_url() -> str:
    with _proxy_lock:
        mode = _proxy_mode
        url = _proxy_url
    if mode == "direct":
        return ""
    if mode == "explicit":
        return url
    return env_proxy()


def _local_proxy_candidates() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def add(item: str) -> None:
        item = (item or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)

    add(_configured_proxy_url())
    with _proxy_lock:
        mode = _proxy_mode
    if mode != "direct":
        for item in LOCAL_HTTP_PROXIES:
            add(item)
    return out


def _curl_session():
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return None
    return curl_requests


def http_get_bytes(url: str, referer: str = "", timeout: float = 30.0) -> bytes:
    headers = _request_headers(referer)
    last_err: Optional[Exception] = None
    attempts = 5
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if not data:
                raise RuntimeError("空响应")
            return data
        except urllib.error.HTTPError as err:
            last_err = err
            if err.code in {401, 403, 404, 410}:
                body = b""
                try:
                    body = err.read(200)
                except Exception:
                    pass
                raise RuntimeError(f"请求失败 HTTP {err.code}: {url}") from err
            time.sleep(0.4 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, BrokenPipeError, OSError) as err:
            last_err = err
            time.sleep(0.5 * (attempt + 1))
        except Exception as err:
            last_err = err
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"请求失败: {url} ({last_err})")


def _browser_get_bytes(
    url: str,
    referer: str = "",
    timeout: float = 25.0,
    proxies: Optional[dict] = None,
) -> bytes:
    curl_requests = _curl_session()
    if curl_requests is None:
        raise RuntimeError("缺少 curl_cffi")
    last_err: Optional[Exception] = None
    # Do not override User-Agent: Cloudflare matches it against the TLS fingerprint.
    headers = {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if referer:
        headers["Referer"] = referer
        parsed = urllib.parse.urlparse(referer)
        if parsed.scheme and parsed.netloc:
            headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    for impersonate in BROWSER_IMPERSONATES:
        try:
            resp = curl_requests.get(
                url,
                impersonate=impersonate,
                headers=headers,
                timeout=timeout,
                proxies=proxies,
                allow_redirects=True,
            )
        except Exception as err:
            last_err = err
            continue
        if resp.status_code == 403:
            last_err = RuntimeError(f"HTTP 403 ({impersonate})")
            continue
        if resp.status_code >= 400:
            last_err = RuntimeError(f"HTTP {resp.status_code}")
            continue
        data = resp.content or b""
        if data:
            return data
        last_err = RuntimeError("空响应")
    raise RuntimeError(str(last_err) if last_err else "浏览器请求失败")


def fetch_html_bytes(url: str, referer: str = "") -> bytes:
    """Fetch HTML. Haijiao needs Chrome TLS; urllib is enough for Heiliao."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not is_haijiao_host(host):
        return http_get_bytes(url, referer=referer or url)
    last_err: Optional[Exception] = None
    if _curl_session() is None:
        try:
            return http_get_bytes(url, referer=referer or url)
        except Exception as err:
            raise RuntimeError(
                f"海角网页面打不开：{err}。本机直连会被 Cloudflare 重置，"
                "请 pip3 install curl_cffi，并在界面填本机代理（例如 http://127.0.0.1:7890）"
            ) from err
    attempts: list[Optional[dict]] = []
    for proxy in _local_proxy_candidates():
        attempts.append({"http": proxy, "https": proxy})
    if not attempts:
        attempts.append(None)
    for proxies in attempts:
        try:
            return _browser_get_bytes(url, referer=referer or url, proxies=proxies)
        except Exception as err:
            last_err = err
            continue
    hint = ""
    if not _local_proxy_candidates():
        hint = " 海角永久站通常要翻墙，请在界面填本机代理，例如 http://127.0.0.1:7890。"
    raise RuntimeError(f"海角网页面打不开：{last_err}.{hint}")


def http_get_text(url: str, referer: str = "", timeout: float = 30.0) -> str:
    return http_get_bytes(url, referer=referer, timeout=timeout).decode("utf-8", "replace")


def _abs_url(maybe: str, base: str) -> str:
    maybe = (maybe or "").strip()
    if not maybe:
        return ""
    return urllib.parse.urljoin(base, maybe)


def _parse_hls_iv(raw: str, sequence: int) -> bytes:
    value = (raw or "").strip()
    if value.lower().startswith("0x"):
        data = bytes.fromhex(value[2:])
        return data.rjust(16, b"\x00")[-16:]
    return sequence.to_bytes(16, "big")


def _unpad_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data.endswith(bytes([pad]) * pad):
        return data[:-pad]
    return data


def aes128_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(key) != 16:
        raise RuntimeError(f"HLS 密钥长度异常: {len(key)} bytes")
    if len(iv) != 16:
        raise RuntimeError(f"HLS IV 长度异常: {len(iv)} bytes")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as err:
        raise RuntimeError("HLS 解密需要 cryptography：pip3 install cryptography") from err
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return _unpad_pkcs7(decryptor.update(data) + decryptor.finalize())


def parse_m3u8_media(text: str, playlist_url: str) -> dict:
    key_url = ""
    iv = b""
    method = ""
    sequence = 0
    segments: list[dict] = []
    variants: list[dict] = []
    pending_dur = 0.0
    pending_bw = 0
    pending_res = ""
    is_master = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            is_master = True
            bw_m = re.search(r"BANDWIDTH=(\d+)", line)
            res_m = re.search(r"RESOLUTION=(\d+x\d+)", line)
            pending_bw = int(bw_m.group(1)) if bw_m else 0
            pending_res = res_m.group(1) if res_m else ""
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1])
            except ValueError:
                sequence = 0
            continue
        if line.startswith("#EXT-X-KEY:"):
            attrs = line.split(":", 1)[1]
            method_m = re.search(r"METHOD=([^,]+)", attrs)
            uri_m = re.search(r'URI="([^"]+)"', attrs)
            iv_m = re.search(r"IV=([^,]+)", attrs)
            method = (method_m.group(1) if method_m else "").upper()
            key_url = _abs_url(uri_m.group(1) if uri_m else "", playlist_url)
            iv = _parse_hls_iv(iv_m.group(1) if iv_m else "", sequence)
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending_dur = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                pending_dur = 0.0
            continue
        if line.startswith("#"):
            continue
        if is_master:
            variants.append(
                {
                    "url": _abs_url(line, playlist_url),
                    "bandwidth": pending_bw,
                    "resolution": pending_res,
                }
            )
            pending_bw = 0
            pending_res = ""
            continue
        segments.append(
            {
                "url": _abs_url(line, playlist_url),
                "duration": pending_dur,
                "sequence": sequence + len(segments),
            }
        )
        pending_dur = 0.0
    return {
        "method": method,
        "key_url": key_url,
        "iv": iv,
        "segments": segments,
        "variants": variants,
        "duration": sum(float(s.get("duration") or 0) for s in segments),
    }


def resolve_hls_playlist(m3u8_url: str, referer: str = "") -> dict:
    text = http_get_text(m3u8_url, referer=referer)
    parsed = parse_m3u8_media(text, m3u8_url)
    if parsed["segments"]:
        parsed["playlist_url"] = m3u8_url
        return parsed
    if not parsed["variants"]:
        raise RuntimeError("m3u8 里没有可用分片")
    best = max(parsed["variants"], key=lambda v: (v["bandwidth"], v["url"]))
    child_url = best["url"]
    child = parse_m3u8_media(http_get_text(child_url, referer=referer), child_url)
    if not child["segments"]:
        raise RuntimeError("清晰度列表没有可用分片")
    child["playlist_url"] = child_url
    child["bandwidth"] = best["bandwidth"]
    child["resolution"] = best["resolution"]
    return child


def remux_ts_to_mp4(ts_path: Path, dest: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("HLS 转 MP4 需要本机 ffmpeg")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp.mp4")
    attempts = [
        [ffmpeg, "-y", "-loglevel", "error", "-f", "mpegts", "-i", str(ts_path), "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(tmp)],
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(ts_path), "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(tmp)],
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(ts_path), "-c", "copy", "-movflags", "+faststart", str(tmp)],
    ]
    last_err = ""
    for cmd in attempts:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(dest)
            return
        last_err = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    raise RuntimeError(f"ffmpeg 转封装失败: {last_err}")


def _transcode_ts_to_mp4(ts_path: Path, dest: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("HLS 转 MP4 需要本机 ffmpeg")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp.mp4")
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-i",
        str(ts_path),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(dest)
        return
    last_err = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    raise RuntimeError(f"ffmpeg 转码失败: {last_err}")


def _looks_like_mpegts(data: bytes) -> bool:
    if len(data) < 188:
        return False
    if data[0] == 0x47:
        return True
    if data[:4] == b"\x00\x00\x00\x01" or data[:3] == b"\x00\x00\x01":
        return True
    return data[:3] == b"ID3"


def download_hls(
    m3u8_url: str,
    dest: Path,
    referer: str = "",
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelFn] = None,
    on_stage: Optional[StageCb] = None,
    alt_urls: Optional[list[str]] = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    candidates: list[str] = []
    for item in [m3u8_url, *(alt_urls or [])]:
        item = (item or "").strip()
        if item and item not in candidates:
            candidates.append(item)
    if not candidates:
        raise RuntimeError("没有可下载的 HLS 地址")
    last_err: Optional[Exception] = None
    for candidate in candidates:
        try:
            _download_hls_once(
                candidate,
                dest,
                referer=referer,
                on_progress=on_progress,
                should_cancel=should_cancel,
                on_stage=on_stage,
            )
            return
        except Cancelled:
            raise
        except Exception as err:
            last_err = err
            continue
    raise RuntimeError(str(last_err) if last_err else "HLS 下载失败")


def _download_hls_once(
    m3u8_url: str,
    dest: Path,
    referer: str = "",
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelFn] = None,
    on_stage: Optional[StageCb] = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    playlist = resolve_hls_playlist(m3u8_url, referer=referer)
    segments = playlist["segments"]
    if not segments:
        raise RuntimeError("没有可下载的 HLS 分片")
    key = b""
    iv = playlist["iv"] or b"\x00" * 16
    method = (playlist["method"] or "NONE").upper()
    if method not in {"", "NONE", "AES-128"}:
        raise RuntimeError(f"不支持的 HLS 加密: {method}")
    if method == "AES-128":
        if not playlist["key_url"]:
            raise RuntimeError("m3u8 缺少 AES 密钥地址")
        key = http_get_bytes(playlist["key_url"], referer=referer, timeout=20)
        if len(key) != 16:
            raise RuntimeError(f"HLS 密钥无效: {len(key)} bytes")

    work = dest.with_suffix(dest.suffix + ".hls")
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    ts_out = work / "all.ts"
    started = time.time()
    downloaded = 0
    total_hint = 0
    last_emit = 0.0
    progress_lock = threading.Lock()
    done: set[int] = set()

    def emit(force: bool = False) -> None:
        nonlocal last_emit
        now = time.time()
        if not on_progress:
            return
        if not force and now - last_emit < 0.2:
            return
        last_emit = now
        elapsed = max(now - started, 0.001)
        speed = downloaded / elapsed
        on_progress(downloaded, total_hint or downloaded, speed)

    def fetch_one(index: int, url: str) -> int:
        nonlocal downloaded, total_hint
        part = work / f"{index:05d}.ts"
        last_err: Optional[Exception] = None
        for attempt in range(HLS_SEGMENT_RETRIES):
            if should_cancel and should_cancel():
                raise Cancelled("已取消")
            try:
                data = http_get_bytes(url, referer=referer, timeout=45)
                if method == "AES-128":
                    seg_iv = iv if playlist["iv"] else _parse_hls_iv("", segments[index]["sequence"])
                    data = aes128_decrypt(data, key, seg_iv)
                if len(data) < 188:
                    raise RuntimeError(f"分片过短 {len(data)} bytes")
                if not _looks_like_mpegts(data):
                    raise RuntimeError("分片不是 TS")
                part.write_bytes(data)
                with progress_lock:
                    if index not in done:
                        done.add(index)
                        downloaded += len(data)
                        if total_hint <= 0:
                            total_hint = max(len(data), 1) * len(segments)
                        emit()
                return index
            except Cancelled:
                raise
            except Exception as err:
                last_err = err
                msg = str(err)
                if any(code in msg for code in ("HTTP 401", "HTTP 403", "HTTP 404", "HTTP 410")):
                    break
                time.sleep(0.35 * (attempt + 1))
        raise RuntimeError(f"分片 {index + 1}/{len(segments)} 下载失败: {last_err}")

    try:
        if should_cancel and should_cancel():
            raise Cancelled("已取消")
        workers = min(HLS_WORKERS, len(segments))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(fetch_one, i, seg["url"]) for i, seg in enumerate(segments)]
            try:
                for fut in as_completed(futures):
                    if should_cancel and should_cancel():
                        raise Cancelled("已取消")
                    fut.result()
            except Exception:
                for pending in futures:
                    pending.cancel()
                raise
        missing = [i for i in range(len(segments)) if not (work / f"{i:05d}.ts").exists()]
        if missing:
            raise RuntimeError(f"缺少 {len(missing)} 个 HLS 分片，例如 {missing[0]:05d}.ts")
        emit(force=True)
        with open(ts_out, "wb") as out:
            for i in range(len(segments)):
                part = work / f"{i:05d}.ts"
                out.write(part.read_bytes())
                try:
                    part.unlink()
                except OSError:
                    pass
        if on_progress:
            on_progress(downloaded, int(downloaded / 0.97) or downloaded + 1, 0.0)
        if should_cancel and should_cancel():
            raise Cancelled("已取消")
        if on_stage:
            on_stage("转封装中")
        try:
            remux_ts_to_mp4(ts_out, dest)
        except Exception:
            if on_stage:
                on_stage("转码中")
            _transcode_ts_to_mp4(ts_out, dest)
        if on_progress:
            size = dest.stat().st_size if dest.exists() else downloaded
            on_progress(size, size, 0.0)
    except Cancelled:
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        raise
    except Exception:
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _text_from_html(html: str, pattern: str) -> str:
    m = re.search(pattern, html, re.I | re.S)
    if not m:
        return ""
    return html_lib.unescape(re.sub(r"\s+", " ", m.group(1))).strip()


def _json_attr(attrs: str, name: str = "config") -> Optional[dict]:
    token = f"{name}='"
    start_at = attrs.find(token)
    quote = "'"
    if start_at < 0:
        token = f'{name}="'
        start_at = attrs.find(token)
        quote = '"'
    if start_at < 0 and name == "config":
        return _json_attr(attrs, "data-config")
    if start_at < 0:
        return None
    brace = attrs.find("{", start_at)
    if brace < 0:
        return None
    depth = 0
    for index, char in enumerate(attrs[brace:], brace):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                raw = html_lib.unescape(attrs[brace : index + 1])
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    return None
                return data if isinstance(data, dict) else None
        elif char == quote and depth == 0:
            break
    return None


def parse_archive_page(page_url: str, html: str) -> list[dict]:
    archive_id = ""
    m_id = ARCHIVES_URL_RE.search(page_url)
    if m_id:
        archive_id = m_id.group(2)
    title = _text_from_html(html, r"<h1[^>]*>(.*?)</h1>") or _text_from_html(html, r"<title[^>]*>(.*?)</title>")
    title = re.sub(r"[-_|].*$", "", title).strip() or f"archives-{archive_id}"
    author = _text_from_html(html, r'property="og:site_name"[^>]*content="([^"]+)"') or "heiliao"
    thumb = _text_from_html(html, r'property="og:image"[^>]*content="([^"]+)"')
    if thumb and "social-default" in thumb:
        lazy = re.search(r'z-image-loader-url="(https?://[^"]+)"', html, re.I)
        if lazy:
            thumb = lazy.group(1)
    desc = _text_from_html(html, r'name="description"[^>]*content="([^"]+)"')
    players: list[dict] = []
    for m in re.finditer(r"<div([^>]*\bdplayer\b[^>]*)>", html, re.I):
        attrs = m.group(1)
        cfg = _json_attr(attrs, "config") or _json_attr(attrs, "data-config")
        if not cfg:
            continue
        video = cfg.get("video") or {}
        url = (video.get("url") or "").strip()
        if not url:
            alts = video.get("urls") or []
            if alts and isinstance(alts, list):
                url = str((alts[0] or {}).get("url") or "").strip()
        if not url:
            continue
        pic = str(video.get("pic") or thumb or "")
        title_m = re.search(r'data-video_title="([^"]*)"', attrs)
        extra: list[str] = []
        for item in video.get("urls") or []:
            if isinstance(item, dict) and item.get("url"):
                extra.append(str(item.get("url") or ""))
        h265 = cfg.get("video_h265") or {}
        if isinstance(h265, dict) and h265.get("url"):
            extra.append(str(h265.get("url") or ""))
        players.append(
            {
                "url": url,
                "alts": extra,
                "pic": pic,
                "title": html_lib.unescape(title_m.group(1) if title_m else title),
            }
        )
    if not players:
        raise RuntimeError("页面里没有可解析的播放器地址")
    items = []
    for i, player in enumerate(players, 1):
        items.append(
            {
                "archive_id": archive_id,
                "page_url": page_url,
                "author": author,
                "title": player["title"] or title,
                "text": desc or title,
                "thumbnail": player["pic"] or thumb,
                "m3u8": player["url"],
                "alts": player["alts"],
                "index": i,
                "total": len(players),
            }
        )
    return items


def _looks_like_archive_html(html: str) -> bool:
    low = (html or "").lower()
    return "dplayer" in low or ".m3u8" in low or 'class="dplayer"' in low


def _hop_url_from_html(html: str, base: str) -> str:
    m = re.search(r'<a[^>]+href=["\'](https?://[^"\']+/archives/\d+/?)["\']', html, re.I)
    if m:
        return m.group(1)
    m = re.search(r'(?:href|location\.replace|window\.location)\s*[=(]\s*["\'](https?://[^"\']+)["\']', html, re.I)
    if m:
        return urllib.parse.urljoin(base, m.group(1))
    m = re.search(r'http-equiv=["\']refresh["\'][^>]*content=["\'][^"\']*url=([^"\']+)', html, re.I)
    if m:
        return urllib.parse.urljoin(base, m.group(1).strip())
    return ""


def fetch_haijiao_mirrors() -> list[str]:
    hosts: list[str] = []
    seen: set[str] = set()

    def add(host: str) -> None:
        host = (host or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        if not host or host in seen:
            return
        seen.add(host)
        hosts.append(host)

    for host in HAIJIAO_MIRRORS:
        add(host)
    try:
        readme = http_get_text(HAIJIAO_GITLAB_README, timeout=15)
        for m in re.finditer(r"https?://(?:www\.)?([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", readme):
            host = m.group(1).lower()
            if is_haijiao_host(host) or host.startswith("hjw") or host.startswith("haijiao"):
                add(host)
    except Exception:
        pass
    return hosts


def archive_candidate_urls(page_url: str) -> list[str]:
    parsed = urllib.parse.urlparse(page_url)
    archive_id = ""
    m = ARCHIVES_URL_RE.search(page_url)
    if m:
        archive_id = m.group(2)
    if not archive_id:
        return [page_url]
    urls: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        url = (url or "").strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    add(page_url)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if is_haijiao_host(host):
        for mirror in fetch_haijiao_mirrors():
            add(f"https://www.{mirror}/archives/{archive_id}/")
            add(f"https://{mirror}/archives/{archive_id}/")
    return urls


def fetch_archive_html(page_url: str) -> tuple[str, str]:
    last_err: Optional[Exception] = None
    for url in archive_candidate_urls(page_url):
        try:
            html = fetch_html_bytes(url, referer=url).decode("utf-8", "replace")
        except Exception as err:
            last_err = err
            continue
        if _looks_like_archive_html(html):
            return url, html
        hop = _hop_url_from_html(html, url)
        if hop and hop.rstrip("/") != url.rstrip("/"):
            try:
                hopped = fetch_html_bytes(hop, referer=url).decode("utf-8", "replace")
            except Exception as err:
                last_err = err
                continue
            if _looks_like_archive_html(hopped):
                return hop, hopped
            last_err = RuntimeError(f"{hop} 跳转后仍没有播放器")
            continue
        last_err = RuntimeError(f"{url} 没有播放器地址")
    host = (urllib.parse.urlparse(page_url).hostname or "").lower()
    if is_haijiao_host(host):
        extra = (
            " 免翻墙镜像目前会跳到假落地页；永久站 hjw01.com 直连会被 Cloudflare 重置。"
            " 请在界面填本机代理后再试，例如 http://127.0.0.1:7890。"
        )
        raise RuntimeError(f"{last_err}{extra}" if last_err else f"海角网页面打不开。{extra}")
    if last_err:
        raise RuntimeError(str(last_err))
    raise RuntimeError("页面里没有可解析的播放器地址")


def fetch_archive_videos(page_url: str) -> list[dict]:
    final_url, html = fetch_archive_html(page_url)
    items = parse_archive_page(final_url, html)
    for item in items:
        item["page_url"] = final_url
    return items


def build_archive_filename(item: dict, fmt: VideoFormat) -> str:
    height = f"{fmt.height}p" if fmt.height else "hls"
    parts = [safe_filename(item.get("author") or "heiliao"), str(item.get("archive_id") or "video")]
    if item.get("total", 1) > 1:
        parts.append(str(item.get("index") or 1))
    parts.append(height)
    return "-".join(p for p in parts if p) + ".mp4"


def prepare_archive_downloads(
    page_url: str,
    out_dir: Path,
    skip_existing: bool = True,
) -> list[PreparedDownload]:
    videos = fetch_archive_videos(page_url)
    prepared: list[PreparedDownload] = []
    for item in videos:
        duration = 0.0
        height = 0
        width = 0
        chosen = item["m3u8"]
        last_err = ""
        source = str(item.get("page_url") or page_url)
        candidates = [item["m3u8"]]
        candidates.extend(item.get("alts") or [])
        for candidate in candidates:
            if not candidate:
                continue
            try:
                playlist = resolve_hls_playlist(candidate, referer=source)
                duration = float(playlist.get("duration") or 0)
                res = str(playlist.get("resolution") or "")
                if "x" in res:
                    try:
                        width, height = [int(x) for x in res.split("x", 1)]
                    except ValueError:
                        width, height = 0, 0
                chosen = candidate
                last_err = ""
                break
            except Exception as err:
                last_err = str(err)
                continue
        if last_err and chosen == item["m3u8"] and duration <= 0:
            # keep the original url; download step will retry
            pass
        fmt = VideoFormat(url=chosen, width=width, height=height, container="m3u8")
        dest = out_dir / build_archive_filename(item, fmt)
        alts = [u for u in candidates if u and u != chosen]
        prepared.append(
            PreparedDownload(
                tweet_id=str(item.get("archive_id") or ""),
                media_id=str(item.get("index") or ""),
                author=str(item.get("author") or "heiliao"),
                text=str(item.get("text") or item.get("title") or ""),
                duration=duration,
                thumbnail=str(item.get("thumbnail") or ""),
                fmt=fmt,
                dest=dest,
                source=source,
                skipped=bool(skip_existing and dest.exists() and dest.stat().st_size > 0),
                alt_urls=alts,
            )
        )
    return prepared


def refresh_archive_prepared(prepared: PreparedDownload) -> PreparedDownload:
    """Re-read the archives page so HLS auth_key is still valid."""
    if not is_archives_url(prepared.source):
        return prepared
    fresh_list = prepare_archive_downloads(prepared.source, prepared.dest.parent, skip_existing=False)
    for item in fresh_list:
        same_slot = str(item.media_id) == str(prepared.media_id)
        if same_slot or len(fresh_list) == 1:
            prepared.fmt = item.fmt
            prepared.duration = item.duration or prepared.duration
            prepared.thumbnail = item.thumbnail or prepared.thumbnail
            prepared.text = item.text or prepared.text
            prepared.author = item.author or prepared.author
            prepared.tweet_id = item.tweet_id or prepared.tweet_id
            prepared.alt_urls = list(item.alt_urls or [])
            prepared.source = item.source or prepared.source
            return prepared
    return prepared


def process_archive(page_url: str, out_dir: Path, info_only: bool, skip_existing: bool) -> int:
    prepared = prepare_archive_downloads(page_url, out_dir, skip_existing=skip_existing)
    if info_only:
        for item in prepared:
            say(
                f"{item.author}/{item.tweet_id}  {format_duration(item.duration)}  "
                f"{item.text.replace(chr(10), ' ')[:80]}"
            )
            say(f"  1. {item.fmt.label}  {item.fmt.url[:80]}")
        return 0
    errors = 0
    for item in prepared:
        if item.skipped:
            say(f"已存在，跳过  {item.dest.name}")
            continue
        say(f"↓ {item.author}  {item.tweet_id}  HLS  -> {item.dest.name}")
        try:
            item = refresh_archive_prepared(item)
            download_hls(item.fmt.url, item.dest, referer=item.source, alt_urls=item.alt_urls)
            size_mb = item.dest.stat().st_size / 1048576
            say(f"✓ {item.dest}  {size_mb:.1f} MB")
        except Exception as err:
            errors += 1
            eprint(f"✗ {page_url}: {err}")
    return errors


def set_eve568_cred_listener(fn: Optional[Callable[[dict], None]]) -> None:
    global _eve568_cred_listener
    _eve568_cred_listener = fn


def set_eve568_account(
    login_id: str = "",
    password: str = "",
    script: str = "",
) -> dict[str, str]:
    with _eve568_lock:
        if login_id:
            _eve568_account["login_id"] = login_id.strip()
        if password:
            _eve568_account["password"] = password
        if script:
            _eve568_account["script"] = script.strip()
        return dict(_eve568_account)


def eve568_account() -> dict[str, str]:
    with _eve568_lock:
        return dict(_eve568_account)


def set_eve568_creds(creds: dict[str, str]) -> dict[str, str]:
    global _eve568_jwt, _eve568_jwt_exp
    cleaned = {
        "u": (creds.get("u") or "").strip(),
        "n": (creds.get("n") or "").strip(),
        "s": (creds.get("s") or "").strip(),
        "origin": (creds.get("origin") or "").strip(),
    }
    with _eve568_lock:
        changed = any(cleaned[k] and cleaned[k] != _eve568_creds.get(k) for k in ("u", "n", "s"))
        for key, value in cleaned.items():
            if value:
                _eve568_creds[key] = value
        if changed:
            _eve568_jwt = ""
            _eve568_jwt_exp = 0.0
        snapshot = dict(_eve568_creds)
    listener = _eve568_cred_listener
    if listener and snapshot.get("u"):
        try:
            listener(snapshot)
        except Exception:
            pass
    return snapshot


def eve568_creds() -> dict[str, str]:
    with _eve568_lock:
        return dict(_eve568_creds)


def eve568_origin(page_url: str = "") -> str:
    creds = eve568_creds()
    if creds.get("origin"):
        return creds["origin"]
    parsed = urllib.parse.urlparse(page_url or "")
    host = parsed.netloc
    if host:
        return f"{parsed.scheme or 'http'}://{host}"
    return "http://pk.eve568.com"


def eve568_api(origin: str, path: str) -> str:
    return urllib.parse.urljoin(origin.rstrip("/") + "/", "api/v1/" + path.lstrip("/"))


def _jwt_exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        pad = "=" * (-len(payload) % 4)
        data = json.loads(__import__("base64").urlsafe_b64decode(payload + pad))
        return float(data.get("exp") or 0)
    except Exception:
        return 0.0


def http_request_json(
    url: str,
    method: str = "GET",
    payload: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 20.0,
):
    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if headers:
        req_headers.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as err:
        body = err.read() if err.fp else b""
        text = body.decode("utf-8", "replace")[:240]
        raise RuntimeError(f"HTTP {err.code}: {text or url}") from err
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as err:
        raise RuntimeError(f"接口不是 JSON: {url}") from err


def _eve568_auth_failed(err: Exception) -> bool:
    text = str(err)
    needles = (
        "HTTP 401",
        "HTTP 403",
        "HTTP 404",
        "HTTP 422",
        "HTTP 500",
        "HTTP 502",
        "userId",
        "秘文不存在",
        "certificate not found",
        "没有返回 token",
        "入口链接可能已过期",
        "需要门户登录",
    )
    return any(item in text for item in needles)


def refresh_eve568_portal(origin: str = "") -> dict[str, str]:
    """Call xh/get_video_url.py to mint a fresh u/n/s portal URL."""
    account = eve568_account()
    script = Path(account.get("script") or XH_GET_VIDEO_URL)
    if not script.is_file():
        raise RuntimeError(f"找不到 xh 入口脚本: {script}")
    cmd = [
        sys.executable,
        str(script),
        "--login-id",
        account.get("login_id") or XH_DEFAULT_LOGIN_ID,
        "--password",
        account.get("password") or XH_DEFAULT_PASSWORD,
        "--no-prompt",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    stdout = (proc.stdout or b"").decode("utf-8", "replace").strip()
    stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"刷新 eve568 入口失败: {stderr[-400:] or stdout[-400:] or proc.returncode}")
    url = ""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if "u=" in line and "n=" in line and "s=" in line:
            url = line
            break
    if not url:
        raise RuntimeError(f"xh 脚本没有返回带 u/n/s 的入口链接: {stdout[-300:]}")
    found = remember_eve568_creds_from_text(url)
    if origin and not found.get("origin"):
        found = set_eve568_creds({**found, "origin": origin})
    if not found.get("u"):
        raise RuntimeError("xh 脚本返回的入口链接解析不出 u/n/s")
    return found


def eve568_login(origin: str = "", creds: Optional[dict[str, str]] = None, refresh: bool = False) -> str:
    global _eve568_jwt, _eve568_jwt_exp
    origin = origin or eve568_origin()
    if refresh:
        refresh_eve568_portal(origin)
    data = creds or eve568_creds()
    u, n, s = data.get("u") or "", data.get("n") or "", data.get("s") or ""
    if not (u and n and s):
        refresh_eve568_portal(origin)
        data = eve568_creds()
        u, n, s = data.get("u") or "", data.get("n") or "", data.get("s") or ""
    if not (u and n and s):
        raise RuntimeError("eve568 需要门户登录参数，xh 脚本也没有拿到 u/n/s")
    try:
        payload = http_request_json(
            eve568_api(origin, "login"),
            method="POST",
            payload={"u": u, "n": n, "s": s},
            headers={"Origin": origin, "Referer": origin + "/"},
        )
    except Exception as err:
        if refresh or not _eve568_auth_failed(err):
            raise
        refresh_eve568_portal(origin)
        data = eve568_creds()
        payload = http_request_json(
            eve568_api(origin, "login"),
            method="POST",
            payload={"u": data.get("u"), "n": data.get("n"), "s": data.get("s")},
            headers={"Origin": origin, "Referer": origin + "/"},
        )
    token = ""
    if isinstance(payload, dict):
        token = str(payload.get("token") or "")
        inner = payload.get("data")
        if not token and isinstance(inner, dict):
            token = str(inner.get("token") or "")
    if not token:
        if refresh:
            raise RuntimeError("eve568 登录没有返回 token，入口链接可能已过期")
        return eve568_login(origin, refresh=True)
    with _eve568_lock:
        _eve568_jwt = token
        _eve568_jwt_exp = _jwt_exp(token)
    return token


def eve568_token(origin: str = "", force: bool = False, refresh_portal: bool = False) -> str:
    origin = origin or eve568_origin()
    with _eve568_lock:
        token = _eve568_jwt
        exp = _eve568_jwt_exp
    now = time.time()
    if not force and not refresh_portal and token and (not exp or exp - now >= 600):
        return token
    with _eve568_refresh_lock:
        with _eve568_lock:
            token = _eve568_jwt
            exp = _eve568_jwt_exp
        now = time.time()
        if not force and not refresh_portal and token and (not exp or exp - now >= 600):
            return token
        return eve568_login(origin, refresh=refresh_portal)


def fetch_eve568_detail(origin: str, video_id: str) -> dict:
    token = eve568_token(origin)
    headers = {
        "Origin": origin,
        "Referer": origin + "/",
        "Authorization": f"Bearer {token}",
    }
    try:
        data = http_request_json(eve568_api(origin, f"videos/id={video_id}"), headers=headers)
    except Exception as err:
        if not _eve568_auth_failed(err):
            raise
        token = eve568_token(origin, force=True, refresh_portal=True)
        headers["Authorization"] = f"Bearer {token}"
        data = http_request_json(eve568_api(origin, f"videos/id={video_id}"), headers=headers)
    if not isinstance(data, dict):
        raise RuntimeError("eve568 详情格式异常")
    detail = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(detail, dict) or not detail.get("videoPath"):
        raise RuntimeError("eve568 详情里没有播放地址")
    return detail


def join_eve568_media(cdn_url: str, path: str) -> str:
    path = (path or "").strip()
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    base = (cdn_url or "").rstrip("/")
    if not base:
        return path
    return base + "/" + path.lstrip("/")


def fetch_eve568_search_page(origin: str, kinds: list[str], queries: dict, page: int) -> dict:
    categories = []
    for kind in kinds:
        category = EVE568_CATEGORY_IDS.get(kind or "")
        if category and category not in categories:
            categories.append(category)
    if not categories:
        raise RuntimeError(f"不支持的 eve568 分类: {kinds}")
    token = eve568_token(origin)
    pairs = [("categoryId", item) for item in categories]
    pairs.append(("limit", str(EVE568_SEARCH_PAGE_SIZE)))
    pairs.append(("page", str(page)))
    for key in ("keyword", "actorName", "manufacturer"):
        value = (queries.get(key) or "").strip()
        if value:
            pairs.append((key, value))
    url = eve568_api(origin, "videos") + "?" + urllib.parse.urlencode(pairs)
    headers = {
        "Origin": origin,
        "Referer": origin + "/",
        "Authorization": f"Bearer {token}",
    }
    try:
        data = http_request_json(url, headers=headers)
    except Exception as err:
        if not _eve568_auth_failed(err):
            raise
        token = eve568_token(origin, force=True, refresh_portal=True)
        headers["Authorization"] = f"Bearer {token}"
        data = http_request_json(url, headers=headers)
    if not isinstance(data, dict):
        raise RuntimeError("eve568 搜索结果格式异常")
    return data


def expand_eve568_search_url(page_url: str) -> list[str]:
    m = EVE568_SEARCH_RE.search(page_url)
    if not m:
        raise RuntimeError("无法识别 eve568 搜索链接")
    host, kind = m.group(1), m.group(2)
    kinds = [kind] if kind else ["adult-long-video", "adult-short-video"]
    origin = eve568_origin(page_url)
    parsed = urllib.parse.urlparse(page_url if "://" in page_url else "http://" + page_url)
    query = {k: (v[0] if v else "") for k, v in urllib.parse.parse_qs(parsed.query).items()}
    if not any(query.get(k) for k in ("keyword", "actorName", "manufacturer")):
        raise RuntimeError("搜索链接缺少 keyword / actorName / manufacturer")
    first = fetch_eve568_search_page(origin, kinds, query, 1)
    items = first.get("data") if isinstance(first.get("data"), list) else []
    num_pages = int(first.get("numOfPages") or 1)
    for page in range(2, max(num_pages, 1) + 1):
        more = fetch_eve568_search_page(origin, kinds, query, page)
        extra = more.get("data") if isinstance(more.get("data"), list) else []
        items.extend(extra)
    urls: list[str] = []
    seen: set[str] = set()
    scheme = parsed.scheme or "http"
    for item in items:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("id") or "")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        item_kind = EVE568_CATEGORY_KINDS.get(str(item.get("categoryId") or ""), kind or "adult-long-video")
        urls.append(f"{scheme}://{host}/v/{item_kind}/play/{video_id}")
    if not urls:
        raise RuntimeError("搜索结果是空的")
    return urls


def expand_eve568_search_jobs(page_url: str) -> list[tuple[str, str]]:
    urls = expand_eve568_search_url(page_url)
    folder = eve568_search_folder_name(page_url) if is_eve568_global_search_url(page_url) else ""
    return [(url, folder) for url in urls]


def refresh_eve568_media_url(page_url: str) -> str:
    m = EVE568_PLAY_RE.search(page_url)
    if not m:
        raise RuntimeError("无法识别 eve568 播放链接")
    origin = eve568_origin(page_url)
    detail = fetch_eve568_detail(origin, m.group(3))
    return str(detail.get("videoPath") or "")


def prepare_eve568_downloads(
    page_url: str,
    out_dir: Path,
    skip_existing: bool = True,
) -> list[PreparedDownload]:
    m = EVE568_PLAY_RE.search(page_url)
    if not m:
        raise RuntimeError("无法识别 eve568 播放链接")
    host, kind, video_id = m.group(1), m.group(2), m.group(3)
    origin = eve568_origin(page_url)
    remember_eve568_creds_from_text(page_url)
    detail = fetch_eve568_detail(origin, video_id)
    media = str(detail.get("videoPath") or "")
    if not media:
        raise RuntimeError("eve568 详情里没有 videoPath")
    author = ""
    actors = detail.get("actorNames") or []
    if isinstance(actors, list) and actors:
        author = str(actors[0] or "")
    author = author or str(detail.get("manufacturer") or "eve568")
    title = str(detail.get("name") or video_id)
    thumb = str(detail.get("coverImage2") or detail.get("coverImage1") or "")
    duration = float(detail.get("watchTime") or detail.get("duration") or 0)
    fmt = VideoFormat(url=media, container="mp4" if not is_hls_url(media) else "m3u8")
    dest = out_dir / (safe_filename(f"{author}-{video_id}") + ".mp4")
    source = f"{origin}/v/{kind}/play/{video_id}"
    return [
        PreparedDownload(
            tweet_id=video_id,
            media_id=kind,
            author=author,
            text=str(detail.get("description") or title),
            duration=duration,
            thumbnail=thumb,
            fmt=fmt,
            dest=dest,
            source=source,
            skipped=bool(skip_existing and dest.exists() and dest.stat().st_size > 0),
        )
    ]


def process_eve568(page_url: str, out_dir: Path, info_only: bool, skip_existing: bool) -> int:
    prepared = prepare_eve568_downloads(page_url, out_dir, skip_existing=skip_existing)
    if info_only:
        for item in prepared:
            say(
                f"{item.author}/{item.tweet_id}  {format_duration(item.duration)}  "
                f"{item.text.replace(chr(10), ' ')[:80]}"
            )
            say(f"  1. {item.fmt.label}  {item.fmt.url[:80]}")
        return 0
    errors = 0
    for item in prepared:
        if item.skipped:
            say(f"已存在，跳过  {item.dest.name}")
            continue
        say(f"↓ {item.author}  {item.tweet_id}  {item.fmt.label}  -> {item.dest.name}")
        try:
            download_with_urllib(
                item.fmt.url,
                item.dest,
                referer=item.source,
                refresh_url=lambda src=item.source: refresh_eve568_media_url(src),
            )
            size_mb = item.dest.stat().st_size / 1048576
            say(f"✓ {item.dest}  {size_mb:.1f} MB")
        except Exception as err:
            errors += 1
            eprint(f"✗ {page_url}: {err}")
    return errors


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
        description="从 X/Twitter、黑料网、海角网 archives 或 eve568 播放页下载视频。",
    )
    p.add_argument("urls", nargs="*", help="推文链接、数字 ID、/archives/ 或 eve568 播放链接，可多个")
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
        eprint("      xdl.py https://example.com/archives/115738/")
        eprint("      xdl.py --proxy http://127.0.0.1:7890 https://www.hjw01.com/archives/193648/")
        eprint("      xdl.py 'http://pk.eve568.com/v?u=...&n=...&s=...' http://pk.eve568.com/v/adult-long-video/play/ID")
        eprint("界面:  xdl.py --serve")
        return 2

    out_dir = Path(args.output).expanduser()
    if args.json:
        return dump_json(items, args.quality)

    errors = 0
    for item in items:
        try:
            if item.startswith("http") and is_eve568_search_url(item):
                for play_url, subdir in expand_eve568_search_jobs(item):
                    dest_dir = out_dir / subdir if subdir else out_dir
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    errors += process_eve568(
                        play_url,
                        out_dir=dest_dir,
                        info_only=args.info,
                        skip_existing=not args.no_skip,
                    )
                continue
            if item.startswith("http") and is_eve568_play_url(item):
                errors += process_eve568(
                    item,
                    out_dir=out_dir,
                    info_only=args.info,
                    skip_existing=not args.no_skip,
                )
                continue
            if item.startswith("http") and is_archives_url(item):
                errors += process_archive(
                    item,
                    out_dir=out_dir,
                    info_only=args.info,
                    skip_existing=not args.no_skip,
                )
                continue
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
