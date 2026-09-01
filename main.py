import os
import shutil
import subprocess
import threading
import uuid
import zipfile
import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import yt_dlp
from yt_dlp.utils import download_range_func
from clip_engine import (
    ClipCandidate,
    TranscriptSegment,
    analyze_spectral_frames,
    build_render_command,
    build_silence_removal_command,
    boost_candidates_with_audio_energy,
    boost_candidates_with_events,
    compress_transcript_timeline,
    detect_audible_ranges,
    detect_scene_changes,
    estimate_music_blocks,
    fit_music_blocks,
    intersect_ranges,
    infer_mood_energy,
    parse_subtitle_file,
    rank_clip_candidates,
    tempo_distance,
    write_ass_subtitles,
)

app = FastAPI(title="YouTube Converter")

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)
MUSIC_DIR = Path(os.environ.get("MUSIC_LIBRARY_PATH", str(DOWNLOADS_DIR / "music_library")))
MUSIC_DIR.mkdir(parents=True, exist_ok=True)
MUSIC_MANIFEST = MUSIC_DIR / "tracks.json"
MAX_RENDER_JOBS = max(1, int(os.environ.get("MAX_RENDER_JOBS", "1")))
_render_slots = threading.BoundedSemaphore(MAX_RENDER_JOBS)

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
    "mp4": {"kind": "video", "merge_output_format": "mp4"},
    "webm": {"kind": "video", "merge_output_format": "webm"},
    "mkv": {"kind": "video", "merge_output_format": "mkv"},
    "mp3": {"kind": "audio", "codec": "mp3", "quality": "192"},
    "m4a": {"kind": "audio", "codec": "m4a", "quality": "0"},
    "opus": {"kind": "audio", "codec": "opus", "quality": "0"},
    "flac": {"kind": "audio", "codec": "flac", "quality": "0"},
    "wav": {"kind": "audio", "codec": "wav", "quality": "0"},
}

VIDEO_QUALITY_HEIGHTS = {
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}
THUMBNAIL_FORMATS = {"mp3", "m4a", "opus", "flac", "mp4", "mkv"}


class ConvertRequest(BaseModel):
    url: str
    format: str
    quality: str = "best"
    filename: str = ""
    album: str = ""
    artist: str = ""
    title: str = ""
    year: str = ""
    track: str = ""
    embed_thumbnail: bool = True
    compatibility: bool = False
    start_time: float | None = Field(default=None, ge=0)
    end_time: float | None = Field(default=None, gt=0)
    split_chapters: bool = False
    chapter_titles: list[str] = Field(default_factory=list)
    subtitle_lang: str = ""
    transcript_format: str = ""


class CancelRequest(BaseModel):
    job_id: str


class AutoClipRequest(BaseModel):
    url: str
    objective: str = "highlights"
    count: int = Field(default=3, ge=1, le=10)
    min_duration: float = Field(default=25, ge=5, le=180)
    max_duration: float = Field(default=60, ge=5, le=300)
    language: str = ""
    detect_music: bool = True
    automatic: bool = False


class ClipSelection(BaseModel):
    index: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = ""


class RenderClipsRequest(BaseModel):
    candidate_indexes: list[int] = Field(default_factory=list)
    selections: list[ClipSelection] = Field(default_factory=list)
    music_block_indexes: list[int] = Field(default_factory=list)
    render_speech_candidates: bool = True
    aspect_ratio: str = "9:16"
    reframe: str = "center"
    subtitle_style: str = "bold"
    burn_subtitles: bool = True
    remove_silence: bool = False
    add_background_music: bool = False
    music_track_id: str = "auto"
    music_mood: str = "inspiring"
    music_energy: str = "medium"
    music_volume: float = Field(default=0.16, ge=0, le=1)


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


@dataclass
class _DownloadJob:
    status: str = "pending"  # pending | running | done | error | cancelled
    progress: float = 0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0
    eta: int | None = None
    current: str = ""
    title: str = "video"
    filename: str = ""
    output_path: Path | None = None
    error: str | None = None
    cancel_requested: bool = False
    created_at: float = 0


@dataclass
class _ClipJob:
    status: str = "pending"  # pending | analyzing | ready | rendering | done | error | cancelled
    phase: str = "pending"
    progress: float = 0
    title: str = "video"
    candidates: list[ClipCandidate] | None = None
    music_blocks: list[tuple[float, float]] | None = None
    audible_ranges: list[tuple[float, float]] | None = None
    suggested_mood: str = "inspiring"
    suggested_energy: str = "medium"
    segments: list[TranscriptSegment] | None = None
    source_path: Path | None = None
    work_dir: Path | None = None
    output_path: Path | None = None
    error: str | None = None
    cancel_requested: bool = False
    created_at: float = 0


_playlist_jobs: dict[str, _PlaylistJob] = {}
_download_jobs: dict[str, _DownloadJob] = {}
_clip_jobs: dict[str, _ClipJob] = {}


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
        **({"js_runtimes": {"node": {"path": NODE}}, "remote_components": {"ejs:github"}} if NODE else {}),
    }
    if has_cookies:
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def video_format_for_quality(quality: str) -> str:
    match = re.fullmatch(r"(\d{2,4})p", quality)
    height = int(match.group(1)) if match else None
    if height is None:
        return "bestvideo+bestaudio/best"

    # A named resolution must not silently fall back to a lower-quality file.
    # YouTube usually exposes 1080p as separate video/audio streams, hence the
    # first selector; the second supports sources with a pre-merged stream.
    return f"bestvideo[height={height}]+bestaudio/best[height={height}]"


def video_format_for_container(fmt: str, quality: str, compatibility: bool = False) -> str:
    match = re.fullmatch(r"(\d{2,4})p", quality)
    height_filter = f"[height={match.group(1)}]" if match else ""
    combined_filter = height_filter
    if fmt == "webm":
        return (f"bestvideo{height_filter}[ext=webm]+bestaudio[ext=webm]"
                f"/best{combined_filter}[ext=webm]")
    if fmt == "mp4":
        codec_filter = "[vcodec^=avc1]" if compatibility else ""
        return (f"bestvideo{height_filter}[ext=mp4]{codec_filter}+bestaudio[ext=m4a]"
                f"/bestvideo{height_filter}{codec_filter}+bestaudio"
                f"/best{combined_filter}[ext=mp4]")
    return video_format_for_quality(quality)


def build_ydl_opts(fmt: str, quality: str, output_path: str,
                   req: ConvertRequest | None = None) -> dict:
    config = FORMAT_OPTIONS[fmt]
    opts = {**base_ydl_opts(), "outtmpl": output_path}
    postprocessors: list[dict] = []

    if config["kind"] == "video":
        opts["format"] = video_format_for_container(fmt, quality, bool(req and req.compatibility))
        opts["merge_output_format"] = config["merge_output_format"]
    else:
        opts["format"] = "bestaudio/best"
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": config["codec"],
            "preferredquality": config["quality"],
        })

    if req:
        ranges: list[list[float]] = []
        if req.start_time is not None or req.end_time is not None:
            start = req.start_time or 0
            end = req.end_time or float("inf")
            if end <= start:
                raise ValueError("End time must be greater than start time")
            ranges.append([start, end])
        is_clipped = bool(ranges or req.chapter_titles)
        if is_clipped:
            chapter_patterns = [f"^{re.escape(title)}$" for title in req.chapter_titles]
            opts["download_ranges"] = download_range_func(chapter_patterns, ranges)
            opts["force_keyframes_at_cuts"] = True
        if req.split_chapters:
            postprocessors.append({"key": "FFmpegSplitChapters", "force_keyframes": False})
        if req.embed_thumbnail and fmt in THUMBNAIL_FORMATS:
            opts["writethumbnail"] = True
            postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        metadata = {
            "title": req.title,
            "artist": req.artist,
            "album": req.album,
            "date": req.year,
            "track_number": req.track,
        }
        opts["postprocessor_args"] = {
            "metadata": [item for key, value in metadata.items() if value
                         for item in ("-metadata", f"{key}={value}")]
        }
        # Original chapter timestamps can extend beyond a downloaded section.
        # Embedding them makes players report the source video's full duration
        # and seek back to the beginning when the clipped media stream ends.
        postprocessors.append({
            "key": "FFmpegMetadata",
            "add_metadata": True,
            "add_chapters": not is_clipped,
        })

    if postprocessors:
        opts["postprocessors"] = postprocessors
    opts["outtmpl"] = output_path
    return opts


def safe_filename(value: str, fallback: str = "video") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", value).strip().rstrip(".")
    return cleaned[:180] or fallback


def _format_info(info: dict) -> dict:
    video_formats: dict[tuple, dict] = {}
    for fmt in info.get("formats") or []:
        if fmt.get("vcodec") in (None, "none") or not fmt.get("height"):
            continue
        key = (fmt.get("height"), fmt.get("fps"), bool(fmt.get("dynamic_range") not in (None, "SDR")))
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        current = video_formats.get(key)
        if not current or size > current["filesize"]:
            video_formats[key] = {
                "height": fmt.get("height"),
                "width": fmt.get("width"),
                "fps": fmt.get("fps"),
                "hdr": key[2],
                "filesize": size,
                "ext": fmt.get("ext"),
                "vcodec": fmt.get("vcodec"),
            }
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    languages = sorted(set(manual) | set(automatic))
    return {
        "title": info.get("title", "Unknown"),
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail", ""),
        "uploader": info.get("uploader") or info.get("channel") or "Unknown",
        "formats": sorted(video_formats.values(), key=lambda item: (item["height"], item["fps"] or 0), reverse=True),
        "subtitles": [{"language": lang, "manual": lang in manual, "automatic": lang in automatic}
                      for lang in languages if lang != "live_chat"],
        "chapters": [{"title": c.get("title") or f"Chapter {index + 1}",
                      "start_time": c.get("start_time", 0), "end_time": c.get("end_time")}
                     for index, c in enumerate(info.get("chapters") or [])],
    }


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


def _run_media_command(command: list[str]):
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        if error.returncode in {-9, 137}:
            raise RuntimeError(
                "FFmpeg was killed by the operating system. The container likely ran out of memory."
            ) from error
        details = (error.stderr or error.stdout or "Media processing failed").strip().splitlines()
        raise RuntimeError("FFmpeg failed: " + " ".join(details[-4:])) from error


class DownloadCancelled(Exception):
    pass


def _subtitle_to_segments(path: Path) -> list[dict]:
    timestamp = re.compile(
        r"(?P<start>\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
        r"(?P<end>\d{1,2}:\d{2}:\d{2}[.,]\d{3})"
    )
    segments: list[dict] = []
    current: dict | None = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        match = timestamp.search(line)
        if match:
            if current and current["text"]:
                segments.append(current)
            current = {"start": match.group("start").replace(",", "."),
                       "end": match.group("end").replace(",", "."), "text": ""}
        elif current and line and not line.isdigit() and not line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:")):
            clean = re.sub(r"<[^>]+>", "", line)
            if clean and clean not in current["text"].split("\n"):
                current["text"] += ("\n" if current["text"] else "") + clean
    if current and current["text"]:
        segments.append(current)
    return segments


def _convert_transcript(source: Path, target_format: str) -> Path:
    if target_format in {"vtt", "srt"} and source.suffix.lower() == f".{target_format}":
        return source
    segments = _subtitle_to_segments(source)
    target = source.with_suffix(f".{target_format}")
    if target_format == "json":
        target.write_text(json.dumps({"segments": segments}, ensure_ascii=False, indent=2), encoding="utf-8")
    elif target_format == "txt":
        target.write_text("\n".join(segment["text"].replace("\n", " ") for segment in segments) + "\n",
                          encoding="utf-8")
    elif target_format == "srt":
        blocks = [f"{index}\n{s['start'].replace('.', ',')} --> {s['end'].replace('.', ',')}\n{s['text']}"
                  for index, s in enumerate(segments, 1)]
        target.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    else:
        raise ValueError(f"Unsupported transcript format: {target_format}")
    if target != source:
        source.unlink(missing_ok=True)
    return target


def _job_progress_hook(job: _DownloadJob):
    def hook(data: dict):
        if job.cancel_requested:
            raise DownloadCancelled("Download cancelled")
        info = data.get("info_dict") or {}
        job.current = info.get("title") or job.current
        if job.current:
            job.title = job.current
        if data.get("status") == "downloading":
            job.downloaded_bytes = data.get("downloaded_bytes") or 0
            job.total_bytes = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            job.speed = data.get("speed") or 0
            job.eta = data.get("eta")
            if job.total_bytes:
                job.progress = min(99, job.downloaded_bytes * 100 / job.total_bytes)
        elif data.get("status") == "finished":
            job.progress = 99
    return hook


def _choose_output(work_dir: Path, requested_format: str) -> Path:
    ignored = {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl"}
    files = [path for path in work_dir.iterdir() if path.is_file() and path.suffix.lower() not in ignored]
    if not files:
        raise FileNotFoundError("Output file not found after conversion")
    if len(files) == 1:
        return files[0]
    zip_path = work_dir.parent / f"{work_dir.name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.name)
    shutil.rmtree(work_dir, ignore_errors=True)
    return zip_path


def _run_download_job(job_id: str, req: ConvertRequest):
    job = _download_jobs[job_id]
    job.status = "running"
    work_dir = DOWNLOADS_DIR / job_id
    work_dir.mkdir(exist_ok=True)
    try:
        if req.transcript_format:
            target_format = req.transcript_format.lower()
            if target_format not in {"txt", "srt", "vtt", "json"}:
                raise ValueError("Transcript format must be TXT, SRT, VTT or JSON")
            opts = {
                **base_ydl_opts(),
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [req.subtitle_lang or "pt.*", "en.*"],
                "subtitlesformat": "vtt/best",
                "outtmpl": str(work_dir / "%(title)s.%(ext)s"),
                "progress_hooks": [_job_progress_hook(job)],
                "noplaylist": False,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(req.url, download=True)
            job.title = (info or {}).get("title") or "transcript"
            sources = [p for p in work_dir.iterdir() if p.suffix.lower() in {".vtt", ".srt"}]
            if not sources:
                raise FileNotFoundError("No transcript is available in the selected language")
            for source in sources:
                _convert_transcript(source, target_format)
            output = _choose_output(work_dir, target_format)
        else:
            if req.format not in FORMAT_OPTIONS:
                raise ValueError(f"Unsupported format: {req.format}")
            output_template = str(work_dir / "%(title)s.%(ext)s")
            opts = build_ydl_opts(req.format, req.quality, output_template, req)
            opts["progress_hooks"] = [_job_progress_hook(job)]
            opts["noplaylist"] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(req.url, download=True)
            job.title = (info or {}).get("title") or "video"
            output = _choose_output(work_dir, req.format)

        job.output_path = output
        extension = output.suffix.lstrip(".")
        job.filename = f"{safe_filename(req.filename or job.title)}.{extension}"
        job.progress = 100
        job.status = "done"
    except DownloadCancelled:
        job.status = "cancelled"
        shutil.rmtree(work_dir, ignore_errors=True)


def _load_music_tracks() -> list[dict]:
    if not MUSIC_MANIFEST.exists():
        return []
    try:
        data = json.loads(MUSIC_MANIFEST.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def _save_music_tracks(tracks: list[dict]):
    temporary = MUSIC_MANIFEST.with_suffix(".tmp")
    temporary.write_text(json.dumps(tracks, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(MUSIC_MANIFEST)


def _select_music_track(track_id: str, mood: str, energy: str) -> Path | None:
    tracks = _load_music_tracks()
    if track_id and track_id != "auto":
        selected = next((track for track in tracks if track["id"] == track_id), None)
    else:
        target_bpm = {"low": 75, "medium": 105, "high": 135}.get(energy, 105)
        ranked = sorted(tracks, key=lambda track: (
            track.get("mood") != mood,
            track.get("energy") != energy,
            tempo_distance(track.get("bpm"), target_bpm),
        ))
        selected = ranked[0] if ranked else None
    if not selected:
        return None
    path = MUSIC_DIR / selected["filename"]
    return path if path.exists() else None


def _download_clip_source(job: _ClipJob, url: str, language: str) -> dict:
    assert job.work_dir
    job.phase, job.progress = "Downloading source and captions", 8

    def hook(data: dict):
        if job.cancel_requested:
            raise DownloadCancelled("Analysis cancelled")
        if data.get("status") == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            if total:
                job.progress = 8 + min(37, downloaded * 37 / total)

    opts = {
        **base_ydl_opts(),
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(job.work_dir / "source.%(ext)s"),
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [language] if language else ["pt.*", "en.*"],
        "subtitlesformat": "vtt/best",
        "progress_hooks": [hook],
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    sources = [path for path in job.work_dir.glob("source.*")
               if path.suffix.lower() in {".mp4", ".mkv", ".webm"}]
    if not sources:
        raise FileNotFoundError("Could not download the source video for analysis")
    job.source_path = sources[0]
    return info or {}


def _transcript_for_clip_job(job: _ClipJob, language: str) -> list[TranscriptSegment]:
    assert job.work_dir and job.source_path
    subtitles = sorted(job.work_dir.glob("source.*.vtt")) + sorted(job.work_dir.glob("source.*.srt"))
    if subtitles:
        return parse_subtitle_file(subtitles[0])

    whisper = which("whisper")
    if not whisper:
        raise FileNotFoundError(
            "No YouTube captions are available. Install the Whisper CLI to enable local transcription fallback."
        )
    model = os.environ.get("WHISPER_MODEL", "small")
    command = [
        whisper, str(job.source_path), "--model", model, "--output_format", "vtt",
        "--output_dir", str(job.work_dir),
    ]
    if language:
        command += ["--language", language.split("-", 1)[0]]
    subprocess.run(command, check=True, capture_output=True, text=True)
    generated = sorted(job.work_dir.glob("*.vtt"))
    if not generated:
        raise FileNotFoundError("Whisper did not produce a transcript")
    return parse_subtitle_file(generated[0])


def _run_clip_analysis(job_id: str, req: AutoClipRequest):
    job = _clip_jobs[job_id]
    job.status = "analyzing"
    job.work_dir = DOWNLOADS_DIR / f"clips-{job_id}"
    job.work_dir.mkdir(exist_ok=True)
    try:
        with _render_slots:
            info = _download_clip_source(job, req.url, req.language)
            job.title = info.get("title") or "video"
            job.phase, job.progress = "Analyzing transcript", 50
            try:
                job.segments = _transcript_for_clip_job(job, req.language)
            except FileNotFoundError:
                if req.objective != "music":
                    raise
                job.segments = []
            if not job.segments and req.objective != "music":
                raise ValueError("The transcript contains no usable speech")
            transcript_text = " ".join(segment.text for segment in job.segments)
            job.suggested_mood, job.suggested_energy = infer_mood_energy(transcript_text, req.objective)
            job.candidates = rank_clip_candidates(
                job.segments, req.objective, req.count, req.min_duration, req.max_duration
            ) if job.segments else []
            if not job.candidates and req.objective != "music":
                raise ValueError("No complete clip candidates matched the requested duration")
            if job.candidates:
                job.phase, job.progress = "Analyzing visual changes", 63
                job.candidates = boost_candidates_with_events(
                    job.candidates, detect_scene_changes(job.source_path)
                )
            job.music_blocks = []
            job.phase, job.progress = "Analyzing voice and music energy", 72
            spectral_rows = analyze_spectral_frames(job.source_path)
            if job.candidates:
                job.candidates = boost_candidates_with_audio_energy(job.candidates, spectral_rows)
            if req.detect_music:
                job.music_blocks = fit_music_blocks(
                    estimate_music_blocks(spectral_rows), req.min_duration, req.max_duration, req.count
                )
            if req.objective == "music" and not job.music_blocks:
                raise ValueError("No musical passages were detected")
            job.phase, job.progress, job.status = "Ready for review", 100, "ready"
        if req.automatic and not job.cancel_requested:
            music_indexes = list(range(len(job.music_blocks or []))) if req.objective == "music" else []
            _run_clip_render(job_id, RenderClipsRequest(
                render_speech_candidates=req.objective != "music",
                music_block_indexes=music_indexes,
                add_background_music=bool(_load_music_tracks()) and req.objective != "music",
                music_mood=job.suggested_mood,
                music_energy=job.suggested_energy,
            ))
    except Exception as error:
        job.status = "cancelled" if job.cancel_requested else "error"
        job.error = None if job.cancel_requested else str(error)


def _render_clip(job: _ClipJob, candidate: ClipCandidate, index: int,
                 req: RenderClipsRequest, music_path: Path | None) -> Path:
    assert job.work_dir and job.source_path and job.segments is not None
    source = job.source_path
    clip_start, clip_end = candidate.start, candidate.end
    subtitle_segments = job.segments
    if req.remove_silence:
        ranges = intersect_ranges(job.audible_ranges or [], candidate.start, candidate.end)
        if ranges:
            source = job.work_dir / f"clip-{index:02d}-no-silence.mp4"
            _run_media_command(build_silence_removal_command(job.source_path, source, ranges))
            subtitle_segments = compress_transcript_timeline(job.segments, ranges)
            clip_start, clip_end = 0, sum(end - start for start, end in ranges)
    subtitle_path = None
    if req.burn_subtitles and candidate.kind == "speech":
        subtitle_path = job.work_dir / f"clip-{index:02d}.ass"
        write_ass_subtitles(
            subtitle_path, subtitle_segments, clip_start, clip_end, req.subtitle_style
        )
    output = job.work_dir / f"clip-{index:02d}.mp4"
    command = build_render_command(
        source, output, clip_start, clip_end,
        req.aspect_ratio, req.reframe, subtitle_path, music_path, req.music_volume,
    )
    _run_media_command(command)
    return output


def _run_clip_render(job_id: str, req: RenderClipsRequest):
    job = _clip_jobs[job_id]
    job.status, job.phase, job.progress = "rendering", "Preparing clips", 0
    try:
        all_candidates = list(job.candidates or [])
        for block in job.music_blocks or []:
            all_candidates.append(ClipCandidate(
                block[0], block[1], 1, f"Music {block[0]:.0f}s", "Detected musical passage", "", "music"
            ))
        if req.selections:
            selected = []
            for selection in req.selections:
                if selection.end <= selection.start:
                    raise ValueError("Every clip end must be greater than its start")
                original = all_candidates[selection.index] if selection.index < len(all_candidates) else None
                if not original:
                    continue
                selected.append(ClipCandidate(
                    selection.start, selection.end, original.score,
                    selection.title or original.title, original.reason, original.transcript, original.kind,
                ))
        else:
            indexes = req.candidate_indexes or (
                list(range(len(job.candidates or []))) if req.render_speech_candidates else []
            )
            indexes += [len(job.candidates or []) + index for index in req.music_block_indexes]
            selected = [all_candidates[index] for index in dict.fromkeys(indexes) if 0 <= index < len(all_candidates)]
        if not selected:
            raise ValueError("Select at least one clip")
        music = _select_music_track(req.music_track_id, req.music_mood, req.music_energy) \
            if req.add_background_music else None
        if req.add_background_music and not music:
            raise FileNotFoundError("No licensed background track matches the selection")
        outputs: list[Path] = []
        with _render_slots:
            if req.remove_silence and job.audible_ranges is None:
                job.phase = "Detecting silence"
                duration = max(
                    max((segment.end for segment in job.segments or []), default=0),
                    max((candidate.end for candidate in selected), default=0),
                )
                job.audible_ranges = detect_audible_ranges(job.source_path, duration) if duration else []
            for index, candidate in enumerate(selected, 1):
                if job.cancel_requested:
                    raise DownloadCancelled("Rendering cancelled")
                job.phase = f"Rendering clip {index} of {len(selected)}"
                job.progress = (index - 1) * 90 / len(selected)
                outputs.append(_render_clip(job, candidate, index, req, music))
        zip_path = DOWNLOADS_DIR / f"clips-{job_id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for output in outputs:
                archive.write(output, output.name)
        job.output_path = zip_path
        job.status, job.phase, job.progress = "done", "Complete", 100
    except Exception as error:
        job.status = "cancelled" if job.cancel_requested else "error"
        job.error = None if job.cancel_requested else str(error)
    except Exception as error:
        job.status = "cancelled" if job.cancel_requested else "error"
        job.error = None if job.cancel_requested else str(error)
        shutil.rmtree(work_dir, ignore_errors=True)


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

    opts = build_ydl_opts(
        fmt, quality, str(work_dir / "%(playlist_index)03d - %(title)s.%(ext)s")
    )
    opts.update({"progress_hooks": [hook], "ignoreerrors": True})

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
        return _format_info(info)
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


@app.post("/jobs")
async def create_download_job(req: ConvertRequest):
    if not req.transcript_format and req.format not in FORMAT_OPTIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {req.format}")
    job_id = uuid.uuid4().hex
    _download_jobs[job_id] = _DownloadJob(created_at=time.time())
    threading.Thread(target=_run_download_job, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
async def download_job_status(job_id: str):
    job = _download_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "status": job.status,
        "progress": round(job.progress, 1),
        "downloaded_bytes": job.downloaded_bytes,
        "total_bytes": job.total_bytes,
        "speed": job.speed,
        "eta": job.eta,
        "current": job.current,
        "error": job.error,
        "filename": job.filename,
    }


@app.delete("/jobs/{job_id}")
async def cancel_download_job(job_id: str):
    job = _download_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in {"pending", "running"}:
        job.cancel_requested = True
    return {"status": job.status, "cancel_requested": job.cancel_requested}


@app.get("/jobs/{job_id}/download")
async def download_job_file(job_id: str, background_tasks: BackgroundTasks):
    job = _download_jobs.get(job_id)
    if not job or job.status != "done" or not job.output_path or not job.output_path.exists():
        raise HTTPException(status_code=404, detail="Download is not ready")
    output = job.output_path

    def cleanup():
        if output.parent.name == job_id:
            shutil.rmtree(output.parent, ignore_errors=True)
        else:
            delete_file_later(str(output))
        _download_jobs.pop(job_id, None)

    background_tasks.add_task(cleanup)
    return FileResponse(output, filename=job.filename, media_type="application/octet-stream")


@app.get("/music-library")
async def music_library():
    return {"tracks": _load_music_tracks()}


@app.post("/music-library")
async def upload_music_track(
    file: UploadFile = File(...),
    title: str = Form(...),
    mood: str = Form("inspiring"),
    energy: str = Form("medium"),
    bpm: float | None = Form(None),
    license_name: str = Form(...),
    licensed: bool = Form(...),
):
    if not licensed:
        raise HTTPException(status_code=400, detail="You must confirm that you are licensed to use this track")
    if not license_name.strip():
        raise HTTPException(status_code=400, detail="License/source information is required")
    extension = Path(file.filename or "").suffix.lower()
    if extension not in {".mp3", ".m4a", ".wav", ".flac", ".opus", ".ogg"}:
        raise HTTPException(status_code=400, detail="Unsupported music file")
    track_id = uuid.uuid4().hex
    filename = f"{track_id}{extension}"
    target = MUSIC_DIR / filename
    temporary = target.with_suffix(target.suffix + ".part")
    size = 0
    try:
        with temporary.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="Music files are limited to 100 MB")
                destination.write(chunk)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    track = {
        "id": track_id, "filename": filename, "title": title,
        "mood": mood, "energy": energy, "bpm": bpm, "license": license_name,
    }
    tracks = _load_music_tracks()
    tracks.append(track)
    _save_music_tracks(tracks)
    return track


@app.delete("/music-library/{track_id}")
async def delete_music_track(track_id: str):
    tracks = _load_music_tracks()
    track = next((item for item in tracks if item["id"] == track_id), None)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    (MUSIC_DIR / track["filename"]).unlink(missing_ok=True)
    _save_music_tracks([item for item in tracks if item["id"] != track_id])
    return {"ok": True}


@app.post("/clip-jobs")
async def create_clip_job(req: AutoClipRequest):
    if req.max_duration < req.min_duration:
        raise HTTPException(status_code=400, detail="Maximum duration must be greater than minimum duration")
    if req.objective not in {"highlights", "educational", "funny", "impact", "shorts", "music"}:
        raise HTTPException(status_code=400, detail="Unsupported clip objective")
    job_id = uuid.uuid4().hex
    _clip_jobs[job_id] = _ClipJob(created_at=time.time())
    threading.Thread(target=_run_clip_analysis, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


@app.get("/clip-jobs/{job_id}")
async def clip_job_status(job_id: str):
    job = _clip_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Clip job not found")
    return {
        "status": job.status, "phase": job.phase, "progress": round(job.progress, 1),
        "title": job.title, "error": job.error,
        "suggested_mood": job.suggested_mood, "suggested_energy": job.suggested_energy,
        "candidates": [candidate.to_dict() for candidate in job.candidates or []],
        "music_blocks": [{"start": start, "end": end} for start, end in job.music_blocks or []],
    }


@app.get("/clip-jobs/{job_id}/source")
async def preview_clip_source(job_id: str):
    job = _clip_jobs.get(job_id)
    if not job or job.status not in {"ready", "rendering", "done"} or not job.source_path:
        raise HTTPException(status_code=404, detail="Clip preview is not ready")
    return FileResponse(job.source_path)


@app.post("/clip-jobs/{job_id}/render")
async def render_clip_job(job_id: str, req: RenderClipsRequest):
    job = _clip_jobs.get(job_id)
    if not job or job.status != "ready":
        raise HTTPException(status_code=409, detail="Clip analysis is not ready")
    threading.Thread(target=_run_clip_render, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


@app.delete("/clip-jobs/{job_id}")
async def cancel_clip_job(job_id: str):
    job = _clip_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Clip job not found")
    job.cancel_requested = True
    return {"cancel_requested": True}


@app.get("/clip-jobs/{job_id}/download")
async def download_clips(job_id: str, background_tasks: BackgroundTasks):
    job = _clip_jobs.get(job_id)
    if not job or job.status != "done" or not job.output_path or not job.output_path.exists():
        raise HTTPException(status_code=404, detail="Clips are not ready")
    output = job.output_path

    def cleanup():
        delete_file_later(str(output))
        if job.work_dir:
            shutil.rmtree(job.work_dir, ignore_errors=True)
        _clip_jobs.pop(job_id, None)

    background_tasks.add_task(cleanup)
    return FileResponse(output, filename=f"{safe_filename(job.title)}-clips.zip", media_type="application/zip")


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
