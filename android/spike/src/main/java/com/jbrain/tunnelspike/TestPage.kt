package com.jbrain.tunnelspike

/** The page and service worker the test server serves. The page runs every browser-side check
 * itself and leaves its findings in `window.__results` for the activity to collect. */
object TestPage {
    const val SERVICE_WORKER = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => {
  if (new URL(e.request.url).pathname !== '/api/ping') return;
  e.respondWith(fetch(e.request).then(async (r) => {
    const h = new Headers(r.headers);
    h.set('X-Via-SW', '1');
    return new Response(await r.text(), { status: r.status, headers: h });
  }));
});
"""

    const val HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tunnel spike</title>
<style>body{font:13px monospace;margin:8px;background:#111;color:#ddd}pre{white-space:pre-wrap}</style>
</head><body><b id="h"></b><pre id="out">running…</pre>
<script>
const R = window.__results = { origin: location.origin, done: false };
const out = document.getElementById('out');
document.getElementById('h').textContent = location.host;
const render = () => { out.textContent = JSON.stringify(R, null, 2); };
const step = (k, v) => { R[k] = v; render(); };
const withTimeout = (p, ms, label) => Promise.race([p, new Promise((r) => setTimeout(() => r(label + ': timeout'), ms))]);

function ws(tag) {
  return withTimeout(new Promise((resolve) => {
    let s;
    try { s = new WebSocket('wss://' + location.host + '/ws'); } catch (e) { resolve('fail: ' + e); return; }
    s.onopen = () => s.send(tag);
    s.onmessage = (m) => { resolve('ok ' + m.data); s.close(); };
    s.onerror = () => resolve('fail: error event');
  }), 8000, 'ws');
}

function sse() {
  return withTimeout(new Promise((resolve) => {
    let n = 0;
    const es = new EventSource('/sse');
    es.onmessage = () => { n += 1; if (n === 3) { es.close(); resolve('ok 3 events'); } };
    es.onerror = () => { if (n < 3) { es.close(); resolve('fail after ' + n + ' events'); } };
  }), 8000, 'sse');
}

// Started before anything else, so they race the main frame's own connection: the cold-start
// case where a WebSocket or SSE is the first subresource on the origin.
const earlyWs = ws('early');
const earlySse = sse();

async function run() {
  step('secureContext', window.isSecureContext);
  step('earlyWs', await earlyWs);
  step('earlySse', await earlySse);
  try {
    await navigator.serviceWorker.register('/sw.js');
    await withTimeout(navigator.serviceWorker.ready, 5000, 'ready');
    step('swRegister', 'ok');
  } catch (e) { step('swRegister', 'fail: ' + e); }
  if (navigator.serviceWorker && !navigator.serviceWorker.controller) {
    await new Promise((r) => { navigator.serviceWorker.addEventListener('controllerchange', r, { once: true }); setTimeout(r, 3000); });
  }
  step('swControlsPage', !!(navigator.serviceWorker && navigator.serviceWorker.controller));
  try {
    const r = await fetch('/api/ping', { cache: 'no-store' });
    const j = await r.json();
    step('fetch', 'ok ' + r.status);
    step('fetchViaServiceWorker', r.headers.get('X-Via-SW') === '1');
    step('clientKeySeenByServer', j.clientSpki);
  } catch (e) { step('fetch', 'fail: ' + e); }
  step('sse', await sse());
  step('ws', await ws('late'));
  try {
    const r = await fetch('https://pinfail.jbrain.home.arpa/api/ping', { cache: 'no-store' });
    step('pinMismatchHost', 'LOADED ' + r.status + ' (pin-set NOT enforced)');
  } catch (e) { step('pinMismatchHost', 'blocked (pin-set enforced): ' + e); }
  try {
    await fetch('https://www.google.com/generate_204', { mode: 'no-cors', cache: 'no-store' });
    step('internetFetch', 'ok');
  } catch (e) { step('internetFetch', 'fail: ' + e); }
  R.done = true;
  render();
}
run();
</script></body></html>"""
}
