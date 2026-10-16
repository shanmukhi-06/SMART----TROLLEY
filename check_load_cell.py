#!/usr/bin/env python3
"""Load-cell wiring checker.

Answers one question: *are the load cells actually connected and sensing load?*

It talks to the HX711 directly, bypassing the smoothing filters in
``app/hardware/scale.py`` — those deliberately hide a bad conversion by reusing
the last good value, which is right for a running till but useless when you are
hunting a wiring fault.

    python check_load_cell.py            # verdict for every channel/gain
    python check_load_cell.py --live     # continuous readout, press the plate
    python check_load_cell.py --live --gain 128

Nothing here writes to the database or .env; it is safe to run repeatedly while
you rewire. Stop ``trolley-agent`` first, because only one process can own the
GPIO pins:  sudo systemctl stop trolley-agent
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time

DT_DEFAULT = 5
SCK_DEFAULT = 6
# extra SCK pulses after the 24 data bits select input channel + gain
GAIN_PULSES = {128: 1, 64: 3, 32: 2}
SATURATED = 8_388_000
FULL_SCALE = 8_388_607


def channel_of(gain: int) -> str:
    return "A" if gain in (128, 64) else "B"


class RawHX711:
    """Bare-bones bit-banged HX711 reader. No filtering, no caching."""

    def __init__(self, dt: int = DT_DEFAULT, sck: int = SCK_DEFAULT, gain: int = 128):
        try:
            import lgpio
        except ImportError:
            sys.exit("lgpio is missing. Run: ./.venv/bin/pip install -r requirements-hardware.txt")
        self.lg = lgpio
        self.dt, self.sck, self.gain = dt, sck, gain
        self.pulses = GAIN_PULSES.get(gain, 1)
        try:
            self.chip = lgpio.gpiochip_open(0)
        except Exception as exc:
            sys.exit(f"cannot open gpiochip0: {exc}")
        try:
            lgpio.gpio_claim_input(self.chip, dt)
            lgpio.gpio_claim_output(self.chip, sck, 0)
        except Exception as exc:
            lgpio.gpiochip_close(self.chip)
            sys.exit(
                f"GPIO {dt}/{sck} unavailable ({exc}).\n"
                "Another process owns the pins. Try: sudo systemctl stop trolley-agent"
            )
        self.wake()

    def wake(self) -> None:
        """Bring the chip out of power-down.

        Holding SCK high for >60 us powers the HX711 down; if a previous run left
        it that way DOUT stays high forever and every read times out. Pulse it
        once and then hold SCK low so the internal oscillator restarts.
        """
        self.lg.gpio_write(self.chip, self.sck, 1)
        time.sleep(0.001)
        self.lg.gpio_write(self.chip, self.sck, 0)
        time.sleep(0.3)

    def responding(self, timeout_s: float = 1.5) -> bool:
        """True if DOUT ever goes low, i.e. the chip is powered and clocking."""
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self.lg.gpio_read(self.chip, self.dt) == 0:
                return True
            time.sleep(0.005)
        return False

    def _pulse(self) -> None:
        self.lg.gpio_write(self.chip, self.sck, 1)
        self.lg.gpio_write(self.chip, self.sck, 0)

    def read(self, timeout_s: float = 1.0) -> int | None:
        """One conversion, exactly as the chip reports it. None = never ready."""
        t0 = time.time()
        while self.lg.gpio_read(self.chip, self.dt) == 1:
            if time.time() - t0 > timeout_s:
                return None
            time.sleep(0.001)
        value = 0
        for _ in range(24):
            self.lg.gpio_write(self.chip, self.sck, 1)
            bit = self.lg.gpio_read(self.chip, self.dt)
            value = (value << 1) | bit
            self.lg.gpio_write(self.chip, self.sck, 0)
        for _ in range(self.pulses):
            self._pulse()
        if value & 0x800000:          # 24-bit two's complement
            value -= 1 << 24
        return value

    def settle(self, cycles: int = 12) -> None:
        """Discard conversions while the Sigma-Delta filter switches channel."""
        for _ in range(cycles):
            self.read()

    def sample(self, n: int) -> list[int | None]:
        return [self.read() for _ in range(n)]

    def close(self) -> None:
        # Leave SCK LOW, otherwise the chip stays in power-down and the next run
        # sees DOUT stuck high and wrongly reports dead hardware.
        try:
            self.lg.gpio_write(self.chip, self.sck, 0)
        except Exception:
            pass
        try:
            self.lg.gpiochip_close(self.chip)
        except Exception:
            pass


def verdict(vals: list[int | None]) -> tuple[str, str]:
    """(status, explanation) for one batch of conversions."""
    live = [v for v in vals if v is not None]
    if not live:
        return "NO DATA", "DOUT never went ready — check VCC (3V3), GND and the DT wire."

    sat = sum(1 for v in live if abs(v) >= SATURATED)
    if sat == len(live):
        return "BRIDGE OPEN", (
            f"Every conversion is pinned at {FULL_SCALE:,} (the ADC rail). The load-cell "
            "bridge is not connected to this channel. Check E+/E-/S+/S- and every "
            "solder joint on the combinator board."
        )
    if sat:
        return "INTERMITTENT", f"{sat}/{len(live)} conversions rail-saturated — a loose bridge joint."

    span = max(live) - min(live)
    median = statistics.median(live)
    if abs(median) < 5_000 and span < 500:
        return "NO SIGNAL", (
            f"Readings hover near zero (median {median:,.0f}, span {span:,}). The input is "
            "floating rather than driven by a bridge — the cells are not wired to this channel."
        )
    return "ALIVE", f"median {median:,.0f}, span {span:,} over {len(live)} conversions."


def scan(dt: int, sck: int) -> None:
    print("=" * 74)
    print(" Load-cell wiring check — probing both HX711 channels")
    print("=" * 74)
    print(f" DT=GPIO{dt}  SCK=GPIO{sck}\n")

    # Step 0: is the chip talking at all? No point probing channels otherwise.
    probe = RawHX711(dt, sck, 128)
    alive = probe.responding()
    probe.close()
    if not alive:
        print(" HX711 NOT RESPONDING — DOUT is stuck high.\n")
        print(" The chip is not clocking out data, so no channel can be tested.")
        print(" This is upstream of the load cells: fix the module first.\n")
        print("   1. VCC -> Pi 3V3 (pin 1) and GND -> Pi GND (pin 6). Is the")
        print("      module's power LED on, if it has one?")
        print(f"   2. DOUT/DT -> GPIO {dt} (pin 29). A loose DT wire floats high,")
        print("      which looks exactly like this.")
        print(f"   3. SCK/PD_SCK -> GPIO {sck} (pin 31).")
        print("   4. Reseat the jumper wires at both ends; check for a broken core.")
        print("\n Re-run this command after checking.")
        print("-" * 74)
        return

    results = {}
    for gain in (128, 64, 32):
        dev = RawHX711(dt, sck, gain)
        dev.settle()
        vals = dev.sample(20)
        dev.close()
        status, why = verdict(vals)
        results[gain] = status
        print(f" Channel {channel_of(gain)} @ gain {gain:<4} {status}")
        print(f"   {why}\n")
        time.sleep(0.2)

    print("-" * 74)
    if "ALIVE" in results.values():
        good = [g for g, s in results.items() if s == "ALIVE"]
        print(f" RESULT: channel {channel_of(good[0])} @ gain {good[0]} is reading a live bridge.")
        print(f"   Set HX711_GAIN={good[0]} in .env, then run:")
        print("     ./.venv/bin/python check_load_cell.py --live")
        print("   and confirm the numbers move when you press the plate.")
    else:
        print(" RESULT: no channel is sensing a load cell. This is a wiring fault;")
        print(" no software change can read a bridge that is not connected.")
        print("\n Check, in this order:")
        print("   1. E+ / E- (excitation) from the HX711 to the combinator board.")
        print("   2. S+ / S- (signal) into the HX711's A+ / A- pads.")
        print("   3. All four cells present in the bridge; no broken or cold joint.")
        print("   4. HX711 VCC on 3V3 (pin 1), never 5V.")
        print("   5. The plate actually rests on the cells so they can deflect.")
    print("-" * 74)


def live(dt: int, sck: int, gain: int) -> None:
    dev = RawHX711(dt, sck, gain)
    dev.settle()
    print(f"Live readout — channel {channel_of(gain)} @ gain {gain}. Ctrl-C to stop.")
    print("Press down on the plate: 'raw' must move well beyond the +/- noise band.\n")

    base_pool: list[int] = []
    lo = hi = None
    try:
        while True:
            vals = [v for v in dev.sample(10) if v is not None]
            if not vals:
                print("  no data (DOUT stuck high)                        ", end="\r")
                time.sleep(0.2)
                continue
            med = statistics.median(vals)
            if abs(med) >= SATURATED:
                print(f"  SATURATED {med:>12,.0f}  <- bridge open        ", end="\r")
                time.sleep(0.2)
                continue

            if len(base_pool) < 20:
                base_pool.append(med)
                base = statistics.median(base_pool)
                noise = statistics.pstdev(base_pool) if len(base_pool) > 1 else 0.0
                print(f"  calibrating zero… raw={med:>10,.0f}            ", end="\r")
                time.sleep(0.1)
                continue

            base = statistics.median(base_pool)
            noise = statistics.pstdev(base_pool) or 1.0
            delta = med - base
            lo = med if lo is None else min(lo, med)
            hi = med if hi is None else max(hi, med)
            sigma = delta / noise if noise else 0.0
            flag = "LOAD" if abs(sigma) > 8 else "    "
            print(
                f"  raw={med:>10,.0f}  delta={delta:>+9,.0f}  "
                f"{sigma:>+7.1f}sigma  span={hi - lo:>8,.0f}  {flag}   ",
                end="\r",
            )
            time.sleep(0.15)
    except KeyboardInterrupt:
        print("\n")
        if lo is not None and hi is not None:
            print(f"Observed span: {hi - lo:,} counts")
            if hi - lo < 1_000:
                print("Barely moved — the cells are not transferring force. Wiring or mounting.")
            else:
                print("Good swing. The cells respond to load; you can calibrate now:")
                print("  ./.venv/bin/python calibrate_scale.py calibrate")
    finally:
        dev.close()


def prove(dt: int, sck: int, gain: int, empty_s: float = 15.0, loaded_s: float = 25.0) -> None:
    """Two-phase test: record the plate empty, then loaded, and compare.

    This is the only honest answer to "is the weight real?" — it never touches
    the product database, so a number can only move if the cells actually move.
    """
    dev = RawHX711(dt, sck, gain)
    if not dev.responding():
        print("HX711 not responding (DOUT stuck high). Check VCC/GND and the DT wire.")
        dev.close()
        return
    dev.settle()

    def capture(seconds: float, tag: str) -> list[int]:
        out: list[int] = []
        t_end = time.time() + seconds
        while time.time() < t_end:
            v = dev.read()
            if v is not None:
                out.append(v)
            time.sleep(0.05)
        print(f"  [{tag}] {len(out)} conversions captured")
        return out

    print("=" * 74)
    print(f" LIVE WEIGHT PROOF — channel {channel_of(gain)} @ gain {gain}")
    print("=" * 74)
    print(f" PHASE 1: keep the plate EMPTY for {empty_s:.0f}s. Starting now…")
    empty = capture(empty_s, "empty")

    print(f"\n PHASE 2: PUT THE WEIGHT ON THE PLATE NOW — {loaded_s:.0f}s…")
    loaded = capture(loaded_s, "loaded")
    dev.close()

    print("\n" + "-" * 74)
    if not empty or not loaded:
        print(" No usable conversions. The chip stopped responding mid-test.")
        return

    e_med = statistics.median(empty)
    l_med = statistics.median(loaded)
    e_sd = statistics.pstdev(empty) if len(empty) > 1 else 0.0
    delta = l_med - e_med
    noise = e_sd or 1.0
    sigma = delta / noise

    print(f" empty  : median {e_med:>12,.0f}   noise sigma {e_sd:>8.1f}")
    print(f" loaded : median {l_med:>12,.0f}")
    print(f" change : {delta:>+12,.0f} counts   ({sigma:>+.1f} x noise)")
    sat_e = sum(1 for v in empty if abs(v) >= SATURATED)
    sat_l = sum(1 for v in loaded if abs(v) >= SATURATED)
    if sat_e or sat_l:
        print(f" saturated: empty {sat_e}/{len(empty)}, loaded {sat_l}/{len(loaded)}")
    print("-" * 74)

    if sat_e == len(empty) and sat_l == len(loaded):
        print(" VERDICT: DEAD — pinned at the ADC rail the whole time. The bridge is")
        print(" open; the cells are not electrically connected. Weight cannot be read.")
    elif abs(sigma) < 5:
        print(" VERDICT: NOT SENSING — the reading did not move beyond its own noise")
        print(" when you added weight. The cells are not transferring force.")
    else:
        grams_hint = ""
        print(f" VERDICT: WORKING — the cells responded to real load.{grams_hint}")
        print(" Calibrate next:  ./.venv/bin/python calibrate_scale.py calibrate")
    print("-" * 74)


def main() -> None:
    ap = argparse.ArgumentParser(description="Check whether the load cells are wired and sensing.")
    ap.add_argument("--dt", type=int, default=DT_DEFAULT, help=f"DOUT GPIO (default {DT_DEFAULT})")
    ap.add_argument("--sck", type=int, default=SCK_DEFAULT, help=f"SCK GPIO (default {SCK_DEFAULT})")
    ap.add_argument("--gain", type=int, choices=(128, 64, 32), default=128, help="gain for --live")
    ap.add_argument("--live", action="store_true", help="continuous readout instead of a scan")
    ap.add_argument("--prove", action="store_true",
                    help="two-phase empty-then-loaded test with a verdict")
    args = ap.parse_args()

    if args.prove:
        prove(args.dt, args.sck, args.gain)
    elif args.live:
        live(args.dt, args.sck, args.gain)
    else:
        scan(args.dt, args.sck)


if __name__ == "__main__":
    main()
