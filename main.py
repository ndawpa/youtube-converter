import os
import uuid
import asyncio
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
                "player_client": ["web", "ios", "android"] if has_cookies else ["ios", "android"]
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


def delete_file_later(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


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
        await loop.run_in_executor(None, _download, opts, req.url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Conversion failed: {str(e)}")
    candidates = list(DOWNLOADS_DIR.glob(f"{job_id}.*"))
    if not candidates:
        raise HTTPException(status_code=500, detail="Output file not found after conversion")
    output_file = str(candidates[0])
    actual_ext = candidates[0].suffix.lstrip(".")
    background_tasks.add_task(delete_file_later, output_file)
    return FileResponse(
        path=output_file,
        filename=f"video.{actual_ext}",
        media_type="application/octet-stream",
    )


def _extract_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(base_ydl_opts()) as ydl:
        return ydl.extract_info(url, download=False)


def _download(opts: dict, url: str):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
