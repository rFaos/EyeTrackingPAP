"""
et_suite/display.py  —  Geometría de pantalla y de página web
===============================================================
Funciones puras (sin hardware) para:

  • Convertir píxeles de pantalla en grados de ángulo visual, que es la
    unidad estándar en eye tracking.  Necesita el ancho físico de la
    pantalla y la distancia ojo-pantalla.
  • Convertir una coordenada de pantalla (donde mira el participante) en una
    coordenada de PÁGINA WEB, usando la posición de scroll y de la ventana
    del navegador que reporta el puente web (``web_bridge``).

Todo lo que viene del navegador está en píxeles CSS.  La pantalla del
tracker (``SCREEN_W``/``SCREEN_H``) está en las unidades de ``screeninfo``.
La razón entre ambas (``k``) se calcula con ``screen.width`` del navegador y
absorbe a la vez el escalado del sistema operativo y el zoom del navegador.
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

# Ancho físico de una pantalla de portátil de 15.6" (16:9).  Sólo se usa si
# el sistema no informa del tamaño real ni el estudio lo define.
FALLBACK_SCREEN_WIDTH_CM = 34.5
DEFAULT_DISTANCE_CM = 60.0


# ─────────────────────────────────────────────────────────────────────────────
#  Ángulo visual
# ─────────────────────────────────────────────────────────────────────────────

def detect_screen_width_cm() -> Tuple[float, str]:
    """
    Ancho físico del monitor principal en cm y de dónde salió el dato.

    Returns:
        (ancho_cm, fuente) — ``fuente`` es "sistema" o "estimado".
    """
    try:
        import screeninfo
        mons = screeninfo.get_monitors()
        if mons:
            mm = getattr(mons[0], "width_mm", None)
            # Algunos sistemas devuelven 0 o valores absurdos
            if mm and 150 <= float(mm) <= 1500:
                return float(mm) / 10.0, "sistema"
    except Exception:
        pass
    return FALLBACK_SCREEN_WIDTH_CM, "estimado"


def px_per_degree(screen_w_px: float, screen_w_cm: float,
                  distance_cm: float = DEFAULT_DISTANCE_CM) -> float:
    """Cuántos píxeles de pantalla abarca 1° de ángulo visual."""
    if screen_w_px <= 0 or screen_w_cm <= 0 or distance_cm <= 0:
        return float("nan")
    cm_per_px = screen_w_cm / float(screen_w_px)
    return distance_cm * math.tan(math.radians(1.0)) / cm_per_px


def px_to_deg(px: float, screen_w_px: float, screen_w_cm: float,
              distance_cm: float = DEFAULT_DISTANCE_CM) -> float:
    """Convierte una distancia en píxeles de pantalla a grados visuales."""
    ppd = px_per_degree(screen_w_px, screen_w_cm, distance_cm)
    return float(px) / ppd if ppd and ppd == ppd else float("nan")


# ─────────────────────────────────────────────────────────────────────────────
#  Pantalla → página web
# ─────────────────────────────────────────────────────────────────────────────

def content_origin(vp: Dict) -> Tuple[float, float]:
    """
    Esquina superior izquierda del ÁREA DE CONTENIDO del navegador, en px CSS
    de pantalla.

    Firefox la da exacta (``mozInnerScreenX/Y``).  En los demás se estima:
    la barra de pestañas/direcciones es la diferencia entre el alto exterior
    e interior de la ventana, menos el borde inferior (que en Windows es
    igual a un borde lateral y en macOS es cero).
    """
    if vp.get("mx") is not None and vp.get("my") is not None:
        return float(vp["mx"]), float(vp["my"])
    ow, oh = float(vp.get("ow", 0)), float(vp.get("oh", 0))
    iw, ih = float(vp.get("iw", 0)), float(vp.get("ih", 0))
    border = max(0.0, (ow - iw) / 2.0)
    left = float(vp.get("wx", 0)) + border
    top = float(vp.get("wy", 0)) + max(0.0, (oh - ih) - border)
    return left, top


def screen_to_page(gx: float, gy: float, vp: Optional[Dict],
                   screen_w: float) -> Optional[Dict[str, float]]:
    """
    Convierte la mirada (pantalla) en coordenadas de la página.

    Args:
        gx, gy:   mirada en las unidades de pantalla del tracker.
        vp:       último estado del navegador (ver ``web_bridge``).
        screen_w: ancho de pantalla del tracker en esas mismas unidades.

    Returns:
        dict con ``px, py`` (página, px CSS), ``vx, vy`` (dentro del área
        visible), ``sx, sy`` (scroll); ``None`` si no hay datos del navegador.
    """
    if not vp:
        return None
    try:
        css_screen_w = float(vp.get("sw") or 0)
        k = (screen_w / css_screen_w) if css_screen_w > 0 else 1.0
        if k <= 0:
            k = 1.0
        cx, cy = gx / k, gy / k                       # px CSS de pantalla
        ox, oy = content_origin(vp)
        vx, vy = cx - ox, cy - oy
        sx, sy = float(vp.get("sx", 0.0)), float(vp.get("sy", 0.0))
        return {"px": vx + sx, "py": vy + sy, "vx": vx, "vy": vy,
                "sx": sx, "sy": sy}
    except (TypeError, ValueError):
        return None
