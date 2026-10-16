"""
et_suite/screenshots.py  —  Captura de pantalla para los fondos de heatmap
============================================================================
Toma una captura de la pantalla completa para usarla como fondo de los
heatmaps, de modo que no haya que hacerlo a mano.

  • macOS:   comando ``screencapture`` (viene con el sistema).
  • Windows: ``PIL.ImageGrab`` (Pillow).
  • Linux:   ``PIL.ImageGrab`` (necesita un servidor X).

macOS exige dar permiso de *Grabación de pantalla* a la aplicación desde la
que se lanza el prototipo (Terminal, Cursor, VS Code…); sin él la captura
sale sin el contenido de las demás ventanas.  Si algo falla, la función
devuelve False y el análisis simplemente usa fondo blanco.
"""

from __future__ import annotations

import os
import subprocess
import sys


def capture_screen(path: str) -> bool:
    """Guarda una captura de la pantalla principal en ``path`` (PNG)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["screencapture", "-x", "-t", "png", path],
                               capture_output=True, timeout=10)
            return r.returncode == 0 and os.path.exists(path) \
                and os.path.getsize(path) > 0
        from PIL import ImageGrab
        img = ImageGrab.grab()
        img.save(path, "PNG")
        return os.path.exists(path)
    except Exception as e:
        print(f"[screenshots] No se pudo capturar la pantalla: {e}")
        return False
