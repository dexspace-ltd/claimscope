from decimal import Decimal

import pytest

from claimscope.geometry import derive


def test_rectangular_room_with_openings():
    derived = derive({
        "length_m": 5, "breadth_m": 4, "height_m": 2.4,
        "openings": [
            {"kind": "door", "width_m": 0.8, "height_m": 2.0},
            {"kind": "window", "width_m": 1.2, "height_m": 1.0},
        ],
    })
    assert derived["floor_area"] == Decimal("20.00")
    assert derived["ceiling_area"] == Decimal("20.00")
    # 18 m perimeter x 2.4 m = 43.2, minus 1.6 door and 1.2 window
    assert derived["wall_area"] == Decimal("40.40")
    # skirting skips the doorway but runs under the window
    assert derived["perimeter"] == Decimal("17.20")


def test_floor_area_and_perimeter_differ():
    # R01: the source schedule priced flooring at the skirting length. They must not coincide here.
    derived = derive({"length_m": 6.3, "breadth_m": 4.67, "height_m": 2.4})
    assert derived["floor_area"] == Decimal("29.42")
    assert derived["perimeter"] == Decimal("21.94")


@pytest.mark.parametrize("bad", [0, -1])
def test_rejects_non_positive_dimensions(bad):
    with pytest.raises(ValueError):
        derive({"length_m": bad, "breadth_m": 3, "height_m": 2.4})
