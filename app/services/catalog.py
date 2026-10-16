"""Product catalogue: lookups plus the write operations behind the staff
"register a product" screen.

Registration flow this supports
------------------------------
1. Staff scans a barcode in the settings menu -> `find_by_barcode_any()`.
2. If nothing comes back it is a new product: they fill in name/price, tap
   "weigh on scale" to capture the unit weight, and `upsert_product()` saves it.
3. If it already exists the same call updates it, so a re-scan is an edit.

`weight_g` is the important field for the anti-theft check: it is the expected
weight of one unit, so capturing it from the real load cell at registration
time is far more accurate than typing the value off the packet.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Product, StockMovement

# Barcodes we generate for loose produce (no printed barcode to scan)
PRODUCE_PREFIX = "PRODUCE-"


class CatalogError(ValueError):
    """Invalid product data supplied by the staff UI."""


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def find_by_barcode(db: Session, barcode: str) -> Product | None:
    """Active products only — used by the shopper scan path."""
    barcode = (barcode or "").strip()
    if not barcode:
        return None
    return db.scalar(select(Product).where(Product.barcode == barcode, Product.active))


def find_by_barcode_any(db: Session, barcode: str) -> Product | None:
    """Including de-listed products — used by the staff lookup/registration."""
    barcode = (barcode or "").strip()
    if not barcode:
        return None
    return db.scalar(select(Product).where(Product.barcode == barcode))


def get_product(db: Session, product_id: int) -> Product | None:
    return db.get(Product, product_id)


def list_produce(db: Session) -> list[Product]:
    return list(
        db.scalars(
            select(Product).where(Product.is_produce, Product.active).order_by(Product.name)
        )
    )


def search(db: Session, query: str = "", limit: int = 50,
           include_inactive: bool = True) -> list[Product]:
    """Name / barcode / category search for the staff product list."""
    stmt = select(Product)
    query = (query or "").strip()
    if query:
        like = f"%{query.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Product.name).like(like),
                func.lower(Product.barcode).like(like),
                func.lower(Product.category).like(like),
            )
        )
    if not include_inactive:
        stmt = stmt.where(Product.active)
    stmt = stmt.order_by(Product.name).limit(max(1, min(limit, 200)))
    return list(db.scalars(stmt))


def counts(db: Session) -> dict:
    total = db.scalar(select(func.count(Product.id))) or 0
    active = db.scalar(select(func.count(Product.id)).where(Product.active)) or 0
    produce = (
        db.scalar(select(func.count(Product.id)).where(Product.is_produce, Product.active))
        or 0
    )
    no_weight = (
        db.scalar(
            select(func.count(Product.id)).where(
                Product.active, ~Product.is_produce, Product.weight_g <= 0
            )
        )
        or 0
    )
    return {
        "total": total,
        "active": active,
        "inactive": total - active,
        "produce": produce,
        # packaged products with no expected weight can't be weight-verified
        "missing_weight": no_weight,
    }


def product_to_dict(p: Product) -> dict:
    return {
        "id": p.id,
        "barcode": p.barcode,
        "name": p.name,
        "category": p.category,
        "price": float(p.price),
        "weight_g": p.weight_g,
        "is_produce": p.is_produce,
        "price_per_kg": float(p.price_per_kg),
        "stock_qty": p.stock_qty,
        "image": p.image,
        "active": p.active,
        # surfaced in the staff UI: these products bypass the weight check
        "weight_verifiable": bool(p.is_produce or (p.weight_g or 0) > 0),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _as_decimal(value, field: str, default: str = "0") -> Decimal:
    if value is None or value == "":
        value = default
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise CatalogError(f"{field} must be a number")
    if dec < 0:
        raise CatalogError(f"{field} cannot be negative")
    return dec


def _as_float(value, field: str, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise CatalogError(f"{field} must be a number")
    if num < 0:
        raise CatalogError(f"{field} cannot be negative")
    return num


def _as_int(value, field: str, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        raise CatalogError(f"{field} must be a whole number")
    if num < 0:
        raise CatalogError(f"{field} cannot be negative")
    return num


def normalise_barcode(raw: str) -> str:
    """Trim scanner noise. Barcode guns often append CR/LF or stray spaces."""
    return (raw or "").strip().replace("\r", "").replace("\n", "")[:64]


def validate_payload(data: dict) -> dict:
    """Turn raw JSON from the staff form into clean, typed values."""
    barcode = normalise_barcode(data.get("barcode", ""))
    name = (data.get("name") or "").strip()[:200]
    is_produce = bool(data.get("is_produce"))

    if not name:
        raise CatalogError("Product name is required")
    if not barcode:
        # Loose produce has no printed barcode, so generate a stable one.
        if is_produce:
            slug = "".join(ch for ch in name.upper() if ch.isalnum())[:20] or "ITEM"
            barcode = f"{PRODUCE_PREFIX}{slug}"
        else:
            raise CatalogError("Scan or type a barcode")

    clean = {
        "barcode": barcode,
        "name": name,
        "category": ((data.get("category") or "").strip() or "General")[:80],
        "is_produce": is_produce,
        "image": (data.get("image") or "").strip()[:255],
        "active": bool(data.get("active", True)),
    }

    if is_produce:
        clean["price_per_kg"] = _as_decimal(data.get("price_per_kg"), "Price per kg")
        if clean["price_per_kg"] <= 0:
            raise CatalogError("Loose produce needs a price per kg")
        clean["price"] = Decimal("0")
        clean["weight_g"] = 0.0
        clean["stock_qty"] = 0
    else:
        clean["price"] = _as_decimal(data.get("price"), "Price")
        if clean["price"] <= 0:
            raise CatalogError("Price must be greater than zero")
        clean["price_per_kg"] = Decimal("0")
        clean["weight_g"] = _as_float(data.get("weight_g"), "Unit weight")
        clean["stock_qty"] = _as_int(data.get("stock_qty"), "Stock quantity")

    return clean


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def upsert_product(db: Session, data: dict) -> tuple[Product, bool]:
    """Create or update a product from the staff form.

    Returns `(product, created)`. Matching is by `id` when supplied, otherwise
    by barcode — so re-scanning a known barcode edits it instead of failing on
    the unique constraint.
    """
    clean = validate_payload(data)

    product = None
    product_id = data.get("id")
    if product_id:
        product = db.get(Product, int(product_id))
        if product is None:
            raise CatalogError("Product no longer exists")
    if product is None:
        product = find_by_barcode_any(db, clean["barcode"])

    created = product is None
    if created:
        product = Product(**clean)
        db.add(product)
    else:
        # barcode collision with a *different* product id
        clash = find_by_barcode_any(db, clean["barcode"])
        if clash and clash.id != product.id:
            raise CatalogError(
                f"Barcode {clean['barcode']} already belongs to {clash.name}"
            )
        for field, value in clean.items():
            setattr(product, field, value)

    db.commit()
    db.refresh(product)

    if created and product.stock_qty:
        _record_movement(db, product, product.stock_qty, "initial_stock",
                         note="registered via settings menu")
    return product, created


def set_stock(db: Session, product_id: int, new_qty: int, note: str = "") -> Product:
    """Absolute stock set, with an audit trail of the delta."""
    product = db.get(Product, product_id)
    if product is None:
        raise CatalogError("Product not found")
    new_qty = _as_int(new_qty, "Stock quantity")
    delta = new_qty - (product.stock_qty or 0)
    product.stock_qty = new_qty
    db.commit()
    db.refresh(product)
    if delta:
        _record_movement(db, product, delta, "stock_adjustment", note=note)
    return product


def add_stock(db: Session, product_id: int, delta: int, note: str = "") -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise CatalogError("Product not found")
    product.stock_qty = max(0, (product.stock_qty or 0) + int(delta))
    db.commit()
    db.refresh(product)
    _record_movement(db, product, int(delta), "stock_adjustment", note=note)
    return product


def set_active(db: Session, product_id: int, active: bool) -> Product:
    """De-list / re-list rather than delete, so order history stays intact."""
    product = db.get(Product, product_id)
    if product is None:
        raise CatalogError("Product not found")
    product.active = bool(active)
    db.commit()
    db.refresh(product)
    return product


def set_unit_weight(db: Session, product_id: int, weight_g: float) -> Product:
    """Store a weight captured live from the load cell."""
    product = db.get(Product, product_id)
    if product is None:
        raise CatalogError("Product not found")
    product.weight_g = _as_float(weight_g, "Unit weight")
    db.commit()
    db.refresh(product)
    return product


def _record_movement(db: Session, product: Product, change: int, reason: str,
                     note: str = "") -> None:
    db.add(
        StockMovement(
            product_id=product.id, change=change, reason=reason, note=note or ""
        )
    )
    db.commit()


def recent_movements(db: Session, product_id: int, limit: int = 10) -> list[dict]:
    rows = db.scalars(
        select(StockMovement)
        .where(StockMovement.product_id == product_id)
        .order_by(StockMovement.created_at.desc())
        .limit(limit)
    )
    return [
        {
            "change": m.change,
            "reason": m.reason,
            "order_ref": m.order_ref,
            "note": m.note,
            "created_at": m.created_at.isoformat(timespec="seconds"),
        }
        for m in rows
    ]
