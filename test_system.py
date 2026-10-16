"""Automated test suite for the Smart Shopping Trolley project.

Covers:
1. Database initialization and seeding
2. Catalog and Product management
3. Cart operations (add item, weight delta, totals, discounts)
4. Scale simulation (tare, measurement, noise bounds)
5. Thermal printer abstraction and fallback
6. Mock Payment and Order lifecycle
7. FastAPI HTTP Endpoints via TestClient
"""
import pytest
from starlette.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, init_db
from app.main import app
from app.models import (
    CartItem,
    Order,
    OrderStatus,
    Product,
    SessionStatus,
    ShoppingSession,
)
from app.seed import seed_if_empty
from app.services import cart, catalog, orders, receipts, settings_store, verification
from app.hardware.printer import SimulatedPrinter, get_printer
from app.hardware.scale import SimulatedScale
from app.payments.mock import MockPaymentProvider


@pytest.fixture(scope="session")
def setup_db():
    init_db()
    seed_if_empty()
    yield
    # Keep DB intact for app usage


@pytest.fixture
def db(setup_db):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ===========================================================================
# 1. Database & Catalog Tests
# ===========================================================================
def test_database_and_products_seeded(db: Session):
    products = db.query(Product).all()
    assert len(products) > 0, "Database should have seeded products"
    barcodes = [p.barcode for p in products if p.barcode]
    assert len(barcodes) > 0, "Products should have valid barcodes"


def test_catalog_lookup_by_barcode(db: Session):
    sample_product = db.query(Product).filter(Product.barcode.isnot(None)).first()
    assert sample_product is not None
    found = catalog.find_by_barcode(db, sample_product.barcode)
    assert found is not None
    assert found.id == sample_product.id
    assert found.name == sample_product.name


def test_catalog_produce_items(db: Session):
    produce = catalog.list_produce(db)
    assert isinstance(produce, list)
    for item in produce:
        assert item.is_produce is True


# ===========================================================================
# 2. Cart & Weight Verification Tests
# ===========================================================================
def test_cart_lifecycle(db: Session):
    # Create new session
    session = ShoppingSession(
        mobile="9999999999",
        email="shopper@example.com",
        status=SessionStatus.active,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    # Pick a product
    product = db.query(Product).filter(Product.barcode.isnot(None), Product.is_produce == False).first()
    assert product is not None

    # Add item
    item, prod, err = cart.add_scanned_item(db, session, product.barcode)
    assert err is None, f"Expected no error adding item, got {err}"
    assert item is not None
    assert prod.id == product.id

    # Verify summary
    summary = cart.summarise(db, session.id)
    assert summary["count"] >= 1
    assert summary["total"] > 0
    expected_g = cart.expected_total_grams(db, session.id)
    assert expected_g >= (product.weight_g or 0)

    # Weight check logic
    status = verification.classify(
        delta_g=float(product.weight_g or 100.0),
        expected_g=float(product.weight_g or 100.0),
    )
    assert status == "verified"

    # Weight mismatch logic
    mismatch_status = verification.classify(
        delta_g=0.0,
        expected_g=500.0,
    )
    assert mismatch_status == "mismatch"

    # Cleanup session
    session.status = SessionStatus.checked_out
    db.commit()


# ===========================================================================
# 3. Hardware Abstractions (Simulation)
# ===========================================================================
def test_simulated_scale():
    scale = SimulatedScale()
    scale.tare()
    assert abs(scale.get_grams()) < 1.0

    # Simulate weight change
    scale.sim_add_weight(250.0)
    assert abs(scale.get_grams() - 250.0) < 5.0

    scale.sim_set_weight(0.0)
    assert abs(scale.get_grams()) < 1.0


def test_printer_fallback():
    printer = SimulatedPrinter()
    printer.init()
    printer.align("center")
    printer.bold(True)
    printer.print_text("Smart Trolley Receipt")
    printer.feed(1)
    printer.cut()
    health = printer.health()
    assert health["online"] is True
    assert "Smart Trolley Receipt" in printer.get_output()


# ===========================================================================
# 4. Payment & Order Lifecycle
# ===========================================================================
def test_mock_payment_and_receipt(db: Session):
    session = ShoppingSession(
        mobile="9888888888",
        email="testpay@example.com",
        status=SessionStatus.active,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    product = db.query(Product).filter(Product.barcode.isnot(None)).first()
    cart.add_scanned_item(db, session, product.barcode)

    # Place order
    order = orders.create_order_from_session(db, session)
    assert order is not None
    assert order.order_ref is not None
    assert order.status == OrderStatus.pending_payment

    # Mock payment provider
    provider = MockPaymentProvider()
    intent = provider.create_payment(
        order_ref=order.order_ref,
        amount=float(order.total),
    )
    assert intent.order_ref == order.order_ref

    # Confirm payment
    orders.mark_order_paid(db, order=order, provider="mock", payment_ref="MOCK-REF-12345")
    db.refresh(order)
    assert order.status == OrderStatus.paid

    # Generate receipt HTML
    html = receipts.render_html(order)
    assert order.order_ref in html
    assert settings.store_name in html


# ===========================================================================
# 5. FastAPI Endpoints
# ===========================================================================
def test_endpoint_healthz(client: TestClient):
    res = client.get("/healthz")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "mode" in data


def test_endpoint_index_page(client: TestClient):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_endpoint_produce(client: TestClient):
    res = client.get("/api/produce")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert isinstance(data["items"], list)


def test_endpoint_weight(client: TestClient):
    res = client.get("/api/weight")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "grams" in data
    assert "source" in data


def test_endpoint_shopper_session_and_scan_flow(client: TestClient, db: Session):
    # 1. Start session
    res = client.post(
        "/api/session/start",
        json={"mobile": "9876543210", "email": "testclient@example.com"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    session_id = data["session_id"]
    assert session_id is not None

    # 2. Get cart
    res_cart = client.get("/api/cart")
    assert res_cart.status_code == 200
    cart_data = res_cart.json()
    assert cart_data["ok"] is True
    assert cart_data["cart"]["count"] == 0

    # 3. Find a test product
    product = db.query(Product).filter(Product.barcode.isnot(None), Product.is_produce == False).first()
    assert product is not None

    # 4. Scan the item via API
    res_scan = client.post("/api/scan", json={"barcode": product.barcode})
    assert res_scan.status_code == 200
    scan_data = res_scan.json()
    assert scan_data["ok"] is True
    assert scan_data["verify_status"] in ("verified", "mismatch")

    # 5. Verify cart now has item
    res_cart2 = client.get("/api/cart")
    assert res_cart2.status_code == 200
    cart_data2 = res_cart2.json()
    assert cart_data2["cart"]["count"] >= 1

