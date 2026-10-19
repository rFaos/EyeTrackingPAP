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

  // ── AOI por texto visible ("text" en el estudio) ────────────────────────
  // Sin mayúsculas ni acentos y con los espacios normalizados.
  function norm(s) {
    s = String(s || '').toLowerCase();
    if (s.normalize) s = s.normalize('NFD').replace(/[̀-ͯ]/g, '');
    return s.replace(/\s+/g, ' ').trim();
  }

  function visible(el) {
    var r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    var st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  }

  // Elemento cuyo texto (o aria-label / title / placeholder) contiene
  // ``a.text`` (o cualquiera de sus textos, si es una lista).  Con ``a.selector`` sube al contenedor más cercano que lo
  // cumpla.  Prefiere uno visible; entre varios, el más pequeño (el más
  // específico).
  function findByText(a) {
    var needles = [].concat(a.text).map(norm).filter(Boolean);
    if (!needles.length || !document.body) return null;
    function has(s) {
      s = norm(s);
      for (var k = 0; k < needles.length; k++) if (s.indexOf(needles[k]) >= 0) return true;
      return false;
    }
    var found = [];
    function add(el) {
      if (!el || el.nodeType !== 1) return;
      if (a.selector) {
        try { el = el.closest(a.selector); } catch (e) { el = null; }
        if (!el) return;
      }
      if (found.indexOf(el) < 0) found.push(el);
    }
    // 1) nodos de texto (no fuerza el cálculo del diseño de toda la página)
    var tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    for (var n = tw.nextNode(); n && found.length < 50; n = tw.nextNode()) {
      if (has(n.nodeValue)) add(n.parentElement);
    }
    // 2) atributos (iconos sin texto, buscadores)
    var attrs = document.querySelectorAll('[aria-label],[title],[placeholder],[alt]');
    for (var i = 0; i < attrs.length && found.length < 50; i++) {
      var el = attrs[i];
      if (has([el.getAttribute('aria-label'), el.getAttribute('title'),
               el.getAttribute('placeholder'), el.getAttribute('alt')].join(' '))) add(el);
    }
    // 3) texto partido en varias etiquetas («Mis <b>Solicitudes</b>»)
    if (!found.length) {
      var cand = document.querySelectorAll('a,button,[role=button],[role=tab],[role=menuitem],h1,h2,h3,h4,h5,h6,label,li,th,td,p');
      for (var j = 0; j < cand.length && found.length < 50; j++) {
        if (has(cand[j].textContent)) add(cand[j]);
      }
    }
    var best = null, bestVis = false, bestArea = Infinity;
    found.forEach(function (el) {
      var r = el.getBoundingClientRect();
      var v = visible(el), area = r.width * r.height;
      if ((v && !bestVis) || (v === bestVis && area < bestArea)) {
        best = el; bestVis = v; bestArea = area;
      }
    });
    return best;
  }

  // Botón que ABRE el menú donde aparece ``texto`` (p. ej. el ícono de
  // cuadrícula que despliega «Trayecto UDEM»): se sube desde el texto hasta
  // un antepasado que contenga un conmutador (aria-expanded, aria-haspopup,
  // data-toggle…) que no sea parte del propio menú.
  var TOGGLE = '[aria-expanded],[aria-haspopup],[data-toggle],[data-bs-toggle],' +
               '[uib-dropdown-toggle],.dropdown-toggle';
  function findOpener(texto) {
    var item = findByText({ text: texto });
    if (!item) return null;
    for (var n = item.parentElement; n && n !== document.body; n = n.parentElement) {
      var cands = n.querySelectorAll(TOGGLE);
      for (var i = 0; i < cands.length; i++) {
        var c = cands[i];
        if (c.contains(item) || c === item) continue;
        if (has_text(c, texto)) continue;
        return c;
      }
    }
    return null;
  }
  function has_text(el, texto) {
    return norm(el.textContent).indexOf(norm(texto)) >= 0;
  }

  function measure() {
    var out = [];
    var sx = window.pageXOffset || 0, sy = window.pageYOffset || 0;
    cfg.aois.forEach(function (a) {
      if (!a.selector && !a.text && !a.opens) return;
      if (a.url_contains && location.href.indexOf(a.url_contains) < 0) return;
      var el = null;
      if (a.opens) el = findOpener(a.opens);
      else if (a.text) el = findByText(a);
      else { try { el = document.querySelector(a.selector); } catch (e) { return; } }
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

  // «pointerdown» en window y en fase de captura: es lo PRIMERO que recibe
  // la página, así que ningún menú o widget puede ocultarnos el clic (antes,
  // con «click» en document, el portal se lo quedaba al abrir menús).
  window.addEventListener('pointerdown', function (e) {
    if (!e.isPrimary || (e.button !== undefined && e.button !== 0)) return;
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
