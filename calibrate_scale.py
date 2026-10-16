#!/usr/bin/env python3
"""Load-cell calibration & diagnostics for 4 x 50 kg cells on an HX711.

Run this on the Raspberry Pi. It turns raw HX711 counts into grams by finding
two numbers:

    OFFSET          raw reading with an empty basket  (the zero point)
    REFERENCE_UNIT  raw counts per gram               (the scale factor)

Commands
--------
    python calibrate_scale.py diagnose      is the wiring alive? noise? drift?
    python calibrate_scale.py live          continuous readout (Ctrl-C to stop)
    python calibrate_scale.py tare          re-zero only, keep the scale factor
    python calibrate_scale.py calibrate     the normal 2-point calibration
    python calibrate_scale.py multipoint    several weights + linearity check
    python calibrate_scale.py verify        check accuracy against a known mass

Where the result goes
---------------------
By default the values are saved to the app's database, which is what the running
app and the settings menu read — no restart needed. Add `--write-env` to also
update `.env`, or `--print-only` to just show them.

    --remote  talks to the cloud/LAN app's API instead of the GPIO pins. Use
              this when `pi_agent.py` is running, because only one process can
              own GPIO 5/6 at a time.

Wiring (4 x 50 kg half-bridge cells -> one full bridge -> HX711 -> Pi 5)
-----------------------------------------------------------------------
    HX711 VCC -> Pi 3V3 (pin 1)     see the note in app/hardware/scale.py
    HX711 GND -> Pi GND (pin 6)
    HX711 DT  -> BCM GPIO 5 (pin 29)
    HX711 SCK -> BCM GPIO 6 (pin 31)
Mount all four cells on a rigid, flat plate so the load spreads evenly;
otherwise readings shift depending on where in the basket the item lands.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.config import settings

ENV_PATH = Path(".env")
BAR = "=" * 68


# ===========================================================================
# Reading sources: direct GPIO, or through the app's API
# ===========================================================================
class DirectSource:
    """Talks to the HX711 on this Pi's GPIO pins."""

    name = "HX711 (direct GPIO)"

    def __init__(self, dt: int, sck: int, gain: int) -> None:
        from app.hardware.scale import HX711Scale

        self._scale = HX711Scale(dt_pin=dt, sck_pin=sck, gain=gain)

    def raw(self, samples: int = 20) -> dict:
        vals = []
        for _ in range(samples):
            vals.append(self._scale.read_raw())
            time.sleep(0.01)
        return {
            "median": statistics.median(vals),
            "mean": statistics.fmean(vals),
            "stdev": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            "min": min(vals),
            "max": max(vals),
            "n": len(vals),
        }

    def grams(self) -> float:
        return self._scale.get_grams()

    def reset(self) -> None:
        self._scale.reset()

    def close(self) -> None:
        self._scale.close()


class RemoteSource:
    """Talks to the running app (local or cloud) over its admin API.

    This is the right choice when pi_agent.py already owns the GPIO pins.
    """

    name = "app API"

    def __init__(self, base: str, pin: str) -> None:
        self._base = base.rstrip("/")
        self._cookie = ""
        self._login(pin)

    def _request(self, path: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(
            self._base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Cookie": self._cookie} if self._cookie else {})},
            method="POST" if payload is not None else "GET",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            cookie = resp.headers.get("set-cookie")
            if cookie:
                self._cookie = cookie.split(";")[0]
            return json.load(resp)

    def _login(self, pin: str) -> None:
        try:
            self._request("/api/admin/login", {"pin": pin})
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise SystemExit(
                    "Wrong admin PIN. Pass the right one with --pin "
                    "(it is ADMIN_PIN in the app's .env)."
                )
            raise

    def raw(self, samples: int = 20) -> dict:
        data = self._request("/api/admin/scale")
        r = data.get("reading", {})
        if r.get("error"):
            raise RuntimeError(r["error"])
        return {
            "median": r.get("raw", 0.0),
            "mean": r.get("raw", 0.0),
            "stdev": r.get("raw_stdev", 0.0),
            "min": r.get("raw_min", 0.0),
            "max": r.get("raw_max", 0.0),
            "n": r.get("samples", 1),
        }

    def grams(self) -> float:
        return self._request("/api/admin/scale").get("reading", {}).get("grams", 0.0)

    def save(self, reference_unit: float, offset: float) -> dict:
        return self._request("/api/admin/scale/calibrate/manual",
                             {"reference_unit": reference_unit, "offset": offset})

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


def open_source(args):
    if args.remote:
        base = args.remote if args.remote != "auto" else settings.cloud_url
        print(f"[calib] using the app API at {base}")
        return RemoteSource(base, args.pin)

    if settings.is_simulated and not args.force:
        raise SystemExit(
            "HARDWARE_MODE resolves to 'simulation', so there is no load cell to\n"
            "calibrate. On the Pi set HARDWARE_MODE=local (or SIMULATION=false)\n"
            "in .env, or pass --force to try the GPIO pins anyway."
        )
    try:
        return DirectSource(args.dt, args.sck, args.gain)
    except Exception as exc:
        raise SystemExit(
            f"Could not open the HX711: {exc}\n\n"
            "Common causes:\n"
            "  * pi_agent.py or the app is already running and owns GPIO "
            f"{args.dt}/{args.sck}.\n"
            "    Stop it first:  sudo systemctl stop trolley-agent\n"
            "    ...or calibrate through the running app:  "
            "python calibrate_scale.py calibrate --remote auto\n"
            "  * lgpio is not installed:  pip install -r requirements-hardware.txt\n"
            "  * wrong pins: check --dt / --sck against your wiring."
        )


# ===========================================================================
# Persisting the result
# ===========================================================================
def save_calibration(reference_unit: float, offset: float, args, source,
                     note: str) -> None:
    print()
    print("--- calibration result " + "-" * 44)
    print(f"  SCALE_REFERENCE_UNIT={reference_unit:.6f}   (raw counts per gram)")
    print(f"  SCALE_OFFSET={offset:.4f}                  (empty-basket zero)")
    print("-" * 68)

    if args.print_only:
        print("(--print-only: nothing was saved)")
        return

    if isinstance(source, RemoteSource):
        source.save(reference_unit, offset)
        print("Saved through the app API — the running app is already using it.")
    else:
        from app.services import settings_store

        settings_store.write_scale_calibration(reference_unit, offset, note=note)
        print("Saved to the app database. The app picks it up on next start,")
        print("and the settings menu shows it immediately.")

    if args.write_env:
        _write_env(reference_unit, offset)


def _write_env(reference_unit: float, offset: float) -> None:
    if not ENV_PATH.exists():
        print(f"[calib] {ENV_PATH} not found; skipping the .env update.")
        return
    lines = ENV_PATH.read_text().splitlines()
    wanted = {
        "SCALE_REFERENCE_UNIT": f"{reference_unit:.6f}",
        "SCALE_OFFSET": f"{offset:.4f}",
    }
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in wanted:
            lines[i] = f"{key}={wanted[key]}"
            seen.add(key)
    for key, value in wanted.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    print(f"Updated {ENV_PATH}.")


# ===========================================================================
# Commands
# ===========================================================================
def cmd_diagnose(source, args) -> int:
    print(BAR)
    print(f" Load-cell diagnostics — source: {source.name}")
    print(BAR)

    try:
        first = source.raw(args.samples)
    except Exception as exc:
        print(f"FAIL  could not read the HX711: {exc}")
        return 1

    print(f"\n1. Reading         median {first['median']:>12,.0f} counts "
          f"(from {first['n']} samples)")

    # noise
    noise_pct = (first["stdev"] / abs(first["median"]) * 100) if first["median"] else 0
    print(f"2. Noise           sigma {first['stdev']:>13,.1f} counts "
          f"({noise_pct:.3f}% of reading)")
    if first["stdev"] == 0:
        print("   WARN  perfectly identical samples — is the HX711 actually")
        print("         converting? Check VCC and the DT wire.")
    elif noise_pct > 1:
        print("   WARN  noisy. Usual causes: 5 V supply instead of 3V3, long")
        print("         unshielded cell wires, or a loose bridge connection.")
    else:
        print("   OK    noise is low enough for gram-level weighing.")

    # stuck rails
    if abs(first["median"]) in (0, 8388607, 8388608):
        print("   WARN  the reading is pinned at a rail — the bridge is probably")
        print("         open. Re-check E+/E-/A+/A- on the combinator board.")

    # does it respond to load?
    print("\n3. Response test")
    print("   Press and release the basket by hand for ~4 seconds…")
    samples = []
    end = time.time() + 4
    while time.time() < end:
        samples.append(source.raw(3)["median"])
        time.sleep(0.05)
    swing = max(samples) - min(samples)
    print(f"   swing observed: {swing:,.0f} counts")
    if swing < max(50, first["stdev"] * 5):
        print("   FAIL  barely moved. The cells are not sensing load. Check the")
        print("         bridge wiring and that the plate actually rests on the cells.")
        return 1
    print("   OK    the cells respond to load.")

    # drift
    print("\n4. Drift (10 s, leave the basket alone)")
    start = source.raw(args.samples)["median"]
    time.sleep(10)
    later = source.raw(args.samples)["median"]
    drift = later - start
    print(f"   {drift:+,.0f} counts over 10 s")
    if abs(drift) > max(200, first["stdev"] * 10):
        print("   WARN  drifting. Let the cells warm up for a minute, and keep")
        print("         them out of direct sun / airflow.")
    else:
        print("   OK    stable.")

    cal = _current_calibration()
    print(f"\n5. Calibration     {cal['reference_unit']:.6f} counts/g, "
          f"offset {cal['offset']:.0f}  [{cal['source']}]")
    if abs(cal["reference_unit"] - 1.0) < 1e-9:
        print("   WARN  still the default 1.0 — grams will be meaningless until")
        print("         you run:  python calibrate_scale.py calibrate")

    print("\n" + BAR)
    return 0


def cmd_live(source, args) -> int:
    cal = _current_calibration()
    print(f"Live readout — {source.name}. Ctrl-C to stop.")
    print(f"Using {cal['reference_unit']:.6f} counts/g, offset {cal['offset']:.0f}\n")
    try:
        while True:
            stats = source.raw(max(3, args.samples // 4))
            grams = (stats["median"] - cal["offset"]) / (cal["reference_unit"] or 1.0)
            bar = "#" * min(40, max(0, int(grams / 50)))
            sys.stdout.write(
                f"\r raw {stats['median']:>12,.0f}  "
                f"sigma {stats['stdev']:>7,.1f}  "
                f"{grams:>10.1f} g  {bar:<40}"
            )
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_tare(source, args) -> int:
    cal = _current_calibration()
    print("Empty the basket completely.")
    input("Press Enter when it is empty… ")
    stats = source.raw(args.samples * 2)
    print(f"New zero: {stats['median']:,.0f} counts (noise ±{stats['stdev']:.1f})")
    save_calibration(cal["reference_unit"], stats["median"], args, source,
                     note="tare via calibrate_scale.py")
    return 0


def cmd_calibrate(source, args) -> int:
    print(BAR)
    print(" 2-point calibration — 4 x 50 kg cells on an HX711")
    print(BAR)
    print("You need one reference weight you trust. For a shopping basket a")
    print("1–5 kg mass works best (a sealed 1 kg sugar/salt pack, or gym plates).")
    print("Anything under ~200 g is too small to calibrate 200 kg of capacity well.\n")

    input("STEP 1 — empty the basket completely, then press Enter… ")
    zero = source.raw(args.samples * 2)
    print(f"   zero: {zero['median']:,.0f} counts   (noise ±{zero['stdev']:.1f})")
    if zero["stdev"] > 2000:
        print("   WARN  very noisy zero. Run `diagnose` before trusting this.")

    known_g = args.known
    if known_g is None:
        try:
            known_g = float(input("\nSTEP 2 — reference weight in GRAMS (e.g. 1000): "))
        except ValueError:
            print("That was not a number.")
            return 1
    if known_g <= 0:
        print("The reference weight must be greater than zero.")
        return 1
    if known_g < 200:
        print(f"   NOTE  {known_g:.0f} g is quite light for a 200 kg range; the")
        print("         scale factor will be less accurate.")

    input(f"   Place the {known_g:.0f} g reference in the basket, then press Enter… ")
    loaded = source.raw(args.samples * 2)
    print(f"   loaded: {loaded['median']:,.0f} counts   (noise ±{loaded['stdev']:.1f})")

    span = loaded["median"] - zero["median"]
    print(f"   span: {span:+,.0f} counts for {known_g:.0f} g")

    if abs(span) < max(100, zero["stdev"] * 5):
        print("\nFAIL  the reading barely changed. Either the weight is not on the")
        print("      basket, or the bridge is not wired through to the HX711.")
        print("      Run:  python calibrate_scale.py diagnose")
        return 1

    reference_unit = span / known_g
    full_scale = settings.scale_capacity_g * reference_unit
    print(f"\n   {reference_unit:.4f} counts per gram")
    print(f"   at {settings.scale_capacity_g / 1000:.0f} kg capacity that is "
          f"{full_scale:,.0f} counts of the ±8,388,608 available")
    if abs(full_scale) > 8_388_608:
        print("   WARN  the cells would saturate the HX711 before reaching full")
        print("         capacity. Consider gain 64 (HX711_GAIN=64).")
    if abs(reference_unit) < 0.5:
        print("   WARN  under 0.5 counts/g means ~2 g resolution at best.")

    save_calibration(reference_unit, zero["median"], args, source,
                     note=f"2-point with {known_g:.0f} g reference")

    # immediate sanity check
    measured = (loaded["median"] - zero["median"]) / reference_unit
    print(f"\nSanity check: the reference now reads {measured:.1f} g "
          f"(error {measured - known_g:+.1f} g)")
    print("\nNext: `python calibrate_scale.py verify` with a DIFFERENT known "
          "weight to confirm.")
    return 0


def cmd_multipoint(source, args) -> int:
    """Several weights, least-squares fit, and a linearity report."""
    print(BAR)
    print(" Multi-point calibration + linearity check")
    print(BAR)
    print("Add weights one at a time. Enter the CUMULATIVE weight in the basket")
    print("each time. Enter a blank line when you are done (3+ points ideal).\n")

    input("Empty the basket, then press Enter… ")
    zero = source.raw(args.samples * 2)
    print(f"   zero: {zero['median']:,.0f} counts\n")

    points: list[tuple[float, float]] = [(0.0, zero["median"])]
    while True:
        entry = input(f"Cumulative weight in grams (point {len(points)}), "
                      "blank to finish: ").strip()
        if not entry:
            break
        try:
            grams = float(entry)
        except ValueError:
            print("   not a number, try again")
            continue
        stats = source.raw(args.samples * 2)
        points.append((grams, stats["median"]))
        print(f"   {grams:>8.0f} g -> {stats['median']:>12,.0f} counts "
              f"(±{stats['stdev']:.1f})")

    if len(points) < 3:
        print("\nNeed at least 2 loaded points. Falling back to the 2-point maths.")
        if len(points) < 2:
            return 1

    # least squares through the points: counts = slope * grams + intercept
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    n = len(points)
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        print("All points have the same weight — cannot fit.")
        return 1
    slope = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n)) / denom
    intercept = mean_y - slope * mean_x

    print(f"\nFit: {slope:.4f} counts/g, zero at {intercept:,.0f} counts")
    print("\n  known (g)   measured (g)    error (g)")
    print("  " + "-" * 40)
    worst = 0.0
    for grams, counts in points:
        measured = (counts - intercept) / slope
        err = measured - grams
        worst = max(worst, abs(err))
        print(f"  {grams:>9.0f}   {measured:>12.1f}   {err:>+10.1f}")

    span_g = max(xs) - min(xs)
    linearity = (worst / span_g * 100) if span_g else 0
    print(f"\nWorst error {worst:.1f} g over a {span_g:.0f} g range "
          f"({linearity:.2f}% non-linearity)")
    if linearity > 2:
        print("WARN  poor linearity. Usually means the four cells are not sharing")
        print("      the load evenly — check the plate is rigid and sits on all four.")
    else:
        print("OK    linearity is good.")

    save_calibration(slope, intercept, args, source,
                     note=f"multipoint fit, {n} points")
    return 0


def cmd_verify(source, args) -> int:
    cal = _current_calibration()
    print(f"Verifying with {cal['reference_unit']:.6f} counts/g, "
          f"offset {cal['offset']:.0f}  [{cal['source']}]\n")

    known_g = args.known
    if known_g is None:
        try:
            known_g = float(input("Known weight in GRAMS: "))
        except ValueError:
            print("That was not a number.")
            return 1

    input(f"Place the {known_g:.0f} g weight in the basket, then press Enter… ")
    stats = source.raw(args.samples * 2)
    measured = (stats["median"] - cal["offset"]) / (cal["reference_unit"] or 1.0)
    error = measured - known_g
    tol = settings.weight_tolerance_g

    print(f"\n  measured : {measured:.1f} g")
    print(f"  expected : {known_g:.1f} g")
    print(f"  error    : {error:+.1f} g")
    print(f"  anti-theft tolerance is ±{tol:.0f} g "
          f"(or ±15% for heavier items)\n")

    if abs(error) <= tol:
        print("PASS  within tolerance — the anti-theft check will behave.")
        return 0
    print("FAIL  outside tolerance. Re-run `calibrate`, and make sure the basket")
    print("      was empty when the zero was captured.")
    return 1


def _current_calibration() -> dict:
    try:
        from app.services import settings_store

        return settings_store.read_scale_calibration()
    except Exception:
        return {
            "reference_unit": settings.scale_reference_unit or 1.0,
            "offset": settings.scale_offset,
            "source": "env",
        }


# ===========================================================================
# CLI
# ===========================================================================
COMMANDS = {
    "diagnose": cmd_diagnose,
    "live": cmd_live,
    "tare": cmd_tare,
    "calibrate": cmd_calibrate,
    "multipoint": cmd_multipoint,
    "verify": cmd_verify,
}


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("command", nargs="?", default="calibrate", choices=COMMANDS,
                   help="what to do (default: calibrate)")
    p.add_argument("--dt", type=int, default=settings.hx711_dt_pin,
                   help="HX711 DOUT BCM pin")
    p.add_argument("--sck", type=int, default=settings.hx711_sck_pin,
                   help="HX711 PD_SCK BCM pin")
    p.add_argument("--gain", type=int, default=settings.hx711_gain,
                   choices=[128, 64, 32])
    p.add_argument("--samples", type=int, default=20,
                   help="samples per measurement (default 20)")
    p.add_argument("--known", type=float, default=None,
                   help="reference weight in grams (skips the prompt)")
    p.add_argument("--remote", nargs="?", const="auto", default=None,
                   metavar="URL",
                   help="calibrate through a running app instead of GPIO "
                        "(use when pi_agent.py owns the pins)")
    p.add_argument("--pin", default=settings.admin_pin,
                   help="admin PIN for --remote")
    p.add_argument("--write-env", action="store_true",
                   help="also update SCALE_* in .env")
    p.add_argument("--print-only", action="store_true",
                   help="show the values, save nothing")
    p.add_argument("--force", action="store_true",
                   help="use the GPIO pins even in simulation mode")
    args = p.parse_args()

    source = open_source(args)
    try:
        return COMMANDS[args.command](source, args)
    except KeyboardInterrupt:
        print("\ncancelled")
        return 130
    finally:
        source.close()


if __name__ == "__main__":
    raise SystemExit(main())
