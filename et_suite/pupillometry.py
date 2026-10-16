"""
et_suite/pupillometry.py  —  Pupilometría, I-VT y TTFF
=======================================================
Módulos:
  • PupilTracker  — estima el tamaño del iris/pupila en cada frame
  • IVTClassifier — clasifica cada muestra como Fijación o Sacada (I-VT)
  • AOI           — Área de Interés configurable
  • TTFFTracker   — Time To First Fixation dentro de un AOI
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# Área de Interés (AOI)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AOI:
    """
    Área de Interés rectangular en coordenadas de pantalla (píxeles).

    Attributes:
        x, y:          esquina superior izquierda
        width, height: dimensiones
        label:         nombre descriptivo
    """
    x: float
    y: float
    width: float
    height: float
    label: str = "AOI"

    def contains(self, px: float, py: float) -> bool:
        """Retorna True si el punto (px, py) está dentro del AOI."""
        return (self.x <= px <= self.x + self.width and
                self.y <= py <= self.y + self.height)

    def as_rect(self) -> Tuple[int, int, int, int]:
        """(x, y, w, h) como enteros."""
        return int(self.x), int(self.y), int(self.width), int(self.height)


# ─────────────────────────────────────────────────────────────────────────────
# PupilTracker — estimación del radio del iris/pupila
# ─────────────────────────────────────────────────────────────────────────────

class PupilTracker:
    """
    Estima el tamaño del iris en píxeles a partir de los landmarks del iris
    de MediaPipe FaceLandmarker (puntos 468–477 con refine_landmarks=True).

    Método rápido:
        Distancia euclidiana entre los extremos horizontales del iris
        (puntos 469 ←→ 471 para el ojo derecho,  474 ←→ 476 para el izquierdo).
        Esto representa el diámetro aparente del iris en píxeles.

    Método preciso (ROI + fitEllipse):
        Recorta el ROI del ojo, aplica umbralización adaptativa Gaussian,
        extrae contornos y ajusta una elipse.  Más robusto ante reflejos,
        pero ~3× más costoso.

    Retorna el radio promedio interocular en píxeles (= diámetro / 2).
    """

    # Índices de landmarks del iris (MediaPipe 478 pts)
    # Ojo derecho del USUARIO  (izq en imagen sin flip)
    R_IRIS_CENTER = 468
    R_IRIS_RIGHT  = 469   # extremo derecho del iris
    R_IRIS_TOP    = 470
    R_IRIS_LEFT   = 471   # extremo izquierdo del iris
    R_IRIS_BOT    = 472

    # Ojo izquierdo del USUARIO  (der en imagen sin flip)
    L_IRIS_CENTER = 473
    L_IRIS_RIGHT  = 474
    L_IRIS_TOP    = 475
    L_IRIS_LEFT   = 476
    L_IRIS_BOT    = 477

    # Límites del ojo (para ROI)
    R_EYE_INNER  = 133;  R_EYE_OUTER  = 33
    R_EYE_TOP    = 159;  R_EYE_BOT    = 145
    L_EYE_INNER  = 362;  L_EYE_OUTER  = 263
    L_EYE_TOP    = 386;  L_EYE_BOT    = 374

    def __init__(self, precise: bool = False) -> None:
        self.precise = precise

    def estimate(self, landmarks, img_w: int, img_h: int,
                 frame_bgr=None) -> float:
        """
        Estima el radio promedio del iris en píxeles.

        Args:
            landmarks:  lista de landmarks de MediaPipe (478 puntos)
            img_w/h:    dimensiones del frame
            frame_bgr:  frame original (solo necesario en modo preciso)

        Returns:
            Radio del iris en píxeles  (≈ 10–20 px típico a 60cm, webcam 640×480)
        """
        r_radius = self._fast_radius(landmarks, img_w, img_h,
                                      self.R_IRIS_LEFT, self.R_IRIS_RIGHT,
                                      self.R_IRIS_TOP,  self.R_IRIS_BOT)
        l_radius = self._fast_radius(landmarks, img_w, img_h,
                                      self.L_IRIS_LEFT, self.L_IRIS_RIGHT,
                                      self.L_IRIS_TOP,  self.L_IRIS_BOT)
        avg = (r_radius + l_radius) / 2.0

        if self.precise and frame_bgr is not None:
            precise_r = self._precise_radius(landmarks, frame_bgr, img_w, img_h)
            if precise_r > 0:
                avg = 0.4 * avg + 0.6 * precise_r   # fusión ponderada

        return avg

    @staticmethod
    def _fast_radius(lm, w: int, h: int,
                     left_idx: int, right_idx: int,
                     top_idx: int, bot_idx: int) -> float:
        lx = lm[left_idx].x  * w;  ly = lm[left_idx].y  * h
        rx = lm[right_idx].x * w;  ry = lm[right_idx].y * h
        tx = lm[top_idx].x   * w;  ty = lm[top_idx].y   * h
        bx = lm[bot_idx].x   * w;  by = lm[bot_idx].y   * h
        diam_h = math.hypot(rx - lx, ry - ly)
        diam_v = math.hypot(bx - tx, by - ty)
        return (diam_h + diam_v) / 4.0   # promedio de semiejes → radio medio

    @staticmethod
    def _precise_radius(lm, frame, w: int, h: int) -> float:
        """Ajuste de elipse sobre el ROI del ojo con umbralización adaptativa."""
        try:
            # ROI ojo derecho
            x1 = int(lm[PupilTracker.R_EYE_OUTER].x * w) - 8
            x2 = int(lm[PupilTracker.R_EYE_INNER].x * w) + 8
            y1 = int(lm[PupilTracker.R_EYE_TOP].y   * h) - 6
            y2 = int(lm[PupilTracker.R_EYE_BOT].y   * h) + 6
            x1 = max(x1, 0); y1 = max(y1, 0)
            x2 = min(x2, w); y2 = min(y2, h)
            if x2 <= x1 or y2 <= y1:
                return 0.0

            roi = frame[y1:y2, x1:x2]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            blur = cv2.GaussianBlur(gray, (7, 7), 0)
            _, thresh = cv2.threshold(
                blur, 0, 255,
                cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
            )
            cnts, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL,
                                        cv2.CHAIN_APPROX_SIMPLE)
            if not cnts:
                return 0.0
            largest = max(cnts, key=cv2.contourArea)
            if len(largest) < 5:
                return 0.0
            ellipse = cv2.fitEllipse(largest)
            _, (ma, mb), _ = ellipse
            return (ma + mb) / 4.0   # semi-eje promedio
        except Exception:
            return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# BlinkDetector — parpadeos por apertura del ojo (EAR)
# ─────────────────────────────────────────────────────────────────────────────

class BlinkDetector:
    """
    Detecta parpadeos con la *Eye Aspect Ratio* (Soukupová & Čech, 2016):

        EAR = (‖p2−p6‖ + ‖p3−p5‖) / (2·‖p1−p4‖)

    calculada sobre 6 landmarks de cada párpado.  Un ojo abierto da ~0.25–0.35
    y uno cerrado ~0.05–0.15.

    El umbral es RELATIVO: se compara con la apertura habitual del propio
    participante (percentil 80 de los últimos ``window_s`` segundos).  Así
    funciona igual con ojos muy abiertos o rasgados, con lentes, etc.

    Los parpadeos importan por dos razones: se reportan como métrica y, sobre
    todo, durante un parpadeo la estimación de mirada es basura, de modo que
    el grabador no debe tratar esas muestras como fijaciones ni sacadas.
    """

    # Orden: p1 (esquina externa), p2, p3 (párpado superior),
    #        p4 (esquina interna), p5, p6 (párpado inferior)
    R_EYE = (33, 160, 158, 133, 153, 144)
    L_EYE = (362, 385, 387, 263, 373, 380)

    def __init__(self, ratio: float = 0.62, window_s: float = 6.0,
                 min_baseline: float = 0.12, warmup: int = 8) -> None:
        self.ratio = ratio
        self.window_s = window_s
        self.min_baseline = min_baseline
        self.warmup = warmup
        self._hist: List[Tuple[float, float]] = []   # (t_s, ear)

    def reset(self) -> None:
        self._hist.clear()

    @staticmethod
    def _eye_ear(lm, idx, w: int, h: int) -> float:
        pts = [(lm[i].x * w, lm[i].y * h) for i in idx]
        d = lambda a, b: math.hypot(pts[a][0] - pts[b][0],
                                    pts[a][1] - pts[b][1])
        horiz = d(0, 3)
        if horiz < 1e-6:
            return 0.0
        return (d(1, 5) + d(2, 4)) / (2.0 * horiz)

    def ear(self, lm, w: int, h: int) -> float:
        """EAR promedio de los dos ojos."""
        return 0.5 * (self._eye_ear(lm, self.R_EYE, w, h) +
                      self._eye_ear(lm, self.L_EYE, w, h))

    def baseline(self) -> float:
        """Apertura habitual (percentil 80) en la ventana reciente."""
        if not self._hist:
            return 0.0
        return float(np.percentile([e for _, e in self._hist], 80))

    def update(self, ear: float, t_s: float) -> bool:
        """
        Registra una medición de EAR y dice si esta muestra es parpadeo.
        """
        # La ventana guarda sólo ojos "abiertos o dudosos": un parpadeo largo
        # no debe arrastrar la línea base hacia abajo.
        base = self.baseline()
        is_blink = (len(self._hist) >= self.warmup
                    and base >= self.min_baseline
                    and ear < self.ratio * base)
        if not is_blink:
            self._hist.append((t_s, ear))
        cutoff = t_s - self.window_s
        while self._hist and self._hist[0][0] < cutoff:
            self._hist.pop(0)
        return bool(is_blink)


# ─────────────────────────────────────────────────────────────────────────────
# I-VT Classifier — Velocity-Threshold Identification
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _FixationCandidate:
    start_t: float
    points: List[Tuple[float, float]] = field(default_factory=list)


@dataclass
class Fixation:
    """Registro de una fijación detectada."""
    start_ms: float
    end_ms: float
    cx: float          # centro X en pantalla
    cy: float          # centro Y en pantalla
    duration_ms: float
    n_samples: int


class IVTClassifier:
    """
    Clasificador de fijaciones y sacadas usando Velocity-Threshold Identification.

    Algoritmo:
        Para cada muestra (px, py, t):
          velocity = dist(prev_pt, cur_pt) / Dt   [px/s]
          si velocity < vel_thresh_px_s -> Fijacion
          si velocity >= vel_thresh_px_s -> Sacada

        Una Fijación válida debe durar >= min_duration_ms milisegundos.

    Args:
        vel_thresh_px_s:   Umbral de velocidad en píxeles/segundo.  80 px/s
                           corresponde a ~1° de ángulo visual a 60 cm del monitor.
        min_duration_ms:   Duración mínima de una fijación (ms).
    """

    def __init__(
        self,
        vel_thresh_px_s: float = 80.0,
        min_duration_ms: float = 100.0,
    ) -> None:
        self.vel_thresh = vel_thresh_px_s
        self.min_dur_ms = min_duration_ms

        self._prev_x = None
        self._prev_y = None
        self._prev_t = None
        self._candidate = None

        self.fixations: List[Fixation] = []
        self.current_event: str = "Unknown"  # "Fixation" | "Saccade" | "Unknown"

    def reset(self) -> None:
        self._prev_x = self._prev_y = self._prev_t = None
        self._candidate = None
        self.fixations.clear()
        self.current_event = "Unknown"

    def update(self, px: float, py: float, timestamp_s: float) -> str:
        """
        Procesa un nuevo punto de mirada.

        Returns:
            "Fixation", "Saccade" o "Unknown".
        """
        if self._prev_t is None:
            self._prev_x, self._prev_y, self._prev_t = px, py, timestamp_s
            self.current_event = "Unknown"
            return self.current_event

        dt = timestamp_s - self._prev_t
        if dt < 1e-6:
            return self.current_event

        dist = math.hypot(px - self._prev_x, py - self._prev_y)
        vel  = dist / dt   # px/s

        if vel < self.vel_thresh:
            event = "Fixation"
            if self._candidate is None:
                self._candidate = _FixationCandidate(start_t=timestamp_s)
            self._candidate.points.append((px, py))
        else:
            event = "Saccade"
            self._close_candidate(timestamp_s)

        self._prev_x, self._prev_y, self._prev_t = px, py, timestamp_s
        self.current_event = event
        return event

    def _close_candidate(self, t_end: float) -> None:
        cand = self._candidate
        if cand is None:
            return
        self._candidate = None
        if not cand.points:
            return
        dur_ms = (t_end - cand.start_t) * 1000.0
        if dur_ms < self.min_dur_ms:
            return
        cx = float(np.mean([p[0] for p in cand.points]))
        cy = float(np.mean([p[1] for p in cand.points]))
        self.fixations.append(Fixation(
            start_ms=cand.start_t * 1000.0,
            end_ms=t_end * 1000.0,
            cx=cx, cy=cy,
            duration_ms=dur_ms,
            n_samples=len(cand.points),
        ))

    def flush(self, current_time_s: float) -> None:
        """Cierra la fijación en curso (llamar al final de la sesión)."""
        self._close_candidate(current_time_s)


# ─────────────────────────────────────────────────────────────────────────────
# I-DT Classifier — Dispersion-Threshold Identification
# ─────────────────────────────────────────────────────────────────────────────

class IDTClassifier:
    """
    Clasificador de fijaciones por umbral de dispersión (I-DT).

    Por qué se prefiere a I-VT con cámaras web
    ------------------------------------------
    I-VT decide según la *velocidad* entre muestras consecutivas.  Con una
    webcam a 15-30 fps y un ruido de estimación de 50-150 px, la velocidad
    aparente entre dos muestras durante una fijación real supera fácilmente
    los 1000 px/s, así que I-VT etiqueta casi todo como sacada (medido: 772
    de 781 muestras).  El resultado es que no hay fijaciones que dibujar.

    I-DT decide según la *dispersión* de un grupo de muestras consecutivas:
    mientras los puntos se mantengan dentro de un radio, siguen siendo la
    misma fijación.  Es mucho más robusto al ruido, que es exactamente el
    problema de un eye-tracker de webcam.

    Algoritmo:
        dispersion = 2 · (σx + σy)                        [px]
        si dispersion <= umbral  -> la ventana sigue siendo fijación
        si dispersion >  umbral  -> se cierra la ventana; si duró al menos
                                    ``min_duration_ms`` se registra como
                                    fijación, si no se descarta

    Por qué desviación estándar y no rango
    --------------------------------------
    La formulación clásica de I-DT usa el rango (``max - min``), pero con
    ruido de webcam un único píxel desviado rompe la ventana y la fijación
    se pierde.  Medido sobre señal sintética con 6 fijaciones conocidas y
    60 px de ruido:

        métrica   sin filtrar      con filtro 1€
        rango     1/6  (th=120)    4/6  (th=120)
        sigma     6/6  (th=200)    6/6  (th=120)

    Con la desviación estándar el clasificador recupera las 6 fijaciones
    incluso sin filtrar.  Por eso es la métrica por defecto.

    Args:
        dispersion_px:   dispersión máxima (2·(σx+σy)) para considerar
                         fijación.  200 px ≈ 5° de ángulo visual a 60 cm de
                         un monitor de 1920 px.
        min_duration_ms: duración mínima de una fijación válida.  Por debajo
                         de ~150 ms no es una fijación, es tránsito.
        max_gap_ms:      si entre dos muestras pasa más que esto (pérdida de
                         cara, tirón de la cámara), la ventana se corta.
    """

    def __init__(
        self,
        dispersion_px: float = 200.0,
        min_duration_ms: float = 150.0,
        max_gap_ms: float = 250.0,
        metric: str = "sigma",      # "sigma" (recomendado) | "range"
    ) -> None:
        self.dispersion = dispersion_px
        self.min_dur_ms = min_duration_ms
        self.max_gap_ms = max_gap_ms
        self.metric = metric

        self._xs: List[float] = []
        self._ys: List[float] = []
        self._t_start: Optional[float] = None
        self._t_last: Optional[float] = None

        self.fixations: List[Fixation] = []
        self.current_event: str = "Unknown"

    def reset(self) -> None:
        self._xs.clear()
        self._ys.clear()
        self._t_start = self._t_last = None
        self.fixations.clear()
        self.current_event = "Unknown"

    def _dispersion(self) -> float:
        if len(self._xs) < 2:
            return 0.0
        if self.metric == "range":
            return ((max(self._xs) - min(self._xs)) +
                    (max(self._ys) - min(self._ys)))
        return 2.0 * (float(np.std(self._xs)) + float(np.std(self._ys)))

    def _close_window(self) -> None:
        if self._t_start is None or self._t_last is None or not self._xs:
            self._reset_window()
            return
        dur_ms = (self._t_last - self._t_start) * 1000.0
        if dur_ms >= self.min_dur_ms and len(self._xs) >= 3:
            self.fixations.append(Fixation(
                start_ms=self._t_start * 1000.0,
                end_ms=self._t_last * 1000.0,
                cx=float(np.mean(self._xs)),
                cy=float(np.mean(self._ys)),
                duration_ms=dur_ms,
                n_samples=len(self._xs),
            ))
        self._reset_window()

    def _reset_window(self) -> None:
        self._xs.clear()
        self._ys.clear()
        self._t_start = self._t_last = None

    def update(self, px: float, py: float, timestamp_s: float) -> str:
        """Procesa un punto de mirada. Retorna "Fixation" o "Saccade"."""
        # Corte por hueco temporal (pérdida de cara, cámara congelada)
        if (self._t_last is not None and
                (timestamp_s - self._t_last) * 1000.0 > self.max_gap_ms):
            self._close_window()

        # Primera muestra: abre la ventana
        if self._t_start is None:
            self._t_start = self._t_last = timestamp_s
            self._xs = [px]
            self._ys = [py]
            self.current_event = "Unknown"
            return self.current_event

        # ¿El punto nuevo sigue dentro de la dispersión de la ventana?
        self._xs.append(px)
        self._ys.append(py)
        if self._dispersion() <= self.dispersion:
            self._t_last = timestamp_s
            self.current_event = "Fixation"
            return "Fixation"

        # No: se quita el punto, se cierra la ventana anterior (que termina
        # en la muestra previa, no en ésta) y éste arranca una nueva.
        self._xs.pop()
        self._ys.pop()
        self._close_window()
        self._t_start = self._t_last = timestamp_s
        self._xs = [px]
        self._ys = [py]
        self.current_event = "Saccade"
        return "Saccade"

    def flush(self, current_time_s: Optional[float] = None) -> None:
        """Cierra la fijación en curso (llamar al final de la sesión)."""
        if current_time_s is not None:
            self._t_last = current_time_s
        self._close_window()


# ─────────────────────────────────────────────────────────────────────────────
# TTFF — Time To First Fixation
# ─────────────────────────────────────────────────────────────────────────────

class TTFFTracker:
    """
    Registra el tiempo desde el inicio de un estímulo hasta la primera fijación
    dentro de un AOI configurado.

    Uso::

        tracker = TTFFTracker()
        tracker.start(aoi=AOI(100, 100, 300, 200), stimulus_label="imagen_A")
        # En cada frame:
        tracker.update(gaze_x, gaze_y, timestamp_s, event_type)
        # Al terminar:
        result = tracker.result()   # None si aún no hubo fijación
    """

    def __init__(self) -> None:
        self._aoi = None
        self._start_t = None
        self._label: str = ""
        self._first_fix_t = None
        self._results: List[dict] = []

    def start(self, aoi: AOI, stimulus_label: str = "",
              timestamp_s=None) -> None:
        """Inicia una nueva medición TTFF."""
        self._aoi = aoi
        self._label = stimulus_label
        self._start_t = timestamp_s if timestamp_s is not None else time.perf_counter()
        self._first_fix_t = None

    def update(self, px: float, py: float,
               timestamp_s: float, event_type: str):
        """
        Procesa un punto de mirada.  Retorna TTFF en ms si ya se detectó,
        o None si aún no.
        """
        if (self._aoi is None or self._start_t is None
                or self._first_fix_t is not None):
            return self._ttff_ms()

        if event_type == "Fixation" and self._aoi.contains(px, py):
            self._first_fix_t = timestamp_s
            ms = self._ttff_ms()
            self._results.append({
                "label": self._label,
                "ttff_ms": ms,
                "timestamp_s": timestamp_s,
            })
            return ms
        return None

    def result(self):
        """Retorna el TTFF en ms, o None si aún no se detectó fijación."""
        return self._ttff_ms()

    def _ttff_ms(self):
        if self._first_fix_t is None or self._start_t is None:
            return None
        return (self._first_fix_t - self._start_t) * 1000.0

    def all_results(self) -> List[dict]:
        return list(self._results)