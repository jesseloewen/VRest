import atexit
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".wmv",
    ".flv",
    ".m4v",
}

VIDEO_EXTENSION_PATTERN = "|".join(
    sorted(
        (re.escape(ext.lstrip(".")) for ext in ALLOWED_EXTENSIONS),
        key=len,
        reverse=True,
    )
)
SUBTITLE_CACHE_VIDEO_RE = re.compile(
    rf"^(?P<video>.+\.(?:{VIDEO_EXTENSION_PATTERN}))(?:\..+)?$",
    re.IGNORECASE,
)

VIDEO_FOLDER = os.getenv("VIDEO_FOLDER", "").strip()
DATA_FOLDER = os.getenv("DATA_FOLDER", "./data").strip() or "./data"
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "3232"))
USE_GPU = os.getenv("USE_GPU", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
APP_PASSWORD_ENABLED = os.getenv("APP_PASSWORD_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "").strip()
APP_SESSION_COOKIE_NAME = os.getenv("APP_SESSION_COOKIE_NAME", "").strip()

CONFIG_ERROR: Optional[str] = None
VIDEO_ROOT: Optional[Path] = None

if not VIDEO_FOLDER:
    CONFIG_ERROR = "VIDEO_FOLDER is required. Set it in your environment or .env file."
else:
    video_candidate = Path(VIDEO_FOLDER).expanduser()
    if not video_candidate.exists() or not video_candidate.is_dir():
        CONFIG_ERROR = f"VIDEO_FOLDER is not a directory: {video_candidate}"
    else:
        VIDEO_ROOT = video_candidate.resolve()

DATA_ROOT = Path(DATA_FOLDER).expanduser().resolve()
DATA_ROOT.mkdir(parents=True, exist_ok=True)
CATALOG_SNAPSHOT_PATH = DATA_ROOT / "catalog_snapshot.json"
CATALOG_SNAPSHOT_VERSION = 1

LEGACY_THUMBNAIL_ROOT = DATA_ROOT / "thumbnails"
LEGACY_PREVIEW_ROOT = DATA_ROOT / "previews"
LEGACY_SUBTITLE_ROOT = DATA_ROOT / "subtitles"
LEGACY_SCRUB_ROOT = DATA_ROOT / "scrubbing"

FFMPEG_AVAILABLE = subprocess.call(
    ["where" if os.name == "nt" else "which", "ffmpeg"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
) == 0
FFPROBE_AVAILABLE = subprocess.call(
    ["where" if os.name == "nt" else "which", "ffprobe"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
) == 0

if not FFMPEG_AVAILABLE or not FFPROBE_AVAILABLE:
    LOGGER.warning("ffmpeg/ffprobe not found on PATH; media generation will fail until installed.")


def ffmpeg_supports_token(command: List[str], token: str) -> bool:
    if not FFMPEG_AVAILABLE:
        return False
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False

    text_blob = f"{result.stdout}\n{result.stderr}".lower()
    return token.lower() in text_blob


DETECTED_NVIDIA_CUDA = ffmpeg_supports_token(["ffmpeg", "-hide_banner", "-hwaccels"], "cuda")
DETECTED_NVIDIA_NVENC = ffmpeg_supports_token(
    ["ffmpeg", "-hide_banner", "-encoders"], "h264_nvenc"
)
DETECTED_NVIDIA_SCALE_CUDA = ffmpeg_supports_token(
    ["ffmpeg", "-hide_banner", "-filters"], "scale_cuda"
)

NVIDIA_CUDA_AVAILABLE = USE_GPU and DETECTED_NVIDIA_CUDA
NVIDIA_NVENC_AVAILABLE = USE_GPU and DETECTED_NVIDIA_NVENC
NVIDIA_SCALE_CUDA_AVAILABLE = USE_GPU and DETECTED_NVIDIA_SCALE_CUDA

if not USE_GPU:
    LOGGER.warning("USE_GPU is disabled; forcing CPU-only media generation.")

if NVIDIA_CUDA_AVAILABLE:
    LOGGER.info("NVIDIA CUDA decode available for ffmpeg.")
else:
    LOGGER.warning("NVIDIA CUDA decode unavailable; using CPU fallback for some jobs.")

if NVIDIA_CUDA_AVAILABLE and NVIDIA_SCALE_CUDA_AVAILABLE:
    LOGGER.info("NVIDIA scale_cuda available for scrub generation.")
elif NVIDIA_CUDA_AVAILABLE:
    LOGGER.warning("NVIDIA scale_cuda unavailable; scrub generation will use CPU scaling.")

if NVIDIA_NVENC_AVAILABLE:
    LOGGER.info("NVIDIA NVENC encode available for preview generation.")
else:
    LOGGER.warning("NVIDIA NVENC unavailable; using libx264 fallback for preview generation.")

CPU_CORES = max(1, os.cpu_count() or 1)


def _env_float(name: str, default: float, min_value: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(min_value, value)


def _env_int(name: str, default: int, min_value: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(min_value, value)


APP_SESSION_REMEMBER_DAYS = _env_int("APP_SESSION_REMEMBER_DAYS", 365)


THUMBNAIL_MAX_WORKERS = _env_int("THUMBNAIL_MAX_WORKERS", min(32, CPU_CORES * 2))
PREVIEW_MAX_WORKERS = _env_int(
    "PREVIEW_MAX_WORKERS",
    min(16, max(2, CPU_CORES)) if NVIDIA_NVENC_AVAILABLE else min(8, max(1, CPU_CORES // 2)),
)
SCRUB_MAX_WORKERS = _env_int(
    "SCRUB_MAX_WORKERS",
    min(8, max(2, CPU_CORES // 2)) if NVIDIA_SCALE_CUDA_AVAILABLE else min(4, max(1, CPU_CORES // 2)),
)

THUMBNAIL_JPEG_Q = _env_int("THUMBNAIL_JPEG_Q", 6)
SCRUB_JPEG_Q = _env_int("SCRUB_JPEG_Q", 8)
PREVIEW_NVENC_CQ = _env_int("PREVIEW_NVENC_CQ", 34)
PREVIEW_LIBX264_CRF = _env_int("PREVIEW_LIBX264_CRF", 32)
PREVIEW_NVENC_PRESET = os.getenv("PREVIEW_NVENC_PRESET", "p1").strip() or "p1"
PREVIEW_LIBX264_PRESET = os.getenv("PREVIEW_LIBX264_PRESET", "ultrafast").strip() or "ultrafast"
PREVIEW_START_OFFSET_SECONDS = _env_int("PREVIEW_START_OFFSET_SECONDS", 1, min_value=0)
PREVIEW_MIN_TOTAL_SECONDS = _env_float("PREVIEW_MIN_TOTAL_SECONDS", 10.0, min_value=1.0)
PREVIEW_MAX_TOTAL_SECONDS = _env_float(
    "PREVIEW_MAX_TOTAL_SECONDS", 30.0, min_value=PREVIEW_MIN_TOTAL_SECONDS
)
PREVIEW_DURATION_AT_MAX_SECONDS = _env_float(
    "PREVIEW_DURATION_AT_MAX_SECONDS", 3600.0, min_value=1.0
)
PREVIEW_SAMPLE_INTERVAL_SECONDS = _env_float(
    "PREVIEW_SAMPLE_INTERVAL_SECONDS", 300.0, min_value=1.0
)
PREVIEW_MIN_SEGMENTS = _env_int("PREVIEW_MIN_SEGMENTS", 3, min_value=1)
PREVIEW_MAX_SEGMENTS = _env_int(
    "PREVIEW_MAX_SEGMENTS", 12, min_value=PREVIEW_MIN_SEGMENTS
)
PREVIEW_MIN_SEGMENT_SECONDS = _env_float("PREVIEW_MIN_SEGMENT_SECONDS", 1.0, min_value=0.2)
PREVIEW_MAX_SEGMENT_SECONDS = _env_float(
    "PREVIEW_MAX_SEGMENT_SECONDS", 4.0, min_value=PREVIEW_MIN_SEGMENT_SECONDS
)
PREVIEW_EDGE_GUARD_MIN_SECONDS = _env_float(
    "PREVIEW_EDGE_GUARD_MIN_SECONDS", 8.0, min_value=0.0
)
PREVIEW_EDGE_GUARD_RATIO = _env_float("PREVIEW_EDGE_GUARD_RATIO", 0.02, min_value=0.0)
PREVIEW_EDGE_GUARD_MAX_SECONDS = _env_float(
    "PREVIEW_EDGE_GUARD_MAX_SECONDS",
    180.0,
    min_value=PREVIEW_EDGE_GUARD_MIN_SECONDS,
)
FFMPEG_CPU_THREADS = os.getenv("FFMPEG_CPU_THREADS", "0").strip() or "0"

LOGGER.info(
    "Generation workers configured: thumbnails=%d previews=%d scrubbing=%d (cpu_cores=%d)",
    THUMBNAIL_MAX_WORKERS,
    PREVIEW_MAX_WORKERS,
    SCRUB_MAX_WORKERS,
    CPU_CORES,
)

app = Flask(__name__)

if not APP_SECRET_KEY:
    APP_SECRET_KEY = secrets.token_urlsafe(32)
    LOGGER.warning("APP_SECRET_KEY not set; using a temporary key for this process.")

app.secret_key = APP_SECRET_KEY
default_session_cookie_name = f"vrest_session_{PORT}"
session_cookie_name = APP_SESSION_COOKIE_NAME or default_session_cookie_name
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_NAME"] = session_cookie_name
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=APP_SESSION_REMEMBER_DAYS)

catalog_lock = threading.Lock()
catalog_tree: Dict = {"name": "Videos", "path": "", "folders": [], "files": []}
catalog_files: List[Dict] = []
subtitle_index: Dict[str, Dict[str, Dict[str, str]]] = {}
catalog_state_lock = threading.Lock()
catalog_refresh_in_progress = False
catalog_has_completed_scan = False
catalog_last_scan_ts = 0.0
CATALOG_REFRESH_MIN_INTERVAL_SECONDS = 12

generation_lock = threading.Lock()
generation_status: Dict[str, Dict[str, str]] = {}
thumbnail_executor = ThreadPoolExecutor(max_workers=THUMBNAIL_MAX_WORKERS)
preview_executor = ThreadPoolExecutor(max_workers=PREVIEW_MAX_WORKERS)
scrub_executor = ThreadPoolExecutor(max_workers=SCRUB_MAX_WORKERS)

SCRUB_INTERVAL_SECONDS = 10
SCRUB_TILE_COLUMNS = 10
SCRUB_TILE_ROWS = 10
SCRUB_CELL_WIDTH = 160
SCRUB_CELL_HEIGHT = 90

scrub_pregen_lock = threading.Lock()
scrub_pregen_state: Dict[str, object] = {
    "running": False,
    "total": 0,
    "done": 0,
    "failed": 0,
    "startedAt": None,
    "finishedAt": None,
}

preview_pregen_lock = threading.Lock()
preview_pregen_state: Dict[str, object] = {
    "running": False,
    "total": 0,
    "done": 0,
    "failed": 0,
    "startedAt": None,
    "finishedAt": None,
}


def auth_enabled() -> bool:
    return APP_PASSWORD_ENABLED and bool(APP_PASSWORD)


def is_authenticated() -> bool:
    return (not auth_enabled()) or bool(session.get("authenticated"))


def normalize_rel_path(rel_path: str) -> str:
    return PurePosixPath(rel_path.replace("\\", "/")).as_posix().lstrip("/")


def is_video_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS


def resolve_video_path(rel_path: str) -> Optional[Path]:
    if VIDEO_ROOT is None:
        return None
    normalized = normalize_rel_path(rel_path)
    candidate = (VIDEO_ROOT / normalized).resolve()
    try:
        candidate.relative_to(VIDEO_ROOT)
    except ValueError:
        return None
    if not is_video_file(candidate):
        return None
    return candidate


def video_cache_root(rel_path: str) -> Path:
    safe_rel = normalize_rel_path(rel_path)
    target = DATA_ROOT / safe_rel
    target.mkdir(parents=True, exist_ok=True)
    return target


def thumbnail_cache_path(rel_path: str) -> Path:
    return video_cache_root(rel_path) / "thumbnail.jpg"


def preview_cache_path(rel_path: str) -> Path:
    return video_cache_root(rel_path) / "preview.mp4"


def scrub_cache_root(rel_path: str) -> Path:
    target = video_cache_root(rel_path) / "scrubbing"
    target.mkdir(parents=True, exist_ok=True)
    return target


def scrub_metadata_cache_path(rel_path: str) -> Path:
    return scrub_cache_root(rel_path) / "metadata.json"


def scrub_sprite_dir(rel_path: str) -> Path:
    return scrub_cache_root(rel_path)


def subtitle_cache_root(rel_path: str) -> Path:
    target = video_cache_root(rel_path) / "subtitles"
    target.mkdir(parents=True, exist_ok=True)
    return target


def subtitle_cache_path(rel_path: str, track_id: str) -> Path:
    safe_track = re.sub(r"[^a-zA-Z0-9._-]", "_", track_id)
    return subtitle_cache_root(rel_path) / f"{safe_track}.vtt"


def ffmpeg_hwaccel_input_args() -> List[str]:
    if not NVIDIA_CUDA_AVAILABLE:
        return []
    return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]


def ffmpeg_cpu_threads_args() -> List[str]:
    return ["-threads", FFMPEG_CPU_THREADS]


def preview_scale_filter() -> str:
    if NVIDIA_CUDA_AVAILABLE and NVIDIA_NVENC_AVAILABLE and NVIDIA_SCALE_CUDA_AVAILABLE:
        return "scale_cuda=480:-2"
    return "scale=480:-2"


def _cached_video_rel_path(cache_root: Path, cache_path: Path, suffix: str) -> Optional[str]:
    rel = normalize_rel_path(cache_path.relative_to(cache_root).as_posix())
    if not rel.lower().endswith(suffix.lower()):
        return None
    return normalize_rel_path(rel[: -len(suffix)])


def _subtitle_cached_video_rel_path(cache_root: Path, cache_path: Path) -> Optional[str]:
    rel = normalize_rel_path(cache_path.relative_to(cache_root).as_posix())
    if not rel.lower().endswith(".vtt"):
        return None
    stem = rel[:-4]
    match = SUBTITLE_CACHE_VIDEO_RE.match(stem)
    if not match:
        return None
    return normalize_rel_path(match.group("video"))


def _new_layout_video_rel_path(
    cache_path: Path,
    expected_name: Optional[str] = None,
    expected_parent: Optional[str] = None,
) -> Optional[str]:
    try:
        rel = cache_path.relative_to(DATA_ROOT)
    except ValueError:
        return None

    rel_parts = rel.parts
    if not rel_parts:
        return None

    if expected_name and rel_parts[-1].lower() != expected_name.lower():
        return None

    if expected_parent:
        if len(rel_parts) < 2 or rel_parts[-2].lower() != expected_parent.lower():
            return None
        video_parts = rel_parts[:-2]
    else:
        video_parts = rel_parts[:-1]

    if not video_parts:
        return None

    return normalize_rel_path(PurePosixPath(*video_parts).as_posix())


def _prune_empty_dirs(root: Path) -> int:
    removed = 0
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            next(directory.iterdir())
        except StopIteration:
            try:
                directory.rmdir()
                removed += 1
            except OSError:
                continue
        except OSError:
            continue
    return removed


def cleanup_orphaned_data_files() -> Dict[str, int]:
    if VIDEO_ROOT is None:
        return {
            "videosTracked": 0,
            "filesDeleted": 0,
            "directoriesDeleted": 0,
            "thumbnailsDeleted": 0,
            "previewsDeleted": 0,
            "subtitlesDeleted": 0,
            "scrubMetadataDeleted": 0,
            "scrubSpritesDeleted": 0,
            "generationStatusRemoved": 0,
            "subtitleIndexRemoved": 0,
        }

    valid_videos = {
        normalize_rel_path(path.relative_to(VIDEO_ROOT).as_posix()).lower()
        for path in VIDEO_ROOT.rglob("*")
        if is_video_file(path)
    }

    result = {
        "videosTracked": len(valid_videos),
        "filesDeleted": 0,
        "directoriesDeleted": 0,
        "thumbnailsDeleted": 0,
        "previewsDeleted": 0,
        "subtitlesDeleted": 0,
        "scrubMetadataDeleted": 0,
        "scrubSpritesDeleted": 0,
        "generationStatusRemoved": 0,
        "subtitleIndexRemoved": 0,
    }

    def delete_file(path: Path, counter_key: str) -> None:
        try:
            path.unlink()
        except OSError:
            return
        result[counter_key] += 1
        result["filesDeleted"] += 1

    for thumb_path in DATA_ROOT.rglob("thumbnail.jpg"):
        if not thumb_path.is_file():
            continue
        rel_video = _new_layout_video_rel_path(thumb_path, expected_name="thumbnail.jpg")
        if rel_video and rel_video.lower() not in valid_videos:
            delete_file(thumb_path, "thumbnailsDeleted")

    for preview_path in DATA_ROOT.rglob("preview.mp4"):
        if not preview_path.is_file():
            continue
        rel_video = _new_layout_video_rel_path(preview_path, expected_name="preview.mp4")
        if rel_video and rel_video.lower() not in valid_videos:
            delete_file(preview_path, "previewsDeleted")

    for subtitle_path in DATA_ROOT.rglob("*.vtt"):
        if not subtitle_path.is_file():
            continue

        rel_video = _new_layout_video_rel_path(subtitle_path, expected_parent="subtitles")
        if rel_video and rel_video.lower() not in valid_videos:
            delete_file(subtitle_path, "subtitlesDeleted")
            continue

        try:
            subtitle_path.relative_to(LEGACY_SUBTITLE_ROOT)
            in_legacy_subtitles = True
        except ValueError:
            in_legacy_subtitles = False

        if in_legacy_subtitles:
            rel_video = _subtitle_cached_video_rel_path(LEGACY_SUBTITLE_ROOT, subtitle_path)
            if rel_video and rel_video.lower() not in valid_videos:
                delete_file(subtitle_path, "subtitlesDeleted")

    for metadata_path in DATA_ROOT.rglob("metadata.json"):
        if not metadata_path.is_file():
            continue
        rel_video = _new_layout_video_rel_path(
            metadata_path,
            expected_name="metadata.json",
            expected_parent="scrubbing",
        )
        if rel_video and rel_video.lower() not in valid_videos:
            delete_file(metadata_path, "scrubMetadataDeleted")

    for sprite_path in DATA_ROOT.rglob("sheet_*.jpg"):
        if not sprite_path.is_file():
            continue
        rel_video = _new_layout_video_rel_path(sprite_path, expected_parent="scrubbing")
        if rel_video and rel_video.lower() not in valid_videos:
            delete_file(sprite_path, "scrubSpritesDeleted")

    if LEGACY_THUMBNAIL_ROOT.exists():
        for thumb_path in LEGACY_THUMBNAIL_ROOT.rglob("*.jpg"):
            if not thumb_path.is_file():
                continue
            rel_video = _cached_video_rel_path(LEGACY_THUMBNAIL_ROOT, thumb_path, ".jpg")
            if rel_video and rel_video.lower() not in valid_videos:
                delete_file(thumb_path, "thumbnailsDeleted")

    if LEGACY_PREVIEW_ROOT.exists():
        for preview_path in LEGACY_PREVIEW_ROOT.rglob("*.mp4"):
            if not preview_path.is_file():
                continue
            rel_video = _cached_video_rel_path(LEGACY_PREVIEW_ROOT, preview_path, ".mp4")
            if rel_video and rel_video.lower() not in valid_videos:
                delete_file(preview_path, "previewsDeleted")

    if LEGACY_SCRUB_ROOT.exists():
        for metadata_path in LEGACY_SCRUB_ROOT.rglob("*.json"):
            if not metadata_path.is_file():
                continue
            rel_video = _cached_video_rel_path(LEGACY_SCRUB_ROOT, metadata_path, ".json")
            if rel_video and rel_video.lower() not in valid_videos:
                delete_file(metadata_path, "scrubMetadataDeleted")

        for sprite_dir in LEGACY_SCRUB_ROOT.rglob("*.sprites"):
            if not sprite_dir.is_dir():
                continue
            rel = normalize_rel_path(sprite_dir.relative_to(LEGACY_SCRUB_ROOT).as_posix())
            if not rel.lower().endswith(".sprites"):
                continue
            rel_video = normalize_rel_path(rel[: -len(".sprites")])
            if rel_video.lower() in valid_videos:
                continue
            try:
                shutil.rmtree(sprite_dir)
            except OSError:
                continue
            result["scrubSpritesDeleted"] += 1
            result["directoriesDeleted"] += 1

    for cache_root in (
        DATA_ROOT,
        LEGACY_THUMBNAIL_ROOT,
        LEGACY_PREVIEW_ROOT,
        LEGACY_SUBTITLE_ROOT,
        LEGACY_SCRUB_ROOT,
    ):
        if cache_root.exists():
            result["directoriesDeleted"] += _prune_empty_dirs(cache_root)

    with generation_lock:
        stale_generation_keys = [
            key for key in generation_status.keys() if normalize_rel_path(key).lower() not in valid_videos
        ]
        for key in stale_generation_keys:
            generation_status.pop(key, None)
        result["generationStatusRemoved"] = len(stale_generation_keys)

    with catalog_lock:
        stale_subtitle_keys = [
            key for key in subtitle_index.keys() if normalize_rel_path(key).lower() not in valid_videos
        ]
        for key in stale_subtitle_keys:
            subtitle_index.pop(key, None)
        result["subtitleIndexRemoved"] = len(stale_subtitle_keys)

    return result


def run_ffmpeg_with_optional_fallback(
    primary_cmd: List[str], fallback_cmd: Optional[List[str]] = None, timeout: int = 240
) -> Tuple[bool, str]:
    try:
        first_result = subprocess.run(
            primary_cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "ffmpeg command timed out"

    if first_result.returncode == 0:
        return True, ""

    if fallback_cmd is None:
        return False, first_result.stderr or first_result.stdout

    try:
        second_result = subprocess.run(
            fallback_cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "ffmpeg fallback command timed out"

    if second_result.returncode == 0:
        return True, ""

    return False, second_result.stderr or second_result.stdout


def normalize_language_code(value: str) -> str:
    lowered = (value or "").strip().lower().replace("_", "-")
    if not lowered:
        return "und"

    alias_map = {
        "eng": "en",
        "english": "en",
        "fre": "fr",
        "fra": "fr",
        "ger": "de",
        "deu": "de",
        "spa": "es",
        "esl": "es",
        "ita": "it",
        "jpn": "ja",
        "japanese": "ja",
        "kor": "ko",
        "korean": "ko",
        "zho": "zh",
        "chi": "zh",
        "chinese": "zh",
    }
    if lowered in alias_map:
        return alias_map[lowered]

    tokens = re.split(r"[^a-z0-9]+", lowered)
    token = tokens[0] if tokens and tokens[0] else lowered
    if token in alias_map:
        return alias_map[token]
    if re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", lowered):
        return lowered
    if re.fullmatch(r"[a-z]{3}", token):
        return token
    return token[:2] if len(token) >= 2 else "und"


def is_english_language(language_code: str, label: str = "") -> bool:
    lang = normalize_language_code(language_code)
    if lang == "en" or lang.startswith("en-"):
        return True
    return "english" in (label or "").strip().lower()


def language_label(language_code: str) -> str:
    lang = normalize_language_code(language_code)
    labels = {
        "en": "English",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "it": "Italian",
        "pt": "Portuguese",
        "ru": "Russian",
        "ja": "Japanese",
        "ko": "Korean",
        "zh": "Chinese",
        "ar": "Arabic",
        "hi": "Hindi",
        "tr": "Turkish",
        "nl": "Dutch",
        "sv": "Swedish",
    }
    if lang in labels:
        return labels[lang]
    if lang == "und":
        return "Unknown"
    if len(lang) == 2:
        return lang.upper()
    return lang


def guess_external_subtitle_language(video_stem: str, subtitle_path: Path) -> Tuple[str, str]:
    suffix_part = subtitle_path.stem[len(video_stem) :].lstrip("._- ")
    if not suffix_part:
        return "und", "External"

    tokens = [token for token in re.split(r"[._\-\s]+", suffix_part) if token]
    if not tokens:
        return "und", "External"

    lang_code = normalize_language_code(tokens[0])
    label_tokens = [token for token in tokens[1:] if token]
    if not label_tokens and lang_code != "und":
        label = language_label(lang_code)
    elif not label_tokens:
        label = suffix_part
    else:
        label = " ".join(label_tokens)

    return lang_code, label


def probe_embedded_subtitle_sources(video_path: Path) -> List[Dict[str, str]]:
    if not FFPROBE_AVAILABLE:
        return []

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "s",
        "-show_entries",
        "stream=index,codec_name:stream_tags=language,title",
        "-of",
        "json",
        str(video_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return []

    if result.returncode != 0:
        return []

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return []

    streams = payload.get("streams", [])
    discovered: List[Dict[str, str]] = []
    for stream_pos, stream in enumerate(streams):
        tags = stream.get("tags") or {}
        lang = normalize_language_code(str(tags.get("language") or "und"))
        title = str(tags.get("title") or "").strip()
        label = title or language_label(lang)
        codec_name = str(stream.get("codec_name") or "").strip().lower()
        stream_index = int(stream.get("index", stream_pos))
        track_id = f"emb-{stream_pos}-{lang}"
        discovered.append(
            {
                "id": track_id,
                "kind": "embedded",
                "lang": lang,
                "label": label,
                "stream_pos": str(stream_pos),
                "stream_index": str(stream_index),
                "codec": codec_name,
            }
        )

    return discovered


def discover_external_subtitle_sources(video_path: Path, rel_video_path: str) -> List[Dict[str, str]]:
    stem = video_path.stem
    parent = video_path.parent
    discovered: List[Dict[str, str]] = []

    for path in sorted(parent.glob("*.srt")):
        if not path.is_file():
            continue
        name = path.name
        if name != f"{stem}.srt" and not name.startswith(f"{stem}."):
            continue

        lang, label = guess_external_subtitle_language(stem, path)
        track_name = path.stem[len(stem) :].lstrip("._- ") or "external"
        safe_track_name = re.sub(r"[^a-zA-Z0-9._-]", "_", track_name.lower())
        track_id = f"ext-{safe_track_name}"

        rel_srt = normalize_rel_path(path.relative_to(VIDEO_ROOT).as_posix()) if VIDEO_ROOT else ""
        discovered.append(
            {
                "id": track_id,
                "kind": "external",
                "lang": lang,
                "label": label,
                "rel_srt": rel_srt,
                "source_video": rel_video_path,
            }
        )

    return discovered


def sort_subtitle_tracks(tracks: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return sorted(
        tracks,
        key=lambda track: (
            0 if is_english_language(track.get("lang", ""), track.get("label", "")) else 1,
            track.get("label", "").lower(),
            track.get("id", "").lower(),
        ),
    )


def discover_subtitles_for_video(video_path: Path, rel_video_path: str) -> Tuple[List[Dict], Dict[str, Dict[str, str]]]:
    source_rows = discover_external_subtitle_sources(video_path, rel_video_path)
    source_rows.extend(probe_embedded_subtitle_sources(video_path))
    ordered = sort_subtitle_tracks(source_rows)

    public_tracks: List[Dict] = []
    source_map: Dict[str, Dict[str, str]] = {}
    for idx, source in enumerate(ordered):
        track_id = source["id"]
        source_map[track_id] = source
        public_tracks.append(
            {
                "id": track_id,
                "lang": source.get("lang", "und"),
                "label": source.get("label", "Subtitle"),
                "default": idx == 0,
            }
        )

    return public_tracks, source_map


def convert_srt_to_vtt(srt_path: Path, out_path: Path) -> bool:
    try:
        raw = srt_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False

    lines = raw.splitlines()
    converted: List[str] = ["WEBVTT", ""]

    for line in lines:
        stripped = line.strip()
        if re.fullmatch(r"\d+", stripped):
            continue
        if "-->" in line:
            converted.append(line.replace(",", "."))
            continue
        converted.append(line)

    try:
        out_path.write_text("\n".join(converted) + "\n", encoding="utf-8")
    except OSError:
        return False
    return out_path.exists()


def ensure_subtitle_cache(rel_path: str, track_id: str) -> Optional[Path]:
    normalized = normalize_rel_path(rel_path)
    cache_path = subtitle_cache_path(normalized, track_id)

    with catalog_lock:
        tracks_for_video = subtitle_index.get(normalized, {})
        source = tracks_for_video.get(track_id)

    if not source:
        return None

    kind = source.get("kind", "")

    if kind == "external":
        if VIDEO_ROOT is None:
            return None
        rel_srt = normalize_rel_path(source.get("rel_srt", ""))
        srt_path = (VIDEO_ROOT / rel_srt).resolve()
        try:
            srt_path.relative_to(VIDEO_ROOT)
        except ValueError:
            return None
        if not srt_path.exists() or srt_path.suffix.lower() != ".srt":
            return None

        if cache_path.exists() and cache_path.stat().st_mtime >= srt_path.stat().st_mtime:
            return cache_path

        ok = convert_srt_to_vtt(srt_path, cache_path)
        return cache_path if ok else None

    if kind == "embedded":
        if not FFMPEG_AVAILABLE:
            return None
        video_path = resolve_video_path(normalized)
        if video_path is None:
            return None

        if cache_path.exists() and cache_path.stat().st_mtime >= video_path.stat().st_mtime:
            return cache_path

        stream_pos = source.get("stream_pos", "0")
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-map",
            f"0:s:{stream_pos}",
            "-f",
            "webvtt",
            str(cache_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return None

        return cache_path if result.returncode == 0 and cache_path.exists() else None

    return None


def set_generation_status(rel_path: str, key: str, value: str) -> None:
    with generation_lock:
        state = generation_status.setdefault(
            rel_path,
            {"thumbnail": "missing", "preview": "missing", "scrub": "missing"},
        )
        state.setdefault("thumbnail", "missing")
        state.setdefault("preview", "missing")
        state.setdefault("scrub", "missing")
        state[key] = value


def get_duration_seconds(video_path: Path) -> Optional[float]:
    if not FFPROBE_AVAILABLE:
        return None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nokey=1:noprint_wrappers=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0:
        return None

    try:
        return float(result.stdout.strip())
    except (TypeError, ValueError):
        return None


def generate_thumbnail(video_path: Path, out_path: Path) -> bool:
    if not FFMPEG_AVAILABLE:
        return False
    duration = get_duration_seconds(video_path) or 0.0
    seek = max(duration * 0.10, 0.0)
    primary_cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{seek:.3f}",
        *ffmpeg_cpu_threads_args(),
        *ffmpeg_hwaccel_input_args(),
        "-i",
        str(video_path),
        "-vframes",
        "1",
        "-q:v",
        str(THUMBNAIL_JPEG_Q),
        str(out_path),
    ]

    fallback_cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{seek:.3f}",
        *ffmpeg_cpu_threads_args(),
        "-i",
        str(video_path),
        "-vframes",
        "1",
        "-q:v",
        str(THUMBNAIL_JPEG_Q),
        str(out_path),
    ]

    ok, _ = run_ffmpeg_with_optional_fallback(primary_cmd, fallback_cmd, timeout=240)
    return ok and out_path.exists()


def generate_preview(video_path: Path, out_path: Path) -> bool:
    if not FFMPEG_AVAILABLE:
        return False

    start_offset_seconds = float(PREVIEW_START_OFFSET_SECONDS)
    duration = get_duration_seconds(video_path)
    if not duration or duration <= 0:
        # Fallback when duration metadata is unavailable.
        primary_cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start_offset_seconds:.3f}",
            *ffmpeg_cpu_threads_args(),
            *ffmpeg_hwaccel_input_args(),
            "-i",
            str(video_path),
            "-t",
            f"{PREVIEW_MIN_TOTAL_SECONDS:.3f}",
            "-vf",
            preview_scale_filter(),
            "-c:v",
            "h264_nvenc" if NVIDIA_NVENC_AVAILABLE else "libx264",
            "-preset",
            PREVIEW_NVENC_PRESET if NVIDIA_NVENC_AVAILABLE else PREVIEW_LIBX264_PRESET,
            "-cq" if NVIDIA_NVENC_AVAILABLE else "-crf",
            str(PREVIEW_NVENC_CQ) if NVIDIA_NVENC_AVAILABLE else str(PREVIEW_LIBX264_CRF),
            "-an",
            "-movflags",
            "+faststart",
            str(out_path),
        ]

        fallback_cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start_offset_seconds:.3f}",
            *ffmpeg_cpu_threads_args(),
            "-i",
            str(video_path),
            "-t",
            f"{PREVIEW_MIN_TOTAL_SECONDS:.3f}",
            "-vf",
            "scale=480:-2",
            "-c:v",
            "libx264",
            "-crf",
            str(PREVIEW_LIBX264_CRF),
            "-preset",
            PREVIEW_LIBX264_PRESET,
            "-an",
            "-movflags",
            "+faststart",
            str(out_path),
        ]

        ok, _ = run_ffmpeg_with_optional_fallback(primary_cmd, fallback_cmd, timeout=360)
        return ok and out_path.exists()

    min_total = min(PREVIEW_MIN_TOTAL_SECONDS, PREVIEW_MAX_TOTAL_SECONDS)
    max_total = max(PREVIEW_MIN_TOTAL_SECONDS, PREVIEW_MAX_TOTAL_SECONDS)
    duration_scale = max(PREVIEW_DURATION_AT_MAX_SECONDS, 1.0)

    duration_ratio = min(duration / duration_scale, 1.0)
    target_total = min_total + ((max_total - min_total) * duration_ratio)
    target_total = min(max_total, max(min_total, target_total))
    target_total = min(target_total, duration)

    segment_count_guess = max(1, math.ceil(duration / PREVIEW_SAMPLE_INTERVAL_SECONDS))
    segment_count = max(PREVIEW_MIN_SEGMENTS, min(PREVIEW_MAX_SEGMENTS, segment_count_guess))

    segment_duration = target_total / max(segment_count, 1)
    segment_duration = max(PREVIEW_MIN_SEGMENT_SECONDS, segment_duration)
    segment_duration = min(PREVIEW_MAX_SEGMENT_SECONDS, segment_duration)
    segment_duration = min(segment_duration, duration)

    usable_span = max(duration - segment_duration, 0.0)
    edge_guard_seconds = max(
        start_offset_seconds,
        PREVIEW_EDGE_GUARD_MIN_SECONDS,
        duration * PREVIEW_EDGE_GUARD_RATIO,
    )
    edge_guard_seconds = min(PREVIEW_EDGE_GUARD_MAX_SECONDS, edge_guard_seconds)

    start_min = min(edge_guard_seconds, usable_span)
    start_max = max(start_min, duration - edge_guard_seconds - segment_duration)

    if start_max <= start_min:
        centered_start = max((duration - segment_duration) / 2, 0.0)
        fallback_start = max(start_offset_seconds, centered_start)
        fallback_start = min(fallback_start, usable_span)
        starts = [fallback_start for _ in range(segment_count)]
    elif segment_count == 1:
        starts = [(start_min + start_max) / 2]
    else:
        starts = [
            start_min + (start_max - start_min) * i / (segment_count - 1)
            for i in range(segment_count)
        ]

    with tempfile.TemporaryDirectory(prefix="vrest_preview_") as tmpdir:
        tmp_root = Path(tmpdir)
        segment_files: List[Path] = []

        for idx, start in enumerate(starts):
            segment_path = tmp_root / f"segment_{idx:02d}.mp4"
            segment_cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                f"{max(start, 0.0):.3f}",
                *ffmpeg_cpu_threads_args(),
                *ffmpeg_hwaccel_input_args(),
                "-i",
                str(video_path),
                "-t",
                f"{segment_duration:.3f}",
                "-vf",
                preview_scale_filter(),
                "-c:v",
                "h264_nvenc" if NVIDIA_NVENC_AVAILABLE else "libx264",
                "-cq" if NVIDIA_NVENC_AVAILABLE else "-crf",
                str(PREVIEW_NVENC_CQ) if NVIDIA_NVENC_AVAILABLE else str(PREVIEW_LIBX264_CRF),
                "-preset",
                PREVIEW_NVENC_PRESET if NVIDIA_NVENC_AVAILABLE else PREVIEW_LIBX264_PRESET,
                "-an",
                "-movflags",
                "+faststart",
                str(segment_path),
            ]

            segment_fallback_cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                f"{max(start, 0.0):.3f}",
                *ffmpeg_cpu_threads_args(),
                "-i",
                str(video_path),
                "-t",
                f"{segment_duration:.3f}",
                "-vf",
                "scale=480:-2",
                "-c:v",
                "libx264",
                "-crf",
                str(PREVIEW_LIBX264_CRF),
                "-preset",
                PREVIEW_LIBX264_PRESET,
                "-an",
                "-movflags",
                "+faststart",
                str(segment_path),
            ]

            ok, _ = run_ffmpeg_with_optional_fallback(
                segment_cmd,
                segment_fallback_cmd,
                timeout=240,
            )
            if not ok or not segment_path.exists():
                return False

            segment_files.append(segment_path)

        list_file = tmp_root / "concat_list.txt"
        list_file.write_text(
            "\n".join(f"file '{segment.as_posix()}'" for segment in segment_files),
            encoding="utf-8",
        )

        concat_cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(out_path),
        ]

        try:
            concat_result = subprocess.run(
                concat_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return False

        return concat_result.returncode == 0 and out_path.exists()


def remove_scrub_sprite_files(sprite_dir: Path) -> None:
    if not sprite_dir.exists():
        return
    for candidate in sprite_dir.glob("sheet_*.jpg"):
        try:
            candidate.unlink()
        except OSError:
            continue


def build_scrub_metadata(
    _rel_path: str, duration: float, sheet_files: List[Path], generated_ts: str
) -> Dict:
    frames_per_sheet = SCRUB_TILE_COLUMNS * SCRUB_TILE_ROWS
    total_frames = int(duration // SCRUB_INTERVAL_SECONDS) + 1

    sheets: List[Dict[str, object]] = []
    for index, sheet_file in enumerate(sheet_files):
        start = index * frames_per_sheet
        end = min(total_frames, start + frames_per_sheet)
        count = max(0, end - start)
        sheets.append(
            {
                "index": index,
                "file": sheet_file.name,
                "count": count,
            }
        )

    frames: List[Dict[str, object]] = []
    for frame_idx in range(total_frames):
        sheet_index = frame_idx // frames_per_sheet
        pos = frame_idx % frames_per_sheet
        col = pos % SCRUB_TILE_COLUMNS
        row = pos // SCRUB_TILE_COLUMNS
        frames.append(
            {
                "timestamp": frame_idx * SCRUB_INTERVAL_SECONDS,
                "sheet": sheet_index,
                "x": col * SCRUB_CELL_WIDTH,
                "y": row * SCRUB_CELL_HEIGHT,
                "w": SCRUB_CELL_WIDTH,
                "h": SCRUB_CELL_HEIGHT,
            }
        )

    return {
        "version": 1,
        "spriteFolderPath": "scrubbing",
        "durationSeconds": duration,
        "generatedAt": generated_ts,
        "intervalSeconds": SCRUB_INTERVAL_SECONDS,
        "tile": {
            "columns": SCRUB_TILE_COLUMNS,
            "rows": SCRUB_TILE_ROWS,
            "cellWidth": SCRUB_CELL_WIDTH,
            "cellHeight": SCRUB_CELL_HEIGHT,
        },
        "sheets": sheets,
        "frames": frames,
    }


def normalize_scrub_metadata_payload(payload: object, _rel_path: str) -> Tuple[Optional[Dict], bool]:
    if not isinstance(payload, dict):
        return None, False

    changed = False
    normalized_payload = dict(payload)

    if "videoPath" in normalized_payload:
        normalized_payload.pop("videoPath", None)
        changed = True

    data_relative_scrub_dir = "scrubbing"
    path_keys = (
        "folder",
        "folderPath",
        "spriteFolder",
        "spriteFolderPath",
        "spritesFolder",
        "spritesFolderPath",
    )
    for key in path_keys:
        if key in normalized_payload and normalized_payload.get(key) != data_relative_scrub_dir:
            normalized_payload[key] = data_relative_scrub_dir
            changed = True

    if normalized_payload.get("spriteFolderPath") != data_relative_scrub_dir:
        normalized_payload["spriteFolderPath"] = data_relative_scrub_dir
        changed = True

    sheets = normalized_payload.get("sheets")
    if isinstance(sheets, list):
        normalized_sheets: List[object] = []
        for index, sheet in enumerate(sheets):
            if not isinstance(sheet, dict):
                normalized_sheets.append(sheet)
                continue

            normalized_sheet = dict(sheet)
            raw_file = normalized_sheet.get("file")
            if isinstance(raw_file, str):
                normalized_file = PurePosixPath(raw_file.replace("\\", "/")).name
                fallback_name = f"sheet_{index:04d}.jpg"
                normalized_file = normalized_file or fallback_name
                if normalized_file != raw_file:
                    normalized_sheet["file"] = normalized_file
                    changed = True
            elif "file" in normalized_sheet:
                normalized_sheet["file"] = f"sheet_{index:04d}.jpg"
                changed = True

            normalized_sheets.append(normalized_sheet)

        if normalized_sheets != sheets:
            normalized_payload["sheets"] = normalized_sheets
            changed = True

    return normalized_payload, changed


def generate_scrub_sprites(video_path: Path, rel_path: str, force: bool = False) -> bool:
    if not FFMPEG_AVAILABLE:
        return False

    metadata_path = scrub_metadata_cache_path(rel_path)
    sprite_dir = scrub_sprite_dir(rel_path)

    if not force and metadata_path.exists() and metadata_path.stat().st_mtime >= video_path.stat().st_mtime:
        set_generation_status(rel_path, "scrub", "ready")
        return True

    set_generation_status(rel_path, "scrub", "pending")
    remove_scrub_sprite_files(sprite_dir)

    sheet_pattern = sprite_dir / "sheet_%04d.jpg"
    cpu_filter_graph = (
        f"fps=1/{SCRUB_INTERVAL_SECONDS},"
        f"scale={SCRUB_CELL_WIDTH}:{SCRUB_CELL_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={SCRUB_CELL_WIDTH}:{SCRUB_CELL_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"tile={SCRUB_TILE_COLUMNS}x{SCRUB_TILE_ROWS}"
    )

    gpu_filter_graph = (
        f"fps=1/{SCRUB_INTERVAL_SECONDS},"
        f"scale_cuda={SCRUB_CELL_WIDTH}:{SCRUB_CELL_HEIGHT}:force_original_aspect_ratio=decrease,"
        "hwdownload,"
        "format=nv12,"
        f"pad={SCRUB_CELL_WIDTH}:{SCRUB_CELL_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"tile={SCRUB_TILE_COLUMNS}x{SCRUB_TILE_ROWS}"
    )

    use_gpu_scrub_pipeline = NVIDIA_CUDA_AVAILABLE and NVIDIA_SCALE_CUDA_AVAILABLE
    primary_filter_graph = gpu_filter_graph if use_gpu_scrub_pipeline else cpu_filter_graph

    primary_cmd = [
        "ffmpeg",
        "-y",
        *ffmpeg_cpu_threads_args(),
        *(ffmpeg_hwaccel_input_args() if use_gpu_scrub_pipeline else []),
        "-i",
        str(video_path),
        "-vf",
        primary_filter_graph,
        "-q:v",
        str(SCRUB_JPEG_Q),
        "-start_number",
        "0",
        str(sheet_pattern),
    ]

    fallback_cmd = [
        "ffmpeg",
        "-y",
        *ffmpeg_cpu_threads_args(),
        "-i",
        str(video_path),
        "-vf",
        cpu_filter_graph,
        "-q:v",
        str(SCRUB_JPEG_Q),
        "-start_number",
        "0",
        str(sheet_pattern),
    ]

    ok, error = run_ffmpeg_with_optional_fallback(primary_cmd, fallback_cmd, timeout=900)
    if not ok:
        LOGGER.warning("Scrub sprite generation failed for %s: %s", rel_path, error)
        set_generation_status(rel_path, "scrub", "failed")
        return False

    sheet_files = sorted(sprite_dir.glob("sheet_*.jpg"))
    if not sheet_files:
        set_generation_status(rel_path, "scrub", "failed")
        return False

    duration = get_duration_seconds(video_path) or 0.0
    generated_ts = datetime.now(timezone.utc).isoformat()
    metadata = build_scrub_metadata(rel_path, duration, sheet_files, generated_ts)

    try:
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    except OSError:
        set_generation_status(rel_path, "scrub", "failed")
        return False

    set_generation_status(rel_path, "scrub", "ready")
    return True


def ensure_scrub_metadata(rel_path: str, force: bool = False) -> Optional[Path]:
    normalized = normalize_rel_path(rel_path)
    video_path = resolve_video_path(normalized)
    if video_path is None:
        return None

    metadata_path = scrub_metadata_cache_path(normalized)
    if (
        not force
        and metadata_path.exists()
        and metadata_path.stat().st_mtime >= video_path.stat().st_mtime
    ):
        set_generation_status(normalized, "scrub", "ready")
        return metadata_path

    ok = generate_scrub_sprites(video_path, normalized, force=force)
    return metadata_path if ok and metadata_path.exists() else None


def get_scrub_sprite_path(rel_path: str, sheet_index: int) -> Optional[Path]:
    normalized = normalize_rel_path(rel_path)
    sprite_file = scrub_sprite_dir(normalized) / f"sheet_{sheet_index:04d}.jpg"
    if sprite_file.exists():
        return sprite_file
    return None


def ensure_folder_node(
    folder_path: str,
    folder_nodes: Dict[str, Dict],
    root_node: Dict,
) -> Dict:
    normalized = "" if folder_path in {"", "."} else folder_path
    if normalized in folder_nodes:
        return folder_nodes[normalized]

    parent = str(PurePosixPath(normalized).parent)
    if parent == ".":
        parent = ""

    parent_node = ensure_folder_node(parent, folder_nodes, root_node)
    folder_name = PurePosixPath(normalized).name
    node = {
        "name": folder_name,
        "path": normalized,
        "folders": [],
        "files": [],
    }
    parent_node["folders"].append(node)
    folder_nodes[normalized] = node
    return node


def sort_tree(node: Dict) -> None:
    node["folders"].sort(key=lambda x: x["name"].lower())
    node["files"].sort(key=lambda x: x["name"].lower())
    for child in node["folders"]:
        sort_tree(child)


def persist_catalog_snapshot(root: Dict, files: List[Dict], subs_index: Dict[str, Dict[str, Dict[str, str]]]) -> None:
    payload = {
        "version": CATALOG_SNAPSHOT_VERSION,
        "savedAt": datetime.now(timezone.utc).isoformat(),
        "savedAtTs": time.time(),
        "root": root,
        "files": files,
        "subtitleIndex": subs_index,
        "count": len(files),
    }

    tmp_path = CATALOG_SNAPSHOT_PATH.with_suffix(f"{CATALOG_SNAPSHOT_PATH.suffix}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(CATALOG_SNAPSHOT_PATH)
    except OSError:
        LOGGER.warning("Failed to persist catalog snapshot to %s", CATALOG_SNAPSHOT_PATH)


def load_catalog_snapshot() -> bool:
    if not CATALOG_SNAPSHOT_PATH.exists():
        return False

    try:
        payload = json.loads(CATALOG_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Catalog snapshot unreadable: %s", CATALOG_SNAPSHOT_PATH)
        return False

    if not isinstance(payload, dict):
        return False

    version = payload.get("version")
    if version not in {None, CATALOG_SNAPSHOT_VERSION}:
        LOGGER.info("Ignoring catalog snapshot version %s", version)
        return False

    root = payload.get("root")
    files = payload.get("files")
    subs_index = payload.get("subtitleIndex")
    if not isinstance(root, dict) or not isinstance(files, list) or not isinstance(subs_index, dict):
        return False

    with catalog_lock:
        global catalog_tree, catalog_files, subtitle_index
        catalog_tree = root
        catalog_files = files
        subtitle_index = subs_index

    saved_at_ts = payload.get("savedAtTs")
    try:
        cached_scan_ts = float(saved_at_ts)
    except (TypeError, ValueError):
        cached_scan_ts = 0.0

    if cached_scan_ts > 0:
        with catalog_state_lock:
            global catalog_last_scan_ts
            catalog_last_scan_ts = cached_scan_ts

    LOGGER.info("Loaded %d videos from catalog snapshot", len(files))
    return True


def scan_video_library() -> Tuple[Dict, List[Dict], Dict[str, Dict[str, Dict[str, str]]]]:
    if VIDEO_ROOT is None:
        return {"name": "Videos", "path": "", "folders": [], "files": []}, [], {}

    files: List[Dict] = []
    subs_index: Dict[str, Dict[str, Dict[str, str]]] = {}

    for path in sorted(VIDEO_ROOT.rglob("*")):
        if not is_video_file(path):
            continue

        rel = normalize_rel_path(path.relative_to(VIDEO_ROOT).as_posix())
        folder = str(PurePosixPath(rel).parent)
        if folder == ".":
            folder = ""
        metadata = {
            "name": path.name,
            "path": rel,
            "folder": folder,
            "ext": path.suffix.lower(),
        }
        try:
            stats = path.stat()
            metadata["fileSizeBytes"] = int(getattr(stats, "st_size", 0) or 0)
            added_ts = int(getattr(stats, "st_ctime", 0) or 0)
            if added_ts <= 0:
                added_ts = int(getattr(stats, "st_mtime", 0) or 0)
        except OSError:
            metadata["fileSizeBytes"] = 0
            added_ts = None

        metadata["dateAddedTs"] = added_ts
        duration_seconds = get_duration_seconds(path)
        metadata["durationSeconds"] = int(duration_seconds) if duration_seconds and duration_seconds > 0 else None

        subtitle_tracks, subtitle_sources = discover_subtitles_for_video(path, rel)
        metadata["subtitles"] = subtitle_tracks

        files.append(metadata)
        if subtitle_sources:
            subs_index[rel] = subtitle_sources

    root = {
        "name": VIDEO_ROOT.name or "Videos",
        "path": "",
        "folders": [],
        "files": [],
    }
    folder_nodes: Dict[str, Dict] = {"": root}

    for item in files:
        folder_node = ensure_folder_node(item["folder"], folder_nodes, root)
        folder_node["files"].append(
            {
                "name": item["name"],
                "path": item["path"],
            }
        )

    sort_tree(root)
    files.sort(key=lambda x: x["path"].lower())
    return root, files, subs_index


def thumbnail_worker(rel_path: str) -> None:
    out_path = thumbnail_cache_path(rel_path)
    if out_path.exists():
        set_generation_status(rel_path, "thumbnail", "ready")
        return

    video_path = resolve_video_path(rel_path)
    if video_path is None:
        set_generation_status(rel_path, "thumbnail", "failed")
        return

    ok = generate_thumbnail(video_path, out_path)
    set_generation_status(rel_path, "thumbnail", "ready" if ok else "failed")


def queue_thumbnail_generation(rel_path: str) -> None:
    rel_path = normalize_rel_path(rel_path)
    out_path = thumbnail_cache_path(rel_path)
    if out_path.exists():
        set_generation_status(rel_path, "thumbnail", "ready")
        return

    with generation_lock:
        state = generation_status.setdefault(
            rel_path,
            {"thumbnail": "missing", "preview": "missing", "scrub": "missing"},
        )
        if state["thumbnail"] == "pending":
            return
        state["thumbnail"] = "pending"

    thumbnail_executor.submit(thumbnail_worker, rel_path)


def preview_worker(rel_path: str) -> None:
    generate_preview_sync(rel_path)


def queue_preview_generation(rel_path: str) -> None:
    rel_path = normalize_rel_path(rel_path)
    out_path = preview_cache_path(rel_path)
    if out_path.exists():
        set_generation_status(rel_path, "preview", "ready")
        return

    with generation_lock:
        state = generation_status.setdefault(
            rel_path,
            {"thumbnail": "missing", "preview": "missing", "scrub": "missing"},
        )
        if state["preview"] == "pending":
            return
        state["preview"] = "pending"

    preview_executor.submit(preview_worker, rel_path)


def scrub_worker(rel_path: str) -> None:
    normalized = normalize_rel_path(rel_path)
    video_path = resolve_video_path(normalized)
    if video_path is None:
        set_generation_status(normalized, "scrub", "failed")
        return

    ok = generate_scrub_sprites(video_path, normalized)
    set_generation_status(normalized, "scrub", "ready" if ok else "failed")


def queue_scrub_generation(rel_path: str) -> None:
    normalized = normalize_rel_path(rel_path)
    video_path = resolve_video_path(normalized)
    if video_path is None:
        return

    metadata_path = scrub_metadata_cache_path(normalized)
    if metadata_path.exists() and metadata_path.stat().st_mtime >= video_path.stat().st_mtime:
        set_generation_status(normalized, "scrub", "ready")
        return

    with generation_lock:
        state = generation_status.setdefault(
            normalized,
            {"thumbnail": "missing", "preview": "missing", "scrub": "missing"},
        )
        if state.get("scrub") == "pending":
            return
        state["scrub"] = "pending"

    scrub_executor.submit(scrub_worker, normalized)


def generate_preview_sync(rel_path: str) -> bool:
    rel_path = normalize_rel_path(rel_path)
    out_path = preview_cache_path(rel_path)
    if out_path.exists():
        set_generation_status(rel_path, "preview", "ready")
        return True

    set_generation_status(rel_path, "preview", "pending")
    video_path = resolve_video_path(rel_path)
    if video_path is None:
        set_generation_status(rel_path, "preview", "failed")
        return False

    ok = generate_preview(video_path, out_path)
    set_generation_status(rel_path, "preview", "ready" if ok else "failed")
    return ok


def scrub_pregenerate_worker(force: bool = False) -> None:
    try:
        with catalog_lock:
            targets = [item["path"] for item in catalog_files]

        with scrub_pregen_lock:
            scrub_pregen_state.update(
                {
                    "running": True,
                    "total": len(targets),
                    "done": 0,
                    "failed": 0,
                    "startedAt": datetime.now(timezone.utc).isoformat(),
                    "finishedAt": None,
                }
            )

        for rel_path in targets:
            meta = ensure_scrub_metadata(rel_path, force=force)
            with scrub_pregen_lock:
                scrub_pregen_state["done"] = int(scrub_pregen_state.get("done", 0)) + 1
                if meta is None:
                    scrub_pregen_state["failed"] = int(scrub_pregen_state.get("failed", 0)) + 1
    finally:
        with scrub_pregen_lock:
            scrub_pregen_state["running"] = False
            scrub_pregen_state["finishedAt"] = datetime.now(timezone.utc).isoformat()


def start_scrub_pregeneration(force: bool = False) -> bool:
    with scrub_pregen_lock:
        if bool(scrub_pregen_state.get("running")):
            return False
    scrub_executor.submit(scrub_pregenerate_worker, force)
    return True


def preview_pregenerate_worker(force: bool = False) -> None:
    try:
        with catalog_lock:
            targets = [item["path"] for item in catalog_files]

        with preview_pregen_lock:
            preview_pregen_state.update(
                {
                    "running": True,
                    "total": len(targets),
                    "done": 0,
                    "failed": 0,
                    "startedAt": datetime.now(timezone.utc).isoformat(),
                    "finishedAt": None,
                }
            )

        for rel_path in targets:
            normalized = normalize_rel_path(rel_path)
            if not force and preview_cache_path(normalized).exists():
                set_generation_status(normalized, "preview", "ready")
                ok = True
            else:
                ok = generate_preview_sync(normalized)

            with preview_pregen_lock:
                preview_pregen_state["done"] = int(preview_pregen_state.get("done", 0)) + 1
                if not ok:
                    preview_pregen_state["failed"] = int(preview_pregen_state.get("failed", 0)) + 1
    finally:
        with preview_pregen_lock:
            preview_pregen_state["running"] = False
            preview_pregen_state["finishedAt"] = datetime.now(timezone.utc).isoformat()


def start_preview_pregeneration(force: bool = False) -> bool:
    with preview_pregen_lock:
        if bool(preview_pregen_state.get("running")):
            return False
    preview_executor.submit(preview_pregenerate_worker, force)
    return True


def refresh_catalog() -> None:
    root, files, subs = scan_video_library()
    with catalog_lock:
        global catalog_tree, catalog_files, subtitle_index
        catalog_tree = root
        catalog_files = files
        subtitle_index = subs

    persist_catalog_snapshot(root, files, subs)

    for file_meta in files:
        queue_thumbnail_generation(file_meta["path"])


def catalog_refresh_worker() -> None:
    global catalog_refresh_in_progress, catalog_has_completed_scan, catalog_last_scan_ts
    try:
        refresh_catalog()
        with catalog_lock:
            indexed_count = len(catalog_files)
        with catalog_state_lock:
            catalog_has_completed_scan = True
            catalog_last_scan_ts = time.time()
        LOGGER.info("Indexed %d video files", indexed_count)
    except Exception:
        LOGGER.exception("Catalog refresh failed")
    finally:
        with catalog_state_lock:
            catalog_refresh_in_progress = False


def schedule_catalog_refresh(force: bool = False) -> bool:
    global catalog_refresh_in_progress
    if CONFIG_ERROR:
        return False

    with catalog_state_lock:
        if catalog_refresh_in_progress:
            return False
        if (
            not force
            and catalog_has_completed_scan
            and (time.time() - catalog_last_scan_ts) < CATALOG_REFRESH_MIN_INTERVAL_SECONDS
        ):
            return False
        catalog_refresh_in_progress = True

    threading.Thread(target=catalog_refresh_worker, daemon=True).start()
    return True


def background_startup_index() -> None:
    if CONFIG_ERROR:
        return
    schedule_catalog_refresh(force=True)


@app.before_request
def require_authentication():
    if request.endpoint in {
        "login_page",
        "logout",
        "favicon",
        "apple_touch_icon",
        "site_webmanifest",
    }:
        return None

    if request.path.startswith("/static/"):
        return None

    if is_authenticated():
        return None

    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication required"}), 401

    next_path = request.full_path.rstrip("?")
    return redirect(url_for("login_page", next=next_path))


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if not auth_enabled():
        return redirect(url_for("index_page"))

    if bool(session.get("authenticated")):
        if not session.permanent:
            session.permanent = True
            session.modified = True
        return redirect(url_for("index_page"))

    error: Optional[str] = None
    if request.method == "POST":
        submitted_password = request.form.get("password", "")
        if hmac.compare_digest(submitted_password, APP_PASSWORD):
            session.permanent = True
            session["authenticated"] = True
            requested_next = request.args.get("next", "")
            if requested_next.startswith("/") and not requested_next.startswith("//"):
                return redirect(requested_next)
            return redirect(url_for("index_page"))
        error = "Invalid password"

    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.get("/favicon.ico")
def favicon():
    return app.send_static_file("icons/favicon.ico")


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    return app.send_static_file("icons/apple-touch-icon.png")


@app.get("/site.webmanifest")
def site_webmanifest():
    return app.send_static_file("site.webmanifest")


@app.get("/")
def index_page():
    return render_template(
        "index.html",
        config_error=CONFIG_ERROR,
        video_root=str(VIDEO_ROOT) if VIDEO_ROOT else "",
    )


@app.get("/api/browse")
def api_browse():
    if CONFIG_ERROR:
        return jsonify(
            {
                "error": CONFIG_ERROR,
                "root": {"name": "Videos", "path": "", "folders": [], "files": []},
                "files": [],
                "count": 0,
                "ffmpeg": FFMPEG_AVAILABLE,
                "ffprobe": FFPROBE_AVAILABLE,
            }
        ), 400

    schedule_catalog_refresh()

    with catalog_state_lock:
        indexing = catalog_refresh_in_progress
        has_scanned = catalog_has_completed_scan
        last_scan_ts = catalog_last_scan_ts

    with catalog_lock:
        return jsonify(
            {
                "root": catalog_tree,
                "files": catalog_files,
                "count": len(catalog_files),
                "ffmpeg": FFMPEG_AVAILABLE,
                "ffprobe": FFPROBE_AVAILABLE,
                "indexing": indexing,
                "hasScanned": has_scanned,
                "lastScanTs": last_scan_ts,
            }
        )


@app.post("/api/reindex")
def api_reindex():
    if CONFIG_ERROR:
        return jsonify({"error": CONFIG_ERROR}), 400

    started = schedule_catalog_refresh(force=True)

    with catalog_state_lock:
        indexing = catalog_refresh_in_progress
        has_scanned = catalog_has_completed_scan
        last_scan_ts = catalog_last_scan_ts

    with catalog_lock:
        count = len(catalog_files)

    return jsonify(
        {
            "started": started,
            "indexing": indexing,
            "hasScanned": has_scanned,
            "lastScanTs": last_scan_ts,
            "count": count,
        }
    ), (202 if started else 200)


@app.get("/api/thumbnail/<path:rel_path>")
def api_thumbnail(rel_path: str):
    normalized = normalize_rel_path(rel_path)
    if resolve_video_path(normalized) is None:
        abort(404)

    out_path = thumbnail_cache_path(normalized)
    if out_path.exists():
        set_generation_status(normalized, "thumbnail", "ready")
        return send_file(out_path, mimetype="image/jpeg", conditional=True)

    queue_thumbnail_generation(normalized)
    return jsonify({"status": "pending"}), 202


@app.get("/api/preview/<path:rel_path>")
def api_preview(rel_path: str):
    normalized = normalize_rel_path(rel_path)
    if resolve_video_path(normalized) is None:
        abort(404)

    out_path = preview_cache_path(normalized)
    if not out_path.exists():
        ok = generate_preview_sync(normalized)
        if not ok:
            return jsonify({"status": "failed"}), 500

    return send_file(out_path, mimetype="video/mp4", conditional=True)


@app.get("/api/video/<path:rel_path>")
def api_video(rel_path: str):
    normalized = normalize_rel_path(rel_path)
    video_path = resolve_video_path(normalized)
    if video_path is None:
        abort(404)

    return send_file(video_path, conditional=True)


@app.get("/api/subtitle/<path:rel_path>/<track_id>.vtt")
def api_subtitle(rel_path: str, track_id: str):
    normalized = normalize_rel_path(rel_path)
    if resolve_video_path(normalized) is None:
        abort(404)

    cache = ensure_subtitle_cache(normalized, track_id)
    if cache is None or not cache.exists():
        abort(404)

    return send_file(cache, mimetype="text/vtt", conditional=True)


@app.get("/api/scrub/<path:rel_path>/metadata")
def api_scrub_metadata(rel_path: str):
    normalized = normalize_rel_path(rel_path)
    if resolve_video_path(normalized) is None:
        abort(404)

    metadata_path = ensure_scrub_metadata(normalized)
    if metadata_path is None or not metadata_path.exists():
        return jsonify({"status": "failed"}), 500

    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return send_file(metadata_path, mimetype="application/json", conditional=True)

    normalized_payload, changed = normalize_scrub_metadata_payload(payload, normalized)
    if normalized_payload is None:
        return send_file(metadata_path, mimetype="application/json", conditional=True)

    if changed:
        try:
            metadata_path.write_text(json.dumps(normalized_payload, indent=2), encoding="utf-8")
        except OSError:
            LOGGER.warning("Failed to persist normalized scrub metadata for %s", normalized)

    return jsonify(normalized_payload)


@app.get("/api/scrub/<path:rel_path>/sprite/<int:sheet_index>.jpg")
def api_scrub_sprite(rel_path: str, sheet_index: int):
    normalized = normalize_rel_path(rel_path)
    if resolve_video_path(normalized) is None:
        abort(404)

    sprite = get_scrub_sprite_path(normalized, sheet_index)
    if sprite is None:
        metadata_path = ensure_scrub_metadata(normalized)
        if metadata_path is None:
            abort(404)
        sprite = get_scrub_sprite_path(normalized, sheet_index)
        if sprite is None:
            abort(404)

    return send_file(sprite, mimetype="image/jpeg", conditional=True)


@app.post("/api/scrub/pregenerate")
def api_scrub_pregenerate():
    force = str(request.args.get("force", "")).strip().lower() in {"1", "true", "yes", "on"}
    started = start_scrub_pregeneration(force=force)
    with scrub_pregen_lock:
        payload = dict(scrub_pregen_state)
    payload["started"] = started
    return jsonify(payload), (202 if started else 200)


@app.get("/api/scrub/pregenerate")
def api_scrub_pregenerate_status():
    with scrub_pregen_lock:
        payload = dict(scrub_pregen_state)
    return jsonify(payload)


@app.post("/api/preview/pregenerate")
def api_preview_pregenerate():
    force = str(request.args.get("force", "")).strip().lower() in {"1", "true", "yes", "on"}
    started = start_preview_pregeneration(force=force)
    with preview_pregen_lock:
        payload = dict(preview_pregen_state)
    payload["started"] = started
    return jsonify(payload), (202 if started else 200)


@app.get("/api/preview/pregenerate")
def api_preview_pregenerate_status():
    with preview_pregen_lock:
        payload = dict(preview_pregen_state)
    return jsonify(payload)


@app.post("/api/data/cleanup")
def api_data_cleanup():
    if CONFIG_ERROR:
        return jsonify({"error": CONFIG_ERROR}), 400

    payload = cleanup_orphaned_data_files()
    return jsonify(payload)


def generation_snapshot_for_path(normalized: str) -> Dict[str, object]:
    thumb_ready = thumbnail_cache_path(normalized).exists()
    preview_ready = preview_cache_path(normalized).exists()
    scrub_ready = scrub_metadata_cache_path(normalized).exists()

    if thumb_ready:
        set_generation_status(normalized, "thumbnail", "ready")
    if preview_ready:
        set_generation_status(normalized, "preview", "ready")
    if scrub_ready:
        set_generation_status(normalized, "scrub", "ready")

    with generation_lock:
        state = generation_status.get(
            normalized,
            {
                "thumbnail": "ready" if thumb_ready else "missing",
                "preview": "ready" if preview_ready else "missing",
                "scrub": "ready" if scrub_ready else "missing",
            },
        )

    return {
        "thumbnail": thumb_ready,
        "preview": preview_ready,
        "scrub": scrub_ready,
        "thumbnailState": state.get("thumbnail", "missing"),
        "previewState": state.get("preview", "missing"),
        "scrubState": state.get("scrub", "missing"),
    }


@app.get("/api/status/all")
def api_status_all():
    if CONFIG_ERROR:
        return jsonify({"items": [], "summary": {}, "count": 0}), 400

    schedule_catalog_refresh()

    with catalog_state_lock:
        indexing = catalog_refresh_in_progress

    with scrub_pregen_lock:
        scrub_job = dict(scrub_pregen_state)

    with preview_pregen_lock:
        preview_job = dict(preview_pregen_state)

    with catalog_lock:
        catalog_snapshot = [
            {
                "path": item.get("path", ""),
                "name": item.get("name", ""),
                "folder": item.get("folder", ""),
            }
            for item in catalog_files
        ]

    summary = {
        "thumbnail": {"ready": 0, "pending": 0, "failed": 0, "missing": 0},
        "preview": {"ready": 0, "pending": 0, "failed": 0, "missing": 0},
        "scrub": {"ready": 0, "pending": 0, "failed": 0, "missing": 0},
    }

    def safe_bucket(value: str) -> str:
        return value if value in {"ready", "pending", "failed", "missing"} else "missing"

    items: List[Dict[str, object]] = []
    for entry in catalog_snapshot:
        rel_path = normalize_rel_path(str(entry.get("path") or ""))
        if not rel_path:
            continue
        snapshot = generation_snapshot_for_path(rel_path)
        items.append(
            {
                "path": rel_path,
                "name": entry.get("name", ""),
                "folder": entry.get("folder", ""),
                **snapshot,
            }
        )

        summary["thumbnail"][safe_bucket(str(snapshot.get("thumbnailState", "missing")))] += 1
        summary["preview"][safe_bucket(str(snapshot.get("previewState", "missing")))] += 1
        summary["scrub"][safe_bucket(str(snapshot.get("scrubState", "missing")))] += 1

    return jsonify(
        {
            "items": items,
            "summary": summary,
            "count": len(items),
            "indexing": indexing,
            "jobs": {
                "preview": preview_job,
                "scrub": scrub_job,
            },
        }
    )


@app.get("/api/status/<path:rel_path>")
def api_status(rel_path: str):
    normalized = normalize_rel_path(rel_path)
    if resolve_video_path(normalized) is None:
        abort(404)

    return jsonify(generation_snapshot_for_path(normalized))


@atexit.register
def shutdown_executor() -> None:
    thumbnail_executor.shutdown(wait=False)
    preview_executor.shutdown(wait=False)
    scrub_executor.shutdown(wait=False)


if not CONFIG_ERROR:
    load_catalog_snapshot()


threading.Thread(target=background_startup_index, daemon=True).start()


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
