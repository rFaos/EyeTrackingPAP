"""
et_suite/data_logger.py  —  Grabación continua de datos de mirada
==================================================================
• Daemon thread a ~60 Hz consumiendo GazeSamples del GazeTracker
• Exporta CSV con columnas estandarizadas para análisis estadístico
• Guardado incremental: si la app se cierra mal, la sesión NO se pierde
• Estado de la sesión alineado en el tiempo: tarea actual y posición de
  scroll de la página se resuelven con la hora de cada muestra, no con la
  hora en que el grabador se entera
• Eventos externos (inicio/fin de tarea, clics, cambios de página…)
• Detector de fijaciones integrado (I-DT) para la columna Event_Type

Columnas CSV:
  Timestamp_ms, Frame_ID, Gaze_X, Gaze_Y, Pupil_Size,
  Event_Type, Head_Pitch, Head_Yaw, Head_Roll,
  Sample_Hz, Jitter_ms, Face_Detected,
  Task_ID, Scroll_X, Scroll_Y, Page_X, Page_Y, Blink, Eye_Open

Las 12 primeras son las de siempre (los CSV antiguos siguen siendo
legibles); las 7 últimas se añadieron para el análisis por tarea.
"""

from __future__ import annotations

import csv
import datetime
import json
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

from .display import screen_to_page
from .gaze_tracker import GazeTracker, GazeSample, SCREEN_W, SCREEN_H
from .pupillometry import IDTClassifier, IVTClassifier

# Cada cuánto se vuelca a disco lo grabado (segundos)
_AUTOSAVE_S = 2.0

# Campos del estado de la ventana del navegador que, si cambian, justifican
# registrar un nuevo punto en el log web.
_VP_KEYS = ("sx", "sy", "iw", "ih", "wx", "wy", "ow", "oh", "sw", "url",
            "pw", "ph")


# ════════════════════════════════════════════════════════════════════════════
#  DataLogger
# ════════════════════════════════════════════════════════════════════════════

class DataLogger:
    """
    Graba GazeSamples en CSV en un hilo de background (daemon).

    Columnas CSV:
        Timestamp_ms  — tiempo absoluto en ms (perf_counter * 1000)
        Frame_ID      — número secuencial de frame
        Gaze_X/Y      — coordenada filtrada en pantalla (px)
        Pupil_Size    — radio del iris en px (NO es la pupila clínica)
        Event_Type    — "Fixation" | "Saccade" | "Blink" | "Unknown" |
                        "EVENT:<etiqueta>"
        Head_*        — orientación de la cabeza (grados)
        Sample_Hz     — frecuencia instantánea de muestreo
        Jitter_ms     — jitter temporal (desv. estándar Δt, 30 frames)
        Face_Detected — 1 / 0
        Task_ID       — tarea en curso (0 = fuera de tarea)
        Scroll_X/Y    — scroll de la página (px CSS; vacío sin puente web)
        Page_X/Y      — mirada en coordenadas de la PÁGINA (px CSS)
        Blink         — 1 si la muestra está dentro de un parpadeo
        Eye_Open      — apertura del ojo (EAR)

    Uso::

        logger = DataLogger(tracker, output_dir="sessions")
        logger.start(session_name="P01")
        logger.start_task(1)                 # marca inicio de la tarea 1
        logger.end_task(1, "success")
        csv_path, stats = logger.stop()
    """

    CSV_FIELDS = [
        "Timestamp_ms", "Frame_ID",
        "Gaze_X", "Gaze_Y",
        "Pupil_Size",
        "Event_Type",
        "Head_Pitch", "Head_Yaw", "Head_Roll",
        "Sample_Hz", "Jitter_ms",
        "Face_Detected",
        "Task_ID",
        "Scroll_X", "Scroll_Y",
        "Page_X", "Page_Y",
        "Blink", "Eye_Open",
    ]

    def __init__(
        self,
        tracker:    GazeTracker,
        output_dir: str = "sessions",
        method:     str = "idt",
        dispersion_px: float = 200.0,
        min_fixation_ms: float = 150.0,
        screen_w:   Optional[float] = None,
        screen_h:   Optional[float] = None,
    ) -> None:
        self.tracker    = tracker
        self.output_dir = output_dir
        self.screen_w   = float(screen_w or SCREEN_W)
        self.screen_h   = float(screen_h or SCREEN_H)
        os.makedirs(output_dir, exist_ok=True)

        # I-DT por defecto: con una webcam el ruido de estimación hace que
        # I-VT etiquete casi todo como sacada (ver docstring de IDTClassifier).
        self.method = method.lower()
        if self.method == "ivt":
            self._classifier = IVTClassifier(
                vel_thresh_px_s=dispersion_px * 8.0,
                min_duration_ms=min_fixation_ms)
        else:
            self._classifier = IDTClassifier(
                dispersion_px=dispersion_px,
                min_duration_ms=min_fixation_ms)

        self._running     = False
        self._thread:     Optional[threading.Thread] = None
        self._rows:       List[dict]  = []
        self._start_t:    Optional[float] = None
        self._csv_path:   Optional[str]   = None
        self._lock        = threading.Lock()
        # Último frame_id ya grabado.  Se usa con tracker.get_since() para
        # no perder muestras cuando el bucle se retrasa.
        self._cursor:     int = 0

        # Cola de cambios de estado y eventos, ordenada por tiempo al aplicarse
        # (t_ms, secuencia, "state"|"event", carga)
        self._pending:    List[Tuple[float, int, str, object]] = []
        self._seq         = 0
        self._state:      Dict[str, object] = {"task": 0, "vp": None}
        self._last_sample: Optional[GazeSample] = None

        # Log de eventos web (viewport, layout, clics, navegación)
        self._web_log:    List[dict] = []
        self._last_logged_vp: Optional[dict] = None
        self._last_logged_vp_t: float = 0.0
        self._last_layout_json: Dict[str, str] = {}
        self.web_event_count: int = 0
        self.last_web_t: Optional[float] = None       # perf_counter (s)
        self.latest_vp: Optional[dict] = None

        # Escritura incremental
        self._fh = None
        self._writer: Optional[csv.DictWriter] = None
        self._written: int = 0
        self._last_save: float = 0.0

    # ── Interfaz pública ─────────────────────────────────────────────────

    def start(self, session_name: str = "",
              output_dir: Optional[str] = None) -> None:
        """Inicia la grabación en background."""
        if self._running:
            return
        if output_dir:
            self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{session_name}_{ts}" if session_name else ts
        self._csv_path = os.path.join(self.output_dir, f"{name}.csv")

        with self._lock:
            self._rows.clear()
            self._pending.clear()
            self._web_log.clear()
            self._state = {"task": 0, "vp": None}
        self._last_sample = None
        self._last_logged_vp = None
        self._last_logged_vp_t = 0.0
        self._last_layout_json = {}
        self.web_event_count = 0
        self.last_web_t = None
        self.latest_vp = None
        self._classifier.reset()
        self._start_t = time.perf_counter()

        # Archivo abierto desde el principio: se va volcando cada pocos s
        self._fh = open(self._csv_path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.CSV_FIELDS)
        self._writer.writeheader()
        self._fh.flush()
        self._written = 0
        self._last_save = time.perf_counter()

        # Cursor: sólo se graba lo que se produzca a partir de ahora, para
        # no arrastrar muestras anteriores al inicio de la sesión.
        _latest = self.tracker.get_latest()
        self._cursor = _latest.frame_id if _latest is not None else 0

        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="DataLogger")
        self._thread.start()
        print(f"[DataLogger] Grabando → {self._csv_path}")

    def stop(self) -> tuple[str, dict]:
        """
        Detiene la grabación, escribe el CSV y retorna (csv_path, stats).
        """
        if not self._running and self._csv_path is None:
            return "", {"error": "sin datos"}
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        try:
            self._classifier.flush(time.perf_counter())
        except Exception:
            pass

        # Muestras que llegaron entre la última vuelta del bucle y el stop
        self._consume_new()
        # Eventos posteriores a la última muestra (p. ej. TASK_END justo
        # antes de detener): antes se perdían en silencio.
        self._drain_tail()

        self._flush_csv(final=True)
        self._write_web_log()
        stats = self._compute_stats()
        print(f"[DataLogger] Sesión guardada: {self._csv_path} "
              f"({len(self._rows)} filas)")
        return self._csv_path, stats  # type: ignore[return-value]

    # ── Tareas y eventos ─────────────────────────────────────────────────

    def log_event(self, label: str, t_ms: Optional[float] = None) -> None:
        """
        Registra un evento externo con timestamp actual.
        Se insertará en el CSV como una fila con Event_Type = "EVENT:<label>".
        """
        t = time.perf_counter() * 1000.0 if t_ms is None else float(t_ms)
        self._push(t, "event", str(label))

    def set_task(self, task_id: int, t_ms: Optional[float] = None) -> None:
        """Cambia la tarea en curso a partir de este instante (0 = ninguna)."""
        t = time.perf_counter() * 1000.0 if t_ms is None else float(t_ms)
        self._push(t, "state", {"task": int(task_id)})

    def start_task(self, task_id: int) -> float:
        """Marca el inicio de una tarea.  Devuelve su instante (ms)."""
        t = time.perf_counter() * 1000.0
        self._push(t, "state", {"task": int(task_id)})
        self._push(t, "event", f"TASK_START:{int(task_id)}")
        return t

    def end_task(self, task_id: int, outcome: str = "success") -> float:
        """Marca el fin de una tarea.  Devuelve su instante (ms)."""
        t = time.perf_counter() * 1000.0
        # El evento va ANTES de limpiar la tarea, para que su fila lleve el
        # Task_ID de la tarea que termina.
        self._push(t, "event", f"TASK_END:{int(task_id)}:{outcome}")
        self._push(t, "state", {"task": 0})
        return t

    @property
    def current_task(self) -> int:
        """Tarea vigente según lo ya solicitado (no según lo ya grabado)."""
        with self._lock:
            task = int(self._state.get("task", 0) or 0)
            pend = sorted(self._pending, key=lambda p: (p[0], p[1]))
        for _, _, kind, payload in pend:
            if kind == "state" and isinstance(payload, dict) and "task" in payload:
                task = int(payload["task"])
        return task

    # ── Eventos del navegador (puente web) ───────────────────────────────

    def web_event(self, kind: str, payload: dict,
                  t_ms: Optional[float] = None) -> bool:
        """
        Recibe un evento del script del navegador.

        kind:
          "vp"      scroll / tamaño / posición de la ventana (estado)
          "layout"  posición de los elementos AOI (por selector)
          "click"   clic del participante
          "nav"     carga de una página
          "vis"     la pestaña se oculta / vuelve a ser visible

        Devuelve False si no se está grabando.
        """
        if not self._running:
            return False
        t = time.perf_counter() * 1000.0 if t_ms is None else float(t_ms)
        self.web_event_count += 1
        self.last_web_t = time.perf_counter()
        ev = {"t_ms": round(t, 3), "k": kind}
        ev.update({k: v for k, v in payload.items() if k != "k"})

        if kind == "vp":
            vp = {k: payload.get(k) for k in
                  ("sx", "sy", "iw", "ih", "ow", "oh", "wx", "wy", "sw",
                   "sh", "dpr", "pw", "ph", "mx", "my", "url")}
            self.latest_vp = vp
            self._push(t, "state", {"vp": vp})
            changed = (self._last_logged_vp is None or any(
                self._last_logged_vp.get(k) != vp.get(k) for k in _VP_KEYS))
            if changed or (t - self._last_logged_vp_t) > 5000.0:
                self._last_logged_vp = vp
                self._last_logged_vp_t = t
                with self._lock:
                    self._web_log.append(ev)
        elif kind == "layout":
            url = str(payload.get("url", ""))
            blob = json.dumps(payload.get("aois", []), sort_keys=True)
            if self._last_layout_json.get(url) != blob:
                self._last_layout_json[url] = blob
                with self._lock:
                    self._web_log.append(ev)
        else:
            with self._lock:
                self._web_log.append(ev)
            if kind == "click":
                self._push(t, "event", "CLICK")
            elif kind == "nav":
                self._push(t, "event", "PAGE")
            elif kind == "vis":
                self._push(t, "event", f"VIS:{payload.get('v', '?')}")
        return True

    # ── Propiedades ──────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        return self._running

    @property
    def csv_path(self) -> Optional[str]:
        return self._csv_path

    @property
    def n_rows(self) -> int:
        return len(self._rows)

    @property
    def start_time_ms(self) -> Optional[float]:
        return None if self._start_t is None else self._start_t * 1000.0

    # ── Cola interna ─────────────────────────────────────────────────────

    def _push(self, t_ms: float, kind: str, payload: object) -> None:
        with self._lock:
            self._seq += 1
            self._pending.append((t_ms, self._seq, kind, payload))

    # ── Loop interno ─────────────────────────────────────────────────────

    def _loop(self) -> None:
        INTERVAL = 1.0 / 60.0   # ~60 Hz

        while self._running:
            t0 = time.perf_counter()
            self._consume_new()
            if t0 - self._last_save >= _AUTOSAVE_S:
                self._flush_csv()
                self._last_save = t0
            elapsed = time.perf_counter() - t0
            sleep_t = INTERVAL - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    def _consume_new(self) -> int:
        """
        Procesa TODAS las muestras nuevas del tracker desde el cursor.

        Se piden todas, no sólo la última.  Así, aunque este bucle se retrase
        (contención del GIL con MediaPipe, ventana de calibración, carga de
        la GUI), la secuencia grabada queda completa.  Antes se llamaba a
        get_latest(), que además VACIABA una cola compartida con la GUI:
        resultado medido, 7.3 Hz grabados de 15 Hz reales.
        """
        nuevos = self.tracker.get_since(self._cursor)
        n = 0
        for sample in nuevos:
            self._cursor = sample.frame_id
            if self._start_t is None:
                continue

            # Clasificar fijación / sacada (I-DT por defecto).  Durante un
            # parpadeo la mirada estimada es basura: la muestra se etiqueta
            # "Blink" y NO entra al clasificador, así la fijación en curso
            # sobrevive al parpadeo en vez de romperse.
            if not sample.face_detected:
                sample.event_type = "Unknown"
            elif sample.blink:
                sample.event_type = "Blink"
            else:
                sample.event_type = self._classifier.update(
                    sample.gaze_x, sample.gaze_y,
                    sample.timestamp_ms / 1000.0)

            with self._lock:
                self._apply_pending(sample.timestamp_ms, sample)
                vp = self._state.get("vp")
                page = (screen_to_page(sample.gaze_x, sample.gaze_y, vp,
                                       self.screen_w)
                        if (vp and sample.face_detected) else None)
                self._rows.append(
                    self._sample_to_row(sample, self._state, page))
            self._last_sample = sample
            n += 1
        return n

    def _apply_pending(self, up_to_ms: float, ref: GazeSample) -> None:
        """Aplica estado y eventos pendientes con t <= up_to_ms (con lock)."""
        ready = [p for p in self._pending if p[0] <= up_to_ms]
        if not ready:
            return
        self._pending = [p for p in self._pending if p[0] > up_to_ms]
        ready.sort(key=lambda p: (p[0], p[1]))
        for t, _, kind, payload in ready:
            if kind == "state":
                self._state.update(payload)            # type: ignore[arg-type]
            else:
                self._rows.append(
                    self._event_row(t, str(payload), ref, self._state))

    def _drain_tail(self) -> None:
        """Escribe los eventos que quedaron después de la última muestra."""
        ref = self._last_sample
        with self._lock:
            if ref is None:
                self._pending.clear()
                return
            self._apply_pending(float("inf"), ref)

    # ── Serialización ────────────────────────────────────────────────────

    @staticmethod
    def _sample_to_row(s: GazeSample, state: Dict[str, object],
                       page: Optional[Dict[str, float]]) -> dict:
        row = {
            "Timestamp_ms":   round(s.timestamp_ms, 3),
            "Frame_ID":       s.frame_id,
            "Gaze_X":         round(s.gaze_x, 2),
            "Gaze_Y":         round(s.gaze_y, 2),
            "Pupil_Size":     round(s.pupil_size, 3),
            "Event_Type":     s.event_type,
            "Head_Pitch":     round(s.head_pitch, 3),
            "Head_Yaw":       round(s.head_yaw, 3),
            "Head_Roll":      round(s.head_roll, 3),
            "Sample_Hz":      round(s.sample_hz, 2),
            "Jitter_ms":      round(s.jitter_ms, 3),
            "Face_Detected":  int(s.face_detected),
            "Task_ID":        int(state.get("task", 0) or 0),
            "Scroll_X":       "",
            "Scroll_Y":       "",
            "Page_X":         "",
            "Page_Y":         "",
            "Blink":          int(bool(getattr(s, "blink", False))),
            "Eye_Open":       round(float(getattr(s, "ear", 0.0)), 4),
        }
        if page is not None:
            row["Scroll_X"] = round(page["sx"], 1)
            row["Scroll_Y"] = round(page["sy"], 1)
            row["Page_X"] = round(page["px"], 1)
            row["Page_Y"] = round(page["py"], 1)
        return row

    @staticmethod
    def _event_row(t_ms: float, label: str, ref: GazeSample,
                   state: Dict[str, object]) -> dict:
        """Fila especial para un evento externo."""
        row = DataLogger._sample_to_row(ref, state, None)
        row["Timestamp_ms"] = round(t_ms, 3)
        row["Event_Type"] = f"EVENT:{label}"
        return row

    def _flush_csv(self, final: bool = False) -> None:
        """Vuelca a disco las filas que aún no se han escrito."""
        if self._writer is None or self._fh is None:
            return
        with self._lock:
            pend = self._rows[self._written:]
            self._written = len(self._rows)
        try:
            if pend:
                self._writer.writerows(pend)
            self._fh.flush()
            if final:
                self._fh.close()
                self._fh = None
                self._writer = None
        except OSError as e:
            print(f"[DataLogger] No se pudo escribir el CSV: {e}")

    def _write_web_log(self) -> None:
        if not self._csv_path or not self._web_log:
            return
        path = os.path.splitext(self._csv_path)[0] + ".web.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"schema": 1, "events": self._web_log}, f)
        except OSError as e:
            print(f"[DataLogger] No se pudo guardar el log web: {e}")

    # ── Estadísticas ─────────────────────────────────────────────────────

    def _compute_stats(self) -> dict:
        with self._lock:
            rows = list(self._rows)

        if not rows:
            return {"error": "sin datos", "n_samples": 0}

        data_rows = [r for r in rows
                     if not str(r["Event_Type"]).startswith("EVENT")]
        n = len(data_rows)
        if n == 0:
            return {"n_samples": 0}

        duration_ms = (data_rows[-1]["Timestamp_ms"] -
                       data_rows[0]["Timestamp_ms"]) if n > 1 else 0.
        sample_hz   = n / max(duration_ms / 1000., 1e-4)
        face_pct    = sum(r["Face_Detected"] for r in data_rows) / n * 100.

        fixations = self._classifier.fixations
        n_fix     = len(fixations)
        mean_fix_ms = (float(np.mean([f.duration_ms for f in fixations]))
                       if fixations else 0.)
        ttff_ms = (fixations[0].start_ms - data_rows[0]["Timestamp_ms"]
                   if fixations else None)

        hz_vals = [r["Sample_Hz"] for r in data_rows if r["Sample_Hz"] > 0]
        jitter_vals = [r["Jitter_ms"] for r in data_rows]

        # Parpadeos = rachas consecutivas de filas "Blink"
        n_blinks, prev = 0, False
        for r in data_rows:
            cur = r["Event_Type"] == "Blink"
            if cur and not prev:
                n_blinks += 1
            prev = cur
        minutes = max(duration_ms / 60000.0, 1e-6)

        return {
            "n_samples":           n,
            "duration_ms":         round(duration_ms, 1),
            "mean_sample_hz":      round(float(np.mean(hz_vals)) if hz_vals else 0., 2),
            "mean_jitter_ms":      round(float(np.mean(jitter_vals)), 3),
            "face_detected_pct":   round(face_pct, 1),
            "n_fixations":         n_fix,
            "mean_fixation_ms":    round(mean_fix_ms, 1),
            "ttff_ms":             round(ttff_ms, 1) if ttff_ms is not None else None,
            "n_blinks":            n_blinks,
            "blink_rate_per_min":  round(n_blinks / minutes, 1),
            "web_events":          self.web_event_count,
            "csv_path":            self._csv_path,
        }
