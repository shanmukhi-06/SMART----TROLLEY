# Raspberry Pi 5 setup guide

The web app already runs in the cloud. The Pi's whole job is:

1. read the **4 × 50 kg load cells** through the **HX711** and stream the raw
   counts up,
2. forward barcodes its **camera** decodes,
3. show the cloud UI **fullscreen** in Chromium.

The **USB barcode gun needs no setup at all** — it behaves as a keyboard, so it
types straight into the kiosk browser and the keystrokes travel to the cloud
with the page. Nothing to install, nothing to configure.

There is **no buzzer and no LED** on this trolley. Scan feedback is a green/red
screen flash plus a beep played by the browser.

---

## 1. Wire the load cell

Combine the four 50 kg half-bridge cells into one full Wheatstone bridge (a
combinator board does this for you), then wire the HX711 to the Pi:

| HX711 pin | Raspberry Pi 5 | Notes |
|---|---|---|
| `VCC` | **3V3, pin 1** | See the warning below. Do **not** use 5 V. |
| `GND` | GND, pin 6 | |
| `DT` / `DOUT` | **GPIO 5**, pin 29 | `HX711_DT_PIN` |
| `SCK` / `PD_SCK` | **GPIO 6**, pin 31 | `HX711_SCK_PIN` |

> **Power it from 3V3, not 5V.** The Pi's GPIO inputs are 3.3 V. If you power the
> HX711 from 5 V its `DOUT` swings to ~5 V, which is out of spec for a Pi input
> and can damage the GPIO. Either use 3V3 (simplest), or keep 5 V and put a level
> shifter — or a 1 kΩ/2 kΩ divider — between `DOUT` and GPIO 5.

Mechanical bit that matters more than people expect: mount all four cells under
a **rigid, flat plate** so load spreads evenly. If the plate flexes, the reading
changes depending on *where* in the basket the item lands, and the anti-theft
check gets unreliable. The `multipoint` calibration command reports exactly this
as a linearity figure.

The Pi 5 uses the new RP1 I/O controller, so the legacy `RPi.GPIO` HX711
libraries do **not** work. This project bit-bangs the HX711 protocol on `lgpio`
instead (`app/hardware/scale.py`).

---

## 2. Install

Flash Raspberry Pi OS (64-bit, Bookworm or newer), boot, connect to Wi-Fi, then:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/rahulkothuri/smart-trolley-project.git ~/smart-trolley
cd ~/smart-trolley

./scripts/install-pi.sh <CLOUD_URL> <DEVICE_TOKEN>
```

Fill in the two values from your deployment. To read the token back off the
server at any time:

```bash
ssh -i ~/.ssh/lightsail-ap-south-1.pem ubuntu@<SERVER_IP> \
  'grep DEVICE_TOKEN /opt/trolley/.env'
```

The script installs the Python venv, `lgpio`, OpenCV and `pyzbar`, adds you to
the `gpio` group, writes `.env` with `HARDWARE_MODE=cloud`, registers the
`trolley-agent` systemd service, and wires the kiosk browser into autostart
(labwc, wayfire and LXDE are all handled).

**Log out and back in** afterwards — the `gpio` group needs a fresh session.

---

## 3. Check the wiring before anything else

```bash
cd ~/smart-trolley
./.venv/bin/python calibrate_scale.py diagnose
```

This runs five checks and tells you what is wrong in plain language:

| Check | What a failure means |
|---|---|
| Reading | `DOUT stuck high` → VCC/GND or the DT wire. |
| Noise (σ) | High σ → powered from 5 V, long unshielded cell wires, or a loose bridge joint. |
| Response | Press the basket by hand. No swing → the bridge is not sensing load. |
| Drift | Large drift → let the cells warm up; keep them out of sun and airflow. |
| Calibration | Still `1.0` → grams are meaningless until you calibrate. |

Do not move on until the response test passes.

---

## 4. Calibrate

You need one weight you trust. **1–5 kg is the sweet spot** for a basket — a
sealed 1 kg salt or sugar pack works. Anything under ~200 g is too light to
calibrate 200 kg of capacity accurately.

```bash
./.venv/bin/python calibrate_scale.py calibrate
```

It prompts for an empty basket, then the reference weight, and saves the result
to the app database — so it takes effect immediately, with no redeploy.

Confirm with a **different** known weight:

```bash
./.venv/bin/python calibrate_scale.py verify
```

Other commands:

| Command | Use |
|---|---|
| `live` | Continuous readout with a bar graph. Great while mounting the cells. |
| `tare` | Re-zero only, keeping the scale factor. |
| `multipoint` | Several weights, least-squares fit, linearity report. Use this to prove the four cells share load evenly. |

### Calibrating later, without stopping anything

Only one process can own GPIO 5/6, so once the agent is running you cannot open
the pins again. Two ways round it:

```bash
# through the running cloud app
./.venv/bin/python calibrate_scale.py calibrate --remote auto --pin <ADMIN_PIN>
```

…or just use **⚙ Settings → Scale** in the browser, which does the same 2-point
wizard on screen. That is the one to teach store staff.

---

## 5. Start the agent

```bash
sudo systemctl start trolley-agent
journalctl -u trolley-agent -f
```

You want to see:

```
[agent] HX711 ready
[agent] connecting to https://…
[agent] connected
```

Confirm the cloud agrees, from any machine:

```bash
curl -s <CLOUD_URL>/api/device/status | python3 -m json.tool
```

`device.online: true` means readings are arriving. The shop screen also shows a
red **⚠ scale offline** badge whenever they stop, so staff notice immediately.

---

## 6. Bring up the kiosk

```bash
sudo reboot
```

Chromium comes up fullscreen on the cloud URL with no address bar, no tabs and
no first-run prompts. Screen blanking is disabled and the mouse pointer hides
after two seconds.

To test the kiosk without rebooting:

```bash
./scripts/kiosk.sh
```

The in-app **⛶** button in the top bar toggles fullscreen too, and
**⚙ Settings → System → Toggle fullscreen** does the same for staff.

---

## 7. Register products

This is the flow for store staff, and it does not need a terminal:

1. Tap **⚙** in the top right, enter the store PIN.
2. **➕ Add product** tab.
3. Scan the product's barcode — the form fills itself if it is already known,
   otherwise it clears for a new entry.
4. Type the name and price.
5. Put **one unit** on the basket and tap **⚖ Use scale weight**.
6. **Save product**.

Step 5 is the important one. `weight_g` is what the anti-theft check compares
against, and capturing it from the real load cell is far more accurate than
reading the packet. The form previews the accepted range, e.g.
*"Accepts 60–110 g (±25 g)"*.

For loose fruit and veg, tick **Loose produce** and give a price per kg instead —
those get weighed at the basket during shopping.

**🔍 Look up** is the other half: scan anything to see its price, stock, and
registered weight, with buttons to edit, adjust stock or de-list it. Products
showing **no weight check** will never be weight-verified, so they are worth
fixing.

---

## 8. Day-to-day

| Task | Command |
|---|---|
| Agent status | `systemctl status trolley-agent` |
| Agent logs | `journalctl -u trolley-agent -f` |
| Restart agent | `sudo systemctl restart trolley-agent` |
| One-off reading | `./.venv/bin/python pi_agent.py --once` |
| Run without the camera | `./.venv/bin/python pi_agent.py --no-camera` |
| Force HTTP instead of WebSocket | `./.venv/bin/python pi_agent.py --http` |

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `LOAD CELL UNAVAILABLE: No module named 'lgpio'` | `pip install -r requirements-hardware.txt` inside the venv. |
| `GPIO 5/6 unavailable` | Another process owns the pins, almost always the agent. `sudo systemctl stop trolley-agent`, or use `--remote`. |
| Agent logs `401` | `DEVICE_TOKEN` in the Pi's `.env` does not match the server's. |
| `⚠ scale offline` in the UI | Agent stopped, or no network. Check `journalctl -u trolley-agent -f`. |
| Weight reads 0 or nonsense | Not calibrated. Run `diagnose`, then `calibrate`. |
| Everything verifies, even obvious theft | The scale fell back to software mode. Check `/api/device/status`; the server sets `ALLOW_SIM_FALLBACK=false` to prevent this. |
| Barcode gun does nothing | The browser lost focus. The page re-focuses every 1.5 s; tap the screen once. |
| Camera scans ignored | `libzbar0` missing, or `CAMERA_ENABLED=false`. The USB gun is unaffected. |
| Beep does not play | Browsers block audio until the first interaction. Tap the screen once; `--autoplay-policy` is already set in the kiosk launcher. |
| Kiosk shows "restore pages?" | Handled by the dedicated profile in `scripts/kiosk.sh`; if it persists, delete `~/.config/trolley-kiosk`. |

---

## 10. How the pieces talk

```
        cloud (Lightsail, Mumbai)                    the trolley
┌──────────────────────────────────┐        ┌────────────────────────────┐
│ FastAPI + SQLite                 │        │ pi_agent.py                │
│                                  │◄─wss───┤   raw HX711 counts, 4/sec  │
│ owns calibration                 ├──cmds─►│   tare / reset             │
│ owns the catalogue               │        │                            │
│ runs the anti-theft weight check │        │ Chromium --kiosk ──────────┼─► shopper
└──────────────┬───────────────────┘        └────────────────────────────┘
               └────── https ─────────────────────► USB gun types into the page
```

The agent sends **raw counts, not grams**, on purpose: calibration then lives in
one place (the cloud), so re-calibrating from the settings menu is instant and
the Pi never needs redeploying.
