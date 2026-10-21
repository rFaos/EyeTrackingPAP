"""
et_suite/web_bridge.py  —  Puente entre el navegador y el grabador
====================================================================
Servidor HTTP minúsculo (sólo localhost, puerto 4243 por defecto) que recibe
del navegador del participante lo que el eye tracker NO puede saber solo:

  • posición de scroll y tamaño/posición de la ventana  → convertir la mirada
    de "píxeles de pantalla" a "píxeles de la página" aunque haya scroll
  • posición real de cada elemento AOI (por selector CSS)  → saber QUÉ
    elemento miró, en cualquier página, sin medir nada a mano
  • clics y páginas visitadas                              → métricas de
    proceso: clics por tarea, páginas por tarea, retrocesos

El lado del navegador es ``web/eyetracking_bridge.user.js`` (userscript de
Tampermonkey para sitios de terceros, o ``<script>`` para páginas propias).

Protocolo (JSON):
    GET  /ping      → {"ok": true}
    GET  /config    → {"rec", "task", "study", "aois": [...], "hosts": [...]}
    POST /e         → un evento (cuerpo JSON).  Responde {"ok", "rec", "task"}
    GET  /e?d=<json url-encoded>   (alternativa sin POST)
    GET  /<archivo>  → sólo los archivos de ``static_files`` (la página de
                       prueba y el script), para abrirlos en el mismo origen
                       y evitar permisos de red local del navegador

El script es opcional: sin él, el prototipo sigue funcionando y las métricas
por AOI usan rectángulos manuales en coordenadas de pantalla.

Modo sin red (``start(listen=False)``, el normal con el navegador
integrado): NO se abre ningún puerto.  El navegador integrado entrega los
eventos por su propio canal (stdin/stdout del proceso, ver ``navegador.py``)
con ``receive_raw``, así que ni el firewall ni el antivirus ven un servidor.
El servidor HTTP sólo hace falta para páginas propias servidas en
``127.0.0.1`` (la página de prueba) o para Tampermonkey.

Seguridad: escucha sólo en 127.0.0.1.  Acepta cualquier origen (CORS *)
porque el navegador del participante necesita llamarlo desde el sitio
evaluado; en un laboratorio controlado el riesgo es nulo.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

MAX_BODY = 256 * 1024     # 256 KB por evento
VALID_KINDS = ("vp", "layout", "click", "nav", "vis", "shot", "mouse")


class _Server(ThreadingHTTPServer):
    """
    En Windows, ``SO_REUSEADDR`` (que HTTPServer activa por defecto) deja que
    DOS programas escuchen en el mismo puerto: si la app se abría dos veces,
    los eventos del navegador se repartían al azar entre ambas y se perdían
    sin aviso.  Aquí el puerto se reserva en exclusiva.
    """
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET,
                                   socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class WebBridge:
    """
    Servidor del puente web.

    Args:
        sink:   ``sink(kind, payload, t_ms) -> bool`` — normalmente
                ``DataLogger.web_event``.
        config: ``config() -> dict`` — lo que se devuelve en ``/config``
                (selectores de AOI, hosts permitidos, estado de grabación).
    """

    def __init__(self,
                 sink: Callable[[str, dict, float], bool],
                 config: Callable[[], dict],
                 host: str = "127.0.0.1",
                 port: int = 4243,
                 static_files: Optional[Dict[str, str]] = None) -> None:
        self.sink = sink
        self.config = config
        # {"/pagina_prueba.html": "/ruta/absoluta/pagina_prueba.html"}
        self.static_files = dict(static_files or {})
        self.host = host
        self.port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._local = False                         # modo sin red
        self.n_events = 0
        self.last_event_t: Optional[float] = None     # perf_counter (s)
        self.last_error: Optional[str] = None

    # ── ciclo de vida ────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._httpd is not None or self._local

    @property
    def listening(self) -> bool:
        """True si hay un servidor HTTP escuchando en ``127.0.0.1``."""
        return self._httpd is not None

    def start(self, listen: bool = True) -> bool:
        """
        Arranca el puente.  Con ``listen=False`` no abre ningún puerto: los
        eventos llegan sólo por ``receive_raw``.  Devuelve False (y guarda
        el motivo) si falla.
        """
        if self._httpd is not None:
            return True
        if not listen:
            self._local = True
            self.last_error = None
            return True
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # ── utilidades ──
            def log_message(self, *_a):          # silencio: 10 req/s ensucian
                pass

            def _cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods",
                                 "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Access-Control-Allow-Private-Network",
                                 "true")
                self.send_header("Access-Control-Max-Age", "600")

            def _json(self, obj: dict, code: int = 200) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _static(self, path: str) -> None:
                try:
                    with open(path, "rb") as f:
                        body = f.read()
                except OSError:
                    self._json({"ok": False, "error": "no existe"}, 404)
                    return
                ctype = ("text/html; charset=utf-8" if path.endswith(".html")
                         else "application/javascript; charset=utf-8")
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _handle_event(self, raw: bytes) -> None:
                try:
                    ack = bridge.receive_raw(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as e:
                    self._json({"ok": False, "error": str(e)}, 400)
                    return
                self._json(ack)

            # ── métodos ──
            def do_OPTIONS(self) -> None:         # preflight CORS / PNA
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:
                u = urlparse(self.path)
                if u.path == "/ping":
                    self._json({"ok": True})
                elif u.path == "/config":
                    self._json(bridge._safe_config())
                elif u.path == "/e":
                    q = parse_qs(u.query).get("d", [""])[0]
                    self._handle_event(q.encode("utf-8"))
                elif u.path in bridge.static_files:
                    self._static(bridge.static_files[u.path])
                else:
                    self._json({"ok": False, "error": "no existe"}, 404)

            def do_POST(self) -> None:
                u = urlparse(self.path)
                if u.path != "/e":
                    self._json({"ok": False, "error": "no existe"}, 404)
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    n = 0
                if n <= 0 or n > MAX_BODY:
                    if 0 < n <= 8 * MAX_BODY:       # vaciar para no cortar al cliente
                        try:
                            self.rfile.read(n)
                        except Exception:
                            pass
                    self._json({"ok": False, "error": "tamaño inválido"}, 413)
                    return
                self._handle_event(self.rfile.read(n))

        try:
            httpd = _Server((self.host, self.port), Handler)
        except OSError as e:
            self.last_error = f"puerto {self.port} ocupado: {e}"
            print(f"[WebBridge] {self.last_error}")
            return False
        httpd.daemon_threads = True
        self.port = httpd.server_address[1]       # útil si se pidió el puerto 0
        self._httpd = httpd
        self.last_error = None
        self._thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.25},
            daemon=True, name="WebBridge")
        self._thread.start()
        print(f"[WebBridge] Escuchando en http://{self.host}:{self.port}")
        return True

    def stop(self) -> None:
        self._local = False
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ── eventos ──────────────────────────────────────────────────────────

    def ack(self) -> dict:
        """Respuesta a un evento: estado de grabación y tarea actual."""
        cfg = self._safe_config()
        return {"ok": True, "rec": bool(cfg.get("rec")),
                "task": cfg.get("task", 0)}

    def receive_raw(self, raw: str) -> dict:
        """
        Registra un evento (JSON en texto) del script del navegador y
        devuelve el acuse.  Lanza ``ValueError`` si el evento es inválido.
        """
        if len(raw) > MAX_BODY:
            raise ValueError("evento demasiado grande")
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("se esperaba un objeto JSON")
        kind = str(d.get("k", ""))
        if kind not in VALID_KINDS:
            raise ValueError(f"tipo desconocido: {kind!r}")
        self._receive(kind, d)
        return self.ack()

    # ── internos ─────────────────────────────────────────────────────────

    def _safe_config(self) -> dict:
        try:
            return self.config() or {}
        except Exception as e:                      # nunca tumbar al cliente
            return {"rec": False, "error": str(e)}

    def _receive(self, kind: str, payload: Dict) -> None:
        self.n_events += 1
        self.last_event_t = time.perf_counter()
        try:
            self.sink(kind, payload, time.perf_counter() * 1000.0)
        except Exception as e:
            self.last_error = f"error al registrar evento: {e}"
            print(f"[WebBridge] {self.last_error}")

    def seconds_since_last_event(self) -> Optional[float]:
        if self.last_event_t is None:
            return None
        return time.perf_counter() - self.last_event_t
