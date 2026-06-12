# VRest

VRest is a Flask-based local video browser for large libraries.

It scans a configured video folder, builds a folder tree, and serves:
- On-demand thumbnails
- On-demand preview videos
- Direct streamed playback of source files
- External and embedded subtitle tracks as WebVTT
- Scrub-preview sprite sheets and metadata for timeline hover previews

## Current Implementation

The current app combines a Flask API (`app.py`) with a single-page frontend (`templates/index.html`).

Backend behavior:
- Loads `.env` settings with sane defaults.
- Validates `VIDEO_FOLDER` and creates `DATA_FOLDER` if needed.
- Detects ffmpeg capabilities (CUDA decode, NVENC encode, `scale_cuda`) and falls back to CPU when unavailable.
- Uses separate thread pools for thumbnail, preview, and scrub jobs.
- Stores a compact catalog snapshot at `DATA_FOLDER/catalog_snapshot.json` and starts background indexing on boot.
- Throttles re-scan requests (minimum interval) to avoid repeated expensive scans.

Frontend behavior:
- Folder sidebar + searchable video grid.
- Sort modes: default, A-Z, date added, length, file size, random (default).
- Item size presets and persistent UI preferences in localStorage.
- Optional hover preview mode for cards.
- Full player dialog with Plyr, scrub thumbnails, subtitle track loading, next/previous navigation, and auto-play next.
- Jobs drawer with live status, bulk generation controls, manual reindex, and orphaned-data cleanup trigger.

## Features

- Background catalog indexing with incremental API refresh.
- Lazy thumbnail generation after indexing.
- Preview generation on demand, or in bulk from the Jobs panel.
- Scrub sprite generation on demand, or in bulk from the Jobs panel.
- Subtitle discovery:
  - External `.srt` files near video files
  - Embedded subtitle streams detected via `ffprobe`
- Automatic conversion/extraction to `.vtt` subtitle cache files.
- Password-protected login with persistent session cookies (optional).

## Project Structure

- `app.py`: Flask backend, indexing, generation pipelines, API routes.
- `templates/index.html`: Main UI (tree, grid, jobs panel, player).
- `templates/login.html`: Login page when password mode is enabled.
- `static/`: Icons and web manifest.
- `.env.example`: Environment template.
- `requirements.txt`: Python dependencies.

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` on `PATH`

Without ffmpeg/ffprobe, browsing/index metadata still works, but media artifact generation (thumbnail, preview, subtitles, scrub) will fail.

## Configuration

1. Copy `.env.example` to `.env`.
2. Minimum required settings:

```env
VIDEO_FOLDER=C:/absolute/path/to/your/videos
DATA_FOLDER=./data
HOST=0.0.0.0
PORT=3232
```

3. Auth/session options:

```env
APP_PASSWORD_ENABLED=true
APP_PASSWORD=change-me
APP_SECRET_KEY=replace-with-a-long-random-secret
APP_SESSION_COOKIE_NAME=
APP_SESSION_REMEMBER_DAYS=365
```

4. Performance and quality options (optional):

```env
USE_GPU=true
FFMPEG_CPU_THREADS=0
THUMBNAIL_MAX_WORKERS=
PREVIEW_MAX_WORKERS=
SCRUB_MAX_WORKERS=
THUMBNAIL_JPEG_Q=6
SCRUB_JPEG_Q=8
PREVIEW_NVENC_PRESET=p1
PREVIEW_NVENC_CQ=34
PREVIEW_LIBX264_PRESET=ultrafast
PREVIEW_LIBX264_CRF=32
PREVIEW_START_OFFSET_SECONDS=1
PREVIEW_MIN_TOTAL_SECONDS=10
PREVIEW_MAX_TOTAL_SECONDS=30
PREVIEW_DURATION_AT_MAX_SECONDS=3600
PREVIEW_SAMPLE_INTERVAL_SECONDS=300
PREVIEW_MIN_SEGMENTS=3
PREVIEW_MAX_SEGMENTS=12
PREVIEW_MIN_SEGMENT_SECONDS=1.0
PREVIEW_MAX_SEGMENT_SECONDS=4.0
PREVIEW_EDGE_GUARD_MIN_SECONDS=8
PREVIEW_EDGE_GUARD_RATIO=0.02
PREVIEW_EDGE_GUARD_MAX_SECONDS=180
```

Notes:
- Auth is enforced only when both `APP_PASSWORD_ENABLED=true` and `APP_PASSWORD` is non-empty.
- If `APP_SECRET_KEY` is omitted, a temporary key is generated at runtime (existing login sessions become invalid after restart).
- Default cookie name is `vrest_session_<PORT>` unless `APP_SESSION_COOKIE_NAME` is set.
- `FFMPEG_CPU_THREADS=0` allows ffmpeg to auto-select CPU threads.
- `USE_GPU=false` forces CPU-only generation paths.

## Run (Python)

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py
```

Open `http://localhost:3232` (or your configured port).

## HTTP Endpoints

Pages and static assets:
- `GET /`: Main app shell.
- `GET /login`: Login page (when auth is enabled).
- `GET /logout`: Clear auth session.
- `GET /favicon.ico`
- `GET /apple-touch-icon.png`
- `GET /site.webmanifest`

Catalog and indexing:
- `GET /api/browse`: Folder tree, files, indexing state, ffmpeg availability.
- `POST /api/reindex`: Force a fresh catalog scan.

Playback and artifacts:
- `GET /api/video/<path:rel_path>`: Stream original video file.
- `GET /api/thumbnail/<path:rel_path>`: Return thumbnail or `202` while queued.
- `GET /api/preview/<path:rel_path>`: Return preview MP4 (generates if missing).
- `GET /api/subtitle/<path:rel_path>/<track_id>.vtt`: Return cached/generated subtitle track.

Scrub previews:
- `GET /api/scrub/<path:rel_path>/metadata`: Scrub metadata JSON.
- `GET /api/scrub/<path:rel_path>/sprite/<int:sheet_index>.jpg`: Scrub sprite sheet image.
- `POST /api/scrub/pregenerate`: Start bulk scrub generation.
- `GET /api/scrub/pregenerate`: Bulk scrub job status.

Preview bulk generation:
- `POST /api/preview/pregenerate`: Start bulk preview generation.
- `GET /api/preview/pregenerate`: Bulk preview job status.

Generation status and maintenance:
- `GET /api/status/<path:rel_path>`: Per-video generation states.
- `GET /api/status/all`: Global generation summary + job states.
- `POST /api/data/cleanup`: Remove orphaned cached files/directories for missing source videos.

All `/api/*` routes require authentication when password mode is enabled.

## Generation and Cache Layout

Generated files are stored under `DATA_FOLDER` using a per-video directory that mirrors source paths.

For a source video at `<VIDEO_FOLDER>/Movies/Example.mp4`, cache files are written to:

```text
<DATA_FOLDER>/Movies/Example.mp4/thumbnail.jpg
<DATA_FOLDER>/Movies/Example.mp4/preview.mp4
<DATA_FOLDER>/Movies/Example.mp4/subtitles/<track>.vtt
<DATA_FOLDER>/Movies/Example.mp4/scrubbing/metadata.json
<DATA_FOLDER>/Movies/Example.mp4/scrubbing/sheet_0000.jpg
```

Artifacts are considered stale and regenerated when source files are newer.

## Authentication Behavior

- Login is session-based using Flask secure cookies.
- Session persistence is controlled by `APP_SESSION_REMEMBER_DAYS`.
- When auth is disabled, login is bypassed and all routes are directly accessible.

## Development Notes

- Startup attempts to load the previous catalog snapshot before background reindex begins.
- Preview generation uses sampled segments to build a short highlight clip and uses CPU fallback if GPU pipelines fail.
- Scrub metadata payloads are normalized on read to keep legacy data compatible.
