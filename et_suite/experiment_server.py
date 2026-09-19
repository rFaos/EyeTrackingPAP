"""
et_suite/experiment_server.py  —  Servidor TCP para OpenSesame / PyGaze
========================================================================
Protocolo ASCII sobre TCP (localhost:4242).
Compatible con el protocolo iViewX simplificado que soporta PyGaze.

Comandos soportados (un mensaje por línea, terminado en \\n):

  START_RECORDING          → inicia DataLogger
  STOP_RECORDING           → detiene DataLogger, guarda CSV
  SET_AOI x y w h [label]  → define un AOI para TTFF
  CLEAR_AOI                → elimina AOI activo
  LOG_EVENT <label>        → inserta evento con timestamp en el CSV
  GET_GAZE                 → responde "gaze_x gaze_y\\n"
  GET_FIXATION             → responde "cx cy duration_ms\\n" o "NONE\\n"
  GET_PUPIL                → responde "pupil_size\\n"
  TTFF                     → responde ms desde inicio de AOI hasta 1ª fijación, o "NONE\\n"
  STATUS                   → responde JSON con estado del sistema
  PING                     → responde "PONG\\n"
  QUIT / DISCONNECT        → cierra conexión del cliente

Múltiples clientes soportados (un hilo por conexión).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Optional

from .gaze_tracker import GazeTracker
from .data_logger import DataLogger
from .pupillometry import AOI, IVTClassifier, TTFFTracker


class ExperimentServer:
    """
    Servidor TCP ligero para integración con OpenSesame/PyGaze.

    Uso::

        server = ExperimentServer(tracker, logger, host="127.0.0.1", port=4242)
        server.start()   # no bloquea
        # ...
        server.stop()

    Desde OpenSesame (Python inline script)::

        import socket
        s = socket.create_connection(("127.0.0.1", 4242))
        s.sendall(b"START_RECORDING\\n")
        print(s.recv(64))   # b"OK\\n"
    """

    def __init__(
        self,
        tracker:    GazeTracker,
        logger:     DataLogger,
        host:       str = "127.0.0.1",
        port:       int = 4242,
    ) -> None:
        self.tracker = tracker
        self.logger  = logger
        self.host    = host
        self.port    = port

        self._server_sock: Optional[socket.socket] = None
        self._running  = False
        self._thread:  Optional[threading.Thread] = None
        self._n_clients = 0

        # Estado del AOI / TTFF
        self._aoi:   Optional[AOI]         = None
        self._ttff:  TTFFTracker           = TTFFTracker()
        self._ivt:   IVTClassifier         = IVTClassifier()
        self._aoi_start_t: Optional[float] = None
        # Último frame_id ya procesado por TTFF.  get_latest() es ahora una
        # lectura no destructiva, así que hay que evitar alimentar el
        # clasificador dos veces con la misma muestra.
        self._last_frame_id: int = -1

    # ── Interfaz pública ─────────────────────────────────────────────────

    def start(self) -> None:
        """Inicia el servidor en un daemon thread."""
        self._running = True
        self._thread  = threading.Thread(
            target=self._serve, daemon=True, name="ExperimentServer")
        self._thread.start()
        print(f"[ExperimentServer] Escuchando en {self.host}:{self.port}")

    def stop(self) -> None:
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def n_clients(self) -> int:
        return self._n_clients

    # ── Servidor principal ────────────────────────────────────────────────

    def _serve(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
            srv.listen(5)
            srv.settimeout(1.0)
            self._server_sock = srv

            while self._running:
                try:
                    conn, addr = srv.accept()
                    self._n_clients += 1
                    t = threading.Thread(
                        target=self._handle_client,
                        args=(conn, addr),
                        daemon=True,
                        name=f"ETClient-{addr[1]}")
                    t.start()
                except socket.timeout:
                    continue
                except OSError:
                    break

    def _handle_client(self, conn: socket.socket,
                        addr: tuple) -> None:
        print(f"[ExperimentServer] Cliente conectado: {addr}")
        buf = ""
        try:
            conn.settimeout(60.0)
            while self._running:
                try:
                    data = conn.recv(256)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data.decode("ascii", errors="ignore")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    response = self._dispatch(line)
                    conn.sendall((response + "\n").encode("ascii"))
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            conn.close()
            self._n_clients -= 1
            print(f"[ExperimentServer] Cliente desconectado: {addr}")

    # ── Dispatcher de comandos ────────────────────────────────────────────

    def _dispatch(self, cmd: str) -> str:
        parts = cmd.split()
        if not parts:
            return "ERROR empty command"
        verb = parts[0].upper()

        # ── PING ─────────────────────────────────────────────────────────
        if verb == "PING":
            return "PONG"

        # ── Grabación ────────────────────────────────────────────────────
        elif verb == "START_RECORDING":
            name = parts[1] if len(parts) > 1 else "opensesame"
            if not self.logger.is_recording:
                self.logger.start(session_name=name)
                self._ivt.reset()
                return "OK"
            return "ALREADY_RECORDING"

        elif verb == "STOP_RECORDING":
            if self.logger.is_recording:
                csv_path, _ = self.logger.stop()
                return f"OK {csv_path}"
            return "NOT_RECORDING"

        # ── AOI ──────────────────────────────────────────────────────────
        elif verb == "SET_AOI":
            try:
                x, y, w, h = float(parts[1]), float(parts[2]), \
                              float(parts[3]), float(parts[4])
                label = parts[5] if len(parts) > 5 else "AOI"
                self._aoi = AOI(x=x, y=y, width=w, height=h, label=label)
                self._aoi_start_t = time.perf_counter()
                self._ttff.start(self._aoi, stimulus_label=label,
                                  timestamp_s=self._aoi_start_t)
                return f"OK AOI:{label} ({x},{y},{w},{h})"
            except (IndexError, ValueError):
                return "ERROR usage: SET_AOI x y w h [label]"

        elif verb == "CLEAR_AOI":
            self._aoi = None
            self._aoi_start_t = None
            return "OK"

        # ── LOG_EVENT ────────────────────────────────────────────────────
        elif verb == "LOG_EVENT":
            label = " ".join(parts[1:]) if len(parts) > 1 else "EVENT"
            self.logger.log_event(label)
            return "OK"

        # ── GET_GAZE ─────────────────────────────────────────────────────
        elif verb == "GET_GAZE":
            s = self.tracker.get_latest()
            if s:
                return f"{int(s.gaze_x)} {int(s.gaze_y)}"
            return "NONE"

        # ── GET_FIXATION ─────────────────────────────────────────────────
        elif verb == "GET_FIXATION":
            fxs = self._ivt.fixations
            if fxs:
                f = fxs[-1]
                return f"{int(f.cx)} {int(f.cy)} {f.duration_ms:.1f}"
            return "NONE"

        # ── GET_PUPIL ────────────────────────────────────────────────────
        elif verb == "GET_PUPIL":
            s = self.tracker.get_latest()
            if s:
                return f"{s.pupil_size:.3f}"
            return "NONE"

        # ── TTFF ─────────────────────────────────────────────────────────
        elif verb == "TTFF":
            s = self.tracker.get_latest()
            if (s and self._aoi and self._aoi_start_t
                    and s.frame_id != self._last_frame_id):
                self._last_frame_id = s.frame_id
                ev = self._ivt.update(s.gaze_x, s.gaze_y,
                                       s.timestamp_ms / 1000.)
                ttff = self._ttff.update(s.gaze_x, s.gaze_y,
                                          s.timestamp_ms / 1000., ev)
                if ttff is not None:
                    return f"{ttff:.1f}"
            return "NONE"

        # ── STATUS ───────────────────────────────────────────────────────
        elif verb == "STATUS":
            s = self.tracker.get_latest()
            status = {
                "calibrated":   self.tracker.calibrator.is_calibrated,
                "recording":    self.logger.is_recording,
                "face":         s.face_detected if s else False,
                "fps":          round(self.tracker.fps, 1),
                "jitter_ms":    round(self.tracker.jitter_ms, 2),
                "gaze_x":       int(s.gaze_x) if s else 0,
                "gaze_y":       int(s.gaze_y) if s else 0,
                "pupil_size":   round(s.pupil_size, 2) if s else 0,
                "event_type":   s.event_type if s else "Unknown",
                "n_fixations":  len(self._ivt.fixations),
                "aoi":          self._aoi.label if self._aoi else None,
            }
            return json.dumps(status)

        # ── QUIT / DISCONNECT ────────────────────────────────────────────
        elif verb in ("QUIT", "DISCONNECT", "BYE"):
            return "BYE"

        else:
            return f"ERROR unknown command: {verb}"
