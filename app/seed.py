"""Seed a sample catalogue + stock so the trolley works out of the box.

Run automatically on first start (only if the products table is empty).
Barcodes below are real-format EAN-13 style strings you can print/scan, but you
can also just type them in simulation mode. Weights are the per-unit expected
weight used by the anti-theft check.
"""
from __future__ import annotations

from app.database import SessionLocal
from app.models import Product

# barcode, name, category, price(INR), weight_g, is_produce, price_per_kg, stock
_SAMPLE = [
    ("8901058000313", "Maggi Noodles 70g", "Grocery", 14.0, 85.0, False, 0, 120),
    ("8901491101837", "Lay's Classic 52g", "Snacks", 20.0, 60.0, False, 0, 90),
    ("8901063092013", "Britannia Marie 120g", "Biscuits", 30.0, 135.0, False, 0, 75),
    ("8901030865278", "Dove Soap 100g", "Personal Care", 65.0, 110.0, False, 0, 60),
    ("8901138511081", "Colgate Paste 100g", "Personal Care", 55.0, 145.0, False, 0, 80),
    ("8904004400229", "Amul Butter 100g", "Dairy", 62.0, 105.0, False, 0, 50),
    ("8901725100018", "Tata Salt 1kg", "Grocery", 28.0, 1010.0, False, 0, 100),
    ("8901719101090", "Parle-G 250g", "Biscuits", 25.0, 260.0, False, 0, 110),
    ("8902080000015", "Coca-Cola 750ml", "Beverages", 40.0, 810.0, False, 0, 70),
    ("8901207012345", "Aashirvaad Atta 5kg", "Grocery", 275.0, 5050.0, False, 0, 40),
    # Loose produce (priced by weight; scanned via on-screen selection)
    ("PRODUCE-APPLE", "Apples (loose)", "Produce", 0, 0, True, 180.0, 0),
    ("PRODUCE-BANANA", "Bananas (loose)", "Produce", 0, 0, True, 60.0, 0),
    ("PRODUCE-TOMATO", "Tomatoes (loose)", "Produce", 0, 0, True, 40.0, 0),
    ("PRODUCE-ONION", "Onions (loose)", "Produce", 0, 0, True, 35.0, 0),
]


def seed_if_empty() -> None:
    db = SessionLocal()
    try:
        if db.query(Product).count() > 0:
            return
        for (bc, name, cat, price, wg, prod, ppk, stock) in _SAMPLE:
            db.add(
                Product(
                    barcode=bc,
                    name=name,
                    category=cat,
                    price=price,
                    weight_g=wg,
                    is_produce=prod,
                    price_per_kg=ppk,
                    stock_qty=stock,
                )
            )
        db.commit()
    finally:
        db.close()
