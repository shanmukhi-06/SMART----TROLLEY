"""Cart logic: add scanned/produce items, remove, and compute totals.

Kept independent of FastAPI and of the hardware so it is easy to unit-test and
to lift into an AWS service later.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CartItem, Product, ShoppingSession
from app.services import catalog

TAX_RATE = Decimal("0.05")  # 5% GST on the subtotal (adjust per store)


def _money(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def get_items(db: Session, session_id: int) -> list[CartItem]:
    return list(
        db.scalars(
            select(CartItem)
            .where(CartItem.session_id == session_id)
            .order_by(CartItem.created_at)
        )
    )


def add_scanned_item(db: Session, session: ShoppingSession, barcode: str):
    """Add a unit-priced product by barcode. Returns (cart_item, product, error)."""
    product = catalog.find_by_barcode(db, barcode)
    if product is None:
        return None, None, "unknown_barcode"
    if product.is_produce:
        # produce is added via the weigh flow, not a plain scan
        return None, product, "is_produce"
    if product.stock_qty <= 0:
        return None, product, "out_of_stock"

    item = db.scalar(
        select(CartItem).where(
            CartItem.session_id == session.id, CartItem.product_id == product.id
        )
    )
    if item is None:
        item = CartItem(
            session_id=session.id,
            product_id=product.id,
            name=product.name,
            unit_price=product.price,
            qty=1,
            verify_status="pending",
        )
        db.add(item)
    else:
        item.qty += 1
    item.line_total = _money(Decimal(str(item.unit_price)) * item.qty)
    db.commit()
    db.refresh(item)
    return item, product, None


def add_produce_item(db: Session, session: ShoppingSession, product_id: int, weighed_g: float):
    """Add a loose-produce line priced by weight. Returns (cart_item, product, error)."""
    product = catalog.get_product(db, product_id)
    if product is None or not product.is_produce:
        return None, None, "invalid_produce"
    if weighed_g <= 0:
        return None, product, "no_weight"

    price = Decimal(str(product.price_per_kg)) * (Decimal(str(weighed_g)) / Decimal("1000"))
    item = CartItem(
        session_id=session.id,
        product_id=product.id,
        name=f"{product.name} ({weighed_g:.0f} g)",
        unit_price=_money(price),
        qty=1,
        weighed_g=weighed_g,
        line_total=_money(price),
        verify_status="verified",  # produce is weighed directly, so it is trusted
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item, product, None


def set_verify_status(db: Session, item_id: int, status: str) -> None:
    item = db.get(CartItem, item_id)
    if item:
        item.verify_status = status
        db.commit()


def remove_item(db: Session, session_id: int, item_id: int) -> bool:
    item = db.get(CartItem, item_id)
    if not item or item.session_id != session_id:
        return False
    if item.qty > 1 and item.weighed_g == 0:
        item.qty -= 1
        item.line_total = _money(Decimal(str(item.unit_price)) * item.qty)
        item.verify_status = "verified"
    else:
        db.delete(item)
    db.commit()
    return True


def expected_total_grams(db: Session, session_id: int) -> float:
    """Sum of expected weights of everything that should be in the basket."""
    total = 0.0
    for item in get_items(db, session_id):
        if item.weighed_g > 0:
            total += item.weighed_g
        else:
            total += (item.product.weight_g or 0.0) * item.qty
    return round(total, 1)


def summarise(db: Session, session_id: int) -> dict:
    items = get_items(db, session_id)
    subtotal = sum(Decimal(str(i.line_total)) for i in items) if items else Decimal("0")
    tax = (subtotal * TAX_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total = subtotal + tax
    return {
        "items": [
            {
                "id": i.id,
                "name": i.name,
                "unit_price": float(i.unit_price),
                "qty": i.qty,
                "weighed_g": i.weighed_g,
                "line_total": float(i.line_total),
                "verify_status": i.verify_status,
            }
            for i in items
        ],
        "count": sum(i.qty for i in items),
        "subtotal": _money(subtotal),
        "tax": _money(tax),
        "total": _money(total),
        "has_mismatch": any(i.verify_status == "mismatch" for i in items),
    }
