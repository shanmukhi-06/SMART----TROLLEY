"""Checkout: turn a live cart into an immutable Order, then settle stock on pay.

Two phases:
  1. create_order_from_session() -> snapshot cart into Order/OrderItems
     (status = pending_payment). Called when the shopper taps Checkout.
  2. mark_order_paid() -> on payment success: decrement stock, write
     StockMovement audit rows, close the session, trigger the receipt.
"""
from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    CartItem,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    SessionStatus,
    ShoppingSession,
    StockMovement,
)
from app.services import cart


def _make_ref() -> str:
    stamp = datetime.utcnow().strftime("%y%m%d%H%M%S")
    return f"ST{stamp}{secrets.token_hex(2).upper()}"


def create_order_from_session(db: Session, session: ShoppingSession) -> Order | None:
    summary = cart.summarise(db, session.id)
    if summary["count"] == 0:
        return None

    # reuse an existing unpaid order for this session if one is open
    existing = (
        db.query(Order)
        .filter(Order.session_id == session.id, Order.status == OrderStatus.pending_payment)
        .first()
    )
    if existing:
        db.query(OrderItem).filter(OrderItem.order_id == existing.id).delete()
        order = existing
    else:
        order = Order(order_ref=_make_ref(), session_id=session.id)
        db.add(order)

    order.mobile = session.mobile
    order.email = session.email
    order.subtotal = summary["subtotal"]
    order.tax = summary["tax"]
    order.total = summary["total"]
    order.status = OrderStatus.pending_payment
    db.flush()

    for ci in cart.get_items(db, session.id):
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=ci.product_id,
                barcode=ci.product.barcode,
                name=ci.name,
                unit_price=ci.unit_price,
                qty=ci.qty,
                weighed_g=ci.weighed_g,
                line_total=ci.line_total,
            )
        )
    db.commit()
    db.refresh(order)
    return order


def mark_order_paid(db: Session, order: Order, provider: str, payment_ref: str) -> Order:
    if order.status == OrderStatus.paid:
        return order

    order.status = OrderStatus.paid
    order.payment_provider = provider
    order.payment_ref = payment_ref
    order.paid_at = datetime.utcnow()

    # decrement stock + write audit trail (produce lines have no unit stock)
    for item in order.items:
        product = db.get(Product, item.product_id)
        if product and not product.is_produce:
            product.stock_qty = max(0, product.stock_qty - item.qty)
            db.add(
                StockMovement(
                    product_id=product.id,
                    change=-item.qty,
                    reason="sale",
                    order_ref=order.order_ref,
                )
            )

    session = db.get(ShoppingSession, order.session_id)
    if session:
        session.status = SessionStatus.checked_out
        # clear the live cart now that it is an order
        db.query(CartItem).filter(CartItem.session_id == session.id).delete()

    db.commit()
    db.refresh(order)
    return order


def get_order_by_ref(db: Session, order_ref: str) -> Order | None:
    return db.query(Order).filter(Order.order_ref == order_ref).first()
