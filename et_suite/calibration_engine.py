"""
et_suite/calibration_engine.py  —  Motor de Calibración Interactiva
=====================================================================
Ventana Tkinter a pantalla completa con puntos animados.

Flujo por punto:
    WAIT   (amarillo pulsante) → el usuario lleva la mirada al punto
    RECORD (verde fijo)        → se capturan muestras

Mejoras frente a la versión anterior:
  • Consume TODAS las muestras de la cola, no sólo una por tick de 60 Hz.
  • Cuenta las muestras por punto y avisa si algún punto quedó flojo
    (poca señal = calibración sesgada).
  • Indicador en vivo de "cara detectada / no detectada" — sin esto el
    usuario no sabe por qué la calibración salió mal.
  • Muestra el error RMS final para saber si la calibración sirve.
  • ESC cancela sin dejar el calibrador a medias.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from typing import Callable, Dict, List, Optional, Tuple

from .gaze_tracker import GazeTracker, GazeCalibrator, SCREEN_W, SCREEN_H

# Paleta
_BG      = "#000000"
_WAIT_C  = "#FFD700"
_REC_C   = "#00FF88"
_ERR_C   = "#FF4444"
_DIM     = "#888888"


class CalibrationEngine:
    """
    Ventana de calibración interactiva a pantalla completa.

    Uso::

        cal = GazeCalibrator(n_points=9)
        tracker = GazeTracker(calibrator=cal)
        tracker.start()

        engine = CalibrationEngine(tracker, cal, on_done=mi_callback)
        engine.run()                  # solo: bloquea hasta terminar
        engine.run(master=root)       # dentro de una app Tk: NO bloquea;
                                      # avisa con on_done al terminar

    Con ``master`` la ventana es un ``Toplevel`` del hilo principal.  Es
    obligatorio en macOS: Tk no admite crear ventanas desde otro hilo y la
    aplicación se cierra de golpe.
    """

    # ── Tiempos (segundos) ───────────────────────────────────────────────
    WAIT_SEC       = 2.0    # tiempo para llevar la mirada al punto
    RECORD_SEC     = 2.0    # tiempo de captura
    MIN_PER_POINT  = 8      # muestras mínimas aceptables por punto

    def __init__(
        self,
        tracker:    GazeTracker,
        calibrator: GazeCalibrator,
        on_done:    Optional[Callable[[bool], None]] = None,
        n_points:   int = 9,
    ) -> None:
        self.tracker    = tracker
        self.calibrator = calibrator
        self.on_done    = on_done

        self.targets: List[Tuple[float, float]] = (
            calibrator.TARGETS_9 if n_points == 9 else calibrator.TARGETS_5
        )
        self.n_pts = len(self.targets)

        self._step        = 0
        self._phase       = "WAIT"
        # Cursor de lectura del histórico del tracker (lectura no destructiva)
        self._cursor      = 0
        self._phase_start = 0.0
        self._pulse_r     = 20.0
        self._pulse_dir   = 1.0
        self._point_n     = 0
        self._cancelled   = False
        self._closed      = False
        self._face_seen   = 0
        self._face_miss   = 0

        self._root:   Optional[tk.Tk]     = None
        self._canvas: Optional[tk.Canvas] = None
        self._lbl:    Optional[tk.Label]  = None
        self._prog:   Optional[tk.Label]  = None
        self._stat:   Optional[tk.Label]  = None

    # ── Punto objetivo (px) ──────────────────────────────────────────────

    def _target_px(self) -> Tuple[int, int]:
        tx, ty = self.targets[self._step]
        return int(tx * SCREEN_W), int(ty * SCREEN_H)

    # ── Dibujo ───────────────────────────────────────────────────────────

    def _draw(self, fill: str, ring: str, radius: int) -> None:
        assert self._canvas is not None
        self._canvas.delete("dot")
        tx, ty = self._target_px()
        r = radius
        self._canvas.create_oval(tx - r - 12, ty - r - 12,
                                 tx + r + 12, ty + r + 12,
                                 outline=ring, width=3, fill="", tags="dot")
        self._canvas.create_oval(tx - r, ty - r, tx + r, ty + r,
                                 fill=fill, outline="", tags="dot")
        self._canvas.create_oval(tx - 5, ty - 5, tx + 5, ty + 5,
                                 fill="white", outline="", tags="dot")

    # ── Captura de muestras ──────────────────────────────────────────────

    def _drain_samples(self, tx: int, ty: int) -> int:
        """
        Registra TODAS las muestras nuevas del tracker desde el cursor.

        Se apoya en ``tracker.get_since()`` (lectura no destructiva) en vez
        de vaciar la cola compartida: así el grabador y la GUI no compiten
        con la calibración por las mismas muestras.

        Retorna cuántas se añadieron.
        """
        added = 0
        for s in self.tracker.get_since(self._cursor):
            self._cursor = s.frame_id
            if s.face_detected:
                self._face_seen += 1
            else:
                self._face_miss += 1
            if s.face_detected and s.raw_feat:
                self.calibrator.add_sample(s.raw_feat, tx, ty, self._step)
                added += 1
        return added

    # ── Ticker principal (~60 Hz) ────────────────────────────────────────

    def _tick(self) -> None:
        if self._closed:
            return
        assert self._root and self._lbl and self._prog and self._stat

        if self._step >= self.n_pts:
            self._finish()
            return

        now     = time.perf_counter()
        elapsed = now - self._phase_start
        tx, ty  = self._target_px()

        if self._phase == "WAIT":
            self._pulse_r += 0.5 * self._pulse_dir
            if self._pulse_r > 30 or self._pulse_r < 16:
                self._pulse_dir *= -1
            self._draw(_WAIT_C, _WAIT_C, int(self._pulse_r))

            remain = max(0.0, self.WAIT_SEC - elapsed)
            self._lbl.config(
                text=f"Mira el punto   ·   {remain:.1f}s",
                fg=_WAIT_C)
            self._prog.config(text=f"Punto {self._step + 1} de {self.n_pts}")

            if elapsed >= self.WAIT_SEC:
                # Descartar lo capturado durante la espera: el ojo iba
                # camino del punto y esas muestras no representan la
                # fijación que queremos medir.  Con el cursor basta con
                # saltar al último frame_id.
                _latest = self.tracker.get_latest()
                if _latest is not None:
                    self._cursor = _latest.frame_id
                self._phase       = "RECORD"
                self._phase_start = time.perf_counter()
                self._point_n     = 0

        elif self._phase == "RECORD":
            self._draw(_REC_C, _REC_C, 24)
            self._point_n += self._drain_samples(tx, ty)

            remain = max(0.0, self.RECORD_SEC - elapsed)
            self._lbl.config(
                text=f"Capturando…   ·   {remain:.1f}s", fg=_REC_C)
            self._prog.config(
                text=f"Punto {self._step + 1} de {self.n_pts}"
                     f"   ·   {self._point_n} muestras")

            if elapsed >= self.RECORD_SEC:
                self._step += 1
                if self._step >= self.n_pts:
                    self._finish()
                    return
                self._phase       = "WAIT"
                self._phase_start = time.perf_counter()

        # Indicador de cara en vivo (ventana de 2 s)
        total = self._face_seen + self._face_miss
        if total < 10:
            self._stat.config(text="Iniciando cámara…", fg=_DIM)
        else:
            pct = 100.0 * self._face_seen / total
            if pct >= 80:
                self._stat.config(text=f"● Cara detectada ({pct:.0f}%)", fg=_REC_C)
            elif pct >= 40:
                self._stat.config(
                    text=f"● Cara a medias ({pct:.0f}%) — acomódate frente a la cámara",
                    fg=_WAIT_C)
            else:
                self._stat.config(
                    text=f"● NO se detecta tu cara ({pct:.0f}%) — acércate / mejora la luz",
                    fg=_ERR_C)
        self._face_seen = 0
        self._face_miss = 0

        self._root.after(16, self._tick)

    # ── Finalización ─────────────────────────────────────────────────────

    def _finish(self) -> None:
        if self._closed:
            return
        assert self._canvas and self._lbl and self._prog and self._stat
        self._canvas.delete("dot")

        weak = [tid + 1 for tid, c in self.calibrator.per_point_counts.items()
                if c < self.MIN_PER_POINT]
        ok = self.calibrator.train()

        if ok:
            rms = self.calibrator.rms_error_px or 0.0
            if rms < 120:
                msg, col = f"✓ Calibración EXCELENTE  (error medio {rms:.0f} px)", _REC_C
            elif rms < 250:
                msg, col = f"✓ Calibración BUENA  (error medio {rms:.0f} px)", _REC_C
            elif rms < 450:
                msg, col = f"⚠ Calibración FLOJA  (error medio {rms:.0f} px) — puedes usarla, pero reintenta si puedes", _WAIT_C
            else:
                msg, col = f"✗ Calibración MALA  (error medio {rms:.0f} px) — repite", _ERR_C
            extra = ""
            if weak:
                extra = f"   ·   puntos con poca señal: {', '.join(map(str, weak))}"
            self._lbl.config(text=msg, fg=col)
            self._prog.config(
                text=f"{self.calibrator.n_samples} muestras útiles{extra}",
                fg=_DIM)
        else:
            self._lbl.config(
                text="✗ Pocas muestras — vuelve a intentarlo", fg=_ERR_C)
            self._prog.config(
                text="Asegúrate de que la cámara te vea bien iluminado y de frente",
                fg=_DIM)

        self._stat.config(text="")
        self._root.after(2600, self._close)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._root is not None:
                self._root.destroy()
        except Exception:
            pass
        self._notify(self.calibrator.is_calibrated and not self._cancelled)

    def _notify(self, ok: bool) -> None:
        """Garantiza que on_done se llame exactamente una vez."""
        if getattr(self, "_notified", False):
            return
        self._notified = True
        if self.on_done:
            self.on_done(ok)

    # ── Interfaz pública ─────────────────────────────────────────────────

    def run(self, master: Optional[tk.Misc] = None) -> None:
        """
        Construye la ventana fullscreen.

        Sin ``master`` crea su propio ``Tk`` y bloquea hasta terminar.  Con
        ``master`` abre un ``Toplevel`` y regresa de inmediato: el resultado
        llega por ``on_done``.
        """
        self.calibrator.reset()
        self._closed      = False
        self._step        = 0
        self._phase       = "WAIT"
        self._phase_start = time.perf_counter()
        self._cancelled   = False
        self._notified    = False

        # No arrastrar muestras anteriores al inicio de la calibración:
        # el cursor salta al último frame_id ya producido.
        _latest = self.tracker.get_latest()
        self._cursor = _latest.frame_id if _latest is not None else 0

        root = tk.Tk() if master is None else tk.Toplevel(master)
        self._root = root
        root.title("Calibración Eye Tracking")
        root.configure(bg=_BG)

        try:
            root.attributes("-fullscreen", True)
        except Exception:
            try:
                root.state("zoomed")
            except Exception:
                pass
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass

        root.bind("<Escape>", lambda _: self._cancel())
        root.bind("<space>",  lambda _: self._cancel())

        canvas = tk.Canvas(root, bg=_BG, width=SCREEN_W, height=SCREEN_H,
                           highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        self._canvas = canvas

        # Los puntos están en filas al 10 %, 50 % y 90 % de la altura: los
        # textos van en las franjas libres entre filas (30 % y 70–75 %).
        self._lbl = tk.Label(root, text="Preparando…", fg="white", bg=_BG,
                             font=("Segoe UI", 22, "bold"))
        self._lbl.place(relx=0.5, rely=0.30, anchor="center")

        self._stat = tk.Label(root, text="Iniciando cámara…", fg=_DIM, bg=_BG,
                              font=("Segoe UI", 13))
        self._stat.place(relx=0.5, rely=0.70, anchor="center")

        self._prog = tk.Label(root, text="", fg=_DIM, bg=_BG,
                              font=("Segoe UI", 15))
        self._prog.place(relx=0.5, rely=0.75, anchor="center")

        tk.Label(root, text="ESC para cancelar", fg="#444444", bg=_BG,
                 font=("Segoe UI", 10)).place(relx=0.99, rely=0.99,
                                              anchor="se")

        try:
            root.lift()
            root.focus_force()
        except Exception:
            pass
        root.after(500, self._tick)
        if master is None:
            root.mainloop()

    def _cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancelled = True
        try:
            if self._root is not None:
                self._root.destroy()
        except Exception:
            pass
        self._notify(False)
