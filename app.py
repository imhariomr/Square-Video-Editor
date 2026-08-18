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
import zipfile

from flask import (
    Flask, render_template, request, jsonify, send_file, abort
)

from video_utils import (
    probe_video, run_export, remux_faststart, VideoProbeError, ExportError,
    FFMPEG_PATH, FFPROBE_PATH,
)
from text_render import (
    render_band_image, render_overlay_image, render_watermark_image, render_subtitle_image,
    MIN_FONT_SIZE, MAX_FONT_SIZE, SUBTITLE_PRESETS,
)
from subtitle_utils import transcribe_clip, SubtitleError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {"mp4", "mov", "m4v", "webm", "mkv", "avi"}
MAX_TEXT_LENGTH = 300
MIN_CLIP_SECONDS = 0.3

# Output canvas sizes per supported aspect ratio (width, height). 1:1 is the
# default so existing behavior is unchanged unless the user picks another.
ASPECT_RATIOS = {
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
}
DEFAULT_ASPECT_RATIO = "1:1"

# Optional watermark burned into the export (bottom-center, small bold white
# text with a subtle shadow) — user opts in and supplies the text; only the
# look (size/margin/opacity) is fixed.
WATERMARK_MAX_LENGTH = 60
WATERMARK_FONT_SIZE = 26
WATERMARK_MARGIN = 30
WATERMARK_OPACITY = 0.55

# Auto-generated subtitles (faster-whisper, tiny model) — burned in as
# timed floating caption boxes near the bottom, reusing the same rendering
# as the manual "floating box" text overlay style.
SUBTITLE_FONT_SIZE = 34
SUBTITLE_MARGIN = 36

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024  # 8GB upload cap


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Video file is too large (max 8GB)."}), 413


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
        return "Cropping to frame…"
    if pct < 75:
        return "Adding text…"
    if pct < 99:
        return "Rendering video…"
    return "Finalizing…"


@app.route("/")
def index():
    return render_template(
        "index.html",
        aspect_ratios=ASPECT_RATIOS,
        default_aspect_ratio=DEFAULT_ASPECT_RATIO,
        max_text_length=MAX_TEXT_LENGTH,
        watermark_max_length=WATERMARK_MAX_LENGTH,
        min_font_size=MIN_FONT_SIZE,
        max_font_size=MAX_FONT_SIZE,
    )


@app.route("/health")
def health():
    """Lightweight liveness/readiness check — no subprocess calls, no disk
    I/O, just reports whether the server is up and whether it actually
    found a usable ffmpeg/ffprobe at startup. Safe to point a host's health
    check (e.g. Render) or an uptime monitor at this."""
    return jsonify({
        "status": "ok",
        "ffmpeg_available": bool(FFMPEG_PATH),
        "ffprobe_available": bool(FFPROBE_PATH),
    })


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


@app.route("/api/subtitles", methods=["POST"])
def subtitles_preview():
    """Transcribes the current [start, end] trim range and returns the
    caption segments (clip-relative timestamps) so the editor can preview
    them live before export runs the same transcription for real."""
    data = request.get_json(silent=True) or {}
    video_id = data.get("video_id", "")

    with VIDEOS_LOCK:
        info = VIDEOS.get(video_id)
    if not info:
        return jsonify({"error": "Video mila nahi. Dobara upload karo."}), 400
    if not info["has_audio"]:
        return jsonify({"error": "This video has no audio track to transcribe."}), 400

    duration = info["duration"]
    try:
        start = float(data.get("start", 0))
        end = float(data.get("end", duration))
    except (TypeError, ValueError):
        return jsonify({"error": "Start/End valid numbers nahi hain."}), 400
    start = max(0.0, min(start, duration))
    end = max(0.0, min(end, duration))
    if end - start < MIN_CLIP_SECONDS:
        return jsonify({"error": "Selected clip bahut chhota hai (min 0.3s)."}), 400

    tmp_dir = tempfile.mkdtemp(prefix="subs_")
    try:
        segments = transcribe_clip(info["path"], start, end, tmp_dir)
    except SubtitleError as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return jsonify({
        "segments": [{"start": s, "end": e, "text": t} for s, e, t in segments],
    })


def _parse_export_config(data, info):
    """Validates and normalizes the export config shared by /api/export and
    /api/movie-cut. Returns (config_dict, None) on success, or (None,
    error_message) so the caller can jsonify it as a 400."""
    duration = info["duration"]
    try:
        start = float(data.get("start", 0))
        end = float(data.get("end", duration))
    except (TypeError, ValueError):
        return None, "Start/End valid numbers nahi hain."

    start = max(0.0, min(start, duration))
    end = max(0.0, min(end, duration))
    if end - start < MIN_CLIP_SECONDS:
        return None, "Selected clip bahut chhota hai (min 0.3s)."

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

    aspect_ratio = data.get("aspect_ratio") if data.get("aspect_ratio") in ASPECT_RATIOS else DEFAULT_ASPECT_RATIO

    watermark_text = ""
    if data.get("watermark_enabled"):
        watermark_text = (data.get("watermark_text") or "").strip()[:WATERMARK_MAX_LENGTH]

    subtitles_enabled = bool(data.get("subtitles_enabled")) and info["has_audio"]
    subtitle_preset = data.get("subtitle_preset") if data.get("subtitle_preset") in SUBTITLE_PRESETS else "none"

    return {
        "start": start, "end": end, "text": text, "font_size": font_size, "bold": bold,
        "align": align, "style": style, "position": position, "aspect_ratio": aspect_ratio,
        "watermark_text": watermark_text, "subtitles_enabled": subtitles_enabled,
        "subtitle_preset": subtitle_preset,
    }, None


def _build_base_overlays(tmp_dir, canvas_w, canvas_h, cfg):
    """Renders the manual caption + watermark layers — both independent of
    which [start, end] range is actually being rendered, so movie-cut can
    render them once and reuse them across every clip. Returns (video_h,
    video_y, overlays, bottom_limit) where bottom_limit is the y-coordinate
    subtitles should stack above."""
    text, font_size, bold, align = cfg["text"], cfg["font_size"], cfg["bold"], cfg["align"]
    style, position, watermark_text = cfg["style"], cfg["position"], cfg["watermark_text"]

    text_image_path = None
    video_h, video_y, text_x, text_y = canvas_h, 0, 0, 0

    if text:
        if style == "band":
            img, band_h = render_band_image(text, canvas_w, font_size, bold, align)
            if position == "bottom":
                video_h, video_y = canvas_h - band_h, 0
                text_x, text_y = 0, video_h
            else:
                video_h, video_y = canvas_h - band_h, band_h
                text_x, text_y = 0, 0
        else:
            img, box_w, box_h = render_overlay_image(text, canvas_w, font_size, bold, align)
            video_h, video_y = canvas_h, 0
            margin = 40
            text_x = (canvas_w - box_w) // 2
            if position == "top":
                text_y = margin
            elif position == "bottom":
                text_y = canvas_h - box_h - margin
            else:
                text_y = (canvas_h - box_h) // 2

        text_image_path = os.path.join(tmp_dir, "text.png")
        img.save(text_image_path)
    # else: text is empty -> text_image_path stays None, full-bleed crop.

    overlays = []
    if text_image_path:
        overlays.append((text_image_path, text_x, text_y, None))

    # Optional watermark. Sits just above the bottom edge of the video's
    # own rendered area — which is usually the canvas bottom, except when a
    # caption band/box is anchored to the bottom, in which case it sits
    # just above that instead of inside it.
    if text_image_path and style == "overlay" and position == "bottom":
        bottom_limit = text_y
    else:
        bottom_limit = video_y + video_h

    if watermark_text:
        wm_img, wm_w, wm_h = render_watermark_image(
            watermark_text, WATERMARK_FONT_SIZE, opacity=WATERMARK_OPACITY
        )
        wm_path = os.path.join(tmp_dir, "watermark.png")
        wm_img.save(wm_path)

        wm_x = (canvas_w - wm_w) // 2
        wm_y = max(0, bottom_limit - WATERMARK_MARGIN - wm_h)
        overlays.append((wm_path, wm_x, wm_y, None))
        bottom_limit = wm_y  # subtitles stack above the watermark, not on top of it

    return video_h, video_y, overlays, bottom_limit


def _render_subtitle_overlays(tmp_dir, segments, canvas_w, bottom_limit, preset, prefix="subtitle_"):
    """Reuses the auto-generated-subtitle renderer for each timed line,
    timed to only show during its own window via ffmpeg's overlay `enable`."""
    overlays = []
    for idx, (seg_start, seg_end, seg_text) in enumerate(segments):
        sub_img, sub_w, sub_h = render_subtitle_image(seg_text, canvas_w, SUBTITLE_FONT_SIZE, preset)
        sub_path = os.path.join(tmp_dir, f"{prefix}{idx}.png")
        sub_img.save(sub_path)
        sub_x = (canvas_w - sub_w) // 2
        sub_y = max(0, bottom_limit - SUBTITLE_MARGIN - sub_h)
        enable = f"between(t,{seg_start:.3f},{seg_end:.3f})"
        overlays.append((sub_path, sub_x, sub_y, enable))
    return overlays


@app.route("/api/export", methods=["POST"])
def export():
    data = request.get_json(silent=True) or {}
    video_id = data.get("video_id", "")

    with VIDEOS_LOCK:
        info = VIDEOS.get(video_id)
    if not info:
        return jsonify({"error": "Video mila nahi. Dobara upload karo."}), 400

    cfg, error = _parse_export_config(data, info)
    if error:
        return jsonify({"error": error}), 400

    job_id = uuid.uuid4().hex
    _set_job(job_id, percent=0, stage="starting", done=False, error=None)

    thread = threading.Thread(target=_run_export_job, args=(job_id, info, cfg), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


def _run_export_job(job_id, info, cfg):
    tmp_dir = tempfile.mkdtemp(prefix="export_")
    try:
        _set_job(job_id, stage="Preparing video…", percent=1)

        canvas_w, canvas_h = ASPECT_RATIOS[cfg["aspect_ratio"]]
        video_h, video_y, overlays, bottom_limit = _build_base_overlays(tmp_dir, canvas_w, canvas_h, cfg)

        # Optional auto-generated subtitles: transcribe the trimmed audio,
        # then reuse the same renderer as the manual overlay caption for
        # each line, timed to only show during its own window.
        if cfg["subtitles_enabled"]:
            _set_job(job_id, stage="Transcribing audio…", percent=2)
            segments = transcribe_clip(info["path"], cfg["start"], cfg["end"], tmp_dir)
            overlays += _render_subtitle_overlays(tmp_dir, segments, canvas_w, bottom_limit, cfg["subtitle_preset"])

        output_path = os.path.join(EXPORT_DIR, f"{job_id}.mp4")

        def on_progress(pct):
            _set_job(job_id, percent=round(pct, 1), stage=_stage_for_percent(pct))

        run_export(
            input_path=info["path"],
            output_path=output_path,
            start=cfg["start"], end=cfg["end"],
            canvas_w=canvas_w, canvas_h=canvas_h,
            video_h=video_h, video_y=video_y,
            has_audio=info["has_audio"],
            on_progress=on_progress,
            overlays=overlays,
        )

        _set_job(job_id, percent=100, stage="Finalizing…", done=True,
                  download_name=_safe_filename(info["original_name"]), output_path=output_path)
    except ExportError as exc:
        _set_job(job_id, error=str(exc), done=True, stage="error")
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected to the UI
        _set_job(job_id, error=str(exc), done=True, stage="error")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.route("/api/movie-cut", methods=["POST"])
def movie_cut():
    """'Movie Cut' filter: slices the current trim range into fixed-length
    clips, renders each with whatever caption/watermark/subtitle config is
    currently selected, and zips them all into one download."""
    data = request.get_json(silent=True) or {}
    video_id = data.get("video_id", "")

    with VIDEOS_LOCK:
        info = VIDEOS.get(video_id)
    if not info:
        return jsonify({"error": "Video mila nahi. Dobara upload karo."}), 400

    cfg, error = _parse_export_config(data, info)
    if error:
        return jsonify({"error": error}), 400

    try:
        clip_seconds = float(data.get("clip_duration_seconds", 60))
    except (TypeError, ValueError):
        return jsonify({"error": "Clip duration valid number nahi hai."}), 400
    clip_seconds = max(MIN_CLIP_SECONDS, min(clip_seconds, cfg["end"] - cfg["start"]))

    job_id = uuid.uuid4().hex
    _set_job(job_id, percent=0, stage="starting", done=False, error=None)

    thread = threading.Thread(target=_run_moviecut_job, args=(job_id, info, cfg, clip_seconds), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


def _run_moviecut_job(job_id, info, cfg, clip_seconds):
    tmp_dir = tempfile.mkdtemp(prefix="moviecut_")
    try:
        _set_job(job_id, stage="Preparing clips…", percent=1)

        canvas_w, canvas_h = ASPECT_RATIOS[cfg["aspect_ratio"]]
        video_h, video_y, base_overlays, bottom_limit = _build_base_overlays(tmp_dir, canvas_w, canvas_h, cfg)

        chunks = []
        t = cfg["start"]
        while t < cfg["end"] - 0.05:
            chunk_end = min(t + clip_seconds, cfg["end"])
            if chunk_end - t >= MIN_CLIP_SECONDS:
                chunks.append((t, chunk_end))
            t = chunk_end
        if not chunks:
            raise ExportError("Clip is too short to cut into pieces of that duration.")

        clip_paths = []
        for idx, (c_start, c_end) in enumerate(chunks):
            overlays = list(base_overlays)
            if cfg["subtitles_enabled"]:
                _set_job(job_id, stage=f"Transcribing clip {idx + 1}/{len(chunks)}…",
                          percent=round(idx / len(chunks) * 100, 1))
                segments = transcribe_clip(info["path"], c_start, c_end, tmp_dir)
                overlays += _render_subtitle_overlays(tmp_dir, segments, canvas_w, bottom_limit,
                                                        cfg["subtitle_preset"], prefix=f"c{idx}_")

            clip_path = os.path.join(tmp_dir, f"clip_{idx + 1:03d}.mp4")

            def on_progress(pct, idx=idx):
                overall = ((idx + pct / 100) / len(chunks)) * 100
                _set_job(job_id, percent=round(overall, 1), stage=f"Rendering clip {idx + 1}/{len(chunks)}…")

            run_export(
                input_path=info["path"],
                output_path=clip_path,
                start=c_start, end=c_end,
                canvas_w=canvas_w, canvas_h=canvas_h,
                video_h=video_h, video_y=video_y,
                has_audio=info["has_audio"],
                on_progress=on_progress,
                overlays=overlays,
            )
            clip_paths.append(clip_path)

        _set_job(job_id, stage="Zipping clips…", percent=99)
        zip_path = os.path.join(EXPORT_DIR, f"{job_id}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, clip_path in enumerate(clip_paths):
                zf.write(clip_path, arcname=f"clip_{idx + 1:03d}.mp4")

        _set_job(job_id, percent=100, stage="Finalizing…", done=True,
                  download_name=_safe_zip_filename(info["original_name"]), output_path=zip_path)
    except ExportError as exc:
        _set_job(job_id, error=str(exc), done=True, stage="error")
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected to the UI
        _set_job(job_id, error=str(exc), done=True, stage="error")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _safe_filename(name):
    base = os.path.splitext(name or "video")[0]
    base = re.sub(r'[\\/:*?"<>|]+', "_", base).strip() or "video"
    return f"{base}_edited.mp4"


def _safe_zip_filename(name):
    base = os.path.splitext(name or "video")[0]
    base = re.sub(r'[\\/:*?"<>|]+', "_", base).strip() or "video"
    return f"{base}_cuts.zip"


@app.route("/api/export/progress/<job_id>")
def export_progress(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


@app.route("/api/export/download/<job_id>")
def export_download(job_id):
    job = _get_job(job_id)
    output_path = job.get("output_path") if job else None
    if not job or not job.get("done") or job.get("error") or not output_path or not os.path.exists(output_path):
        abort(404)
    return send_file(
        output_path,
        as_attachment=True,
        download_name=job.get("download_name", "video_edited.mp4"),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5050, threaded=True)
