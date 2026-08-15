# Square Video Editor (Flask + ffmpeg)

Local web app: upload a video, trim it to a start/end range, add a bold
caption (top/bottom black band, or a floating semi-transparent box), and
export a real 1:1 (1080x1080) MP4 with the trim and text permanently baked
in — not a preview trick, the exported file is actually cropped, trimmed,
and captioned.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5050 in your browser.

## How it works

- **Upload** — the video is probed with ffprobe (duration, resolution,
  audio) and, if it's an MP4/MOV/M4V, losslessly remuxed with
  `-movflags +faststart` so the browser preview can scrub it smoothly (many
  phone-recorded videos store their index at the end of the file, which
  some browsers otherwise fail to stream).
- **Live preview** — the trim, timeline, and caption controls update an
  on-screen CSS/DOM approximation instantly, with no server round-trip.
  Play / Pause / Restart only play the selected clip range.
- **Export** — clicking "Export square video" runs one real ffmpeg pass on
  the server: trims to the exact start/end, scales+crops the source onto a
  1080x1080 canvas without distortion (same idiom as CSS `object-fit:
  cover`), and composites a Pillow-rendered caption image (real word-wrap,
  not ffmpeg's non-wrapping `drawtext`) on top. Progress shown in the UI is
  read from ffmpeg's own `-progress` output — it is the real encoding
  percentage, not a simulated timer.

## Notes

- ffmpeg/ffprobe: the app looks for both on your system PATH first. If
  `ffmpeg` isn't found, it falls back to the bundled static binary from the
  `imageio-ffmpeg` package (no manual install needed). If `ffprobe` isn't
  found anywhere, it falls back to parsing `ffmpeg -i`'s own output — less
  precise, but avoids a hard dependency on ffprobe specifically.
- Uploads and exports are kept in `uploads/` and `exports/` next to
  `app.py`; nothing is uploaded anywhere else. Feel free to delete old
  files from those folders — nothing tracks them after the app restarts.
- Minimum clip length is 0.3s; maximum caption length is 300 characters;
  max upload size is 1GB. These are easy to change at the top of `app.py`.
- Supported input formats: MP4, MOV, M4V, WEBM, MKV, AVI.
- `GET /health` returns `{"status": "ok", "ffmpeg_available": ..., "ffprobe_available": ...}` —
  a lightweight check with no disk I/O, safe for a host's health check or
  an uptime monitor (already wired into `render.yaml` via `healthCheckPath`).
- Trim inputs accept either plain seconds ("71.2") or m:ss / h:mm:ss
  ("1:11.2"). Whenever Start is changed (typed, dragged, or slider), End
  automatically follows to a fixed clip length (`AUTO_CLIP_LENGTH` in
  `static/app.js`, currently 29.49s), clamped to the video's actual
  duration if there isn't enough room left. End can still be typed/dragged
  independently afterward.
- Every export gets a small fixed watermark ("dumbestposting") burned in
  bottom-center, matching what's shown in the live preview. It's
  positioned to automatically sit just above a bottom-anchored caption
  band/box instead of overlapping it. To change the text or styling, edit
  `WATERMARK_TEXT` / `WATERMARK_FONT_SIZE` / `WATERMARK_MARGIN` in
  `app.py` (and the matching constants in `static/app.js` so the preview
  stays in sync).

## Deploying to Render

The simplest path needs no Dockerfile at all: `requirements.txt` already
includes `imageio-ffmpeg` (a bundled static ffmpeg binary) and
`video_utils.py` already falls back to parsing `ffmpeg -i`'s own output
when `ffprobe` specifically isn't installed — so Render's native Python
runtime (which doesn't offer a way to `apt-get install ffmpeg`) still
works fine.

1. Push this project to a GitHub (or GitLab/Bitbucket) repo — Render
   deploys from a connected repo, not a local folder.
2. In the Render dashboard: **New → Blueprint**, pick the repo. Render
   reads `render.yaml` (included) and sets everything up automatically —
   Python runtime, build command, start command, free plan. Review and
   click **Apply**.
   - Prefer to configure it by hand instead? **New → Web Service**, pick
     the repo, Language = **Python 3**, then set:
     - Build Command: `pip install -r requirements.txt`
     - Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120`
3. Deploy. Render assigns a `https://<name>.onrender.com` URL — open that
   instead of `127.0.0.1:5050`.

**Things that behave differently on Render than on your own machine:**

- **Ephemeral storage.** `uploads/`, `exports/`, and the in-memory
  job/video tracking dicts in `app.py` are all wiped on every restart or
  redeploy (this is standard for Render's free/default disk — the
  workflow here is "upload → edit → export → download in one sitting,"
  not long-term storage, so this is expected rather than a bug). If you
  need uploads to survive restarts, Render's **Persistent Disks** feature
  can be attached to a web service, but it's paid-plan only.
- **Free plan spin-down.** A free web service spins down after 15 minutes
  with no traffic, and takes roughly a minute to wake back up on the next
  request — including losing anything in `uploads/`/`exports/` from before
  it spun down. Fine for occasional/personal use; annoying if you want it
  always instantly available (upgrade the plan to avoid it).
- **Upload size vs. free-plan memory.** `app.py`'s `MAX_CONTENT_LENGTH` is
  set to 1GB — reasonable locally, but worth lowering (e.g. to
  200-300MB) if you're on a memory-constrained free instance and hit
  crashes on large uploads.

**Want real system ffmpeg/ffprobe instead of the pip fallback?** A
`Dockerfile` is included that installs actual ffmpeg via `apt-get`. On
Render, set the service's Language to **Docker** (instead of Python) and
it'll build/run that file instead of `render.yaml`'s
buildCommand/startCommand — same behavior, no code changes needed either
way since `video_utils.py` always prefers a real system ffmpeg/ffprobe
when present.
