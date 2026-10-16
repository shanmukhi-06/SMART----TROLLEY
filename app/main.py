"""FastAPI application: pages, JSON API, device bridge and live WebSocket feed.

Deployment shapes (see app/config.py)
-------------------------------------
* HARDWARE_MODE=cloud      this process runs on AWS. It never touches GPIO;
                           `pi_agent.py` on the trolley streams raw load-cell
                           counts in over /ws/device and receives commands back.
* HARDWARE_MODE=local      app + hardware together on the Pi (original setup).
* HARDWARE_MODE=simulation laptop demo, software scale.

Threading note: the weight-settle check blocks for up to ~6 s. Every scan is
therefore handed to a worker thread — if it ran on the event loop it would
freeze the WebSocket that delivers the very readings it is waiting for.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import security
from app.config import settings
from app.database import SessionLocal, get_db, init_db
from app.devices import bridge
from app.hardware.scale import (
    RemoteScale,
    SimulatedScale,
    get_scale,
    reload_calibration,
    scale_health,
)
from app.hardware.scanner import get_scanner
from app.models import OrderStatus, SessionStatus, ShoppingSession
from app.payments.factory import get_provider
from app.payments.mock import MockPaymentProvider
from app.seed import seed_if_empty
from app.security import require_admin, require_device
from app.services import cart, catalog, orders, receipts, settings_store, verification
from app.services.catalog import CatalogError

# ---- lightweight runtime state (single-cart prototype) --------------------
_state = {
    "current_session_id": None,   # active shopper session
    "payment_intents": {},        # order_ref -> PaymentIntent
    "force_theft": False,         # sim toggle: next scan mismatches
    "calibration_zero": None,     # raw counts captured by the calibrate wizard
    "loop": None,                 # main event loop, for thread -> loop hops
}
_ws_clients: set[WebSocket] = set()


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    seed_if_empty()
    _state["loop"] = asyncio.get_running_loop()

    get_scale()  # build the scale singleton + load stored calibration

    # Barcodes decoded by the Pi agent's camera enter the same scan pipeline.
    bridge.set_scan_sink(_on_agent_scan)

    # Only the machine physically holding the camera runs the capture loop.
    if not settings.is_cloud:
        get_scanner(on_scan=_on_agent_scan).start()

    print(f"[app] ready | mode={settings.mode} | payment={settings.payment_provider}")
    for warning in security.startup_warnings():
        print(f"[app] WARNING: {warning}")

    yield

    bridge.set_scan_sink(None)
    if not settings.is_cloud:
        get_scanner(on_scan=_on_agent_scan).stop()
    get_scale().close()


app = FastAPI(title="Smart Shopping Trolley", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _current_session(db: Session, request: Request) -> ShoppingSession | None:
    sid = request.cookies.get("sid")
    if not sid:
        return None
    try:
        session = db.get(ShoppingSession, int(sid))
    except (TypeError, ValueError):
        return None
    if session and session.status == SessionStatus.active:
        return session
    return None


def _page_ctx(request: Request, **extra) -> dict:
    """Common template context: branding, mode flags, kiosk + admin state."""
    ctx = {
        "settings": settings,
        "mode": settings.mode,
        "is_cloud": settings.is_cloud,
        "is_simulated": settings.is_simulated,
        "kiosk": settings.kiosk_mode or request.query_params.get("kiosk") == "1",
        "is_admin": security.is_admin(request),
    }
    ctx.update(extra)
    return ctx


async def _broadcast(message: dict) -> None:
    dead = []
    payload = json.dumps(message)
    for ws in _ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


def _software_scale(scale) -> SimulatedScale | None:
    """The software scale we should nudge, or None when weight is real.

    In simulation we fake the shopper dropping the item in. In cloud mode with
    the trolley offline we do the same through the fallback scale, so the demo
    still works — but `_weight_source()` reports it so the UI can say the
    anti-theft check is degraded.
    """
    if isinstance(scale, SimulatedScale):
        return scale
    if isinstance(scale, RemoteScale) and scale.using_fallback:
        return scale.fallback
    return None


def _weight_source(scale) -> str:
    if isinstance(scale, SimulatedScale):
        return "simulated"
    if isinstance(scale, RemoteScale):
        return "device" if scale.online else (
            "fallback" if settings.allow_sim_fallback else "offline"
        )
    return "load_cell"


# ---------------------------------------------------------------------------
# Scan pipeline (runs in a worker thread: it blocks on the load cell)
# ---------------------------------------------------------------------------
def _scan_sync(session_id: int, barcode: str) -> dict:
    """Add a scanned item, weight-verify it, and return the result + cart.

    Owns its own DB session so it is safe to call from a thread.
    """
    db = SessionLocal()
    try:
        session = db.get(ShoppingSession, session_id)
        if not session or session.status != SessionStatus.active:
            return {"ok": False, "reason": "no_session", "feedback": "alert"}

        scale = get_scale()
        source = _weight_source(scale)

        if source == "offline":
            return {
                "ok": False,
                "reason": "scale_offline",
                "feedback": "alert",
                "weight_source": source,
                "cart": cart.summarise(db, session.id),
            }

        try:
            baseline = scale.get_grams()
        except Exception as exc:
            return {
                "ok": False, "reason": "scale_error", "detail": str(exc),
                "feedback": "alert", "weight_source": source,
                "cart": cart.summarise(db, session.id),
            }

        item, product, error = cart.add_scanned_item(db, session, barcode)
        if error == "unknown_barcode":
            return {"ok": False, "reason": "unknown_barcode", "barcode": barcode,
                    "feedback": "alert", "cart": cart.summarise(db, session.id)}
        if error == "is_produce":
            return {"ok": False, "reason": "is_produce", "product_id": product.id,
                    "name": product.name, "feedback": "info",
                    "cart": cart.summarise(db, session.id)}
        if error == "out_of_stock":
            return {"ok": False, "reason": "out_of_stock", "name": product.name,
                    "feedback": "alert", "cart": cart.summarise(db, session.id)}

        expected = product.weight_g or 0.0

        # Software scale: mimic the shopper dropping the item in the basket.
        sim = _software_scale(scale)
        if sim is not None:
            placed = expected * (2.0 if _state["force_theft"] else 1.0)
            sim.sim_add_weight(placed)
            _state["force_theft"] = False

        try:
            settled = verification.wait_for_settle(scale, baseline,
                                                   expected_g=expected)
        except Exception as exc:
            # device dropped off mid-scan: flag the item rather than crash
            cart.set_verify_status(db, item.id, "pending")
            return {
                "ok": False, "reason": "scale_error", "detail": str(exc),
                "feedback": "alert", "weight_source": _weight_source(scale),
                "cart": cart.summarise(db, session.id),
            }
        delta = settled - baseline
        status = verification.classify(delta, expected)

        # level-2 camera cross-check (stubbed until a model is wired in)
        if status == "verified" and not verification.camera_confirms_addition(True):
            status = "mismatch"

        cart.set_verify_status(db, item.id, status)

        return {
            "ok": True,
            "item_id": item.id,
            "name": item.name,
            "verify_status": status,
            "expected_g": round(expected, 1),
            "measured_delta_g": round(delta, 1),
            "weight_source": source,
            "weight_checked": expected > 0,
            "feedback": "ok" if status == "verified" else "alert",
            "cart": cart.summarise(db, session.id),
        }
    finally:
        db.close()


def _on_agent_scan(barcode: str) -> None:
    """Camera scan from the Pi agent (or the local camera thread).

    Called from a non-async thread, so hop onto the event loop explicitly.
    `asyncio.get_event_loop()` here would raise in a worker thread — the
    original bug that made camera scans never reach the browser.
    """
    loop = _state.get("loop")
    if loop is None:
        return
    asyncio.run_coroutine_threadsafe(_handle_agent_scan(barcode), loop)


async def _handle_agent_scan(barcode: str) -> None:
    sid = _state["current_session_id"]
    if not sid:
        return
    result = await asyncio.to_thread(_scan_sync, sid, barcode)
    await _broadcast({"type": "scan", "result": result,
                      "cart": result.get("cart")})


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def page_login(request: Request):
    return templates.TemplateResponse(request, "index.html", _page_ctx(request))


@app.get("/shop", response_class=HTMLResponse)
def page_shop(request: Request, db: Session = Depends(get_db)):
    session = _current_session(db, request)
    if not session:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request,
        "shop.html",
        _page_ctx(
            request,
            session=session,
            produce=[catalog.product_to_dict(p) for p in catalog.list_produce(db)],
        ),
    )


@app.get("/checkout", response_class=HTMLResponse)
def page_checkout(request: Request, db: Session = Depends(get_db)):
    session = _current_session(db, request)
    if not session:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request, "checkout.html", _page_ctx(request, session=session)
    )


@app.get("/receipt/{order_ref}", response_class=HTMLResponse)
def page_receipt(order_ref: str, db: Session = Depends(get_db)):
    order = orders.get_order_by_ref(db, order_ref)
    if not order:
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(receipts.render_html(order, actions=True))


@app.post("/api/receipt/print/{order_ref}")
def api_receipt_print(order_ref: str, db: Session = Depends(get_db)):
    """Print a paid receipt on the connected thermal printer.

    Shopper-facing (no admin PIN): the kiosk's own "Print bill" button calls
    this. Only *paid* orders can be printed, so it cannot be used to spool
    arbitrary paper.
    """
    order = orders.get_order_by_ref(db, order_ref)
    if not order:
        return JSONResponse({"ok": False, "error": "order_not_found"}, status_code=404)
    if order.status != OrderStatus.paid:
        return JSONResponse({"ok": False, "error": "order_not_paid"}, status_code=409)

    try:
        from app.hardware.printer import get_printer

        get_printer().print_receipt(order)
        return {"ok": True, "printed": True, "order_ref": order_ref}
    except Exception as exc:
        print(f"[receipt] print failed for {order_ref}: {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/healthz")
def healthz():
    """Load-balancer / ECS health check."""
    return {
        "ok": True,
        "mode": settings.mode,
        "device_online": bridge.is_online() if settings.is_cloud else None,
    }


# ---------------------------------------------------------------------------
# Shopper JSON API
# ---------------------------------------------------------------------------
@app.post("/api/session/start")
async def api_session_start(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    mobile = (body.get("mobile") or "").strip()
    email = (body.get("email") or "").strip()
    if len(mobile) < 10 or "@" not in email:
        return JSONResponse(
            {"ok": False, "error": "Enter a valid mobile number and email."},
            status_code=400,
        )

    session = ShoppingSession(mobile=mobile, email=email, status=SessionStatus.active)
    db.add(session)
    db.commit()
    db.refresh(session)

    _state["current_session_id"] = session.id
    # tare for a fresh, empty basket (blocking read -> thread)
    await asyncio.to_thread(_tare_scale)

    resp = JSONResponse({"ok": True, "session_id": session.id, "redirect": "/shop"})
    resp.set_cookie("sid", str(session.id), httponly=True, samesite="lax")
    return resp


def _tare_scale() -> dict:
    """Zero the basket. On cloud we also tell the agent, for its own display."""
    scale = get_scale()
    try:
        offset = scale.tare()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if settings.is_cloud:
        bridge.send_command("tare")
    return {"ok": True, "offset": offset, "grams": scale.get_grams()}


@app.post("/api/scan")
async def api_scan(request: Request):
    sid = request.cookies.get("sid")
    if not sid:
        return JSONResponse({"ok": False, "error": "no_session"}, status_code=401)
    body = await request.json()
    barcode = catalog.normalise_barcode(body.get("barcode") or "")
    if not barcode:
        return {"ok": False, "reason": "empty_barcode", "feedback": "alert"}
    # blocking weight check -> worker thread, keeps the WS feed alive
    result = await asyncio.to_thread(_scan_sync, int(sid), barcode)
    if result.get("reason") == "no_session":
        return JSONResponse({"ok": False, "error": "no_session"}, status_code=401)
    return result


@app.get("/api/cart")
def api_cart(request: Request, db: Session = Depends(get_db)):
    session = _current_session(db, request)
    if not session:
        return JSONResponse({"ok": False, "error": "no_session"}, status_code=401)
    return {"ok": True, "cart": cart.summarise(db, session.id)}


@app.post("/api/cart/remove")
async def api_cart_remove(request: Request, db: Session = Depends(get_db)):
    session = _current_session(db, request)
    if not session:
        return JSONResponse({"ok": False, "error": "no_session"}, status_code=401)
    body = await request.json()
    ok = cart.remove_item(db, session.id, int(body.get("item_id", 0)))
    # keep a software basket consistent with what should be in it
    sim = _software_scale(get_scale())
    if sim is not None:
        sim.sim_set_weight(cart.expected_total_grams(db, session.id))
    return {"ok": ok, "cart": cart.summarise(db, session.id)}


@app.get("/api/produce")
def api_produce(db: Session = Depends(get_db)):
    return {"ok": True,
            "items": [catalog.product_to_dict(p) for p in catalog.list_produce(db)]}


@app.get("/api/weight")
async def api_weight():
    scale = get_scale()
    try:
        grams = await asyncio.to_thread(scale.get_grams)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "source": _weight_source(scale)}
    return {"ok": True, "grams": grams, "source": _weight_source(scale)}


@app.post("/api/produce/add")
async def api_produce_add(request: Request, db: Session = Depends(get_db)):
    session = _current_session(db, request)
    if not session:
        return JSONResponse({"ok": False, "error": "no_session"}, status_code=401)
    body = await request.json()
    product_id = int(body.get("product_id", 0))
    scale = get_scale()
    sim = _software_scale(scale)

    if sim is not None and body.get("sim_grams"):
        weighed = float(body["sim_grams"])
        sim.sim_add_weight(weighed)
    else:
        baseline = cart.expected_total_grams(db, session.id)
        try:
            current = await asyncio.to_thread(scale.get_grams)
        except Exception as exc:
            return {"ok": False, "reason": "scale_error", "detail": str(exc),
                    "feedback": "alert"}
        weighed = max(0.0, current - baseline)

    item, product, error = cart.add_produce_item(db, session, product_id, weighed)
    if error:
        return {"ok": False, "reason": error, "feedback": "alert"}
    return {"ok": True, "name": item.name, "weighed_g": round(weighed, 1),
            "feedback": "ok", "cart": cart.summarise(db, session.id)}


# ---- Checkout & payment ----------------------------------------------------
@app.post("/api/checkout")
async def api_checkout(request: Request, db: Session = Depends(get_db)):
    session = _current_session(db, request)
    if not session:
        return JSONResponse({"ok": False, "error": "no_session"}, status_code=401)

    summary = cart.summarise(db, session.id)
    if summary["count"] == 0:
        return {"ok": False, "error": "empty_cart"}
    if summary["has_mismatch"]:
        return {"ok": False, "error": "weight_mismatch",
                "message": "Please resolve the flagged item before checkout."}

    order = orders.create_order_from_session(db, session)
    provider = get_provider()
    intent = provider.create_payment(order.order_ref, float(order.total),
                                     note=f"{settings.store_name} {order.order_ref}")
    _state["payment_intents"][order.order_ref] = intent

    return {
        "ok": True,
        "order_ref": order.order_ref,
        "total": float(order.total),
        "provider": intent.provider,
        "qr": intent.qr_data_uri,
        "qr_payload": intent.qr_payload,
    }


@app.get("/api/payment/status/{order_ref}")
def api_payment_status(order_ref: str, db: Session = Depends(get_db)):
    intent = _state["payment_intents"].get(order_ref)
    order = orders.get_order_by_ref(db, order_ref)
    if not order:
        return JSONResponse({"ok": False, "error": "unknown_order"}, status_code=404)
    if order.status == OrderStatus.paid:
        return {"ok": True, "status": "paid", "order_ref": order_ref}

    status = get_provider().check_status(intent) if intent else "pending"
    if status == "paid":
        provider_name = order.payment_provider or (intent.provider if intent else "mock")
        order = orders.mark_order_paid(
            db, order, provider_name,
            intent.payment_ref if intent else "",
        )
        delivery = receipts.deliver(order)
        order.receipt_sent = bool(delivery["emailed"] or delivery["local_path"])
        db.commit()
        _state["current_session_id"] = None
    return {"ok": True, "status": status, "order_ref": order_ref}


@app.post("/api/payment/confirm/{order_ref}")
def api_payment_confirm(order_ref: str):
    """Demo helper: force the MOCK provider to mark this order paid now."""
    provider = get_provider()
    if isinstance(provider, MockPaymentProvider):
        provider.mark_paid(order_ref)
        return {"ok": True}
    return JSONResponse({"ok": False, "error": "not_mock"}, status_code=400)


@app.post("/api/sim/theft")
def api_sim_theft():
    """Arm a one-shot 'theft': the next scanned item adds the wrong weight."""
    if _software_scale(get_scale()) is None:
        return JSONResponse(
            {"ok": False, "error": "not_available_on_real_hardware"}, status_code=400
        )
    _state["force_theft"] = True
    return {"ok": True, "armed": True}


# ---------------------------------------------------------------------------
# Device bridge: the Pi agent talks to these
# ---------------------------------------------------------------------------
@app.websocket("/ws/device")
async def ws_device(ws: WebSocket):
    """Primary agent transport: readings up, commands down."""
    if not security.check_device_token(ws.query_params.get("token")):
        await ws.close(code=4401)
        return

    device_id = ws.query_params.get("device_id") or settings.device_id
    await ws.accept()
    bridge.on_connect(device_id, {"agent_version": ws.query_params.get("v", "")})
    print(f"[device] {device_id} connected")

    recv = asyncio.create_task(ws.receive_text())
    try:
        while True:
            done, _ = await asyncio.wait({recv}, timeout=0.2)
            if recv in done:
                raw = recv.result()
                recv = asyncio.create_task(ws.receive_text())
                _handle_device_message(device_id, raw)
            for cmd in bridge.drain_commands():
                await ws.send_text(json.dumps(cmd))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[device] {device_id} error: {exc}")
    finally:
        recv.cancel()
        bridge.on_disconnect()
        print(f"[device] {device_id} disconnected")


def _handle_device_message(device_id: str, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    kind = msg.get("type")
    if kind == "hello":
        bridge.on_connect(device_id, msg)
    elif kind == "reading":
        bridge.push_reading(msg.get("raw", 0.0), msg.get("grams"))
    elif kind == "scan":
        bridge.push_scan(catalog.normalise_barcode(msg.get("barcode", "")))
    elif kind == "error":
        bridge.push_error(msg.get("message", ""))
    elif kind == "pong":
        bridge.touch()


@app.post("/api/device/report")
async def api_device_report(request: Request, device_id: str = Depends(require_device)):
    """HTTP fallback for the agent when WebSockets are blocked by a proxy.

    Accepts a reading and/or scans, returns any pending commands.
    """
    body = await request.json()
    if "raw" in body:
        bridge.push_reading(body.get("raw", 0.0), body.get("grams"))
    if body.get("error"):
        bridge.push_error(body["error"])
    for code in body.get("scans", []) or []:
        bridge.push_scan(catalog.normalise_barcode(code))
    if body.get("hello"):
        bridge.on_connect(device_id, body)
    return {"ok": True, "commands": bridge.drain_commands()}


@app.get("/api/device/commands")
def api_device_commands(_: str = Depends(require_device), wait: float = 0.0):
    """Long-poll for commands (HTTP fallback path)."""
    cmds = bridge.wait_for_commands(min(wait, 30.0)) if wait else bridge.drain_commands()
    return {"ok": True, "commands": cmds}


@app.get("/api/device/status")
def api_device_status():
    """Read-only device health, used by the shop UI badge."""
    return {"ok": True, "mode": settings.mode, "device": bridge.status(),
            "scale": scale_health()}


# ---------------------------------------------------------------------------
# Staff settings menu API
# ---------------------------------------------------------------------------
@app.post("/api/admin/login")
async def api_admin_login(request: Request):
    body = await request.json()
    if not security.check_admin_pin(body.get("pin", "")):
        return JSONResponse({"ok": False, "error": "bad_pin"}, status_code=401)
    resp = JSONResponse({"ok": True, "hours": settings.admin_session_hours})
    security.set_admin_cookie(resp, security.issue_admin_token())
    return resp


@app.post("/api/admin/logout")
def api_admin_logout():
    resp = JSONResponse({"ok": True})
    security.clear_admin_cookie(resp)
    return resp


@app.get("/api/admin/session")
def api_admin_session(request: Request):
    return {
        "ok": True,
        "authenticated": security.is_admin(request),
        "default_pin": security.admin_pin_is_default(),
        "mode": settings.mode,
    }


@app.get("/api/admin/products")
def api_admin_products(request: Request, q: str = "", limit: int = 50,
                       _: bool = Depends(require_admin),
                       db: Session = Depends(get_db)):
    rows = catalog.search(db, q, limit=limit)
    return {"ok": True, "items": [catalog.product_to_dict(p) for p in rows],
            "counts": catalog.counts(db)}


@app.get("/api/admin/products/lookup")
def api_admin_lookup(barcode: str = "", _: bool = Depends(require_admin),
                     db: Session = Depends(get_db)):
    """Scan-to-identify: the store staff's 'what is this?' button."""
    code = catalog.normalise_barcode(barcode)
    if not code:
        return {"ok": False, "error": "empty_barcode"}
    product = catalog.find_by_barcode_any(db, code)
    if not product:
        return {"ok": True, "found": False, "barcode": code}
    return {
        "ok": True,
        "found": True,
        "barcode": code,
        "product": catalog.product_to_dict(product),
        "movements": catalog.recent_movements(db, product.id),
    }


@app.post("/api/admin/products")
async def api_admin_product_save(request: Request, _: bool = Depends(require_admin),
                                 db: Session = Depends(get_db)):
    body = await request.json()
    try:
        product, created = catalog.upsert_product(db, body)
    except CatalogError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "created": created,
            "product": catalog.product_to_dict(product)}


@app.post("/api/admin/products/{product_id}/stock")
async def api_admin_stock(product_id: int, request: Request,
                          _: bool = Depends(require_admin),
                          db: Session = Depends(get_db)):
    body = await request.json()
    try:
        if "delta" in body:
            product = catalog.add_stock(db, product_id, int(body["delta"]),
                                        note=body.get("note", ""))
        else:
            product = catalog.set_stock(db, product_id, body.get("stock_qty", 0),
                                        note=body.get("note", ""))
    except CatalogError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "product": catalog.product_to_dict(product)}


@app.post("/api/admin/products/{product_id}/active")
async def api_admin_active(product_id: int, request: Request,
                           _: bool = Depends(require_admin),
                           db: Session = Depends(get_db)):
    body = await request.json()
    try:
        product = catalog.set_active(db, product_id, bool(body.get("active", True)))
    except CatalogError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "product": catalog.product_to_dict(product)}


# ---- Scale tools (tare / calibrate) inside the settings menu ---------------
@app.get("/api/admin/scale")
async def api_admin_scale(_: bool = Depends(require_admin)):
    scale = get_scale()
    try:
        stats = await asyncio.to_thread(scale.read_stats, 10)
    except Exception as exc:
        stats = {"error": str(exc)}
    return {
        "ok": True,
        "reading": stats,
        "health": scale_health(),
        "calibration": settings_store.read_scale_calibration(),
        "device": bridge.status(),
        "capacity_g": settings.scale_capacity_g,
        "tolerance_g": settings.weight_tolerance_g,
        "zero_captured": _state["calibration_zero"] is not None,
    }


@app.post("/api/admin/scale/tare")
async def api_admin_tare(_: bool = Depends(require_admin)):
    result = await asyncio.to_thread(_tare_scale)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    # a tare is a new zero: persist it so a restart keeps it
    scale = get_scale()
    if not isinstance(scale, SimulatedScale):
        cal = scale.calibration
        settings_store.write_scale_calibration(
            cal["reference_unit"], cal["offset"], note="tare from settings menu"
        )
    return result


@app.post("/api/admin/scale/calibrate/zero")
async def api_admin_cal_zero(_: bool = Depends(require_admin)):
    """Step 1 of 2: capture the empty-basket raw reading."""
    scale = get_scale()
    if isinstance(scale, SimulatedScale):
        return JSONResponse(
            {"ok": False, "error": "No load cell attached (software scale)."},
            status_code=400,
        )
    try:
        stats = await asyncio.to_thread(scale.read_stats, 25)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    _state["calibration_zero"] = stats["raw"]
    return {"ok": True, "zero_raw": stats["raw"], "noise": stats["raw_stdev"],
            "next": "Place a known weight, then submit its weight in grams."}


@app.post("/api/admin/scale/calibrate/span")
async def api_admin_cal_span(request: Request, _: bool = Depends(require_admin)):
    """Step 2 of 2: with a known weight on the basket, compute the scale factor."""
    body = await request.json()
    zero = _state["calibration_zero"]
    if zero is None:
        return JSONResponse(
            {"ok": False, "error": "Capture the empty-basket zero first."},
            status_code=400,
        )
    try:
        known_g = float(body.get("known_grams", 0))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Enter a number of grams."},
                            status_code=400)
    if known_g <= 0:
        return JSONResponse({"ok": False, "error": "Reference weight must be > 0 g."},
                            status_code=400)

    scale = get_scale()
    try:
        stats = await asyncio.to_thread(scale.read_stats, 25)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    span = stats["raw"] - zero
    if abs(span) < 1:
        return JSONResponse(
            {"ok": False,
             "error": "The reading did not change. Check the load cell wiring "
                      "and that the weight is actually on the basket."},
            status_code=400,
        )

    reference_unit = span / known_g
    stored = settings_store.write_scale_calibration(
        reference_unit, zero, note=f"2-point via settings menu ({known_g:.0f} g)"
    )
    reload_calibration()
    _state["calibration_zero"] = None

    verify = await asyncio.to_thread(scale.get_grams)
    return {
        "ok": True,
        "calibration": stored,
        "reference_unit": reference_unit,
        "offset": zero,
        "counts_per_gram": round(reference_unit, 4),
        "measured_now_g": verify,
        "error_g": round(verify - known_g, 1),
    }


@app.post("/api/admin/scale/calibrate/manual")
async def api_admin_cal_manual(request: Request, _: bool = Depends(require_admin)):
    """Paste values straight from `calibrate_scale.py`."""
    body = await request.json()
    try:
        reference_unit = float(body.get("reference_unit"))
        offset = float(body.get("offset"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Both values must be numbers."},
                            status_code=400)
    if reference_unit == 0:
        return JSONResponse({"ok": False, "error": "Reference unit cannot be 0."},
                            status_code=400)
    stored = settings_store.write_scale_calibration(
        reference_unit, offset, note="entered manually"
    )
    reload_calibration()
    return {"ok": True, "calibration": stored}


@app.get("/api/admin/diagnostics")
def api_admin_diagnostics(_: bool = Depends(require_admin),
                          db: Session = Depends(get_db)):
    return {
        "ok": True,
        "mode": settings.mode,
        "store": settings.store_name,
        "database": settings.database_url.split("://")[0],
        "payment_provider": settings.payment_provider,
        "camera_enabled": settings.camera_enabled,
        "scale": scale_health(),
        "device": bridge.status(),
        "calibration": settings_store.read_scale_calibration(),
        "catalogue": catalog.counts(db),
        "warnings": security.startup_warnings(),
        "kiosk_url": settings.effective_kiosk_url,
    }


# ---------------------------------------------------------------------------
# Printer
# ---------------------------------------------------------------------------
@app.get("/api/admin/printers")
def api_admin_printers(_: bool = Depends(require_admin)):
    """Scan for every printer transport the host can reach."""
    try:
        from app.hardware.printer import discover_printers

        return {"ok": True, "printers": [
            {"kind": p.kind, "name": p.name, "address": p.address, "detail": p.detail}
            for p in discover_printers()
        ]}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/api/admin/printer")
def api_admin_printer(_: bool = Depends(require_admin)):
    """Health + info for the current printer singleton."""
    try:
        from app.hardware.printer import printer_health

        return {"ok": True, "printer": printer_health()}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/admin/printer/print")
async def api_admin_printer_print(request: Request, _: bool = Depends(require_admin)):
    """Print a receipt for a given order ref, or print raw text."""
    body = await request.json()
    order_ref = (body.get("order_ref") or "").strip()
    text = body.get("text", "")
    try:
        from app.hardware.printer import get_printer

        printer = get_printer()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    if order_ref:
        try:
            from app.services.orders import get_order_by_ref
            from app.database import SessionLocal

            with SessionLocal() as db:
                order = get_order_by_ref(db, order_ref)
                if not order:
                    return JSONResponse({"ok": False, "error": "order_not_found"}, status_code=404)
                printer.print_receipt(order)
            return {"ok": True, "printed": True, "order_ref": order_ref}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    if text:
        try:
            printer.init()
            printer.print_text(text)
            printer.feed(2)
            printer.cut()
            return {"ok": True, "printed": True}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse({"ok": False, "error": "order_ref or text required"}, status_code=400)


# ---------------------------------------------------------------------------
# Admin printer settings
# ---------------------------------------------------------------------------
@app.post("/api/admin/printer/settings")
async def api_admin_printer_settings(request: Request, _: bool = Depends(require_admin)):
    body = await request.json()
    try:
        transport = (body.get("transport") or "").strip().lower()
        if transport and transport not in {"auto", "simulation", "usb", "bluetooth", "network", "remote"}:
            return JSONResponse({"ok": False, "error": "invalid transport"}, status_code=400)

        device = body.get("device")
        driver = body.get("driver")
        auto_print = body.get("auto_print")

        from app.services import settings_store
        stored = settings_store.write_printer_settings(
            transport=transport,
            device=device if device is not None else "",
            driver=driver if driver is not None else "",
            auto_print=auto_print,
        )

        from app.hardware import printer as printer_module
        printer_module.reset_printer()
        return {"ok": True, "applied": True, "settings": stored}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Browser WebSocket: live basket weight + agent-pushed scans
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_feed(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    scale = get_scale()
    try:
        while True:
            try:
                grams = await asyncio.to_thread(scale.get_grams)
                payload = {"type": "weight", "grams": grams,
                           "source": _weight_source(scale)}
            except Exception as exc:
                payload = {"type": "weight", "grams": None, "source": "offline",
                           "error": str(exc)}
            if settings.is_cloud:
                payload["device_online"] = bridge.is_online()
            await ws.send_text(json.dumps(payload))
            await asyncio.sleep(0.6)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _ws_clients.discard(ws)
