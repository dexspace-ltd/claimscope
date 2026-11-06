"""QA rules that need only the priced schedule (see docs/QA_RULES.md). Geometry/evidence rules come later."""

from collections import Counter


def _ref(room: dict, line: dict) -> str:
    return f"{room['name']} #{line['line_no']}"


def run_rules(groups: list[dict], catalogue: dict) -> list[dict]:
    findings = []

    def add(rule_id, severity, message, refs):
        findings.append({"rule_id": rule_id, "severity": severity, "message": message, "line_refs": refs, "resolved": False})

    for room in groups:
        lines = room["lines"]
        derived = room.get("derived") or {}
        codes = {l["item_code"] for l in lines}

        for line in lines:
            # R01: area item priced at the skirting length, or a linear item at the floor area.
            if line["unit"] == "M2" and derived and line["quantity"] == derived.get("perimeter") != derived.get("floor_area"):
                add("R01", "error", f"{line['item_code']} is per m2 but its quantity equals the room perimeter", [_ref(room, line)])
            if line["unit"] == "M" and derived and line["quantity"] == derived.get("floor_area") != derived.get("perimeter"):
                add("R01", "error", f"{line['item_code']} is per metre but its quantity equals the floor area", [_ref(room, line)])

        # R03: duplicate item code within a room.
        for code, n in Counter(l["item_code"] for l in lines).items():
            if n > 1:
                refs = [_ref(room, l) for l in lines if l["item_code"] == code]
                add("R03", "error", f"{code} appears {n} times in {room['name']}", refs)

        # R08-R10: an item without the items that normally follow it (take up -> supply + fit, stain block -> paint).
        for line in lines:
            missing = [c for c in catalogue.get(line["item_code"], {}).get("pairs_with", []) if c not in codes]
            if missing:
                add("R08", "warn", f"{line['item_code']} in {room['name']} is usually followed by {', '.join(missing)}", [_ref(room, line)])

    # R06: a floor covering replaced in some rooms but not others where the same covering was seen.
    floor_codes = {"vinyl": "FLR-001", "laminate": "FLR-010", "carpet": "FLR-020", "tile": "FLR-030"}
    for material, take_up in floor_codes.items():
        replaced = [g for g in groups if any(l["item_code"] == take_up for l in g["lines"])]
        if not replaced:
            continue
        missing = [g["name"] for g in groups if g not in replaced
                   and any(material in f and "floor" in f for f in g.get("finishes", []))]
        if missing:
            add("R06", "error", f"{material.title()} floor replaced in {len(replaced)} room(s) but not in "
                f"{', '.join(missing)}, where the same floor was seen. Check whether it is continuous.", [])

    return findings
