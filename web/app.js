const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="offset-token"]').content;
const isDock = new URLSearchParams(location.search).get('dock') === '1';
if (isDock) document.body.classList.add('dock-mode');

let snapshot = null;
let busy = false;
let initialized = false;
let selectedPreset = 'local';
let liveDelayChoice = 30;
let lastSlateUpdated = null;
let lastCopyMessage = '';
const copyTimers = new WeakMap();

const TERMINAL = new Set(['READY', 'FINISHED', 'STOPPED', 'FAULT']);

const PRESET_META = {
  local: { name: 'Local', needsKey: false, url: 'rtmp://127.0.0.1:1936/live/offset', help: 'Pruebas locales en el puerto 1936.' },
  twitch: { name: 'Twitch', needsKey: true, url: 'rtmps://ingest.global-contribute.live-video.net/app/', help: 'Panel de creador de Twitch → Ajustes → Emisión.' },
  youtube: { name: 'YouTube', needsKey: true, url: 'rtmps://a.rtmps.youtube.com/live2/', help: 'YouTube Studio → Emitir en directo → Clave.' },
  kick: { name: 'Kick', needsKey: true, url: 'rtmps://fa723fc1b171.global-contribute.live-video.net/app/', help: 'Creator Dashboard → Settings → Stream Key.' },
  custom: { name: 'Otro', needsKey: false, url: '', help: 'URL completa rtmp:// o rtmps:// con tu clave.' }
};

const mib = bytes => ((bytes || 0) / 1048576).toFixed(1);

async function api(path, values) {
  const options = { headers: { 'X-Offset-Token': token }, signal: AbortSignal.timeout(10000) };
  if (values !== undefined) {
    Object.assign(options, {
      method: 'POST',
      body: JSON.stringify(values),
      headers: { ...options.headers, 'Content-Type': 'application/json' }
    });
  }
  const response = await fetch('/api/' + path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'No se pudo completar la acción.');
  return data;
}

/* --- Copy Button (modern-web-guidance copy-button + OBS CEF fallback) --- */
function legacyCopy(text) {
  const previousFocus = document.activeElement;
  const field = Object.assign(document.createElement('textarea'), { value: text });
  field.setAttribute('readonly', '');
  field.style.cssText = 'position:fixed;inset-block-start:0;opacity:0';
  document.body.append(field);
  field.select();
  try { return document.execCommand('copy'); }
  finally { field.remove(); previousFocus?.focus({ preventScroll: true }); }
}

function reportCopy(button, ok) {
  const tip = button.nextElementSibling;
  clearTimeout(copyTimers.get(button));
  button.removeAttribute('data-copied');
  button.removeAttribute('data-failed');
  let message = ok ? 'Copiado' : 'Error';
  if (message === lastCopyMessage) message += '\u00A0';
  lastCopyMessage = message;
  tip.textContent = '';
  void button.offsetWidth;
  button.setAttribute(ok ? 'data-copied' : 'data-failed', '');
  requestAnimationFrame(() => { tip.textContent = message; });
  copyTimers.set(button, setTimeout(() => {
    button.removeAttribute('data-copied');
    button.removeAttribute('data-failed');
    tip.textContent = '';
  }, 1600));
}

async function copyTarget(button, target) {
  const text = target.matches('input, textarea') ? target.value : target.textContent.trim();
  if (!text) return;
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else if (!legacyCopy(text)) throw new Error('Copy failed');
    reportCopy(button, true);
  } catch {
    if (legacyCopy(text)) reportCopy(button, true);
    else reportCopy(button, false);
  }
}

if ('CommandEvent' in window) {
  document.addEventListener('command', event => {
    if (event.command === '--copy') copyTarget(event.source, event.target);
  }, true);
} else {
  document.addEventListener('click', event => {
    if (event.defaultPrevented) return;
    const button = event.target.closest('button[command="--copy"]');
    if (!button) return;
    const target = document.getElementById(button.getAttribute('commandfor'));
    if (target) copyTarget(button, target);
  });
}

/* --- Native Dialogs + closedby fallback --- */
const settingsDialog = $('settings-dialog');
const stopDialog = $('stop-dialog');

if (!('closedBy' in HTMLDialogElement.prototype)) {
  const outside = (e, d) => {
    const r = d.getBoundingClientRect();
    return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
  };
  for (const dialog of [settingsDialog, stopDialog]) {
    let pressedOutside = false;
    dialog.addEventListener('pointerdown', e => { pressedOutside = outside(e, dialog); });
    dialog.addEventListener('pointerup', e => { if (pressedOutside && outside(e, dialog)) dialog.close(); });
  }
}

stopDialog.addEventListener('close', () => {
  const choice = stopDialog.returnValue;
  stopDialog.returnValue = '';
  if (choice === 'stop') runAction(() => api('stop', {}));
});

settingsDialog.addEventListener('close', () => {
  settingsDialog.returnValue = '';
});

function openSettingsTab(tabName) {
  document.querySelectorAll('.seg-tab').forEach(tab => {
    const active = tab.dataset.tab === tabName;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
  });
  document.querySelectorAll('.tab-pane').forEach(pane => {
    pane.hidden = pane.dataset.pane !== tabName;
  });
  if (!settingsDialog.open) settingsDialog.showModal();
}

/* --- UI State Helpers --- */
function setPresetUI(presetId) {
  selectedPreset = PRESET_META[presetId] ? presetId : 'local';
  const meta = PRESET_META[selectedPreset];
  document.querySelectorAll('.preset-card').forEach(tab => {
    const active = tab.dataset.preset === selectedPreset;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
  });
  $('key-field-wrap').hidden = !meta.needsKey;
  $('url-field-wrap').hidden = meta.needsKey;
  $('preset-help').textContent = meta.help;
  const savedKeys = snapshot?.saved_key_presets || [];
  if (meta.needsKey && savedKeys.includes(selectedPreset)) {
    $('preset-key-hint').textContent = 'Clave guardada';
    $('stream-key').placeholder = 'Usará tu clave cifrada guardada';
  } else {
    $('preset-key-hint').textContent = 'Oculta';
    $('stream-key').placeholder = 'Pega tu clave de emisión';
  }
  if (!meta.needsKey && selectedPreset === 'local' && !$('output-url').value.trim()) {
    $('output-url').value = meta.url;
  }
  if (selectedPreset === 'custom' && $('output-url').value === PRESET_META.local.url) {
    $('output-url').value = '';
    $('output-url').placeholder = 'rtmps://servidor/app/tu_clave';
  }
  $('dest-label').textContent = meta.name;
}

function setLiveDelayChoice(seconds) {
  const val = Math.max(1, Math.min(120, Math.round(Number(seconds) || 30)));
  liveDelayChoice = val;
  $('live-delay-input').value = String(val);
  $('cta-secs').textContent = String(val);
  document.querySelectorAll('[data-live-delay]').forEach(btn => {
    btn.classList.toggle('active', Number(btn.dataset.liveDelay) === val);
  });
  const mb = (liveDelayChoice * 6160000 / 8 / 1048576);
  $('estimate').textContent = `A 6 Mbps, ${liveDelayChoice} s ocupan ~${mb.toFixed(1)} MiB en RAM.`;
  updateControls();
}

function describeState(data) {
  const st = data.state;
  const dm = data.delay_mode || 'live';
  if (st === 'READY') return ['Sin conectar', '', 'Pulsa Iniciar y transmite desde OBS'];
  if (st === 'WAITING_INPUT') return ['Esperando OBS', '', 'Inicia tu transmisión en OBS'];
  if (st === 'BUFFERING') return ['Cargando delay', 'delayed', `Preparando ${data.delay_target_s} s iniciales…`];
  if (st === 'DRAINING') return ['Finalizando', 'delayed', 'Enviando últimos segundos…'];
  if (st === 'FINISHED') return ['Finalizado', '', 'Sesión terminada limpiamente'];
  if (st === 'STOPPED') return ['Detenido', '', 'Listo para iniciar de nuevo'];
  if (st === 'FAULT') return ['Error', 'fault', 'Sesión interrumpida'];
  if (dm === 'slate_hold') return ['Directo oculto', 'slating', 'Mostrando tu imagen de espera'];
  if (dm === 'reconnecting_input') return ['Reconectando OBS', 'delayed', 'Directo protegido con imagen'];
  if (dm === 'entering_delay') return ['Activando delay…', 'slating', 'Sincronizando corte limpio…'];
  if (dm === 'slating') return ['Cargando delay', 'slating', 'Imagen en pantalla · 0 frames perdidos'];
  if (dm === 'catching_up') return ['Volviendo a directo', 'delayed', `Poniéndose al día a ${(data.catchup_rate || 1.25).toFixed(2)}×`];
  if (dm === 'jumping_live') return ['Volviendo ya…', 'live', 'Sincronizando en directo…'];
  if (dm === 'delayed' || (data.delay_target_s || 0) > 0) return [`Delay · ${data.delay_target_s} s`, 'delayed', 'Protegido contra stream-sniping'];
  return ['En directo', 'live', 'Sin retraso · tiempo real'];
}

function updateControls() {
  const active = snapshot && !TERMINAL.has(snapshot.state);
  const running = snapshot && (snapshot.state === 'RELAYING' || snapshot.state === 'BUFFERING');
  const dm = snapshot?.delay_mode || 'live';
  const target = Number(snapshot?.delay_target_s || 0);
  const isDelayedOrBusy = active && (dm !== 'live' || target > 0);
  const panicOn = Boolean(snapshot?.panic_active || dm === 'slate_hold');
  const hasBuffered = active && ((snapshot?.buffer?.packets || 0) > 0);

  $('btn-quick-start').hidden = Boolean(active);
  $('btn-quick-start').disabled = busy || !snapshot;
  $('btn-stop').hidden = !active;
  $('btn-stop').disabled = busy || !active;
  $('config-fields').disabled = busy || active;

  // Hide OBS quick-connect instructions once OBS is actively streaming
  $('obs-quick-tip').hidden = Boolean(running);

  $('btn-enable-delay').hidden = isDelayedOrBusy;
  $('btn-enable-delay').disabled = busy || !running;
  $('disable-group').hidden = !isDelayedOrBusy;
  $('btn-jump-live').disabled = busy || !active;
  $('btn-catchup-live').disabled = busy || !active || dm === 'catching_up';

  const canAdjust = active && running && isDelayedOrBusy && !panicOn && Math.round(target) !== liveDelayChoice;
  $('btn-adjust-delay').hidden = !canAdjust;
  $('btn-adjust-delay').disabled = busy || !canAdjust;

  $('btn-panic').disabled = busy || !running;
  $('btn-panic').classList.toggle('active', panicOn);
  $('panic-title').textContent = panicOn ? 'Mostrar directo' : 'Ocultar directo';
  $('panic-sub').textContent = panicOn ? 'Salir de imagen segura' : 'Muestra tu imagen ya';

  document.querySelectorAll('.purge-pill').forEach(btn => {
    btn.disabled = busy || !hasBuffered;
  });

  const rate = Number($('catchup-speed').value || 1.25).toFixed(2);
  $('catchup-rate-label').textContent = `Ponte al día a ${rate}×`;
}

function render(data) {
  snapshot = data;
  const [label, tone, heroSub] = describeState(data);
  $('status-pill').textContent = label;
  $('live-dot').className = 'led' + (tone ? ' ' + tone : '');
  $('hero-hint').textContent = heroSub;

  // State-reactive Aurora Mesh Gradient background (only active while connected/running)
  if (data.state === 'WAITING_INPUT') {
    document.body.dataset.aurora = 'waiting';
  } else if (data.state === 'FAULT') {
    document.body.dataset.aurora = 'fault';
  } else if (data.state === 'BUFFERING' || data.state === 'RELAYING' || data.state === 'DRAINING') {
    document.body.dataset.aurora = tone || 'live';
  } else {
    delete document.body.dataset.aurora;
  }

  const liveAge = data.last_packet_residence_s == null ? 0 : data.last_packet_residence_s;
  $('live-clock').textContent = `${liveAge.toFixed(1)}s`;
  $('live-clock').className = 'mono-clock' + (tone === 'delayed' || tone === 'slating' ? ' delayed' : tone === 'live' ? ' live' : '');

  const bufS = (data.buffer?.seconds || 0).toFixed(1);
  const bufMb = mib(data.buffer?.bytes);
  $('buffer-mini').textContent = `${bufS}s · ${bufMb} MB`;

  const s = data.stream || {};
  if (s.width && s.height) {
    $('stream-meta').textContent = `OBS ${s.width}×${s.height} · ${s.fps || '?'} fps`;
    $('stream-info').textContent = `${s.width}×${s.height} ${s.fps || '?'}fps · ${s.has_bframes ? 'B-frames OK' : 'B=0'}`;
  } else if (data.state === 'BUFFERING' || data.state === 'RELAYING') {
    $('stream-meta').textContent = 'OBS conectado';
    $('stream-info').textContent = 'Detectando formato…';
  } else {
    $('stream-meta').textContent = data.state === 'WAITING_INPUT' ? 'Esperando señal en OBS' : 'Listo para OBS';
    $('stream-info').textContent = '—';
  }

  // Banners
  const slating = data.delay_mode === 'slating' && data.slate && data.slate.target_s > 0;
  $('slate-progress-wrap').hidden = !slating;
  if (slating) {
    const pct = Math.round((data.slate.progress || 0) * 100);
    $('slate-progress-pct').textContent = `${pct}%`;
    $('slate-progress-bar').style.width = `${pct}%`;
    $('slate-progress-label').textContent = `Cargando delay (${data.slate.elapsed_s.toFixed(0)}/${data.slate.target_s.toFixed(0)}s · 0 frames perdidos)`;
  }

  const catching = data.delay_mode === 'catching_up';
  $('catchup-banner').hidden = !catching;
  if (catching) {
    const eta = data.catchup_eta_s != null ? `~${Math.ceil(data.catchup_eta_s)} s restantes` : 'calculando…';
    $('catchup-eta-text').textContent = `${(data.catchup_rate || 1.25).toFixed(2)}× · ${eta}`;
  }

  $('reconnect-banner').hidden = !data.reconnects?.input_reconnecting;
  $('panic-banner').hidden = !(data.panic_active || data.delay_mode === 'slate_hold');
  $('fault').hidden = !data.error;
  $('fault').textContent = data.error || '';

  // Slate preview & shortcut chip
  const store = data.slate_store || {};
  if (store.has_custom) {
    if (lastSlateUpdated !== store.updated_at) {
      lastSlateUpdated = store.updated_at;
      $('slate-preview-img').src = '/slate-preview?t=' + Date.now();
    }
    $('slate-preview-img').hidden = false;
    $('slate-empty').hidden = true;
    $('btn-slate-reset').hidden = false;
    $('slate-meta').textContent = `${store.filename} (${(store.size_bytes / 1024).toFixed(0)} KB)`;
    $('slate-chip-text').textContent = `Imagen: ${store.filename}`;
  } else {
    lastSlateUpdated = null;
    $('slate-preview-img').hidden = true;
    $('slate-empty').hidden = false;
    $('btn-slate-reset').hidden = true;
    $('slate-meta').textContent = 'Usando pantalla oscura automática.';
    $('slate-chip-text').textContent = 'Imagen: Automática';
  }

  // Hotkeys
  const hk = data.hotkeys || {};
  $('hk-chip-text').textContent = hk.enabled ? 'Atajos: Activos' : 'Atajos: Off';
  if (!initialized && hk.bindings) {
    $('hotkeys-enabled').checked = Boolean(hk.enabled);
    $('hk-toggle').value = hk.bindings.toggle_delay || 'Ctrl+Shift+F9';
    $('hk-jump').value = hk.bindings.jump_live || 'Ctrl+Shift+F10';
    $('hk-catchup').value = hk.bindings.catchup_live || 'Ctrl+Shift+F11';
    $('hk-panic').value = hk.bindings.panic_slate || 'Ctrl+Shift+F12';
  }
  renderHotkeyBadge('hk-toggle-st', hk.enabled, hk.registered?.toggle_delay);
  renderHotkeyBadge('hk-jump-st', hk.enabled, hk.registered?.jump_live);
  renderHotkeyBadge('hk-catchup-st', hk.enabled, hk.registered?.catchup_live);
  renderHotkeyBadge('hk-panic-st', hk.enabled, hk.registered?.panic_slate);

  // Diagnostics tab
  $('dest-masked').textContent = data.output_url || '';
  $('bytes').textContent = `${bufMb} / ${mib(data.buffer?.limit_bytes)} MiB`;
  $('rss').textContent = data.memory?.rss_bytes == null ? 'No disponible' : `${mib(data.memory.rss_bytes)} MiB`;
  $('packets').textContent = `${data.received_packets || 0} / ${data.sent_packets || 0} / ${data.slate_packets_sent || 0}`;
  $('reconn-count').textContent = `${data.reconnects?.input || 0} / ${data.reconnects?.output || 0}`;

  const evList = data.events?.length ? data.events.slice().reverse() : [{ at: '', message: 'Aún no hay actividad.' }];
  $('events').replaceChildren(...evList.map(ev => {
    const li = document.createElement('li');
    li.textContent = `${ev.at} ${ev.message}`.trim();
    return li;
  }));

  if (!initialized) {
    $('auto-reconnect').checked = data.auto_reconnect !== false;
    setPresetUI(data.preset || 'local');
    setLiveDelayChoice(data.default_delay_s || 30);
    if (data.output_url && data.preset === 'local') $('output-url').value = data.output_url;
    const mem = String(Math.round((data.buffer?.limit_bytes || 134217728) / 1048576));
    if ([...$('memory').options].some(o => o.value === mem)) $('memory').value = mem;
    try {
      const input = new URL(data.input_url);
      const parts = input.pathname.split('/');
      $('obs-key').textContent = parts.pop();
      $('obs-server').textContent = `rtmp://${input.host}${parts.join('/')}`;
    } catch {}
    const dockUrl = `${location.origin}/?dock=1`;
    $('obs-dock-url').textContent = dockUrl;
    $('obs-dock-url-visible').textContent = dockUrl;
    initialized = true;
  } else {
    $('dest-label').textContent = (PRESET_META[data.preset] || PRESET_META[selectedPreset] || PRESET_META.local).name;
  }

  $('connection-error').hidden = true;
  updateControls();
}

function renderHotkeyBadge(id, enabled, info) {
  const el = $(id);
  if (!enabled) { el.textContent = 'Off'; el.className = 'hk-st'; return; }
  if (info?.ok) { el.textContent = 'OK'; el.className = 'hk-st ok'; }
  else { el.textContent = info?.error || 'Error'; el.className = 'hk-st err'; }
}

async function runAction(task) {
  busy = true;
  updateControls();
  $('action-error').hidden = true;
  try {
    const res = await task();
    if (res && res.state) render(res);
    else render(await api('status'));
  } catch (err) {
    $('action-error').textContent = err.message;
    $('action-error').hidden = false;
  } finally {
    busy = false;
    updateControls();
  }
}

function buildStartPayload() {
  const meta = PRESET_META[selectedPreset];
  const payload = {
    preset: selectedPreset,
    delay: Number($('start-delay').value || 0),
    default_delay: liveDelayChoice,
    memory_mb: Number($('memory').value || 128),
    catchup_rate: Number($('catchup-speed').value || 1.25),
    allow_bframes: true,
    auto_reconnect: $('auto-reconnect').checked,
    remember_key: $('remember-key').checked
  };
  if (meta.needsKey) payload.stream_key = $('stream-key').value.trim();
  else payload.output_url = $('output-url').value.trim();
  return payload;
}

/* --- Events --- */
$('btn-open-settings').addEventListener('click', () => openSettingsTab('dest'));
$('btn-dest-pill').addEventListener('click', () => openSettingsTab('dest'));
$('btn-shortcut-slate').addEventListener('click', () => openSettingsTab('slate'));
$('btn-shortcut-hotkeys').addEventListener('click', () => openSettingsTab('hotkeys'));

document.querySelectorAll('.seg-tab').forEach(tab => {
  tab.addEventListener('click', () => openSettingsTab(tab.dataset.tab));
});

$('btn-quick-start').addEventListener('click', () => {
  const meta = PRESET_META[selectedPreset];
  const savedKeys = snapshot?.saved_key_presets || [];
  if (meta.needsKey && !$('stream-key').value.trim() && !savedKeys.includes(selectedPreset)) {
    openSettingsTab('dest');
    $('stream-key').focus();
    return;
  }
  runAction(() => api('start', buildStartPayload()));
});
$('btn-stop').addEventListener('click', () => stopDialog.showModal());

$('btn-enable-delay').addEventListener('click', () => runAction(() => api('delay/enable', { delay: liveDelayChoice })));
$('btn-adjust-delay').addEventListener('click', () => runAction(() => api('delay/set', {
  delay: liveDelayChoice,
  mode: 'catchup',
  catchup_rate: Number($('catchup-speed').value || 1.25)
})));
$('btn-jump-live').addEventListener('click', () => runAction(() => api('delay/disable', { mode: 'jump' })));
$('btn-jump-from-catchup').addEventListener('click', () => runAction(() => api('delay/disable', { mode: 'jump' })));
$('btn-catchup-live').addEventListener('click', () => runAction(() => api('delay/disable', {
  mode: 'catchup',
  catchup_rate: Number($('catchup-speed').value || 1.25)
})));

$('btn-panic').addEventListener('click', () => runAction(() => api('panic/toggle', {})));
$('btn-panic-resume-live').addEventListener('click', () => runAction(() => api('panic/off', { mode: 'jump' })));
$('btn-panic-resume-buf').addEventListener('click', () => runAction(() => api('panic/off', { mode: 'resume' })));

document.querySelectorAll('.purge-pill').forEach(btn => {
  btn.addEventListener('click', () => {
    const val = btn.dataset.purge;
    runAction(() => api('purge', val === 'all' ? {} : { seconds: Number(val) }));
  });
});

document.querySelectorAll('[data-live-delay]').forEach(btn => {
  btn.addEventListener('click', () => setLiveDelayChoice(btn.dataset.liveDelay));
});
$('live-delay-input').addEventListener('input', e => setLiveDelayChoice(e.target.value));
$('catchup-speed').addEventListener('change', updateControls);

document.querySelectorAll('.preset-card').forEach(tab => {
  tab.addEventListener('click', () => setPresetUI(tab.dataset.preset));
});
$('toggle-key-vis').addEventListener('click', () => {
  const inp = $('stream-key');
  const show = inp.type === 'password';
  inp.type = show ? 'text' : 'password';
  $('toggle-key-vis').textContent = show ? 'Ocultar' : 'Ver';
});

/* --- Slate Upload --- */
async function uploadSlateFile(file) {
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) {
    $('action-error').textContent = 'La imagen supera los 10 MiB.';
    $('action-error').hidden = false;
    return;
  }
  const bytes = await file.arrayBuffer();
  await runAction(async () => {
    const resp = await fetch('/api/slate', {
      method: 'POST',
      headers: {
        'X-Offset-Token': token,
        'Content-Type': file.type || 'application/octet-stream',
        'X-Slate-Filename': encodeURIComponent(file.name || 'slate')
      },
      body: bytes
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'No se pudo subir la imagen.');
    lastSlateUpdated = null;
    return api('status');
  });
}

$('slate-file').addEventListener('change', e => {
  const file = e.target.files?.[0];
  e.target.value = '';
  uploadSlateFile(file);
});
$('btn-slate-reset').addEventListener('click', () => runAction(() => api('slate/reset', {})));

const dropzone = $('slate-dropzone');
['dragenter', 'dragover'].forEach(ev => dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.add('dragover'); }));
['dragleave', 'drop'].forEach(ev => dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.remove('dragover'); }));
dropzone.addEventListener('drop', e => {
  const file = e.dataTransfer?.files?.[0];
  if (file) uploadSlateFile(file);
});

/* --- Hotkeys --- */
async function saveHotkeys() {
  $('hk-msg').textContent = 'Guardando…';
  try {
    await runAction(() => api('hotkeys', {
      enabled: $('hotkeys-enabled').checked,
      bindings: {
        toggle_delay: $('hk-toggle').value.trim(),
        jump_live: $('hk-jump').value.trim(),
        catchup_live: $('hk-catchup').value.trim(),
        panic_slate: $('hk-panic').value.trim()
      }
    }));
    $('hk-msg').textContent = $('hotkeys-enabled').checked ? 'Atajos activos.' : 'Atajos desactivados.';
  } catch {
    $('hk-msg').textContent = 'No se pudieron guardar.';
  }
}
$('btn-save-hotkeys').addEventListener('click', saveHotkeys);
$('hotkeys-enabled').addEventListener('change', saveHotkeys);

/* --- Export Diagnostics --- */
$('export').addEventListener('click', () => {
  if (!snapshot) return;
  const blob = new Blob([JSON.stringify({ exported_at: new Date().toISOString(), ...snapshot }, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = 'offset-diagnostico.json'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

/* --- Poll Loop --- */
async function poll() {
  try {
    if (!busy) render(await api('status'));
  } catch {
    $('connection-error').hidden = false;
    $('btn-quick-start').disabled = true;
    $('btn-enable-delay').disabled = true;
  }
  setTimeout(poll, 1000);
}

setLiveDelayChoice(30);
updateControls();
poll();
