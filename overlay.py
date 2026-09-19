"""
overlay.py  ─  Punto de gaze semi-transparente always-on-top (Windows)
==========================================================================
Usa Tkinter con:
  • wm_attributes("-alpha") para transparencia de la ventana
  • "-topmost" para estar siempre encima
  • overrideredirect para quitar la barra de título
  • Un canvas con un círculo de contorno (no relleno sólido) para no obstruir la vista

Actualiza su posición cada vez que GazeEngine emite un nuevo GazePoint.
"""

import tkinter as tk
import threading
import time
import math
from typing import Optional

from gaze_engine import GazeEngine, GazePoint

# Radio y estilo del puntero
DOT_RADIUS   = 22      # radio del círculo exterior en píxeles
DOT_INNER    = 5       # radio del punto central sólido
DOT_COLOR    = "#00FFAA"   # color del contorno
DOT_ALPHA    = 0.75    # transparencia de la ventana (0=invisible, 1=opaco)
UPDATE_HZ    = 60      # frecuencia de actualización del overlay


class GazeOverlay:
    """
    Ventana transparente always-on-top que muestra un punto de contorno
    en las coordenadas de pantalla del punto de mirada estimado.

    Uso:
        overlay = GazeOverlay(engine)
        overlay.start()           # inicia el hilo de actualización
        ...
        overlay.stop()            # detiene
    """

    def __init__(self, engine: GazeEngine):
        self.engine = engine
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._root: Optional[tk.Tk] = None
        self._canvas: Optional[tk.Canvas] = None
        self._ready = threading.Event()

    # ─── Hilo TK ─────────────────────────────────────────

    def _build_window(self):
        self._root = tk.Tk()
        r = self._root

        # Pantalla completa sin decoraciones, completamente transparente
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-transparentcolor", "black")   # negro = invisible
        r.attributes("-alpha", DOT_ALPHA)
        r.configure(bg="black")

        import ctypes
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)

        r.geometry(f"{sw}x{sh}+0+0")

        self._canvas = tk.Canvas(r, width=sw, height=sh,
                                 bg="black", highlightthickness=0)
        self._canvas.pack()

        self._ready.set()
        self._tick()
        r.mainloop()

    def _tick(self):
        if not self._running:
            if self._root:
                self._root.quit()
            return

        gp = self.engine.get_latest_gaze()
        if gp:
            self._draw(int(gp.x), int(gp.y), gp.face_detected)

        interval_ms = int(1000 / UPDATE_HZ)
        self._root.after(interval_ms, self._tick)

    def _draw(self, x: int, y: int, detected: bool):
        c = self._canvas
        c.delete("gaze")

        if not detected:
            # Cruz pequeña si no detecta cara
            c.create_line(x-12, y, x+12, y, fill="#FF4444", width=2, tags="gaze")
            c.create_line(x, y-12, x, y+12, fill="#FF4444", width=2, tags="gaze")
            return

        # Círculo exterior de contorno (hueco = no obstruye)
        c.create_oval(x - DOT_RADIUS, y - DOT_RADIUS,
                      x + DOT_RADIUS, y + DOT_RADIUS,
                      outline=DOT_COLOR, width=3, fill="", tags="gaze")

        # Líneas cruzadas internas (tipo crosshair)
        half = DOT_RADIUS // 2
        c.create_line(x - half, y, x + half, y,
                      fill=DOT_COLOR, width=1, tags="gaze")
        c.create_line(x, y - half, x, y + half,
                      fill=DOT_COLOR, width=1, tags="gaze")

        # Punto central sólido pequeño
        c.create_oval(x - DOT_INNER, y - DOT_INNER,
                      x + DOT_INNER, y + DOT_INNER,
                      fill=DOT_COLOR, outline="", tags="gaze")

    # ─── Interfaz pública ────────────────────────────────

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._build_window,
                                        daemon=True, name="GazeOverlay")
        self._thread.start()
        self._ready.wait(timeout=5.0)   # espera a que la ventana esté lista

    def stop(self):
        self._running = False
        if self._root:
            try:
                self._root.after(0, self._root.quit)
            except Exception:
                pass
