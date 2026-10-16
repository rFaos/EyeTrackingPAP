"""
recorder.py  ─  Grabación continua de datos de gaze + análisis
==========================================================================
• Graba en background (no bloquea la UI).
• CSV con columnas: timestamp, screen_x, screen_y, eye_dx, eye_dy,
                    yaw_deg, pitch_deg, pupil_radius_px, face_detected
• Calcula frecuencia de muestreo, fijaciones, tiempo hasta primera fijación.
• Genera un heatmap de densidad Gaussiana sobre un screenshot de la pantalla
  o sobre un fondo blanco si no hay screenshot disponible.
"""

import csv
import time
import os
import math
import threading
import datetime
from typing import Optional, List
from dataclasses import asdict

import numpy as np
from PIL import Image, ImageDraw

from gaze_engine import GazeEngine, GazePoint, SCREEN_W, SCREEN_H


# ════════════════════════════════════════════════════════
#  DETECTOR DE FIJACIONES  (I-VT: Velocity Threshold)
# ════════════════════════════════════════════════════════

class FixationDetector:
    """
    Classifica cada GazePoint como FIJACIÓN o SACADA usando el umbral
    de velocidad (Velocity-Threshold, I-VT).

    Parámetros sintonizados para distancia monitor ~50-70 cm:
      vel_thresh   px/s  → umbral de velocidad para definir fijación
      min_duration  s    → duración mínima de una fijación
    """
    def __init__(self, vel_thresh: float = 80.0, min_duration: float = 0.10):
        self.vel_thresh = vel_thresh
        self.min_dur = min_duration

        self._last: Optional[GazePoint] = None
        self._fx_start: Optional[float] = None
        self._fx_pts: List[GazePoint] = []
        self.fixations: List[dict] = []   # lista de fijaciones completas

    def update(self, gp: GazePoint) -> bool:
        """Procesa un nuevo punto. Retorna True si es una fijación."""
        if not gp.face_detected:
            self._end_fixation()
            self._last = gp
            return False

        if self._last is None:
            self._last = gp
            return False

        dt = gp.timestamp - self._last.timestamp
        if dt < 1e-5:
            return False

        dx = gp.x - self._last.x
        dy = gp.y - self._last.y
        vel = math.hypot(dx, dy) / dt

        is_fix = vel < self.vel_thresh

        if is_fix:
            if self._fx_start is None:
                self._fx_start = gp.timestamp
                self._fx_pts = [self._last]
            self._fx_pts.append(gp)
        else:
            self._end_fixation()

        self._last = gp
        return is_fix

    def _end_fixation(self):
        if self._fx_pts and self._fx_start is not None:
            dur = (self._fx_pts[-1].timestamp - self._fx_start)
            if dur >= self.min_dur:
                cx = float(np.mean([p.x for p in self._fx_pts]))
                cy = float(np.mean([p.y for p in self._fx_pts]))
                self.fixations.append({
                    'start': self._fx_start,
                    'duration': dur,
                    'cx': cx, 'cy': cy,
                    'n_samples': len(self._fx_pts)
                })
        self._fx_start = None
        self._fx_pts = []


# ════════════════════════════════════════════════════════
#  GRABADOR
# ════════════════════════════════════════════════════════

class GazeRecorder:
    """
    Graba GazePoints en un archivo CSV y computa estadísticas.

    Uso típico:
        rec = GazeRecorder(engine, output_dir="sessions")
        rec.start()
        # ... el usuario trabaja ...
        path = rec.stop()   # devuelve ruta del CSV
        stats = rec.get_stats()
    """
    CSV_FIELDS = [
        "timestamp", "elapsed",
        "screen_x", "screen_y",
        "eye_dx", "eye_dy",
        "yaw_deg", "pitch_deg",
        "pupil_radius_px",
        "face_detected",
        "is_fixation",
    ]

    def __init__(self, engine: GazeEngine, output_dir: str = "sessions"):
        self.engine = engine
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._rows: List[dict] = []
        self._start_time: Optional[float] = None
        self._csv_path: Optional[str] = None
        self._fix_det = FixationDetector()

    # ─── Interfaz pública ────────────────────────────────

    def start(self, session_name: str = ""):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{session_name}_{ts}" if session_name else ts
        self._csv_path = os.path.join(self.output_dir, f"{name}.csv")
        self._rows.clear()
        self._fix_det = FixationDetector()
        self._start_time = time.perf_counter()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="Recorder")
        self._thread.start()
        print(f"[Recorder] Grabando → {self._csv_path}")

    def stop(self) -> str:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        self._flush_csv()
        print(f"[Recorder] Sesión guardada: {self._csv_path}  ({len(self._rows)} muestras)")
        return self._csv_path

    def get_stats(self) -> dict:
        if not self._rows:
            return {}
        duration = self._rows[-1]['elapsed']
        n = len(self._rows)
        sample_rate = n / max(duration, 1e-4)
        face_ok = sum(1 for r in self._rows if r['face_detected'])
        fix_all = self._fix_det.fixations
        first_fix_t = fix_all[0]['start'] - self._start_time if fix_all else None

        return {
            "n_samples": n,
            "duration_s": duration,
            "sample_rate_hz": sample_rate,
            "face_detected_pct": face_ok / max(n, 1) * 100,
            "n_fixations": len(fix_all),
            "mean_fix_duration_ms": (
                float(np.mean([f['duration'] for f in fix_all])) * 1000
                if fix_all else 0
            ),
            "time_to_first_fix_ms": (first_fix_t * 1000 if first_fix_t else None),
            "csv_path": self._csv_path,
        }

    # ─── Loop interno ────────────────────────────────────

    def _loop(self):
        POLL_HZ = 60
        interval = 1.0 / POLL_HZ

        while self._running:
            t0 = time.perf_counter()
            gp = self.engine.get_latest_gaze()
            if gp and self._start_time:
                is_fix = self._fix_det.update(gp)
                self._rows.append({
                    "timestamp": gp.timestamp,
                    "elapsed": gp.timestamp - self._start_time,
                    "screen_x": gp.x,
                    "screen_y": gp.y,
                    "eye_dx": round(gp.eye_dx, 5),
                    "eye_dy": round(gp.eye_dy, 5),
                    "yaw_deg": round(gp.yaw_deg, 2),
                    "pitch_deg": round(gp.pitch_deg, 2),
                    "pupil_radius_px": round(gp.pupil_radius_px, 2),
                    "face_detected": int(gp.face_detected),
                    "is_fixation": int(is_fix),
                })
            elapsed = time.perf_counter() - t0
            sleep_t = interval - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    def _flush_csv(self):
        if not self._csv_path or not self._rows:
            return
        with open(self._csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.CSV_FIELDS)
            w.writeheader()
            w.writerows(self._rows)


# ════════════════════════════════════════════════════════
#  GENERADOR DE HEATMAP
# ════════════════════════════════════════════════════════

class HeatmapGenerator:
    """
    Genera un heatmap de densidad Gaussiana a partir de un CSV de sesión.

    Modos:
      - 'fixations': solo puntos clasificados como fijaciones
      - 'all':       todos los puntos de gaze
      - 'duration':  pondera por duración de fijación (amplifica puntos de más tiempo)

    Retorna la ruta del PNG generado.
    """

    def __init__(self, screen_w: int = SCREEN_W, screen_h: int = SCREEN_H):
        self.sw = screen_w
        self.sh = screen_h

    def generate(self,
                 csv_path: str,
                 background_path: Optional[str] = None,
                 mode: str = "fixations",
                 sigma_px: float = 60.0,
                 colormap: str = "jet",
                 alpha: float = 0.65,
                 output_path: Optional[str] = None) -> str:
        """
        Genera el heatmap y lo guarda como PNG.

        Args:
            csv_path:        Ruta al CSV de sesión.
            background_path: Imagen de fondo (screenshot opcional).
            mode:            'fixations', 'all' o 'duration'.
            sigma_px:        Radio Gaussiano en píxeles.
            colormap:        Nombre de mapa de color matplotlib.
            alpha:           Opacidad del heatmap sobre el fondo (0-1).
            output_path:     Si None, guarda junto al CSV.

        Returns:
            Ruta del PNG generado.
        """
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from scipy.ndimage import gaussian_filter

        # ── Leer CSV ──────────────────────────────────────
        rows = self._load_csv(csv_path)
        if not rows:
            raise ValueError("CSV vacío o sin datos válidos")

        # ── Filtrar puntos ────────────────────────────────
        pts, weights = self._filter_points(rows, mode)
        if not pts:
            raise ValueError(f"No hay puntos en modo '{mode}'")

        # ── Mapa de densidad ──────────────────────────────
        density = np.zeros((self.sh, self.sw), dtype=np.float32)
        for (px, py), w in zip(pts, weights):
            xi = int(np.clip(px, 0, self.sw - 1))
            yi = int(np.clip(py, 0, self.sh - 1))
            density[yi, xi] += w

        # Suavizado Gaussiano
        density = gaussian_filter(density, sigma=sigma_px)
        if density.max() > 0:
            density = density / density.max()

        # ── Renderizar ────────────────────────────────────
        dpi = 96
        figw = self.sw / dpi
        figh = self.sh / dpi
        fig, ax = plt.subplots(figsize=(figw, figh), dpi=dpi)
        ax.set_position([0, 0, 1, 1])

        # Fondo
        if background_path and os.path.exists(background_path):
            bg = Image.open(background_path).resize((self.sw, self.sh))
            ax.imshow(np.array(bg), aspect='auto')
        else:
            ax.set_facecolor("white")
            ax.set_xlim(0, self.sw)
            ax.set_ylim(self.sh, 0)

        # Heatmap
        cmap_rgba = plt.get_cmap(colormap)
        heatmap_rgba = cmap_rgba(density)
        heatmap_rgba[..., 3] = density * alpha  # canal alpha proporcional a densidad

        ax.imshow(heatmap_rgba, extent=[0, self.sw, self.sh, 0],
                  aspect='auto', interpolation='bilinear')

        # Marcadores de fijación (solo en modo 'fixations' o 'duration')
        if mode in ("fixations", "duration"):
            fix_pts = [(r['screen_x'], r['screen_y'])
                       for r in rows if r.get('is_fixation', 0) and r.get('face_detected', 0)]
            if fix_pts:
                fx_arr = np.array(fix_pts)
                ax.scatter(fx_arr[:, 0], fx_arr[:, 1],
                           s=8, c="white", alpha=0.25, linewidths=0)

        ax.axis("off")

        # ── Estadísticas en el título ─────────────────────
        n_fix = sum(1 for r in rows if r.get('is_fixation', 0))
        duration = rows[-1]['elapsed'] if rows else 0
        n_pts_lbl = f"{len(pts)} puntos"
        title = f"Heatmap · {n_pts_lbl} · {n_fix} fijaciones · {duration:.1f}s sesión"
        ax.set_title(title, fontsize=11, color="#333333",
                     pad=4, loc="center")

        # ── Guardar ───────────────────────────────────────
        if output_path is None:
            base = os.path.splitext(csv_path)[0]
            output_path = f"{base}_heatmap.png"

        fig.savefig(output_path, bbox_inches="tight", pad_inches=0,
                    facecolor="white", dpi=dpi)
        plt.close(fig)
        print(f"[Heatmap] Guardado: {output_path}")
        return output_path

    # ─── Helpers ─────────────────────────────────────────

    @staticmethod
    def _load_csv(path: str) -> List[dict]:
        rows = []
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    rows.append({
                        'screen_x':       float(row['screen_x']),
                        'screen_y':       float(row['screen_y']),
                        'elapsed':        float(row['elapsed']),
                        'face_detected':  int(row['face_detected']),
                        'is_fixation':    int(row['is_fixation']),
                        'pupil_radius_px': float(row.get('pupil_radius_px', 0)),
                    })
                except (ValueError, KeyError):
                    continue
        return rows

    @staticmethod
    def _filter_points(rows: List[dict], mode: str):
        pts = []; weights = []
        if mode == "all":
            for r in rows:
                if r['face_detected']:
                    pts.append((r['screen_x'], r['screen_y']))
                    weights.append(1.0)
        elif mode == "fixations":
            for r in rows:
                if r['face_detected'] and r['is_fixation']:
                    pts.append((r['screen_x'], r['screen_y']))
                    weights.append(1.0)
        elif mode == "duration":
            # Agrupa fijaciones consecutivas y pondera por duración
            last_fix = None; cur_start = None; cur_pts = []
            for r in rows:
                if r['is_fixation'] and r['face_detected']:
                    if cur_start is None:
                        cur_start = r['elapsed']
                    cur_pts.append((r['screen_x'], r['screen_y']))
                else:
                    if cur_pts and cur_start is not None:
                        dur = r['elapsed'] - cur_start
                        cx = float(np.mean([p[0] for p in cur_pts]))
                        cy = float(np.mean([p[1] for p in cur_pts]))
                        pts.append((cx, cy))
                        weights.append(max(dur, 0.01) * 1000)  # escalar a ms
                    cur_pts = []; cur_start = None
        return pts, weights
