# YouTube Converter

Web application built with FastAPI, yt-dlp and FFmpeg for downloading videos,
audio, playlists and transcripts.

## Features

- Asynchronous downloads with progress, ETA and cancellation
- Dynamic resolution discovery, including FPS, HDR and approximate file size
- MP4, WebM and MKV video
- MP3, M4A, Opus, FLAC and WAV audio
- Exact resolution selection and an H.264/AAC compatibility mode
- Manual and automatically generated transcripts in TXT, SRT, VTT or JSON
- Transcript ZIP export when a playlist URL is used
- Start/end clipping and chapter selection
- Optional splitting into one file per chapter
- Embedded thumbnail where supported, plus chapters and custom title, artist,
  album, year and track metadata (WAV and WebM do not support embedded covers)
- Playlist downloads as ZIP files
- Automatic highlight, educational, funny, impact, Shorts and music clip analysis
- Reviewable clip candidates with editable start/end timestamps
- Burned ASS captions with bold, highlight and minimal styles
- 9:16, 1:1 and 16:9 layouts with center crop or fit framing
- Optional silence removal with synchronized video, audio and captions
- Musical-passage detection based on spectral stability
- Licensed background-music library with mood, energy and BPM matching
- Background loops, fades, speech ducking and EBU loudness normalization
- Fully automatic clip generation mode

## Run locally

FFmpeg and Node.js must be installed. Node.js is used by yt-dlp's YouTube
JavaScript challenge solver.

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000`.

Docker is also supported:

```bash
docker compose up --build
```

## How downloads work

The browser creates a job with `POST /jobs` and polls `GET /jobs/{job_id}`.
This keeps long downloads from holding an HTTP request open and avoids common
proxy timeouts. A finished file is retrieved from
`GET /jobs/{job_id}/download`; `DELETE /jobs/{job_id}` requests cancellation.

`POST /info` returns the resolutions, subtitle languages and chapters actually
available for a URL. The interface uses that response instead of presenting
qualities that the video does not have.

Transcripts prefer creator-provided subtitles when available and can also use
YouTube automatic captions. TXT removes timestamps, while JSON returns a
`segments` array with `start`, `end` and `text` fields.

## Automatic clips

Paste a video URL in the main field, open **Automatic Clips**, choose an
objective and click **Analyze video**. Analysis downloads a maximum 720p working
copy to limit memory use and combines:

- transcript keyword, punctuation, density and sentence-completeness signals;
- visual scene-change density;
- spectral flatness, flux and RMS energy for musical passages.

Candidates are deliberately shown for review because “interesting” is
subjective. Their start/end values can be edited before rendering. Selecting
**Fully automatic** skips review and renders the ranked speech candidates, or
detected musical passages when the Music objective is selected.

The caption pipeline first uses creator subtitles, then YouTube automatic
captions. If neither exists, it uses a locally installed `whisper` CLI with the
model configured by `WHISPER_MODEL` (default `small`). Whisper is optional and
is not installed in the lightweight Docker image; use a custom worker image if
captionless videos must be supported.

Build the opt-in Whisper image with:

```bash
docker build --build-arg INSTALL_WHISPER=true -t youtube-converter:whisper .
```

### Licensed background music

Upload tracks through **Licensed music library** and confirm usage rights. Each
track stores mood, energy, BPM and its license/source. Automatic selection ranks
the mood and energy inferred from the spoken content first, then chooses the closest BPM (including half/double-tempo
matches). Music is looped to the clip duration, faded at both ends, lowered
during speech with sidechain compression and mixed to a normalized -16 LUFS
target. WAV and other supported audio uploads are limited to 100 MB.

Uploaded tracks are stored under `MUSIC_LIBRARY_PATH`. The Helm deployment maps
this to `/app/data/music-library`; use persistent storage in production if the
library must survive pod replacement.

### Resource controls

`MAX_RENDER_JOBS` controls concurrent analysis/render operations and defaults to
`1`. FFmpeg rendering and local speech recognition are resource-intensive. For
Whisper or multiple concurrent renders, run separate workers with at least
4–8 GiB of memory rather than raising concurrency in the default 1 GiB pod.

Automatic-clip endpoints:

- `POST /clip-jobs` starts analysis.
- `GET /clip-jobs/{id}` returns progress, candidates and detected music blocks.
- `POST /clip-jobs/{id}/render` renders reviewed selections.
- `GET /clip-jobs/{id}/download` downloads the resulting ZIP.
- `DELETE /clip-jobs/{id}` requests cancellation.
- `GET/POST /music-library` lists or uploads licensed music.

## Authentication

Some YouTube videos require authentication. Export Netscape-format cookies from
a browser session and upload the resulting `cookies.txt` in the application.
The server stores it at `COOKIES_PATH`, or `cookies.txt` by default. Treat this
file as a secret and never commit it.

## Tests

```bash
python -m unittest -v
python -m py_compile main.py test_main.py
```

The test suite covers format selection, dynamic video information, audio
pipelines and transcript conversion.

## Operational notes

- Finished files are deleted after they are served.
- Job state is in process memory. For multiple replicas or restart-safe jobs,
  move state and execution to Redis plus a worker queue.
- Downloads are intended only for content you are authorized to save. Follow
  the source platform's terms and applicable copyright law.
