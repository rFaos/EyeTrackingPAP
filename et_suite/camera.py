"""
et_suite/camera.py  —  Detección y apertura robusta de cámaras
==============================================================
Problema que resuelve:
  El código anterior hacía ``cv2.VideoCapture(0)`` y nada más. Si esa cámara
  no existía, estaba ocupada, era una cámara virtual (OBS / DroidCam / Iriun)
  o el backend por defecto de OpenCV fallaba, el hilo moría en silencio y la
  app parecía "abierta pero no hace nada".

Este módulo:
  • Prueba varios backends por plataforma (DSHOW / MSMF en Windows,
    AVFoundation en macOS, V4L2 en Linux).
  • Prueba varios índices y elige el primero que entregue imagen real.
  • Descarta frames negros o planos (cámaras virtuales sin señal).
  • Verifica con un calentamiento de varios frames antes de dar por buena
    la cámara — abrir no es lo mismo que capturar.
  • Permite reabrir en caliente si la cámara se desconecta.

API principal:
    infos, caps = probe_cameras()          # qué cámaras hay
    cap, info   = open_camera(index=None)  # abrir la mejor disponible
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
#  Backends por plataforma
# ─────────────────────────────────────────────────────────────────────────────

def _backend_candidates() -> List[Tuple[str, int]]:
    """Orden de preferencia de backends de OpenCV para la plataforma actual."""
    if sys.platform == "win32":
        # DirectShow es el más compatible con webcams USB baratas y virtuales.
        return [("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF)]
    if sys.platform == "darwin":
        return [("AVFoundation", cv2.CAP_AVFOUNDATION)]
    return [("V4L2", cv2.CAP_V4L2)]


# ─────────────────────────────────────────────────────────────────────────────
#  CameraInfo
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CameraInfo:
    """Descripción de una cámara que respondió correctamente."""
    index: int
    backend_name: str
    backend: int
    width: int
    height: int
    fps: float
    brightness: float = 0.0   # media del frame de prueba (0-255)
    measured_fps: float = 0.0  # FPS medidos de verdad (no los que declara)

    @property
    def label(self) -> str:
        if self.measured_fps > 0:
            return (f"Cámara {self.index}  ·  {self.backend_name}  ·  "
                    f"{self.width}×{self.height}  ·  "
                    f"{self.measured_fps:.0f} fps")
        return (f"Cámara {self.index}  ·  {self.backend_name}  ·  "
                f"{self.width}×{self.height}")

    def __str__(self) -> str:
        return self.label


# Resoluciones que se prueban, de mayor a menor.
#
# 640×480 se usa como objetivo por defecto: es el modo que prácticamente
# cualquier webcam (integrada, USB barata, virtual) entrega a su máxima
# frecuencia.  Para eye tracking la resolución temporal importa más que la
# espacial — a 5 fps la detección de fijaciones no funciona, aunque la
# imagen sea más nítida.
RES_LADDER: Sequence[Tuple[int, int]] = [
    (1280, 720), (960, 540), (800, 600),
    (640, 480), (480, 360), (320, 240),
]

DEFAULT_TARGET: Tuple[int, int] = (640, 480)

# FPS por debajo de los cuales la clasificación I-VT de fijaciones deja de
# ser fiable.  Por debajo de ~12 Hz, dos muestras consecutivas pueden estar
# separadas más de 80 ms y una fijación corta se pierde.
MIN_USABLE_FPS = 12.0


def measure_fps(cap: cv2.VideoCapture, seconds: float = 1.0) -> float:
    """Mide los FPS reales leyendo frames durante ``seconds``."""
    n = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        ok, fr = cap.read()
        if ok and fr is not None:
            n += 1
    dt = time.perf_counter() - t0
    return n / dt if dt > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  Validación de frames
# ─────────────────────────────────────────────────────────────────────────────

def frame_is_valid(frame: Optional[np.ndarray]) -> bool:
    """
    True si el frame parece una imagen real de cámara.

    Descarta:
      • None / vacío
      • frames diminutos (drivers que devuelven 1×1 cuando fallan)
      • imágenes totalmente negras o de un solo color plano
        (típico de cámaras virtuales sin señal o lentes tapadas)
    """
    if frame is None or frame.size == 0:
        return False
    h, w = frame.shape[:2]
    if w < 32 or h < 32:
        return False
    small = frame[::8, ::8]
    if float(small.std()) < 3.0:      # plano -> sin señal
        return False
    if float(small.mean()) < 2.0:     # negro -> sin señal
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Apertura de una cámara concreta
# ─────────────────────────────────────────────────────────────────────────────

def open_camera(
    index: int = 0,
    backend: Optional[int] = None,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
    warmup_frames: int = 6,
    timeout_s: float = 3.0,
) -> Tuple[Optional[cv2.VideoCapture], Optional[CameraInfo]]:
    """
    Abre la cámara ``index`` probando backends hasta que uno entregue imagen.

    Returns:
        (cap, info) con cap listo para ``read()``, o (None, None) si no se pudo.
    """
    backends = _backend_candidates()
    if backend is not None:
        name = next((n for n, b in backends if b == backend), "custom")
        backends = [(name, backend)] + [(n, b) for n, b in backends if b != backend]

    last_err = ""
    for bname, bcode in backends:
        cap = None
        try:
            cap = cv2.VideoCapture(index, bcode)
            if not cap.isOpened():
                cap.release()
                last_err = f"{bname}: no se pudo abrir"
                continue

            # MJPG primero: muchas webcams USB2 sólo dan >15 fps en MJPG.
            try:
                cap.set(cv2.CAP_PROP_FOURCC,
                        cv2.VideoWriter_fourcc(*"MJPG"))
            except Exception:
                pass
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            cap.set(cv2.CAP_PROP_FPS, fps)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

            # Calentamiento: abrir != capturar. Algunas cámaras tardan
            # 1-2 s en entregar el primer frame válido.
            good = None
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < timeout_s:
                ok, fr = cap.read()
                if ok and frame_is_valid(fr):
                    good = fr
                    warmup_frames -= 1
                    if warmup_frames <= 0:
                        break
                else:
                    time.sleep(0.03)

            if good is None:
                cap.release()
                last_err = f"{bname}: abrió pero no entrega imagen"
                continue

            rw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or good.shape[1]
            rh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or good.shape[0]
            rfps = float(cap.get(cv2.CAP_PROP_FPS)) or 0.0
            if rfps <= 1 or rfps > 240:
                rfps = 30.0

            info = CameraInfo(index=index, backend_name=bname, backend=bcode,
                              width=rw, height=rh, fps=rfps,
                              brightness=float(good.mean()))
            return cap, info

        except Exception as e:                       # driver roto
            last_err = f"{bname}: {e}"
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

    if last_err:
        print(f"[camera] índice {index} descartado ({last_err})")
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
#  Exploración de todas las cámaras disponibles
# ─────────────────────────────────────────────────────────────────────────────

def probe_cameras(
    max_index: int = 6,
    width: int = 1280,
    height: int = 720,
) -> List[CameraInfo]:
    """
    Prueba los índices 0..max_index-1 y devuelve las cámaras que funcionan,
    con los FPS medidos de verdad.

    Es lento a propósito (cada cámara se abre, se calienta y se mide), así
    que se llama cuando el usuario pulsa "Detectar cámaras", no en bucle.
    """
    found: List[CameraInfo] = []
    seen: set = set()
    for idx in range(max_index):
        cap, info = open_camera(idx, width=width, height=height,
                                warmup_frames=3, timeout_s=1.5)
        if cap is None or info is None:
            continue
        try:
            info.measured_fps = measure_fps(cap, 0.7)
            # Evitar listar dos veces la misma cámara física (algunos
            # drivers exponen el mismo dispositivo en dos índices).
            key = (info.width, info.height, round(info.brightness, 1))
            if key not in seen:
                seen.add(key)
                found.append(info)
        finally:
            cap.release()
    return found


def auto_open(
    preferred_index: Optional[int] = None,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
) -> Tuple[Optional[cv2.VideoCapture], Optional[CameraInfo]]:
    """
    Abre la mejor cámara disponible.

    1. Si ``preferred_index`` funciona, se usa.
    2. Si no, se prueban los índices en orden hasta encontrar una que dé imagen.

    Devuelve (None, None) sólo si no hay ninguna cámara utilizable.
    """
    order: Sequence[Optional[int]] = (
        [preferred_index] if preferred_index is not None else []
    )
    order = list(order) + [i for i in range(6) if i != preferred_index]

    for idx in order:
        if idx is None:
            continue
        cap, info = open_camera(int(idx), width=width, height=height, fps=fps)
        if cap is not None and info is not None:
            return cap, info
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
#  Negociación automática de resolución
# ─────────────────────────────────────────────────────────────────────────────

def open_camera_best(
    index: int,
    target: Tuple[int, int] = DEFAULT_TARGET,
    min_fps: float = MIN_USABLE_FPS,
    measure_seconds: float = 0.8,
    cache_path: Optional[str] = None,
) -> Tuple[Optional[cv2.VideoCapture], Optional[CameraInfo]]:
    """
    Abre la cámara en la resolución más alta que alcance ``min_fps``.

    Por qué existe esto
    -------------------
    Muchas webcams baratas dan 15-30 fps a 640×480 pero caen a 5 fps a
    1280×720 (el bus USB no da para más).  A 5 fps la detección de fijaciones
    por umbral de velocidad deja de funcionar: dos muestras consecutivas
    quedan separadas más de 200 ms y una fijación corta se pierde.  En vez de
    asumir una resolución, se mide y se elige la mejor que realmente funcione.

    Implementación
    --------------
    La cámara se abre **una sola vez** y se cambia de resolución en caliente
    con ``CAP_PROP_FRAME_WIDTH/HEIGHT``.  Abrir y cerrar el dispositivo
    repetidamente (que fue el primer intento) deja a los drivers DirectShow
    en un estado inconsistente y la cámara deja de entregar frames; por eso
    no se reabre nunca dentro de esta función.
    """
    cap, info = open_camera(index, width=target[0], height=target[1],
                            warmup_frames=4, timeout_s=3.0)
    if cap is None or info is None:
        return None, None

    def apply_and_measure(w: int, h: int) -> Tuple[int, int, float]:
        """Cambia la resolución en caliente y mide los FPS reales."""
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        except Exception:
            pass
        time.sleep(0.2)
        for _ in range(6):          # descartar frames viejos del buffer
            cap.read()
        rw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or w
        rh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or h
        return rw, rh, measure_fps(cap, measure_seconds)

    def current_size() -> Tuple[int, int]:
        return (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))

    # Primero la resolución que funcionó la última vez, si la conocemos y
    # sabemos que alcanzó los FPS mínimos.
    candidates: List[Tuple[int, int]] = []
    cached = _read_cache(cache_path)
    if (cached and cached.get("index") == index and cached.get("width")
            and float(cached.get("measured_fps", 0)) >= min_fps):
        candidates.append((int(cached["width"]), int(cached["height"])))
    for wh in RES_LADDER:
        if wh[0] <= target[0] and wh not in candidates:
            candidates.append(wh)
    if target not in candidates:
        candidates.insert(0, target)

    best: Optional[Tuple[float, int, int]] = None
    for (w, h) in candidates:
        rw, rh, f = apply_and_measure(w, h)
        if best is None or f > best[0]:
            best = (f, rw, rh)
        if f >= min_fps:
            break

    assert best is not None
    f, rw, rh = best

    # Dejar la cámara en la mejor resolución encontrada (el bucle pudo
    # haber terminado en una peor).
    if current_size() != (rw, rh):
        apply_and_measure(rw, rh)
        f = measure_fps(cap, measure_seconds)

    info.width, info.height = rw, rh
    info.measured_fps = f

    if f < min_fps:
        print(f"[camera] AVISO: ninguna resolución alcanza {min_fps:.0f} fps. "
              f"Se usa {info.label}")
    else:
        print(f"[camera] {info.label}")

    _write_cache(cache_path, index, info)
    return cap, info


def _read_cache(path: Optional[str]) -> Optional[dict]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(path: Optional[str], index: int, info: CameraInfo) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"index": index, "width": info.width,
                       "height": info.height,
                       "measured_fps": info.measured_fps}, f)
    except Exception:
        pass
