/**
 * Square Video Editor — frontend.
 *
 * Everything here is organized around one `state` object plus a small set
 * of pure "sync" functions that re-render the relevant piece of UI from
 * that state. Controls only ever mutate `state` and then call the render
 * function for what they touched — there's no duplicated logic scattered
 * across event handlers.
 *
 * The live preview (video + black band / floating caption box) is a CSS/DOM
 * approximation of what the server will actually render with ffmpeg+Pillow:
 * same font, same relative font size, same padding/line-height, same
 * alignment — scaled down to the on-screen preview size. It updates
 * instantly with no server round trip. The *export* is the real,
 * independently-playable trimmed+cropped+captioned MP4, produced by ffmpeg
 * on the server (see /api/export).
 */
(() => {
  "use strict";

  const root = document.querySelector(".app");
  const CANVAS = parseInt(root.dataset.canvas, 10) || 1080;
  const MAX_TEXT_LENGTH = parseInt(root.dataset.maxText, 10) || 300;
  const MIN_FONT = parseInt(root.dataset.minFont, 10) || 18;
  const MAX_FONT = parseInt(root.dataset.maxFont, 10) || 96;
  const MIN_CLIP = 0.3; // keep in sync with app.py's minimum clip length
  const AUTO_CLIP_LENGTH = 29.49; // default clip length auto-applied whenever Start changes

  // Fixed watermark shown in the preview and burned into every export.
  // Keep these four in sync with WATERMARK_* in app.py so the preview
  // never shows something different from what actually gets rendered.
  const WATERMARK_FONT_SIZE = 26;
  const WATERMARK_MARGIN = 30;
  const WATERMARK_OPACITY = 0.55;

  // ---------------------------------------------------------------------
  // DOM references
  // ---------------------------------------------------------------------
  const dom = {
    uploadZone: document.querySelector("[data-upload-zone]"),
    dropzone: document.querySelector("[data-dropzone]"),
    fileInput: document.querySelector("[data-file-input]"),
    uploadError: document.querySelector("[data-upload-error]"),

    editor: document.querySelector("[data-editor]"),
    currentFilename: document.querySelector("[data-current-filename]"),
    currentMeta: document.querySelector("[data-current-meta]"),
    replaceBtn: document.querySelector("[data-replace-btn]"),
    removeBtn: document.querySelector("[data-remove-btn]"),

    previewCanvas: document.querySelector("[data-preview-canvas]"),
    previewVideo: document.querySelector("[data-preview-video]"),
    textBand: document.querySelector("[data-text-band]"),
    textOverlay: document.querySelector("[data-text-overlay]"),
    watermark: document.querySelector("[data-watermark]"),

    playPauseBtn: document.querySelector("[data-playpause-btn]"),
    restartBtn: document.querySelector("[data-restart-btn]"),
    playbackTime: document.querySelector("[data-playback-time]"),

    labelStart: document.querySelector("[data-label-start]"),
    labelEnd: document.querySelector("[data-label-end]"),
    labelDuration: document.querySelector("[data-label-duration]"),
    timelineTrack: document.querySelector("[data-timeline-track]"),
    timelineRange: document.querySelector("[data-timeline-range]"),
    timelinePlayhead: document.querySelector("[data-timeline-playhead]"),
    handleStart: document.querySelector('[data-handle="start"]'),
    handleEnd: document.querySelector('[data-handle="end"]'),

    startInput: document.querySelector("[data-start-input]"),
    endInput: document.querySelector("[data-end-input]"),
    startSlider: document.querySelector("[data-start-slider]"),
    endSlider: document.querySelector("[data-end-slider]"),
    trimError: document.querySelector("[data-trim-error]"),

    textInput: document.querySelector("[data-text-input]"),
    fontSizeSlider: document.querySelector("[data-font-size-slider]"),
    fontSizeReadout: document.querySelector("[data-font-size-readout]"),
    boldToggle: document.querySelector("[data-bold-toggle]"),
    alignGroup: document.querySelector("[data-align-group]"),
    styleSelect: document.querySelector("[data-style-select]"),
    positionSelect: document.querySelector("[data-position-select]"),

    exportBtn: document.querySelector("[data-export-btn]"),
    exportProgress: document.querySelector("[data-export-progress]"),
    exportProgressFill: document.querySelector("[data-export-progress-fill]"),
    exportProgressLabel: document.querySelector("[data-export-progress-label]"),
    exportError: document.querySelector("[data-export-error]"),
    exportResult: document.querySelector("[data-export-result]"),
    downloadLink: document.querySelector("[data-download-link]"),
    exportAgainBtn: document.querySelector("[data-export-again-btn]"),
  };

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------
  const state = {
    videoId: null,
    filename: "",
    duration: 0,
    width: 0,
    height: 0,
    hasAudio: false,

    start: 0,
    end: 0,

    text: "",
    fontSize: 44,
    bold: true,
    align: "center",
    style: "band", // "band" | "overlay"
    position: "top", // "top" | "center" | "bottom"

    exportJobId: null,
    // Bumped every time an export is (re)started or reset. Each polling
    // loop captures the epoch it was started with and checks it before
    // ever touching the DOM — so if the video gets replaced/removed, or a
    // new export starts, mid-poll, an old/abandoned loop's late-arriving
    // response can never be mistaken for the current export's completion.
    exportEpoch: 0,
  };

  // ---------------------------------------------------------------------
  // Small helpers
  // ---------------------------------------------------------------------
  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  function formatTime(sec) {
    if (!isFinite(sec) || sec < 0) sec = 0;
    const m = Math.floor(sec / 60);
    const s = sec - m * 60;
    return `${m}:${s.toFixed(2).padStart(5, "0")}`;
  }

  // Accepts either plain seconds ("71.2") or m:ss / h:mm:ss ("1:11.2"), so
  // people trimming a long video don't have to do the seconds math by hand.
  // Returns NaN for anything that isn't a complete, valid time yet (e.g.
  // while someone is still mid-way through typing "1:").
  function parseTimeInput(str) {
    if (str == null) return NaN;
    str = str.trim();
    if (!str) return NaN;

    if (str.includes(":")) {
      const parts = str.split(":").map((p) => p.trim());
      if (parts.length > 3 || parts.some((p) => p === "" || isNaN(parseFloat(p)))) {
        return NaN;
      }
      const seconds = parseFloat(parts.pop());
      const minutes = parts.length ? parseInt(parts.pop(), 10) : 0;
      const hours = parts.length ? parseInt(parts.pop(), 10) : 0;
      if (isNaN(seconds) || isNaN(minutes) || isNaN(hours)) return NaN;
      return hours * 3600 + minutes * 60 + seconds;
    }

    const v = parseFloat(str);
    return isNaN(v) ? NaN : v;
  }

  function resetEditorInputs() {
    dom.uploadError.textContent = "";
    dom.trimError.textContent = "";
    dom.exportError.textContent = "";
  }

  // ---------------------------------------------------------------------
  // Upload handling
  // ---------------------------------------------------------------------
  dom.dropzone.addEventListener("click", () => dom.fileInput.click());
  dom.dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      dom.fileInput.click();
    }
  });
  dom.replaceBtn.addEventListener("click", () => dom.fileInput.click());
  dom.removeBtn.addEventListener("click", () => removeVideo());

  ["dragenter", "dragover"].forEach((evt) =>
    dom.dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dom.dropzone.classList.add("is-dragover");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dom.dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dom.dropzone.classList.remove("is-dragover");
    })
  );
  dom.dropzone.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) uploadFile(file);
  });
  dom.fileInput.addEventListener("change", () => {
    const file = dom.fileInput.files && dom.fileInput.files[0];
    if (file) uploadFile(file);
    dom.fileInput.value = ""; // allow re-selecting the same file later
  });

  async function uploadFile(file) {
    resetEditorInputs();
    dom.uploadError.textContent = "";
    const formData = new FormData();
    formData.append("video", file);

    setUploadBusy(true);
    try {
      const res = await fetch("/api/upload", { method: "POST", body: formData });
      const data = await res.json();
      if (!res.ok) {
        dom.uploadError.textContent = data.error || "Upload failed.";
        return;
      }
      loadVideoIntoEditor(data);
    } catch (err) {
      console.error("[editor] upload failed:", err);
      dom.uploadError.textContent = "Upload failed — check your connection and try again.";
    } finally {
      setUploadBusy(false);
    }
  }

  function setUploadBusy(busy) {
    dom.dropzone.setAttribute("aria-disabled", busy ? "true" : "false");
    dom.dropzone.style.opacity = busy ? "0.6" : "1";
    dom.dropzone.querySelector(".upload-title").textContent = busy
      ? "Uploading…"
      : "Drag & drop a video here";
  }

  function loadVideoIntoEditor(data) {
    state.videoId = data.video_id;
    state.filename = data.filename;
    state.duration = data.duration;
    state.width = data.width;
    state.height = data.height;
    state.hasAudio = data.has_audio;

    state.start = 0;
    state.end = data.duration;

    resetExportUI();

    dom.uploadZone.hidden = true;
    dom.editor.hidden = false;

    dom.currentFilename.textContent = state.filename;
    dom.currentMeta.textContent = `${formatTime(state.duration)} · ${state.width}×${state.height}${
      state.hasAudio ? "" : " · no audio"
    }`;

    dom.previewVideo.src = `/media/${state.videoId}`;

    dom.startSlider.min = "0";
    dom.startSlider.max = String(state.duration);
    dom.endSlider.min = "0";
    dom.endSlider.max = String(state.duration);

    syncTrimInputs();
    renderTimeline();
    renderPreviewLayout();
    updatePlaybackTimeLabel();
  }

  function removeVideo() {
    state.videoId = null;
    dom.previewVideo.pause();
    dom.previewVideo.removeAttribute("src");
    dom.previewVideo.load();
    dom.editor.hidden = true;
    dom.uploadZone.hidden = false;
    resetExportUI();
  }

  // ---------------------------------------------------------------------
  // Trim controls (number inputs, sliders, draggable timeline handles) —
  // all funnel through setStart()/setEnd() so every control stays in sync.
  // ---------------------------------------------------------------------
  function setStart(value, { fromDrag = false } = {}) {
    // Leave room for at least a MIN_CLIP-long clip after this start point.
    const maxStart = Math.max(0, state.duration - MIN_CLIP);
    value = clamp(value, 0, maxStart);
    state.start = Math.round(value * 100) / 100;

    // Whenever Start changes, End follows automatically to a fixed
    // AUTO_CLIP_LENGTH-second clip (clamped to the video's actual duration
    // if there isn't enough room left) — the person only has to set Start
    // and gets a ready-made clip instead of typing both ends every time.
    const autoEnd = clamp(state.start + AUTO_CLIP_LENGTH, state.start + MIN_CLIP, state.duration);
    state.end = Math.round(autoEnd * 100) / 100;

    validateTrim();
    syncTrimInputs();
    renderTimeline();
    if (!fromDrag) seekPreviewToStart();
  }

  function setEnd(value, { fromDrag = false } = {}) {
    value = clamp(value, 0, state.duration);
    value = Math.max(value, state.start + MIN_CLIP);
    value = Math.min(state.duration, value);
    state.end = Math.round(value * 100) / 100;
    validateTrim();
    syncTrimInputs();
    renderTimeline();
  }

  function validateTrim() {
    if (state.end - state.start < MIN_CLIP) {
      dom.trimError.textContent = `Clip must be at least ${MIN_CLIP}s long.`;
    } else {
      dom.trimError.textContent = "";
    }
  }

  function syncTrimInputs() {
    // Don't stomp on the m:ss text field while the person is actively typing
    // in it (re-formatting mid-keystroke, e.g. turning "1:1" into "0:01",
    // would fight the cursor) — only reformat it once it's not focused.
    if (document.activeElement !== dom.startInput) dom.startInput.value = formatTime(state.start);
    if (document.activeElement !== dom.endInput) dom.endInput.value = formatTime(state.end);
    dom.startSlider.value = String(state.start);
    dom.endSlider.value = String(state.end);
  }

  dom.startInput.addEventListener("input", () => {
    const v = parseTimeInput(dom.startInput.value);
    if (!isNaN(v)) setStart(v);
  });
  dom.endInput.addEventListener("input", () => {
    const v = parseTimeInput(dom.endInput.value);
    if (!isNaN(v)) setEnd(v);
  });
  // On blur, always snap the field back to the canonical m:ss display —
  // covers invalid/partial text and values that got clamped.
  dom.startInput.addEventListener("blur", () => { dom.startInput.value = formatTime(state.start); });
  dom.endInput.addEventListener("blur", () => { dom.endInput.value = formatTime(state.end); });

  dom.startSlider.addEventListener("input", () => setStart(parseFloat(dom.startSlider.value)));
  dom.endSlider.addEventListener("input", () => setEnd(parseFloat(dom.endSlider.value)));

  function renderTimeline() {
    if (!state.duration) return;
    const startPct = (state.start / state.duration) * 100;
    const endPct = (state.end / state.duration) * 100;

    dom.timelineRange.style.left = `${startPct}%`;
    dom.timelineRange.style.width = `${Math.max(0, endPct - startPct)}%`;
    dom.handleStart.style.left = `${startPct}%`;
    dom.handleEnd.style.left = `${endPct}%`;

    dom.labelStart.textContent = formatTime(state.start);
    dom.labelEnd.textContent = formatTime(state.end);
    dom.labelDuration.textContent = formatTime(state.end - state.start);
  }

  // Draggable timeline handles (pointer events cover mouse + touch + pen).
  function makeHandleDraggable(handleEl, which) {
    handleEl.addEventListener("pointerdown", (e) => {
      handleEl.setPointerCapture(e.pointerId);
      handleEl.classList.add("is-dragging");

      const onMove = (ev) => {
        const rect = dom.timelineTrack.getBoundingClientRect();
        const ratio = clamp((ev.clientX - rect.left) / rect.width, 0, 1);
        const value = ratio * state.duration;
        if (which === "start") setStart(value, { fromDrag: true });
        else setEnd(value, { fromDrag: true });
      };
      const onUp = () => {
        handleEl.classList.remove("is-dragging");
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        seekPreviewToStart();
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    });

    // Keyboard support for accessibility.
    handleEl.addEventListener("keydown", (e) => {
      const step = e.shiftKey ? 1 : 0.1;
      let value = which === "start" ? state.start : state.end;
      if (e.key === "ArrowLeft") value -= step;
      else if (e.key === "ArrowRight") value += step;
      else return;
      e.preventDefault();
      if (which === "start") setStart(value);
      else setEnd(value);
    });
  }
  makeHandleDraggable(dom.handleStart, "start");
  makeHandleDraggable(dom.handleEnd, "end");

  function seekPreviewToStart() {
    if (dom.previewVideo.readyState > 0) {
      dom.previewVideo.currentTime = state.start;
    }
  }

  // ---------------------------------------------------------------------
  // Text overlay controls
  // ---------------------------------------------------------------------
  dom.textInput.addEventListener("input", () => {
    state.text = dom.textInput.value.slice(0, MAX_TEXT_LENGTH);
    renderPreviewLayout();
  });
  dom.fontSizeSlider.addEventListener("input", () => {
    state.fontSize = parseInt(dom.fontSizeSlider.value, 10);
    dom.fontSizeReadout.textContent = `${state.fontSize}px`;
    renderPreviewLayout();
  });
  dom.boldToggle.addEventListener("click", () => {
    state.bold = !state.bold;
    dom.boldToggle.classList.toggle("is-active", state.bold);
    renderPreviewLayout();
  });
  dom.alignGroup.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-align]");
    if (!btn) return;
    state.align = btn.dataset.align;
    dom.alignGroup.querySelectorAll("[data-align]").forEach((b) => b.classList.toggle("is-active", b === btn));
    renderPreviewLayout();
  });
  dom.styleSelect.addEventListener("change", () => {
    state.style = dom.styleSelect.value;
    // A solid band can only sensibly sit at the top or bottom — mirrors the
    // same rule the server enforces in /api/export.
    if (state.style === "band" && state.position === "center") {
      state.position = "top";
      dom.positionSelect.value = "top";
    }
    renderPreviewLayout();
  });
  dom.positionSelect.addEventListener("change", () => {
    state.position = dom.positionSelect.value;
    renderPreviewLayout();
  });

  // ---------------------------------------------------------------------
  // Live preview layout — a CSS approximation of the server's compositing:
  // scale the preview canvas's pixel budget (CANVAS x CANVAS) down to
  // whatever the on-screen box actually measures, then position the video
  // and the text layer using the exact same relative font size / padding /
  // alignment the export will use.
  // ---------------------------------------------------------------------
  function renderPreviewLayout() {
    const boxWidth = dom.previewCanvas.clientWidth || 460;
    const scale = boxWidth / CANVAS;

    dom.textBand.hidden = true;
    dom.textOverlay.hidden = true;

    // Tracks where the video's own rendered area ends, and — if a floating
    // caption box is anchored to the bottom — where that box begins, so the
    // watermark (positioned below) can avoid both, mirroring the server's
    // compositing logic in _run_export_job exactly.
    let videoAreaBottomPx = boxWidth;
    let captionTopPx = null;

    if (!state.text.trim()) {
      dom.previewVideo.style.top = "0px";
      dom.previewVideo.style.height = "100%";
    } else if (state.style === "band") {
      const band = dom.textBand;
      band.hidden = false;
      applyTextStyle(band, scale, { paddingX: 48, paddingY: 28 });
      band.style.top = state.position === "bottom" ? "" : "0px";
      band.style.bottom = state.position === "bottom" ? "0px" : "";

      // Let the browser wrap naturally, then measure how tall the band
      // actually rendered so the video can fill exactly the rest.
      const bandHeight = band.getBoundingClientRect().height;
      const videoHeight = boxWidth - bandHeight;
      if (state.position === "bottom") {
        dom.previewVideo.style.top = "0px";
        videoAreaBottomPx = videoHeight;
      } else {
        dom.previewVideo.style.top = `${bandHeight}px`;
        videoAreaBottomPx = boxWidth;
      }
      dom.previewVideo.style.height = `${Math.max(0, videoHeight)}px`;
    } else {
      dom.previewVideo.style.top = "0px";
      dom.previewVideo.style.height = "100%";

      const box = dom.textOverlay;
      box.hidden = false;
      applyTextStyle(box, scale, { paddingX: 32, paddingY: 20 });
      box.style.borderRadius = `${18 * scale}px`;

      const margin = 40 * scale;
      const boxHeight = box.getBoundingClientRect().height;
      if (state.position === "top") {
        box.style.top = `${margin}px`;
        box.style.bottom = "";
      } else if (state.position === "bottom") {
        box.style.top = "";
        box.style.bottom = `${margin}px`;
        captionTopPx = boxWidth - margin - boxHeight;
      } else {
        box.style.top = `calc(50% - ${boxHeight / 2}px)`;
        box.style.bottom = "";
      }
    }

    renderWatermark(scale, captionTopPx !== null ? captionTopPx : videoAreaBottomPx);
  }

  function renderWatermark(scale, bottomLimitPx) {
    const wm = dom.watermark;
    wm.style.fontSize = `${WATERMARK_FONT_SIZE * scale}px`;
    wm.style.textShadow = `0 ${scale}px ${3 * scale}px rgba(0, 0, 0, 0.65)`;
    wm.style.opacity = String(WATERMARK_OPACITY);
    const wmHeight = wm.getBoundingClientRect().height;
    const marginPx = WATERMARK_MARGIN * scale;
    wm.style.top = `${Math.max(0, bottomLimitPx - marginPx - wmHeight)}px`;
  }

  function applyTextStyle(el, scale, { paddingX, paddingY }) {
    el.textContent = state.text;
    el.style.fontSize = `${state.fontSize * scale}px`;
    el.style.fontWeight = state.bold ? "700" : "400";
    el.style.textAlign = state.align;
    el.style.padding = `${paddingY * scale}px ${paddingX * scale}px`;
  }

  // Keep the preview in sync when the panel is resized (window resize,
  // orientation change, or the two-column layout collapsing on mobile).
  new ResizeObserver(() => renderPreviewLayout()).observe(dom.previewCanvas);

  // ---------------------------------------------------------------------
  // Playback controls — plays only the selected [start, end] clip.
  // Play/Pause is a single toggle button: its icon always reflects the
  // video element's actual state (via the 'play'/'pause' events below),
  // rather than being flipped by the click handler itself — that way it
  // stays correct even if playback stops for a reason other than this
  // button (e.g. reaching the clip's end).
  // ---------------------------------------------------------------------
  dom.playPauseBtn.addEventListener("click", () => {
    if (dom.previewVideo.paused || dom.previewVideo.ended) {
      if (dom.previewVideo.currentTime < state.start || dom.previewVideo.currentTime >= state.end) {
        dom.previewVideo.currentTime = state.start;
      }
      dom.previewVideo.play();
    } else {
      dom.previewVideo.pause();
    }
  });
  dom.restartBtn.addEventListener("click", () => {
    dom.previewVideo.currentTime = state.start;
    dom.previewVideo.play();
  });

  dom.previewVideo.addEventListener("play", () => {
    dom.playPauseBtn.innerHTML = "&#10074;&#10074;"; // ⏸
    dom.playPauseBtn.title = "Pause";
    dom.playPauseBtn.setAttribute("aria-label", "Pause");
  });
  dom.previewVideo.addEventListener("pause", () => {
    dom.playPauseBtn.innerHTML = "&#9654;"; // ▶
    dom.playPauseBtn.title = "Play";
    dom.playPauseBtn.setAttribute("aria-label", "Play");
  });
  dom.previewVideo.addEventListener("timeupdate", () => {
    if (dom.previewVideo.currentTime >= state.end) {
      dom.previewVideo.pause();
      dom.previewVideo.currentTime = state.start;
    }
    updatePlaybackTimeLabel();
    if (state.duration) {
      const pct = (dom.previewVideo.currentTime / state.duration) * 100;
      dom.timelinePlayhead.style.left = `${clamp(pct, 0, 100)}%`;
    }
  });
  dom.previewVideo.addEventListener("loadedmetadata", () => {
    dom.previewVideo.currentTime = state.start;
  });

  function updatePlaybackTimeLabel() {
    const clipPos = Math.max(0, dom.previewVideo.currentTime - state.start);
    const clipDur = Math.max(0, state.end - state.start);
    dom.playbackTime.textContent = `${formatTime(clipPos)} / ${formatTime(clipDur)}`;
  }

  // ---------------------------------------------------------------------
  // Export
  // ---------------------------------------------------------------------
  dom.exportBtn.addEventListener("click", startExport);
  dom.exportAgainBtn.addEventListener("click", resetExportUI);

  function resetExportUI() {
    state.exportEpoch += 1; // invalidate any in-flight polling loop
    state.exportJobId = null;
    dom.exportError.textContent = "";
    dom.exportProgress.hidden = true;
    dom.exportProgressFill.style.width = "0%";
    dom.exportResult.hidden = true;
    dom.exportBtn.hidden = false;
    dom.exportBtn.disabled = false;
    dom.exportBtn.textContent = "Export square video";
  }

  async function startExport() {
    if (!state.videoId) return;
    if (state.end - state.start < MIN_CLIP) {
      dom.exportError.textContent = `Clip must be at least ${MIN_CLIP}s long.`;
      return;
    }

    // Every export gets its own epoch. If the video is replaced/removed, or
    // this fires again, mid-export, any older polling loop's captured
    // epoch stops matching state.exportEpoch and it silently stops acting —
    // so a late-arriving response from an abandoned export can never be
    // mistaken for *this* export's completion (or point the download link
    // at the wrong job).
    state.exportEpoch += 1;
    const myEpoch = state.exportEpoch;

    dom.exportError.textContent = "";
    dom.exportResult.hidden = true;
    dom.exportBtn.disabled = true;
    dom.exportBtn.textContent = "Exporting…";
    dom.exportProgress.hidden = false;
    dom.exportProgressFill.style.width = "0%";
    dom.exportProgressLabel.textContent = "Preparing video…";

    const payload = {
      video_id: state.videoId,
      start: state.start,
      end: state.end,
      text: state.text,
      font_size: state.fontSize,
      bold: state.bold,
      align: state.align,
      style: state.style,
      position: state.position,
    };

    try {
      const res = await fetch("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (myEpoch !== state.exportEpoch) return; // superseded while this request was in flight
      if (!res.ok) {
        exportFailed(myEpoch, data.error || "Export failed to start.");
        return;
      }
      state.exportJobId = data.job_id;
      pollExportProgress(myEpoch, data.job_id);
    } catch (err) {
      if (myEpoch !== state.exportEpoch) return;
      console.error("[editor] export request failed:", err);
      exportFailed(myEpoch, "Could not reach the server — check your connection and try again.");
    }
  }

  // epoch/jobId are captured locally (not re-read from `state` on every
  // loop iteration) specifically so this loop can never be redirected onto
  // a different export than the one it started tracking.
  async function pollExportProgress(epoch, jobId) {
    while (epoch === state.exportEpoch) {
      try {
        const res = await fetch(`/api/export/progress/${jobId}`, { cache: "no-store" });
        if (epoch !== state.exportEpoch) return; // superseded while this request was in flight
        const data = await res.json();
        if (epoch !== state.exportEpoch) return;
        if (!res.ok) {
          exportFailed(epoch, data.error || "Lost track of the export job.");
          return;
        }

        const pct = clamp(data.percent || 0, 0, 100);
        dom.exportProgressFill.style.width = `${pct}%`;
        dom.exportProgressLabel.textContent = data.stage || "Processing…";

        if (data.error) {
          exportFailed(epoch, data.error);
          return;
        }
        if (data.done) {
          exportSucceeded(epoch, jobId);
          return;
        }
      } catch (err) {
        // transient poll hiccup — keep trying on the next tick
        console.warn("[editor] progress poll failed, retrying:", err);
      }
      await new Promise((r) => setTimeout(r, 600));
    }
  }

  function exportSucceeded(epoch, jobId) {
    if (epoch !== state.exportEpoch) return; // this export was superseded — ignore
    dom.exportProgressLabel.textContent = "Finalizing…";
    dom.exportProgressFill.style.width = "100%";
    dom.exportBtn.hidden = true;
    dom.exportResult.hidden = false;
    dom.downloadLink.href = `/api/export/download/${jobId}`;
  }

  function exportFailed(epoch, message) {
    if (epoch !== state.exportEpoch) return; // this export was superseded — ignore
    dom.exportProgress.hidden = true;
    dom.exportError.textContent = message;
    dom.exportBtn.disabled = false;
    dom.exportBtn.textContent = "Export square video";
  }
})();
