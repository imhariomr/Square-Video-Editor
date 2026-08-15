"""
Simple square (1:1) video editor: upload a video, trim it to a start/end
range, add a text caption (top band like an Instagram Reel, or a floating
overlay), and export the actual rendered, trimmed, square MP4.

Run locally:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5050 in your browser.

All video processing (trim, crop-to-square, text burn-in) happens with
ffmpeg on the server; nothing is faked in the UI — the exported file is a
real, independently-playable MP4.
"""
import os
import re
import shutil
import tempfile
import threading
import time
import uuid

from flask import (
    Flask, render_template, request, jsonify, send_file, abort
)

from video_utils import probe_video, run_export, remux_faststart, VideoProbeError, ExportError
from text_render import (
    render_band_image, render_overlay_image, render_watermark_image,
    MIN_FONT_SIZE, MAX_FONT_SIZE,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {"mp4", "mov", "m4v", "webm", "mkv", "avi"}
CANVAS = 1080
MAX_TEXT_LENGTH = 300

# Fixed brand watermark burned into every export (bottom-center, small bold
# white text with a subtle shadow) — not user-configurable by design.
WATERMARK_TEXT = "dumbestposting"
WATERMARK_FONT_SIZE = 26
WATERMARK_MARGIN = 30
WATERMARK_OPACITY = 0.55

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1GB upload cap


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Video file is too large (max 1GB)."}), 413


@app.context_processor
def inject_asset_version():
    def asset_version(filename):
        path = os.path.join(app.static_folder, filename)
        try:
            return int(os.path.getmtime(path))
        except OSError:
            return 0
    return dict(asset_version=asset_version)


# In-memory registries. Fine for a single-user local tool; not meant to
# survive a restart (uploaded files on disk do, but metadata doesn't).
VIDEOS = {}
VIDEOS_LOCK = threading.Lock()

JOBS = {}
JOBS_LOCK = threading.Lock()


def _allowed_file(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in ALLOWED_EXTENSIONS


def _set_job(job_id, **fields):
    with JOBS_LOCK:
        entry = JOBS.setdefault(job_id, {
            "percent": 0, "stage": "starting", "done": False, "error": None,
        })
        entry.update(fields)
        entry["ts"] = time.time()


def _get_job(job_id):
    with JOBS_LOCK:
        entry = JOBS.get(job_id)
        return dict(entry) if entry else None


def _stage_for_percent(pct):
    """Purely a friendlier label for the same real percentage ffmpeg is
    reporting — not a separate fake timer. ffmpeg does trim + square-crop +
    text overlay in a single pass, so these are progress *bands* within
    that one real pass rather than literally sequential steps."""
    if pct < 5:
        return "Trimming clip…"
    if pct < 45:
        return "Creating square format…"
    if pct < 75:
        return "Adding text…"
    if pct < 99:
        return "Rendering video…"
    return "Finalizing…"


@app.route("/")
def index():
    return render_template(
        "index.html",
        canvas=CANVAS,
        max_text_length=MAX_TEXT_LENGTH,
        min_font_size=MIN_FONT_SIZE,
        max_font_size=MAX_FONT_SIZE,
    )


@app.route("/api/upload", methods=["POST"])
def upload():
    file = request.files.get("video")
    if not file or not file.filename:
        return jsonify({"error": "Koi video file nahi mili."}), 400
    if not _allowed_file(file.filename):
        return jsonify({"error": "Unsupported format. Use MP4, MOV, WEBM, MKV, AVI, or M4V."}), 400

    video_id = uuid.uuid4().hex
    ext = file.filename.rsplit(".", 1)[-1].lower()
    stored_path = os.path.join(UPLOAD_DIR, f"{video_id}.{ext}")
    file.save(stored_path)

    # Best-effort: move the moov atom to the front so the browser's <video>
    # preview can scrub this file over HTTP range requests. Some
    # phone-recorded MP4/MOVs write it at the end, which can otherwise make
    # the preview fail to load entirely even though the file is perfectly
    # valid for export. Falls back to the original file if this fails.
    remuxed_path = remux_faststart(stored_path)
    if remuxed_path:
        os.remove(stored_path)
        stored_path = remuxed_path

    try:
        meta = probe_video(stored_path)
    except VideoProbeError as exc:
        os.remove(stored_path)
        return jsonify({"error": f"Ye video read nahi ho payi: {exc}"}), 400

    with VIDEOS_LOCK:
        VIDEOS[video_id] = {
            "path": stored_path,
            "original_name": file.filename,
            **meta,
        }

    return jsonify({
        "video_id": video_id,
        "filename": file.filename,
        "duration": meta["duration"],
        "width": meta["width"],
        "height": meta["height"],
        "has_audio": meta["has_audio"],
    })


@app.route("/media/<video_id>")
def media(video_id):
    with VIDEOS_LOCK:
        info = VIDEOS.get(video_id)
    if not info:
        abort(404)
    return send_file(info["path"], conditional=True)


@app.route("/api/export", methods=["POST"])
def export():
    data = request.get_json(silent=True) or {}
    video_id = data.get("video_id", "")

    with VIDEOS_LOCK:
        info = VIDEOS.get(video_id)
    if not info:
        return jsonify({"error": "Video mila nahi. Dobara upload karo."}), 400

    duration = info["duration"]
    try:
        start = float(data.get("start", 0))
        end = float(data.get("end", duration))
    except (TypeError, ValueError):
        return jsonify({"error": "Start/End valid numbers nahi hain."}), 400

    start = max(0.0, min(start, duration))
    end = max(0.0, min(end, duration))
    if end - start < 0.3:
        return jsonify({"error": "Selected clip bahut chhota hai (min 0.3s)."}), 400

    text = (data.get("text") or "").strip()[:MAX_TEXT_LENGTH]
    try:
        font_size = int(data.get("font_size", 44))
    except (TypeError, ValueError):
        font_size = 44
    font_size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, font_size))

    bold = bool(data.get("bold", True))
    align = data.get("align") if data.get("align") in ("left", "center", "right") else "center"
    style = data.get("style") if data.get("style") in ("band", "overlay") else "band"
    position = data.get("position") if data.get("position") in ("top", "center", "bottom") else "top"
    if style == "band" and position == "center":
        position = "top"  # a solid band can only sensibly sit at the top or bottom

    job_id = uuid.uuid4().hex
    _set_job(job_id, percent=0, stage="starting", done=False, error=None)

    thread = threading.Thread(
        target=_run_export_job,
        args=(job_id, info, start, end, text, font_size, bold, align, style, position),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


def _run_export_job(job_id, info, start, end, text, font_size, bold, align, style, position):
    tmp_dir = tempfile.mkdtemp(prefix="export_")
    try:
        _set_job(job_id, stage="Preparing video…", percent=1)

        canvas = CANVAS
        text_image_path = None
        video_h, video_y, text_x, text_y = canvas, 0, 0, 0

        if text:
            if style == "band":
                img, band_h = render_band_image(text, canvas, font_size, bold, align)
                if position == "bottom":
                    video_h, video_y = canvas - band_h, 0
                    text_x, text_y = 0, video_h
                else:
                    video_h, video_y = canvas - band_h, band_h
                    text_x, text_y = 0, 0
            else:
                img, box_w, box_h = render_overlay_image(text, canvas, font_size, bold, align)
                video_h, video_y = canvas, 0
                margin = 40
                text_x = (canvas - box_w) // 2
                if position == "top":
                    text_y = margin
                elif position == "bottom":
                    text_y = canvas - box_h - margin
                else:
                    text_y = (canvas - box_h) // 2

            text_image_path = os.path.join(tmp_dir, "text.png")
            img.save(text_image_path)
        # else: text is empty -> text_image_path stays None, full-bleed square crop.

        overlays = []
        if text_image_path:
            overlays.append((text_image_path, text_x, text_y))

        # Fixed watermark, always added last (on top). Sits just above the
        # bottom edge of the video's own rendered area — which is usually
        # the canvas bottom, except when a caption band is anchored to the
        # bottom, in which case the watermark sits just above the band
        # instead of inside it. A bottom-anchored floating caption box gets
        # the same treatment so the two never overlap.
        wm_img, wm_w, wm_h = render_watermark_image(
            WATERMARK_TEXT, WATERMARK_FONT_SIZE, opacity=WATERMARK_OPACITY
        )
        wm_path = os.path.join(tmp_dir, "watermark.png")
        wm_img.save(wm_path)

        if text_image_path and style == "overlay" and position == "bottom":
            wm_bottom_limit = text_y
        else:
            wm_bottom_limit = video_y + video_h
        wm_x = (canvas - wm_w) // 2
        wm_y = max(0, wm_bottom_limit - WATERMARK_MARGIN - wm_h)
        overlays.append((wm_path, wm_x, wm_y))

        output_path = os.path.join(EXPORT_DIR, f"{job_id}.mp4")

        def on_progress(pct):
            _set_job(job_id, percent=round(pct, 1), stage=_stage_for_percent(pct))

        run_export(
            input_path=info["path"],
            output_path=output_path,
            start=start, end=end,
            video_h=video_h, video_y=video_y,
            has_audio=info["has_audio"],
            on_progress=on_progress,
            overlays=overlays,
        )

        _set_job(job_id, percent=100, stage="Finalizing…", done=True,
                  download_name=_safe_filename(info["original_name"]))
    except ExportError as exc:
        _set_job(job_id, error=str(exc), done=True, stage="error")
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected to the UI
        _set_job(job_id, error=str(exc), done=True, stage="error")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _safe_filename(name):
    base = os.path.splitext(name or "video")[0]
    base = re.sub(r'[\\/:*?"<>|]+', "_", base).strip() or "video"
    return f"{base}_square.mp4"


@app.route("/api/export/progress/<job_id>")
def export_progress(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


@app.route("/api/export/download/<job_id>")
def export_download(job_id):
    job = _get_job(job_id)
    output_path = os.path.join(EXPORT_DIR, f"{job_id}.mp4")
    if not job or not job.get("done") or job.get("error") or not os.path.exists(output_path):
        abort(404)
    return send_file(
        output_path,
        as_attachment=True,
        download_name=job.get("download_name", "video_square.mp4"),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5050, threaded=True)
