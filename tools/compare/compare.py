# /// script
# requires-python = ">=3.12"
# dependencies = ["pdfplumber>=0.11"]
# ///
"""Compare a ClaimScope draft with the assessor's original schedule. Runs entirely on this machine.

The original report is live claim data: it is parsed locally and never uploaded. Output goes to
./local/ (git-ignored).

    uv run tools/compare/compare.py --report path/to/original.pdf --draft claimscope-<id>.json

If the PDF layout defeats the parser, export or type the lines into a CSV with columns
room,description,unit,quantity,total and pass that as --report instead.
"""

import argparse
import csv
import difflib
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

UNITS = r"m2|m²|sqm|sq\.?m|lm|m|each|ea|nr|no\.?|item|items|hrs?|hours?|days?"
NUM = r"\d[\d,]*(?:\.\d+)?"
MONEY = r"£?\s?\d[\d,]*\.\d{2}"
LINE_PATTERNS = [
    # description  qty  unit  rate  total
    re.compile(rf"^(?P<desc>.+?)\s+(?P<qty>{NUM})\s*(?P<unit>{UNITS})\s+(?P<rate>{MONEY})\s+(?P<total>{MONEY})$", re.I),
    # description  unit  qty  rate  total
    re.compile(rf"^(?P<desc>.+?)\s+(?P<unit>{UNITS})\s+(?P<qty>{NUM})\s+(?P<rate>{MONEY})\s+(?P<total>{MONEY})$", re.I),
    # description  qty  unit  total
    re.compile(rf"^(?P<desc>.+?)\s+(?P<qty>{NUM})\s*(?P<unit>{UNITS})\s+(?P<total>{MONEY})$", re.I),
]
SKIP = re.compile(r"\b(group total|sub ?total|total|net|vat|gross|page \d+|carried forward|brought forward)\b", re.I)

# Coarse work keys so differently-worded lines about the same work can be paired.
ELEMENTS = [
    ("vinyl", r"vinyl|lino|lvt|karndean"), ("laminate", r"laminate"), ("carpet", r"carpet|underlay|gripper"),
    ("floor_tiles", r"floor til"), ("wall_tiles", r"wall til|tiling to walls"), ("subfloor", r"ply|latex|levelling|screed|dpm|membrane|scrabbl|scarif"),
    ("skirting", r"skirting"), ("ceiling_board", r"ceiling.*(board|plaster)|(board|plaster).*ceiling"),
    ("plaster", r"plaster|skim|bonding|hack"), ("stain_block", r"stain ?block|stain seal"),
    ("anti_fungal", r"anti.?fungal|mould|fungicid(?!e sealant)"), ("wallpaper", r"wallpaper|lining paper"),
    ("emulsion_ceiling", r"ceiling.*(emulsion|paint|decorat)|(emulsion|paint|decorat).*ceiling"),
    ("emulsion_walls", r"emulsion|paint.*wall|wall.*paint|decorat"), ("gloss", r"gloss|eggshell|satinwood"),
    ("door", r"\bdoors?\b|architrave|lining"), ("kitchen", r"kitchen|plinth|base unit|worktop"),
    ("wc", r"\bwc\b|toilet|cistern|pan\b"), ("bath", r"\bbath\b|bath panel"), ("basin", r"basin|vanity|sink"),
    ("sealant", r"sealant|silicone|mastic|caulk"), ("electrical", r"electric|socket|switch|light|eicr|circuit"),
    ("drying", r"dehumid|drying|moisture"), ("protection", r"protect"), ("skip", r"skip"),
    ("clean", r"clean"), ("radiator", r"radiator|towel rail"), ("dust", r"dust extract"),
    ("labour", r"labour hours|labourer"), ("welfare", r"welfare|toilet|signage|black sacks|skip permit"), ("debris", r"debris|strip.?out|waste"), ("inspect", r"inspect|investigat|cut.?out|exploratory"),
]
OPERATIONS = [
    ("refit", r"set aside|refit|re-?fix|remove and replace|r ?& ?r"), ("supply_fit", r"supply (and|&) (fit|lay|hang|install)"),
    ("remove", r"take up|uplift|remove|strip|hack off|take down|dispose"), ("supply", r"supply"),
    ("fit", r"\bfit|\blay|hang|install"), ("apply", r"apply|paint|coat|treat|seal|clean|emulsion|gloss"),
]


@dataclass
class Line:
    room: str
    description: str
    unit: str
    quantity: float
    total: float
    element: str = ""
    operation: str = ""


@dataclass
class ReportDoc:
    lines: list
    room_dims: dict = field(default_factory=dict)   # room -> {"floor_area", "wall_area", "perimeter"}
    cover_totals: dict = field(default_factory=dict)  # "net" / "vat" / "gross" from the cover page


@dataclass
class RoomResult:
    name: str
    matched: list = field(default_factory=list)
    missed: list = field(default_factory=list)
    extra: list = field(default_factory=list)


def money(text: str) -> float:
    return float(re.sub(r"[£,\s]", "", text))


def classify(line: Line) -> Line:
    text = line.description.lower()
    line.element = next((k for k, rx in ELEMENTS if re.search(rx, text)), "other")
    line.operation = next((k for k, rx in OPERATIONS if re.search(rx, text)), "other")
    return line


# Insurer-schedule layout: "ROOM <name>", a metric dimensions table, then numbered lines
# "<no> <sub category + description> <unit> <qty> [£cost]" whose descriptions wrap onto following lines.
SCHED_ROOM = re.compile(r"^ROOM\s+(.+)$")
SCHED_LINE = re.compile(rf"^(?P<no>\d{{1,3}})\s+(?P<desc>.+?)\s+(?P<unit>Each|HR|M2|M|NR|LM|Day|Days|Item)\s+(?P<qty>{NUM})(?:\s+(?P<cost>{MONEY}))?$", re.I)
SCHED_STOP = re.compile(r"^(Group Total|Special Notes|www\.|ROOM\s|Client\s|Damage\s|DIMENSIONS|Length\s|Wall Size|Square Size|Linear Form|Line\s+Sub|item$|no\.$)", re.I)
COVER_TOTAL = re.compile(rf"Total Price \((NET|GROSS)\)\s+({MONEY})|Tax \(Client\)\s+({MONEY})", re.I)


def parse_schedule_layout(texts: list[str]) -> ReportDoc:
    doc = ReportDoc(lines=[])
    room, current = "General", None
    for text in texts:
        for raw in text.splitlines():
            line = " ".join(raw.split())
            if not line:
                continue
            for m in COVER_TOTAL.finditer(line):
                if m.group(1):
                    doc.cover_totals[m.group(1).lower()] = money(m.group(2))
                else:
                    doc.cover_totals["vat"] = money(m.group(3))
            if m := SCHED_ROOM.match(line):
                room, current = m.group(1).strip().title(), None
                continue
            dims = doc.room_dims.setdefault(room, {})
            if m := re.match(rf"^Wall Size \(M2\)\s+{NUM}\s+{NUM}\s+{NUM}\s+({NUM})$", line):
                dims["wall_area"] = float(m.group(1).replace(",", ""))
            elif m := re.match(rf"^Square Size \(M2\)\s+({NUM})$", line):
                dims["floor_area"] = float(m.group(1).replace(",", ""))
            elif m := re.match(rf"^Linear Form \(lm\)\s+({NUM})$", line):
                dims["perimeter"] = float(m.group(1).replace(",", ""))
            if m := SCHED_LINE.match(line):
                current = Line(room, m["desc"].strip(), m["unit"].upper(), float(m["qty"].replace(",", "")),
                               money(m["cost"]) if m["cost"] else 0.0)
                doc.lines.append(current)
            elif SCHED_STOP.match(line):
                current = None
            elif current is not None:
                current.description += " " + line  # wrapped sub category / description text
    doc.room_dims = {k: v for k, v in doc.room_dims.items() if v}
    return doc


def parse_pdf(path: Path) -> ReportDoc:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        texts = [page.extract_text() or "" for page in pdf.pages]
    if any(SCHED_ROOM.match(" ".join(l.split())) for t in texts for l in t.splitlines()):
        return parse_schedule_layout(texts)
    return ReportDoc(lines=parse_generic(texts))


def parse_generic(texts: list[str]) -> list[Line]:
    """Fallback for other layouts: "description qty unit [rate] total" lines under short headings."""
    lines, room = [], "General"
    for page in texts:
        for raw in page.splitlines():
            text = " ".join(raw.split())
            if not text:
                continue
            match = next((m for p in LINE_PATTERNS if (m := p.match(text))), None)
            if match and not SKIP.search(match["desc"]):
                lines.append(Line(room, match["desc"].strip(" .-"), match["unit"].upper(),
                                  float(match["qty"].replace(",", "")), money(match["total"])))
            elif not re.search(r"\d", text) and len(text) <= 40 and not SKIP.search(text):
                room = text.strip(" :")  # short text-only line: treat as a group heading
    return lines


def parse_csv(path: Path) -> ReportDoc:
    with path.open(newline="") as f:
        return ReportDoc(lines=[Line(r["room"], r["description"], r.get("unit", ""), float(r.get("quantity") or 0),
                     float(str(r.get("total") or 0).replace("£", "").replace(",", ""))) for r in csv.DictReader(f)])


def draft_lines(draft: dict) -> list[Line]:
    out = []
    for group in [draft["general_items"], *draft["rooms"]]:
        for l in group["lines"]:
            out.append(Line(group["name"], l["description"], l["unit"], float(l["quantity"]), float(l["total"])))
    return out


def norm_room(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    text = text.replace("living room", "lounge").replace("wc", "toilet").replace("en suite", "ensuite")
    # drop unit/flat codes such as "903c" or "flat 702c"; they never appear in draft room names
    words = [w for w in text.split() if not re.search(r"\d", w) or w.isdigit() and len(w) == 1]
    return " ".join(w for w in words if w not in ("flat", "and", "the", "room", "area"))


def room_similarity(a: str, b: str) -> float:
    na, nb = norm_room(a), norm_room(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    overlap = len(ta & tb) / min(len(ta), len(tb))  # "kitchen" vs "lounge kitchen" -> 1.0
    if overlap:
        return overlap
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return ratio if ratio >= 0.8 else 0.0  # spelling-alike words ("bedroom"/"bathroom") are not a match


def pair_rooms(report_rooms: list[str], draft_rooms: list[str]) -> dict[str, str]:
    """Map each report room to its most similar draft room, best pairs first, one-to-one."""
    pairs = sorted(((room_similarity(r, d), r, d) for r in report_rooms for d in draft_rooms), reverse=True)
    mapping, used = {}, set()
    for score, r, d in pairs:
        if score >= 0.5 and r not in mapping and d not in used:
            mapping[r] = d
            used.add(d)
    return mapping


def compare(doc: ReportDoc, draft: list[Line], draft_rooms: dict | None = None) -> tuple[list[RoomResult], dict]:
    report = [classify(l) for l in doc.lines]
    draft = [classify(l) for l in draft]
    rooms = list(dict.fromkeys(l.room for l in report))
    mapping = pair_rooms(rooms, list(dict.fromkeys(l.room for l in draft)))
    remaining = list(draft)
    results = []

    draft_rooms = draft_rooms or {}
    for room in rooms:
        res = RoomResult(f"{room} ↔ {mapping.get(room, '—')}")
        theirs_d, ours_d = doc.room_dims.get(room, {}), draft_rooms.get(mapping.get(room), {})
        if theirs_d.get("floor_area") and ours_d.get("floor_area"):
            res.name += f"  (floor {theirs_d['floor_area']:g} m² vs ours {float(ours_d['floor_area']):g} m²)"
        ours = [l for l in remaining if l.room == mapping.get(room)]
        for theirs in [l for l in report if l.room == room]:
            candidates = [o for o in ours if o.element == theirs.element and o.operation == theirs.operation] or \
                         [o for o in ours if o.element == theirs.element != "other"]
            if candidates:
                best = min(candidates, key=lambda o: abs(o.quantity - theirs.quantity))
                ours.remove(best)
                remaining.remove(best)
                res.matched.append((theirs, best))
            else:
                res.missed.append(theirs)
        res.extra = ours
        for o in ours:
            remaining.remove(o)
        results.append(res)

    if remaining:
        results.append(RoomResult("Draft rooms not in report", extra=remaining))

    matched = sum(len(r.matched) for r in results)
    summary = {
        "report_lines": len(report),
        "draft_lines": len(draft),
        "matched": matched,
        "recall": matched / len(report) if report else 0,
        "precision": matched / len(draft) if draft else 0,
        "report_total": doc.cover_totals.get("net") or sum(l.total for l in report),
        "report_total_source": "cover NET" if doc.cover_totals.get("net") else "sum of line costs",
        "draft_total": sum(l.total for l in draft),
    }
    return results, summary


def render_html(results: list[RoomResult], summary: dict, title: str) -> str:
    e = html.escape

    def row(*cells, cls=""):
        return f"<tr class='{cls}'>" + "".join(f"<td>{e(str(c))}</td>" for c in cells) + "</tr>"

    body = []
    for r in results:
        rows = [row(t.description, f"{t.quantity:g} {t.unit}", f"£{t.total:,.2f}", o.description,
                    f"{o.quantity:g} {o.unit}", f"£{o.total:,.2f}", cls="match") for t, o in r.matched]
        rows += [row(t.description, f"{t.quantity:g} {t.unit}", f"£{t.total:,.2f}", "— missed —", "", "", cls="miss") for t in r.missed]
        rows += [row("— not in report —", "", "", o.description, f"{o.quantity:g} {o.unit}", f"£{o.total:,.2f}", cls="extra") for o in r.extra]
        body.append(f"<h2>{e(r.name)}</h2><table><tr><th>Report line</th><th>Qty</th><th>£</th>"
                    f"<th>ClaimScope line</th><th>Qty</th><th>£</th></tr>{''.join(rows)}</table>")
    s = summary
    return f"""<!doctype html><meta charset=utf-8><title>{e(title)}</title>
<style>body{{font:14px system-ui;margin:24px;max-width:1100px}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left}}.miss{{background:#fde8e6}}
.extra{{background:#fff4dd}}.match{{background:#eaf6ee}}</style>
<h1>{e(title)}</h1>
<p>Report lines {s['report_lines']} · Draft lines {s['draft_lines']} · Matched {s['matched']} ·
Recall {s['recall']:.0%} · Precision {s['precision']:.0%} ·
Report £{s['report_total']:,.2f} ({e(s['report_total_source'])}) vs draft £{s['draft_total']:,.2f} (draft lines, net)</p>
<p>Matching is by room name and a coarse work key (element + operation). Check pairings by eye.
Draft rates are placeholders, so compare scope and quantities before £.</p>
{''.join(body)}"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", required=True, type=Path, help="original schedule (.pdf or .csv)")
    ap.add_argument("--draft", required=True, type=Path, help="ClaimScope schedule JSON downloaded from the demo")
    ap.add_argument("--out", type=Path, default=Path("local"))
    args = ap.parse_args()

    doc = parse_csv(args.report) if args.report.suffix.lower() == ".csv" else parse_pdf(args.report)
    if not doc.lines:
        raise SystemExit("No line items found in the report. Try the CSV route (see --help).")
    report = doc.lines
    draft = json.loads(args.draft.read_text())
    draft_rooms = {g["name"]: g.get("derived", {}) for g in draft["rooms"]}
    results, summary = compare(doc, draft_lines(draft), draft_rooms)

    args.out.mkdir(exist_ok=True)
    stem = f"compare-{draft.get('assessment_id', 'draft')}"
    (args.out / f"{stem}.html").write_text(render_html(results, summary, f"Comparison {draft.get('assessment_id', '')}"))
    with (args.out / f"{stem}-report-lines.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["room", "description", "unit", "quantity", "total"])
        w.writerows([l.room, l.description, l.unit, l.quantity, l.total] for l in report)

    print(f"Parsed {summary['report_lines']} report lines across {len({l.room for l in report})} groups.")
    print(f"Matched {summary['matched']} · recall {summary['recall']:.0%} · precision {summary['precision']:.0%}")
    print(f"Wrote {args.out / (stem + '.html')} and the parsed report lines CSV (check the parse there).")


if __name__ == "__main__":
    main()
