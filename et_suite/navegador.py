"""
et_suite/navegador.py  —  Navegador integrado del estudio (sin Tampermonkey)
============================================================================
Abre el sitio del estudio en una ventana de navegador PROPIA de la app y le
inyecta el mismo script de ``web/eyetracking_bridge.user.js`` que antes había
que instalar a mano con Tampermonkey.  El resultado es idéntico (scroll,
posición real de cada AOI, clics y navegación llegan al ``WebBridge``), pero
el participante no instala ninguna extensión ni cambia ajustes de Chrome.

Por qué es mejor así:
  • Cero pasos para el participante: no hay extensión, ni «Permitir scripts
    de usuario», ni copiar/pegar código.
  • Usa el motor web que ya trae el sistema (Edge WebView2 en Windows,
    WebKit en macOS): no se descarga ningún navegador.
  • Privado: no comparte cookies ni contraseñas con el navegador personal y,
    al cerrarse, borra la sesión (cada estudiante inicia sesión con su
    cuenta y al terminar no queda nada abierto en la computadora).
  • El script SÓLO se inyecta en los dominios del estudio
    (``bridge.hosts``).  En la página de inicio de sesión no se inyecta
    nada: la contraseña nunca pasa cerca del prototipo.
  • Los enlaces que abrirían otra pestaña se abren en la misma ventana, para
    no perder el registro.
  • La posición exacta del área de contenido en la pantalla se mide con el
    ratón (``screenX − clientX``), como hace Firefox con
    ``mozInnerScreenX``: la mirada se sitúa en la página sin estimar el alto
    de la barra de título.
  • Sin red local: los eventos del script viajan a la app por el stdin/stdout
    de este proceso, no por un puerto.  No hay ningún servidor escuchando que
    el firewall o el antivirus tengan que aprobar.

Se ejecuta como proceso aparte porque Tkinter (la app) y el navegador
necesitan, cada uno, el hilo principal en macOS::

    python -m et_suite.navegador --url URL --hosts a.com,b.com --script X.js

Órdenes por stdin (una línea JSON):  {"cmd": "goto", "url": ...} · {"cmd": "cerrar"}
                                      {"cmd": "config", "cfg": {...}}  (AOI, tarea, grabando)
Eventos por stdout:                   @@EV {"tipo": "listo" | "pagina" | "web" | "cerrado", ...}
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional, Set
from urllib.parse import urlparse

PREFIX = "@@EV "


# ════════════════════════════════════════════════════════════════════════════
#  Lado de la APP: controla el proceso del navegador
# ════════════════════════════════════════════════════════════════════════════

class NavegadorIntegrado:
    """
    Lanza y controla la ventana del navegador del estudio.

    Uso::

        nav = NavegadorIntegrado(base_dir, script, port=4243, hosts=[...],
                                 bridge=web_bridge)
        nav.abrir("https://portal.udem.edu/")     # abre o navega
        nav.cerrar()

    Los eventos del script («web») se entregan a ``bridge.receive_raw`` y la
    configuración de ``bridge.config()`` se reenvía al navegador cada vez
    que cambia.  ``port`` sólo se conserva para comparar configuraciones.
    """

    def __init__(self, base_dir: str, script_path: str, port: int,
                 hosts: List[str], titulo: str = "Estudio",
                 on_evento: Optional[Callable[[dict], None]] = None,
                 bridge=None) -> None:
        self.base_dir = base_dir
        self.script_path = script_path
        self.port = port
        self.hosts = list(hosts)
        self.titulo = titulo
        self.on_evento = on_evento
        self.bridge = bridge
        self._cfg_json = ""
        self.proc: Optional[subprocess.Popen] = None
        self.hosts_visitados: Set[str] = set()
        self.url_actual = ""
        self.inyectado_actual = False
        self.salida: List[str] = []
        self._lock = threading.Lock()

    @staticmethod
    def disponible() -> bool:
        return importlib.util.find_spec("webview") is not None

    @property
    def vivo(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def abrir(self, url: str) -> bool:
        """Abre la ventana en ``url``; si ya está abierta, navega ahí."""
        if self.vivo:
            return self._orden({"cmd": "goto", "url": url})
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        args = [sys.executable, "-m", "et_suite.navegador", "--url", url,
                "--script", self.script_path, "--titulo", self.titulo]
        if self.hosts:
            args += ["--hosts", ",".join(self.hosts)]
        try:
            self.proc = subprocess.Popen(
                args, cwd=self.base_dir, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1, **kw)
        except Exception as e:
            self.salida.append(str(e))
            self.proc = None
            return False
        self._cfg_json = ""
        threading.Thread(target=self._leer, args=(self.proc,), daemon=True,
                         name="NavegadorIntegrado").start()
        if self.bridge is not None:
            threading.Thread(target=self._enviar_config, args=(self.proc,),
                             daemon=True, name="NavegadorConfig").start()
        return True

    def _enviar_config(self, proc: subprocess.Popen) -> None:
        """Reenvía al navegador la configuración del puente cuando cambia
        (AOI, tarea actual, si se está grabando)."""
        while self.proc is proc and proc.poll() is None:
            try:
                j = json.dumps(self.bridge.config() or {}, sort_keys=True)
            except Exception:
                j = ""
            if j and j != self._cfg_json:
                if self._orden({"cmd": "config", "cfg": json.loads(j)}):
                    self._cfg_json = j
            time.sleep(0.25)

    def ir(self, url: str) -> bool:
        return self.abrir(url)

    def cerrar(self) -> None:
        if not self.vivo:
            return
        self._orden({"cmd": "cerrar"})
        proc = self.proc

        def matar():
            try:
                proc.wait(timeout=4)                 # type: ignore[union-attr]
            except Exception:
                try:
                    proc.kill()                      # type: ignore[union-attr]
                except Exception:
                    pass
        threading.Thread(target=matar, daemon=True).start()

    def _orden(self, d: dict) -> bool:
        try:
            with self._lock:
                self.proc.stdin.write(json.dumps(d) + "\n")   # type: ignore[union-attr]
                self.proc.stdin.flush()                        # type: ignore[union-attr]
            return True
        except Exception:
            return False

    def _leer(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith(PREFIX):
                try:
                    ev = json.loads(line[len(PREFIX):])
                except json.JSONDecodeError:
                    continue
                if ev.get("tipo") == "web":
                    if self.bridge is not None:
                        try:
                            self.bridge.receive_raw(str(ev.get("d") or ""))
                        except Exception:
                            pass
                    continue
                if ev.get("tipo") == "aviso":
                    self.salida.append(str(ev.get("detalle", "")))
                    self.salida = self.salida[-40:]
                if ev.get("tipo") == "pagina":
                    self.url_actual = ev.get("url", "")
                    self.inyectado_actual = bool(ev.get("inyectado"))
                    h = urlparse(self.url_actual).hostname
                    if h:
                        self.hosts_visitados.add(h)
                if self.on_evento:
                    try:
                        self.on_evento(ev)
                    except Exception:
                        pass
            else:
                self.salida.append(line.rstrip())
                self.salida = self.salida[-40:]
        proc.wait()
        if self.on_evento:
            try:
                self.on_evento({"tipo": "cerrado", "codigo": proc.returncode})
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════════════════
#  Lado del NAVEGADOR (proceso aparte)
# ════════════════════════════════════════════════════════════════════════════

_print_lock = threading.Lock()


def emit(tipo: str, **datos) -> None:
    datos["tipo"] = tipo
    with _print_lock:
        try:
            sys.stdout.write(PREFIX + json.dumps(datos, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception:
            pass


# El userscript se envuelve con dos piezas:
#   1. GM_xmlhttpRequest propio, que manda las peticiones a Python (este
#      proceso) en lugar de hacer fetch desde la página.  Así el navegador no
#      bloquea ni pregunta por «acceso a la red local» (https → 127.0.0.1).
#   2. mozInnerScreenX/Y medidos con el ratón: el userscript ya los usa si
#      existen, y dan el origen EXACTO del área de contenido.
WRAPPER_JS = r"""
(function () {
  if (window.__etIntegrado) return;
  window.__etIntegrado = true;

  // Origen medido en una página anterior (la ventana no se movió): así no
  // hay que esperar a que el participante mueva el ratón en cada página.
  var origen = window.__etOrigen || null;
  function medir(e) {
    if (!e.isTrusted) return;
    origen = { x: e.screenX - e.clientX, y: e.screenY - e.clientY,
               wx: window.screenX, wy: window.screenY };
  }
  function vigente() {
    // si la ventana se movió, la medición ya no sirve hasta el próximo ratón
    return origen && origen.wx === window.screenX && origen.wy === window.screenY;
  }
  document.addEventListener('mousemove', medir, true);
  document.addEventListener('mousedown', medir, true);
  try {
    Object.defineProperty(window, 'mozInnerScreenX',
      { configurable: true, get: function () { return vigente() ? origen.x : null; } });
    Object.defineProperty(window, 'mozInnerScreenY',
      { configurable: true, get: function () { return vigente() ? origen.y : null; } });
  } catch (e) {}

  function conApi(cb, intentos) {
    var a = window.pywebview && window.pywebview.api;
    if (a && a.gm) { cb(a); return; }
    if ((intentos || 0) > 100) return;
    setTimeout(function () { conApi(cb, (intentos || 0) + 1); }, 100);
  }
  var GM_xmlhttpRequest = function (o) {
    conApi(function (a) {
      a.gm(o.method || 'GET', o.url, o.data || null).then(function (t) {
        if (t === null || t === undefined) { if (o.onerror) o.onerror(); }
        else if (o.onload) o.onload({ responseText: t, status: 200 });
      }, function () { if (o.onerror) o.onerror(); });
    });
  };

/*__USERSCRIPT__*/
})();
"""


# Última configuración recibida de la app (orden «config»).  Vive fuera de
# _Api porque pywebview expone al JavaScript lo que tenga la API.
_cfg: dict = {}
_cfg_lista = threading.Event()
MAX_EVENTO = 256 * 1024


# ── Capturas de la página («lo que vio» el participante) ─────────────────────
#
# Se captura SÓLO el área de contenido del navegador del estudio, desde el
# propio motor web (WebView2 en Windows, WKWebView en macOS): no hace falta
# el permiso de «Grabación de pantalla» de macOS y no salen otras ventanas,
# notificaciones ni el escritorio.  Sólo en las páginas de los dominios del
# estudio (nunca en el login), sólo durante una tarea, y una vez por cada
# vista distinta (posición de scroll, menú abierto…).  Con la posición de
# scroll de cada captura, el análisis reconstruye la página completa y pinta
# encima el heatmap y el recorrido de la mirada.

def _captura_windows(win, path: str, listo: Callable[[bool], None]) -> None:
    import clr  # noqa: F401  (carga pythonnet)
    from Microsoft.Web.WebView2.Core import \
        CoreWebView2CapturePreviewImageFormat as Formato
    from System import Action, Func, Object
    from System.IO import MemoryStream
    from System.Threading.Tasks import Task

    wv = win.native.browser.webview
    ms = MemoryStream()

    def fin(task) -> None:
        ok = False
        try:
            if not task.IsFaulted:
                with open(path, "wb") as f:
                    f.write(bytes(ms.ToArray()))
                ok = True
        except Exception:
            ok = False
        listo(ok)

    def empezar():
        return wv.CoreWebView2.CapturePreviewAsync(
            Formato.Jpeg, ms).ContinueWith(Action[Task](fin))

    wv.Invoke(Func[Object](empezar))


def _captura_mac(win, path: str, listo: Callable[[bool], None]) -> None:
    import AppKit
    import WebKit
    from PyObjCTools import AppHelper
    from webview.platforms.cocoa import BrowserView

    wk = BrowserView.instances[win.uid].webview

    def handler(img, _err) -> None:
        ok = False
        try:
            if img is not None:
                rep = AppKit.NSBitmapImageRep.imageRepWithData_(
                    img.TIFFRepresentation())
                tipo = getattr(AppKit, "NSBitmapImageFileTypeJPEG", 3)
                data = rep.representationUsingType_properties_(
                    tipo, {AppKit.NSImageCompressionFactor: 0.8})
                ok = bool(data.writeToFile_atomically_(path, True))
        except Exception:
            ok = False
        listo(ok)

    def empezar() -> None:
        try:
            conf = WebKit.WKSnapshotConfiguration.alloc().init()
            wk.takeSnapshotWithConfiguration_completionHandler_(conf, handler)
        except Exception:
            listo(False)

    AppHelper.callAfter(empezar)


class _Capturador:
    """Decide cuándo capturar la vista actual y avisa a la app."""

    MAX_POR_TAREA = 40
    MIN_ENTRE_S = 1.0           # entre dos capturas
    QUIETO_S = 0.5              # el scroll tiene que estar quieto
    REPETIR_S = 20.0            # la misma vista se vuelve a capturar tras esto

    def __init__(self) -> None:
        self.win = None
        self.inyectado = False
        self.vp: Optional[dict] = None
        self.t_vp = 0.0               # último CAMBIO de scroll/tamaño
        self.t_layout = 0.0           # último cambio de diseño (menú, modal…)
        self.firma_layout = ""
        self.vistos: dict = {}
        self.n: dict = {}
        self.ultimo = 0.0
        self.ocupado = False
        self.fallos = 0
        self.origen: Optional[dict] = None    # origen exacto del contenido
        self._lock = threading.Lock()

    def pagina(self, inyectado: bool) -> None:
        with self._lock:
            self.inyectado = inyectado
            self.vp = None

    def evento(self, raw: str) -> None:
        """Mira (sin copiarlos) los eventos del script: scroll y diseño."""
        try:
            d = json.loads(raw)
        except Exception:
            return
        k = d.get("k")
        now = time.monotonic()
        with self._lock:
            if k == "vp":
                if d.get("mx") is not None and d.get("my") is not None:
                    self.origen = {"x": d["mx"], "y": d["my"],
                                   "wx": d.get("wx"), "wy": d.get("wy")}
                clave = tuple(d.get(x) for x in ("sx", "sy", "iw", "ih", "url"))
                if self.vp is None or clave != tuple(
                        self.vp.get(x) for x in ("sx", "sy", "iw", "ih", "url")):
                    self.t_vp = now
                self.vp = d
            elif k == "layout":
                firma = json.dumps([(a.get("id"), a.get("vis"), a.get("w"),
                                     a.get("h")) for a in d.get("aois", [])])
                if firma != self.firma_layout:
                    self.firma_layout = firma
                    self.t_layout = now

    def bucle(self) -> None:
        while True:
            time.sleep(0.25)
            try:
                self._quizas_capturar()
            except Exception as e:
                emit("aviso", detalle=f"captura: {e}")

    def _quizas_capturar(self) -> None:
        cfg = _cfg
        task = int(cfg.get("task") or 0)
        carpeta = cfg.get("shots_dir")
        if not (cfg.get("rec") and cfg.get("capture") and task and carpeta):
            return
        now = time.monotonic()
        with self._lock:
            vp = dict(self.vp) if self.vp else None
            if (self.win is None or not self.inyectado or vp is None
                    or self.ocupado or self.fallos >= 3
                    or now - self.t_vp < self.QUIETO_S
                    or now - self.t_layout < self.QUIETO_S
                    or now - self.ultimo < self.MIN_ENTRE_S
                    or self.n.get(task, 0) >= self.MAX_POR_TAREA):
                return
            url = str(vp.get("url") or "").split("#")[0]
            vista = (task, url, vp.get("sx"), int(vp.get("sy") or 0) // 40,
                     vp.get("iw"), vp.get("ih"), self.firma_layout)
            if now - self.vistos.get(vista, -1e9) < self.REPETIR_S:
                return
            self.vistos[vista] = now
            self.ocupado = True
            self.ultimo = now
            n = self.n.get(task, 0) + 1
            self.n[task] = n
            t_inicio = now

        os.makedirs(carpeta, exist_ok=True)
        nombre = f"t{task}_{n:03d}.jpg"
        path = os.path.join(carpeta, nombre)

        def listo(ok: bool) -> None:
            with self._lock:
                self.ocupado = False
                movido = self.t_vp > t_inicio      # hubo scroll durante la captura
                if not ok:
                    self.fallos += 1
                    if self.fallos == 3:
                        emit("aviso", detalle="no se pudo capturar la página")
            if ok and movido:
                try:
                    os.remove(path)
                except OSError:
                    pass
                with self._lock:
                    self.vistos.pop(vista, None)
                return
            if ok:
                ev = {"k": "shot", "file": nombre, "task": task,
                      "url": vp.get("url"), "sx": vp.get("sx"),
                      "sy": vp.get("sy"), "iw": vp.get("iw"),
                      "ih": vp.get("ih"), "dpr": vp.get("dpr")}
                emit("web", d=json.dumps(ev))

        try:
            if sys.platform == "win32":
                _captura_windows(self.win, path, listo)
            elif sys.platform == "darwin":
                _captura_mac(self.win, path, listo)
            else:
                listo(False)
        except Exception as e:
            emit("aviso", detalle=f"captura no disponible: {e}")
            listo(False)


_capturador = _Capturador()


class _Api:
    """API que ve el JavaScript de la página: sólo ``gm``."""

    def gm(self, method: str, url: str, data: Optional[str] = None):
        path = urlparse(str(url)).path
        if path == "/config":
            # La página puede cargar antes de que llegue la primera
            # configuración: se espera un poco (corre en otro hilo).
            if not _cfg_lista.wait(timeout=5.0):
                return None
            return json.dumps(_cfg)
        if path == "/e":
            if data and len(data) <= MAX_EVENTO:
                emit("web", d=str(data))          # a la app, por stdout
                _capturador.evento(str(data))
            return json.dumps({"ok": True, "rec": bool(_cfg.get("rec")),
                               "task": _cfg.get("task", 0)})
        if path == "/ping":
            return json.dumps({"ok": True})
        return None


def ir_a(window, url: str) -> None:
    """
    Navega a ``url`` aunque ya sea la página actual.  (``load_url`` con la
    misma URL no hace nada en WebView2 y deja a pywebview esperando un
    «loaded» que no llega.)
    """
    try:
        window.run_js(f"window.location.href = {json.dumps(url)};")
    except Exception:
        try:
            window.load_url(url)
        except Exception:
            pass


# Páginas de inicio/cierre de sesión: NUNCA se inyecta nada en ellas, aunque
# estén en un dominio del estudio (ServiceNow tiene su login en el mismo
# dominio que el portal: /login.do, /login_with_sso.do, /logout.do…).
_RUTAS_LOGIN = ("login", "logout", "signin", "sign_in", "sso", "saml",
                "oauth", "auth", "password", "mfa")


def es_pagina_de_login(url: str) -> bool:
    u = urlparse(url or "")
    ruta = (u.path or "").lower()
    return any(p in ruta for p in _RUTAS_LOGIN)


def host_permitido(url: str, hosts: List[str]) -> bool:
    h = (urlparse(url or "").hostname or "").lower()
    if not h:
        return False
    if es_pagina_de_login(url):
        return False
    if h in ("127.0.0.1", "localhost"):
        return True                       # páginas propias (página de prueba)
    if not hosts:
        return True
    return h in [x.lower() for x in hosts]


def correr(url: str, hosts: List[str], script_path: str,
           titulo: str) -> None:
    import webview

    # Enlaces con target=_blank / window.open: misma ventana, no Chrome.
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    except Exception:
        pass

    with open(script_path, encoding="utf-8") as f:
        inyeccion = WRAPPER_JS.replace("/*__USERSCRIPT__*/", f.read())

    api = _Api()
    win = webview.create_window(
        titulo, url, js_api=api, maximized=True, text_select=True,
        x=0, y=0,                      # pantalla principal (la calibrada)
        confirm_close=True,
        localization={
            "global.quitConfirmation":
                "¿Cerrar el navegador del estudio? Si estás en una tarea, "
                "se dejará de registrar.",
            "global.ok": "Cerrar", "global.quit": "Cerrar",
            "global.cancel": "Seguir"})

    def on_loaded():
        try:
            actual = win.get_current_url() or ""
        except Exception:
            actual = ""
        inyectado = host_permitido(actual, hosts)
        _capturador.pagina(inyectado)
        emit("pagina", url=actual, inyectado=inyectado)
        if inyectado:
            previo = (f"window.__etOrigen = {json.dumps(_capturador.origen)};\n"
                      if _capturador.origen else "")
            try:
                win.run_js(previo + inyeccion)
            except Exception as e:
                emit("aviso", detalle=f"no se pudo inyectar: {e}")

    def leer_ordenes():
        global _cfg
        for line in sys.stdin:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("cmd") == "config" and isinstance(d.get("cfg"), dict):
                _cfg = d["cfg"]               # se reemplaza entero: sin carreras
                _cfg_lista.set()
            elif d.get("cmd") == "goto" and d.get("url"):
                ir_a(win, d["url"])
            elif d.get("cmd") == "cerrar":
                break
        # «cerrar» o la app se cerró (stdin terminó): no dejar la ventana
        # huérfana con la sesión del estudiante abierta.
        win.confirm_close = False
        try:
            win.destroy()
        except Exception:
            pass

    win.events.loaded += on_loaded
    _capturador.win = win
    threading.Thread(target=_capturador.bucle, daemon=True,
                     name="Capturas").start()
    emit("listo", url=url)
    # private_mode (por defecto): cookies y sesión sólo en memoria.
    webview.start(leer_ordenes, private_mode=True)
    emit("cerrado")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--hosts", default="")
    ap.add_argument("--script", required=True)
    ap.add_argument("--titulo", default="Estudio")
    a = ap.parse_args()
    hosts = [h.strip() for h in a.hosts.split(",") if h.strip()]
    correr(a.url, hosts, a.script, a.titulo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
