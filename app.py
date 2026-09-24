#!/usr/bin/env python3
"""Local visual UI for xdl: queue, live progress, library, playback."""

from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import xdl

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web"
DEFAULT_OUT = ROOT / "downloads"
LIBRARY_NAME = ".xdl-library.json"
SETTINGS_NAME = ".xdl-settings.json"
THUMBS = ".thumbs"

lock = threading.Lock()
jobs: dict[str, dict[str, Any]] = {}
job_order: list[str] = []
subscribers: list[Queue] = []
settings = {
    "out_dir": str(DEFAULT_OUT),
    "quality": "best",
    "proxy": "system",
    "eve568_u": "",
    "eve568_n": "",
    "eve568_s": "",
    "eve568_origin": "",
    "xh_login_id": "a10006",
    "xh_password": "Aa11221122",
}
worker_busy = False
stop_worker = threading.Event()


def log(msg: str) -> None:
    print(msg, flush=True)


def out_dir() -> Path:
    path = Path(settings["out_dir"]).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    (path / THUMBS).mkdir(parents=True, exist_ok=True)
    return path


def library_path() -> Path:
    return out_dir() / LIBRARY_NAME


def thumbs_dir() -> Path:
    path = out_dir() / THUMBS
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return ROOT / SETTINGS_NAME


def public_settings() -> dict[str, Any]:
    proxy = xdl.proxy_status()
    return {
        "out_dir": settings["out_dir"],
        "quality": settings["quality"],
        "proxy": settings["proxy"],
        "proxy_mode": proxy["mode"],
        "proxy_env": proxy["env"],
        "proxy_label": proxy["label"],
        "eve568_u": settings.get("eve568_u") or "",
        "eve568_n": settings.get("eve568_n") or "",
        "eve568_s": settings.get("eve568_s") or "",
        "eve568_origin": settings.get("eve568_origin") or "",
        "xh_login_id": settings.get("xh_login_id") or "",
    }


def persist_settings() -> None:
    payload = {
        "out_dir": settings["out_dir"],
        "quality": settings["quality"],
        "proxy": settings["proxy"],
        "eve568_u": settings.get("eve568_u") or "",
        "eve568_n": settings.get("eve568_n") or "",
        "eve568_s": settings.get("eve568_s") or "",
        "eve568_origin": settings.get("eve568_origin") or "",
        "xh_login_id": settings.get("xh_login_id") or "",
        "xh_password": settings.get("xh_password") or "",
    }
    settings_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _persist_eve568_creds(creds: dict) -> None:
    if creds.get("u"):
        settings["eve568_u"] = str(creds.get("u") or "")
        settings["eve568_n"] = str(creds.get("n") or "")
        settings["eve568_s"] = str(creds.get("s") or "")
        if creds.get("origin"):
            settings["eve568_origin"] = str(creds.get("origin") or "")
        persist_settings()


def apply_proxy(value: str) -> dict[str, Any]:
    status = xdl.set_proxy(value)
    settings["proxy"] = "direct" if status["mode"] == "direct" else (status["url"] if status["mode"] == "explicit" else "system")
    persist_settings()
    return public_settings()


def load_persisted_settings(cli_out: Optional[str] = None, cli_proxy: Optional[str] = None) -> None:
    path = settings_path()
    saved: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                saved = data
        except (OSError, json.JSONDecodeError):
            saved = {}
    if saved.get("quality"):
        settings["quality"] = str(saved["quality"])
    for key in ("eve568_u", "eve568_n", "eve568_s", "eve568_origin", "xh_login_id", "xh_password"):
        if saved.get(key):
            settings[key] = str(saved[key])
    xdl.set_eve568_account(
        login_id=settings.get("xh_login_id") or "",
        password=settings.get("xh_password") or "",
    )
    xdl.set_eve568_cred_listener(_persist_eve568_creds)
    if settings.get("eve568_u") and settings.get("eve568_n") and settings.get("eve568_s"):
        xdl.set_eve568_creds(
            {
                "u": settings.get("eve568_u") or "",
                "n": settings.get("eve568_n") or "",
                "s": settings.get("eve568_s") or "",
                "origin": settings.get("eve568_origin") or "",
            }
        )
    if cli_out:
        settings["out_dir"] = str(Path(cli_out).expanduser())
    elif saved.get("out_dir"):
        settings["out_dir"] = str(Path(saved["out_dir"]).expanduser())
    proxy_value = cli_proxy if cli_proxy and cli_proxy != "system" else saved.get("proxy") or cli_proxy or "system"
    try:
        apply_proxy(str(proxy_value))
    except (ValueError, RuntimeError) as err:
        log(f"代理设置无效，改回系统环境: {err}")
        apply_proxy("system")


def load_library() -> list[dict[str, Any]]:
    path = library_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_library(items: list[dict[str, Any]]) -> None:
    library_path().write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def upsert_library(item: dict[str, Any]) -> dict[str, Any]:
    items = load_library()
    key = item.get("filename") or item.get("id")
    items = [x for x in items if (x.get("filename") or x.get("id")) != key]
    items.insert(0, item)
    save_library(items)
    return item


def remove_library(filename: str) -> None:
    items = [x for x in load_library() if x.get("filename") != filename]
    save_library(items)


def guess_from_filename(path: Path) -> dict[str, Any]:
    stem = path.stem
    author = ""
    tweet_id = ""
    quality = ""
    parts = stem.split("-")
    if len(parts) >= 3 and parts[-1].endswith("p") and parts[-1][:-1].isdigit():
        quality = parts[-1]
        tweet_id = parts[-2] if parts[-2].isdigit() else ""
        author = "-".join(parts[:-2]) if tweet_id else parts[0]
    elif len(parts) >= 2 and parts[-1].isdigit():
        tweet_id = parts[-1]
        author = "-".join(parts[:-1])
    stat = path.stat()
    return {
        "id": stem,
        "filename": path.name,
        "author": author or "unknown",
        "tweet_id": tweet_id,
        "text": "",
        "duration": 0,
        "quality": quality or "video",
        "width": 0,
        "height": int(quality[:-1]) if quality.endswith("p") and quality[:-1].isdigit() else 0,
        "bitrate": 0,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "url": f"https://x.com/{author}/status/{tweet_id}" if author and tweet_id else "",
        "thumb": f"/thumb/{stem}.jpg" if (thumbs_dir() / f"{stem}.jpg").exists() else "",
    }


def scan_library() -> list[dict[str, Any]]:
    folder = out_dir()
    saved = {item.get("filename"): item for item in load_library() if item.get("filename")}
    found: list[dict[str, Any]] = []
    seen = set()
    videos = list(folder.glob("*.mp4"))
    videos.extend(folder.glob("*/*.mp4"))
    for path in sorted(videos, key=lambda p: p.stat().st_mtime, reverse=True):
        if path.name.startswith("."):
            continue
        rel = library_relpath(path)
        if rel in seen:
            continue
        seen.add(rel)
        item = dict(saved.get(rel) or saved.get(path.name) or guess_from_filename(path))
        stat = path.stat()
        item["filename"] = rel
        item["id"] = path.stem
        item["size"] = stat.st_size
        item["mtime"] = stat.st_mtime
        thumb = thumbs_dir() / f"{path.stem}.jpg"
        if thumb.exists():
            item["thumb"] = f"/thumb/{path.stem}.jpg"
        else:
            item["thumb"] = extract_thumb(path) or item.get("thumb") or ""
        found.append(item)
    save_library(found)
    return found


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    data = dict(job)
    data.pop("cancel", None)
    return data


def snapshot() -> dict[str, Any]:
    with lock:
        job_list = [public_job(jobs[i]) for i in job_order if i in jobs]
    return {
        "settings": public_settings(),
        "jobs": job_list,
        "library": scan_library(),
    }


def publish(event: dict[str, Any]) -> None:
    payload = json.dumps(event, ensure_ascii=False)
    dead = []
    with lock:
        targets = list(subscribers)
    for q in targets:
        try:
            q.put_nowait(payload)
        except Exception:
            dead.append(q)
    if dead:
        with lock:
            for q in dead:
                if q in subscribers:
                    subscribers.remove(q)


def emit_job(job: dict[str, Any]) -> None:
    publish({"type": "job", "job": public_job(job)})


def library_relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(out_dir().resolve())).replace("\\", "/")
    except ValueError:
        return path.name


def media_path(rel: str) -> Optional[Path]:
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in Path(rel).parts:
        return None
    path = (out_dir() / rel).resolve()
    try:
        path.relative_to(out_dir().resolve())
    except ValueError:
        return None
    return path


def new_job(source: str, quality: str, subdir: str = "") -> dict[str, Any]:
    job = {
        "id": uuid.uuid4().hex[:12],
        "source": source,
        "quality": quality,
        "subdir": subdir,
        "status": "queued",
        "stage": "等待中",
        "author": "",
        "tweet_id": "",
        "text": "",
        "filename": "",
        "thumb": "",
        "percent": 0,
        "downloaded": 0,
        "total": 0,
        "speed": 0,
        "eta": 0,
        "error": "",
        "created": time.time(),
        "updated": time.time(),
        "cancel": False,
    }
    with lock:
        jobs[job["id"]] = job
        job_order.insert(0, job["id"])
    emit_job(job)
    return job


def retry_failed_jobs(job_ids: Optional[list[str]] = None) -> list[dict[str, Any]]:
    with lock:
        candidates = []
        if job_ids:
            wanted = {str(item) for item in job_ids if item}
            for job_id in job_order:
                job = jobs.get(job_id)
                if job and job_id in wanted and job.get("status") == "error":
                    candidates.append(dict(job))
        else:
            for job_id in job_order:
                job = jobs.get(job_id)
                if job and job.get("status") == "error":
                    candidates.append(dict(job))
    created: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for job in candidates:
        source = (job.get("source") or "").strip()
        if not source:
            continue
        quality = (job.get("quality") or settings.get("quality") or "best").strip()
        if quality in {"HLS", "video"} or quality.endswith("p"):
            quality = settings.get("quality") or "best"
        subdir = (job.get("subdir") or "").strip()
        key = (source, subdir)
        if key in seen:
            continue
        seen.add(key)
        created.append(new_job(source, quality, subdir=subdir))
    return created


def update_job(job_id: str, **fields: Any) -> dict[str, Any]:
    with lock:
        job = jobs.get(job_id)
        if not job:
            return {}
        job.update(fields)
        job["updated"] = time.time()
        snapshot_job = dict(job)
    emit_job(snapshot_job)
    return snapshot_job


def job_cancelled(job_id: str) -> bool:
    with lock:
        job = jobs.get(job_id)
        return bool(job and job.get("cancel"))


def probe_media(video_path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not video_path.exists():
        return {}
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    stream = (data.get("streams") or [{}])[0]
    duration = 0.0
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": duration,
    }


def extract_thumb(video_path: Path) -> str:
    dest = thumbs_dir() / f"{video_path.stem}.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        return f"/thumb/{video_path.stem}.jpg"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not video_path.exists():
        return ""
    tmp = dest.with_suffix(".tmp.jpg")
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-ss",
            "1",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=480:-2",
            str(tmp),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(dest)
        return f"/thumb/{video_path.stem}.jpg"
    if tmp.exists():
        tmp.unlink()
    return ""


def ensure_thumb(prepared: xdl.PreparedDownload) -> str:
    dest = thumbs_dir() / f"{prepared.dest.stem}.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        return f"/thumb/{prepared.dest.stem}.jpg"
    if prepared.thumbnail and xdl.save_url_to_file(prepared.thumbnail, dest):
        return f"/thumb/{prepared.dest.stem}.jpg"
    return extract_thumb(prepared.dest)


def finish_item(job_id: str, prepared: xdl.PreparedDownload, skipped: bool, final: bool = True) -> None:
    size = prepared.dest.stat().st_size if prepared.dest.exists() else 0
    probed = probe_media(prepared.dest) if prepared.dest.exists() else {}
    if probed.get("height") and not prepared.fmt.height:
        prepared.fmt.height = int(probed["height"])
        prepared.fmt.width = int(probed.get("width") or prepared.fmt.width or 0)
    if probed.get("duration") and not prepared.duration:
        prepared.duration = float(probed["duration"])
    thumb = ensure_thumb(prepared)
    meta = prepared.to_meta(size)
    meta["filename"] = library_relpath(prepared.dest)
    meta["mtime"] = prepared.dest.stat().st_mtime if prepared.dest.exists() else time.time()
    meta["thumb"] = thumb
    upsert_library(meta)
    fields = {
        "author": prepared.author,
        "tweet_id": prepared.tweet_id,
        "text": prepared.text,
        "filename": library_relpath(prepared.dest),
        "thumb": thumb,
        "duration": prepared.duration,
        "quality": meta["quality"],
        "percent": 100,
        "downloaded": size,
        "total": size,
        "speed": 0,
        "eta": 0,
    }
    if final:
        fields["status"] = "skipped" if skipped else "done"
        fields["stage"] = "已跳过" if skipped else "已完成"
    update_job(job_id, **fields)
    publish({"type": "library"})


def run_one(job: dict[str, Any]) -> None:
    job_id = job["id"]
    source = job["source"]
    quality = job["quality"]
    folder = out_dir()
    subdir = (job.get("subdir") or "").strip()
    if subdir:
        folder = folder / Path(subdir).name
        folder.mkdir(parents=True, exist_ok=True)
    update_job(job_id, status="resolving", stage="解析中", percent=0)
    try:
        if source.startswith("http") and xdl.is_eve568_play_url(source):
            prepared_list = xdl.prepare_eve568_downloads(source, folder, skip_existing=True)
        elif source.startswith("http") and xdl.is_archives_url(source):
            prepared_list = xdl.prepare_archive_downloads(source, folder, skip_existing=True)
        elif source.startswith("http") and "video.twimg.com" in source:
            prepared_list = [xdl.prepare_cdn_download(source, folder, skip_existing=True)]
        else:
            prepared_list = xdl.prepare_tweet_downloads(source, folder, quality, skip_existing=True)
    except Exception as err:
        update_job(job_id, status="error", stage="失败", error=str(err))
        return

    if not prepared_list:
        update_job(job_id, status="error", stage="失败", error="这条帖没有视频")
        return

    primary = prepared_list[0]
    update_job(
        job_id,
        author=primary.author,
        tweet_id=primary.tweet_id,
        text=primary.text,
        filename=primary.dest.name,
        duration=primary.duration,
        quality=f"{primary.fmt.height}p" if primary.fmt.height else ("HLS" if xdl.is_hls_url(primary.fmt.url) else quality),
    )

    total_items = len(prepared_list)
    item_errors: list[str] = []
    for index, prepared in enumerate(prepared_list, 1):
        if job_cancelled(job_id):
            update_job(job_id, status="cancelled", stage="已取消")
            return
        stage = f"下载中 {index}/{total_items}" if total_items > 1 else "下载中"
        update_job(
            job_id,
            filename=prepared.dest.name,
            duration=prepared.duration,
            quality=f"{prepared.fmt.height}p" if prepared.fmt.height else ("HLS" if xdl.is_hls_url(prepared.fmt.url) else quality),
            status="downloading",
            stage="已跳过" if prepared.skipped else stage,
            percent=0,
        )
        if prepared.skipped:
            finish_item(job_id, prepared, skipped=True, final=(index == total_items))
            continue
        if xdl.is_archives_url(prepared.source):
            update_job(job_id, stage="刷新播放地址")
            prepared = xdl.refresh_archive_prepared(prepared)
        if xdl.is_eve568_play_url(prepared.source):
            update_job(job_id, stage="刷新播放地址")
            prepared.fmt.url = xdl.refresh_eve568_media_url(prepared.source)
        last = {"t": 0.0}

        def on_progress(downloaded: int, total: int, speed: float, _last=last) -> None:
            now = time.time()
            if now - _last["t"] < 0.15 and downloaded != total:
                return
            _last["t"] = now
            eta = ((total - downloaded) / speed) if speed and total > downloaded else 0
            percent = int(downloaded * 100 / total) if total else 0
            update_job(
                job_id,
                downloaded=downloaded,
                total=total,
                speed=speed,
                eta=eta,
                percent=percent,
                status="downloading",
                stage=stage,
            )

        try:
            if xdl.is_eve568_play_url(prepared.source):
                xdl.download_with_urllib(
                    prepared.fmt.url,
                    prepared.dest,
                    on_progress=on_progress,
                    should_cancel=lambda: job_cancelled(job_id),
                    referer=prepared.source,
                    refresh_url=lambda src=prepared.source: xdl.refresh_eve568_media_url(src),
                )
            else:
                xdl.download_url(
                    prepared.fmt.url,
                    prepared.dest,
                    on_progress=on_progress,
                    should_cancel=lambda: job_cancelled(job_id),
                    prefer_urllib=True,
                    referer=prepared.source if xdl.is_archives_url(prepared.source) else "",
                    on_stage=lambda s: update_job(job_id, stage=s, status="downloading"),
                    alt_urls=getattr(prepared, "alt_urls", None),
                )
            finish_item(job_id, prepared, skipped=False, final=(index == total_items))
        except xdl.Cancelled:
            if prepared.dest.exists():
                try:
                    prepared.dest.unlink()
                except OSError:
                    pass
            update_job(job_id, status="cancelled", stage="已取消")
            return
        except Exception as err:
            item_errors.append(f"{prepared.dest.name}: {err}")
            if index < total_items:
                update_job(job_id, error=str(err), stage=f"第 {index} 段失败，继续下一段")
                continue
            break
    if item_errors:
        done_ok = total_items - len(item_errors)
        if done_ok > 0:
            update_job(
                job_id,
                status="error",
                stage=f"完成 {done_ok}/{total_items} 段",
                error="；".join(item_errors),
            )
        else:
            update_job(job_id, status="error", stage="失败", error="；".join(item_errors))


def worker_loop() -> None:
    global worker_busy
    while not stop_worker.is_set():
        nxt = None
        with lock:
            for job_id in reversed(job_order):
                job = jobs.get(job_id)
                if job and job["status"] == "queued":
                    nxt = dict(job)
                    job["status"] = "resolving"
                    break
        if not nxt:
            worker_busy = False
            time.sleep(0.15)
            continue
        worker_busy = True
        run_one(nxt)


def enqueue(text: str, quality: str) -> list[dict[str, Any]]:
    items = xdl.extract_all_inputs(text)
    expanded: list[str] = []
    seen: set[str] = set()
    for item in items:
        batch: list[tuple[str, str]] = [(item, "")]
        if item.startswith("http") and xdl.is_eve568_search_url(item):
            batch = xdl.expand_eve568_search_jobs(item)
        for url, subdir in batch:
            if url not in seen:
                seen.add(url)
                expanded.append((url, subdir))
    if not expanded:
        raise ValueError("没有识别到推文链接、ID、archives、eve568 链接或搜索关键词")
    created = [new_job(url, quality, subdir=subdir) for url, subdir in expanded]
    return created


def pick_folder() -> Optional[str]:
    if sys.platform != "darwin":
        return None
    proc = subprocess.run(
        [
            "osascript",
            "-e",
            'POSIX path of (choose folder with prompt "选择视频保存目录")',
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    path = proc.stdout.strip()
    return path or None


def reveal_in_finder(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(path)], check=False)
    else:
        subprocess.run(["open", str(path.parent)], check=False)


def json_bytes(data: Any, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:  # noqa: ARG002
        err = sys.exc_info()[1]
        if isinstance(err, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "xdl/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        if "/api/events" in str(args[0] if args else ""):
            return
        log("%s - %s" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in {"/", "/index.html"}:
            return self._send_file(STATIC / "index.html", "text/html; charset=utf-8")
        if path.startswith("/web/"):
            return self._send_file(STATIC / path[len("/web/") :], None)
        if path in {"/app.js", "/style.css"}:
            return self._send_file(STATIC / path.lstrip("/"), None)
        if path == "/api/state":
            status, body, ctype = json_bytes(snapshot())
            return self._send(status, body, ctype)
        if path == "/api/events":
            return self._sse()
        if path.startswith("/media/"):
            return self._send_video(path[len("/media/") :])
        if path.startswith("/thumb/"):
            return self._send_thumb(path[len("/thumb/") :])
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        data = read_json(self)
        try:
            if path == "/api/download":
                quality = (data.get("quality") or settings["quality"] or "best").strip()
                text = data.get("text") or ""
                found = xdl.remember_eve568_creds_from_text(text)
                if found.get("u"):
                    _persist_eve568_creds(found)
                created = enqueue(text, quality)
                status, body, ctype = json_bytes({"ok": True, "jobs": created})
                return self._send(status, body, ctype)
            if path == "/api/cancel":
                job_id = data.get("id") or ""
                with lock:
                    job = jobs.get(job_id)
                    if job:
                        job["cancel"] = True
                        if job["status"] == "queued":
                            job["status"] = "cancelled"
                            job["stage"] = "已取消"
                            snap = dict(job)
                        else:
                            snap = dict(job)
                    else:
                        snap = None
                if snap:
                    emit_job(snap)
                status, body, ctype = json_bytes({"ok": bool(snap)})
                return self._send(status, body, ctype)
            if path == "/api/settings":
                if data.get("out_dir"):
                    settings["out_dir"] = str(Path(data["out_dir"]).expanduser())
                    out_dir()
                if data.get("quality"):
                    settings["quality"] = str(data["quality"])
                if "proxy" in data:
                    apply_proxy(str(data.get("proxy") or "system"))
                for key in ("eve568_u", "eve568_n", "eve568_s", "eve568_origin", "xh_login_id", "xh_password"):
                    if key in data:
                        settings[key] = str(data.get(key) or "")
                xdl.set_eve568_account(
                    login_id=settings.get("xh_login_id") or "",
                    password=settings.get("xh_password") or "",
                )
                if settings.get("eve568_u") and settings.get("eve568_n") and settings.get("eve568_s"):
                    xdl.set_eve568_creds(
                        {
                            "u": settings.get("eve568_u") or "",
                            "n": settings.get("eve568_n") or "",
                            "s": settings.get("eve568_s") or "",
                            "origin": settings.get("eve568_origin") or "",
                        }
                    )
                persist_settings()
                status, body, ctype = json_bytes({"ok": True, "settings": public_settings()})
                return self._send(status, body, ctype)
            if path == "/api/proxy-test":
                if "proxy" in data:
                    apply_proxy(str(data.get("proxy") or "system"))
                result = xdl.test_proxy()
                status, body, ctype = json_bytes({"ok": True, "settings": public_settings(), "test": result})
                return self._send(status, body, ctype)
            if path == "/api/pick-dir":
                picked = pick_folder()
                if picked:
                    settings["out_dir"] = picked.rstrip("/")
                    out_dir()
                    persist_settings()
                status, body, ctype = json_bytes({"ok": bool(picked), "out_dir": settings["out_dir"]})
                return self._send(status, body, ctype)
            if path == "/api/open":
                filename = data.get("filename") or ""
                folder = data.get("folder")
                if folder:
                    reveal_in_finder(out_dir())
                elif filename:
                    target = media_path(filename) or (out_dir() / Path(filename).name)
                    reveal_in_finder(target)
                else:
                    reveal_in_finder(out_dir())
                status, body, ctype = json_bytes({"ok": True})
                return self._send(status, body, ctype)
            if path == "/api/delete":
                filename = (data.get("filename") or "").replace("\\", "/").lstrip("/")
                target = media_path(filename)
                if filename and target and target.exists():
                    target.unlink()
                thumb = thumbs_dir() / f"{Path(filename).stem}.jpg"
                if thumb.exists():
                    thumb.unlink()
                remove_library(filename)
                publish({"type": "library"})
                status, body, ctype = json_bytes({"ok": True})
                return self._send(status, body, ctype)
            if path == "/api/retry-failed":
                ids = data.get("ids") if isinstance(data.get("ids"), list) else None
                if data.get("id") and not ids:
                    ids = [data.get("id")]
                created = retry_failed_jobs(ids)
                status, body, ctype = json_bytes({"ok": True, "jobs": created, "count": len(created)})
                return self._send(status, body, ctype)
            if path == "/api/clear-done":
                with lock:
                    keep_ids = [
                        i
                        for i in job_order
                        if jobs.get(i, {}).get("status") in {"queued", "resolving", "downloading"}
                    ]
                    keep = set(keep_ids)
                    for i in list(jobs):
                        if i not in keep:
                            jobs.pop(i, None)
                    job_order[:] = keep_ids
                publish({"type": "jobs-cleared"})
                status, body, ctype = json_bytes({"ok": True})
                return self._send(status, body, ctype)
        except ValueError as err:
            status, body, ctype = json_bytes({"ok": False, "error": str(err)}, 400)
            return self._send(status, body, ctype)
        except Exception as err:
            status, body, ctype = json_bytes({"ok": False, "error": str(err)}, 500)
            return self._send(status, body, ctype)
        self._send(404, b"not found", "text/plain")

    def _sse(self) -> None:
        q: Queue = Queue(maxsize=200)
        with lock:
            subscribers.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        hello = json.dumps({"type": "hello", "state": snapshot()}, ensure_ascii=False)
        try:
            self.wfile.write(f"data: {hello}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=15)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with lock:
                if q in subscribers:
                    subscribers.remove(q)

    def _send_file(self, path: Path, content_type: Optional[str]) -> None:
        path = path.resolve()
        try:
            path.relative_to(STATIC.resolve())
        except ValueError:
            self._send(403, b"forbidden", "text/plain")
            return
        if not path.exists() or not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype)

    def _send_thumb(self, name: str) -> None:
        name = Path(unquote(name)).name
        path = thumbs_dir() / name
        if not path.exists():
            self._send(404, b"", "image/jpeg")
            return
        self._send(200, path.read_bytes(), "image/jpeg")

    def _send_video(self, name: str) -> None:
        rel = unquote(name).replace("\\", "/").lstrip("/")
        path = media_path(rel)
        if not path or not path.exists() or not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        size = path.stat().st_size
        ctype = "video/mp4"
        range_header = self.headers.get("Range")
        if not range_header:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            try:
                with open(path, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                return
            return
        unit, _, spec = range_header.partition("=")
        if unit != "bytes" or not spec:
            self._send(416, b"", "text/plain")
            return
        start_s, _, end_s = spec.partition("-")
        try:
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else size - 1
        except ValueError:
            self._send(416, b"", "text/plain")
            return
        end = min(end, size - 1)
        if start > end or start < 0:
            self._send(416, b"", "text/plain")
            return
        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1024 * 256, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return


def serve(port: int = 8787, out_dir_value: str = "downloads", proxy: Optional[str] = None) -> int:
    cli_out = None if out_dir_value in {"downloads", str(DEFAULT_OUT)} else str(Path(out_dir_value).expanduser())
    load_persisted_settings(cli_out=cli_out, cli_proxy=proxy)
    out_dir()
    persist_settings()
    scan_library()
    worker = threading.Thread(target=worker_loop, name="xdl-worker", daemon=True)
    worker.start()

    httpd = None
    bound = port
    last_err: Optional[Exception] = None
    for candidate in range(port, port + 20):
        try:
            httpd = Server(("127.0.0.1", candidate), Handler)
            bound = candidate
            break
        except OSError as err:
            last_err = err
            httpd = None
    if httpd is None:
        raise RuntimeError(f"无法绑定端口 {port}: {last_err}")

    url = f"http://127.0.0.1:{bound}"
    log(f"视频下载器    {url}")
    log(f"保存目录      {out_dir()}")
    log(f"代理          {xdl.proxy_status()['label']}")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("已关闭")
    finally:
        stop_worker.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    serve()
