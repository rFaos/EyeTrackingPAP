"""
calibration.py  ─  Ventana de calibración a pantalla completa (Tkinter)
==========================================================================
Flujo:
  1. Muestra un punto en la pantalla.
  2. Espera N segundos para que el usuario ajuste la mirada.
  3. Recolecta M muestras del GazeEngine.
  4. Avanza al siguiente punto.
  5. Al terminar llama a calibrator.train() y ejecuta un callback.

No bloquea el hilo del motor; todo corre en el hilo de la UI.
"""

import tkinter as tk
import time
import math
from typing import Callable, Optional

from gaze_engine import GazeEngine, GazeCalibrator, SCREEN_W, SCREEN_H


class CalibrationWindow:
    """
    Ventana negra fullscreen con animación de punto de calibración.
    
    Uso:
        cal = CalibrationWindow(engine, calibrator, on_done=callback)
        cal.run()        # bloquea hasta terminar
    """

    # Tiempos configurables (segundos)
    WAIT_SEC    = 2.0   # tiempo para enfocar antes de capturar
    RECORD_SEC  = 1.5   # tiempo de captura por punto

    def __init__(self,
                 engine: GazeEngine,
                 calibrator: GazeCalibrator,
                 on_done: Optional[Callable] = None,
                 n_points: int = 9):
        self.engine = engine
        self.calibrator = calibrator
        self.on_done = on_done

        # Seleccionar puntos
        self.targets = (calibrator.TARGETS_9 if n_points == 9
                        else calibrator.TARGETS_5)
        self.n_pts = len(self.targets)

        self._step = 0
        self._phase = "WAIT"   # WAIT | RECORD | DONE
        self._phase_start = 0.0

        # Tkinter setup
        self.root = tk.Tk()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="black")
        self.root.title("Calibración")

        self.canvas = tk.Canvas(self.root, bg="black",
                                width=SCREEN_W, height=SCREEN_H,
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # Instrucción superior
        self._lbl = tk.Label(self.root, text="",
                             fg="white", bg="black",
                             font=("Arial", 22, "bold"))
        self._lbl.place(relx=0.5, rely=0.06, anchor="center")

        # Progreso
        self._prog = tk.Label(self.root, text="",
                              fg="#888888", bg="black",
                              font=("Arial", 16))
        self._prog.place(relx=0.5, rely=0.92, anchor="center")

        # Pulse animation tag
        self._dot_id = None
        self._ring_id = None
        self._pulse_r = 20
        self._pulse_dir = 1

    # ─── Punto objetivo ──────────────────────────────────

    def _current_target_px(self):
        tx_norm, ty_norm = self.targets[self._step]
        return int(tx_norm * SCREEN_W), int(ty_norm * SCREEN_H)

    # ─── Dibujo del punto ────────────────────────────────

    def _draw_dot(self, color_dot, color_ring, radius):
        self.canvas.delete("dot")
        tx, ty = self._current_target_px()
        self.canvas.create_oval(tx - radius, ty - radius,
                                 tx + radius, ty + radius,
                                 fill=color_dot, outline="", tags="dot")
        self.canvas.create_oval(tx - radius - 8, ty - radius - 8,
                                 tx + radius + 8, ty + radius + 8,
                                 fill="", outline=color_ring, width=3, tags="dot")
        self.canvas.create_oval(tx - 5, ty - 5, tx + 5, ty + 5,
                                 fill="white", outline="", tags="dot")

    # ─── Ticker principal ─────────────────────────────────

    def _tick(self):
        now = time.perf_counter()
        elapsed = now - self._phase_start

        if self._step >= self.n_pts:
            self._finish()
            return

        tx, ty = self._current_target_px()

        if self._phase == "WAIT":
            # Animación de pulso amarillo (esperar al usuario)
            self._pulse_r += 0.4 * self._pulse_dir
            if self._pulse_r > 26 or self._pulse_r < 16:
                self._pulse_dir *= -1
            self._draw_dot("#FFD700", "#FFD700", int(self._pulse_r))
            remain = max(0, self.WAIT_SEC - elapsed)
            self._lbl.config(text=f"Mira el punto y espera… {remain:.1f}s")
            self._prog.config(text=f"Punto {self._step + 1} de {self.n_pts}")

            if elapsed >= self.WAIT_SEC:
                self._phase = "RECORD"
                self._phase_start = time.perf_counter()

        elif self._phase == "RECORD":
            # Punto verde → capturando
            self._draw_dot("#00FF88", "#00FF88", 22)
            remain = max(0, self.RECORD_SEC - elapsed)
            self._lbl.config(text=f"Capturando…  {remain:.1f}s")

            # Acumular muestras del engine
            gp = self.engine.get_latest_gaze()
            if gp and gp.face_detected:
                feat = {
                    'gx': gp.eye_dx,       # reutilizamos eye_dx como gx
                    'gy': gp.eye_dy,
                    'eye_dx': gp.eye_dx,
                    'eye_dy': gp.eye_dy,
                    'yaw': math.radians(gp.yaw_deg),
                    'pitch': math.radians(gp.pitch_deg),
                }
                self.calibrator.add_sample(feat, tx, ty)

            if elapsed >= self.RECORD_SEC:
                self._step += 1
                if self._step >= self.n_pts:
                    self._finish()
                    return
                self._phase = "WAIT"
                self._phase_start = time.perf_counter()

        # Repetir a ~60 Hz
        self.root.after(16, self._tick)

    def _finish(self):
        ok = self.calibrator.train()
        self.canvas.delete("dot")
        if ok:
            msg = "✓  ¡Calibración completada!"
            color = "#00FF88"
        else:
            msg = "⚠  Pocas muestras. Reintenta."
            color = "#FF4444"
        self._lbl.config(text=msg, fg=color)
        self._prog.config(text="Cerrando en 2 segundos…")
        self.root.after(2000, self._close)

    def _close(self):
        self.root.destroy()
        if self.on_done:
            self.on_done(self.calibrator.is_calibrated)

    # ─── Interfaz pública ────────────────────────────────

    def run(self):
        """Muestra la ventana y bloquea hasta completar."""
        self.calibrator.reset()
        self._step = 0
        self._phase = "WAIT"
        self._phase_start = time.perf_counter()
        self._lbl.config(text="Preparando…", fg="white")
        self.root.after(500, self._tick)
        self.root.mainloop()
