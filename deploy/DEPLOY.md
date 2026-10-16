# Deploying Smart Trolley to the cloud

The app used to run entirely on the Raspberry Pi. It now splits in two:

```
        AWS                                    In the store
┌────────────────────────┐            ┌─────────────────────────────┐
│  FastAPI app + DB      │            │  Raspberry Pi 5             │
│  the whole UI + API    │◄──wss──────┤  pi_agent.py                │
│                        │  raw counts│    ├─ HX711 -> 4x50kg cells │
│  owns the calibration  ├──commands─►│    └─ camera (backup scan)  │
│  owns the catalogue    │            │                             │
└──────────┬─────────────┘            │  Chromium --kiosk ──────────┼─► shows the cloud UI
           │                          └─────────────────────────────┘
           └── https ─────────────────────────────────────────────────┘
                                       USB barcode gun = keyboard,
                                       types straight into the browser
```

Why this shape:

* The Pi has no database, no business logic and no catalogue. Reflashing a
  trolley takes minutes and loses nothing.
* The agent sends **raw HX711 counts**, not grams. Calibration lives in the
  cloud, so staff re-calibrate from the settings menu with no redeploy.
* The barcode gun keeps working untouched: it is a keyboard, and the keystrokes
  land in the kiosk browser.

---

## 1. Pick a hosting shape

| Option | Good for | WebSockets | Notes |
|---|---|---|---|
| **Lightsail `nano` + Compose + Caddy** | **this prototype** | yes | $5/mo flat, includes a static IP and 512 GB transfer. SQLite instead of Postgres. See §2. |
| EC2 + Docker Compose + Caddy | you already live in EC2 | yes | ~$17/mo on `t4g.small`. Template at `deploy/aws/ec2-stack.yaml`. |
| ECS Fargate + ALB + RDS | multi-store production | yes | ALB proxies WebSockets natively. Run **one task** (see the state warning below). |
| ~~App Runner~~ | — | **no** | No WebSocket support, and [AWS closed it to new customers from 30 April 2026](https://docs.aws.amazon.com/apprunner/latest/relnotes/relnotes.html). Don't. |
| ~~Lambda + DynamoDB~~ | — | via API GW only | Tempting, but see §7 — it is **not** cheaper here, and it fights the design. |

### Why Lightsail nano is enough

One trolley means one writer, and the app already runs a single uvicorn worker,
so there is no concurrency argument for Postgres. Dropping it for SQLite frees
~150 MB of RAM, which is what makes the 512 MB box viable. The bootstrap adds
2 GB of swap so a Docker build can spike without the OOM killer firing.

> **Run a single app instance.** The device bridge, the active-session pointer
> and the pending payment intents are in-process state (`app/devices.py`,
> `_state` in `app/main.py`). Two replicas would each see a different load cell.
> That is fine for one trolley. To scale to many, put `app/devices.py` behind
> Redis or AWS IoT Core first — the interface is already narrow enough for that.

---

## 2. Deploy to Lightsail (the path this project uses)

Everything below assumes an AWS CLI profile named `calqo`. Swap it for yours.

```bash
export AWS_PROFILE=calqo AWS_REGION=ap-south-1

# 1. instance ($5/mo nano, Ubuntu 24.04, bootstrap installs Docker + swap)
aws lightsail create-instances \
  --instance-names smart-trolley \
  --availability-zone ap-south-1a \
  --blueprint-id ubuntu_24_04 \
  --bundle-id nano_3_1 \
  --user-data "$(cat deploy/lightsail/user-data.sh)"

# 2. static IP
aws lightsail allocate-static-ip --static-ip-name smart-trolley-ip
aws lightsail attach-static-ip --static-ip-name smart-trolley-ip \
  --instance-name smart-trolley
IP=$(aws lightsail get-static-ip --static-ip-name smart-trolley-ip \
  --query 'staticIp.ipAddress' --output text)

# 3. firewall
aws lightsail put-instance-public-ports --instance-name smart-trolley --port-infos \
  'fromPort=22,toPort=22,protocol=TCP,cidrs=0.0.0.0/0' \
  'fromPort=80,toPort=80,protocol=TCP,cidrs=0.0.0.0/0' \
  'fromPort=443,toPort=443,protocol=TCP,cidrs=0.0.0.0/0'

# 4. SSH key
aws lightsail download-default-key-pair --query privateKeyBase64 --output text \
  > ~/.ssh/lightsail-ap-south-1.pem
chmod 600 ~/.ssh/lightsail-ap-south-1.pem
```

### HTTPS with no domain to buy

`sslip.io` resolves any dashed IP back to that IP, so `13-205-253-172.sslip.io`
points at `13.205.253.172`. Let's Encrypt issues for it happily, which means real
`https://` and `wss://` without owning a domain. Use `TROLLEY_DOMAIN=<dashed-ip>.sslip.io`.

Swap in a real domain later by pointing an A record at the static IP and changing
that one variable.

### Ship the code and start it

```bash
rsync -az --delete \
  -e "ssh -i ~/.ssh/lightsail-ap-south-1.pem -o StrictHostKeyChecking=no" \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude '*.db' --exclude '.env' \
  ./ ubuntu@"$IP":/opt/trolley/

ssh -i ~/.ssh/lightsail-ap-south-1.pem ubuntu@"$IP" \
  "umask 077 && cat > /opt/trolley/.env" <<EOF
TROLLEY_DOMAIN=$(echo $IP | tr '.' '-').sslip.io
PUBLIC_URL=https://$(echo $IP | tr '.' '-').sslip.io
SECRET_KEY=$(openssl rand -hex 32)
DEVICE_TOKEN=$(openssl rand -hex 32)
ADMIN_PIN=<pick 6 digits>
ALLOW_SIM_FALLBACK=false
EOF

ssh -i ~/.ssh/lightsail-ap-south-1.pem ubuntu@"$IP" \
  'cd /opt/trolley && docker compose --env-file .env \
     -f deploy/lightsail/docker-compose.yml up -d --build'
```

> **`--env-file` is required.** Compose looks for `.env` next to the compose
> file (`deploy/lightsail/`), but ours lives at the project root. Do *not* add
> `--project-directory`: the relative paths inside the compose file
> (`context: ../..`, `../Caddyfile`) resolve against the compose file's own
> directory and would break.

Two gotchas already fixed in this repo, worth knowing if you edit these files:

* **Lightsail user-data must be POSIX `sh`.** Lightsail prepends its own
  `#!/bin/sh` preamble and appends your script below it, so your shebang is
  ignored. `set -o pipefail` and `>(process substitution)` are bashisms that make
  dash abort immediately.
* **Don't interpolate a possibly-empty variable into a Caddy directive.**
  `tls {$ACME_EMAIL}` collapses to a bare `tls` and Caddy refuses to start.
  Automatic HTTPS needs no directive at all. The Caddyfile parser also reads
  directive names and brace-dollar tokens *inside comments*, so keep both out of
  comment text.

### Verify

```bash
curl -s https://<dashed-ip>.sslip.io/healthz
curl -s https://<dashed-ip>.sslip.io/api/device/status | python3 -m json.tool
```

`/healthz` should report `"mode":"cloud"`. Before the Pi connects,
`device.online` is `false` and scans are **refused** rather than silently passed —
that is `ALLOW_SIM_FALLBACK=false` doing its job.

### Cost

| Item | Monthly |
|---|---|
| Lightsail `nano_3_1` (2 vCPU, 512 MB, 20 GB SSD, 512 GB transfer) | $5.00 |
| Static IP (attached) | $0.00 |
| TLS certificate | $0.00 |
| **Total** | **$5.00** |

Upgrade path if you add trolleys: `aws lightsail create-instance-snapshot`, then
launch `micro_3_1` ($7, 1 GB) from it.

---

## 3. Deploy with the EC2 CloudFormation template

```bash
# generate the two secrets first
openssl rand -hex 32   # -> SECRET_KEY
openssl rand -hex 32   # -> DEVICE_TOKEN

aws cloudformation deploy \
  --stack-name smart-trolley \
  --template-file deploy/aws/ec2-stack.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      KeyName=my-keypair \
      DomainName=trolley.example.com \
      AcmeEmail=you@example.com \
      SshCidr=203.0.113.4/32 \
      SecretKey=<first secret> \
      DeviceToken=<second secret> \
      AdminPin=4821 \
      RepoUrl=https://github.com/you/smart-trolley.git

aws cloudformation describe-stacks --stack-name smart-trolley \
  --query 'Stacks[0].Outputs' --output table
```

Point a DNS **A record** for `trolley.example.com` at the `PublicIp` output.
Caddy gets a Let's Encrypt certificate on first request, so `https://` and
`wss://` start working within a minute.

Set `SshCidr` to your own IP. The default `0.0.0.0/0` exposes port 22 to the
internet.

### Without a domain

Leave `DomainName` blank and the stack serves plain HTTP on port 8000. Fine for
a first smoke test on a private network, but **the device token and admin PIN
travel unencrypted** — get a domain before a real demo.

---

## 4. Deploy by hand (any Linux box)

```bash
git clone <your repo> /opt/trolley && cd /opt/trolley

cat > .env <<'EOF'
TROLLEY_DOMAIN=trolley.example.com
ACME_EMAIL=you@example.com
SECRET_KEY=<openssl rand -hex 32>
DEVICE_TOKEN=<openssl rand -hex 32>
POSTGRES_PASSWORD=<a password>
ADMIN_PIN=4821
ALLOW_SIM_FALLBACK=false
EOF
chmod 600 .env

docker compose up -d --build
curl -s https://trolley.example.com/healthz
```

Local check with no TLS: `docker compose up app db` then hit
`http://localhost:8000`.

### Using RDS instead of the bundled Postgres

Point `DATABASE_URL` at the instance and drop the `db` service:

```
DATABASE_URL=postgresql+psycopg://user:pass@mydb.abc123.ap-south-1.rds.amazonaws.com:5432/trolley
```

Tables are created on startup with `Base.metadata.create_all`. There are no
migrations, so **`create_all` will not alter existing tables** — if you change a
model later, add Alembic or recreate the schema.

---

## 5. Set up the trolley

Full walkthrough with wiring diagrams, calibration and the staff
product-registration flow: **[PI_SETUP.md](PI_SETUP.md)**.

The short version, on the Pi with the repo cloned:

```bash
./scripts/install-pi.sh https://<your-host> <DEVICE_TOKEN>
```

That installs the venv and hardware deps, writes `.env` with
`HARDWARE_MODE=cloud`, registers the `trolley-agent` systemd service, and wires
the kiosk browser into autostart (labwc, wayfire and LXDE all covered).

Then:

```bash
# 1. confirm the load cell is alive
./.venv/bin/python calibrate_scale.py diagnose

# 2. calibrate with a known weight (1 kg is ideal)
./.venv/bin/python calibrate_scale.py calibrate

# 3. start streaming
sudo systemctl start trolley-agent
journalctl -u trolley-agent -f

# 4. bring up the fullscreen browser
sudo reboot
```

Verify from anywhere:

```bash
curl -s https://trolley.example.com/api/device/status | python3 -m json.tool
```

`device.online: true` means readings are arriving. The shop screen also shows a
red **⚠ scale offline** badge whenever they stop.

### Calibrating while the agent is running

Only one process can own GPIO 5/6. Rather than stopping the agent, calibrate
through the API:

```bash
python calibrate_scale.py calibrate --remote auto --pin <ADMIN_PIN>
```

Or just use **⚙ Settings → Scale** in the browser, which does the same thing.

---

## 6. Security checklist

| Item | Why |
|---|---|
| `DEVICE_TOKEN` set on **both** sides | Without it the cloud rejects the agent. With `HARDWARE_MODE=cloud` and no token, every device request 401s by design. |
| `SECRET_KEY` set | Signs the admin cookie. Missing, and admin sessions reset on every restart. |
| `ADMIN_PIN` changed from `1234` | It gates product registration and calibration. The app warns at boot and in the UI while it is default. |
| HTTPS in front | The token is a bearer secret and the PIN is posted in a form body. |
| `ALLOW_SIM_FALLBACK=false` | Otherwise a disconnected trolley silently falls back to software weights and the anti-theft check stops being real. |
| `SshCidr` narrowed | The template default is the whole internet. |

The staff settings menu is PIN-protected, which suits a terminal inside a store.
It is not a user-account system — don't expose this app to the open internet as
a multi-tenant service without adding real auth.

---

## 7. Why not Lambda + DynamoDB

The admin endpoints really are plain CRUD, so serverless looks obvious. It isn't,
because the CRUD is not what costs money here.

Two things in this app are stateful and continuous:

* **`/ws/device`** — the Pi streams raw HX711 counts, 4/sec by default
* **`wait_for_settle()`** — blocks up to 6 s polling the scale ~7×/sec, waiting
  for the basket weight to stabilise before deciding verified vs mismatch

Lambda cannot hold a WebSocket. API Gateway holds it and
[invokes Lambda once per message](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-websocket-api-overview.html),
which turns a continuous stream into billable events. At 4 Hz, one trolley,
12 h/day:

| | Volume/month | Cost |
|---|---|---|
| Agent → cloud | 5.2M msgs | API GW $5.20 |
| Cloud → browser | 2.2M msgs | API GW $2.16 |
| Lambda | 5.2M invokes | ~$2 |
| DynamoDB writes | 5.2M | ~$6.50 |
| **Total** | | **~$16–18** |
| **Lightsail nano (what we run)** | | **$5.00** |

API Gateway WebSockets are
[$1.00/million messages + $0.25/million connection-minutes](https://aws.amazon.com/api-gateway/pricing/).
So the rewrite costs *more* than the box it replaces.

**The cost lever is the streaming rate, not the compute model.** Make the agent
stream only while a scan is in flight (~4 s at 10 Hz, 500 scans/day ≈ 600k
msgs/month) and serverless lands inside the free tier. That optimisation is worth
doing regardless — it also cuts CPU on the current box — and it is the honest
prerequisite for any serverless move.

What it would still cost you: `wait_for_settle` becomes an event-driven state
machine, `catalog.py`/`cart.py` come off SQLAlchemy (DynamoDB has no substring
match for `search()`, no filtered `COUNT` for `counts()`), and 1–3 s cold starts
land on the shopper's scan latency. One genuine win: DynamoDB conditional writes
would fix the read-then-write race in `orders.mark_order_paid`.

Verdict for a prototype: stay on the $5 box.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `⚠ scale offline` badge | Agent not running or can't reach the cloud. `journalctl -u trolley-agent -f`. |
| Agent logs `401` | `DEVICE_TOKEN` mismatch between the Pi's `.env` and the cloud. |
| `GPIO 5/6 unavailable` | Another process owns the pins — usually the agent. Stop it, or use `--remote`. |
| Weights read 0 or wild | Not calibrated. `calibrate_scale.py diagnose` then `calibrate`. |
| Everything verifies, even theft | The scale fell back to software mode. Check `/api/device/status` and set `ALLOW_SIM_FALLBACK=false`. |
| Scans do nothing | The kiosk browser lost focus. The page re-focuses the hidden input every 1.5 s; tap the screen once. |
| Camera scans ignored | `libzbar0` missing, or `CAMERA_ENABLED=false`. The USB gun is unaffected. |
| Receipts vanish after redeploy | `data/receipts` is a container path. The compose file mounts a volume; on ECS use EFS or switch to S3. |
