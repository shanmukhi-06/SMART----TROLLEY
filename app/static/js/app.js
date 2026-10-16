/* Smart Trolley shopping page.

   - captures the USB barcode scanner (keyboard-wedge) globally
   - sends scans to the API, renders the cart + weight-verification feedback
   - live basket weight over WebSocket (works the same whether the load cell is
     on this machine or streaming up to the cloud from pi_agent.py)
   - scan feedback is on-screen: a colour flash plus an optional beep played by
     the browser. There is no buzzer or LED wired to the trolley.
   - fullscreen / kiosk handling

   Anything the settings panel (admin.js) needs is exposed on window.Trolley. */

const $ = (id) => document.getElementById(id);
const rupee = (n) => '₹' + Number(n).toFixed(2);
const CFG = window.TROLLEY || { simulation: true, mode: 'simulation', beep: true };

// ---------------------------------------------------------------------------
// Feedback: toast + colour flash + beep (replaces the old buzzer/LED)
// ---------------------------------------------------------------------------
function toast(msg, kind = 'info', ms = 2600) {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function flash(kind) {
  const el = $('flash');
  if (!el) return;
  el.className = 'flash show ' + kind;
  setTimeout(() => { el.className = 'flash'; }, 420);
}

let audioCtx = null;
function beep(kind) {
  if (!CFG.beep) return;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    audioCtx = audioCtx || new Ctx();
    if (audioCtx.state === 'suspended') audioCtx.resume();

    // ok: one short high chirp. alert: three low pulses.
    const pattern = kind === 'ok' ? [[880, 0, 0.09]]
                                  : [[240, 0, 0.12], [240, 0.18, 0.12], [240, 0.36, 0.12]];
    pattern.forEach(([freq, at, dur]) => {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'square';
      osc.frequency.value = freq;
      gain.gain.value = 0.045;
      osc.connect(gain).connect(audioCtx.destination);
      const t0 = audioCtx.currentTime + at;
      osc.start(t0);
      osc.stop(t0 + dur);
    });
  } catch (_) { /* audio is a nicety, never break the scan flow */ }
}

/** One call for "tell the shopper what just happened". */
function signal(kind) {
  if (kind !== 'ok' && kind !== 'alert') return;
  flash(kind);
  beep(kind);
}

// ---------------------------------------------------------------------------
// Cart rendering
// ---------------------------------------------------------------------------
function initial(name) { return (name || '?').trim().charAt(0).toUpperCase(); }

function renderCart(cart) {
  if (!cart) return;
  const list = $('itemsList');
  list.innerHTML = '';
  $('emptyState').style.display = cart.count ? 'none' : 'block';

  cart.items.forEach((it) => {
    const li = document.createElement('li');
    li.className = 'item';
    const flag = it.verify_status === 'mismatch'
      ? '<span class="badge badge-warn" style="margin-left:8px">weight mismatch</span>'
      : (it.verify_status === 'verified'
          ? '<span class="badge badge-ok" style="margin-left:8px">verified</span>' : '');
    li.innerHTML = `
      <div class="thumb">${initial(it.name)}</div>
      <div class="grow">
        <div class="name">${escapeHtml(it.name)}${flag}</div>
        <div class="meta">${rupee(it.unit_price)} ${it.weighed_g ? '' : '× ' + it.qty}</div>
      </div>
      <div class="price">${rupee(it.line_total)}</div>
      <button class="rm" title="Remove" data-id="${it.id}">✕</button>`;
    list.appendChild(li);
  });

  list.querySelectorAll('.rm').forEach((b) =>
    b.addEventListener('click', () => removeItem(b.dataset.id)));

  $('countBadge').textContent = '🛒 ' + cart.count + ' item' + (cart.count === 1 ? '' : 's');
  $('sumSubtotal').textContent = rupee(cart.subtotal);
  $('sumTax').textContent = rupee(cart.tax);
  $('sumTotal').textContent = rupee(cart.total);

  $('mismatchBanner').classList.toggle('show', cart.has_mismatch);
  $('checkoutBtn').disabled = cart.count === 0 || cart.has_mismatch;
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

// ---------------------------------------------------------------------------
// Scan routing
//
// The USB scanner is a keyboard. Normally its keystrokes land in a hidden input
// and go straight to the cart. The settings panel needs the same scans instead
// (to register/look up a product), so it "claims" the scanner: while a claim is
// active we stop stealing focus, and the panel's own visible input receives the
// keystrokes.
// ---------------------------------------------------------------------------
const scanBox = $('scanInput');
let scanClaim = null;

function claimScanner(label) {
  scanClaim = label || 'panel';
}
function releaseScanner() {
  scanClaim = null;
  keepFocus();
}
function scannerClaimed() { return scanClaim !== null; }

function keepFocus() {
  if (scannerClaimed()) return;
  if (document.querySelector('.modal-back.show')) return;
  const active = document.activeElement;
  // never yank focus out of something the shopper is typing in
  if (active && active !== scanBox &&
      (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' ||
       active.tagName === 'SELECT')) return;
  if (active !== scanBox) scanBox.focus();
}

document.addEventListener('click', keepFocus);
setInterval(keepFocus, 1500);

scanBox.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    const code = scanBox.value.trim();
    scanBox.value = '';
    sendScan(code);
  }
});

// ---------------------------------------------------------------------------
// Scanning
// ---------------------------------------------------------------------------
async function sendScan(barcode) {
  if (!barcode) return;
  try {
    const res = await fetch('/api/scan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ barcode })
    });
    if (res.status === 401) { window.location = '/'; return; }
    handleScanResult(await res.json());
  } catch (_) {
    signal('alert');
    toast('Network error while scanning', 'err');
  }
}

function handleScanResult(data) {
  if (data.cart) renderCart(data.cart);
  signal(data.feedback);

  if (data.ok) {
    if (data.verify_status === 'verified') {
      const note = data.weight_checked === false ? ' (no weight on file)' : '';
      toast(`Added ${data.name} ✓${note}`, 'ok');
    } else {
      toast(`${data.name}: weight mismatch (expected ${data.expected_g}g, ` +
            `got ${data.measured_delta_g}g)`, 'err', 4200);
    }
    return;
  }

  switch (data.reason) {
    case 'unknown_barcode':
      toast('Unknown barcode — register it in ⚙ Settings → Add product', 'err', 4200);
      break;
    case 'is_produce': openProduce(); toast('Weigh this item on the scale', 'info'); break;
    case 'out_of_stock': toast(`${data.name} is out of stock`, 'err'); break;
    case 'scale_offline':
      toast('Trolley scale offline — cannot verify weight. Call staff.', 'err', 5000);
      break;
    case 'scale_error':
      toast('Scale read failed: ' + (data.detail || 'unknown error'), 'err', 5000);
      break;
    case 'empty_barcode': break;
    default: toast('Could not add item', 'err');
  }
}

async function removeItem(itemId) {
  const res = await fetch('/api/cart/remove', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ item_id: Number(itemId) })
  });
  const data = await res.json();
  if (data.cart) renderCart(data.cart);
  toast('Item removed', 'info', 1500);
}

// ---------------------------------------------------------------------------
// Manual / dev controls
// ---------------------------------------------------------------------------
$('manualScanBtn').addEventListener('click', () => {
  const v = $('manualBarcode').value.trim();
  $('manualBarcode').value = '';
  sendScan(v);
});
$('manualBarcode').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); $('manualScanBtn').click(); }
});
if ($('theftBtn')) {
  $('theftBtn').addEventListener('click', async () => {
    const res = await fetch('/api/sim/theft', { method: 'POST' });
    if (res.ok) toast('Theft armed: next scan will mismatch', 'info');
    else toast('Not available on real hardware', 'err');
  });
}

// ---------------------------------------------------------------------------
// Produce modal
// ---------------------------------------------------------------------------
function openProduce() { $('produceModal').classList.add('show'); }
function closeProduce() { $('produceModal').classList.remove('show'); keepFocus(); }
$('produceBtn').addEventListener('click', openProduce);
$('produceClose').addEventListener('click', closeProduce);

document.querySelectorAll('.produce-btn').forEach((b) => {
  b.addEventListener('click', async () => {
    const body = { product_id: Number(b.dataset.id) };
    // With a software scale there is nothing to weigh, so invent a plausible
    // amount to keep the demo flowing.
    if (CFG.simulation) body.sim_grams = 250 + Math.floor(Math.random() * 500);
    const res = await fetch('/api/produce/add', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    closeProduce();
    signal(data.feedback);
    if (data.ok) { renderCart(data.cart); toast(`Added ${data.name} ✓`, 'ok'); }
    else if (data.reason === 'no_weight') toast('Place the produce on the basket first', 'err');
    else toast('Could not weigh produce', 'err');
  });
});

// ---------------------------------------------------------------------------
// Checkout
// ---------------------------------------------------------------------------
$('checkoutBtn').addEventListener('click', async () => {
  const res = await fetch('/api/checkout', { method: 'POST' });
  const data = await res.json();
  if (data.ok) { window.location = '/checkout'; }
  else if (data.error === 'weight_mismatch') { toast(data.message, 'err', 4000); }
  else if (data.error === 'empty_cart') { toast('Your basket is empty', 'info'); }
  else { toast('Checkout failed', 'err'); }
});

// ---------------------------------------------------------------------------
// Fullscreen / kiosk
// ---------------------------------------------------------------------------
function isFullscreen() {
  return !!(document.fullscreenElement || document.webkitFullscreenElement);
}

async function toggleFullscreen() {
  try {
    if (isFullscreen()) {
      await (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    } else {
      const el = document.documentElement;
      await (el.requestFullscreen || el.webkitRequestFullscreen).call(el);
    }
  } catch (err) {
    toast('Fullscreen was blocked by the browser', 'err');
  }
  syncFullscreenBtn();
}

function syncFullscreenBtn() {
  const btn = $('fullscreenBtn');
  if (!btn) return;
  const on = isFullscreen();
  btn.textContent = on ? '⤡' : '⛶';
  btn.title = on ? 'Exit fullscreen' : 'Fullscreen';
  document.body.classList.toggle('is-fullscreen', on);
}

$('fullscreenBtn').addEventListener('click', toggleFullscreen);
document.addEventListener('fullscreenchange', syncFullscreenBtn);
document.addEventListener('webkitfullscreenchange', syncFullscreenBtn);

// Kiosk mode: browsers only allow fullscreen from a user gesture, so arm it on
// the first tap/keypress. Chromium launched with --kiosk is already fullscreen,
// in which case this is a no-op.
if (CFG.kiosk) {
  document.body.classList.add('kiosk');
  const arm = () => {
    if (!isFullscreen()) toggleFullscreen();
    document.removeEventListener('pointerdown', arm);
    document.removeEventListener('keydown', arm);
  };
  document.addEventListener('pointerdown', arm, { once: false });
  document.addEventListener('keydown', arm, { once: false });
}

// ---------------------------------------------------------------------------
// Live weight via WebSocket
// ---------------------------------------------------------------------------
let latestGrams = null;

function setDeviceBadge(offline, label) {
  const badge = $('deviceBadge');
  if (!badge) return;
  badge.style.display = offline ? 'inline-flex' : 'none';
  if (offline && label) badge.textContent = label;
}

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'weight') {
      latestGrams = msg.grams;
      $('weightBadge').textContent = msg.grams == null
        ? '⚖ —' : '⚖ ' + Math.round(msg.grams) + ' g';
      if (msg.source === 'offline') setDeviceBadge(true, '⚠ scale offline');
      else if (msg.source === 'fallback') setDeviceBadge(true, '⚠ scale offline (demo weights)');
      else setDeviceBadge(false);
      document.dispatchEvent(new CustomEvent('trolley:weight', { detail: msg }));
    } else if (msg.type === 'scan') {
      // pushed by the camera scanner (local) or the Pi agent (cloud)
      handleScanResult({ ...msg.result, cart: msg.cart || msg.result.cart });
    }
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}

// ---------------------------------------------------------------------------
// Public surface for admin.js
// ---------------------------------------------------------------------------
window.Trolley = {
  cfg: CFG,
  toast, signal, beep, flash,
  claimScanner, releaseScanner, scannerClaimed, keepFocus,
  toggleFullscreen, isFullscreen,
  latestWeight: () => latestGrams,
  escapeHtml
};

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
(async function boot() {
  try {
    const res = await fetch('/api/cart');
    if (res.ok) {
      const data = await res.json();
      if (data.ok) renderCart(data.cart);
    }
  } catch (_) { /* offline at boot; the WS reconnect loop will recover */ }
  connectWS();
  syncFullscreenBtn();
  keepFocus();
})();
