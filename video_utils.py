"""ffprobe/ffmpeg helpers: reading video metadata and running the actual
trim + square-crop + text-burn export with real progress reporting."""
import json
import os
import re
import shutil
import subprocess
import threading


class VideoProbeError(Exception):
    pass


class ExportError(Exception):
    pass


# We first check if ffmpeg is already on the system PATH; if not, we fall
# back to the bundled static binary that ships inside the imageio-ffmpeg pip
# package, so no manual ffmpeg install is required on the machine running
# this app (same pattern used by the yt-downloader tool, where a missing
# system ffmpeg was a real bug users hit).
def _find_ffmpeg():
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


# imageio-ffmpeg only bundles the ffmpeg binary, not ffprobe. If ffprobe
# isn't on PATH, we fall back to asking ffmpeg itself for the same
# information via `-i` and parse the human-readable info it prints to
# stderr — ffmpeg always ships with a full demuxer/decoder stack, it just
# doesn't have ffprobe's structured JSON output.
def _find_ffprobe():
    return shutil.which("ffprobe")


FFMPEG_PATH = _find_ffmpeg() or "ffmpeg"
FFPROBE_PATH = _find_ffprobe()


def _probe_via_ffmpeg_fallback(path):
    """Best-effort metadata extraction using `ffmpeg -i` when ffprobe isn't
    available at all. Parses ffmpeg's stderr banner, which always includes a
    Duration line and one Stream line per stream."""
    result = subprocess.run(
        [FFMPEG_PATH, "-i", path], capture_output=True, text=True
    )
    banner = result.stderr or ""

    dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", banner)
    if not dur_match:
        raise VideoProbeError("Could not read this video's duration/dimensions")
    h, m, s = dur_match.groups()
    duration = int(h) * 3600 + int(m) * 60 + float(s)

    video_match = re.search(r"Stream #\d+:\d+.*?Video:.*?(\d{2,5})x(\d{2,5})", banner)
    if not video_match:
        raise VideoProbeError("No video stream found in this file")
    width, height = int(video_match.group(1)), int(video_match.group(2))

    fps = 30.0
    fps_match = re.search(r"(\d+(?:\.\d+)?)\s*fps", banner)
    if fps_match:
        try:
            fps = float(fps_match.group(1))
        except ValueError:
            pass

    has_audio = bool(re.search(r"Stream #\d+:\d+.*?Audio:", banner))

    if duration <= 0 or width <= 0 or height <= 0:
        raise VideoProbeError("Could not read this video's duration/dimensions")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": has_audio,
        "fps": fps,
    }


def remux_faststart(path):
    """Best-effort, lossless (-c copy) remux that moves the MP4/MOV 'moov'
    atom to the front of the file. Many phone-recorded videos write it at
    the end, which some browsers' <video> elements fail to demux at all
    when the file is served progressively over HTTP range requests (as ours
    is, for scrubbing) — they only ever see the front of the file and never
    find the index. This is quick (no re-encoding) and safe to skip on
    failure, since the original file still works for the export pipeline
    either way.

    Returns the new path on success, or None if the remux wasn't attempted
    or failed (caller should keep using the original path in that case).
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext not in ("mp4", "mov", "m4v"):
        return None  # faststart is an MP4/MOV-family concept

    remuxed_path = f"{path}.faststart.mp4"
    cmd = [
        FFMPEG_PATH, "-y", "-i", path,
        "-c", "copy", "-movflags", "+faststart",
        remuxed_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(remuxed_path):
        if os.path.exists(remuxed_path):
            os.remove(remuxed_path)
        return None
    return remuxed_path


def probe_video(path):
    """Returns {duration, width, height, has_audio, fps} for a video file."""
    if not FFPROBE_PATH:
        return _probe_via_ffmpeg_fallback(path)

    cmd = [
        FFPROBE_PATH, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise VideoProbeError(result.stderr.strip() or "ffprobe failed to read this file")

    data = json.loads(result.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not video_stream:
        raise VideoProbeError("No video stream found in this file")

    duration = float(fmt.get("duration") or video_stream.get("duration") or 0)
    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)

    fps = 30.0
    rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
    if rate and rate != "0/0":
        try:
            num, den = rate.split("/")
            if float(den) > 0:
                fps = float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            pass

    if duration <= 0 or width <= 0 or height <= 0:
        raise VideoProbeError("Could not read this video's duration/dimensions")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": audio_stream is not None,
        "fps": fps,
    }


def _even(n):
    n = int(round(n))
    return n if n % 2 == 0 else n + 1


def build_export_filters(canvas_w, canvas_h, video_h, video_y, overlays):
    """Builds the ffmpeg filter_complex graph for: crop/scale the source
    video to fill its target area without distortion (the "cover" idiom),
    composite it onto a black canvas, then stack zero or more pre-rendered
    image layers on top in order (each just a PNG by this point — a caption
    band/box, a watermark, timed subtitle lines, etc). `overlays` is a list
    of (x, y, enable) tuples; overlay i corresponds to ffmpeg input index
    i+1 (input 0 is always the source video). `enable` is either None (the
    layer is visible for the whole clip) or an ffmpeg timeline expression
    (e.g. "between(t,1.20,3.40)") so the layer only appears during that
    window — how subtitle lines are shown only while they're being spoken.
    """
    video_h = _even(video_h)
    parts = [
        f"[0:v]scale={canvas_w}:{video_h}:force_original_aspect_ratio=increase,"
        f"crop={canvas_w}:{video_h},setsar=1[vid]",
        f"color=c=black:s={canvas_w}x{canvas_h}[bg]",
    ]
    if not overlays:
        parts.append(f"[bg][vid]overlay=0:{video_y}[outv]")
        return ";".join(parts)

    parts.append(f"[bg][vid]overlay=0:{video_y}[stage0]")
    prev_label = "stage0"
    for idx, (x, y, enable) in enumerate(overlays):
        input_idx = idx + 1
        stage_label = "outv" if idx == len(overlays) - 1 else f"stage{idx + 1}"
        enable_clause = f":enable='{enable}'" if enable else ""
        parts.append(f"[{prev_label}][{input_idx}:v]overlay={x}:{y}{enable_clause}[{stage_label}]")
        prev_label = stage_label
    return ";".join(parts)


def run_export(input_path, output_path, start, end, canvas_w, canvas_h, video_h, video_y,
                has_audio, on_progress, overlays=None):
    """Runs the ffmpeg export as a subprocess, calling on_progress(percent)
    as it reports real encoding progress via -progress pipe:1. Raises
    ExportError with ffmpeg's own message on failure.

    overlays: list of (path, x, y, enable) tuples, layered in order on top of
    the cropped video (e.g. the caption band/box, then subtitle lines, then
    the watermark on top of everything). `enable` is None for a layer shown
    for the whole clip, or an ffmpeg timeline expression to show it only
    during a time window (subtitle lines). An empty/None list means the crop
    is composited with no extra inputs at all, rather than faking invisible
    overlays.
    """
    overlays = overlays or []
    duration = max(0.01, end - start)

    filter_complex = build_export_filters(
        canvas_w, canvas_h, video_h, video_y, [(x, y, enable) for _, x, y, enable in overlays]
    )

    cmd = [FFMPEG_PATH, "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", input_path]
    for path, _, _, _ in overlays:
        cmd += ["-loop", "1", "-i", path]
    cmd += ["-filter_complex", filter_complex, "-map", "[outv]"]
    if has_audio:
        cmd += ["-map", "0:a?"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    cmd += [
        "-movflags", "+faststart",
        # NOTE: -shortest is NOT reliable here. The filter graph includes
        # infinite synthetic sources (the color= black canvas, and a
        # -loop 1 looped text-image input), which lack a real demuxer EOF
        # for -shortest to detect — ffmpeg's overlay filter follows its
        # "main" (first) input's duration by default, which traces back to
        # the infinite color= source, so encoding never stops on its own.
        # An explicit -t on the output reliably caps it to the real trimmed
        # duration regardless of what's in the filter graph.
        "-t", f"{duration:.3f}",
        "-progress", "pipe:1", "-nostats",
        output_path,
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    # ffmpeg writes progress on stdout and logs/warnings on stderr
    # concurrently. If we only read stdout here, stderr's pipe buffer can
    # fill up and block ffmpeg mid-write, which in turn blocks our stdout
    # read forever — a classic subprocess deadlock. Draining stderr on its
    # own thread avoids that.
    stderr_lines = []

    def _drain_stderr():
        for line in proc.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    time_re = re.compile(r"out_time_ms=(\d+)")

    for line in proc.stdout:
        line = line.strip()
        match = time_re.match(line)
        if match:
            out_time_s = int(match.group(1)) / 1_000_000
            pct = min(99, (out_time_s / duration) * 100)
            on_progress(pct)
        elif line == "progress=end":
            on_progress(100)

    proc.wait()
    stderr_thread.join(timeout=5)

    if proc.returncode != 0:
        tail = "".join(stderr_lines[-15:])
        raise ExportError(tail.strip() or "ffmpeg exited with an error")
