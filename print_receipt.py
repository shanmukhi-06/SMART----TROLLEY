#!/usr/bin/env python3
"""Thermal printer test & receipt-preview utility.

Run this on any machine that has a thermal printer configured.

Commands
--------
    python print_receipt.py discover         list every printer we can find
    python print_receipt.py test             print a hardware self-test page
    python print_receipt.py sample            print a sample receipt
    python print_receipt.py order <ref>      print a real order from the database
    python print_receipt.py text "hello"     print arbitrary text
    python print_receipt.py qr "https://..." print a QR code

The transport is chosen by the same PRINTER_* env vars as the running app.
Use ``--transport usb|bluetooth|network|auto`` to override.
"""
from __future__ import annotations

import argparse
import sys

from app.config import settings  # noqa: E402


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_discover(_args) -> None:
    from app.hardware.printer import discover_printers

    printers = discover_printers()
    if not printers:
        print("No printers found.")
        return
    for p in printers:
        print(f"  [{p.kind}] {p.name}  address={p.address or '-'}  {p.detail}")


def cmd_test(args) -> None:
    printer = _open_printer(args)
    try:
        printer.init()
        printer.align("center")
        printer.bold(True)
        printer.print_text(settings.store_name or "Smart Mart")
        printer.bold(False)
        printer.feed(1)
        printer.align("left")
        printer.print_text("Printer self-test")
        printer.print_text(f"Transport: {printer.kind}")
        printer.print_text(f"Charset: {settings.printer_charset}")
        printer.feed(1)
        printer.print_text("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        printer.print_text("abcdefghijklmnopqrstuvwxyz")
        printer.print_text("0123456789 !@#$%^&*()")
        printer.feed(2)
        printer.cut()
        print(f"Self-test sent to {printer.kind} printer.")
    finally:
        printer.close()


def cmd_sample(args) -> None:
    printer = _open_printer(args)
    try:
        _print_sample_receipt(printer)
        print("Sample receipt sent.")
    finally:
        printer.close()


def cmd_order(args) -> None:
    from app.database import SessionLocal
    from app.services.orders import get_order_by_ref

    with SessionLocal() as db:
        order = get_order_by_ref(db, args.ref)
    if not order:
        print(f"Order not found: {args.ref}")
        sys.exit(1)

    printer = _open_printer(args)
    try:
        printer.print_receipt(order)
        print(f"Receipt for {args.ref} sent to {printer.kind} printer.")
    finally:
        printer.close()


def cmd_text(args) -> None:
    printer = _open_printer(args)
    try:
        printer.init()
        printer.print_text(args.text)
        printer.feed(2)
        printer.cut()
        print(f"Printed {len(args.text)} chars.")
    finally:
        printer.close()


def cmd_qr(args) -> None:
    printer = _open_printer(args)
    try:
        printer.init()
        printer.align("center")
        printer.qr(args.data, size=6)
        printer.feed(2)
        printer.cut()
        print(f"QR code sent to {printer.kind} printer.")
    finally:
        printer.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _open_printer(args):
    from app.hardware.printer import get_printer, reset_printer

    if args.transport and args.transport != "auto":
        reset_printer()
        _patch_settings(args)
    printer = get_printer()
    info = printer.health()
    print(f"Using {info['kind']} printer  real_hardware={info['real_hardware']}")
    if info.get("detail"):
        print(f"  detail: {info['detail']}")
    if not info.get("online", True) and info.get("real_hardware"):
        print("  WARNING: printer appears offline")
    return printer


def _patch_settings(args) -> None:
    mapping = {
        "usb": ("printer_transport", "usb"),
        "bluetooth": ("printer_transport", "bluetooth"),
        "network": ("printer_transport", "network"),
        "remote": ("printer_transport", "remote"),
        "simulation": ("printer_transport", "simulation"),
        "sim": ("printer_transport", "simulation"),
        "auto": ("printer_transport", "auto"),
    }
    attr, value = mapping.get(args.transport, ("printer_transport", "auto"))
    setattr(settings, attr, value)
    if args.transport == "bluetooth" and args.bt_address:
        settings.printer_bt_address = args.bt_address
    if args.transport == "network" and args.net_host:
        settings.printer_net_host = args.net_host
        settings.printer_net_port = args.net_port or 9100


def _print_sample_receipt(printer) -> None:
    from app.models import Order, OrderItem

    class FakeOrder:
        order_ref = "SAMPLE-001"
        paid_at = None
        mobile = "9999999999"
        email = "test@example.com"
        payment_provider = "mock"
        payment_ref = "MOCK123"
        subtotal = 150.0
        tax = 7.5
        total = 157.5
        items = [
            OrderItem(name="Apples (1 kg)", qty=1, unit_price=120.0, line_total=120.0),
            OrderItem(name="Bread loaf", qty=2, unit_price=15.0, line_total=30.0),
        ]

    printer.print_receipt(FakeOrder())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Thermal printer test & preview utility",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="list available printers")

    p_test = sub.add_parser("test", help="print a hardware self-test page")
    p_test.add_argument("--transport", default="")
    p_test.add_argument("--bt-address", default="")
    p_test.add_argument("--net-host", default="")
    p_test.add_argument("--net-port", type=int, default=9100)

    p_sample = sub.add_parser("sample", help="print a sample receipt")
    p_sample.add_argument("--transport", default="")
    p_sample.add_argument("--bt-address", default="")
    p_sample.add_argument("--net-host", default="")
    p_sample.add_argument("--net-port", type=int, default=9100)

    p_order = sub.add_parser("order", help="print a real order by order_ref")
    p_order.add_argument("ref", help="order reference, e.g. ORD-20240918-001")
    p_order.add_argument("--transport", default="")
    p_order.add_argument("--bt-address", default="")
    p_order.add_argument("--net-host", default="")
    p_order.add_argument("--net-port", type=int, default=9100)

    p_text = sub.add_parser("text", help="print arbitrary text")
    p_text.add_argument("text", help="text to print")
    p_text.add_argument("--transport", default="")
    p_text.add_argument("--bt-address", default="")
    p_text.add_argument("--net-host", default="")
    p_text.add_argument("--net-port", type=int, default=9100)

    p_qr = sub.add_parser("qr", help="print a QR code")
    p_qr.add_argument("data", help="data encoded into the QR code")
    p_qr.add_argument("--transport", default="")
    p_qr.add_argument("--bt-address", default="")
    p_qr.add_argument("--net-host", default="")
    p_qr.add_argument("--net-port", type=int, default=9100)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "discover": cmd_discover,
        "test": cmd_test,
        "sample": cmd_sample,
        "order": cmd_order,
        "text": cmd_text,
        "qr": cmd_qr,
    }
    dispatch[args.command](args)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    raise SystemExit(main())
