from decimal import Decimal

import pytest

from claimscope.pricing import price_line, totals

DERIVED = {"floor_area": Decimal("29.42"), "ceiling_area": Decimal("29.42"),
           "wall_area": Decimal("50.10"), "perimeter": Decimal("21.94")}
RATES = {"floorer": 30, "decorator": 28}

VINYL = {"code": "FLR-012", "category": "Vinyl", "description": "Vinyl sheeting - supply and fit",
         "unit": "M2", "operation": "supply_and_fit", "material_rate": 12.50,
         "labour_minutes": 20, "trade": "floorer"}
SEALANT = {"code": "DEC-040", "category": "Anti-fungal", "description": "Anti-fungal treatment, standard room",
           "unit": "EACH", "operation": "apply", "material_rate": 45, "labour_minutes": 90,
           "trade": "decorator", "per_room": True}


def test_area_item_priced_from_floor_area():
    line = price_line(VINYL, "floor_area", DERIVED, RATES)
    assert line["quantity"] == Decimal("29.42")
    assert line["material_cost"] == Decimal("367.75")
    assert line["labour_cost"] == Decimal("294.20")  # 29.42 x 20 min x £30/h
    assert line["total"] == Decimal("661.95")


def test_per_room_item_rejects_fractional_quantity():
    # R02: "standard size room" x 9.24 in the source schedule.
    with pytest.raises(ValueError, match="per room"):
        price_line(SEALANT, "manual", DERIVED, RATES, manual_quantity=9.24)
    assert price_line(SEALANT, "count", DERIVED, RATES, manual_quantity=1)["total"] == Decimal("87.00")


def test_manual_basis_needs_quantity():
    with pytest.raises(ValueError):
        price_line(VINYL, "manual", DERIVED, RATES)


def test_totals_add_vat():
    lines = [price_line(VINYL, "floor_area", DERIVED, RATES)]
    t = totals(lines)
    assert t["net"] == Decimal("661.95")
    assert t["vat"] == Decimal("132.39")
    assert t["gross"] == Decimal("794.34")
