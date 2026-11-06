"""Deterministic pricing engine. Every pound on a schedule comes from here, never from the model."""

from decimal import ROUND_HALF_UP, Decimal

from .geometry import BASES

PENNY = Decimal("0.01")
VAT_RATE = Decimal("0.20")


def _money(value: Decimal) -> Decimal:
    return value.quantize(PENNY, ROUND_HALF_UP)


def quantity_for(basis: str, derived: dict, manual_quantity=None) -> Decimal:
    if basis in BASES:
        return Decimal(str(derived[basis]))
    if basis in ("count", "hours", "manual"):
        if manual_quantity is None:
            raise ValueError(f"basis '{basis}' needs a manual_quantity")
        return Decimal(str(manual_quantity))
    raise ValueError(f"unknown quantity basis '{basis}'")


def price_line(item: dict, basis: str, derived: dict, trade_rates: dict, manual_quantity=None) -> dict:
    """Price one line from a catalogue item. trade_rates maps trade -> GBP per hour."""
    quantity = quantity_for(basis, derived, manual_quantity)
    if quantity < 0:
        raise ValueError("quantity must not be negative")
    if item.get("per_room") and quantity != quantity.to_integral_value():
        raise ValueError(f"{item['code']} is priced per room; quantity {quantity} must be a whole number")

    trade = item["trade"]
    hourly = Decimal(0) if trade == "none" else Decimal(str(trade_rates[trade]))
    labour_per_unit = Decimal(str(item["labour_minutes"])) / 60 * hourly

    material_cost = _money(quantity * Decimal(str(item["material_rate"])))
    labour_cost = _money(quantity * labour_per_unit)
    return {
        "item_code": item["code"],
        "category": item.get("category"),
        "description": item["description"],
        "unit": item["unit"],
        "quantity_basis": basis,
        "quantity": quantity,
        "material_cost": material_cost,
        "labour_cost": labour_cost,
        "total": material_cost + labour_cost,
    }


def totals(lines: list[dict]) -> dict:
    materials = sum((l["material_cost"] for l in lines), Decimal(0))
    labour = sum((l["labour_cost"] for l in lines), Decimal(0))
    net = materials + labour
    vat = _money(net * VAT_RATE)
    return {"net": net, "vat": vat, "gross": net + vat, "materials": materials, "labour": labour}
