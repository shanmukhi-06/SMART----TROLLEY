#!/usr/bin/env python3
"""Thermal printer test script for 58mm USB receipt printer."""
import os
import sys

PRINTER_PATH = "/dev/usb/lp0"

ESC = b"\x1b"
GS = b"\x1d"

INIT = ESC + b"@"
ALIGN_LEFT = ESC + b"a\x00"
ALIGN_CENTER = ESC + b"a\x01"
BOLD_ON = ESC + b"E\x01"
BOLD_OFF = ESC + b"E\x00"
DOUBLE_SIZE = GS + b"!\x11"
NORMAL_SIZE = GS + b"!\x00"

def test_print():
    if not os.path.exists(PRINTER_PATH):
        print(f"Error: {PRINTER_PATH} not found.")
        sys.exit(1)

    try:
        with open(PRINTER_PATH, "wb") as f:
            f.write(INIT)
            f.write(ALIGN_CENTER)
            f.write(DOUBLE_SIZE + BOLD_ON + b"SMART MART\n" + NORMAL_SIZE + BOLD_OFF)
            f.write(b"Self-Checkout Trolley\n")
            f.write(b"--------------------------------\n")
            f.write(ALIGN_LEFT)
            f.write(b"Printer: USB 58mm OK\n")
            f.write(b"Status : Pitch Demo Ready\n")
            f.write(b"--------------------------------\n")
            f.write(ALIGN_CENTER)
            f.write(b"Thank you for shopping!\n")
            f.write(b"\n\n\n\n")
        print("Test print sent to /dev/usb/lp0 successfully!")
    except Exception as e:
        print(f"Failed to print: {e}")
        sys.exit(1)

if __name__ == "__main__":
    test_print()
