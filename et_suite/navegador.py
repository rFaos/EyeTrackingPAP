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

Se ejecuta como proceso aparte porque Tkinter (la app) y el navegador
necesitan, cada uno, el hilo principal en macOS::

    python -m et_suite.navegador --url URL --hosts a.com,b.com --port 4243

Órdenes por stdin (una línea JSON):  {"cmd": "goto", "url": ...} · {"cmd": "cerrar"}
Eventos por stdout:                   @@EV {"tipo": "listo" | "pagina" | "cerrado", ...}
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
import urllib.request
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

        nav = NavegadorIntegrado(base_dir, script, port=4243, hosts=[...])
        nav.abrir("https://portal.udem.edu/")     # abre o navega
        nav.cerrar()
    """

    def __init__(self, base_dir: str, script_path: str, port: int,
                 hosts: List[str], titulo: str = "Estudio",
                 on_evento: Optional[Callable[[dict], None]] = None) -> None:
        self.base_dir = base_dir
        self.script_path = script_path
        self.port = port
        self.hosts = list(hosts)
        self.titulo = titulo
        self.on_evento = on_evento
        self.proc: Optional[subprocess.Popen] = None
        self.hosts_visitados: Set[str] = set()
        self.url_actual = ""
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
                "--port", str(self.port), "--script", self.script_path,
                "--titulo", self.titulo]
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
        threading.Thread(target=self._leer, args=(self.proc,), daemon=True,
                         name="NavegadorIntegrado").start()
        return True

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
                if ev.get("tipo") == "pagina":
                    self.url_actual = ev.get("url", "")
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

  var origen = null;
  function medir(e) {
    if (!e.isTrusted) return;
    origen = { x: e.screenX - e.clientX, y: e.screenY - e.clientY };
  }
  document.addEventListener('mousemove', medir, true);
  document.addEventListener('mousedown', medir, true);
  try {
    Object.defineProperty(window, 'mozInnerScreenX',
      { configurable: true, get: function () { return origen ? origen.x : null; } });
    Object.defineProperty(window, 'mozInnerScreenY',
      { configurable: true, get: function () { return origen ? origen.y : null; } });
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


class _Api:
    """API que ve el JavaScript de la página.  Estado en atributos con «_»."""

    def __init__(self, port: int) -> None:
        self._port = port
        # Sin proxy: 127.0.0.1 tiene que ir directo aunque el sistema
        # tenga un proxy configurado.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def gm(self, method: str, url: str, data: Optional[str] = None):
        u = urlparse(str(url))
        if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost"):
            return None                       # sólo el puente local
        # El userscript trae el puerto 4243 fijo; se manda al puerto real
        # del puente, por si el estudio usa otro.
        url = f"http://127.0.0.1:{self._port}{u.path}" + (f"?{u.query}" if u.query else "")
        body = data.encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, method=method or "GET",
                                     headers={"Content-Type": "text/plain"})
        try:
            with self._opener.open(req, timeout=2.0) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
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


def host_permitido(url: str, hosts: List[str]) -> bool:
    h = (urlparse(url or "").hostname or "").lower()
    if not h:
        return False
    if h in ("127.0.0.1", "localhost"):
        return True                       # páginas propias (página de prueba)
    if not hosts:
        return True
    return h in [x.lower() for x in hosts]


def correr(url: str, hosts: List[str], port: int, script_path: str,
           titulo: str) -> None:
    import webview

    # Enlaces con target=_blank / window.open: misma ventana, no Chrome.
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    except Exception:
        pass

    with open(script_path, encoding="utf-8") as f:
        inyeccion = WRAPPER_JS.replace("/*__USERSCRIPT__*/", f.read())

    api = _Api(port)
    win = webview.create_window(
        titulo, url, js_api=api, maximized=True, text_select=True,
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
        emit("pagina", url=actual, inyectado=inyectado)
        if inyectado:
            try:
                win.run_js(inyeccion)
            except Exception as e:
                emit("aviso", detalle=f"no se pudo inyectar: {e}")

    def leer_ordenes():
        for line in sys.stdin:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("cmd") == "goto" and d.get("url"):
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
    emit("listo", url=url)
    # private_mode (por defecto): cookies y sesión sólo en memoria.
    webview.start(leer_ordenes, private_mode=True)
    emit("cerrado")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--hosts", default="")
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--script", required=True)
    ap.add_argument("--titulo", default="Estudio")
    a = ap.parse_args()
    hosts = [h.strip() for h in a.hosts.split(",") if h.strip()]
    correr(a.url, hosts, a.port, a.script, a.titulo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
