"""Claude stages. Stage A reads one image; Stage B turns a room's findings into catalogue lines.

The model never sees prices and never does arithmetic: it picks item codes and a quantity basis.
"""

import base64
import json
import logging
import os
from functools import cache
from typing import Literal

import anthropic
import boto3
from pydantic import BaseModel

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ModelDeclined(Exception):
    pass


def _key_from_secret(raw: str) -> str:
    """Accept a plaintext secret or a key/value secret (the console's default) with one value."""
    raw = raw.strip()
    if raw.startswith("{"):
        values = [v for v in json.loads(raw).values() if isinstance(v, str) and v.strip()]
        if len(values) != 1:
            raise ValueError("Anthropic key secret must hold exactly one value")
        raw = values[0].strip()
    return raw.strip('"')


@cache
def client() -> anthropic.Anthropic:
    secret_name = os.environ.get("ANTHROPIC_SECRET_NAME")
    if secret_name:
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=secret_name)["SecretString"]
        return anthropic.Anthropic(api_key=_key_from_secret(raw), max_retries=4)
    return anthropic.Anthropic(max_retries=4)  # local runs: ANTHROPIC_API_KEY or an `ant auth login` profile


def _parse(output_format, *, system, content, effort: str, max_tokens: int = 16000):
    response = client().beta.messages.parse(
        model=MODEL,
        max_tokens=max_tokens,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        system=system,
        messages=[{"role": "user", "content": content}],
        output_format=output_format,
    )
    if response.stop_reason == "refusal":
        category = response.stop_details.category if response.stop_details else None
        raise ModelDeclined(f"model declined (category: {category}, request {response._request_id})")
    if response.stop_reason == "max_tokens":
        raise ModelDeclined(f"output truncated at max_tokens (request {response._request_id})")
    log.info(
        "claude usage in=%s out=%s cache_read=%s cache_write=%s",
        response.usage.input_tokens,
        response.usage.output_tokens,
        response.usage.cache_read_input_tokens,
        response.usage.cache_creation_input_tokens,
    )
    return response.parsed_output


# --- Stage A: per-image damage detection ------------------------------------

Surface = Literal[
    "floor", "wall", "ceiling", "skirting", "door", "window",
    "kitchen_units", "bathroom_fittings", "electrical", "fixture", "other",
]
DamageType = Literal[
    "staining", "tide_mark", "mould", "swelling", "lifting", "delamination", "cracking",
    "peeling", "bubbling", "rot", "cut_out", "missing", "loose_tiles", "debris", "other",
]


class Observation(BaseModel):
    surface: Surface
    element: str
    damage_type: DamageType
    severity: Literal["minor", "moderate", "severe"]
    extent_hint: str
    location_description: str
    inspection_needed: bool
    confidence: float


class RoomSize(BaseModel):
    length_m: float
    breadth_m: float


RoomType = Literal[
    "kitchen", "open-plan kitchen/living", "living room", "dining room", "bedroom", "bathroom", "shower room",
    "en-suite", "wc", "hallway", "landing", "stairs", "utility", "storage", "study", "conservatory", "garage",
    "external", "unclear",
]


class ImageFindings(BaseModel):
    room_type_guess: RoomType
    room_match: str
    room_size_estimate: RoomSize | None
    finishes: list[str]
    observations: list[Observation]


STAGE_A_SYSTEM = """You are assisting a UK loss assessor with a property insurance claim. You look at one \
image of one room and record visible damage only.

Rules:
- Record only damage you can actually see. Never assume hidden damage. If damage probably continues out of \
view (behind units, under flooring, inside boxing), set inspection_needed=true and say why in \
location_description.
- extent_hint describes how much of the surface is affected in words an assessor would use: "lower 300mm \
of wall along left side", "approx 1m x 0.5m patch near window", "whole floor".
- finishes lists the finishes you can identify even where undamaged (e.g. "vinyl sheet floor", \
"emulsion walls", "MDF skirting", "textured ceiling"). Assessors need these to price reinstatement.
- confidence is 0 to 1. Use below 0.5 when lighting, angle or resolution makes you unsure.
- Images come from a 3D scan viewer, so expect wide-angle distortion and occasional stitching artefacts. \
Do not report artefacts as damage.
- An empty observations list is a valid and useful answer.
- room_type_guess: the kind of space this is, from the allowed list. Use "unclear" when the view shows too \
little to tell (a close-up of a wall, a doorway).
- room_match: when you are given a list of known rooms, the one this image most likely shows, copied \
exactly, or "unknown". When a room is already given, repeat it.
- room_size_estimate: a rough length and breadth of the whole room in metres if enough of it is visible \
to judge from fixtures (doors are about 0.8 m wide, kitchen worktops 0.6 m deep), otherwise null."""


def detect_damage(
    image_bytes: bytes, media_type: str, *, room_name: str | None, peril: str, room_options: list[str] = (),
    commentary: str = "",
) -> ImageFindings:
    if room_name:
        where = f"Room: {room_name}"
    elif room_options:
        where = "Room: not yet known. Known rooms: " + "; ".join(room_options)
    else:
        where = "Room: not yet known, and no floor plan has been read."
    content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(image_bytes).decode()},
        },
        {"type": "text", "text": f"{where}\nPeril: {peril.replace('_', ' ')}\n"
            + (f"The assessor said around this moment: \"{commentary}\"\nUse it to understand what you are \
looking at (what is damaged, what it is made of), but record only what the image itself shows.\n" if commentary else "")
            + "Record the visible damage."},
    ]
    return _parse(ImageFindings, system=STAGE_A_SYSTEM, content=content, effort="medium")


# --- Stage B: room itemisation ----------------------------------------------

QuantityBasis = Literal["floor_area", "ceiling_area", "wall_area", "perimeter", "count", "hours", "manual"]
LineFlag = Literal["inspection_needed", "hidden_damage_possible", "manual_quantity", "low_confidence", "from_brief"]


class ProposedLine(BaseModel):
    item_code: str
    quantity_basis: QuantityBasis
    manual_quantity: float | None
    rationale: str
    evidence_refs: list[str]
    confidence: float
    flags: list[LineFlag]


class RoomSchedule(BaseModel):
    room_name: str
    lines: list[ProposedLine]
    site_checks: list[str]


STAGE_B_INSTRUCTIONS = """You draft the schedule of works for one room of a UK property insurance claim. \
A loss assessor reviews and approves every line before anything is submitted, so be complete but never \
invent damage.

You receive the room's dimensions and derived quantities, the identified finishes, observations made from \
scan images (each tagged with an evidence id), the other rooms in the property, and usually an incident brief \
written by the assessor. Choose items ONLY from the catalogue below, by code.

The incident brief is the assessor's own site knowledge: cause, extent, moisture readings, floor and wall \
construction, what continues behind units or under coverings. Treat it as authoritative for things the images \
cannot show. When it says works are needed in this room, or throughout the property (for example a continuous \
floor covering replaced in every room, a damp subfloor needing DPM or screed, damage behind kitchen units), \
schedule the full reinstatement sequence here even without visible evidence, add the from_brief flag, and say \
in rationale which part of the brief it rests on. Do not stretch the brief beyond what it says. Without a \
brief, schedule visible damage only.

For each line choose a quantity_basis. The pricing engine computes the number from the room geometry:
- floor_area / ceiling_area / wall_area (m2) and perimeter (m, skirting length) for whole-surface work.
- count or hours with manual_quantity for EACH/NR/DAY/HR items (e.g. 1 door, 2 sockets).
- manual with manual_quantity only for partial areas or lengths (e.g. 3.5 m2 of plaster up to 1m high, \
2.4 m of kitchen plinth). Add the manual_quantity flag. Base the estimate on the room dimensions and the \
extent described; state your arithmetic in rationale.
Never pick an M2 item with perimeter, or an M item with an area.

Reinstatement rules assessors expect:
- Floor coverings: take up and dispose, then supply, then fit, as separate lines. Continuous floor \
coverings are replaced across the whole room, not patched.
- Stained ceilings or walls: stain block, then emulsion on the same surface. Decorate whole surfaces \
(the full wall area or ceiling), not patches, so the finish matches.
- Damaged skirting: remove, supply and fit, then paint.
- Mould: anti-fungal treatment once per room (count 1), plus decoration.
- Hidden or uncertain damage (behind units, under floors, inside boxing) that the brief does not confirm: add \
an investigation item with the inspection_needed flag, not the repair it might lead to.
- Subfloor works the brief calls for go in order: take up covering, scarify or prepare, DPM, screed or \
levelling, plywood where a timber base is used, then the new covering.
- Preparing a room for decoration (taking down and refitting fittings, releasing sockets) is one line per \
room; decorating sundries are one line per room that is decorated.
- Do not add general items (protection, skips, welfare, signage, dust extraction, labourers, final clean, \
the electrical test certificate): rules add those for the whole job.
- Contents (furniture, soft furnishings, belongings, loose rugs and carpets) belong to the contents claim, \
not this buildings schedule. Do not schedule them; mention them in site_checks if they matter.
- One line per item code per room. No duplicates.

evidence_refs lists the evidence ids that support each line. Use low_confidence when the evidence is weak. \
site_checks: at most 5 things the assessor should check on site, each one short sentence (under 20 words), \
most important first. Plain words, no evidence ids, no catalogue codes. Leave out anything already covered by \
a line's flags.

Catalogue (code | description | unit | operation | default basis):
"""


@cache
def _catalogue_block(catalogue_json: str) -> str:
    items = json.loads(catalogue_json)
    rows = [
        f"{i['code']} | {i['description']} | {i['unit']} | {i['operation']} | {i.get('default_quantity_basis', 'manual')}"
        for i in items
        if i["operation"] != "prelim"
    ]
    return STAGE_B_INSTRUCTIONS + "\n".join(rows)


def itemise_room(room_context: dict, catalogue_items: list[dict]) -> RoomSchedule:
    system = [
        {
            "type": "text",
            # Identical across rooms and assessments, so it is cached.
            "text": _catalogue_block(json.dumps(catalogue_items, sort_keys=True)),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    content = [{"type": "text", "text": json.dumps(room_context, indent=1, default=str)}]
    return _parse(RoomSchedule, system=system, content=content, effort="high")


# --- Floor plan: rooms and dimensions -----------------------------------------


class PlanRoom(BaseModel):
    name: str
    length_m: float
    breadth_m: float
    dimensions_source: Literal["labelled", "scaled", "estimated"]
    notes: str


class FloorPlan(BaseModel):
    rooms: list[PlanRoom]
    dimensions_visible: bool
    notes: str


FLOOR_PLAN_SYSTEM = """You read a floor plan view captured from a 3D property scan viewer for a UK loss assessor. \
List every room or space you can see (including halls, landings, corridors, cupboards and en-suites) with its \
length and breadth in metres.

- dimensions_source "labelled": the dimensions are printed on the plan. Use them exactly. Convert feet and \
inches to metres.
- "scaled": no label for this room, but a scale bar or other labelled rooms let you measure it. Explain how in notes.
- "estimated": no usable scale. Estimate from typical UK fixtures (doors about 0.76 to 0.84 m wide, \
standard bath 1.7 m) and say so in notes.
- length_m is the longer side. For L-shaped or irregular rooms give the overall bounding rectangle and \
note the shape in notes.
- Use the room labels shown on the plan. If unlabelled, name rooms by apparent use (e.g. "Bedroom 1", \
"Bathroom") and say so in notes.
- If the image is not a floor plan, return an empty rooms list and explain in notes."""


def read_floor_plan(image_bytes: bytes, media_type: str) -> FloorPlan:
    content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(image_bytes).decode()},
        },
        {"type": "text", "text": "List the rooms and their dimensions."},
    ]
    return _parse(FloorPlan, system=FLOOR_PLAN_SYSTEM, content=content, effort="high")


# --- Walkthrough video commentary -----------------------------------------------


class VideoRoom(BaseModel):
    name: str
    room_type: RoomType
    start_s: float
    end_s: float
    notes: str


class VideoPlan(BaseModel):
    rooms: list[VideoRoom]
    detail: str
    cause: str
    further_info: str
    contents_mentioned: list[str]


VIDEO_PLAN_SYSTEM = """You read the transcript of a loss assessor's walkthrough video of a UK property \
insurance claim. Lines are timestamped in seconds. The assessor walks room to room and says what they see.

Return:
- rooms: the rooms in the order filmed, with start_s and end_s covering the whole video between them \
(the first starts at 0, each ends where the next starts, the last ends at the video length). A room \
starts when the assessor says they are entering it or starts describing it. Use the assessor's own \
names, numbered when repeated ("Bedroom 1", "Bedroom 2"). notes: what they said about that room, in \
plain words. If nothing is said for a stretch, keep it with the previous room.
- detail, cause, further_info: an incident brief in the assessor's terms, as on an incident report: \
which areas are affected; the cause; and anything about extent, construction, floor continuity, \
moisture or repairs already done. Only what was said; leave a field empty if nothing was said about it.
- contents_mentioned: belongings, furniture, soft furnishings or loose carpets said to be damaged. \
These go to the contents claim, not the buildings schedule.

Transcripts come from speech recognition and contain errors. Correct obvious mishearings from context \
(for example "boss pipe" is "burst pipe") but do not invent facts."""


def plan_video(timeline: str, duration_s: float) -> VideoPlan:
    content = [{"type": "text", "text": f"Video length: {duration_s:.0f}s\n\nTranscript:\n{timeline}"}]
    return _parse(VideoPlan, system=VIDEO_PLAN_SYSTEM, content=content, effort="medium")
