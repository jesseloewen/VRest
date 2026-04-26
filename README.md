# VRest

VRest is a Flask-based local video browser for large libraries.

It scans a configured video folder, renders a folder tree UI, and serves:
- On-demand thumbnails
- On-demand previews
- Streamed playback
- Subtitle extraction and conversion to VTT
- Scrub-preview sprite sheets for timeline preview

## Features

- Fast folder + file browsing with search and sorting
- Generation jobs panel (right-side drawer) with live status for thumbnail/preview/scrub
- Lazy thumbnail generation plus on-demand preview/scrub generation
- Optional bulk generation actions from UI for previews and scrubbing
- ffmpeg hardware acceleration support (CUDA/NVENC/scale_cuda) with CPU fallback
- Subtitle support:
  - External `.srt` files next to videos
  - Embedded subtitle tracks extracted to `.vtt`
- Optional password login
- Background indexing and generation jobs
- Pinokio script support (`install.js`, `start.js`, `reset.js`)

## Project Structure

- `app.py`: Flask backend, media generation, cache handling, APIs
- `templates/index.html`: Main single-page UI (tree, grid, player)
- `templates/login.html`: Optional auth page
- `requirements.txt`: Python dependencies
- `pinokio.js`: Pinokio menu/runtime integration
- `install.js`: Creates venv and installs dependencies
- `start.js`: Runs the Flask server and publishes URL to Pinokio
- `reset.js`: Removes venv and local generated data cache
- `.env.example`: Environment variable template

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` available on `PATH`

Without ffmpeg/ffprobe, browsing works but thumbnails/previews/subtitles/scrub assets cannot be generated.

## Configuration

1. Copy `.env.example` to `.env`.
2. Set at least:

```env
VIDEO_FOLDER=C:/absolute/path/to/your/videos
DATA_FOLDER=./data
HOST=0.0.0.0
PORT=3232
```

3. Auth/session settings:

```env
APP_PASSWORD_ENABLED=true
APP_PASSWORD=change-me
APP_SECRET_KEY=replace-with-a-long-random-secret
APP_SESSION_COOKIE_NAME=vrest_session_custom
APP_SESSION_REMEMBER_DAYS=365
```

4. Hardware/performance settings (optional):

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
```

Notes:
- Authentication is enabled by default when APP_PASSWORD_ENABLED is true and APP_PASSWORD is set.
- Set APP_PASSWORD_ENABLED=false to disable password auth entirely.
- If APP_PASSWORD is empty, authentication is effectively disabled.
- APP_SESSION_REMEMBER_DAYS controls how long login is remembered across browser restarts.
- Set USE_GPU=false to force CPU-only generation even if NVIDIA hardware is detected.
- If APP_SECRET_KEY is omitted, a temporary key is generated for the current process.
- FFMPEG_CPU_THREADS=0 lets ffmpeg auto-select thread usage.

## Run (Python)

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py
```

Open `http://localhost:3232` (or your configured port).

## Run (Pinokio)

- `install.js`: bootstrap environment
- `start.js`: launch server
- `reset.js`: clean environment/cache data

## HTTP Endpoints

- `GET /`: Main UI
- `GET /login`: Login page (if password enabled)
- `GET /logout`: Clear session
- `GET /api/browse`: Catalog tree + file metadata
- `GET /api/video/<path>`: Stream original video file
- `GET /api/thumbnail/<path>`: Thumbnail JPEG (or `202` while generating)
- `GET /api/preview/<path>`: Preview MP4
- `POST /api/preview/pregenerate`: Start bulk preview generation
- `GET /api/preview/pregenerate`: Bulk preview generation status
- `GET /api/status/<path>`: Generation status for thumbnail/preview/scrub
- `GET /api/status/all`: Aggregate generation status for all indexed videos
- `GET /api/subtitle/<path>/<track_id>.vtt`: Cached/extracted subtitle track
- `GET /api/scrub/<path>/metadata`: Scrub metadata JSON
- `GET /api/scrub/<path>/sprite/<sheet>.jpg`: Scrub sprite sheet
- `POST /api/scrub/pregenerate`: Start bulk scrub generation
- `GET /api/scrub/pregenerate`: Bulk scrub generation status

## Generation Behavior

- Thumbnail generation is triggered when library items are loaded.
- Preview generation is on demand (video card preview/open playback) or via bulk action.
- Scrub generation is on demand (scrub metadata/sprite access) or via bulk action.
- Jobs panel actions:
  - Generate All Previews
  - Generate All Scrubbing
- Jobs panel displays per-item state and aggregate counts from /api/status/all.

## Caching Behavior

Generated artifacts are stored under `DATA_FOLDER`:
- `thumbnails/`
- `previews/`
- `subtitles/`
- `scrubbing/`

Files are regenerated when source files are newer than cache outputs.

## Development Notes

- Backend uses thread pools for generation jobs.
- Catalog refresh is throttled to reduce repeated expensive scans.
- Frontend uses Plyr for playback and fullscreen controls.
- Preview and scrub artifacts are generated on demand or via manual bulk generation.
