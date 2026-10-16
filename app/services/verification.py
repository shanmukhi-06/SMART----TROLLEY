"""Anti-theft "sensor fusion": cross-check scan vs. measured basket weight.

Level 1 (primary): weight. Every scanned item has an expected unit weight. When
it is dropped in the basket, the load cell must register a matching increase.

Level 2 (secondary): camera item-count. The camera confirms that *an* object
was added when one was expected (presence/counting), catching the case where a
scan happened but nothing was placed, or vice-versa. Full product recognition
(identifying *which* item) is a documented phase-2 bonus.
"""
from __future__ import annotations

import time

from app.config import settings
from app.hardware.scale import BaseScale


def wait_for_settle(
    scale: BaseScale,
    baseline_g: float,
    timeout_s: float = 6.0,
    stable_readings: int = 3,
    expected_g: float = 0.0,
) -> float:
    """Block until the basket weight stops changing, then return grams.

    Detects a stable reading, i.e. the shopper has finished placing the item.
    Returns the settled weight, or the last reading on timeout.

    Two shortcuts keep the shopper waiting as little as possible:
      * if the weight settles within tolerance of the expected value we accept
        it straight away rather than running the stability counter out;
      * a software scale changes instantly, so this returns on the first pass.

    Must be called from a worker thread — it blocks for up to `timeout_s`.
    """
    deadline = time.time() + timeout_s
    last = scale.get_grams()
    stable = 0
    tolerance = max(settings.weight_tolerance_g, expected_g * 0.15)

    while time.time() < deadline:
        time.sleep(0.15)
        current = scale.get_grams()
        moved = abs(current - baseline_g) > 3.0

        if abs(current - last) <= 2.0:
            stable += 1
            # settled at roughly the weight we were expecting -> done
            if (
                expected_g > 0
                and stable >= 2
                and abs((current - baseline_g) - expected_g) <= tolerance
            ):
                return current
            if stable >= stable_readings and moved:
                return current
        else:
            stable = 0
        last = current
    return last


def classify(delta_g: float, expected_g: float) -> str:
    """Return 'verified' or 'mismatch' for a single item addition."""
    tol = settings.weight_tolerance_g
    # generous lower bound for very light items; tolerance scales a little too
    allowed = max(tol, expected_g * 0.15)
    if expected_g <= 0:
        # unknown expected weight (e.g. missing catalog data) -> don't block
        return "verified"
    if abs(delta_g - expected_g) <= allowed:
        return "verified"
    return "mismatch"


def camera_confirms_addition(added: bool = True) -> bool:
    """Level-2 check stub.

    In simulation (and until a trained model is wired in), we assume the camera
    agrees with the weight sensor. The hook exists so phase-2 object
    detection can drop in here and return a real True/False.
    """
    return added
