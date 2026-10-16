"""Hardware abstraction layer.

Two devices are wired to this trolley and nothing else:

  * scale.py    4 x 50 kg load cells -> HX711 -> Pi (GPIO 5 / GPIO 6)
  * scanner.py  camera barcode/QR reader (the USB scanner gun is a keyboard,
                so it is captured in the browser, not here)

There are no buzzers or LEDs: scan feedback is visual (and an optional beep
played by the browser), which also means the kiosk works when the app is in the
cloud and the browser is the only thing in front of the shopper.

Every module exposes the same interface whether it is driving real hardware on
the Pi, reading through the cloud device bridge, or simulating on a laptop. The
choice comes from `settings.mode`, so application code never imports lgpio or
opencv directly.
"""
