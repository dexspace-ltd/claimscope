from decimal import Decimal

from claimscope import ai
from claimscope.geometry import derive
from claimscope.pipeline import price_and_check

DIMS = {"length_m": 5, "breadth_m": 4, "height_m": 2.4, "openings": [{"kind": "door", "width_m": 0.8, "height_m": 2.0}]}
ROOMS = {"r1": {"room_id": "r1", "name": "Room 1", "name_is_placeholder": True, "dimensions": DIMS, "derived": derive(DIMS)}}


def line(code, basis, qty=None, conf=0.9):
    return ai.ProposedLine(item_code=code, quantity_basis=basis, manual_quantity=qty, rationale="",
                           evidence_refs=["e1"], confidence=conf, flags=[])


def test_prices_model_draft_and_adds_prelims():
    proposed = {"r1": ai.RoomSchedule(room_name="Lounge", site_checks=[], lines=[
        line("FLR-001", "floor_area"), line("FLR-002", "floor_area"), line("FLR-003", "floor_area"),
        line("DEC-003", "ceiling_area"), line("DEC-002", "ceiling_area"),
    ])}
    out = price_and_check(proposed, ROOMS)
    room = out["rooms"][0]
    assert room["name"] == "Lounge"  # placeholder scan name replaced by the model's
    assert [l["quantity"] for l in room["lines"]] == [Decimal("20.00")] * 5
    codes = {l["item_code"] for l in out["general_items"]["lines"]}
    # strip-out of 20 m2 vinyl: skip + permit + sacks + labourer, dust extraction, electrical test
    assert {"PRL-004", "PRL-001", "PRL-006", "PRL-002", "PRL-008", "PRL-007", "PRL-012", "PRL-010", "ELC-001", "PRL-003"} <= codes
    assert "PRL-005" not in codes  # every room has works
    assert out["qa_findings"] == []
    assert out["totals"]["gross"] == out["totals"]["net"] + out["totals"]["vat"] > 0


def test_flags_unknown_items_broken_pairs_and_bad_quantities():
    proposed = {"r1": ai.RoomSchedule(room_name="", site_checks=[], lines=[
        line("XXX-999", "count", 1),
        line("FLR-001", "floor_area"),                # take up with no supply/fit
        line("DEC-005", "manual", 9.24, conf=0.3),    # per-room item with a fractional quantity
        line("PLS-001", "manual"),                    # manual basis, no quantity
    ])}
    out = price_and_check(proposed, ROOMS)
    rules = sorted(f["rule_id"] for f in out["qa_findings"])
    assert rules == ["R00", "R02", "R02", "R08", "R08"]  # FLR-001 and PLS-001 both unpaired
    lines = {l["item_code"]: l for l in out["rooms"][0]["lines"]}
    assert lines["DEC-005"]["quantity"] == 0 and "low_confidence" in lines["DEC-005"]["flags"]
    assert out["rooms"][0]["name"] == "Room 1"


def findings(match="unknown", kind="kitchen", size=None, finishes=()):
    from claimscope.pipeline import canonical_room_type

    return ai.ImageFindings(room_type_guess=canonical_room_type(kind), room_match=match, finishes=list(finishes), observations=[],
                            room_size_estimate=ai.RoomSize(length_m=size[0], breadth_m=size[1]) if size else None)


def test_assign_rooms_votes_per_scan_point_and_creates_estimated_rooms(monkeypatch):
    from claimscope import pipeline, store

    put, updates = [], []
    monkeypatch.setattr(store, "put", put.append)
    monkeypatch.setattr(store, "table", lambda: type("T", (), {"update_item": lambda self, **kw: updates.append(kw)})())

    rooms = {"r1": {**ROOMS["r1"], "name": "Lounge"}}
    results = {
        # scan point 1: two of three views say Lounge
        "a": {"room_id": None, "scan_point": "ss1", "findings": findings("Lounge")},
        "b": {"room_id": None, "scan_point": "ss1", "findings": findings("lounge ")},
        "c": {"room_id": None, "scan_point": "ss1", "findings": findings("unknown")},
        # scan point 2: no known room -> new estimated kitchen
        "d": {"room_id": None, "scan_point": "ss2", "findings": findings(kind="Kitchen", size=(3.1, 4.2))},
        "e": {"room_id": None, "scan_point": "ss2", "findings": findings(kind="kitchen", size=(4.0, 3.0))},
        "f": {"room_id": None, "scan_point": "ss3", "error": "boom"},
    }
    pipeline.assign_rooms("0123456789ab", rooms, results)

    assert {results[k]["room_id"] for k in "abc"} == {"r1"}
    kitchen_id = results["d"]["room_id"]
    assert results["e"]["room_id"] == kitchen_id != "r1"
    assert results["f"]["room_id"] is None
    kitchen = rooms[kitchen_id]
    assert kitchen["name"] == "Kitchen" and kitchen["dimensions"]["source"] == "estimated"
    assert (kitchen["dimensions"]["length_m"], kitchen["dimensions"]["breadth_m"]) == (Decimal("4.1"), Decimal("3.05"))
    assert len(updates) == 5 and len(put) == 1


def test_key_from_secret_accepts_plaintext_and_key_value():
    assert ai._key_from_secret("  sk-ant-x \n") == "sk-ant-x"
    assert ai._key_from_secret('{"ANTHROPIC_KEY": "sk-ant-x"}') == "sk-ant-x"
    assert ai._key_from_secret('"sk-ant-x"') == "sk-ant-x"


def test_capture_keys_and_unknown_guesses(monkeypatch):
    from claimscope import pipeline, store

    assert pipeline._parse_capture_key("captures/0123456789ab/scan_view/abcdef012345") == ("0123456789ab", "scan_view", "abcdef012345")
    assert pipeline._parse_capture_key("assessments/x/schedule/draft.json") is None

    put = []
    monkeypatch.setattr(store, "put", put.append)
    monkeypatch.setattr(store, "table", lambda: type("T", (), {"update_item": lambda self, **kw: None})())
    rooms = {"r1": {**ROOMS["r1"], "name": "Kitchen / Diner"}}
    results = {
        "a": {"room_id": None, "scan_point": "ss1", "findings": findings("unknown", kind="kitchen")},  # guess matches a known name
        "b": {"room_id": None, "scan_point": "ss2", "findings": findings("unknown", kind="close-up of a wall")},  # nothing useful
    }
    pipeline.assign_rooms("0123456789ab", rooms, results)
    assert results["a"]["room_id"] == "r1"
    assert put[0]["name"] == "Unidentified Area"


def test_room_types_are_canonical():
    from claimscope.pipeline import canonical_room_type

    assert canonical_room_type("Kitchen (Open-Plan, Inferred From Tall Units)") == "open-plan kitchen/living"
    assert canonical_room_type("Cloakroom / WC or small bathroom") == "wc"
    assert canonical_room_type("Shower Room") == "shower room"
    assert canonical_room_type("Unknown (possibly hallway)") == "hallway"
    assert canonical_room_type("close-up of wall") == "unclear"


def test_general_items_scale_with_the_job():
    from claimscope.pipeline import catalogue, general_items, trade_rates

    by_code = {i["code"]: i for i in catalogue()["items"]}
    rates = trade_rates()["per_hour"]
    big = {"name": "Lounge", "derived": {"floor_area": Decimal("70")}, "lines": [
        {"item_code": "FLR-001", "quantity": Decimal("70")}, {"item_code": "FLR-044", "quantity": Decimal("70")},
        {"item_code": "PLS-002", "quantity": Decimal("40")}]}
    idle = {"name": "Hall", "derived": {"floor_area": Decimal("5")}, "lines": []}
    lines = {l["item_code"]: l for l in general_items([big, idle], by_code, rates)["lines"]}
    assert lines["PRL-002"]["quantity"] == 3  # 70 m2 stripped -> 3 skips
    assert lines["PRL-005"]["quantity"] == 1  # the hall is protected, not worked
    assert "PRL-009" in lines and "PRL-011" in lines  # several days on site: welfare and extra dust extraction days


def test_r06_flags_continuous_floor_gaps():
    from claimscope.qa import run_rules

    rooms = [
        {"name": "Lounge", "finishes": ["vinyl sheet floor"], "lines": [{"line_no": 1, "item_code": "FLR-001", "unit": "M2", "quantity": 20}]},
        {"name": "Bedroom", "finishes": ["vinyl sheet floor", "emulsion walls"], "lines": []},
    ]
    found = [f for f in run_rules(rooms, {}) if f["rule_id"] == "R06"]
    assert len(found) == 1 and "Bedroom" in found[0]["message"]
