from decimal import Decimal

from claimscope.qa import run_rules

DERIVED = {"floor_area": Decimal("29.42"), "perimeter": Decimal("17.60")}


def test_r01_area_item_at_perimeter_and_r03_duplicates():
    room = {"name": "Lounge", "derived": DERIVED, "lines": [
        {"line_no": 1, "item_code": "FLR-002", "unit": "M2", "quantity": Decimal("17.60")},
        {"line_no": 2, "item_code": "BTH-001", "unit": "EACH", "quantity": Decimal("1")},
        {"line_no": 3, "item_code": "BTH-001", "unit": "EACH", "quantity": Decimal("1")},
    ]}
    rules = sorted(f["rule_id"] for f in run_rules([room], {}))
    assert rules == ["R01", "R03"]
