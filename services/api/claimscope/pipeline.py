"""Pipeline Lambda: invoked asynchronously by the API, runs one assessment end to end.

Stages are plain functions so they can move into Step Functions states unchanged
if runs approach the 15-minute Lambda limit.
"""

import hashlib
import json
import math
import re
import logging
import os
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from statistics import median
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from functools import cache
from pathlib import Path

import boto3

from . import ai, store
from .geometry import derive
from .pricing import price_line, totals
from .qa import run_rules

log = logging.getLogger()
log.setLevel(logging.INFO)

DATA = Path(__file__).parent / "data"
ROOM_WORKERS = 4
# Stop starting new work with this much time left, so the run can record a failure cleanly.
SAFETY_MARGIN_MS = 90_000


class OutOfTime(Exception):
    pass


@cache
def catalogue() -> dict:
    return json.loads((DATA / "catalogue_v0.json").read_text())


@cache
def trade_rates() -> dict:
    return json.loads((DATA / "trade_rates_v0.json").read_text())


@cache
def s3():
    return boto3.client("s3")


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _put_json(key: str, body) -> None:
    s3().put_object(
        Bucket=os.environ["EVIDENCE_BUCKET"],
        Key=key,
        Body=json.dumps(body, default=_json_default).encode(),
        ContentType="application/json",
    )


class Run:
    def __init__(self, assessment_id: str, context=None):
        self.assessment_id = assessment_id
        self.context = context
        self._lock = threading.Lock()

    def check_time(self) -> None:
        if self.context is not None and self.context.get_remaining_time_in_millis() < SAFETY_MARGIN_MS:
            raise OutOfTime("pipeline ran out of time; split into Step Functions or capture fewer views")

    def progress(self, message: str) -> None:
        with self._lock:
            store.table().update_item(
                Key={"PK": store.asmt_pk(self.assessment_id), "SK": "META"},
                UpdateExpression="SET progress = :p",
                ExpressionAttributeValues={":p": message},
            )


CAPTURE_PREFIX = "captures/"
PENDING_WAIT_S = 150   # how long a draft waits for views still being analysed
STALE_PENDING_S = 300  # a view still pending after this never uploaded; ignore it


def _parse_capture_key(key: str):
    # captures/<assessment_id>/<kind>/<evidence_id>
    parts = key.split("/")
    if len(parts) != 4 or parts[0] + "/" != CAPTURE_PREFIX:
        return None
    return parts[1], parts[2], parts[3]


def analyse_view(assessment_id: str, evidence_id: str, key: str) -> None:
    """Stage A for one uploaded image, run as soon as it lands. Findings are kept as text and the
    image is deleted (the demo does not keep captures)."""
    pk = store.asmt_pk(assessment_id)
    item = store.table().get_item(Key={"PK": pk, "SK": f"EVD#{evidence_id}"}).get("Item")
    meta = store.get_meta(assessment_id)
    if item is None or meta is None:
        return
    rooms = [i for i in store.query_assessment(assessment_id) if i["SK"].startswith("ROOM#")]
    room = next((r for r in rooms if r["room_id"] == item.get("room_id")), None)
    bucket = os.environ["EVIDENCE_BUCKET"]
    try:
        body = s3().get_object(Bucket=bucket, Key=key)["Body"].read()
        findings = ai.detect_damage(body, item["content_type"], room_name=room["name"] if room else None,
                                    peril=meta["peril"], room_options=sorted(r["name"] for r in rooms),
                                    commentary=(item.get("capture") or {}).get("commentary", ""))
        update = "SET analysis = :a, findings_json = :f REMOVE analysis_error"
        values = {":a": "done", ":f": findings.model_dump_json()}
    except Exception as exc:
        log.exception("stage A failed for %s", evidence_id)
        update, values = "SET analysis = :a, analysis_error = :e", {":a": "failed", ":e": _explain(exc)[:300]}
    store.table().update_item(Key={"PK": pk, "SK": f"EVD#{evidence_id}"}, UpdateExpression=update,
                              ExpressionAttributeValues=values)
    if os.environ.get("KEEP_CAPTURES") != "1":
        s3().delete_object(Bucket=bucket, Key=key)


VIDEO_PREFIX = "videos/"
TRANSCRIBE_WAIT_S = 600


def _video_status(assessment_id: str, status: str, message: str) -> None:
    store.table().update_item(
        Key={"PK": store.asmt_pk(assessment_id), "SK": "META"},
        UpdateExpression="SET video_status = :s, video_message = :m",
        ExpressionAttributeValues={":s": status, ":m": message},
    )


def _transcribe(assessment_id: str, evidence_id: str, wav_path: str, context) -> list[dict]:
    """Amazon Transcribe (UK English, in-region) on the soundtrack; returns timestamped segments."""
    from . import video

    bucket = os.environ["EVIDENCE_BUCKET"]
    audio_key = f"audio/{assessment_id}/{evidence_id}.wav"
    s3().upload_file(wav_path, bucket, audio_key)
    transcribe = boto3.client("transcribe")
    job = f"claimscope-{assessment_id}-{evidence_id}-{int(time.time())}"
    out_key = f"transcripts/{assessment_id}/{evidence_id}.json"
    transcribe.start_transcription_job(
        TranscriptionJobName=job, LanguageCode="en-GB", MediaFormat="wav",
        Media={"MediaFileUri": f"s3://{bucket}/{audio_key}"},
        OutputBucketName=bucket, OutputKey=out_key,
    )
    deadline = time.monotonic() + TRANSCRIBE_WAIT_S
    while True:
        status = transcribe.get_transcription_job(TranscriptionJobName=job)["TranscriptionJob"]
        state = status["TranscriptionJobStatus"]
        if state == "COMPLETED":
            break
        if state == "FAILED":
            raise RuntimeError(f"transcription failed: {status.get('FailureReason', 'unknown')}")
        if time.monotonic() > deadline or (context and context.get_remaining_time_in_millis() < SAFETY_MARGIN_MS * 2):
            raise OutOfTime("transcription took too long")
        time.sleep(5)
    body = s3().get_object(Bucket=bucket, Key=out_key)["Body"].read()
    try:
        transcribe.delete_transcription_job(TranscriptionJobName=job)  # the transcript is kept in our bucket
    except Exception:
        log.warning("could not delete transcription job %s", job)
    return video.transcript_segments(body.decode())


def process_video(assessment_id: str, evidence_id: str, key: str, context=None) -> None:
    """Walkthrough video -> transcript, rooms from the commentary, an incident brief, and frames that are
    handed to per-view analysis (each with its room and what was said at that moment)."""
    import tempfile
    from concurrent.futures import ThreadPoolExecutor as Pool
    from . import video

    pk = store.asmt_pk(assessment_id)
    meta = store.get_meta(assessment_id)
    item = store.table().get_item(Key={"PK": pk, "SK": f"EVD#{evidence_id}"}).get("Item")
    if meta is None or item is None:
        return
    bucket = os.environ["EVIDENCE_BUCKET"]
    # A new video replaces the previous video's frames and rooms (tour views and floor-plan rooms are kept).
    for old in store.query_assessment(assessment_id):
        sk = old["SK"]
        old_frame = sk.startswith("EVD#") and str((old.get("capture") or {}).get("sweep_id", "")).startswith("video:")
        old_room = sk.startswith("ROOM#") and old.get("dimensions_source") == "video"
        old_video = sk.startswith("EVD#") and old.get("kind") == "video" and old.get("evidence_id") != evidence_id
        if old_frame or old_room or old_video:
            store.table().delete_item(Key={"PK": pk, "SK": sk})
    with tempfile.TemporaryDirectory() as tmp:
        path, wav = f"{tmp}/video", f"{tmp}/audio.wav"
        _video_status(assessment_id, "processing", "Reading the video")
        s3().download_file(bucket, key, path)
        length = video.duration_s(path)
        has_audio = video.extract_audio_wav(path, wav) > 1

        with Pool(1) as pool:
            transcript_job = pool.submit(_transcribe, assessment_id, evidence_id, wav, context) if has_audio else None
            _video_status(assessment_id, "processing", "Transcribing the commentary and picking frames")
            frames = video.select_frames(path)
            segments = transcript_job.result() if transcript_job else []

    _video_status(assessment_id, "processing", "Splitting the walkthrough into rooms")
    plan = ai.plan_video(video.timeline_text(segments), length) if segments else None
    store.table().update_item(
        Key={"PK": pk, "SK": f"EVD#{evidence_id}"},
        UpdateExpression="SET transcript = :t, duration_s = :d, frame_count = :n",
        ExpressionAttributeValues={":t": json.dumps(segments), ":d": Decimal(str(round(length, 1))), ":n": len(frames)},
    )

    # Rooms from the commentary. Sizes are estimated from the frames at draft time; the assessor can edit them.
    room_ids = []
    for n, room in enumerate(plan.rooms if plan else []):
        room_id = hashlib.sha256(f"{assessment_id}:video:{evidence_id}:{n}".encode()).hexdigest()[:8]
        dims = {"length_m": DEFAULT_SIZE[0], "breadth_m": DEFAULT_SIZE[1], "height_m": Decimal("2.4"),
                "openings": [], "source": "video"}
        store.put({
            "PK": pk, "SK": f"ROOM#{room_id}", "room_id": room_id, "name": room.name[:80],
            "dimensions": dims, "derived": derive(dims), "dimensions_source": "video",
            "plan_notes": room.notes[:300], "video_span": [Decimal(str(room.start_s)), Decimal(str(room.end_s))],
            "updated_at": store.now(),
        })
        room_ids.append((room.start_s, room.end_s, room_id))

    # The commentary becomes the incident brief, unless the assessor has already written one.
    if plan and not any((meta.get("incident") or {}).values()):
        brief = {k: v for k, v in {"detail": plan.detail, "cause": plan.cause, "further_info": plan.further_info}.items() if v}
        if plan.contents_mentioned:
            brief["further_info"] = (brief.get("further_info", "") + " Contents mentioned (contents claim): "
                                     + ", ".join(plan.contents_mentioned) + ".").strip()
        if brief:
            store.table().update_item(Key={"PK": pk, "SK": "META"}, UpdateExpression="SET incident = :i, incident_source = :s",
                                      ExpressionAttributeValues={":i": brief, ":s": "video commentary"})

    _video_status(assessment_id, "processing", f"Analysing {len(frames)} frames")
    for frame in frames:
        span = next(((start, end, rid) for start, end, rid in room_ids if start <= frame.t < end),
                    room_ids[-1] if room_ids else (0, length + 1, None))
        room_id = span[2]
        # only what was said while in this room, so a frame never "hears" the previous room's commentary
        room_segments = [seg for seg in segments if seg["end"] >= span[0] and seg["start"] < span[1]]
        frame_id = uuid.uuid4().hex[:12]
        frame_key = f"{CAPTURE_PREFIX}{assessment_id}/scan_view/{frame_id}"
        # The record goes first so the per-view analysis triggered by the upload can find it.
        store.put({
            "PK": pk, "SK": f"EVD#{frame_id}", "evidence_id": frame_id, "kind": "scan_view", "room_id": room_id,
            "s3_key": frame_key, "content_type": "image/jpeg", "analysis": "pending", "created_at": store.now(),
            "capture": {"sweep_id": f"video:{evidence_id}", "t": Decimal(str(frame.t)),
                        "commentary": video.commentary_near(room_segments, frame.t)[:600]},
        })
        s3().put_object(Bucket=bucket, Key=frame_key, Body=frame.jpeg, ContentType="image/jpeg")
    _video_status(assessment_id, "ready", f"{len(frames)} frames from a {length / 60:.1f} min video"
                  + (f", {len(room_ids)} rooms from the commentary" if room_ids else ", no commentary heard"))


def video_handler(event, context):
    """S3 ObjectCreated under videos/: process each new walkthrough video."""
    for record in event.get("Records", []):
        key = record["s3"]["object"]["key"]
        parts = key.split("/")
        if len(parts) != 3 or parts[0] + "/" != VIDEO_PREFIX:
            continue
        assessment_id, evidence_id = parts[1], parts[2]
        try:
            process_video(assessment_id, evidence_id, key, context)
        except Exception as exc:
            log.exception("video processing failed for %s", assessment_id)
            _video_status(assessment_id, "failed", _explain(exc)[:300])


def view_handler(event, context):
    """S3 ObjectCreated under captures/: analyse each new view."""
    for record in event.get("Records", []):
        key = record["s3"]["object"]["key"]
        parsed = _parse_capture_key(key)
        if parsed and parsed[1] in ("photo", "scan_view"):
            analyse_view(parsed[0], parsed[2], key)


ROOM_TYPE_RULES = [
    ("en-suite", r"en.?suite"), ("open-plan kitchen/living", r"open.?plan|kitchen.*(living|lounge|diner)|(living|lounge).*kitchen"),
    ("kitchen", r"kitchen"), ("shower room", r"shower"), ("wc", r"\bwc\b|cloak|toilet"), ("bathroom", r"bath"),
    ("bedroom", r"bed"), ("living room", r"living|lounge|sitting|reception"), ("dining room", r"dining"),
    ("hallway", r"hall|corridor|entrance"), ("landing", r"landing"), ("stairs", r"stair"), ("utility", r"utility"),
    ("storage", r"stor|cupboard|box room"), ("study", r"study|office"), ("conservatory", r"conservatory"),
    ("garage", r"garage"), ("external", r"external|outside|garden"),
]
ROOM_LABELS = {"open-plan kitchen/living": "Kitchen / Living Room", "wc": "WC", "en-suite": "En-suite"}


# Room types that can be the same space: a kitchen view belongs to a "Kitchen / Diner" on the plan.
COMPATIBLE = {
    "open-plan kitchen/living": {"kitchen", "living room", "dining room"},
    "bathroom": {"shower room", "en-suite", "wc"},
    "hallway": {"landing", "stairs"},
}


def same_space(name_type: str, guess: str) -> bool:
    return (name_type == guess or guess in COMPATIBLE.get(name_type, ())
            or name_type in COMPATIBLE.get(guess, ()))


def canonical_room_type(guess: str) -> str:
    g = (guess or "").lower()
    return next((kind for kind, rx in ROOM_TYPE_RULES if re.search(rx, g)), "unclear")


def _findings(raw: str) -> ai.ImageFindings:
    data = json.loads(raw)
    data["room_type_guess"] = canonical_room_type(data.get("room_type_guess", ""))
    return ai.ImageFindings.model_validate(data)


def load_results(run: Run, assessment_id: str) -> dict[str, dict]:
    """Collect stored per-view findings, waiting briefly for views still being analysed."""
    deadline = time.monotonic() + PENDING_WAIT_S
    while True:
        evidence = [i for i in store.query_assessment(assessment_id)
                    if i["SK"].startswith("EVD#") and i["kind"] in ("photo", "scan_view")]
        now = datetime.now(timezone.utc)
        pending = [e for e in evidence if e.get("analysis", "pending") == "pending"
                   and (now - datetime.fromisoformat(e["created_at"])).total_seconds() < STALE_PENDING_S]
        if not pending or time.monotonic() > deadline:
            break
        run.progress(f"Finishing analysis of {len(pending)} view(s)")
        time.sleep(4)

    results = {}
    for e in evidence:
        base = {"room_id": e.get("room_id"), "scan_point": (e.get("capture") or {}).get("sweep_id") or e["evidence_id"]}
        if e.get("analysis") == "done":
            results[e["evidence_id"]] = {**base, "findings": _findings(e["findings_json"])}
        elif e.get("analysis") == "failed":
            results[e["evidence_id"]] = {**base, "error": e.get("analysis_error", "failed")}
    return results


DEFAULT_SIZE = (Decimal("3.50"), Decimal("3.00"))


def _useful(guess: str) -> bool:
    g = guess.strip().lower()
    return bool(g) and g not in ("room", "n/a") and not g.startswith(("unknown", "unclear", "uncertain", "unidentified"))


def assign_rooms(assessment_id: str, rooms: dict, image_results: dict) -> None:
    """Give every unassigned image a room. All views from one scan point go to the same room: the
    known room most views name, else a new room of the most-guessed type with an estimated size."""
    by_name = {r["name"].strip().lower(): room_id for room_id, r in rooms.items()}
    groups: dict[str, list[str]] = {}
    for evidence_id, r in image_results.items():
        if not r["room_id"] and "findings" in r:
            groups.setdefault(r["scan_point"], []).append(evidence_id)

    new_rooms: dict[str, dict] = {}  # room type -> {"room_id", "sizes"}
    for scan_point, ids in groups.items():
        findings = [image_results[i]["findings"] for i in ids]
        votes = Counter(by_name.get(f.room_match.strip().lower()) for f in findings)
        votes.pop(None, None)
        if not votes:  # no exact match: does a guessed type match a known room name?
            # an exact type match outweighs a compatible one ("en-suite" view -> "En-suite", not "Bathroom")
            votes = Counter()
            for f in findings:
                for name, room_id in by_name.items():
                    name_type = canonical_room_type(name)
                    if _useful(f.room_type_guess) and same_space(name_type, f.room_type_guess):
                        votes[room_id] += 2 if name_type == f.room_type_guess else 1
        if votes:
            room_id = votes.most_common(1)[0][0]
        else:
            kinds = Counter(f.room_type_guess for f in findings if _useful(f.room_type_guess))
            kind = kinds.most_common(1)[0][0] if kinds else "unidentified area"
            entry = new_rooms.setdefault(kind, {
                "room_id": hashlib.sha256(f"{assessment_id}:guess:{kind}".encode()).hexdigest()[:8], "sizes": []})
            entry["sizes"] += [f.room_size_estimate for f in findings if f.room_size_estimate]
            room_id = entry["room_id"]
        for i in ids:
            image_results[i]["room_id"] = room_id
            store.table().update_item(
                Key={"PK": store.asmt_pk(assessment_id), "SK": f"EVD#{i}"},
                UpdateExpression="SET room_id = :r", ExpressionAttributeValues={":r": room_id},
            )

    for kind, entry in new_rooms.items():
        sizes = entry["sizes"]
        if sizes:
            length = Decimal(str(round(median(max(s.length_m, s.breadth_m) for s in sizes), 2)))
            breadth = Decimal(str(round(median(min(s.length_m, s.breadth_m) for s in sizes), 2)))
        else:
            length, breadth = DEFAULT_SIZE
        if length <= 0 or breadth <= 0:
            length, breadth = DEFAULT_SIZE
        dims = {"length_m": length, "breadth_m": breadth, "height_m": Decimal("2.4"), "openings": [], "source": "estimated"}
        room = {
            "PK": store.asmt_pk(assessment_id),
            "SK": f"ROOM#{entry['room_id']}",
            "room_id": entry["room_id"],
            "name": ROOM_LABELS.get(kind, kind.title())[:80],
            "dimensions": dims,
            "derived": derive(dims),
            "dimensions_source": "estimated",
            "plan_notes": "Created from auto-walk views; size estimated from the images. Check before relying on it.",
            "updated_at": store.now(),
        }
        store.put(room)
        rooms[entry["room_id"]] = room


def room_context(room: dict, peril: str, image_results: dict, brief: dict | None = None, all_rooms: list[str] = ()) -> dict:
    observations, finishes, guesses = [], Counter(), Counter()
    for evidence_id, r in image_results.items():
        if r["room_id"] != room["room_id"] or "findings" not in r:
            continue
        f = r["findings"]
        guesses[f.room_type_guess] += 1
        finishes.update(f.finishes)
        observations += [{"evidence_id": evidence_id, **o.model_dump()} for o in f.observations]
    return {
        "room_name": room["name"],
        "room_name_is_placeholder": room.get("name_is_placeholder", False),
        "room_type_guesses": dict(guesses),
        "peril": peril,
        "dimensions": room["dimensions"],
        "derived_quantities": room["derived"],
        "finishes_seen": [f for f, _ in finishes.most_common(12)],
        "observations": observations,
        "other_rooms_in_property": [n for n in all_rooms if n != room["name"]],
        "incident_brief": brief or "none provided: schedule visible damage only",
    }


def itemise_all(run: Run, rooms: dict, peril: str, image_results: dict, brief: dict | None = None) -> dict[str, ai.RoomSchedule]:
    items = catalogue()["items"]
    out, done = {}, 0
    pool = ThreadPoolExecutor(ROOM_WORKERS)
    seen = {r["room_id"] for r in image_results.values() if "findings" in r}
    names = sorted(r["name"] for room_id, r in rooms.items() if room_id in seen)
    futures = {
        pool.submit(ai.itemise_room, room_context(room, peril, image_results, brief, names), items): room_id
        for room_id, room in rooms.items()
        if room_id in seen
    }
    run.progress(f"Drafting rooms: 0 of {len(futures)}")
    try:
        for future in as_completed(futures):
            out[futures[future]] = future.result()
            done += 1
            run.progress(f"Drafting rooms: {done} of {len(futures)}")
            run.check_time()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def price_and_check(proposed: dict[str, ai.RoomSchedule], rooms: dict, image_results: dict | None = None) -> dict:
    image_results = image_results or {}
    by_code = {i["code"]: i for i in catalogue()["items"]}
    rates = trade_rates()["per_hour"]
    groups, findings = [], []

    for room_id, room in rooms.items():
        schedule = proposed.get(room_id)
        if schedule is None and not any(v["room_id"] == room_id for v in image_results.values()):
            continue  # a floor-plan room nobody captured
        lines = []
        for p in schedule.lines if schedule else []:
            item = by_code.get(p.item_code)
            if item is None or item["operation"] == "prelim":
                findings.append({"rule_id": "R00", "severity": "warn", "resolved": False, "line_refs": [],
                                 "message": f"Model proposed unknown item {p.item_code} in {room['name']}; dropped"})
                continue
            flags = list(p.flags)
            try:
                priced = price_line(item, p.quantity_basis, room["derived"], rates, p.manual_quantity)
            except ValueError as exc:
                findings.append({"rule_id": "R02", "severity": "error", "resolved": False, "line_refs": [],
                                 "message": f"{p.item_code} in {room['name']}: {exc}; quantity set to 0 for review"})
                priced = price_line(item, "manual", room["derived"], rates, 0)
                flags.append("manual_quantity")
            if p.confidence < 0.5 and "low_confidence" not in flags:
                flags.append("low_confidence")
            lines.append({
                "line_no": len(lines) + 1,
                **priced,
                "rationale": p.rationale,
                "evidence_refs": p.evidence_refs,
                "confidence": p.confidence,
                "flags": sorted(set(flags)),
                "status": "ai_suggested",
                "origin": "model",
            })
        name = room["name"]
        if room.get("name_is_placeholder") and schedule and schedule.room_name:
            name = schedule.room_name
        groups.append({
            "room_id": room_id,
            "name": name,
            "dimensions": room["dimensions"],
            "derived": room["derived"],
            "lines": lines,
            "group_total": sum((l["total"] for l in lines), Decimal(0)),
            "site_checks": list(schedule.site_checks) if schedule else [],
            "special_notes": "\n".join(schedule.site_checks) if schedule else "",
            "view_count": sum(1 for v in image_results.values() if v["room_id"] == room_id),
            "finishes": sorted({f.lower() for v in image_results.values() if v["room_id"] == room_id and "findings" in v
                                for f in v["findings"].finishes}),
        })

    general = general_items(groups, by_code, rates)
    findings += run_rules(groups, by_code)
    all_lines = general["lines"] + [l for g in groups for l in g["lines"]]
    return {
        "catalogue_version": catalogue()["version"],
        "rates_version": trade_rates()["version"],
        "general_items": general,
        "rooms": groups,
        "qa_findings": findings,
        "totals": totals(all_lines),
    }


STRIP_OUT = ("take_up", "remove_dispose")
DUSTY = ("FLR-043", "PLS-001", "PLS-003", "PLS-005", "SKT-001", "BTH-004", "DEC-006")
OPERATIVES = 2  # crew size assumed when turning labour hours into days on site
SKIP_M2 = Decimal(30)  # floor area of strip-out one 6-yard skip takes


def general_items(groups: list[dict], by_code: dict, rates: dict) -> dict:
    """Job-wide preliminaries come from rules, not the model, scaled by the works scheduled."""
    worked = [g for g in groups if g["lines"]]
    idle_rooms = [g for g in groups if not g["lines"]]
    all_lines = [l for g in worked for l in g["lines"]]
    ops = {l["item_code"]: by_code[l["item_code"]]["operation"] for l in all_lines if l["item_code"] in by_code}
    strip = [l for l in all_lines if ops.get(l["item_code"]) in STRIP_OUT]
    stripped_floor = sum((Decimal(str(l["quantity"])) for l in strip if l["item_code"].startswith("FLR")), Decimal(0))
    hours = sum((Decimal(str(l["quantity"])) * Decimal(str(by_code[l["item_code"]]["labour_minutes"])) / 60
                 for l in all_lines if l["item_code"] in by_code), Decimal(0))
    days = max(1, math.ceil(hours / (8 * OPERATIVES))) if worked else 0
    dusty = bool(strip) or any(l["item_code"] in DUSTY for l in all_lines)
    has_electrical_test = any(l["item_code"] == "ELC-001" for l in all_lines)
    lines = []

    def add(code, quantity, rationale):
        priced = price_line(by_code[code], "manual", {}, rates, quantity)
        lines.append({**priced, "line_no": len(lines) + 1, "rationale": rationale, "evidence_refs": [],
                      "flags": [], "status": "ai_suggested", "origin": "rule"})

    if not worked:
        return {"name": "General Items", "lines": [], "group_total": Decimal(0)}

    add("PRL-004", 1, "Sheet-up and preparation of the working areas")
    area = sum((Decimal(str(g["derived"]["floor_area"])) for g in worked), Decimal(0))
    add("PRL-001", area, f"Floor protection across {len(worked)} room(s) with works")
    if idle_rooms:
        add("PRL-005", len(idle_rooms), "Protection to rooms without works and the access route")
    add("PRL-006", 1, "Site signage and first aid provision")
    if strip:
        skips = max(1, math.ceil(stripped_floor / SKIP_M2))
        add("PRL-002", skips, f"Waste from strip-out ({stripped_floor} m2 of floor covering)")
        add("PRL-008", 1, "Permit for the skip")
        add("PRL-007", 5, "Bagging strip-out waste")
        add("PRL-012", 8, "Labourer for strip-out support and waste handling")
    if days >= 2:
        add("PRL-009", max(1, math.ceil(days / 5)), f"Welfare for about {days} working days on site")
    if dusty:
        add("PRL-010", 1, "Dust extraction during strip-out and preparation")
        if days > 1:
            add("PRL-011", days - 1, f"Dust extraction for the remaining {days - 1} day(s)")
    if (strip or any(l["item_code"].startswith(("PLS", "DRY")) for l in all_lines)) and not has_electrical_test:
        add("ELC-001", 1, "Electrical test and certificate after water ingress and strip-out")
    add("PRL-003", len(worked), "Final clean, one per room with works")
    return {"name": "General Items", "lines": lines, "group_total": sum((l["total"] for l in lines), Decimal(0))}


def size_video_rooms(assessment_id: str, rooms: dict, image_results: dict) -> None:
    """Rooms named from video commentary start with a default size; estimate it from what the frames showed."""
    for room_id, room in rooms.items():
        if room["dimensions"].get("source") != "video":
            continue
        sizes = [v["findings"].room_size_estimate for v in image_results.values()
                 if v["room_id"] == room_id and "findings" in v and v["findings"].room_size_estimate]
        sizes = [z for z in sizes if z.length_m > 0 and z.breadth_m > 0]
        if not sizes:
            continue
        dims = {**room["dimensions"],
                "length_m": Decimal(str(round(median(max(z.length_m, z.breadth_m) for z in sizes), 2))),
                "breadth_m": Decimal(str(round(median(min(z.length_m, z.breadth_m) for z in sizes), 2)))}
        room.update(dimensions=dims, derived=derive(dims))
        store.put(room)


def reset_guessed_rooms(assessment_id: str, rooms: dict, image_results: dict) -> None:
    """Rooms guessed from views are rebuilt on every draft, so improved matching applies to old walks."""
    guessed = {room_id for room_id, r in rooms.items() if r.get("dimensions_source") == "estimated"}
    for room_id in guessed:
        store.table().delete_item(Key={"PK": store.asmt_pk(assessment_id), "SK": f"ROOM#{room_id}"})
        del rooms[room_id]
    for r in image_results.values():
        if r["room_id"] in guessed:
            r["room_id"] = None


def run(assessment_id: str, context=None) -> dict:
    r = Run(assessment_id, context)
    items = store.query_assessment(assessment_id)
    meta = next(i for i in items if i["SK"] == "META")
    rooms = {i["room_id"]: i for i in items if i["SK"].startswith("ROOM#")}
    image_results = load_results(r, assessment_id)
    if not image_results:
        raise ValueError("nothing to analyse yet: capture the scan or upload photos first")
    if all("error" in v for v in image_results.values()):
        raise RuntimeError(f"no image could be read: {next(iter(image_results.values()))['error']}")
    size_video_rooms(assessment_id, rooms, image_results)
    r.progress("Matching views to rooms")
    reset_guessed_rooms(assessment_id, rooms, image_results)
    assign_rooms(assessment_id, rooms, image_results)
    proposed = itemise_all(r, rooms, meta["peril"], image_results, meta.get("incident"))
    r.progress("Pricing and checking")
    schedule = price_and_check(proposed, rooms, image_results)
    failed = [k for k, v in image_results.items() if "error" in v]
    if failed:
        schedule["qa_findings"].append({"rule_id": "R00", "severity": "warn", "resolved": False, "line_refs": failed,
                                        "message": f"{len(failed)} image(s) could not be read and were skipped"})
    return {"assessment_id": assessment_id, **schedule}


def rooms_from_plan(assessment_id: str, evidence_id: str) -> int:
    """Read rooms and dimensions off a captured floor plan and save them as rooms (source "plan")."""
    item = store.table().get_item(Key={"PK": store.asmt_pk(assessment_id), "SK": f"EVD#{evidence_id}"}).get("Item")
    if item is None or item["kind"] != "floor_plan":
        raise ValueError("floor plan evidence not found")
    obj = s3().get_object(Bucket=os.environ["EVIDENCE_BUCKET"], Key=item["s3_key"])
    plan = ai.read_floor_plan(obj["Body"].read(), item["content_type"])
    if os.environ.get("KEEP_CAPTURES") != "1":
        s3().delete_object(Bucket=os.environ["EVIDENCE_BUCKET"], Key=item["s3_key"])
    for n, room in enumerate(plan.rooms):
        if room.length_m <= 0 or room.breadth_m <= 0:
            continue
        dims = {
            "length_m": Decimal(str(round(room.length_m, 2))),
            "breadth_m": Decimal(str(round(room.breadth_m, 2))),
            "height_m": Decimal("2.4"),  # plans carry no heights; assessor confirms
            "openings": [],
            "source": "plan",
        }
        room_id = hashlib.sha256(f"{assessment_id}:plan:{evidence_id}:{n}".encode()).hexdigest()[:8]
        store.put({
            "PK": store.asmt_pk(assessment_id),
            "SK": f"ROOM#{room_id}",
            "room_id": room_id,
            "name": room.name[:80],
            "dimensions": dims,
            "derived": derive(dims),
            "dimensions_source": room.dimensions_source,
            "plan_notes": room.notes[:300],
            "updated_at": store.now(),
        })
    return len(plan.rooms)


def plan_handler(event) -> None:
    assessment_id = event["assessment_id"]
    key = {"PK": store.asmt_pk(assessment_id), "SK": "META"}
    try:
        count = rooms_from_plan(assessment_id, event["evidence_id"])
        store.table().update_item(Key=key, UpdateExpression="SET plan_status = :s, plan_message = :m",
                                  ExpressionAttributeValues={":s": "done", ":m": f"Found {count} room(s)"})
    except Exception as exc:
        log.exception("floor plan failed for %s", assessment_id)
        store.table().update_item(Key=key, UpdateExpression="SET plan_status = :s, plan_message = :m",
                                  ExpressionAttributeValues={":s": "failed", ":m": str(exc)[:300]})


def handler(event, context):
    if event.get("task") == "floor_plan":
        return plan_handler(event)
    assessment_id = event["assessment_id"]
    if not store.set_status(assessment_id, "processing", expect=("queued",)):
        log.warning("assessment %s not queued; skipping duplicate invocation", assessment_id)
        return
    try:
        schedule = run(assessment_id, context)
        key = f"assessments/{assessment_id}/schedule/draft.json"
        _put_json(key, schedule)
        store.table().update_item(
            Key={"PK": store.asmt_pk(assessment_id), "SK": "META"},
            UpdateExpression="SET schedule_key = :k, progress = :p REMOVE #e",
            ExpressionAttributeNames={"#e": "error"},  # clear a failure left by an earlier run
            ExpressionAttributeValues={":k": key, ":p": "Done"},
        )
        store.set_status(assessment_id, "draft_ready")
    except Exception as exc:
        log.exception("pipeline failed for %s", assessment_id)
        store.set_status(assessment_id, "failed", error=_explain(exc))


def _explain(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, ai.anthropic.AuthenticationError) or "invalid x-api-key" in text:
        return ("The Claude API key was rejected (401). Store a valid key (starts sk-ant-api) in Secrets Manager "
                "as claimscope/anthropic-api-key, then capture again.")
    return text[:500]
