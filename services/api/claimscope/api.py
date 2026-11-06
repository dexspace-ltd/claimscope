"""HTTP API Lambda (payload v2). Short requests only; analysis runs in the pipeline Lambda."""

import base64
import hashlib
import ipaddress
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config

from . import store
from .geometry import derive

PERILS = {"escape_of_water", "flood", "fire", "storm", "impact", "subsidence", "accidental", "malicious"}
EVIDENCE_KINDS = {"photo": "image/", "scan_view": "image/", "floor_plan": "image/", "voice": "audio/", "video": "video/"}
PRESIGN_TTL_SECONDS = 300
ASSESSMENTS_PER_IP_PER_DAY = int(os.environ.get("RATE_LIMIT_PER_DAY", "2"))
MAX_VIEWS_PER_ASSESSMENT = 400  # cumulative, so repeated walks can't run up the Claude bill
MAX_DRAFTS_PER_ASSESSMENT = 5
# Share links (my.matterport.com/show/?m=ID), Discover pages (discover.matterport.com/space/ID),
# model pages (my.matterport.com/models/ID), or a bare 11-character model ID.
MATTERPORT_URL = re.compile(
    r"^(?:https://my\.matterport\.com/show/?\?(?:.*&)?m=([A-Za-z0-9]{11})(?:&.*)?"
    r"|https://(?:discover|my)\.matterport\.com/(?:space|models)/([A-Za-z0-9]{11})(?:[/?#].*)?"
    r"|([A-Za-z0-9]{11}))$"
)

_s3 = None
_lambda = None


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            region_name=os.environ["AWS_REGION"],
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3


def lambda_client():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda")
    return _lambda


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def respond(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, default=_json_default),
    }


def parse_body(event) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        body = json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError:
        raise HttpError(400, "body must be JSON")
    if not isinstance(body, dict):
        raise HttpError(400, "body must be a JSON object")
    return body


def require_meta(assessment_id: str) -> dict:
    meta = store.get_meta(assessment_id)
    if meta is None:
        raise HttpError(404, "assessment not found")
    return meta


def _strip_keys(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in ("PK", "SK")}


def _capture_meta(raw) -> dict | None:
    """Where in the scan a view was taken: sweep id plus camera yaw/pitch in degrees."""
    if not isinstance(raw, dict):
        return None
    out = {"sweep_id": str(raw.get("sweep_id", ""))[:64]}
    for k in ("yaw", "pitch"):
        if isinstance(raw.get(k), (int, Decimal)):
            out[k] = raw[k]
    return out


# --- demo limits -------------------------------------------------------------


def client_ip(event) -> str:
    """The viewer's address as CloudFront saw it ("ip:port"; IPv6 is unbracketed)."""
    headers = event.get("headers") or {}
    viewer = headers.get("cloudfront-viewer-address")
    if viewer:
        return viewer.rsplit(":", 1)[0].strip("[]")
    return event["requestContext"]["http"].get("sourceIp", "unknown")


def _ip_key(ip: str) -> str:
    # Only a salted hash is stored, never the address itself.
    return hashlib.sha256(f"{os.environ.get('IP_HASH_SALT', '')}:{ip}".encode()).hexdigest()[:32]


def is_exempt(ip: str) -> bool:
    """RATE_LIMIT_EXEMPT_IPS: comma-separated addresses or networks (e.g. 203.0.113.7, 2001:db8:1:2::/64)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in os.environ.get("RATE_LIMIT_EXEMPT_IPS", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def take_daily_allowance(event) -> None:
    ip = client_ip(event)
    if is_exempt(ip):
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        store.table().update_item(
            Key={"PK": f"RATE#{_ip_key(ip)}", "SK": day},
            UpdateExpression="ADD n :one SET #ttl = :ttl",
            ConditionExpression="attribute_not_exists(n) OR n < :limit",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":one": 1, ":limit": ASSESSMENTS_PER_IP_PER_DAY,
                                       ":ttl": int(time.time()) + 2 * 86400},
        )
    except store.table().meta.client.exceptions.ConditionalCheckFailedException:
        raise HttpError(429, f"Demo limit reached: {ASSESSMENTS_PER_IP_PER_DAY} new assessments per day from your network. "
                             "Try again tomorrow, or reopen an earlier assessment.")


def take_assessment_allowance(event, assessment_id: str, counter: str, limit: int, message: str) -> None:
    if is_exempt(client_ip(event)):
        return
    try:
        store.table().update_item(
            Key={"PK": store.asmt_pk(assessment_id), "SK": "META"},
            UpdateExpression="ADD #c :one",
            ConditionExpression=f"attribute_exists(PK) AND (attribute_not_exists(#c) OR #c < :limit)",
            ExpressionAttributeNames={"#c": counter},
            ExpressionAttributeValues={":one": 1, ":limit": limit},
        )
    except store.table().meta.client.exceptions.ConditionalCheckFailedException:
        raise HttpError(429, message)


# --- handlers -------------------------------------------------------------


def _matterport_model(url) -> str:
    match = MATTERPORT_URL.fullmatch(str(url).strip())
    if not match:
        raise HttpError(400, "matterport_url must be a Matterport share or Discover link")
    return next(g for g in match.groups() if g)


INCIDENT_FIELDS = ("date_of_incident", "date_noticed", "detail", "cause", "further_info")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _incident(raw) -> dict:
    """The assessor's incident brief, as on the incident report page of a schedule."""
    if not isinstance(raw, dict):
        raise HttpError(400, "incident must be an object")
    out = {}
    for name in INCIDENT_FIELDS:
        value = str(raw.get(name) or "").strip()
        if name.startswith("date") and value and not DATE.match(value):
            raise HttpError(400, f"{name} must be YYYY-MM-DD")
        if value:
            out[name] = value[:2000]
    return out


def update_assessment(event, assessment_id):
    require_meta(assessment_id)
    body = parse_body(event)
    sets, values = ["updated_at = :u"], {":u": store.now()}
    if "matterport_url" in body:
        sets.append("matterport_model = :m")
        values[":m"] = _matterport_model(body["matterport_url"])
    if "incident" in body:
        incident = _incident(body["incident"])
        sets.append("incident = :i")
        values[":i"] = incident
        if incident.get("date_of_incident"):
            sets.append("date_of_loss = :d")
            values[":d"] = incident["date_of_incident"]
    if len(sets) == 1:
        raise HttpError(400, "nothing to update")
    store.table().update_item(
        Key={"PK": store.asmt_pk(assessment_id), "SK": "META"},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeValues=values,
    )
    return respond(200, {"assessment_id": assessment_id, **{k: body[k] for k in ("incident",) if k in body}})


def create_assessment(event):
    body = parse_body(event)
    peril = body.get("peril")
    if peril not in PERILS:
        raise HttpError(400, f"peril must be one of {sorted(PERILS)}")
    matterport_model = _matterport_model(body.get("matterport_url")) if body.get("matterport_url") else None
    take_daily_allowance(event)
    assessment_id = uuid.uuid4().hex[:12]
    meta = {
        "PK": store.asmt_pk(assessment_id),
        "SK": "META",
        "assessment_id": assessment_id,
        "claim_ref": str(body.get("claim_ref", ""))[:64],
        "peril": peril,
        "date_of_loss": body.get("date_of_loss"),
        "matterport_model": matterport_model,
        "status": "created",
        "created_at": store.now(),
    }
    store.put(meta)
    return respond(201, _strip_keys(meta))


def get_assessment(event, assessment_id):
    items = store.query_assessment(assessment_id)
    meta = next((i for i in items if i["SK"] == "META"), None)
    if meta is None:
        raise HttpError(404, "assessment not found")
    return respond(200, {
        **_strip_keys(meta),
        "rooms": [_strip_keys(i) for i in items if i["SK"].startswith("ROOM#")],
        "evidence": [{k: v for k, v in _strip_keys(i).items() if k not in ("findings_json", "transcript")}
                     for i in items if i["SK"].startswith("EVD#")],
    })


def put_room(event, assessment_id):
    require_meta(assessment_id)
    body = parse_body(event)
    name = str(body.get("name", "")).strip()
    if not name:
        raise HttpError(400, "room name is required")
    raw = body.get("dimensions") or {}
    try:
        dims = {
            "length_m": raw["length_m"],
            "breadth_m": raw["breadth_m"],
            "height_m": raw["height_m"],
            "openings": [
                {"kind": o["kind"], "width_m": o["width_m"], "height_m": o["height_m"]}
                for o in raw.get("openings") or []
            ],
            "source": raw.get("source", "assessor"),
        }
        if any(o["kind"] not in ("door", "window", "other") for o in dims["openings"]):
            raise ValueError("opening kind must be door, window or other")
        if dims["source"] not in ("assessor", "scan", "scan_corrected", "plan", "estimated", "video"):
            raise ValueError("source must be assessor, scan, scan_corrected, plan, estimated or video")
        derived = derive(dims)
    except (KeyError, TypeError, ArithmeticError, ValueError) as exc:
        raise HttpError(400, f"invalid dimensions: {exc}")
    scan_room_id = str(body["scan_room_id"])[:64] if body.get("scan_room_id") else None
    if scan_room_id:
        # Re-capturing the same scan updates rooms in place instead of duplicating them.
        room_id = hashlib.sha256(f"{assessment_id}:{scan_room_id}".encode()).hexdigest()[:8]
    else:
        room_id = body.get("room_id") or uuid.uuid4().hex[:8]
    if not re.fullmatch(r"[0-9a-f]{8}", room_id):
        raise HttpError(400, "invalid room_id")
    room = {
        "PK": store.asmt_pk(assessment_id),
        "SK": f"ROOM#{room_id}",
        "room_id": room_id,
        "name": name[:80],
        "dimensions": dims,
        "derived": derived,
        "scan_room_id": scan_room_id,
        "name_is_placeholder": bool(body.get("name_is_placeholder")),
        "updated_at": store.now(),
    }
    store.put(room)
    return respond(200, _strip_keys(room))


def presign_evidence(event, assessment_id):
    require_meta(assessment_id)
    body = parse_body(event)
    kind = body.get("kind")
    content_type = str(body.get("content_type", ""))
    if kind not in EVIDENCE_KINDS:
        raise HttpError(400, f"kind must be one of {sorted(EVIDENCE_KINDS)}")
    if not content_type.startswith(EVIDENCE_KINDS[kind]):
        raise HttpError(400, f"{kind} needs a {EVIDENCE_KINDS[kind]}* content type")

    if kind not in ("floor_plan", "video"):
        take_assessment_allowance(event, assessment_id, "views_used", MAX_VIEWS_PER_ASSESSMENT,
                                  f"Demo limit reached: {MAX_VIEWS_PER_ASSESSMENT} captured views per assessment.")
    evidence_id = uuid.uuid4().hex[:12]
    if kind == "video":
        key = f"videos/{assessment_id}/{evidence_id}"  # processed by pipeline.video_handler
        store.table().update_item(
            Key={"PK": store.asmt_pk(assessment_id), "SK": "META"},
            UpdateExpression="SET video_status = :s, video_message = :m",
            ExpressionAttributeValues={":s": "uploading", ":m": "Uploading the video"},
        )
    else:
        key = f"captures/{assessment_id}/{kind}/{evidence_id}"  # analysed on arrival; see pipeline.analyse_view
    url = s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": os.environ["EVIDENCE_BUCKET"], "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGN_TTL_SECONDS,
    )
    store.put({
        "PK": store.asmt_pk(assessment_id),
        "SK": f"EVD#{evidence_id}",
        "evidence_id": evidence_id,
        "kind": kind,
        "room_id": body.get("room_id"),
        "s3_key": key,
        "content_type": content_type,
        "capture": _capture_meta(body.get("capture")),
        "analysis": "pending",
        "created_at": store.now(),
    })
    return respond(200, {"evidence_id": evidence_id, "upload_url": url, "headers": {"content-type": content_type}})


def start_analysis(event, assessment_id):
    require_meta(assessment_id)
    take_assessment_allowance(event, assessment_id, "drafts_used", MAX_DRAFTS_PER_ASSESSMENT,
                              f"Demo limit reached: {MAX_DRAFTS_PER_ASSESSMENT} drafts per assessment.")
    if not store.set_status(assessment_id, "queued", expect=("created", "draft_ready", "failed"), progress="Queued"):
        raise HttpError(409, "analysis already running")
    lambda_client().invoke(
        FunctionName=os.environ["PIPELINE_FUNCTION"],
        InvocationType="Event",
        Payload=json.dumps({"assessment_id": assessment_id}).encode(),
    )
    return respond(202, {"assessment_id": assessment_id, "status": "queued"})


def rooms_from_plan(event, assessment_id):
    require_meta(assessment_id)
    evidence_id = str(parse_body(event).get("evidence_id", ""))
    if not re.fullmatch(r"[0-9a-f]{12}", evidence_id):
        raise HttpError(400, "evidence_id is required")
    store.table().update_item(
        Key={"PK": store.asmt_pk(assessment_id), "SK": "META"},
        UpdateExpression="SET plan_status = :s, plan_message = :m",
        ExpressionAttributeValues={":s": "reading", ":m": "Reading the floor plan…"},
    )
    lambda_client().invoke(
        FunctionName=os.environ["PIPELINE_FUNCTION"],
        InvocationType="Event",
        Payload=json.dumps({"task": "floor_plan", "assessment_id": assessment_id, "evidence_id": evidence_id}).encode(),
    )
    return respond(202, {"plan_status": "reading"})


def evidence_image(event, assessment_id, evidence_id):
    """Redirect to a short-lived link for a kept capture (evidence thumbnails on schedule lines)."""
    item = store.table().get_item(Key={"PK": store.asmt_pk(assessment_id), "SK": f"EVD#{evidence_id}"}).get("Item")
    if item is None or not item.get("s3_key"):
        raise HttpError(404, "evidence not found")
    url = s3().generate_presigned_url(
        "get_object", Params={"Bucket": os.environ["EVIDENCE_BUCKET"], "Key": item["s3_key"]}, ExpiresIn=PRESIGN_TTL_SECONDS
    )
    return {"statusCode": 302, "headers": {"location": url, "cache-control": "private, max-age=240"}, "body": ""}


def reset_views(event, assessment_id):
    """Drop the tour's captured views (before a fresh auto-walk) and rooms guessed from them. Video frames stay."""
    require_meta(assessment_id)
    removed = 0
    for item in store.query_assessment(assessment_id):
        sk = item["SK"]
        from_video = str((item.get("capture") or {}).get("sweep_id", "")).startswith("video:")
        is_view = sk.startswith("EVD#") and item.get("kind") == "scan_view" and not from_video
        is_guessed_room = sk.startswith("ROOM#") and item.get("dimensions_source") == "estimated"
        if is_view or is_guessed_room:
            store.table().delete_item(Key={"PK": item["PK"], "SK": sk})
            removed += is_view
            if is_view and item.get("s3_key"):
                s3().delete_object(Bucket=os.environ["EVIDENCE_BUCKET"], Key=item["s3_key"])
    return respond(200, {"removed_views": removed})


def delete_room(event, assessment_id, room_id):
    require_meta(assessment_id)
    store.table().delete_item(Key={"PK": store.asmt_pk(assessment_id), "SK": f"ROOM#{room_id}"})
    return respond(200, {"deleted": room_id})


def get_schedule(event, assessment_id):
    meta = require_meta(assessment_id)
    if meta["status"] != "draft_ready":
        return respond(200, {"status": meta["status"], "error": meta.get("error")})
    obj = s3().get_object(Bucket=os.environ["EVIDENCE_BUCKET"], Key=meta["schedule_key"])
    return respond(200, {"status": meta["status"], "schedule": json.loads(obj["Body"].read())})


def whoami(event):
    """Your own address as the demo sees it (for adding it to the rate-limit exemptions)."""
    ip = client_ip(event)
    return respond(200, {"ip": ip, "rate_limit_exempt": is_exempt(ip)})


ID = r"([0-9a-f]{12})"
ROUTES = [
    ("GET", re.compile(r"/api/whoami"), whoami),
    ("POST", re.compile(r"/api/assessments"), create_assessment),
    ("GET", re.compile(rf"/api/assessments/{ID}"), get_assessment),
    ("PATCH", re.compile(rf"/api/assessments/{ID}"), update_assessment),
    ("POST", re.compile(rf"/api/assessments/{ID}/rooms"), put_room),
    ("POST", re.compile(rf"/api/assessments/{ID}/evidence/presign"), presign_evidence),
    ("POST", re.compile(rf"/api/assessments/{ID}/analyse"), start_analysis),
    ("POST", re.compile(rf"/api/assessments/{ID}/rooms-from-plan"), rooms_from_plan),
    ("DELETE", re.compile(rf"/api/assessments/{ID}/rooms/([0-9a-f]{{8}})"), delete_room),
    ("DELETE", re.compile(rf"/api/assessments/{ID}/views"), reset_views),
    ("GET", re.compile(rf"/api/assessments/{ID}/evidence/{ID}/image"), evidence_image),
    ("GET", re.compile(rf"/api/assessments/{ID}/schedule"), get_schedule),
]


def handler(event, context):
    # Only CloudFront knows this value, so the API can't be called directly to dodge the limits.
    expected = os.environ.get("ORIGIN_VERIFY")
    if expected and (event.get("headers") or {}).get("x-origin-verify") != expected:
        return respond(403, {"error": "forbidden"})
    method = event["requestContext"]["http"]["method"]
    path = event["rawPath"].rstrip("/")
    try:
        for route_method, pattern, fn in ROUTES:
            match = pattern.fullmatch(path)
            if match and route_method == method:
                return fn(event, *match.groups())
        raise HttpError(404, "not found")
    except HttpError as exc:
        return respond(exc.status, {"error": str(exc)})
