// ==UserScript==
// @name         Eye-Tracking Suite · puente web
// @namespace    https://github.com/eye-tracking-suite
// @version      1.0.0
// @description  Envía a la Eye-Tracking Suite (localhost) el scroll, la posición de los elementos AOI, los clics y la navegación, para poder ubicar la mirada en la página aunque haya scroll.
// @match        *://*/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// @noframes
// ==/UserScript==

/*
 * USO
 * ---
 * 1) Sitios de terceros (p. ej. el sitio de la universidad):
 *      instalar la extensión Tampermonkey (Chrome / Edge / Firefox) y crear un
 *      script nuevo con este contenido.  Es recomendable cambiar la línea
 *      "@match" por el sitio evaluado, p. ej.  // @match  https://www.udem.edu.mx/*
 *      (aunque no hace falta: el script se desactiva solo en cualquier sitio
 *      que no esté en la lista de hosts del estudio).
 *
 * 2) Páginas propias (como pagina_prueba.html):
 *      <script src="web/eyetracking_bridge.user.js"></script>
 *
 * No modifica la página: sólo LEE posiciones.  Si la Eye-Tracking Suite no
 * está abierta, no hace nada.  No envía texto escrito en formularios; de los
 * clics sólo guarda la etiqueta, el id, la clase y los primeros 60 caracteres
 * del texto visible del elemento pulsado.
 */

(function () {
  'use strict';
  if (window.top !== window.self) return;                // sólo la ventana principal
  if (window.__etBridgeLoaded) return;
  window.__etBridgeLoaded = true;

  var BASE = 'http://127.0.0.1:4243';
  var HEARTBEAT_MS = 2000;     // reenvío periódico del estado
  var SCROLL_MS = 80;          // máx. ~12 mensajes de scroll por segundo
  var MUTATION_MS = 600;       // espera tras cambios del DOM antes de medir

  var gm = (typeof GM_xmlhttpRequest === 'function') ? GM_xmlhttpRequest
         : (typeof GM !== 'undefined' && GM && GM.xmlHttpRequest) ? GM.xmlHttpRequest.bind(GM)
         : null;

  var cfg = { aois: [], hosts: [], rec: false, task: 0 };
  var enabled = false;
  var lastLayoutJson = '';
  var lastRec = false;

  // ── Comunicación ────────────────────────────────────────────────────────
  function request(method, path, body, cb) {
    var url = BASE + path;
    try {
      if (gm) {
        gm({
          method: method, url: url, data: body,
          headers: { 'Content-Type': 'text/plain' },
          timeout: 1500,
          onload: function (r) { if (cb) { try { cb(JSON.parse(r.responseText)); } catch (e) { cb(null); } } },
          onerror: function () { if (cb) cb(null); },
          ontimeout: function () { if (cb) cb(null); }
        });
      } else {
        // text/plain = petición "simple": sin preflight
        fetch(url, { method: method, body: body, headers: { 'Content-Type': 'text/plain' } })
          .then(function (r) { return r.json(); })
          .then(function (j) { if (cb) cb(j); })
          .catch(function () { if (cb) cb(null); });
      }
    } catch (e) { if (cb) cb(null); }
  }

  function send(obj) {
    if (!enabled) return;
    obj.url = location.href;
    request('POST', '/e', JSON.stringify(obj), function (ack) {
      if (!ack) return;
      cfg.rec = !!ack.rec;
      cfg.task = ack.task || 0;
      // Al empezar a grabar a mitad de página hay que reenviar el diseño
      if (cfg.rec && !lastRec) { lastLayoutJson = ''; sendViewport(true); sendLayout(); }
      lastRec = cfg.rec;
    });
  }

  // ── Viewport (scroll, tamaño y posición de la ventana) ──────────────────
  function sendViewport() {
    var de = document.documentElement, b = document.body || de;
    send({
      k: 'vp',
      sx: Math.round(window.pageXOffset || de.scrollLeft || 0),
      sy: Math.round(window.pageYOffset || de.scrollTop || 0),
      iw: window.innerWidth, ih: window.innerHeight,
      ow: window.outerWidth, oh: window.outerHeight,
      wx: window.screenX !== undefined ? window.screenX : window.screenLeft,
      wy: window.screenY !== undefined ? window.screenY : window.screenTop,
      sw: window.screen.width, sh: window.screen.height,
      dpr: window.devicePixelRatio || 1,
      pw: Math.max(de.scrollWidth, b.scrollWidth),
      ph: Math.max(de.scrollHeight, b.scrollHeight),
      // Firefox da el origen exacto del área de contenido
      mx: (typeof window.mozInnerScreenX === 'number') ? window.mozInnerScreenX : null,
      my: (typeof window.mozInnerScreenY === 'number') ? window.mozInnerScreenY : null
    });
  }

  // ── Diseño: posición de cada elemento AOI ───────────────────────────────
  function isFixed(el) {
    for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
      var p = getComputedStyle(n).position;
      if (p === 'fixed' || p === 'sticky') return true;
    }
    return false;
  }

  function measure() {
    var out = [];
    var sx = window.pageXOffset || 0, sy = window.pageYOffset || 0;
    cfg.aois.forEach(function (a) {
      if (!a.selector) return;
      if (a.url_contains && location.href.indexOf(a.url_contains) < 0) return;
      var el = null;
      try { el = document.querySelector(a.selector); } catch (e) { return; }
      if (!el) return;
      var r = el.getBoundingClientRect();
      var st = getComputedStyle(el);
      var vis = r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
      var fixed = isFixed(el);
      out.push({
        id: a.id,
        // fijos/sticky: coordenadas de la ventana; el resto: de la página
        x: Math.round(fixed ? r.left : r.left + sx),
        y: Math.round(fixed ? r.top : r.top + sy),
        w: Math.round(r.width), h: Math.round(r.height),
        fixed: fixed, vis: vis
      });
    });
    return out;
  }

  function sendLayout() {
    if (!cfg.aois.length) return;
    var aois = measure();
    var j = JSON.stringify(aois);
    if (j === lastLayoutJson) return;
    lastLayoutJson = j;
    var de = document.documentElement, b = document.body || de;
    send({ k: 'layout', aois: aois,
           pw: Math.max(de.scrollWidth, b.scrollWidth),
           ph: Math.max(de.scrollHeight, b.scrollHeight) });
  }

  // ── Clics y navegación ──────────────────────────────────────────────────
  function describe(el) {
    if (!el || el.nodeType !== 1) return {};
    var interactive = el.closest ? el.closest('a,button,input,select,textarea,[role=button],[onclick]') : null;
    var t = interactive || el;
    var txt = '';
    if (t.tagName !== 'INPUT' && t.tagName !== 'TEXTAREA') {
      txt = (t.innerText || t.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60);
    }
    return {
      tag: t.tagName ? t.tagName.toLowerCase() : '',
      id: t.id || '',
      cls: (typeof t.className === 'string' ? t.className : '').slice(0, 60),
      txt: txt,
      href: t.href ? String(t.href).slice(0, 200) : ''
    };
  }

  document.addEventListener('click', function (e) {
    var d = describe(e.target);
    d.k = 'click';
    d.x = e.clientX; d.y = e.clientY;
    d.px = Math.round(e.pageX); d.py = Math.round(e.pageY);
    send(d);
  }, true);

  document.addEventListener('visibilitychange', function () {
    send({ k: 'vis', v: document.visibilityState });
  });

  // ── Arranque ────────────────────────────────────────────────────────────
  function onThrottled(fn, ms) {
    var last = 0, timer = null;
    return function () {
      var now = Date.now();
      if (now - last >= ms) { last = now; fn(); }
      else if (!timer) {
        timer = setTimeout(function () { timer = null; last = Date.now(); fn(); }, ms - (now - last));
      }
    };
  }

  function boot() {
    window.addEventListener('scroll', onThrottled(sendViewport, SCROLL_MS), { passive: true });
    window.addEventListener('resize', onThrottled(function () { sendViewport(); sendLayout(); }, 200));
    if (window.MutationObserver) {
      var mt = null;
      new MutationObserver(function () {
        clearTimeout(mt); mt = setTimeout(sendLayout, MUTATION_MS);
      }).observe(document.documentElement, { childList: true, subtree: true, attributes: true });
    }
    var lastHref = location.href;
    function sendNav() {
      lastHref = location.href;
      send({ k: 'nav', title: (document.title || '').slice(0, 120), ref: (document.referrer || '').slice(0, 200) });
    }
    sendNav();
    sendViewport();
    sendLayout();
    setInterval(function () {
      // Sitios de una sola página (SPA): la URL cambia sin recargar
      if (location.href !== lastHref) { sendNav(); lastLayoutJson = ''; }
      sendViewport();
      sendLayout();
    }, HEARTBEAT_MS);
    window.addEventListener('popstate', function () {
      setTimeout(function () { if (location.href !== lastHref) { sendNav(); lastLayoutJson = ''; sendLayout(); } }, 50);
    });
  }

  request('GET', '/config', null, function (c) {
    if (!c) return;                                       // la Suite no está abierta
    cfg = c;
    cfg.aois = c.aois || [];
    var hosts = c.hosts || [];
    // Si el estudio declara hosts, sólo se activa en ellos
    if (hosts.length && hosts.indexOf(location.hostname) < 0) return;
    enabled = true;
    lastRec = !!c.rec;
    boot();
  });
})();
