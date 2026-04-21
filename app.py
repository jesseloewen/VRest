import atexit
import hmac
import json
import logging
import os
import secrets
import subprocess
import tempfile
import threading
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

VIDEO_FOLDER = os.getenv("VIDEO_FOLDER", "").strip()
DATA_FOLDER = os.getenv("DATA_FOLDER", "./data").strip() or "./data"
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "3232"))
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "").strip()

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
THUMBNAIL_ROOT = DATA_ROOT / "thumbnails"
PREVIEW_ROOT = DATA_ROOT / "previews"
SUBTITLE_ROOT = DATA_ROOT / "subtitles"
THUMBNAIL_ROOT.mkdir(parents=True, exist_ok=True)
PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
SUBTITLE_ROOT.mkdir(parents=True, exist_ok=True)

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

app = Flask(__name__)

if not APP_SECRET_KEY:
    APP_SECRET_KEY = secrets.token_urlsafe(32)
    LOGGER.warning("APP_SECRET_KEY not set; using a temporary key for this process.")

app.secret_key = APP_SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

catalog_lock = threading.Lock()
catalog_tree: Dict = {"name": "Videos", "path": "", "folders": [], "files": []}
catalog_files: List[Dict] = []
video_index: Dict[str, Path] = {}

generation_lock = threading.Lock()
generation_status: Dict[str, Dict[str, str]] = {}
thumbnail_executor = ThreadPoolExecutor(max_workers=4)


def auth_enabled() -> bool:
    return bool(APP_PASSWORD)


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


def resolve_subtitle_path(rel_path: str) -> Optional[Path]:
    if VIDEO_ROOT is None:
        return None
    normalized = normalize_rel_path(rel_path)
    candidate = (VIDEO_ROOT / normalized).resolve()
    try:
        candidate.relative_to(VIDEO_ROOT)
    except ValueError:
        return None
    if not candidate.is_file() or candidate.suffix.lower() != ".srt":
        return None
    return candidate


def thumbnail_cache_path(rel_path: str) -> Path:
    safe_rel = normalize_rel_path(rel_path)
    target = THUMBNAIL_ROOT / f"{safe_rel}.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def preview_cache_path(rel_path: str) -> Path:
    safe_rel = normalize_rel_path(rel_path)
    target = PREVIEW_ROOT / f"{safe_rel}.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def set_generation_status(rel_path: str, key: str, value: str) -> None:
    with generation_lock:
        state = generation_status.setdefault(
            rel_path, {"thumbnail": "missing", "preview": "missing"}
        )
        state[key] = value


def subtitle_language_meta(raw_hint: str) -> Tuple[str, str]:
    hint = (raw_hint or "").strip().replace("_", "-")
    if not hint:
        return "en", "English"

    lower_hint = hint.lower()
    known = {
        "en": "English",
        "english": "English",
        "de": "German",
        "german": "German",
        "fr": "French",
        "french": "French",
        "es": "Spanish",
        "spanish": "Spanish",
        "it": "Italian",
        "italian": "Italian",
        "pt": "Portuguese",
        "portuguese": "Portuguese",
        "nl": "Dutch",
        "dutch": "Dutch",
        "pl": "Polish",
        "polish": "Polish",
        "sv": "Swedish",
        "swedish": "Swedish",
        "ja": "Japanese",
        "japanese": "Japanese",
        "ko": "Korean",
        "korean": "Korean",
        "zh": "Chinese",
        "chinese": "Chinese",
    }
    if lower_hint in known:
        label = known[lower_hint]
        lang_code = lower_hint[:2] if len(lower_hint) >= 2 else "en"
        return lang_code, label

    lang_code = lower_hint[:2] if len(lower_hint) >= 2 else "en"
    return lang_code, hint.replace("-", " ").title()


def find_external_subtitles(video_path: Path) -> List[Dict[str, str]]:
    base_stem = video_path.stem
    subs: List[Dict[str, str]] = []

    for srt_file in sorted(video_path.parent.glob(f"{base_stem}*.srt")):
        stem_name = srt_file.stem
        if stem_name != base_stem and not stem_name.startswith(f"{base_stem}."):
            continue

        suffix_hint = stem_name[len(base_stem) :].lstrip(".")
        srclang, label = subtitle_language_meta(suffix_hint)
        rel_sub = normalize_rel_path(srt_file.relative_to(VIDEO_ROOT).as_posix())

        subs.append(
            {
                "kind": "captions",
                "source": "external",
                "label": label,
                "srclang": srclang,
                "src": f"/api/subtitle/external/{rel_sub}",
                "default": suffix_hint in {"", "en", "english"},
            }
        )

    return subs


def find_embedded_subtitles(video_path: Path, rel_video_path: str) -> List[Dict[str, str]]:
    if not FFPROBE_AVAILABLE:
        return []

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "s",
        "-show_entries",
        "stream=index:stream_tags=language,title",
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
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return []

    tracks: List[Dict[str, str]] = []
    streams = data.get("streams", [])
    for ordinal, stream in enumerate(streams, start=1):
        stream_index = stream.get("index")
        tags = stream.get("tags") or {}
        language = str(tags.get("language", "") or "").strip()
        title = str(tags.get("title", "") or "").strip()

        if language:
            srclang, language_label = subtitle_language_meta(language)
        else:
            srclang, language_label = ("en", "Embedded")

        label = title or f"Embedded {ordinal} ({language_label})"
        tracks.append(
            {
                "kind": "captions",
                "source": "embedded",
                "label": label,
                "srclang": srclang,
                "src": f"/api/subtitle/embedded/{rel_video_path}?stream_index={stream_index}",
                "default": False,
            }
        )

    return tracks


def convert_external_srt_to_vtt(subtitle_path: Path, out_path: Path) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(subtitle_path),
        str(out_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False

    return result.returncode == 0 and out_path.exists()


def convert_embedded_subtitle_to_vtt(video_path: Path, stream_index: int, out_path: Path) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-map",
        f"0:{stream_index}",
        "-f",
        "webvtt",
        str(out_path),
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
        return False

    return result.returncode == 0 and out_path.exists()


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
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{seek:.3f}",
        "-i",
        str(video_path),
        "-vframes",
        "1",
        "-q:v",
        "2",
        str(out_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=240)
    except subprocess.TimeoutExpired:
        return False

    return result.returncode == 0 and out_path.exists()


def generate_preview(video_path: Path, out_path: Path) -> bool:
    if not FFMPEG_AVAILABLE:
        return False

    duration = get_duration_seconds(video_path)
    if not duration or duration <= 0:
        # Fallback when duration metadata is unavailable.
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            "0",
            "-i",
            str(video_path),
            "-t",
            "12",
            "-vf",
            "scale=480:-2",
            "-crf",
            "28",
            "-preset",
            "ultrafast",
            "-an",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=360)
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0 and out_path.exists()

    target_total = min(12.0, duration)
    min_segment_duration = 0.8
    max_segments = 4

    if duration < min_segment_duration:
        segment_count = 1
    else:
        segment_count = max(1, min(max_segments, int(duration / min_segment_duration)))

    segment_duration = min(3.0, target_total / segment_count)
    usable_span = max(duration - segment_duration, 0.0)

    if segment_count == 1:
        starts = [0.0 if usable_span == 0 else usable_span / 2]
    else:
        starts = [usable_span * i / (segment_count - 1) for i in range(segment_count)]

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
                "-i",
                str(video_path),
                "-t",
                f"{segment_duration:.3f}",
                "-vf",
                "scale=480:-2",
                "-crf",
                "28",
                "-preset",
                "ultrafast",
                "-an",
                "-movflags",
                "+faststart",
                str(segment_path),
            ]

            try:
                segment_result = subprocess.run(
                    segment_cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=240,
                )
            except subprocess.TimeoutExpired:
                return False

            if segment_result.returncode != 0 or not segment_path.exists():
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


def scan_video_library() -> Tuple[Dict, List[Dict], Dict[str, Path]]:
    if VIDEO_ROOT is None:
        return {"name": "Videos", "path": "", "folders": [], "files": []}, [], {}

    files: List[Dict] = []
    index: Dict[str, Path] = {}

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
        files.append(metadata)
        index[rel] = path

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
    return root, files, index


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
            rel_path, {"thumbnail": "missing", "preview": "missing"}
        )
        if state["thumbnail"] == "pending":
            return
        state["thumbnail"] = "pending"

    thumbnail_executor.submit(thumbnail_worker, rel_path)


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


def refresh_catalog() -> None:
    root, files, index = scan_video_library()
    with catalog_lock:
        global catalog_tree, catalog_files, video_index
        catalog_tree = root
        catalog_files = files
        video_index = index

    for file_meta in files:
        queue_thumbnail_generation(file_meta["path"])


def background_startup_index() -> None:
    if CONFIG_ERROR:
        return
    refresh_catalog()
    LOGGER.info("Indexed %d video files", len(catalog_files))


@app.before_request
def require_authentication():
    if request.endpoint in {"login_page", "logout"}:
        return None

    if request.path.startswith("/static/"):
        return None

    if request.path.startswith("/api/thumbnail/") or request.path.startswith("/api/preview/"):
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
        return redirect(url_for("index_page"))

    error: Optional[str] = None
    if request.method == "POST":
        submitted_password = request.form.get("password", "")
        if hmac.compare_digest(submitted_password, APP_PASSWORD):
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

    refresh_catalog()
    with catalog_lock:
        return jsonify(
            {
                "root": catalog_tree,
                "files": catalog_files,
                "count": len(catalog_files),
                "ffmpeg": FFMPEG_AVAILABLE,
                "ffprobe": FFPROBE_AVAILABLE,
            }
        )


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


@app.get("/api/subtitles/<path:rel_video_path>")
def api_subtitles(rel_video_path: str):
    normalized = normalize_rel_path(rel_video_path)
    video_path = resolve_video_path(normalized)
    if video_path is None:
        abort(404)

    tracks = find_external_subtitles(video_path)
    tracks.extend(find_embedded_subtitles(video_path, normalized))

    # Keep at most one subtitle track per language to avoid stacked duplicate captions.
    # External sidecar files are preferred over embedded tracks for the same language.
    deduped_by_lang: Dict[str, Dict[str, str]] = {}
    for track in tracks:
        lang_key = (track.get("srclang") or "").strip().lower() or "und"
        existing = deduped_by_lang.get(lang_key)
        if existing is None:
            deduped_by_lang[lang_key] = track
            continue

        if existing.get("source") == "embedded" and track.get("source") == "external":
            deduped_by_lang[lang_key] = track

    tracks = list(deduped_by_lang.values())

    has_default = any(track.get("default") for track in tracks)
    if tracks and not has_default:
        tracks[0]["default"] = True

    return jsonify({"tracks": tracks})


@app.get("/api/subtitle/external/<path:rel_subtitle_path>")
def api_subtitle_external(rel_subtitle_path: str):
    normalized = normalize_rel_path(rel_subtitle_path)
    subtitle_path = resolve_subtitle_path(normalized)
    if subtitle_path is None:
        abort(404)

    cache_path = SUBTITLE_ROOT / "external" / f"{normalized}.vtt"
    if not cache_path.exists() or subtitle_path.stat().st_mtime > cache_path.stat().st_mtime:
        if not FFMPEG_AVAILABLE:
            return jsonify({"error": "ffmpeg not available"}), 500
        ok = convert_external_srt_to_vtt(subtitle_path, cache_path)
        if not ok:
            return jsonify({"error": "failed to convert subtitle"}), 500

    return send_file(cache_path, mimetype="text/vtt", conditional=True)


@app.get("/api/subtitle/embedded/<path:rel_video_path>")
def api_subtitle_embedded(rel_video_path: str):
    normalized = normalize_rel_path(rel_video_path)
    video_path = resolve_video_path(normalized)
    if video_path is None:
        abort(404)

    stream_index_text = request.args.get("stream_index", "").strip()
    if not stream_index_text.isdigit():
        return jsonify({"error": "stream_index must be an integer"}), 400
    stream_index = int(stream_index_text)

    cache_path = SUBTITLE_ROOT / "embedded" / normalized / f"stream_{stream_index}.vtt"
    if not cache_path.exists() or video_path.stat().st_mtime > cache_path.stat().st_mtime:
        if not FFMPEG_AVAILABLE:
            return jsonify({"error": "ffmpeg not available"}), 500
        ok = convert_embedded_subtitle_to_vtt(video_path, stream_index, cache_path)
        if not ok:
            return jsonify({"error": "failed to extract embedded subtitle"}), 500

    return send_file(cache_path, mimetype="text/vtt", conditional=True)


@app.get("/api/status/<path:rel_path>")
def api_status(rel_path: str):
    normalized = normalize_rel_path(rel_path)
    if resolve_video_path(normalized) is None:
        abort(404)

    thumb_ready = thumbnail_cache_path(normalized).exists()
    preview_ready = preview_cache_path(normalized).exists()

    if thumb_ready:
        set_generation_status(normalized, "thumbnail", "ready")
    if preview_ready:
        set_generation_status(normalized, "preview", "ready")

    with generation_lock:
        state = generation_status.get(
            normalized,
            {
                "thumbnail": "ready" if thumb_ready else "missing",
                "preview": "ready" if preview_ready else "missing",
            },
        )

    return jsonify(
        {
            "thumbnail": thumb_ready,
            "preview": preview_ready,
            "thumbnailState": state["thumbnail"],
            "previewState": state["preview"],
        }
    )


@atexit.register
def shutdown_executor() -> None:
    thumbnail_executor.shutdown(wait=False)


threading.Thread(target=background_startup_index, daemon=True).start()


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
