"""
et_suite/validation_engine.py  —  Validación de precisión
===========================================================
Mide la precisión REAL del eye tracker con un participante concreto:
muestra 5 puntos, registra a dónde estima el sistema que mira la persona y
calcula la distancia al punto verdadero.

Por qué hace falta además de la calibración:
  • El error de la calibración (RMS) se mide sobre los MISMOS puntos con los
    que se ajustó el modelo, así que es optimista.  La validación usa puntos
    distintos y mide cuánto se equivoca el sistema de verdad.
  • Repetida AL FINAL de la sesión revela la deriva: si la persona se movió
    o se cansó, el error sube, y esa sesión puede excluirse.

Los 5 puntos NO coinciden con los de la calibración (que están a 10 %/50 %/90 %
de la pantalla).  Resultado en píxeles y en grados de ángulo visual.
"""

from __future__ import annotations

import math
import time
import tkinter as tk
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .display import detect_screen_width_cm, px_to_deg
from .gaze_tracker import GazeTracker, SCREEN_W, SCREEN_H

_BG, _WAIT_C, _REC_C, _ERR_C, _DIM = ("#000000", "#FFD700", "#00FF88",
                                       "#FF4444", "#888888")

# Puntos de validación (fracción de pantalla): centro y cuatro intermedios
VALIDATION_POINTS: List[Tuple[float, float]] = [
    (0.50, 0.50), (0.30, 0.30), (0.70, 0.30), (0.30, 0.70), (0.70, 0.70),
]


def summarize(per_point: List[Dict], screen_w_px: float,
              screen_w_cm: float, distance_cm: float) -> Optional[Dict]:
    """Agrega el error de cada punto válido.  None si hay menos de 3."""
    ok = [p for p in per_point if p.get("err_px") is not None]
    if len(ok) < 3:
        return None
    errs = [p["err_px"] for p in ok]
    mean_px = float(np.mean(errs))
    return {
        "mean_px": mean_px,
        "median_px": float(np.median(errs)),
        "max_px": float(np.max(errs)),
        "mean_deg": px_to_deg(mean_px, screen_w_px, screen_w_cm, distance_cm),
        "precision_px": float(np.mean([p["spread_px"] for p in ok])),
        "n_points": len(ok),
        "per_point": per_point,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


class ValidationEngine:
    """
    Ventana de validación a pantalla completa.

        eng = ValidationEngine(tracker, on_done=callback)
        eng.run(master=root)     # no bloquea; callback(result | None)

    ``result`` es un dict (ver ``summarize``) o ``None`` si se canceló o no
    hubo datos suficientes.
    """

    WAIT_SEC = 1.3          # tiempo para llevar la mirada al punto
    RECORD_SEC = 1.4        # tiempo de captura
    MIN_SAMPLES = 5         # muestras mínimas para dar por bueno un punto

    def __init__(self, tracker: GazeTracker,
                 on_done: Optional[Callable[[Optional[Dict]], None]] = None,
                 title: str = "Verificación de precisión",
                 screen_w: int = SCREEN_W, screen_h: int = SCREEN_H,
                 screen_w_cm: Optional[float] = None,
                 distance_cm: float = 60.0) -> None:
        self.tracker = tracker
        self.on_done = on_done
        self.title = title
        self.sw, self.sh = screen_w, screen_h
        if screen_w_cm is None:
            screen_w_cm, _ = detect_screen_width_cm()
        self.screen_w_cm = screen_w_cm
        self.distance_cm = distance_cm
        self.targets = list(VALIDATION_POINTS)

        self._step = 0
        self._phase = "WAIT"
        self._phase_start = 0.0
        self._cursor = 0
        self._xs: List[float] = []
        self._ys: List[float] = []
        self._per_point: List[Dict] = []
        self._pulse = 18.0
        self._pulse_dir = 1.0
        self._closed = False
        self._notified = False
        self._result: Optional[Dict] = None

        self._root: Optional[tk.Misc] = None
        self._canvas: Optional[tk.Canvas] = None
        self._lbl: Optional[tk.Label] = None
        self._prog: Optional[tk.Label] = None

    # ── interfaz ─────────────────────────────────────────────────────────

    def run(self, master: Optional[tk.Misc] = None) -> None:
        self._step, self._phase = 0, "WAIT"
        self._phase_start = time.perf_counter()
        self._per_point = []
        self._closed = self._notified = False
        self._skip_to_now()

        root = tk.Tk() if master is None else tk.Toplevel(master)
        self._root = root
        root.title(self.title)
        root.configure(bg=_BG)
        try:
            root.attributes("-fullscreen", True)
        except Exception:
            pass
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        root.bind("<Escape>", lambda _e: self._cancel())

        self._canvas = tk.Canvas(root, bg=_BG, width=self.sw, height=self.sh,
                                 highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)
        self._lbl = tk.Label(root, text=self.title, fg="white", bg=_BG,
                             font=("Helvetica", 22, "bold"))
        self._lbl.place(relx=0.5, rely=0.06, anchor="center")
        self._prog = tk.Label(root, text="", fg=_DIM, bg=_BG,
                              font=("Helvetica", 15))
        self._prog.place(relx=0.5, rely=0.94, anchor="center")
        tk.Label(root, text="ESC para omitir", fg="#444444", bg=_BG,
                 font=("Helvetica", 10)).place(relx=0.99, rely=0.99,
                                               anchor="se")
        try:
            root.lift()
            root.focus_force()
        except Exception:
            pass
        root.after(400, self._tick)
        if master is None:
            root.mainloop()

    # ── bucle ────────────────────────────────────────────────────────────

    def _skip_to_now(self) -> None:
        latest = self.tracker.get_latest()
        self._cursor = latest.frame_id if latest is not None else 0

    def _target_px(self) -> Tuple[int, int]:
        tx, ty = self.targets[self._step]
        return int(tx * self.sw), int(ty * self.sh)

    def _draw(self, color: str, r: int) -> None:
        c = self._canvas
        if c is None:
            return
        c.delete("dot")
        tx, ty = self._target_px()
        c.create_oval(tx - r - 10, ty - r - 10, tx + r + 10, ty + r + 10,
                      outline=color, width=3, fill="", tags="dot")
        c.create_oval(tx - r, ty - r, tx + r, ty + r, fill=color,
                      outline="", tags="dot")
        c.create_oval(tx - 5, ty - 5, tx + 5, ty + 5, fill="white",
                      outline="", tags="dot")

    def _drain(self) -> None:
        for s in self.tracker.get_since(self._cursor):
            self._cursor = s.frame_id
            if s.face_detected and not getattr(s, "blink", False):
                self._xs.append(float(s.gaze_x))
                self._ys.append(float(s.gaze_y))

    def _tick(self) -> None:
        if self._closed or not (self._root and self._lbl and self._prog):
            return
        now = time.perf_counter()
        el = now - self._phase_start
        n = len(self.targets)

        if self._phase == "WAIT":
            self._pulse += 0.5 * self._pulse_dir
            if self._pulse > 28 or self._pulse < 14:
                self._pulse_dir *= -1
            self._draw(_WAIT_C, int(self._pulse))
            self._lbl.config(text="Mira el punto", fg=_WAIT_C)
            self._prog.config(text=f"Punto {self._step + 1} de {n}")
            if el >= self.WAIT_SEC:
                self._skip_to_now()          # lo de la espera no cuenta
                self._xs, self._ys = [], []
                self._phase, self._phase_start = "RECORD", now
        else:
            self._draw(_REC_C, 22)
            self._drain()
            self._lbl.config(text="No muevas la mirada…", fg=_REC_C)
            if el >= self.RECORD_SEC:
                self._end_point()
                self._step += 1
                if self._step >= n:
                    self._finish()
                    return
                self._phase, self._phase_start = "WAIT", time.perf_counter()
        self._root.after(16, self._tick)

    def _end_point(self) -> None:
        tx, ty = self._target_px()
        entry: Dict = {"target": [tx, ty], "n": len(self._xs),
                       "err_px": None, "spread_px": None, "est": None}
        if len(self._xs) >= self.MIN_SAMPLES:
            ex, ey = float(np.median(self._xs)), float(np.median(self._ys))
            entry["est"] = [ex, ey]
            entry["err_px"] = math.hypot(ex - tx, ey - ty)
            entry["spread_px"] = float(math.hypot(np.std(self._xs),
                                                  np.std(self._ys)))
        self._per_point.append(entry)

    def _finish(self) -> None:
        if self._canvas:
            self._canvas.delete("dot")
        self._result = summarize(self._per_point, self.sw, self.screen_w_cm,
                                 self.distance_cm)
        if self._result and self._lbl and self._prog:
            r = self._result
            col = _REC_C if r["mean_px"] < 250 else (
                _WAIT_C if r["mean_px"] < 350 else _ERR_C)
            self._lbl.config(
                text=f"Precisión: {r['mean_px']:.0f} px "
                     f"(≈ {r['mean_deg']:.1f}°)", fg=col)
            self._prog.config(text=f"{r['n_points']} puntos válidos")
        elif self._lbl and self._prog:
            self._lbl.config(text="No hubo datos suficientes", fg=_ERR_C)
            self._prog.config(text="¿Se perdió la cara durante la prueba?")
        if self._root:
            self._root.after(1500, self._close)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._root is not None:
                self._root.destroy()
        except Exception:
            pass
        self._notify(self._result)

    def _cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._result = None
        try:
            if self._root is not None:
                self._root.destroy()
        except Exception:
            pass
        self._notify(None)

    def _notify(self, result: Optional[Dict]) -> None:
        if self._notified:
            return
        self._notified = True
        if self.on_done:
            self.on_done(result)
