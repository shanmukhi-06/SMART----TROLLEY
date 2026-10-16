"""Database models: catalog, stock, shopping sessions, orders.

Design notes
------------
* Product carries an expected `weight_g` used for the anti-theft weight check,
  plus `is_produce`/`price_per_kg` for loose fruit & veg priced by weight.
* A ShoppingSession is one shopper's trip (mobile + email captured at login).
* Cart items live on the session while shopping; on successful checkout they
  are copied into an immutable Order + OrderItems and stock is decremented.
* StockMovement is an audit trail so we can reconcile inventory later on AWS.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _now() -> datetime:
    return datetime.utcnow()


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    barcode: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(80), default="General")
    price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    # expected weight of a single unit, grams (for the anti-theft check)
    weight_g: Mapped[float] = mapped_column(Float, default=0.0)
    # loose produce (fruit/veg) is priced by weight instead of per-unit
    is_produce: Mapped[bool] = mapped_column(Boolean, default=False)
    price_per_kg: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    stock_qty: Mapped[int] = mapped_column(Integer, default=0)
    image: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AppSetting(Base):
    """Small key/value store for values that must outlive a restart.

    Used for load-cell calibration captured from the settings UI, so the Pi
    (or the cloud) does not need a .env edit + redeploy after calibrating.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class SessionStatus(str, enum.Enum):
    active = "active"
    checked_out = "checked_out"
    abandoned = "abandoned"


class ShoppingSession(Base):
    __tablename__ = "shopping_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mobile: Mapped[str] = mapped_column(String(20), index=True)
    email: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus), default=SessionStatus.active
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    items: Mapped[list["CartItem"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class CartItem(Base):
    __tablename__ = "cart_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("shopping_sessions.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    name: Mapped[str] = mapped_column(String(200))
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    qty: Mapped[int] = mapped_column(Integer, default=1)
    # for produce: the weighed amount in grams; else 0
    weighed_g: Mapped[float] = mapped_column(Float, default=0.0)
    line_total: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    # result of the weight/camera cross-check: verified | pending | mismatch
    verify_status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    session: Mapped["ShoppingSession"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()


class OrderStatus(str, enum.Enum):
    pending_payment = "pending_payment"
    paid = "paid"
    failed = "failed"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_ref: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("shopping_sessions.id"))
    mobile: Mapped[str] = mapped_column(String(20))
    email: Mapped[str] = mapped_column(String(200))
    subtotal: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    tax: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    total: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), default=OrderStatus.pending_payment
    )
    payment_provider: Mapped[str] = mapped_column(String(30), default="mock")
    payment_ref: Mapped[str] = mapped_column(String(120), default="")
    receipt_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    barcode: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    qty: Mapped[int] = mapped_column(Integer, default=1)
    weighed_g: Mapped[float] = mapped_column(Float, default=0.0)
    line_total: Mapped[float] = mapped_column(Numeric(10, 2), default=0)

    order: Mapped["Order"] = relationship(back_populates="items")


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    change: Mapped[int] = mapped_column(Integer)  # negative for a sale
    reason: Mapped[str] = mapped_column(String(120), default="sale")
    order_ref: Mapped[str] = mapped_column(String(40), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
