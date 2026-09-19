"""
et_suite/data_logger.py  —  Grabación continua de datos de mirada
==================================================================
• Daemon thread a ~60 Hz consumiendo GazeSamples del GazeTracker
• Exporta CSV con columnas estandarizadas para análisis estadístico
• Soporte de eventos externos (triggers de OpenSesame, etc.)
• Cálculo de estadísticas de sesión al cerrar
• Detector de fijaciones integrado (IVT) para la columna Event_Type

Columnas CSV:
  Timestamp_ms, Frame_ID, Gaze_X, Gaze_Y, Pupil_Size,
  Event_Type, Head_Pitch, Head_Yaw, Head_Roll,
  Sample_Hz, Jitter_ms, Face_Detected
"""

from __future__ import annotations

import csv
import datetime
import os
import threading
import time
from typing import Dict, List, Optional

import numpy as np

from .gaze_tracker import GazeTracker, GazeSample
from .pupillometry import IDTClassifier, IVTClassifier


# ════════════════════════════════════════════════════════════════════════════
#  DataLogger
# ════════════════════════════════════════════════════════════════════════════

class DataLogger:
    """
    Graba GazeSamples en CSV en un hilo de background (daemon).

    Columnas CSV:
        Timestamp_ms  — tiempo absoluto en ms (perf_counter * 1000)
        Frame_ID      — número secuencial de frame
        Gaze_X        — coordenada X filtrada en pantalla (px)
        Gaze_Y        — coordenada Y filtrada en pantalla (px)
        Pupil_Size    — radio del iris en px
        Event_Type    — "Fixation" | "Saccade" | "Unknown" | "EVENT:<label>"
        Head_Pitch    — grados
        Head_Yaw      — grados
        Head_Roll     — grados
        Sample_Hz     — frecuencia instantánea de muestreo
        Jitter_ms     — jitter temporal (desv. estándar Δt, ventana 30 frames)
        Face_Detected — 1 / 0

    Uso::

        logger = DataLogger(tracker, output_dir="sessions")
        logger.start(session_name="nina_laura_01")
        # ... sesión en curso ...
        logger.log_event("STIMULUS_START")   # event externo
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
    ]

    def __init__(
        self,
        tracker:    GazeTracker,
        output_dir: str = "sessions",
        method:     str = "idt",
        dispersion_px: float = 200.0,
        min_fixation_ms: float = 150.0,
    ) -> None:
        self.tracker    = tracker
        self.output_dir = output_dir
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
        self._events:     List[tuple] = []   # (timestamp_ms, label) externos
        self._start_t:    Optional[float] = None
        self._csv_path:   Optional[str]   = None
        self._lock        = threading.Lock()
        # Último frame_id ya grabado.  Se usa con tracker.get_since() para
        # no perder muestras cuando el bucle se retrasa.
        self._cursor:     int = 0

    # ── Interfaz pública ─────────────────────────────────────────────────

    def start(self, session_name: str = "") -> None:
        """Inicia la grabación en background."""
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{session_name}_{ts}" if session_name else ts
        self._csv_path = os.path.join(self.output_dir, f"{name}.csv")

        with self._lock:
            self._rows.clear()
            self._events.clear()
        self._classifier.reset()
        self._start_t = time.perf_counter()
        self._running = True

        # Cursor: sólo se graba lo que se produzca a partir de ahora, para
        # no arrastrar muestras anteriores al inicio de la sesión.
        _latest = self.tracker.get_latest()
        self._cursor = _latest.frame_id if _latest is not None else 0

        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="DataLogger")
        self._thread.start()
        print(f"[DataLogger] Grabando → {self._csv_path}")

    def stop(self) -> tuple[str, dict]:
        """
        Detiene la grabación, escribe el CSV y retorna (csv_path, stats).
        """
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        self._classifier.flush(time.perf_counter())
        self._flush_csv()
        stats = self._compute_stats()
        print(f"[DataLogger] Sesión guardada: {self._csv_path} "
              f"({len(self._rows)} muestras)")
        return self._csv_path, stats  # type: ignore[return-value]

    def log_event(self, label: str) -> None:
        """
        Registra un evento externo con timestamp actual.
        Se insertará en el CSV como una fila con Event_Type = "EVENT:<label>".
        """
        t_ms = time.perf_counter() * 1000.0
        with self._lock:
            self._events.append((t_ms, label))

    @property
    def is_recording(self) -> bool:
        return self._running

    @property
    def csv_path(self) -> Optional[str]:
        return self._csv_path

    # ── Loop interno ─────────────────────────────────────────────────────

    def _loop(self) -> None:
        INTERVAL = 1.0 / 60.0   # ~60 Hz

        while self._running:
            t0 = time.perf_counter()

            # Se piden TODAS las muestras nuevas desde el cursor, no sólo la
            # última.  Así, aunque este bucle se retrase (contención del GIL
            # con MediaPipe, ventana de calibración, carga de la GUI), la
            # secuencia grabada queda completa.
            #
            # Antes se llamaba a get_latest(), que además VACIABA una cola
            # compartida con la GUI: el que llegaba primero se llevaba la
            # muestra.  Resultado medido: 7.3 Hz grabados de 15 Hz reales,
            # con huecos de hasta 736 ms, lo que rompía la detección de
            # fijaciones y producía scanpaths ilegibles.
            nuevos = self.tracker.get_since(self._cursor)

            for sample in nuevos:
                self._cursor = sample.frame_id
                if self._start_t is None:
                    continue

                # Clasificar fijación / sacada (I-DT por defecto)
                if sample.face_detected:
                    sample.event_type = self._classifier.update(
                        sample.gaze_x, sample.gaze_y,
                        sample.timestamp_ms / 1000.0)
                else:
                    sample.event_type = "Unknown"

                row = self._sample_to_row(sample)

                # Insertar eventos externos pendientes antes de esta muestra
                with self._lock:
                    for ev_t, ev_label in self._events:
                        if ev_t <= sample.timestamp_ms:
                            self._rows.append(
                                self._event_row(ev_t, ev_label, sample))
                    self._events = [(t, l) for t, l in self._events
                                    if t > sample.timestamp_ms]
                    self._rows.append(row)

            elapsed = time.perf_counter() - t0
            sleep_t = INTERVAL - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    # ── Serialización ────────────────────────────────────────────────────

    @staticmethod
    def _sample_to_row(s: GazeSample) -> dict:
        return {
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
        }

    @staticmethod
    def _event_row(t_ms: float, label: str, ref: GazeSample) -> dict:
        """Fila especial para un evento externo."""
        return {
            "Timestamp_ms":   round(t_ms, 3),
            "Frame_ID":       ref.frame_id,
            "Gaze_X":         round(ref.gaze_x, 2),
            "Gaze_Y":         round(ref.gaze_y, 2),
            "Pupil_Size":     round(ref.pupil_size, 3),
            "Event_Type":     f"EVENT:{label}",
            "Head_Pitch":     round(ref.head_pitch, 3),
            "Head_Yaw":       round(ref.head_yaw, 3),
            "Head_Roll":      round(ref.head_roll, 3),
            "Sample_Hz":      round(ref.sample_hz, 2),
            "Jitter_ms":      round(ref.jitter_ms, 3),
            "Face_Detected":  int(ref.face_detected),
        }

    def _flush_csv(self) -> None:
        if not self._csv_path or not self._rows:
            return
        with self._lock:
            rows_copy = list(self._rows)
        with open(self._csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.CSV_FIELDS)
            w.writeheader()
            w.writerows(rows_copy)

    # ── Estadísticas ─────────────────────────────────────────────────────

    def _compute_stats(self) -> dict:
        with self._lock:
            rows = list(self._rows)

        if not rows:
            return {"error": "sin datos"}

        data_rows = [r for r in rows if not str(r["Event_Type"]).startswith("EVENT")]
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
        ttff_ms = fixations[0].start_ms - data_rows[0]["Timestamp_ms"] if fixations else None

        hz_vals = [r["Sample_Hz"] for r in data_rows if r["Sample_Hz"] > 0]
        jitter_vals = [r["Jitter_ms"] for r in data_rows]

        return {
            "n_samples":           n,
            "duration_ms":         round(duration_ms, 1),
            "mean_sample_hz":      round(float(np.mean(hz_vals)) if hz_vals else 0., 2),
            "mean_jitter_ms":      round(float(np.mean(jitter_vals)), 3),
            "face_detected_pct":   round(face_pct, 1),
            "n_fixations":         n_fix,
            "mean_fixation_ms":    round(mean_fix_ms, 1),
            "ttff_ms":             round(ttff_ms, 1) if ttff_ms is not None else None,
            "csv_path":            self._csv_path,
        }
