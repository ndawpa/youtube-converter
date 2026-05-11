#!/usr/bin/env python3
"""
sync_cookies.py – companion script for YouTube Converter
=========================================================
Reads YouTube cookies from Firefox, Chrome, or Edge on the local Windows /
WSL2 machine and uploads them to a running YouTube Converter instance.

Usage (run once):
    python sync_cookies.py --url https://your-service.example.com

Schedule automatic hourly sync on Windows (run once as Administrator):
    python sync_cookies.py --url https://your-service.example.com --schedule

Remove the scheduled task:
    python sync_cookies.py --unschedule
"""

import argparse
import base64
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# ── constants ─────────────────────────────────────────────────────────────────

AUTH_COOKIES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
    "__Secure-3PSID", "__Secure-3PAPISID", "__Secure-1PSID",
}

TASK_NAME = "YouTubeConverterCookieSync"

# Detect whether we're running inside WSL2 or native Windows
_ON_WSL = sys.platform == "linux" and Path("/mnt/c/Windows").exists()
_ON_WIN = sys.platform == "win32"

if _ON_WSL:
    USERS_ROOT = Path("/mnt/c/Users")
    POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
elif _ON_WIN:
    USERS_ROOT = Path("C:/Users")
    POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / \
        "System32/WindowsPowerShell/v1.0/powershell.exe"
else:
    USERS_ROOT = Path("/mnt/c/Users")   # fallback
    POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")

FIREFOX_BASE = "AppData/Roaming/Mozilla/Firefox/Profiles"
CHROME_STATE = "AppData/Local/Google/Chrome/User Data/Local State"
CHROME_DB    = "AppData/Local/Google/Chrome/User Data/Default/Network/Cookies"
EDGE_STATE   = "AppData/Local/Microsoft/Edge/User Data/Local State"
EDGE_DB      = "AppData/Local/Microsoft/Edge/User Data/Default/Network/Cookies"

SYSTEM_DIRS = {"All Users", "Default", "Default User", "Public"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _win_user() -> Path | None:
    try:
        for d in USERS_ROOT.iterdir():
            if d.name not in SYSTEM_DIRS and not d.name.endswith(".ini"):
                try:
                    list(d.iterdir())
                    return d
                except PermissionError:
                    continue
    except (PermissionError, FileNotFoundError):
        pass
    return None


def _win_path(p: Path) -> str:
    """Convert /mnt/c/... → C:\\... for PowerShell."""
    if _ON_WIN:
        return str(p)
    parts = p.parts
    drive = parts[2].upper() + ":\\"
    return drive + "\\".join(parts[3:])


def _run_ps(script: str, timeout: int = 15) -> str:
    r = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "PowerShell error")
    return r.stdout.strip()


# ── Firefox ───────────────────────────────────────────────────────────────────

def export_firefox(dest: Path) -> bool:
    user = _win_user()
    if not user:
        return False
    ff_base = user / FIREFOX_BASE
    if not ff_base.exists():
        return False

    profiles = [
        p for p in ff_base.iterdir()
        if (p / "cookies.sqlite").exists()
    ]
    if not profiles:
        return False

    profile = next((p for p in profiles if "default-release" in p.name), profiles[0])
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
            sub = "TRUE" if host.startswith(".") else "FALSE"
            sf  = "TRUE" if secure else "FALSE"
            lines.append(f"{host}\t{sub}\t{path}\t{sf}\t{expiry}\t{name}\t{value}\n")
        dest.write_text("".join(lines))
        return True
    except Exception as e:
        print(f"  Firefox export failed: {e}")
        return False
    finally:
        tmp.unlink(missing_ok=True)


# ── Chrome / Edge (DPAPI) ────────────────────────────────────────────────────

def _chrome_master_key(local_state: Path) -> bytes:
    win_ls = _win_path(local_state)
    script = f"""
Add-Type -AssemblyName System.Security
$ls  = Get-Content '{win_ls}' -Raw | ConvertFrom-Json
$enc = [Convert]::FromBase64String($ls.os_crypt.encrypted_key)
$enc = $enc[5..($enc.Length-1)]
$key = [System.Security.Cryptography.ProtectedData]::Unprotect($enc, $null, 'CurrentUser')
[Convert]::ToBase64String($key)
"""
    return base64.b64decode(_run_ps(script))


def _decrypt_chrome_value(encrypted_value: bytes, key: bytes) -> str:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return ""
    if not encrypted_value:
        return ""
    if encrypted_value[:3] in (b"v10", b"v11"):
        nonce = encrypted_value[3:15]
        return AESGCM(key).decrypt(nonce, encrypted_value[15:], None).decode("utf-8", errors="replace")
    return ""


def _copy_locked_db(win_src: str, dest: Path):
    win_dst = _win_path(dest)

    def copy_one(src: str, dst: str) -> bool:
        ps = f"""
try {{
  $s = [System.IO.File]::Open('{src}','Open','Read',[System.IO.FileShare]'ReadWrite,Delete')
  $d = [System.IO.File]::Create('{dst}')
  $s.CopyTo($d); $s.Close(); $d.Close(); exit 0
}} catch {{ exit 1 }}
"""
        r = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0

    if not copy_one(win_src, win_dst):
        raise RuntimeError(
            "Browser holds an exclusive lock. Close the browser completely and retry."
        )
    copy_one(win_src + "-wal", win_dst + "-wal")
    copy_one(win_src + "-shm", win_dst + "-shm")


def export_chromium(state_path: Path, db_path: Path, dest: Path) -> bool:
    if not POWERSHELL.exists():
        return False
    try:
        key = _chrome_master_key(state_path)
    except Exception as e:
        print(f"  DPAPI key error: {e}")
        return False

    tmp_db = Path(tempfile.mktemp(suffix=".db"))
    try:
        _copy_locked_db(_win_path(db_path), tmp_db)
        con = sqlite3.connect(str(tmp_db))
        rows = con.execute(
            "SELECT host_key, path, is_secure, expires_utc, name, encrypted_value FROM cookies"
        ).fetchall()
        con.close()
    except Exception as e:
        print(f"  DB copy/read error: {e}")
        return False
    finally:
        for suf in ("", "-wal", "-shm"):
            Path(str(tmp_db) + suf).unlink(missing_ok=True)

    lines = ["# Netscape HTTP Cookie File\n"]
    for host, path, secure, expires_utc, name, enc_val in rows:
        value = _decrypt_chrome_value(bytes(enc_val), key)
        if not value:
            continue
        unix_expiry = max(0, (expires_utc // 1_000_000) - 11_644_473_600)
        sub = "TRUE" if host.startswith(".") else "FALSE"
        sf  = "TRUE" if secure else "FALSE"
        lines.append(f"{host}\t{sub}\t{path}\t{sf}\t{unix_expiry}\t{name}\t{value}\n")

    dest.write_text("".join(lines))
    return True


# ── upload ────────────────────────────────────────────────────────────────────

def upload(cookies_path: Path, service_url: str) -> bool:
    url = service_url.rstrip("/") + "/upload-cookies"
    boundary = "----FormBoundary7MA4YWxkTrZu0gW"
    data = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="cookies.txt"\r\n'
        f"Content-Type: text/plain\r\n\r\n"
    ).encode() + cookies_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"  Upload error: {e}")
        return False


def has_auth_cookie(path: Path) -> bool:
    for line in path.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) == 7 and parts[5] in AUTH_COOKIES:
            return True
    return False


# ── scheduling ────────────────────────────────────────────────────────────────

def schedule(service_url: str):
    script_path = Path(sys.argv[0]).resolve()
    ps = f"""
$action  = New-ScheduledTaskAction -Execute 'python' `
             -Argument '"{script_path}" --url {service_url}'
$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Hours 1) `
             -Once -At (Get-Date)
$settings = New-ScheduledTaskSettingsSet -RunOnlyIfNetworkAvailable `
              -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action `
  -Trigger $trigger -Settings $settings -Force
"""
    try:
        _run_ps(ps, timeout=30)
        print(f"Scheduled task '{TASK_NAME}' registered — syncs every hour to {service_url}")
    except Exception as e:
        print(f"Failed to register scheduled task: {e}")
        sys.exit(1)


def unschedule():
    ps = f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue"
    try:
        _run_ps(ps, timeout=15)
        print(f"Scheduled task '{TASK_NAME}' removed.")
    except Exception as e:
        print(f"Error removing task: {e}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync browser cookies to YouTube Converter")
    parser.add_argument("--url", default="http://localhost:8000",
                        help="Service base URL (default: http://localhost:8000)")
    parser.add_argument("--schedule", action="store_true",
                        help="Register as a Windows scheduled task (hourly)")
    parser.add_argument("--unschedule", action="store_true",
                        help="Remove the scheduled task")
    args = parser.parse_args()

    if args.unschedule:
        unschedule()
        return

    if args.schedule:
        schedule(args.url)
        # fall through to also run once now

    tmp = Path(tempfile.mktemp(suffix=".txt"))
    try:
        user = _win_user()
        exported = False

        print("Trying Firefox…")
        if export_firefox(tmp):
            if has_auth_cookie(tmp):
                print("  ✓ YouTube session found in Firefox")
                exported = True
            else:
                print("  Firefox cookies found but no YouTube login — skipping")

        if not exported and user:
            for browser, state, db in [
                ("Chrome", user / CHROME_STATE, user / CHROME_DB),
                ("Edge",   user / EDGE_STATE,   user / EDGE_DB),
            ]:
                if not state.exists() or not db.exists():
                    print(f"Trying {browser}… not installed")
                    continue
                print(f"Trying {browser}…")
                try:
                    if export_chromium(state, db, tmp):
                        if has_auth_cookie(tmp):
                            print(f"  ✓ YouTube session found in {browser}")
                            exported = True
                            break
                        else:
                            print(f"  {browser} cookies found but no YouTube login — skipping")
                except Exception as e:
                    print(f"  {browser} error: {e}")

        if not exported:
            print("No authenticated YouTube session found in any browser.")
            print("Log in to YouTube in Firefox, Chrome, or Edge and try again.")
            sys.exit(1)

        print(f"Uploading to {args.url} …")
        if upload(tmp, args.url):
            print("✓ Cookies uploaded successfully.")
        else:
            print("Upload failed — is the service running?")
            sys.exit(1)
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
