"""Walkthrough videos: pick the frames worth analysing, and turn a timestamped transcript into text.

Frames: decode about two frames a second and keep the sharpest nearby frame each time the view has
changed enough from the last one kept. Phone walkthroughs pan constantly, so this keeps one sharp
frame per new view instead of hundreds of near-duplicates.
"""

import io
import json
from dataclasses import dataclass

import av
import numpy as np
from PIL import Image

SAMPLE_EVERY_S = 0.5
MAX_EDGE = 1568           # long edge sent to the model
THUMB = (48, 48)          # fingerprint size for change detection
NEW_VIEW_DIFF = 34.0      # mean grey-level change (0-255) from the last kept frame
MIN_GAP_S = 2.5           # never keep two frames closer than this
FORCE_EVERY_S = 10.0      # keep something at least this often, even when panning slowly
MAX_FRAMES = 60


@dataclass
class Frame:
    t: float
    jpeg: bytes
    sharpness: float


def _sharpness(grey: np.ndarray) -> float:
    """Variance of a Laplacian: low for motion blur and out-of-focus frames."""
    g = grey.astype(np.float32)
    lap = -4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return float(lap.var())


def _rotation(stream) -> int:
    for key in ("rotate",):
        if key in (stream.metadata or {}):
            return int(stream.metadata[key]) % 360
    return 0


def select_frames(path: str, max_frames: int = MAX_FRAMES) -> list[Frame]:
    container = av.open(path)
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    stream_rotation = _rotation(stream)

    candidates = []  # (t, image, thumb, sharpness)
    next_t = 0.0
    for frame in container.decode(stream):
        if frame.time is None or frame.time < next_t:
            continue
        next_t = frame.time + SAMPLE_EVERY_S
        img = frame.to_image()
        rotation = getattr(frame, "rotation", 0) or stream_rotation
        if rotation:
            img = img.rotate(rotation, expand=True)  # PIL rotates anticlockwise, like the display matrix
        small = img.convert("L").resize((256, int(256 * img.height / img.width)))
        grey = np.asarray(small)
        thumb = np.asarray(small.resize(THUMB), dtype=np.float32)
        candidates.append((frame.time, img, thumb, _sharpness(grey)))
    container.close()
    if not candidates:
        return []

    # Every new view is kept; blur is handled by taking the sharpest sample from the next second rather than a
    # global threshold, which would drop whole rooms of plain walls (little fine detail, but in focus).
    kept, last_thumb, last_t = [], None, -1e9
    for i, (t, img, thumb, sharp) in enumerate(candidates):
        changed = last_thumb is None or float(np.abs(thumb - last_thumb).mean()) >= NEW_VIEW_DIFF
        overdue = t - last_t >= FORCE_EVERY_S
        if t - last_t < MIN_GAP_S or not (changed or overdue):
            continue
        window = [c for c in candidates[i:i + 3] if c[0] - t <= 1.0]
        best = max(window, key=lambda c: c[3])
        kept.append(best)
        last_thumb, last_t = best[2], best[0]

    if len(kept) > max_frames:  # evenly thin a very long or busy video
        step = len(kept) / max_frames
        kept = [kept[int(i * step)] for i in range(max_frames)]

    out = []
    for t, img, _, sharp in kept:
        img = img.convert("RGB")
        img.thumbnail((MAX_EDGE, MAX_EDGE))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        out.append(Frame(round(t, 2), buf.getvalue(), round(sharp, 1)))
    return out


# --- transcripts -------------------------------------------------------------------


def transcript_segments(transcribe_json: str | dict, gap_s: float = 1.2) -> list[dict]:
    """Amazon Transcribe output -> sentence-ish segments [{start, end, text}] split on pauses and full stops."""
    data = json.loads(transcribe_json) if isinstance(transcribe_json, str) else transcribe_json
    segments, words, start, end = [], [], None, None
    for item in data["results"]["items"]:
        content = item["alternatives"][0]["content"]
        if item["type"] == "punctuation":
            if words:
                words[-1] += content
            if content in ".?!" and words:
                segments.append({"start": start, "end": end, "text": " ".join(words)})
                words, start = [], None
            continue
        t0, t1 = float(item["start_time"]), float(item["end_time"])
        if words and t0 - end > gap_s:
            segments.append({"start": start, "end": end, "text": " ".join(words)})
            words, start = [], None
        if start is None:
            start = t0
        words.append(content)
        end = t1
    if words:
        segments.append({"start": start, "end": end, "text": " ".join(words)})
    return segments


def commentary_near(segments: list[dict], t: float, before: float = 8.0, after: float = 4.0) -> str:
    """What was said around time t: the assessor usually talks about what the camera is pointing at."""
    return " ".join(s["text"] for s in segments if s["end"] >= t - before and s["start"] <= t + after)


def timeline_text(segments: list[dict]) -> str:
    return "\n".join(f"[{s['start']:.0f}s-{s['end']:.0f}s] {s['text']}" for s in segments)


def extract_audio_wav(path: str, out_path: str, rate: int = 16000) -> float:
    """Mono 16 kHz WAV of the soundtrack (any container: .mp4, .mov, .webm). Returns its duration in seconds."""
    import wave

    container = av.open(path)
    if not container.streams.audio:
        container.close()
        return 0.0
    stream = container.streams.audio[0]
    resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
    samples = 0
    with wave.open(out_path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                data = out.to_ndarray().tobytes()
                wav.writeframes(data)
                samples += out.samples
        for out in resampler.resample(None):
            wav.writeframes(out.to_ndarray().tobytes())
            samples += out.samples
    container.close()
    return samples / rate


def duration_s(path: str) -> float:
    container = av.open(path)
    d = float(container.duration / av.time_base) if container.duration else 0.0
    container.close()
    return d
