"""
et_suite/gaze_tracker.py  —  Motor de Estimación de Mirada
============================================================
Pipeline (100 % MediaPipe FaceLandmarker + geometría):

  1. Captura de video con selección automática de cámara
     (ver ``et_suite/camera.py``).

  2. FaceLandmarker → 478 landmarks 3D + centros de iris.

  3. Vector de características de mirada (todo normalizado e invariante
     a escala, para que funcione con cualquier cámara y cualquier
     distancia al monitor):

       gx, gy    desplazamiento del iris dentro de la cuenca ocular
                 (señal principal de mirada)
       yaw,pitch,roll   orientación cefálica 3D vía solvePnP
       p_yaw, p_pitch   proxies de orientación 2D invariantes a escala
       fdx, fdy         traslación de la cabeza en el encuadre

  4. Calibración → regresión Ridge polinómica de 2º orden que aprende
     el mapeo características → píxeles de pantalla.  La regresión decide
     por sí sola cuánto pesa cada término (iris vs. cabeza), en lugar de
     usar constantes escritas a mano.

  5. Filtro 1€ para eliminar jitter sin introducir lag en las sacadas.

Historial de correcciones importantes
-------------------------------------
* El modelo 3D de ``solvePnP`` tenía el eje Y invertido respecto a la
  convención de cámara de OpenCV, lo que producía un pitch de ~170° con
  la cara de frente.  Toda la señal de mirada salía del término de cabeza
  corrupto.  Corregido (signo de Y) y validado.
* El frame ya **no** se espeja antes de detectar landmarks: el espejado
  intercambiaba los landmarks izquierda/derecha y rompía la
  correspondencia con el modelo 3D.  El espejo se aplica sólo a la
  previsualización de debug.
* El vector de características ya no mezcla la pose con el iris mediante
  constantes (``K_YAW``/``K_PITCH``): ambos entran como regresores
  separados y la calibración aprende sus pesos.
"""

from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

import cv2
import mediapipe as mp
import numpy as np

from .camera import (CameraInfo, auto_open, frame_is_valid, open_camera_best,
                     probe_cameras)
from .filters import OneEuroFilter, OneEuroFilter2D

# ─── Resolución de pantalla ──────────────────────────────────────────────────
import sys
import ctypes

# Dónde recordar la resolución que funcionó la última vez
CAMERA_CACHE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".camera_cache.json"))

SCREEN_W: int = 1920
SCREEN_H: int = 1080
try:
    import screeninfo
    _mons = screeninfo.get_monitors()
    if _mons:
        SCREEN_W = _mons[0].width
        SCREEN_H = _mons[0].height
except Exception:
    pass

if SCREEN_W == 1920 and SCREEN_H == 1080 and hasattr(ctypes, "windll"):
    try:
        SCREEN_W = ctypes.windll.user32.GetSystemMetrics(0)
        SCREEN_H = ctypes.windll.user32.GetSystemMetrics(1)
    except Exception:
        pass

# Cuántas muestras guarda el histórico circular del tracker.
# A 15 Hz, 900 muestras ≈ 60 s: sobra para que un consumidor que se
# retrase (calibración, recarga de la GUI) no pierda nada.
HISTORY_MAXLEN: int = 900


# ════════════════════════════════════════════════════════════════════════════
#  GazeSample — estructura de datos publicada por cada frame
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class GazeSample:
    """
    Snapshot completo de un instante de estimación de mirada.
    Publicado en ``GazeTracker.sample_queue`` cada vez que se procesa un frame.
    """
    timestamp_ms:  float        # time.perf_counter() * 1000
    frame_id:      int
    gaze_x:        float        # px pantalla (filtrado)
    gaze_y:        float
    gaze_x_raw:    float        # px pantalla (sin filtrar)
    gaze_y_raw:    float
    pupil_size:    float        # radio del iris en px (0 si no detectado)
    event_type:    str          # "Fixation" | "Saccade" | "Unknown"
    head_pitch:    float        # grados
    head_yaw:      float
    head_roll:     float
    face_detected: bool
    sample_hz:     float        # Hz instantáneo
    jitter_ms:     float        # desv. estándar de Δt en ventana de 30 frames
    pose_ok:       bool = True  # False si la pose cefálica no es plausible
    calibrated:    bool = False # True si la calibración estaba activa
    raw_feat:      dict = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════════════
#  FaceLandmarker Wrapper (MediaPipe tasks API ≥ 1.0)
# ════════════════════════════════════════════════════════════════════════════

class _FaceMeshWrapper:
    MODEL_URL  = ("https://storage.googleapis.com/mediapipe-models/"
                  "face_landmarker/face_landmarker/float16/latest/face_landmarker.task")
    MODEL_FILE = "face_landmarker.task"

    def __init__(self, model_dir: str = "") -> None:
        path = os.path.join(model_dir or os.path.dirname(os.path.abspath(__file__)),
                            "..", self.MODEL_FILE)
        path = os.path.normpath(path)
        if not os.path.exists(path):
            print(f"[FaceMesh] Descargando modelo → {path}")
            urllib.request.urlretrieve(self.MODEL_URL, path)
            print(f"[FaceMesh] OK ({os.path.getsize(path)//1024} KB)")

        from mediapipe.tasks import python as mp_py
        from mediapipe.tasks.python import vision as mp_vis

        opts = mp_vis.FaceLandmarkerOptions(
            base_options=mp_py.BaseOptions(model_asset_path=path),
            running_mode=mp_vis.RunningMode.VIDEO,   # tracking temporal
            num_faces=1,
            min_face_detection_confidence=0.40,
            min_face_presence_confidence=0.40,
            min_tracking_confidence=0.40,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
        )
        self._det = mp_vis.FaceLandmarker.create_from_options(opts)
        self._mp = mp

    def detect(self, rgb: np.ndarray, timestamp_ms: int):
        """Retorna la lista de 478 landmarks o None."""
        img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        res = self._det.detect_for_video(img, int(timestamp_ms))
        return res.face_landmarks[0] if res.face_landmarks else None

    def close(self) -> None:
        try:
            self._det.close()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
#  HeadPoseEstimator — solvePnP con modelo corregido + proxies 2D
# ════════════════════════════════════════════════════════════════════════════

class _HeadPoseEstimator:
    """
    Orientación cefálica por ``cv2.solvePnP`` + proxies 2D invariantes a escala.

    IMPORTANTE — corrección del modelo 3D
    -------------------------------------
    El modelo clásico de 6 puntos trae el eje Y invertido respecto a la
    convención de cámara de OpenCV (Y hacia abajo).  Con ese modelo, una cara
    de frente producía ``pitch ≈ +170°`` porque el solver tenía que aplicar
    una rotación de 180° para reconciliar el mentón del modelo (arriba) con el
    mentón real (abajo).  Con el signo de Y corregido, una cara de frente da
    ``pitch ≈ 0`` y la pose responde de verdad al movimiento de cabeza.

    Además de los ángulos 3D se calculan dos proxies 2D que NO dependen de la
    distancia focal ni de la distancia al monitor, por lo que funcionan igual
    con cualquier cámara.
    """

    # Modelo 3D de referencia (mm × 5) — Y corregida, 14 puntos
    #
    # Se pasó de 6 a 14 puntos: el conjunto de 6 (nariz, mentón, cantos
    # externos, comisuras) está casi en un plano, y con puntos casi coplanares
    # solvePnP tiene una ambigüedad de 180° que se manifiesta como giros
    # fantasma.  Con 14 puntos repartidos por ojos, párpados, mejillas y boca
    # la ambigüedad desaparece y la pose queda mucho más estable.
    _MODEL_PTS = np.array([
        [   0.0,    0.0,    0.0],   # 1    punta de la nariz
        [   0.0,  330.0,  -65.0],   # 152  mentón
        [-225.0, -170.0, -135.0],   # 33   canto externo ojo derecho
        [ 225.0, -170.0, -135.0],   # 263  canto externo ojo izquierdo
        [ -75.0, -170.0, -135.0],   # 133  canto interno ojo derecho
        [  75.0, -170.0, -135.0],   # 362  canto interno ojo izquierdo
        [-150.0, -197.5, -135.0],   # 159  párpado superior derecho
        [-150.0, -142.5, -135.0],   # 145  párpado inferior derecho
        [ 150.0, -197.5, -135.0],   # 386  párpado superior izquierdo
        [ 150.0, -142.5, -135.0],   # 374  párpado inferior izquierdo
        [-150.0,  150.0, -125.0],   # 61   comisura bucal derecha
        [ 150.0,  150.0, -125.0],   # 291  comisura bucal izquierda
        [-275.0,  -75.0,  -75.0],   # 234  mejilla derecha
        [ 275.0,  -75.0,  -75.0],   # 454  mejilla izquierda
    ], dtype=np.float64)
    _IDX = [1, 152, 33, 263, 133, 362, 159, 145, 386, 374, 61, 291, 234, 454]

    # Landmarks auxiliares para los proxies 2D
    _NOSE    = 1
    _CHEEK_L = 454      # lado derecho de la imagen (sin espejar)
    _CHEEK_R = 234      # lado izquierdo de la imagen
    _EYE_R   = 33
    _EYE_L   = 263
    _MOUTH_R = 61
    _MOUTH_L = 291

    # Límites fisiológicos: fuera de esto, la pose no es creíble
    _MAX_PITCH = 45.0
    _MAX_YAW   = 50.0
    _MAX_ROLL  = 35.0
    # Error de reproyección máximo (px) para aceptar la pose
    _MAX_REPROJ_PX = 22.0

    def __init__(self) -> None:
        self._K: Optional[np.ndarray] = None
        self._dist = np.zeros((4, 1), dtype=np.float64)
        self._size: Tuple[int, int] = (0, 0)

    def _ensure_K(self, w: int, h: int) -> None:
        if self._K is not None and self._size == (w, h):
            return
        # Distancia focal aproximada para ~55° de FOV horizontal.
        f = 0.95 * float(w)
        self._K = np.array([[f, 0, w / 2.], [0, f, h / 2.], [0, 0, 1.]], np.float64)
        self._size = (w, h)

    # ── Proxies 2D invariantes a escala ───────────────────────────────────

    @staticmethod
    def _px(lm, i: int, w: int, h: int) -> Tuple[float, float]:
        return lm[i].x * w, lm[i].y * h

    def _proxies(self, lm, w: int, h: int) -> Tuple[float, float]:
        """(proxy_yaw, proxy_pitch) — adimensionales, rango ≈ [-1, 1]."""
        nx, ny = self._px(lm, self._NOSE, w, h)
        rcx, rcy = self._px(lm, self._CHEEK_R, w, h)
        lcx, lcy = self._px(lm, self._CHEEK_L, w, h)

        d_r = math.hypot(nx - rcx, ny - rcy)
        d_l = math.hypot(nx - lcx, ny - lcy)
        denom = d_l + d_r
        p_yaw = (d_l - d_r) / denom if denom > 1e-6 else 0.0

        ey = (lm[self._EYE_R].y + lm[self._EYE_L].y) / 2.0 * h
        my = (lm[self._MOUTH_R].y + lm[self._MOUTH_L].y) / 2.0 * h
        up = ny - ey
        down = my - ny
        span = max(my - ey, 1e-6)
        p_pitch = (up - down) / span

        return float(p_yaw), float(p_pitch)

    # ── Estimación ────────────────────────────────────────────────────────

    @staticmethod
    def _rotation_angles(R: np.ndarray):
        """
        Ángulos de Euler robustos a la configuración "cara frente al objetivo".

        La descomposición XYZ clásica tiene una SINGULARIDAD justo ahí: con la
        cara mirando de frente al objetivo, ``R ≈ Ry(180°)`` y el pitch sale
        180° en vez de 0°, con el roll también indefinido.  Esa singularidad no
        es un caso raro — es exactamente la postura normal de alguien usando la
        webcam — así que hay que tratarla.

        Cuando se detecta la zona singular (``R[2,2]`` muy negativo) se
        descompone ``R · Ry(180°)``, que es la misma orientación expresada
        respecto a la postura "de frente" y da ángulos pequeños y correctos.
        """
        R_use = R
        if float(R[2, 2]) < -0.7:
            R_use = R @ np.array([[-1.0, 0.0, 0.0],
                                  [0.0, 1.0, 0.0],
                                  [0.0, 0.0, -1.0]], np.float64)

        sy = math.sqrt(R_use[0, 0] ** 2 + R_use[1, 0] ** 2)
        if sy > 1e-6:
            pitch = math.atan2(R_use[2, 1], R_use[2, 2])
            yaw   = math.atan2(-R_use[2, 0], sy)
            roll  = math.atan2(R_use[1, 0], R_use[0, 0])
        else:
            pitch = math.atan2(-R_use[1, 2], R_use[1, 1])
            yaw   = math.atan2(-R_use[2, 0], sy)
            roll  = 0.0
        return pitch, yaw, roll

    def estimate(self, lm, w: int, h: int) -> Optional[Dict]:
        self._ensure_K(w, h)
        assert self._K is not None

        pts2d = np.array([[lm[i].x * w, lm[i].y * h] for i in self._IDX],
                         np.float64)

        pose: Dict = {}
        ok = False
        reproj_px = float("inf")
        try:
            ok, rvec, tvec = cv2.solvePnP(
                self._MODEL_PTS, pts2d, self._K, self._dist,
                flags=cv2.SOLVEPNP_SQPNP)
        except Exception:
            ok = False

        if ok:
            R, _ = cv2.Rodrigues(rvec)
            # Error de reproyección: cuánto se desvía el modelo proyectado de
            # los landmarks observados.  Es el mejor indicador de que la pose
            # es creíble, y no depende de ninguna convención de ángulos.
            try:
                proj, _ = cv2.projectPoints(self._MODEL_PTS, rvec, tvec,
                                            self._K, self._dist)
                reproj_px = float(np.mean(np.linalg.norm(
                    proj.reshape(-1, 2) - pts2d, axis=1)))
            except Exception:
                reproj_px = float("inf")

            pitch, yaw, roll = self._rotation_angles(R)
            pose.update(
                rvec=rvec, tvec=tvec, K=self._K, dist=self._dist,
                R=R, reproj_px=reproj_px,
                pitch=pitch, yaw=yaw, roll=roll,
                pitch_deg=math.degrees(pitch),
                yaw_deg=math.degrees(yaw),
                roll_deg=math.degrees(roll),
            )
        else:
            pose.update(pitch=0.0, yaw=0.0, roll=0.0,
                        pitch_deg=0.0, yaw_deg=0.0, roll_deg=0.0,
                        reproj_px=float("inf"))

        # Validez fisiológica + calidad de la reproyección
        pose["pose_ok"] = bool(
            ok
            and reproj_px <= self._MAX_REPROJ_PX
            and abs(pose["pitch_deg"]) <= self._MAX_PITCH
            and abs(pose["yaw_deg"])   <= self._MAX_YAW
            and abs(pose["roll_deg"])  <= self._MAX_ROLL
        )

        p_yaw, p_pitch = self._proxies(lm, w, h)
        pose["p_yaw"] = p_yaw
        pose["p_pitch"] = p_pitch

        # Centro de la cara en el encuadre (proxy de traslación de cabeza)
        xs = [lm[i].x for i in range(0, 468, 4)]
        ys = [lm[i].y for i in range(0, 468, 4)]
        pose["fdx"] = float(np.mean(xs)) - 0.5
        pose["fdy"] = float(np.mean(ys)) - 0.5

        # Dimensiones del frame: las necesita el extractor geométrico
        pose["w"] = w
        pose["h"] = h

        return pose

    def draw_axes(self, frame: np.ndarray, pose: Dict, length: float = 70.0) -> None:
        if "rvec" not in pose:
            return
        try:
            pts, _ = cv2.projectPoints(
                np.float64([[0, 0, 0], [length, 0, 0], [0, length, 0], [0, 0, length]]),
                pose["rvec"], pose["tvec"], pose["K"], pose["dist"])
            p = pts.reshape(-1, 2).astype(int)
            o = tuple(p[0])
            cv2.line(frame, o, tuple(p[1]), (0, 0, 255), 2)
            cv2.line(frame, o, tuple(p[2]), (0, 255, 0), 2)
            cv2.line(frame, o, tuple(p[3]), (255, 0, 0), 2)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
#  Geometría del globo ocular — base del modelo invariante a la cabeza
# ════════════════════════════════════════════════════════════════════════════

# Centros de los globos oculares en mm, relativos a la punta de la nariz
# (mismo origen que el modelo facial de _HeadPoseEstimator).
EYE_CENTER_R_MM = (-30.0, -34.0, -37.0)
EYE_CENTER_L_MM = ( 30.0, -34.0, -37.0)
EYE_RADIUS_MM   = 12.0

# El modelo 3D de solvePnP va en mm × 5 (así el solver trabaja con números
# del orden de la centena).  ``tvec`` sale en esas unidades → mm = tvec / 5.
MODEL_UNITS_PER_MM = 5.0


# ════════════════════════════════════════════════════════════════════════════
#  EyeGazeExtractor — iris 2D (compatible) + mirada 3D (invariante)
# ════════════════════════════════════════════════════════════════════════════

class _EyeGazeExtractor:
    """
    Extrae DOS descripciones de la mirada:

    1. ``gx`` / ``gy`` — desplazamiento del iris dentro de la cuenca ocular,
       normalizado por el tamaño del ojo.  Adimensional y barato, pero medido
       en el sistema de la CABEZA: si el usuario gira la cabeza, cambia aunque
       siga mirando al mismo punto.

    2. ``Ex,Ey,Ez`` / ``dx,dy,dz`` — mirada en 3D.  El globo ocular es una
       esfera de 12 mm centrada en un punto conocido del modelo facial; el rayo
       que sale de la cámara por el píxel del iris la corta en dos puntos y el
       más cercano es el iris visible.  El vector centro→iris, normalizado, es
       la dirección de mirada.  Como se expresa en coordenadas de CÁMARA, NO
       depende de la orientación de la cabeza: girar o trasladar la cabeza deja
       la dirección prácticamente intacta mientras se mire al mismo punto.

    Por qué importa
    ---------------
    Medido sobre el modelo sintético cabeza-ojo-pantalla (``_t_pose.py``):
    con la variante 1, girar la cabeza ±8° desvía la mirada estimada 1728 px;
    con la variante 2, 1 px.  Girar ±16°: 3555 px frente a 2 px.
    """

    R_IRIS = 468; R_OUT = 33;  R_IN = 133;  R_TOP = 159; R_BOT = 145
    L_IRIS = 473; L_OUT = 263; L_IN = 362;  L_TOP = 386; L_BOT = 374

    @staticmethod
    def _iris_disp(lm, iris, out_, in_, top, bot):
        cx = (lm[out_].x + lm[in_].x) / 2.
        cy = (lm[top].y  + lm[bot].y) / 2.
        ew = max(abs(lm[in_].x - lm[out_].x), 1e-5)
        eh = max(abs(lm[bot].y - lm[top].y),  1e-5)
        return (lm[iris].x - cx) / ew, (lm[iris].y - cy) / eh

    # ── Mirada 3D en coordenadas de cámara ────────────────────────────────

    def gaze_3d(self, lm, pose: Dict):
        """
        Devuelve ``(E, d)``: centro medio de los globos oculares (mm) y
        dirección de mirada unitaria, ambos en coordenadas de cámara.
        ``None`` si la pose no es utilizable.
        """
        rvec = pose.get("rvec")
        w, h = pose.get("w"), pose.get("h")
        K = pose.get("K")
        if rvec is None or not pose.get("pose_ok", False):
            return None
        if not w or not h or K is None:
            return None

        R, _ = cv2.Rodrigues(rvec)
        t_mm = (np.asarray(pose["tvec"], np.float64).reshape(3)
                / MODEL_UNITS_PER_MM)
        f  = float(K[0, 0])
        cx = float(K[0, 2])
        cy = float(K[1, 2])

        centers, dirs = [], []
        for iris_idx, eye_mm in ((self.R_IRIS, EYE_CENTER_R_MM),
                                 (self.L_IRIS, EYE_CENTER_L_MM)):
            C = R @ np.asarray(eye_mm, np.float64) + t_mm

            px = float(lm[iris_idx].x) * w
            py = float(lm[iris_idx].y) * h
            d = np.array([(px - cx) / f, (py - cy) / f, 1.0])
            n = float(np.linalg.norm(d))
            if n < 1e-9:
                continue
            d /= n

            # Corte del rayo con la esfera del globo ocular (raíz cercana)
            b = float(C @ d)
            disc = b * b - float(C @ C) + EYE_RADIUS_MM ** 2
            if disc <= 0:
                continue
            lam = b - math.sqrt(disc)
            if lam <= 0:
                continue
            g = lam * d - C
            ng = float(np.linalg.norm(g))
            if ng < 1e-9:
                continue
            centers.append(C)
            dirs.append(g / ng)

        if not centers:
            return None
        E = np.mean(centers, axis=0)
        d = np.mean(dirs, axis=0)
        nd = float(np.linalg.norm(d))
        if nd < 1e-9:
            return None
        return E, d / nd

    def extract(self, lm, pose: Dict) -> Dict:
        r_dx, r_dy = self._iris_disp(lm, self.R_IRIS, self.R_OUT,
                                     self.R_IN, self.R_TOP, self.R_BOT)
        l_dx, l_dy = self._iris_disp(lm, self.L_IRIS, self.L_OUT,
                                     self.L_IN, self.L_TOP, self.L_BOT)
        eye_dx = (r_dx + l_dx) / 2.
        eye_dy = (r_dy + l_dy) / 2.

        out = dict(
            # Señal de mirada pura (iris), en el sistema de la cabeza
            gx=eye_dx, gy=eye_dy,
            eye_dx=eye_dx, eye_dy=eye_dy,
            # Orientación de cabeza (3D), normalizada a ≈[-1, 1]
            yaw=pose["yaw_deg"] / 45.0,
            pitch=pose["pitch_deg"] / 45.0,
            roll=pose["roll_deg"] / 45.0,
            # Proxies 2D invariantes a escala
            p_yaw=pose["p_yaw"],
            p_pitch=pose["p_pitch"],
            # Traslación de cabeza en el encuadre
            fdx=pose["fdx"],
            fdy=pose["fdy"],
            # Valores crudos para inspección
            yaw_deg=pose["yaw_deg"], pitch_deg=pose["pitch_deg"],
            roll_deg=pose["roll_deg"],
            pose_ok=pose["pose_ok"],
        )

        # ── Mirada 3D invariante a la cabeza ──────────────────────────────
        geo = self.gaze_3d(lm, pose)
        if geo is not None:
            E, d = geo
            out.update(
                Ex=float(E[0]), Ey=float(E[1]), Ez=float(E[2]),
                dx=float(d[0]), dy=float(d[1]), dz=float(d[2]),
            )
        out["geom_ok"] = geo is not None
        return out


# ════════════════════════════════════════════════════════════════════════════
#  GazeCalibrator — Ridge polinómico 2º orden + Homografía
# ════════════════════════════════════════════════════════════════════════════

class GazeCalibrator:
    """
    Mapea el vector de características de mirada a coordenadas de pantalla.

    Métodos:
      • ``polynomial`` — Ridge sobre expansión polinómica de 2º orden
        con estandarización previa de columnas (mucho más estable que
        Ridge sobre features sin normalizar).
      • ``homography`` — ``cv2.findHomography`` + RANSAC sobre el iris.

    Mejoras frente a la versión anterior:
      • Rechazo de outliers por punto de calibración (MAD × 3).
      • Estandarización de features antes de Ridge.
      • Error RMS de calibración en píxeles, para saber si quedó bien.
      • Guardar / cargar la calibración en JSON.
    """

    TARGETS_9 = [
        (0.10, 0.10), (0.50, 0.10), (0.90, 0.10),
        (0.10, 0.50), (0.50, 0.50), (0.90, 0.50),
        (0.10, 0.90), (0.50, 0.90), (0.90, 0.90),
    ]
    TARGETS_5 = [
        (0.50, 0.50),
        (0.10, 0.10), (0.90, 0.10),
        (0.90, 0.90), (0.10, 0.90),
    ]

    FEATURE_NAMES = [
        "gx", "gy", "yaw", "pitch", "roll", "p_yaw", "p_pitch",
        "fdx", "fdy",
    ]

    # Ganancias del modo pre-calibración (sólo para la previsualización)
    PREVIEW_GAIN_X = 5.0
    PREVIEW_GAIN_Y = 4.0

    def __init__(self, screen_w: int = SCREEN_W, screen_h: int = SCREEN_H,
                 n_points: int = 9, method: str = "geometric",
                 alpha_l2: float = 3e-2) -> None:
        self.sw = screen_w
        self.sh = screen_h
        self.targets = self.TARGETS_9 if n_points == 9 else self.TARGETS_5
        self.method  = method
        self.alpha   = alpha_l2
        self.is_calibrated = False

        self._samples: List[Dict] = []
        self._Wx: Optional[np.ndarray] = None
        self._Wy: Optional[np.ndarray] = None
        self._mu: Optional[np.ndarray] = None
        self._sd: Optional[np.ndarray] = None
        self._H:  Optional[np.ndarray] = None
        # Modelo geométrico: plano de pantalla + transformación afín 2D
        self._z_plane: Optional[float] = None
        self._Gx: Optional[np.ndarray] = None
        self._Gy: Optional[np.ndarray] = None
        self.rms_error_px: Optional[float] = None
        self.per_point_counts: Dict[int, int] = {}

    # ── Vector de características ─────────────────────────────────────────

    @staticmethod
    def _raw_feats(feat: Dict) -> np.ndarray:
        g = lambda k: float(feat.get(k, 0.0) or 0.0)
        return np.array([g("gx"), g("gy"), g("yaw"), g("pitch"), g("roll"),
                         g("p_yaw"), g("p_pitch"), g("fdx"), g("fdy")],
                        dtype=np.float64)

    @staticmethod
    def _phi(feat: Dict) -> np.ndarray:
        """Expansión polinómica de 2º orden (17 términos)."""
        f = GazeCalibrator._raw_feats(feat)
        gx, gy, yaw, pitch, roll, p_yaw, p_pitch, fdx, fdy = f
        return np.array([
            1.0,
            gx, gy, yaw, pitch, roll, p_yaw, p_pitch, fdx, fdy,
            gx * gx, gy * gy, gx * gy,
            gx * yaw, gy * pitch,
            gx * p_yaw, gy * p_pitch,
        ], dtype=np.float64)

    # ── Recolección de muestras ───────────────────────────────────────────

    def add_sample(self, feat: Dict, tx: int, ty: int,
                   target_id: int = -1) -> None:
        if not feat:
            return
        raw = self._raw_feats(feat)
        if not np.all(np.isfinite(raw)):
            return

        # Vector geométrico (si lo hay) para el rechazo de outliers y el
        # ajuste del plano de pantalla.
        geo = None
        if feat.get("geom_ok"):
            E = np.array([feat["Ex"], feat["Ey"], feat["Ez"]], np.float64)
            d = np.array([feat["dx"], feat["dy"], feat["dz"]], np.float64)
            if np.all(np.isfinite(E)) and np.all(np.isfinite(d)):
                geo = (E, d)

        self._samples.append({
            "phi": self._phi(feat),
            "raw": raw,
            "geo": geo,
            # Para el filtro de outliers: en modo geométrico se vigilan las
            # componentes 3D, que son las que de verdad entran en el ajuste.
            "out": (np.concatenate([geo[0], geo[1]]) if geo is not None
                    else raw),
            "tx": float(tx), "ty": float(ty),
            "gx": float(raw[0]), "gy": float(raw[1]),
            "tid": int(target_id),
        })
        self.per_point_counts[target_id] = \
            self.per_point_counts.get(target_id, 0) + 1

    def reset(self) -> None:
        self._samples.clear()
        self._Wx = self._Wy = self._H = None
        self._mu = self._sd = None
        self._z_plane = self._Gx = self._Gy = None
        self.is_calibrated = False
        self.rms_error_px = None
        self.per_point_counts = {}

    @property
    def n_samples(self) -> int:
        return len(self._samples)

    # ── Entrenamiento ─────────────────────────────────────────────────────

    def _reject_outliers(self) -> int:
        """
        Elimina muestras atípicas dentro de cada punto de calibración.

        Se comprueba el vector completo de características (iris + pose +
        traslación), no sólo el iris: si el usuario movió la cabeza mientras
        miraba un punto, esa muestra enseña a la regresión una relación
        iris→pantalla que no es cierta y degrada toda la calibración.

        Criterio: distancia robusta > 3 · MAD respecto a la mediana de su
        propio punto, en cualquiera de las dimensiones.
        """
        by_tid: Dict[int, List[Dict]] = {}
        for s in self._samples:
            by_tid.setdefault(s["tid"], []).append(s)

        kept: List[Dict] = []
        removed = 0
        for tid, group in by_tid.items():
            if len(group) < 6:
                kept.extend(group)      # muy pocas muestras: no filtrar
                continue
            arr = np.array([s["out"] for s in group])
            med = np.median(arr, axis=0)
            mad = np.median(np.abs(arr - med), axis=0)
            mad = np.where(mad < 1e-6, 1e-6, mad)
            z = np.abs(arr - med) / (1.4826 * mad)
            keep_mask = np.max(z, axis=1) <= 3.0
            kept.extend(s for s, k in zip(group, keep_mask) if k)
            removed += int((~keep_mask).sum())
        self._samples = kept
        return removed

    def train(self) -> bool:
        n = len(self._samples)
        if n < 12:
            print(f"[Calibrador] Solo {n} muestras — mínimo 12 requeridas.")
            return False

        removed = self._reject_outliers()
        if removed:
            print(f"[Calibrador] {removed} muestras descartadas por outlier.")
        if len(self._samples) < 12:
            print("[Calibrador] Muestras insuficientes tras filtrar outliers.")
            return False

        if self.method == "homography":
            return self._train_homography()
        if self.method == "geometric":
            if self._train_geometric():
                return True
            print("[Calibrador] Modelo geométrico sin datos 3D suficientes "
                  "— se usa el polinómico.")
            self.method = "polynomial"
        return self._train_polynomial()

    # ── Modelo geométrico (invariante a la cabeza) ────────────────────────

    # Rejilla de distancias cámara↔pantalla exploradas (mm)
    Z_GRID = np.linspace(-250.0, 500.0, 376)
    Z_TOL_PX = 2.0        # tolerancia absoluta de RMS (px)
    Z_TOL_REL = 0.15      # tolerancia relativa de RMS

    def _train_geometric(self) -> bool:
        """
        Ajusta el modelo de mirada 3D.

        Modelo
        ------
        El usuario mira al punto P de la pantalla.  El ojo está en E y su
        dirección de mirada es d (ambos en coordenadas de cámara).  Entonces
        ``P = E + λ·d`` con ``λ = (Z − E_z)/d_z``, siendo Z el plano de la
        pantalla.  El paso de P (mm sobre la pantalla) a píxeles es afín.

        Sólo hay que estimar Z y una transformación afín 2D.  Con Z fijo el
        ajuste es LINEAL, así que se barre Z en una rejilla y se resuelve un
        sistema lineal por cada valor: barato y sin mínimos locales.

        Identificabilidad de Z
        ----------------------
        Con la cabeza QUIETA, Z **no** es identificable: cualquier Z da un
        ajuste afín exacto, porque el punto de corte del rayo y el punto real
        difieren en una transformación afín.  Sólo se identifica si el usuario
        traslada la cabeza durante la calibración.  Por eso, entre todas las
        distancias con RMS prácticamente idéntico se elige la más cercana a 0:
        es la geometría real de una webcam de portátil (la pantalla está en el
        plano del objetivo) y, medido en ``_t_pose.py``, equivocarse en ±50 mm
        cuesta menos de 20 px.
        """
        rows = [s for s in self._samples if s.get("geo") is not None]
        if len(rows) < 6:
            return False

        def solve_at(Z: float):
            A, bx, by = [], [], []
            for s in rows:
                E, d = s["geo"]
                dz = float(d[2])
                if abs(dz) < 1e-9:
                    return None
                lam = (Z - float(E[2])) / dz
                if lam <= 0:
                    return None
                P = E + lam * d
                A.append([float(P[0]) / 1000.0, float(P[1]) / 1000.0, 1.0])
                bx.append(s["tx"])
                by.append(s["ty"])
            A = np.array(A)
            bx = np.array(bx, np.float64)
            by = np.array(by, np.float64)
            Wx = np.linalg.lstsq(A, bx, rcond=None)[0]
            Wy = np.linalg.lstsq(A, by, rcond=None)[0]
            rms = float(np.sqrt(np.mean((A @ Wx - bx) ** 2 +
                                        (A @ Wy - by) ** 2)))
            return rms, Wx, Wy

        resultados = []
        for Z in self.Z_GRID:
            r = solve_at(float(Z))
            if r is not None:
                resultados.append((float(Z), r[0], r[1], r[2]))
        if not resultados:
            return False

        mejor = min(r[1] for r in resultados)
        tol = max(self.Z_TOL_PX, self.Z_TOL_REL * mejor)
        candidatos = [r for r in resultados if r[1] <= mejor + tol]
        # Desempate: la distancia más cercana a 0 (geometría de portátil)
        Z_sel, rms, Wx, Wy = min(candidatos, key=lambda r: abs(r[0]))

        self._z_plane = float(Z_sel)
        self._Gx, self._Gy = Wx, Wy
        self.rms_error_px = float(rms)
        self.is_calibrated = True
        print(f"[Calibrador] Geométrico — {len(rows)} muestras, "
              f"plano de pantalla Z = {Z_sel:+.0f} mm, RMS = {rms:.0f} px")
        return True

    def _train_polynomial(self) -> bool:
        X   = np.array([s["phi"] for s in self._samples])
        Y_x = np.array([s["tx"] for s in self._samples])
        Y_y = np.array([s["ty"] for s in self._samples])

        # Estandarizar todas las columnas menos el término de sesgo
        self._mu = X.mean(axis=0)
        self._sd = X.std(axis=0)
        self._sd[self._sd < 1e-9] = 1.0
        self._mu[0] = 0.0
        self._sd[0] = 1.0
        Z = (X - self._mu) / self._sd

        lam = np.eye(Z.shape[1]) * self.alpha
        lam[0, 0] = 0.0
        ZtZ = Z.T @ Z + lam
        try:
            self._Wx = np.linalg.solve(ZtZ, Z.T @ Y_x)
            self._Wy = np.linalg.solve(ZtZ, Z.T @ Y_y)
        except np.linalg.LinAlgError:
            P = np.linalg.pinv(ZtZ)
            self._Wx = P @ (Z.T @ Y_x)
            self._Wy = P @ (Z.T @ Y_y)

        # Error RMS sobre los propios puntos de calibración
        px = Z @ self._Wx
        py = Z @ self._Wy
        self.rms_error_px = float(np.sqrt(np.mean((px - Y_x) ** 2 +
                                                   (py - Y_y) ** 2)))
        self.is_calibrated = True
        print(f"[Calibrador] Polinomial entrenado — {len(self._samples)} muestras, "
              f"RMS = {self.rms_error_px:.0f} px")
        return True

    def _train_homography(self) -> bool:
        src = np.array([[s["gx"], s["gy"]] for s in self._samples], np.float32)
        dst = np.array([[s["tx"], s["ty"]] for s in self._samples], np.float32)
        H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        if H is None:
            print("[Calibrador] Homografía falló — usando polinomial.")
            return self._train_polynomial()
        self._H = H
        self.is_calibrated = True
        inliers = int(mask.sum()) if mask is not None else len(self._samples)
        try:
            proj = cv2.perspectiveTransform(src.reshape(-1, 1, 2), H).reshape(-1, 2)
            self.rms_error_px = float(np.sqrt(np.mean(np.sum((proj - dst) ** 2,
                                                              axis=1))))
        except Exception:
            self.rms_error_px = None
        print(f"[Calibrador] Homografía RANSAC — {inliers}/{len(self._samples)} inliers, "
              f"RMS = {self.rms_error_px:.0f} px" if self.rms_error_px
              else f"[Calibrador] Homografía RANSAC — {inliers} inliers")
        return True

    # ── Predicción ────────────────────────────────────────────────────────

    def _predict_geometric(self, feat: Dict):
        """Corta el rayo de mirada con el plano de pantalla → píxeles."""
        if (not feat.get("geom_ok") or self._Gx is None
                or self._z_plane is None):
            return None
        try:
            E = np.array([feat["Ex"], feat["Ey"], feat["Ez"]], np.float64)
            d = np.array([feat["dx"], feat["dy"], feat["dz"]], np.float64)
        except (KeyError, TypeError):
            return None
        if not (np.all(np.isfinite(E)) and np.all(np.isfinite(d))):
            return None
        dz = float(d[2])
        if abs(dz) < 1e-9:
            return None
        lam = (self._z_plane - float(E[2])) / dz
        if lam <= 0:
            return None
        P = E + lam * d
        a = np.array([float(P[0]) / 1000.0, float(P[1]) / 1000.0, 1.0])
        return float(a @ self._Gx), float(a @ self._Gy)

    def predict(self, feat: Dict) -> Tuple[int, int]:
        px = py = None

        # 1) Modelo geométrico 3D (invariante a la orientación de la cabeza)
        if self.is_calibrated and self._Gx is not None:
            geo = self._predict_geometric(feat)
            if geo is not None:
                px, py = geo

        # 2) Homografía sobre el iris
        if px is None and self.is_calibrated and self._H is not None:
            raw = self._raw_feats(feat)
            pt = np.array([[[raw[0], raw[1]]]], dtype=np.float32)
            res = cv2.perspectiveTransform(pt, self._H)
            px, py = float(res[0, 0, 0]), float(res[0, 0, 1])

        # 3) Polinomio de 2º orden
        if px is None and self.is_calibrated and self._Wx is not None:
            phi = self._phi(feat)
            assert self._mu is not None and self._sd is not None
            z = (phi - self._mu) / self._sd
            px = float(z @ self._Wx)
            py = float(z @ self._Wy)

        # 4) Pre-calibración: mapeo suave con tanh para que el punto NUNCA
        #    se quede pegado en una esquina (el bug anterior saturaba
        #    porque multiplicaba linealmente y recortaba con clip).
        if px is None:
            raw = self._raw_feats(feat)
            px = self.sw / 2. + (self.sw / 2.) * math.tanh(
                raw[0] * self.PREVIEW_GAIN_X)
            py = self.sh / 2. + (self.sh / 2.) * math.tanh(
                raw[1] * self.PREVIEW_GAIN_Y)

        if not (math.isfinite(px) and math.isfinite(py)):
            return int(self.sw // 2), int(self.sh // 2)
        px = int(np.clip(px, 0, self.sw - 1))
        py = int(np.clip(py, 0, self.sh - 1))
        return px, py

    # ── Persistencia ──────────────────────────────────────────────────────

    def save(self, path: str) -> bool:
        if not self.is_calibrated:
            return False
        try:
            data = {
                "version": 3,
                "sw": self.sw, "sh": self.sh,
                "method": self.method,
                "mu": None if self._mu is None else self._mu.tolist(),
                "sd": None if self._sd is None else self._sd.tolist(),
                "Wx": None if self._Wx is None else self._Wx.tolist(),
                "Wy": None if self._Wy is None else self._Wy.tolist(),
                "H":  None if self._H is None else self._H.tolist(),
                # Modelo geométrico
                "z_plane": self._z_plane,
                "Gx": None if self._Gx is None else self._Gx.tolist(),
                "Gy": None if self._Gy is None else self._Gy.tolist(),
                "rms": self.rms_error_px,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return True
        except Exception as e:
            print(f"[Calibrador] No se pudo guardar: {e}")
            return False

    def load(self, path: str) -> bool:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("sw") != self.sw or d.get("sh") != self.sh:
                print("[Calibrador] La calibración guardada es de otra resolución.")
                return False
            # Las calibraciones anteriores a la v3 no traen el modelo
            # geométrico y su regresión se ajustó con las features 2D
            # antiguas.  Cargarlas daría una mirada peor que no calibrar,
            # así que se rechazan y se pide recalibrar.
            if int(d.get("version", 1)) < 3:
                print("[Calibrador] La calibración guardada es del modelo "
                      "antiguo (v2). Hay que recalibrar: pulsa "
                      "'🎯 Calibrar Ahora'.")
                return False

            # Los parámetros guardados sólo sirven para el modelo con el que
            # se ajustaron.  Antes se hacía ``self.method = d["method"]``, lo
            # que SOBRESCRIBÍA en silencio el modelo que pide la aplicación:
            # un archivo viejo de tipo "polynomial" degradaba la app al
            # modelo sin invariancia a la cabeza sin avisar.  Ahora, si no
            # coinciden, se rechaza y se pide recalibrar.
            saved_method = d.get("method")
            if saved_method and saved_method != self.method:
                print(f"[Calibrador] La calibración guardada se ajustó con el "
                      f"modelo '{saved_method}' pero la aplicación usa "
                      f"'{self.method}'. Hay que recalibrar: pulsa "
                      f"'🎯 Calibrar Ahora'.")
                return False

            self.method = d.get("method", self.method)
            self._mu = None if d.get("mu") is None else np.array(d["mu"])
            self._sd = None if d.get("sd") is None else np.array(d["sd"])
            self._Wx = None if d.get("Wx") is None else np.array(d["Wx"])
            self._Wy = None if d.get("Wy") is None else np.array(d["Wy"])
            self._H  = None if d.get("H") is None else np.array(d["H"])
            self._z_plane = d.get("z_plane")
            self._Gx = None if d.get("Gx") is None else np.array(d["Gx"])
            self._Gy = None if d.get("Gy") is None else np.array(d["Gy"])
            self.rms_error_px = d.get("rms")
            self.is_calibrated = bool(self._Wx is not None
                                      or self._H is not None
                                      or self._Gx is not None)
            return self.is_calibrated
        except Exception as e:
            print(f"[Calibrador] No se pudo cargar: {e}")
            return False


# ════════════════════════════════════════════════════════════════════════════
#  GazeTracker  —  Motor principal
# ════════════════════════════════════════════════════════════════════════════

class GazeTracker:
    """
    Motor de estimación de mirada en tiempo real.

    Corre en un hilo daemon.  El hilo principal consume muestras de
    ``sample_queue`` o llama a ``get_latest()`` sin bloquear.

    Selección de cámara:
        ``camera_index=None``  → elige automáticamente la primera cámara que
                                 entregue imagen real (probando backends).
        ``camera_index=N``     → intenta N; si falla, cae al modo automático.

    Estado:
        ``status``       — texto legible del estado del motor
        ``camera_info``  — CameraInfo de la cámara en uso (o None)
        ``running``      — True mientras el hilo está vivo
    """

    def __init__(self,
                 camera_index: Optional[int] = 0,
                 calibrator: Optional[GazeCalibrator] = None,
                 use_l2cs: bool = False,
                 cam_width: int = 640,
                 cam_height: int = 480,
                 cam_fps: int = 30,
                 auto_tune: bool = True) -> None:
        self.camera_index = camera_index
        self.calibrator   = calibrator or GazeCalibrator()
        self.use_l2cs     = use_l2cs          # legado: ya no se usa

        self.cam_width  = cam_width
        self.cam_height = cam_height
        self.cam_fps    = cam_fps
        self.auto_tune  = auto_tune           # negociar resolución por FPS

        # Componentes internos
        self._face_mesh     = _FaceMeshWrapper()
        self._head_pose     = _HeadPoseEstimator()
        self._eye_extractor = _EyeGazeExtractor()
        self._pupil         = None            # se crea una sola vez
        self._filter        = OneEuroFilter2D(min_cutoff=0.4, beta=0.005)

        # Filtro del vector geométrico (Ex,Ey,Ez,dx,dy,dz).
        #
        # La mirada 3D AMPLIFICA el ruido de landmarks: el globo ocular mide
        # 12 mm y un píxel de ruido a 550 mm son ~0,9 mm, o sea ~4° de ángulo.
        # El filtro temporal es la defensa principal.  Los coeficientes beta
        # están escalados a cada señal: las componentes de la dirección (rango
        # ±0,5, velocidad de sacada ~5/s) y la posición del ojo (mm, velocidad
        # de giro de cabeza ~200 mm/s).
        self._geo_filters = [
            OneEuroFilter(0.6, 0.012), OneEuroFilter(0.6, 0.012),
            OneEuroFilter(0.6, 0.012),
            OneEuroFilter(0.6, 0.50),  OneEuroFilter(0.6, 0.50),
            OneEuroFilter(0.6, 0.50),
        ]

        # ── Publicación de muestras ──────────────────────────────────────
        #
        # Antes había una sola cola (maxsize=2) que TODOS los consumidores
        # vaciaban con get_latest().  Eso era un bug grave: la GUI, el
        # calibrador y el grabador competían por la misma cola, y el que
        # llegaba primero se llevaba la muestra.  Con la GUI refrescando a
        # 60 Hz, el grabador sólo recibía ~la mitad de las muestras
        # (medido: 7.3 Hz de 15 Hz reales), lo que arruinaba la detección
        # de fijaciones y producía scanpaths ilegibles.
        #
        # Ahora la fuente de verdad es un histórico circular que se lee
        # SIN consumir:
        #   • get_latest()  → último sample (GUI, calibración, estado)
        #   • get_since(id) → todos los samples nuevos desde un cursor
        #                     (grabador; no pierde ni una muestra)
        self._history: Deque[GazeSample] = deque(maxlen=HISTORY_MAXLEN)
        self._hist_lock = threading.Lock()
        self._latest_sample: Optional[GazeSample] = None

        # Cola heredada: se sigue alimentando por compatibilidad, pero ya
        # nadie depende de ella para no perder datos.
        self.sample_queue: queue.Queue[GazeSample] = queue.Queue(maxsize=2)

        # Frame debug compartido
        self._dbg_frame: Optional[np.ndarray] = None
        self._dbg_lock  = threading.Lock()

        # Estado
        self.fps          = 0.0
        self.jitter_ms    = 0.0
        self._dt_history: List[float] = []
        self.camera_info: Optional[CameraInfo] = None
        self.status       = "iniciando…"
        self.last_error: Optional[str] = None

        # Control del hilo
        self._running   = False
        self._thread:   Optional[threading.Thread] = None
        self._frame_id  = 0
        self._reopen_request: Optional[int] = None
        self._reopen_lock = threading.Lock()
        self._cap: Optional[cv2.VideoCapture] = None

        self.ready = threading.Event()

    # ── Interfaz pública ─────────────────────────────────────────────────

    def start(self) -> None:
        """Inicia el hilo de captura en background."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="GazeTracker")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        self._face_mesh.close()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def get_latest(self) -> Optional[GazeSample]:
        """
        Último GazeSample disponible.  **No consume nada.**

        Antes esta función vaciaba la cola, así que cada llamada destruía
        las muestras que otro consumidor aún no había leído.  Ahora es una
        lectura pura del histórico: se puede llamar desde la GUI a 60 Hz
        sin robarle datos al grabador.
        """
        with self._hist_lock:
            return self._latest_sample

    def get_since(self, last_frame_id: int) -> List[GazeSample]:
        """
        Todos los samples con ``frame_id`` mayor que ``last_frame_id``.

        Pensado para consumidores que deben ver la secuencia COMPLETA
        (grabador CSV, servidor de experimentos).  El consumidor guarda el
        ``frame_id`` del último sample recibido y lo pasa aquí en la
        siguiente vuelta; así no se pierde ninguno aunque el bucle se
        retrase (bloqueos del GIL, ventana de calibración, etc.).

        Devuelve la lista en orden temporal ascendente.
        """
        with self._hist_lock:
            if not self._history:
                return []
            return [s for s in self._history if s.frame_id > last_frame_id]

    def clear_history(self) -> None:
        """Descarta el histórico (al iniciar una grabación nueva)."""
        with self._hist_lock:
            self._history.clear()
            self._latest_sample = None

    def get_debug_frame(self) -> Optional[np.ndarray]:
        with self._dbg_lock:
            return self._dbg_frame.copy() if self._dbg_frame is not None else None

    def request_camera(self, index: Optional[int]) -> None:
        """Pide cambiar de cámara en caliente (lo aplica el hilo del motor)."""
        with self._reopen_lock:
            self._reopen_request = index

    @staticmethod
    def list_cameras(max_index: int = 6,
                     width: int = 640, height: int = 480) -> List[CameraInfo]:
        """Explora las cámaras disponibles (bloqueante, ~1 s por índice)."""
        return probe_cameras(max_index=max_index, width=width, height=height)

    # ── Apertura de cámara ───────────────────────────────────────────────

    def _open(self, index: Optional[int]):
        cap = info = None

        if self.auto_tune:
            # Primero probar el índice pedido negociando resolución.
            for idx in ([index] if index is not None else []) + \
                       [i for i in range(6) if i != index]:
                if idx is None:
                    continue
                cap, info = open_camera_best(
                    int(idx), target=(self.cam_width, self.cam_height),
                    cache_path=CAMERA_CACHE)
                if cap is not None:
                    break
        else:
            cap, info = auto_open(preferred_index=index,
                                  width=self.cam_width,
                                  height=self.cam_height,
                                  fps=self.cam_fps)

        if cap is not None and info is not None:
            self.camera_info  = info
            self.camera_index = info.index
            self.last_error   = None
            fps_txt = (f" @ {info.measured_fps:.0f} fps"
                       if info.measured_fps else "")
            self.status = f"OK · {info.label}"
            if info.measured_fps and info.measured_fps < 12:
                self.status += "  (⚠ pocos FPS)"
            print(f"[GazeTracker] Cámara abierta: {info.label}{fps_txt}")
            return cap

        self.camera_info = None
        self.last_error = "No se encontró ninguna cámara utilizable."
        self.status = "ERROR: sin cámara"
        print("[GazeTracker] ERROR: no se pudo abrir ninguna cámara.")
        return None

    # ── Loop interno ─────────────────────────────────────────────────────

    def _loop(self) -> None:
        if self._pupil is None:
            from .pupillometry import PupilTracker
            self._pupil = PupilTracker()

        cap = self._open(self.camera_index)
        self.ready.set()

        consecutive_fail = 0
        last_retry = 0.0
        t_prev = time.perf_counter()

        while self._running:
            # ── ¿Cambio de cámara solicitado? ──────────────────────────
            with self._reopen_lock:
                req = self._reopen_request
                self._reopen_request = None
            if req is not None or cap is None:
                if cap is not None:
                    cap.release()
                    cap = None
                target = req if req is not None else self.camera_index
                cap = self._open(target)
                consecutive_fail = 0
                if cap is None:
                    # Sin cámara: reintentar cada 3 s sin morir en silencio
                    time.sleep(3.0)
                    continue

            ok, raw = cap.read()
            if not ok or raw is None:
                consecutive_fail += 1
                if consecutive_fail >= 45:
                    # La cámara se cayó (USB / suspensión) → reabrir
                    print("[GazeTracker] Cámara dejó de responder, reabriendo…")
                    self.status = "Reconectando cámara…"
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap = None
                    consecutive_fail = 0
                    time.sleep(0.5)
                else:
                    time.sleep(0.01)
                continue
            consecutive_fail = 0

            self._frame_id += 1
            t_now = time.perf_counter()
            dt    = t_now - t_prev
            t_prev = t_now

            # Métricas temporales
            self.fps = 0.92 * self.fps + 0.08 / max(dt, 1e-5)
            self._dt_history.append(dt * 1000.)
            if len(self._dt_history) > 30:
                self._dt_history.pop(0)
            self.jitter_ms = (float(np.std(self._dt_history))
                              if len(self._dt_history) > 2 else 0.)

            # OJO: se detecta sobre el frame REAL (sin espejar).  Espejar
            # antes de detectar intercambia los landmarks izquierda/derecha
            # y rompe la correspondencia con el modelo 3D de solvePnP.
            h, w = raw.shape[:2]
            rgb  = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)

            sample = GazeSample(
                timestamp_ms=t_now * 1000.,
                frame_id=self._frame_id,
                gaze_x=SCREEN_W / 2., gaze_y=SCREEN_H / 2.,
                gaze_x_raw=SCREEN_W / 2., gaze_y_raw=SCREEN_H / 2.,
                pupil_size=0., event_type="Unknown",
                head_pitch=0., head_yaw=0., head_roll=0.,
                face_detected=False,
                sample_hz=self.fps, jitter_ms=self.jitter_ms,
                pose_ok=False,
                calibrated=self.calibrator.is_calibrated,
            )

            lm = self._face_mesh.detect(rgb, int(t_now * 1000))

            if lm is not None:
                sample.face_detected = True

                # ── Pupilometría ──────────────────────────────────────
                try:
                    sample.pupil_size = self._pupil.estimate(lm, w, h)
                except Exception:
                    sample.pupil_size = 0.0

                # ── Pose cefálica ─────────────────────────────────────
                pose = self._head_pose.estimate(lm, w, h)
                if pose:
                    sample.head_pitch = pose["pitch_deg"]
                    sample.head_yaw   = pose["yaw_deg"]
                    sample.head_roll  = pose["roll_deg"]
                    sample.pose_ok    = pose["pose_ok"]

                    feat = self._eye_extractor.extract(lm, pose)
                    if not pose["pose_ok"]:
                        # Pose no creíble: se anulan los regresores de cabeza
                        # para no envenenar la predicción; el iris sigue
                        # siendo válido y es la señal principal.
                        for k in ("yaw", "pitch", "roll", "p_yaw", "p_pitch"):
                            feat[k] = 0.0

                    # ── Filtrado temporal del vector geométrico ───────────
                    # Necesario: la reconstrucción 3D amplifica el ruido de
                    # landmarks (~4° por píxel).  El filtro 1€ reduce el ruido
                    # ~4× y se abre solo durante las sacadas para no añadir lag.
                    if feat.get("geom_ok"):
                        vals = [feat["Ex"], feat["Ey"], feat["Ez"],
                                feat["dx"], feat["dy"], feat["dz"]]
                        vals = [flt(v, t_now)
                                for flt, v in zip(self._geo_filters, vals)]
                        feat["Ex"], feat["Ey"], feat["Ez"] = vals[:3]
                        dvec = np.array(vals[3:], np.float64)
                        nrm = float(np.linalg.norm(dvec))
                        if nrm > 1e-9:
                            dvec = dvec / nrm
                            feat["dx"], feat["dy"], feat["dz"] = (
                                float(dvec[0]), float(dvec[1]), float(dvec[2]))
                    else:
                        for flt in self._geo_filters:
                            flt.reset()

                    sample.raw_feat = feat
                    raw_x, raw_y = self.calibrator.predict(feat)
                    sample.gaze_x_raw = float(raw_x)
                    sample.gaze_y_raw = float(raw_y)
                    fx, fy = self._filter(float(raw_x), float(raw_y), t_now)
                    sample.gaze_x = fx
                    sample.gaze_y = fy

                    self._draw_debug(raw, lm, sample, pose, w, h)
                else:
                    cv2.putText(raw, "Pose no disponible", (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 80, 255), 2)
            else:
                # Sin cara: reiniciar filtros para no arrastrar estado viejo
                for flt in self._geo_filters:
                    flt.reset()
                self._filter.reset()
                cv2.putText(raw, "Sin cara detectada", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 80, 255), 2)

            # HUD
            cv2.putText(
                raw,
                f"FPS:{self.fps:.0f}  Jitter:{self.jitter_ms:.1f}ms  "
                f"Cal:{'OK' if self.calibrator.is_calibrated else 'NO'}",
                (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

            # El frame de debug SÍ se espeja: así el usuario se ve natural
            # (como en un espejo) aunque la detección use el frame real.
            with self._dbg_lock:
                self._dbg_frame = cv2.flip(raw, 1)

            self._publish(sample)

        if cap is not None:
            cap.release()
        self.status = "detenido"

    # ── Debug ────────────────────────────────────────────────────────────

    def _draw_debug(self, frame: np.ndarray, lm, s: GazeSample,
                    pose: Dict, w: int, h: int) -> None:
        # Iris
        for idx in (468, 473):
            px = (int(lm[idx].x * w), int(lm[idx].y * h))
            cv2.circle(frame, px, 4, (0, 255, 200), -1)

        # Ejes de orientación cefálica
        self._head_pose.draw_axes(frame, pose)

        # Flecha de dirección de mirada (desde la nariz)
        nose = (int(lm[1].x * w), int(lm[1].y * h))
        gx = s.raw_feat.get("gx", 0.)
        gy = s.raw_feat.get("gy", 0.)
        tip = (int(nose[0] + gx * 600), int(nose[1] + gy * 600))
        cv2.arrowedLine(frame, nose, tip, (0, 0, 255), 2, tipLength=0.25)

        cv2.putText(frame,
                    f"Head Y:{s.head_yaw:+.1f} P:{s.head_pitch:+.1f} "
                    f"R:{s.head_roll:+.1f}{'' if s.pose_ok else ' (!)'}",
                    (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
        cv2.putText(frame,
                    f"Iris dx:{gx:+.3f} dy:{gy:+.3f}  Pupil:{s.pupil_size:.1f}px",
                    (10, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 220, 255), 2)

    def _publish(self, s: GazeSample) -> None:
        # Histórico circular: fuente de verdad, lectura no destructiva
        with self._hist_lock:
            self._history.append(s)
            self._latest_sample = s

        # Cola heredada (compatibilidad con consumidores antiguos)
        try:
            self.sample_queue.put_nowait(s)
        except queue.Full:
            try:
                self.sample_queue.get_nowait()
                self.sample_queue.put_nowait(s)
            except queue.Empty:
                pass
