"""
tests/helpers.py  —  Utilidades para probar sin cámara ni participantes
=========================================================================
  • FakeTracker       reemplaza a GazeTracker con muestras inventadas
  • make_sample       construye un GazeSample
  • write_session     escribe un CSV + meta + web de una sesión sintética con
                      reloj virtual (instantáneo, no hace falta esperar)
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Dict, List, Optional, Tuple

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from et_suite.data_logger import DataLogger                      # noqa: E402
from et_suite.gaze_tracker import GazeSample                     # noqa: E402
from et_suite.session_meta import (new_meta, save_meta,          # noqa: E402
                                   web_path_for)


def make_sample(t_ms: float, frame_id: int, x: float, y: float,
                face: bool = True, blink: bool = False) -> GazeSample:
    return GazeSample(
        timestamp_ms=t_ms, frame_id=frame_id, gaze_x=x, gaze_y=y,
        gaze_x_raw=x, gaze_y_raw=y, pupil_size=7.0, event_type="Unknown",
        head_pitch=0.0, head_yaw=0.0, head_roll=0.0, face_detected=face,
        sample_hz=15.0, jitter_ms=5.0, calibrated=True,
        ear=0.05 if blink else 0.3, blink=blink)


class FakeTracker:
    """
    Imita a GazeTracker.  Con ``start()`` se comporta como un participante
    virtual: genera muestras a ``hz`` mirando donde diga ``gaze_fn()``.
    Las ``raw_feat`` son lineales en la posición de pantalla, de modo que el
    calibrador (método polinomial) puede entrenarse de verdad con ellas.
    """

    def __init__(self, hz: float = 30.0, screen=(1920, 1080)) -> None:
        import time
        from types import SimpleNamespace
        self._hist: List[GazeSample] = []
        self._lock = threading.Lock()
        self.calibrator = None
        self.hz = hz
        self.screen = screen
        self.gaze_fn = lambda: (screen[0] / 2, screen[1] / 2)
        self.face = True
        self.running = False
        self.camera_info = SimpleNamespace(label="Cámara simulada",
                                           width=640, height=480, index=0)
        self.fps, self.jitter_ms = hz, 1.0
        self.status, self.last_error = "simulada", None
        self.camera_index = 0
        self.cam_width, self.cam_height, self.auto_tune = 640, 480, True
        self._th = None
        self._stop = False
        self._fid = 0
        self._time = time
        self._rng = np.random.default_rng(1)

    def push(self, s: GazeSample) -> None:
        with self._lock:
            self._hist.append(s)

    def start(self) -> None:
        if self._th is not None:
            return
        self.running = True
        self._stop = False
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def stop(self) -> None:
        self._stop = True
        self.running = False

    def _run(self) -> None:
        sw, sh = self.screen
        while not self._stop:
            gx, gy = self.gaze_fn()
            gx += self._rng.normal(0, 8)
            gy += self._rng.normal(0, 8)
            self._fid += 1
            s = make_sample(self._time.perf_counter() * 1000.0, self._fid,
                            gx, gy, face=self.face)
            s.raw_feat = {"gx": gx / sw - 0.5, "gy": gy / sh - 0.5,
                          "yaw": 0.0, "pitch": 0.0, "roll": 0.0,
                          "p_yaw": 0.0, "p_pitch": 0.0, "fdx": 0.0,
                          "fdy": 0.0}
            self.push(s)
            self._time.sleep(1.0 / self.hz)

    def request_camera(self, index) -> None:
        pass

    def get_debug_frame(self):
        return None

    def get_latest(self) -> Optional[GazeSample]:
        with self._lock:
            return self._hist[-1] if self._hist else None

    def get_since(self, last_frame_id: int) -> List[GazeSample]:
        with self._lock:
            return [s for s in self._hist if s.frame_id > last_frame_id]


# ── sesión sintética con reloj virtual ──────────────────────────────────────

def write_session(directory: str, participant: str, study,
                  script: Dict[int, List[Tuple[float, float, float]]],
                  group: str = "", hz: float = 15.0, noise: float = 25.0,
                  cal_rms: float = 120.0, val_post: float = 150.0,
                  face_pct: float = 100.0, seed: int = 0,
                  outcomes: Optional[Dict[int, str]] = None,
                  web: Optional[List[dict]] = None,
                  consent: bool = True, blink_every: int = 0,
                  panel_rect: Optional[List[float]] = None,
                  screen=(1920, 1080), t_origin: float = 1_000_000.0,
                  scroll_fn=None) -> str:
    """
    Escribe una sesión completa en disco y devuelve la ruta del CSV.

    script: ``{id_tarea: [(x, y, segundos), ...]}`` — cada tupla es una
            fijación en esa posición.  Entre tareas hay 2 s de "lectura"
            (Task_ID = 0).

    Los eventos TASK_START/TASK_END y las filas se escriben con el mismo
    formato que ``DataLogger``.
    """
    import csv
    rng = np.random.default_rng(seed)
    os.makedirs(directory, exist_ok=True)
    name = f"{participant}_20261003_100000"
    csv_path = os.path.join(directory, name + ".csv")
    dt = 1000.0 / hz
    t = t_origin
    frame = 0
    rows: List[dict] = []
    tasks_meta = []
    state_vp = None

    def row(ts, x, y, ev, task, face=True, blink=False, page=None):
        r = {
            "Timestamp_ms": round(ts, 3), "Frame_ID": frame,
            "Gaze_X": round(x, 2), "Gaze_Y": round(y, 2), "Pupil_Size": 7.0,
            "Event_Type": ev, "Head_Pitch": 0, "Head_Yaw": 0, "Head_Roll": 0,
            "Sample_Hz": hz, "Jitter_ms": 5.0, "Face_Detected": int(face),
            "Task_ID": task, "Scroll_X": "", "Scroll_Y": "",
            "Page_X": "", "Page_Y": "", "Blink": int(blink),
            "Eye_Open": 0.05 if blink else 0.3}
        if page:
            r.update(Scroll_X=page["sx"], Scroll_Y=page["sy"],
                     Page_X=page["px"], Page_Y=page["py"])
        return r

    def emit_fix(x0, y0, secs, task):
        nonlocal t, frame
        n = max(1, int(secs * hz))
        for i in range(n):
            frame += 1
            face = rng.random() < face_pct / 100.0
            ev = "Unknown" if not face else (
                "Saccade" if i == 0 else "Fixation")
            blink = bool(blink_every and frame % blink_every == 0)
            if blink and face:
                ev = "Blink"
            x = x0 + rng.normal(0, noise)
            y = y0 + rng.normal(0, noise)
            page = scroll_fn(x, y, t) if scroll_fn else None
            rows.append(row(t, x, y, ev, task, face, blink, page))
            t += dt

    for tid in sorted(script):
        emit_fix(960, 1000, 2.0, 0)                     # lectura del panel
        start = t
        rows.append(row(t, 960, 540, f"EVENT:TASK_START:{tid}", tid))
        for (x, y, secs) in script[tid]:
            emit_fix(x, y, secs, tid)
        end = t
        outcome = (outcomes or {}).get(tid, "success")
        rows.append(row(t, 960, 540, f"EVENT:TASK_END:{tid}:{outcome}", tid))
        tasks_meta.append({"id": tid, "title": f"Tarea {tid}",
                           "start_ms": start, "end_ms": end,
                           "outcome": outcome})

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DataLogger.CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)

    meta = new_meta(study.id, study.name, study.version, participant, group,
                    consent={"accepted": consent},
                    env={"screen_w": screen[0], "screen_h": screen[1],
                         "screen_width_cm": 34.5, "distance_cm": 60.0})
    meta["calibration"] = {"rms_px": cal_rms}
    meta["validation"] = {"post": {"mean_px": val_post}}
    meta["tasks"] = tasks_meta
    meta["task_panel_rect"] = panel_rect
    save_meta(csv_path, meta)
    if web:
        with open(web_path_for(csv_path), "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "events": web}, f)
    return csv_path
