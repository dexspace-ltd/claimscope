import fractions

import av
import numpy as np

from claimscope import video


def _transcribe_json(words):
    items = []
    for start, text in words:
        if text in ".,?":
            items.append({"type": "punctuation", "alternatives": [{"content": text}]})
        else:
            items.append({"type": "pronunciation", "start_time": str(start), "end_time": str(start + 0.3),
                          "alternatives": [{"content": text}]})
    return {"results": {"items": items}}


def test_transcript_segments_split_on_full_stops_and_pauses():
    data = _transcribe_json([(0.0, "This"), (0.4, "is"), (0.8, "the"), (1.2, "bathroom"), (0, "."),
                             (2.0, "Burst"), (2.4, "pipe"), (6.0, "Now"), (6.4, "the"), (6.8, "kitchen")])
    segs = video.transcript_segments(data)
    assert [s["text"] for s in segs] == ["This is the bathroom.", "Burst pipe", "Now the kitchen"]
    assert segs[2]["start"] == 6.0
    assert "Burst pipe" in video.commentary_near(segs, 3.0)
    assert video.timeline_text(segs).startswith("[0s-2s] This is the bathroom.")


def _make_video(path, scenes=4, seconds_each=3, fps=10, size=(160, 120)):
    """Silent test video: a few flat-colour 'rooms' with a textured edge so frames are not blank."""
    out = av.open(path, "w")
    stream = out.add_stream("mpeg4", rate=fps)
    stream.width, stream.height, stream.pix_fmt = size[0], size[1], "yuv420p"
    stream.time_base = fractions.Fraction(1, fps)
    rng = np.random.default_rng(0)
    for scene in range(scenes):
        base = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        base[:] = 20 + scene * 70  # clearly different brightness per scene
        base[:, : size[0] // 4] = rng.integers(0, 255, (size[1], size[0] // 4, 3), dtype=np.uint8)
        for _ in range(seconds_each * fps):
            frame = av.VideoFrame.from_ndarray(base, format="rgb24")
            for packet in stream.encode(frame):
                out.mux(packet)
    for packet in stream.encode():
        out.mux(packet)
    out.close()


def test_select_frames_keeps_one_per_scene_and_no_audio(tmp_path):
    path = str(tmp_path / "walk.mp4")
    _make_video(path)
    frames = video.select_frames(path)
    assert 4 <= len(frames) <= 6  # one per scene, plus at most an overdue keep
    assert frames[0].t == 0
    assert abs(video.duration_s(path) - 12) < 1
    assert video.extract_audio_wav(path, str(tmp_path / "a.wav")) == 0.0
