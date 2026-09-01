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
