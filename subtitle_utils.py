"""Speech-to-text subtitle generation using faster-whisper's tiny CPU model.
The model is downloaded once (on first use) and cached across requests."""
import os
import subprocess
import threading

from video_utils import FFMPEG_PATH

_MODEL = None
_MODEL_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()  # the CTranslate2 model isn't safe for concurrent transcribe() calls

# Chunking words into captions (instead of using whisper's own, looser
# segment boundaries) so a caption's end time tracks the actual last word
# spoken. Without this, a caption can linger on screen through a silent gap
# whenever whisper's segment boundary runs past the real speech.
MAX_CHUNK_WORDS = 12
MAX_CHUNK_SECONDS = 6.0
WORD_GAP_SPLIT = 0.45  # a pause longer than this starts a new caption
TRAIL_BUFFER = 0.15  # small grace period so a caption doesn't vanish right on the last phoneme


class SubtitleError(Exception):
    pass


def _get_model():
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise SubtitleError(
                        "Subtitles need the 'faster-whisper' package — pip install faster-whisper."
                    ) from exc
                _MODEL = WhisperModel("tiny", device="cpu", compute_type="int8")
    return _MODEL


def _chunk_words(words):
    """Groups word-level timestamps into caption-sized chunks, splitting on
    a natural pause (or once a chunk runs too long). Each chunk's end is the
    actual last word's end time (+ a small grace period, clipped so it never
    overlaps the next chunk) rather than whisper's own segment boundary."""
    chunks = []
    current = []
    for word in words:
        if current:
            gap = word.start - current[-1].end
            duration = word.end - current[0].start
            if gap > WORD_GAP_SPLIT or duration > MAX_CHUNK_SECONDS or len(current) >= MAX_CHUNK_WORDS:
                chunks.append(current)
                current = []
        current.append(word)
    if current:
        chunks.append(current)

    out = []
    for i, chunk in enumerate(chunks):
        text = "".join(w.word for w in chunk).strip()
        if not text:
            continue
        start = chunk[0].start
        end = chunk[-1].end + TRAIL_BUFFER
        if i + 1 < len(chunks):
            end = min(end, chunks[i + 1][0].start)
        out.append((start, max(end, start + 0.2), text))
    return out


def transcribe_clip(input_path, start, end, tmp_dir):
    """Extracts the [start, end) audio range and transcribes it. Returns a
    list of (seg_start, seg_end, text) tuples with timestamps relative to the
    clip itself (0 = start of the trimmed export)."""
    audio_path = os.path.join(tmp_dir, "subtitle_audio.wav")
    cmd = [
        FFMPEG_PATH, "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", input_path, "-vn", "-ac", "1", "-ar", "16000",
        "-f", "wav", audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(audio_path):
        raise SubtitleError("Could not extract audio for subtitles.")

    model = _get_model()
    with _INFERENCE_LOCK:
        segments, _ = model.transcribe(audio_path, beam_size=1, vad_filter=True, word_timestamps=True)
        words = [w for seg in segments for w in (seg.words or [])]

    return _chunk_words(words)
