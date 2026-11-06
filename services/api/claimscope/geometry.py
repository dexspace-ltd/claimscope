"""Derived room quantities. The model picks a quantity basis; these numbers fill it in."""

from decimal import ROUND_HALF_UP, Decimal

TWO_DP = Decimal("0.01")

BASES = ("floor_area", "ceiling_area", "wall_area", "perimeter")


def _d(value) -> Decimal:
    return Decimal(str(value))


def derive(dimensions: dict) -> dict:
    """Floor, ceiling and wall area (m2) and skirting perimeter (m) for a rectangular room.

    wall_area subtracts every opening; perimeter subtracts door widths only, since
    skirting runs under windows but not across doorways.
    """
    length = _d(dimensions["length_m"])
    breadth = _d(dimensions["breadth_m"])
    height = _d(dimensions["height_m"])
    if min(length, breadth, height) <= 0:
        raise ValueError("room dimensions must be positive")

    openings = dimensions.get("openings") or []
    opening_area = sum((_d(o["width_m"]) * _d(o["height_m"]) for o in openings), Decimal(0))
    door_width = sum((_d(o["width_m"]) for o in openings if o["kind"] == "door"), Decimal(0))

    room_perimeter = 2 * (length + breadth)
    floor = length * breadth
    derived = {
        "floor_area": floor,
        "ceiling_area": floor,
        "wall_area": max(room_perimeter * height - opening_area, Decimal(0)),
        "perimeter": max(room_perimeter - door_width, Decimal(0)),
    }
    return {k: v.quantize(TWO_DP, ROUND_HALF_UP) for k, v in derived.items()}
