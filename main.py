import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import yt_dlp

app = FastAPI(title="YouTube Converter")

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

STATIC_DIR = Path("static")
COOKIES_FILE = Path(os.environ.get("COOKIES_PATH", "cookies.txt"))
COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)

YTDLP_CACHE = Path(os.environ.get("YTDLP_CACHE_DIR", str(COOKIES_FILE.parent / "yt-dlp-cache")))
OAUTH2_FLAG = YTDLP_CACHE / "oauth2.done"
YTDLP_CACHE.mkdir(parents=True, exist_ok=True)

WINDOWS_USERS = Path("/mnt/c/Users")
FIREFOX_BASE = "AppData/Roaming/Mozilla/Firefox/Profiles"
CHROME_LOCAL_STATE = "AppData/Local/Google/Chrome/User Data/Local State"
CHROME_COOKIES = "AppData/Local/Google/Chrome/User Data/Default/Network/Cookies"
EDGE_LOCAL_STATE = "AppData/Local/Microsoft/Edge/User Data/Local State"
EDGE_COOKIES = "AppData/Local/Microsoft/Edge/User Data/Default/Network/Cookies"


def _win_user() -> Path | None:
    """Return the first readable non-system Windows user directory."""
    system = {"All Users", "Default", "Default User", "Public"}
    try:
        for d in WINDOWS_USERS.iterdir():
            if d.name not in system and not d.name.endswith(".ini"):
                try:
                    list(d.iterdir())
                    return d
                except PermissionError:
                    continue
    except (PermissionError, FileNotFoundError):
        pass
    return None


# Windows temp dir: resolved at startup from the detected user
def _win_temp() -> Path:
    user = _win_user()
    if user:
        return user / "AppData/Local/Temp"
    return Path(tempfile.gettempdir())

POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
NODE = which("node") or ""

_powershell_usable_cache: bool | None = None

def _powershell_usable() -> bool:
    global _powershell_usable_cache
    if _powershell_usable_cache is not None:
        return _powershell_usable_cache
    if not POWERSHELL.exists():
        _powershell_usable_cache = False
        return False
    try:
        r = subprocess.run([str(POWERSHELL), "-Version"], capture_output=True, timeout=5)
        _powershell_usable_cache = r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        # OSError [Errno 8]: .exe can't run in a Linux container (no WSL2 interop)
        _powershell_usable_cache = False
    return _powershell_usable_cache

AUTH_COOKIES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
    "__Secure-3PSID", "__Secure-3PAPISID", "__Secure-1PSID",
}


# ── OAuth2 device-code login ──────────────────────────────────────────────────

@dataclass
class _OAuthSession:
    session_id: str
    device_url: str | None = None
    user_code: str | None = None
    done: bool = False
    success: bool = False
    error: str | None = None
    _url_ready: threading.Event = field(default_factory=threading.Event)


_oauth_sessions: dict[str, _OAuthSession] = {}

_URL_RE  = re.compile(r'https://\S*google\.com\S*device\S*', re.I)
_CODE_RE = re.compile(r'code[:\s]+([A-Z0-9]{4}-[A-Z0-9]{4})', re.I)


class _OAuthLogger:
    """Captures the device-code URL/code printed by yt-dlp during OAuth2 flow."""
    def __init__(self, session: _OAuthSession):
        self._s = session

    def _parse(self, msg: str):
        if self._s.device_url and self._s.user_code:
            return
        url_m  = _URL_RE.search(msg)
        code_m = _CODE_RE.search(msg)
        if url_m:
            self._s.device_url = url_m.group()
        if code_m:
            self._s.user_code = code_m.group(1)
        if url_m or code_m:
            self._s._url_ready.set()

    def debug(self, msg: str):   self._parse(msg)
    def warning(self, msg: str): self._parse(msg)
    def error(self, msg: str):
        self._s.error = msg
        self._s._url_ready.set()


def _oauth_token_cached() -> bool:
    """True if yt-dlp cached an OAuth2 access_token (resilient to version changes)."""
    try:
        for f in YTDLP_CACHE.rglob("*.json"):
            if '"access_token"' in f.read_text():
                return True
    except Exception:
        pass
    return False


def _run_oauth_flow(session: _OAuthSession):
    opts = {
        "username": "oauth2",
        "password": "",
        "logger": _OAuthLogger(session),
        "quiet": False,
        "no_warnings": False,
        "cachedir": str(YTDLP_CACHE),
        **({"js_runtimes": {"node": {"path": NODE}}, "remote_components": {"ejs:github"}} if NODE else {}),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Any public YouTube video triggers the login flow
            ydl.extract_info("https://www.youtube.com/watch?v=jNQXAC9IVRw", download=False)
        session.success = True
    except Exception:
        # Video extraction may fail after a successful OAuth2 auth — check the cache
        session.success = _oauth_token_cached()
    finally:
        if session.success:
            OAUTH2_FLAG.write_text("ok")
        session.done = True
        session._url_ready.set()


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

FORMAT_OPTIONS = {
    "mp4": {
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "ext": "mp4",
    },
    "webm": {
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "webm",
        "ext": "webm",
    },
    "mp3": {
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        "ext": "mp3",
    },
    "wav": {
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        "ext": "wav",
    },
    "aac": {
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "aac"}],
        "ext": "aac",
    },
}


class ConvertRequest(BaseModel):
    url: str
    format: str
    quality: str = "best"


def delete_file_later(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def is_authenticated() -> bool:
    return cookies_are_authenticated() or OAUTH2_FLAG.exists()


def base_ydl_opts() -> dict:
    has_cookies = cookies_are_authenticated()
    has_oauth   = OAUTH2_FLAG.exists()
    has_auth    = has_cookies or has_oauth
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "cachedir": str(YTDLP_CACHE),
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "ios", "android"] if has_auth else ["ios", "android"]
            }
        },
        **({"js_runtimes": {"node": {"path": NODE}}, "remote_components": {"ejs:github"}} if NODE else {}),
    }
    if has_cookies:
        opts["cookiefile"] = str(COOKIES_FILE)
    if has_oauth:
        opts["username"] = "oauth2"
        opts["password"] = ""
    return opts


def build_ydl_opts(fmt: str, quality: str, output_path: str) -> dict:
    opts = {**base_ydl_opts(), **FORMAT_OPTIONS[fmt]}
    if quality == "720p" and fmt in ("mp4", "webm"):
        opts["format"] = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
    elif quality == "480p" and fmt in ("mp4", "webm"):
        opts["format"] = "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
    elif quality == "360p" and fmt in ("mp4", "webm"):
        opts["format"] = "bestvideo[height<=360]+bestaudio/best[height<=360]/best"
    opts["outtmpl"] = output_path
    return opts


def cookies_are_authenticated() -> bool:
    if not COOKIES_FILE.exists():
        return False
    for line in COOKIES_FILE.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) == 7 and parts[5] in AUTH_COOKIES:
            return True
    return False


# ── Firefox ───────────────────────────────────────────────────────────────────

def find_firefox_profiles() -> list[Path]:
    profiles = []
    if not WINDOWS_USERS.exists():
        return profiles
    try:
        user_dirs = list(WINDOWS_USERS.iterdir())
    except PermissionError:
        return profiles
    for user_dir in user_dirs:
        try:
            ff_dir = user_dir / FIREFOX_BASE
            if not ff_dir.exists():
                continue
            for p in ff_dir.iterdir():
                try:
                    if (p / "cookies.sqlite").exists():
                        profiles.append(p)
                except PermissionError:
                    continue
        except PermissionError:
            continue
    return profiles


def export_firefox_cookies(profile: Path, dest: Path):
    tmp = Path(tempfile.mktemp(suffix=".sqlite"))
    try:
        shutil.copy2(profile / "cookies.sqlite", tmp)
        con = sqlite3.connect(str(tmp))
        rows = con.execute(
            "SELECT host, path, isSecure, expiry, name, value FROM moz_cookies"
        ).fetchall()
        con.close()
        lines = ["# Netscape HTTP Cookie File\n"]
        for host, path, secure, expiry, name, value in rows:
            subdomain = "TRUE" if host.startswith(".") else "FALSE"
            secure_flag = "TRUE" if secure else "FALSE"
            lines.append(f"{host}\t{subdomain}\t{path}\t{secure_flag}\t{expiry}\t{name}\t{value}\n")
        dest.write_text("".join(lines))
    finally:
        tmp.unlink(missing_ok=True)


# ── Chrome / Edge (DPAPI via PowerShell) ─────────────────────────────────────

def _chrome_master_key(local_state_path: Path) -> bytes:
    """
    Chrome encrypts its AES master key with Windows DPAPI.
    We call PowerShell to do the DPAPI decryption, then return the raw key bytes.
    """
    win_ls = _win_path(local_state_path)
    ps_script = f"""
Add-Type -AssemblyName System.Security
$ls = Get-Content '{win_ls}' -Raw | ConvertFrom-Json
$enc = [Convert]::FromBase64String($ls.os_crypt.encrypted_key)
$enc = $enc[5..($enc.Length-1)]
$key = [System.Security.Cryptography.ProtectedData]::Unprotect($enc, $null, 'CurrentUser')
[Convert]::ToBase64String($key)
"""
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PowerShell DPAPI error: {result.stderr.strip()}")
    return base64.b64decode(result.stdout.strip())


def _decrypt_chrome_value(encrypted_value: bytes, key: bytes) -> str:
    if not encrypted_value:
        return ""
    if encrypted_value[:3] in (b"v10", b"v11"):
        nonce = encrypted_value[3:15]
        ciphertext_and_tag = encrypted_value[15:]
        return AESGCM(key).decrypt(nonce, ciphertext_and_tag, None).decode("utf-8", errors="replace")
    return ""


def _copy_locked_file_via_powershell(win_path: str, dest: Path):
    """Copy a Chromium cookies DB + WAL/SHM while the browser may be running."""
    win_dest = _win_path(dest)

    def copy_one(src_win: str, dst_win: str) -> bool:
        ps = f"""
try {{
  $src = [System.IO.File]::Open('{src_win}', 'Open', 'Read', [System.IO.FileShare]'ReadWrite, Delete')
  $dst = [System.IO.File]::Create('{dst_win}')
  $src.CopyTo($dst); $src.Close(); $dst.Close(); exit 0
}} catch {{ exit 1 }}
"""
        r = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0

    if not copy_one(win_path, win_dest):
        raise RuntimeError(
            "Browser is holding an exclusive lock on the cookies file. "
            "Please close the browser completely (including from the system tray), "
            "then try again."
        )
    copy_one(win_path + "-wal", win_dest + "-wal")
    copy_one(win_path + "-shm", win_dest + "-shm")


def _win_path(wsl_path: Path) -> str:
    """Convert /mnt/c/... to C:\\... for PowerShell."""
    parts = wsl_path.parts
    drive = parts[2].upper() + ":\\"
    return drive + "\\".join(parts[3:])


def export_chromium_cookies(local_state: Path, cookies_db: Path, dest: Path):
    master_key = _chrome_master_key(local_state)

    tmp_db = _win_temp() / "yt_conv_cookies_tmp.db"
    _copy_locked_file_via_powershell(_win_path(cookies_db), tmp_db)

    con = sqlite3.connect(str(tmp_db))
    rows = con.execute(
        "SELECT host_key, path, is_secure, expires_utc, name, encrypted_value FROM cookies"
    ).fetchall()
    con.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(tmp_db) + suffix).unlink(missing_ok=True)

    lines = ["# Netscape HTTP Cookie File\n"]
    for host, path, secure, expires_utc, name, enc_val in rows:
        try:
            value = _decrypt_chrome_value(bytes(enc_val), master_key)
        except Exception:
            continue
        if not value:
            continue
        # Chrome timestamps: microseconds since 1601-01-01 → Unix epoch
        unix_expiry = max(0, (expires_utc // 1_000_000) - 11_644_473_600)
        subdomain = "TRUE" if host.startswith(".") else "FALSE"
        secure_flag = "TRUE" if secure else "FALSE"
        lines.append(f"{host}\t{subdomain}\t{path}\t{secure_flag}\t{unix_expiry}\t{name}\t{value}\n")

    dest.write_text("".join(lines))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content=(STATIC_DIR / "index.html").read_text())


@app.get("/sync_cookies.py")
async def download_sync_script():
    script = Path("sync_cookies.py")
    if not script.exists():
        raise HTTPException(status_code=404, detail="Sync script not found.")
    return FileResponse(path=str(script), filename="sync_cookies.py", media_type="text/plain")


@app.post("/login-start")
async def login_start():
    """Begin a YouTube OAuth2 device-code login flow."""
    session_id = uuid.uuid4().hex
    session = _OAuthSession(session_id=session_id)
    _oauth_sessions[session_id] = session

    thread = threading.Thread(target=_run_oauth_flow, args=(session,), daemon=True)
    thread.start()

    # Wait up to 25 s for yt-dlp to print the device URL
    got_url = await asyncio.get_event_loop().run_in_executor(
        None, lambda: session._url_ready.wait(25)
    )
    if not got_url or (not session.device_url and not session.user_code):
        _oauth_sessions.pop(session_id, None)
        if session.error:
            raise HTTPException(status_code=500, detail=session.error)
        raise HTTPException(status_code=504, detail="Timed out waiting for Google device code.")

    return {
        "session_id": session_id,
        "device_url": session.device_url or "https://www.google.com/device",
        "user_code": session.user_code,
    }


@app.get("/login-poll/{session_id}")
async def login_poll(session_id: str):
    """Poll for OAuth2 login completion."""
    session = _oauth_sessions.get(session_id)
    if not session:
        # Session already cleaned up — check persisted flag
        return {"done": True, "success": OAUTH2_FLAG.exists()}
    result = {"done": session.done, "success": session.success, "error": session.error}
    if session.done:
        _oauth_sessions.pop(session_id, None)
    return result


@app.delete("/login-oauth2")
async def logout_oauth2():
    """Clear the stored OAuth2 token."""
    OAUTH2_FLAG.unlink(missing_ok=True)
    for f in YTDLP_CACHE.rglob("*.json"):
        try:
            if '"access_token"' in f.read_text():
                f.unlink()
        except Exception:
            pass
    return {"ok": True}


@app.get("/cookies-status")
async def cookies_status():
    profiles = find_firefox_profiles()
    user = _win_user()
    chrome_path = (user / CHROME_COOKIES) if user else None
    edge_path = (user / EDGE_COOKIES) if user else None
    ps = _powershell_usable()
    return {
        "has_cookies": is_authenticated(),
        "oauth2": OAUTH2_FLAG.exists(),
        "firefox_available": len(profiles) > 0,
        "chrome_available": bool(ps and chrome_path and chrome_path.exists()),
        "edge_available": bool(ps and edge_path and edge_path.exists()),
    }


@app.post("/sync-firefox")
async def sync_firefox():
    profiles = find_firefox_profiles()
    if not profiles:
        raise HTTPException(status_code=404, detail="No Firefox profile found.")
    profile = next((p for p in profiles if "default-release" in p.name), profiles[0])
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, export_firefox_cookies, profile, COOKIES_FILE)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read Firefox cookies: {e}")
    found_auth = _count_auth_cookies()
    return {"ok": True, "browser": "firefox", "authenticated": found_auth > 0}


@app.post("/sync-chrome")
async def sync_chrome():
    user = _win_user()
    if not user:
        raise HTTPException(status_code=404, detail="No Windows user directory found.")
    local_state = user / CHROME_LOCAL_STATE
    cookies_db = user / CHROME_COOKIES
    if not local_state.exists() or not cookies_db.exists():
        raise HTTPException(status_code=404, detail="Chrome not found.")
    if not _powershell_usable():
        raise HTTPException(status_code=500, detail="PowerShell not available (Chrome sync requires running outside a container, or on WSL2 directly).")
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, export_chromium_cookies, local_state, cookies_db, COOKIES_FILE)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chrome sync failed: {e}")
    found_auth = _count_auth_cookies()
    return {"ok": True, "browser": "chrome", "authenticated": found_auth > 0}


@app.post("/sync-edge")
async def sync_edge():
    user = _win_user()
    if not user:
        raise HTTPException(status_code=404, detail="No Windows user directory found.")
    local_state = user / EDGE_LOCAL_STATE
    cookies_db = user / EDGE_COOKIES
    if not local_state.exists() or not cookies_db.exists():
        raise HTTPException(status_code=404, detail="Edge not found.")
    if not _powershell_usable():
        raise HTTPException(status_code=500, detail="PowerShell not available (Edge sync requires running outside a container, or on WSL2 directly).")
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, export_chromium_cookies, local_state, cookies_db, COOKIES_FILE)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Edge sync failed: {e}")
    found_auth = _count_auth_cookies()
    return {"ok": True, "browser": "edge", "authenticated": found_auth > 0}


@app.post("/upload-cookies")
async def upload_cookies(file: UploadFile = File(...)):
    content = await file.read()
    COOKIES_FILE.write_bytes(content)
    return {"ok": True}


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


def _count_auth_cookies() -> int:
    if not COOKIES_FILE.exists():
        return 0
    return sum(
        1 for line in COOKIES_FILE.read_text().splitlines()
        if len(line.split("\t")) == 7 and line.split("\t")[5] in AUTH_COOKIES
    )


def _extract_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(base_ydl_opts()) as ydl:
        return ydl.extract_info(url, download=False)


def _download(opts: dict, url: str):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
