/* Store settings panel (staff only).

   Four tabs:
     Add product  scan a barcode, capture the unit weight off the real scale,
                  save. Re-scanning a known barcode edits it.
     Look up      scan to identify a product: price, stock, registered weight.
     Scale        live reading, tare, and the 2-point calibration wizard.
     System       fullscreen / kiosk controls and hardware diagnostics.

   The USB scanner is a keyboard, so while this panel is open it "claims" the
   scanner (see app.js) and keystrokes land in the panel's own input instead of
   the shopping cart. */

(function () {
  const T = window.Trolley;
  const $ = (id) => document.getElementById(id);
  const esc = T.escapeHtml;

  const modal = $('adminModal');
  const loginView = $('adminLogin');
  const panelView = $('adminPanel');

  let scalePoll = null;
  let currentTab = 'register';
  let editingId = null;

  // -- helpers -----------------------------------------------------------
  async function api(path, options) {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options
    });
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (res.status === 401 && data.detail === 'admin_required') {
      showLogin();
      throw new Error('locked');
    }
    return { res, data };
  }

  function setErr(id, msg) { const el = $(id); if (el) el.textContent = msg || ''; }

  const grams = (v) => (v == null ? '—' : Number(v).toFixed(1) + ' g');

  // -- open / close ------------------------------------------------------
  async function open() {
    modal.classList.add('show');
    T.claimScanner('admin');
    const { data } = await api('/api/admin/session');
    if (data.authenticated) showPanel();
    else showLogin(data.default_pin);
  }

  function close() {
    modal.classList.remove('show');
    stopScalePoll();
    T.releaseScanner();
  }

  function showLogin(defaultPin) {
    loginView.style.display = 'block';
    panelView.style.display = 'none';
    stopScalePoll();
    setErr('adminLoginErr', defaultPin
      ? 'Default PIN is 1234 — change ADMIN_PIN in .env before going live.' : '');
    setTimeout(() => $('adminPin').focus(), 50);
  }

  function showPanel() {
    loginView.style.display = 'none';
    panelView.style.display = 'block';
    $('adminModeBadge').textContent = T.cfg.mode;
    selectTab(currentTab);
    loadDiagnostics();
  }

  $('settingsBtn').addEventListener('click', open);
  document.querySelectorAll('[data-admin-close]').forEach((b) =>
    b.addEventListener('click', close));
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modal.classList.contains('show')) close();
  });

  // -- login -------------------------------------------------------------
  async function doLogin() {
    const pin = $('adminPin').value;
    if (!pin) { setErr('adminLoginErr', 'Enter the PIN'); return; }
    const { res, data } = await api('/api/admin/login', {
      method: 'POST', body: JSON.stringify({ pin })
    });
    $('adminPin').value = '';
    if (res.ok && data.ok) { setErr('adminLoginErr', ''); showPanel(); }
    else setErr('adminLoginErr', 'Wrong PIN');
  }
  $('adminLoginBtn').addEventListener('click', doLogin);
  $('adminPin').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); doLogin(); }
  });
  $('adminLogoutBtn').addEventListener('click', async () => {
    await api('/api/admin/logout', { method: 'POST' });
    showLogin();
  });

  // -- tabs --------------------------------------------------------------
  function selectTab(name) {
    currentTab = name;
    document.querySelectorAll('.tab').forEach((t) =>
      t.classList.toggle('is-active', t.dataset.tab === name));
    document.querySelectorAll('.tab-body').forEach((p) =>
      p.classList.toggle('is-active', p.dataset.panel === name));

    stopScalePoll();
    if (name === 'register') { startScalePoll(); focusSoon('regBarcode'); }
    if (name === 'lookup') { focusSoon('lookupBarcode'); loadProducts(); }
    if (name === 'scale') { startScalePoll(); }
    if (name === 'system') { loadDiagnostics(); }
  }

  function focusSoon(id) { setTimeout(() => { const el = $(id); if (el) el.focus(); }, 60); }

  document.querySelectorAll('.tab').forEach((t) =>
    t.addEventListener('click', () => selectTab(t.dataset.tab)));

  // ====================================================================
  // TAB: register a product
  // ====================================================================
  function toggleProduceFields() {
    const isProduce = $('regIsProduce').checked;
    $('regPackagedFields').style.display = isProduce ? 'none' : 'grid';
    $('regProduceFields').style.display = isProduce ? 'block' : 'none';
  }
  $('regIsProduce').addEventListener('change', toggleProduceFields);

  function clearForm(keepBarcode) {
    editingId = null;
    if (!keepBarcode) $('regBarcode').value = '';
    ['regName', 'regCategory', 'regPrice', 'regStock', 'regWeight', 'regPricePerKg']
      .forEach((id) => { $(id).value = ''; });
    $('regIsProduce').checked = false;
    toggleProduceFields();
    $('regFoundNote').textContent = '';
    $('regFoundNote').className = 'scan-note';
    setErr('regErr', '');
    updateToleranceNote();
  }
  $('regClearBtn').addEventListener('click', () => { clearForm(); focusSoon('regBarcode'); });

  function fillForm(p) {
    editingId = p.id;
    $('regBarcode').value = p.barcode;
    $('regName').value = p.name;
    $('regCategory').value = p.category;
    $('regIsProduce').checked = p.is_produce;
    $('regPrice').value = p.price || '';
    $('regStock').value = p.stock_qty || '';
    $('regWeight').value = p.weight_g || '';
    $('regPricePerKg').value = p.price_per_kg || '';
    toggleProduceFields();
    updateToleranceNote();
  }

  /** Show what the anti-theft tolerance will be for the entered weight. */
  function updateToleranceNote() {
    const w = Number($('regWeight').value) || 0;
    const note = $('regToleranceNote');
    if (!note) return;
    if (!w) {
      note.textContent = 'No weight → this product skips the weight check.';
      note.className = 'static-note warn';
      return;
    }
    const tol = Math.max(25, w * 0.15);
    note.textContent = `Accepts ${(w - tol).toFixed(0)}–${(w + tol).toFixed(0)} g (±${tol.toFixed(0)} g).`;
    note.className = 'static-note';
  }
  $('regWeight').addEventListener('input', updateToleranceNote);

  async function findForRegister() {
    const barcode = $('regBarcode').value.trim();
    if (!barcode) return;
    const { data } = await api('/api/admin/products/lookup?barcode=' +
                               encodeURIComponent(barcode));
    const note = $('regFoundNote');
    if (data.found) {
      fillForm(data.product);
      note.textContent = `Already registered — editing "${data.product.name}".`;
      note.className = 'scan-note info';
    } else {
      const keep = $('regBarcode').value;
      clearForm(true);
      $('regBarcode').value = keep;
      note.textContent = 'New barcode. Fill in the details and save.';
      note.className = 'scan-note ok';
      focusSoon('regName');
    }
  }

  $('regFindBtn').addEventListener('click', findForRegister);
  $('regBarcode').addEventListener('keydown', (e) => {
    // the scanner gun sends the code then Enter
    if (e.key === 'Enter') { e.preventDefault(); findForRegister(); }
  });

  // capture the unit weight straight off the load cell
  $('regWeighBtn').addEventListener('click', async () => {
    const { data } = await api('/api/admin/scale');
    const reading = data.reading || {};
    if (reading.error) { setErr('regErr', reading.error); return; }
    if (reading.grams == null) { setErr('regErr', 'No reading from the scale.'); return; }
    if (reading.grams <= 0) {
      setErr('regErr', 'Scale reads zero or less. Put one unit on the basket, ' +
                       'and tare first if needed.');
      return;
    }
    $('regWeight').value = Number(reading.grams).toFixed(1);
    updateToleranceNote();
    setErr('regErr', '');
    T.toast(`Captured ${grams(reading.grams)}`, 'ok', 1800);
  });

  $('regSaveBtn').addEventListener('click', async () => {
    const payload = {
      id: editingId,
      barcode: $('regBarcode').value.trim(),
      name: $('regName').value.trim(),
      category: $('regCategory').value.trim(),
      is_produce: $('regIsProduce').checked,
      price: $('regPrice').value,
      stock_qty: $('regStock').value,
      weight_g: $('regWeight').value,
      price_per_kg: $('regPricePerKg').value,
      active: true
    };
    const { res, data } = await api('/api/admin/products', {
      method: 'POST', body: JSON.stringify(payload)
    });
    if (!res.ok || !data.ok) { setErr('regErr', data.error || 'Could not save'); return; }
    T.signal('ok');
    T.toast(`${data.created ? 'Registered' : 'Updated'} ${data.product.name} ✓`, 'ok');
    clearForm();
    focusSoon('regBarcode');
  });

  // ====================================================================
  // TAB: look up
  // ====================================================================
  function productCard(p, movements) {
    const weightLine = p.is_produce
      ? `<div class="kv"><span>Price / kg</span><b>₹${p.price_per_kg.toFixed(2)}</b></div>`
      : `<div class="kv"><span>Price</span><b>₹${p.price.toFixed(2)}</b></div>
         <div class="kv"><span>Unit weight</span><b>${p.weight_g ? p.weight_g + ' g' : '— not set'}</b></div>
         <div class="kv"><span>Stock</span><b>${p.stock_qty}</b></div>`;
    const flags = [
      p.active ? '<span class="badge badge-ok">active</span>'
               : '<span class="badge badge-warn">de-listed</span>',
      p.is_produce ? '<span class="badge badge-blue">loose produce</span>' : '',
      p.weight_verifiable ? '<span class="badge badge-ok">weight checked</span>'
                          : '<span class="badge badge-warn">no weight check</span>'
    ].join(' ');
    const moves = (movements || []).length
      ? '<div class="moves">' + movements.map((m) =>
          `<div><span>${m.change > 0 ? '+' : ''}${m.change}</span> ${esc(m.reason)}
           <em>${esc(m.created_at)}</em></div>`).join('') + '</div>'
      : '';

    return `
      <div class="found-card">
        <div class="found-head">
          <div>
            <div class="fname">${esc(p.name)}</div>
            <div class="fmeta">${esc(p.barcode)} · ${esc(p.category)}</div>
          </div>
          <div class="fflags">${flags}</div>
        </div>
        <div class="kvs">${weightLine}</div>
        <div class="row-end">
          <button class="btn btn-ghost btn-sm" data-edit="${p.id}">Edit</button>
          <button class="btn btn-ghost btn-sm" data-stock="${p.id}">Stock ±</button>
          <button class="btn ${p.active ? 'btn-danger-ghost' : 'btn-ghost'} btn-sm"
                  data-active="${p.id}" data-next="${p.active ? 'false' : 'true'}">
            ${p.active ? 'De-list' : 'Re-list'}
          </button>
        </div>
        ${moves}
      </div>`;
  }

  function wireCardButtons(container) {
    container.querySelectorAll('[data-edit]').forEach((b) =>
      b.addEventListener('click', async () => {
        const { data } = await api('/api/admin/products?q=&limit=200');
        const p = (data.items || []).find((x) => x.id === Number(b.dataset.edit));
        if (p) { selectTab('register'); fillForm(p); }
      }));

    container.querySelectorAll('[data-stock]').forEach((b) =>
      b.addEventListener('click', async () => {
        const delta = prompt('Change stock by how many? (use -3 to reduce)');
        if (delta === null || delta.trim() === '') return;
        const { res, data } = await api(
          `/api/admin/products/${b.dataset.stock}/stock`,
          { method: 'POST', body: JSON.stringify({ delta: Number(delta), note: 'settings menu' }) }
        );
        if (res.ok && data.ok) {
          T.toast(`Stock now ${data.product.stock_qty}`, 'ok');
          doLookup(); loadProducts();
        } else T.toast(data.error || 'Could not update stock', 'err');
      }));

    container.querySelectorAll('[data-active]').forEach((b) =>
      b.addEventListener('click', async () => {
        const next = b.dataset.next === 'true';
        const { res, data } = await api(
          `/api/admin/products/${b.dataset.active}/active`,
          { method: 'POST', body: JSON.stringify({ active: next }) }
        );
        if (res.ok && data.ok) {
          T.toast(next ? 'Product re-listed' : 'Product de-listed', 'ok');
          doLookup(); loadProducts();
        }
      }));
  }

  async function doLookup() {
    const term = $('lookupBarcode').value.trim();
    const out = $('lookupResult');
    if (!term) { out.innerHTML = ''; return; }

    const { data } = await api('/api/admin/products/lookup?barcode=' +
                               encodeURIComponent(term));
    if (data.found) {
      out.innerHTML = productCard(data.product, data.movements);
      wireCardButtons(out);
      T.signal('ok');
      return;
    }

    // not a barcode: fall back to a name/category search
    const { data: search } = await api('/api/admin/products?q=' +
                                       encodeURIComponent(term) + '&limit=20');
    const items = search.items || [];
    if (!items.length) {
      out.innerHTML = `<div class="found-card empty-card">
          Nothing found for <b>${esc(term)}</b>.
          <button class="btn btn-ghost btn-sm" id="lookupRegisterBtn">Register it</button>
        </div>`;
      $('lookupRegisterBtn').addEventListener('click', () => {
        selectTab('register');
        clearForm();
        $('regBarcode').value = term;
        findForRegister();
      });
      T.signal('alert');
      return;
    }
    out.innerHTML = items.map((p) => productCard(p)).join('');
    wireCardButtons(out);
  }

  $('lookupBtn').addEventListener('click', doLookup);
  $('lookupBarcode').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); doLookup(); }
  });

  async function loadProducts() {
    const { data } = await api('/api/admin/products?limit=200');
    const list = $('productList');
    const items = data.items || [];
    list.innerHTML = items.map((p) => `
      <button class="prow" data-pick="${esc(p.barcode)}">
        <span class="pn">${esc(p.name)}</span>
        <span class="pb">${esc(p.barcode)}</span>
        <span class="pw">${p.is_produce ? '₹' + p.price_per_kg + '/kg'
                                        : (p.weight_g ? p.weight_g + ' g' : 'no weight')}</span>
      </button>`).join('');
    list.querySelectorAll('[data-pick]').forEach((b) =>
      b.addEventListener('click', () => {
        $('lookupBarcode').value = b.dataset.pick;
        doLookup();
      }));
    const c = data.counts || {};
    $('catalogueCounts').textContent =
      `${c.active || 0} active · ${c.produce || 0} produce · ${c.missing_weight || 0} without a weight`;
  }

  // ====================================================================
  // TAB: scale
  // ====================================================================
  function startScalePoll() {
    stopScalePoll();
    refreshScale();
    scalePoll = setInterval(refreshScale, 1200);
  }
  function stopScalePoll() {
    if (scalePoll) { clearInterval(scalePoll); scalePoll = null; }
  }

  async function refreshScale() {
    if (!modal.classList.contains('show')) { stopScalePoll(); return; }
    let data;
    try { ({ data } = await api('/api/admin/scale')); } catch (_) { return; }

    const r = data.reading || {};
    const health = data.health || {};

    if ($('regLiveWeight')) $('regLiveWeight').textContent = grams(r.grams);
    if ($('scLiveGrams')) {
      $('scLiveGrams').textContent = grams(r.grams);
      $('scRaw').textContent = r.raw == null ? '—' : Math.round(r.raw).toLocaleString();
      $('scNoise').textContent = r.raw_stdev == null ? '—' : '±' + r.raw_stdev;
      $('scSource').textContent = health.kind || '—';
    }

    const status = $('scStatus');
    if (status) {
      const cal = data.calibration || {};
      const parts = [];
      if (!health.online) {
        parts.push(`<div class="alert-line">⚠ ${esc(health.detail || 'scale offline')}</div>`);
      }
      if (!health.real_hardware) {
        parts.push('<div class="alert-line">⚠ Software scale — weight verification is not real.</div>');
      }
      parts.push(`<div class="kv"><span>Counts per gram</span>
                  <b>${Number(cal.reference_unit || 0).toFixed(4)}</b></div>`);
      parts.push(`<div class="kv"><span>Zero offset</span>
                  <b>${Math.round(cal.offset || 0).toLocaleString()}</b></div>`);
      parts.push(`<div class="kv"><span>Calibrated</span>
                  <b>${cal.calibrated_at ? esc(cal.calibrated_at) : 'never (using .env defaults)'}</b></div>`);
      if (data.device && T.cfg.isCloud) {
        const d = data.device;
        parts.push(`<div class="kv"><span>Trolley device</span>
                    <b>${d.online ? 'online' : 'offline'}${d.reading_age_s != null
                        ? ' · ' + d.reading_age_s + 's ago' : ''}</b></div>`);
      }
      status.innerHTML = parts.join('');
    }

    $('scSpanBtn').disabled = !data.zero_captured;
  }

  $('scTareBtn').addEventListener('click', async () => {
    const { res, data } = await api('/api/admin/scale/tare', { method: 'POST' });
    if (res.ok && data.ok) { T.toast('Basket zeroed', 'ok'); setErr('scErr', ''); }
    else setErr('scErr', data.error || 'Tare failed');
    refreshScale();
  });

  $('scZeroBtn').addEventListener('click', async () => {
    const { res, data } = await api('/api/admin/scale/calibrate/zero', { method: 'POST' });
    const note = $('scZeroNote');
    if (res.ok && data.ok) {
      note.textContent = `Zero captured at ${Math.round(data.zero_raw).toLocaleString()} counts ` +
                         `(noise ±${data.noise}). Now place the reference weight.`;
      note.className = 'scan-note ok';
      setErr('scErr', '');
      $('scSpanBtn').disabled = false;
      focusSoon('scKnownG');
    } else {
      note.textContent = '';
      setErr('scErr', data.error || 'Could not capture zero');
    }
  });

  $('scSpanBtn').addEventListener('click', async () => {
    const known = Number($('scKnownG').value);
    if (!known || known <= 0) { setErr('scErr', 'Enter the reference weight in grams'); return; }
    const { res, data } = await api('/api/admin/scale/calibrate/span', {
      method: 'POST', body: JSON.stringify({ known_grams: known })
    });
    const note = $('scSpanNote');
    if (res.ok && data.ok) {
      note.innerHTML = `Calibrated: <b>${data.counts_per_gram}</b> counts/g. ` +
        `Reading now <b>${grams(data.measured_now_g)}</b> ` +
        `(off by ${data.error_g} g).`;
      note.className = 'scan-note ok';
      setErr('scErr', '');
      T.signal('ok');
      T.toast('Scale calibrated and saved', 'ok');
    } else {
      note.textContent = '';
      setErr('scErr', data.error || 'Calibration failed');
    }
    refreshScale();
  });

  $('scManualBtn').addEventListener('click', async () => {
    const { res, data } = await api('/api/admin/scale/calibrate/manual', {
      method: 'POST',
      body: JSON.stringify({
        reference_unit: $('scRefUnit').value, offset: $('scOffset').value
      })
    });
    if (res.ok && data.ok) { T.toast('Calibration saved', 'ok'); setErr('scErr', ''); }
    else setErr('scErr', data.error || 'Could not save');
    refreshScale();
  });

  // ====================================================================
  // TAB: system
  // ====================================================================
  $('sysFullscreenBtn').addEventListener('click', () => T.toggleFullscreen());
  $('sysReloadBtn').addEventListener('click', () => location.reload());
  $('sysRefreshBtn').addEventListener('click', loadDiagnostics);

  async function loadDiagnostics() {
    const box = $('sysDiag');
    if (!box) return;
    let data;
    try { ({ data } = await api('/api/admin/diagnostics')); } catch (_) { return; }
    if (!data.ok) { box.textContent = 'Unavailable'; return; }

    const rows = [
      ['Mode', data.mode],
      ['Database', data.database],
      ['Payments', data.payment_provider],
      ['Load cell', `${data.scale.kind}${data.scale.online ? '' : ' (offline)'}`],
      ['Camera scanner', data.camera_enabled ? 'enabled' : 'disabled'],
      ['Products', `${data.catalogue.active} active / ${data.catalogue.total} total`],
      ['Without weight', String(data.catalogue.missing_weight)],
      ['Kiosk URL', data.kiosk_url]
    ];
    if (data.mode === 'cloud') {
      const d = data.device || {};
      rows.splice(4, 0, ['Trolley agent',
        `${d.connected ? 'connected' : 'not connected'} · ${d.readings_received || 0} readings`]);
    }
    const warn = (data.warnings || []).map((w) =>
      `<div class="alert-line">⚠ ${esc(w)}</div>`).join('');
    box.innerHTML = warn + rows.map(([k, v]) =>
      `<div class="kv"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
  }
})();
