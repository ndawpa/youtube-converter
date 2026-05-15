import os
import shutil
import subprocess
import threading
import uuid
import zipfile
import asyncio
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="YouTube Converter")

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

STATIC_DIR = Path("static")
COOKIES_FILE = Path(os.environ.get("COOKIES_PATH", "cookies.txt"))
COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)

NODE = which("node") or ""

AUTH_COOKIES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
    "__Secure-3PSID", "__Secure-3PAPISID", "__Secure-1PSID",
}

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

FORMAT_OPTIONS = {
    "mp4": {"format": "bestvideo+bestaudio/best", "merge_output_format": "mp4", "ext": "mp4"},
    "mp3": {"format": "bestaudio/best", "ext": "mp3",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]},
}


class ConvertRequest(BaseModel):
    url: str
    format: str
    quality: str = "best"
    filename: str = ""
    album: str = ""


@dataclass
class _PlaylistJob:
    status: str = "pending"   # pending | running | done | error
    total: int = 0
    downloaded: int = 0
    current: str = ""
    playlist_title: str = "playlist"
    playlist_uploader: str = ""
    zip_path: Path | None = None
    error: str | None = None


_playlist_jobs: dict[str, _PlaylistJob] = {}


def cookies_are_authenticated() -> bool:
    if not COOKIES_FILE.exists():
        return False
    for line in COOKIES_FILE.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) == 7 and parts[5] in AUTH_COOKIES:
            return True
    return False


def base_ydl_opts() -> dict:
    has_cookies = cookies_are_authenticated()
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android", "web"] if has_cookies else ["ios", "android"]
            }
        },
        **({"js_runtimes": {"node": {"path": NODE}}, "remote_components": {"ejs:github"}} if NODE else {}),
    }
    if has_cookies:
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def build_ydl_opts(fmt: str, quality: str, output_path: str) -> dict:
    opts = {**base_ydl_opts(), **FORMAT_OPTIONS[fmt]}
    if fmt == "mp4":
        if quality == "1080p":
            opts["format"] = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
        elif quality == "720p":
            opts["format"] = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        elif quality == "480p":
            opts["format"] = "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
        elif quality == "360p":
            opts["format"] = "bestvideo[height<=360]+bestaudio/best[height<=360]/best"
    opts["outtmpl"] = output_path
    return opts


def _embed_album(path: Path, album: str, artist: str = ""):
    tmp = path.with_suffix(".tmp" + path.suffix)
    codec = ["-codec:a", "copy"] if path.suffix == ".mp3" else ["-codec", "copy"]
    metadata = ["-metadata", f"album={album}"]
    if artist:
        metadata += ["-metadata", f"artist={artist}"]
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(path)] + metadata + codec + [str(tmp)],
        check=True, capture_output=True,
    )
    tmp.replace(path)


def delete_file_later(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _run_playlist(job_id: str, url: str, fmt: str, quality: str, album: str):
    job = _playlist_jobs[job_id]
    job.status = "running"
    work_dir = DOWNLOADS_DIR / job_id
    work_dir.mkdir(exist_ok=True)

    # Quick metadata pass to get playlist title and track count
    try:
        with yt_dlp.YoutubeDL({**base_ydl_opts(), "extract_flat": True, "quiet": True}) as ydl:
            meta = ydl.extract_info(url, download=False)
            if meta:
                job.playlist_title = meta.get("title") or "playlist"
                job.playlist_uploader = (
                    meta.get("uploader") or meta.get("channel") or
                    meta.get("uploader_id") or ""
                )
                entries = list(meta.get("entries") or [])
                if entries:
                    job.total = len(entries)
    except Exception:
        pass

    def hook(d: dict):
        info = d.get("info_dict") or {}
        if job.total == 0 and info.get("playlist_count"):
            job.total = info["playlist_count"]
        if not job.playlist_uploader:
            job.playlist_uploader = (
                info.get("playlist_uploader") or info.get("playlist_channel") or
                info.get("uploader") or info.get("channel") or ""
            )
        if d.get("status") == "downloading":
            job.current = info.get("title", "")
        elif d.get("status") == "finished":
            job.downloaded += 1
            job.current = info.get("title", "")

    opts = {
        **base_ydl_opts(),
        **FORMAT_OPTIONS[fmt],
        "outtmpl": str(work_dir / "%(playlist_index)03d - %(title)s.%(ext)s"),
        "progress_hooks": [hook],
        "ignoreerrors": True,
    }
    if fmt == "mp4":
        if quality == "1080p":
            opts["format"] = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
        elif quality == "720p":
            opts["format"] = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        elif quality == "480p":
            opts["format"] = "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
        elif quality == "360p":
            opts["format"] = "bestvideo[height<=360]+bestaudio/best[height<=360]/best"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        shutil.rmtree(work_dir, ignore_errors=True)
        return

    embed_album = job.playlist_title or album or "playlist"
    for f in work_dir.iterdir():
        try:
            _embed_album(f, embed_album, job.playlist_uploader)
        except Exception:
            pass

    safe_title = job.playlist_title.replace("/", "").replace("\\", "").replace("\0", "") or "playlist"
    zip_path = DOWNLOADS_DIR / f"{job_id}.zip"
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(work_dir.iterdir()):
            zf.write(f, f.name)

    shutil.rmtree(work_dir, ignore_errors=True)
    job.zip_path = zip_path
    job.status = "done"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content=(STATIC_DIR / "index.html").read_text())


@app.get("/cookies-status")
async def cookies_status():
    return {"has_cookies": cookies_are_authenticated()}


@app.post("/upload-cookies")
async def upload_cookies(file: UploadFile = File(...)):
    content = await file.read()
    COOKIES_FILE.write_bytes(content)
    return {"ok": True, "authenticated": cookies_are_authenticated()}


@app.post("/info")
async def get_video_info(req: ConvertRequest):
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _extract_info, req.url)
        return {
            "title": info.get("title", "Unknown"),
            "duration": info.get("duration", 0),
            "thumbnail": info.get("thumbnail", ""),
            "uploader": info.get("uploader", "Unknown"),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch video info: {str(e)}")


@app.post("/convert")
async def convert_video(req: ConvertRequest, background_tasks: BackgroundTasks):
    if req.format not in FORMAT_OPTIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {req.format}")
    job_id = uuid.uuid4().hex
    output_template = str(DOWNLOADS_DIR / f"{job_id}.%(ext)s")
    opts = build_ydl_opts(req.format, req.quality, output_template)
    try:
        loop = asyncio.get_event_loop()
        title = await loop.run_in_executor(None, _download, opts, req.url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Conversion failed: {str(e)}")
    candidates = list(DOWNLOADS_DIR.glob(f"{job_id}.*"))
    if not candidates:
        raise HTTPException(status_code=500, detail="Output file not found after conversion")
    output_file = candidates[0]
    actual_ext = output_file.suffix.lstrip(".")

    if req.album:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _embed_album, output_file, req.album)
        except Exception:
            pass

    base_name = req.filename.strip() or title or "video"
    safe_name = base_name.replace("/", "").replace("\\", "").replace("\0", "")
    background_tasks.add_task(delete_file_later, str(output_file))
    return FileResponse(
        path=str(output_file),
        filename=f"{safe_name}.{actual_ext}",
        media_type="application/octet-stream",
    )


@app.post("/convert-playlist")
async def convert_playlist(req: ConvertRequest):
    if req.format not in FORMAT_OPTIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {req.format}")
    job_id = uuid.uuid4().hex
    _playlist_jobs[job_id] = _PlaylistJob()
    thread = threading.Thread(
        target=_run_playlist,
        args=(job_id, req.url, req.format, req.quality, req.album),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


@app.get("/playlist-status/{job_id}")
async def playlist_status(job_id: str):
    job = _playlist_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "status": job.status,
        "total": job.total,
        "downloaded": job.downloaded,
        "current": job.current,
        "error": job.error,
    }


@app.get("/playlist-download/{job_id}")
async def playlist_download(job_id: str, background_tasks: BackgroundTasks):
    job = _playlist_jobs.get(job_id)
    if not job or job.status != "done" or not job.zip_path:
        raise HTTPException(status_code=404, detail="Playlist not ready")
    zip_path = job.zip_path
    safe_title = job.playlist_title.replace("/", "").replace("\\", "").replace("\0", "") or "playlist"

    def cleanup():
        delete_file_later(str(zip_path))
        _playlist_jobs.pop(job_id, None)

    background_tasks.add_task(cleanup)
    return FileResponse(
        path=str(zip_path),
        filename=f"{safe_title}.zip",
        media_type="application/zip",
    )


def _extract_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(base_ydl_opts()) as ydl:
        return ydl.extract_info(url, download=False)


def _download(opts: dict, url: str) -> str:
    title: dict = {}

    def _hook(d: dict):
        if "title" not in title:
            t = (d.get("info_dict") or {}).get("title")
            if t:
                title["title"] = t

    opts = {**opts, "progress_hooks": [_hook]}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return title.get("title", "video")
