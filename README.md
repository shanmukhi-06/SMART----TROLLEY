# Smart Shopping Trolley

Self-checkout shopping trolley. Shoppers log in with mobile + email, scan
products, a load cell weight-verifies every item as an anti-theft check, they pay
by UPI QR at the cart, and a receipt is emailed and stored.

The web app runs **in the cloud**. The Raspberry Pi is a thin client that reads
the load cell and shows the UI fullscreen.

```
        cloud (Lightsail / EC2 / Fargate)              the trolley
┌────────────────────────────────────┐        ┌────────────────────────────┐
│ FastAPI + SQLite/Postgres          │        │ pi_agent.py                │
│   the whole UI + API               │◄─wss───┤   raw HX711 counts, 4/sec  │
│   owns the catalogue               ├──cmds─►│   tare / reset             │
│   owns the calibration             │        │                            │
│   runs the anti-theft weight check │        │ Chromium --kiosk ──────────┼─► shopper
└──────────────┬─────────────────────┘        └────────────────────────────┘
               └────── https ──────────────────────► USB barcode gun is a
                                                     keyboard: types into the page
```

Why this split: the Pi holds no database, no catalogue and no business logic, so
reflashing a trolley takes minutes and loses nothing. The agent streams **raw
counts, not grams**, so calibration lives in one place and staff can re-calibrate
from the settings menu with no redeploy.

## Features
- Clean white/blue touch UI: login → shop → checkout → receipt
- **Staff settings menu (⚙)** — PIN-protected, no terminal needed:
  - **Add product** — scan a barcode, capture the unit weight off the real scale, save
  - **Look up** — scan anything to see price, stock and registered weight
  - **Scale** — live reading, tare, and a 2-point calibration wizard
  - **System** — fullscreen toggle and hardware diagnostics
- Barcode/QR scanning: **USB gun (primary)** + **camera/pyzbar (backup)**
- **Anti-theft weight check**: every item has an expected weight; a mismatch
  blocks checkout. Tolerance is `max(25 g, 15%)`.
- Loose **produce weighed and priced by the kg**
- **UPI payment** via pluggable providers: `mock` and `phonepe`
- Orders, live cart, stock and a stock-movement audit trail
- Receipts saved as HTML and emailed over SMTP
- **Kiosk fullscreen** with autostart on the Pi
- Runs with **no hardware at all** for development

## Hardware

Only two devices are wired, and neither is a buzzer or an LED — scan feedback is
a green/red screen flash plus a beep played by the browser.

| Device | Connection |
|---|---|
| 4 × 50 kg load cells → HX711 | `VCC`→3V3 pin 1, `GND`→pin 6, `DT`→GPIO 5 pin 29, `SCK`→GPIO 6 pin 31 |
| Camera (backup scanner) | CSI or USB |
| USB barcode gun | USB, needs no configuration |

> Power the HX711 from **3V3, not 5V**. The Pi's GPIO inputs are 3.3 V; a 5 V
> `DOUT` is out of spec and can damage the pin. The Pi 5's RP1 controller also
> breaks the legacy `RPi.GPIO` HX711 libraries, so this project bit-bangs the
> protocol on `lgpio`.

Full wiring, calibration and staff walkthrough: **[deploy/PI_SETUP.md](deploy/PI_SETUP.md)**

## Run it locally (no hardware)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # defaults are set for simulation
python run.py
```

Open http://localhost:8000

Demo tips:
- Type a barcode in the **Manual / dev** box, e.g. `8901058000313`
- **Simulate theft** arms the next scan to add the wrong weight → it gets flagged
  and checkout is blocked
- **⚙ Settings** → PIN `1234` → try registering a product
- On checkout, **Simulate payment now** completes the UPI flow

## Deploy

**[deploy/DEPLOY.md](deploy/DEPLOY.md)** covers Lightsail ($5/mo, the path this
project uses), EC2 CloudFormation, manual Docker, a security checklist, and why
Lambda + DynamoDB is *not* cheaper for this workload.

```bash
docker compose up -d --build     # app + Postgres + Caddy (auto HTTPS)
```

## Modes

`HARDWARE_MODE` decides where the load cell lives:

| Mode | Meaning |
|---|---|
| `simulation` | Software scale, no hardware. Laptop development. |
| `local` | App and HX711 on the same Pi (single-box setup). |
| `cloud` | App in the cloud, `pi_agent.py` streams the load cell up. |
| `auto` | Follow the legacy `SIMULATION` flag. |

A failed HX711 never silently falls back to fake weights on a real trolley: the
app logs loudly, the UI shows **⚠ scale offline**, and with
`ALLOW_SIM_FALLBACK=false` scans are refused rather than passed.

## Calibration

```bash
python calibrate_scale.py diagnose     # is the wiring alive? noise? drift?
python calibrate_scale.py calibrate    # 2-point, the normal one
python calibrate_scale.py verify       # check against a second known weight
python calibrate_scale.py live         # continuous readout
python calibrate_scale.py multipoint   # linearity check across several weights
python calibrate_scale.py tare         # re-zero only
```

Add `--remote auto` to calibrate through a running app instead of the GPIO pins —
necessary when `pi_agent.py` already owns them. Or just use ⚙ Settings → Scale.

## Payments

- `PAYMENT_PROVIDER=mock` — real UPI QR, auto-confirms. No money moves.
- `PAYMENT_PROVIDER=phonepe` — fill `PHONEPE_MERCHANT_ID`, `PHONEPE_SALT_KEY`,
  `PHONEPE_SALT_INDEX`, `PHONEPE_ENV`. Implements Standard Checkout `pay` + `status`.

## Project layout
```
run.py                     entry point
pi_agent.py                Pi thin client: load cell + camera -> cloud
calibrate_scale.py         calibration & diagnostics CLI
Dockerfile                 cloud image (single worker, see the note inside)
docker-compose.yml         app + Postgres + Caddy
app/
  config.py                settings, HARDWARE_MODE resolution
  main.py                  routes, device bridge, admin API, WebSockets
  devices.py               cloud <-> Pi transport (readings up, commands down)
  security.py              admin PIN cookie + device token
  database.py models.py seed.py
  services/                catalog, cart, verification, orders, receipts, settings_store
  payments/                base, mock, phonepe, factory
  hardware/                scale (HX711/remote/simulated), scanner
  templates/ static/       UI, settings panel, kiosk fullscreen
deploy/
  DEPLOY.md                cloud deployment + cost analysis
  PI_SETUP.md              Raspberry Pi guide
  Caddyfile                TLS reverse proxy
  lightsail/               $5/mo stack + bootstrap
  aws/ec2-stack.yaml       CloudFormation alternative
scripts/
  install-pi.sh            one-shot Pi setup
  kiosk.sh                 fullscreen Chromium launcher
```

## Scaling note

The device bridge, active-session pointer and payment intents are in-process
state, so the app must run as **one worker / one replica**. That is fine for one
trolley. For many, move `app/devices.py` behind Redis or AWS IoT Core first — the
interface is deliberately narrow enough for that swap.
