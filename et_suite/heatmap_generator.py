"""
et_suite/heatmap_generator.py  —  Generador de Heatmaps Post-Sesión
====================================================================
Toma el CSV generado por DataLogger y produce dos tipos de heatmap:

  1. Densidad de fijaciones  — acumula puntos de fijación, aplica Gaussian blur
  2. Dwell time (permanencia) — pondera por duración acumulada de fijación

Técnica:
  • scipy.ndimage.gaussian_filter para suavizado Gaussiano 2D
  • cv2.COLORMAP_JET (o cualquier otro de OpenCV) como mapa de color térmico
  • Blend alpha sobre imagen de fondo (screenshot o fondo blanco)
  • Marcadores de fijación opcionales con escaneo de ruta (scanpath)

Uso::

    gen = HeatmapGenerator(screen_w=1920, screen_h=1080)

    # Densidad de fijaciones
    out1 = gen.fixation_density("sessions/session.csv",
                                background="screenshot.png")

    # Dwell time
    out2 = gen.dwell_time("sessions/session.csv",
                          background="screenshot.png")

    # Scanpath
    out3 = gen.scanpath("sessions/session.csv",
                         background="screenshot.png")
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import List, Optional

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter


# ════════════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════════════

# Duración mínima para que un grupo de muestras cuente como fijación.
# Coincide con el umbral del clasificador I-DT: por debajo de ~150 ms el ojo
# está en tránsito, no fijando.
MIN_FIXATION_MS: float = 150.0


# ════════════════════════════════════════════════════════════════════════════
#  Estructuras de datos internas
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class _FixRecord:
    cx: float
    cy: float
    duration_ms: float
    start_ms: float


# ════════════════════════════════════════════════════════════════════════════
#  HeatmapGenerator
# ════════════════════════════════════════════════════════════════════════════

class HeatmapGenerator:
    """
    Genera heatmaps de densidad de fijaciones y dwell time a partir de un CSV.

    Args:
        screen_w / screen_h: resolución de la pantalla en píxeles
        colormap:            cv2.COLORMAP_* (default: COLORMAP_JET)
        sigma_px:            radio del desenfoque Gaussiano en píxeles
        alpha:               transparencia del heatmap sobre el fondo (0–1)
    """

    def __init__(
        self,
        screen_w:  int   = 1920,
        screen_h:  int   = 1080,
        colormap:  int   = cv2.COLORMAP_JET,
        sigma_px:  float = 55.0,
        alpha:     float = 0.65,
    ) -> None:
        self.sw       = screen_w
        self.sh       = screen_h
        self.colormap = colormap
        self.sigma    = sigma_px
        self.alpha    = alpha

    # ── API pública ───────────────────────────────────────────────────────

    def fixation_density(
        self,
        csv_path:   str,
        background: Optional[str] = None,
        output:     Optional[str] = None,
        show_markers: bool = False,
    ) -> str:
        """
        Heatmap de densidad de fijaciones.

        Cada punto de fijación contribuye con peso 1.0 al mapa de densidad.
        Ideal para visualizar **dónde** miró el usuario (frecuencia espacial).

        Returns:
            Ruta del PNG generado.
        """
        rows      = self._load(csv_path)
        fix_pts   = self._extract_fixations(rows)
        density   = self._build_density_map(
            [(f.cx, f.cy) for f in fix_pts],
            [1.0] * len(fix_pts)
        )
        bg        = self._load_bg(background)
        heatmap   = self._render(density, bg)

        if show_markers and fix_pts:
            heatmap = self._draw_markers(heatmap, fix_pts)

        self._draw_legend(
            heatmap,
            f"DENSIDAD DE FIJACIONES  ·  {len(fix_pts)} fijaciones",
            ["Cada fijacion suma 1 punto de calor",
             "Rojo/caliente = miro AHI muchas veces",
             "Azul/frio = apenas paso por esa zona"])

        out = output or self._out_path(csv_path, "density")
        cv2.imwrite(out, heatmap)
        print(f"[HeatmapGenerator] Densidad de fijaciones -> {out}")
        return out

    def dwell_time(
        self,
        csv_path:   str,
        background: Optional[str] = None,
        output:     Optional[str] = None,
        show_markers: bool = False,
    ) -> str:
        """
        Heatmap de dwell time (tiempo de permanencia acumulado).

        Cada fijacion contribuye con peso proporcional a su duracion.
        Ideal para visualizar cuanto tiempo se miro cada region.

        Returns:
            Ruta del PNG generado.
        """
        rows    = self._load(csv_path)
        fix_pts = self._extract_fixations(rows)
        density = self._build_density_map(
            [(f.cx, f.cy) for f in fix_pts],
            [f.duration_ms for f in fix_pts]
        )
        bg      = self._load_bg(background)
        heatmap = self._render(density, bg)

        if show_markers and fix_pts:
            heatmap = self._draw_markers(heatmap, fix_pts,
                                          scale_by_duration=True)

        total_s = sum(f.duration_ms for f in fix_pts) / 1000.0
        self._draw_legend(
            heatmap,
            f"DWELL TIME  ·  {len(fix_pts)} fijaciones  ·  {total_s:.1f} s",
            ["Cada fijacion pesa segun cuanto duro",
             "Rojo/caliente = miro AHI durante MAS tiempo",
             "El circulo blanco marca cada fijacion"])

        out = output or self._out_path(csv_path, "dwell")
        cv2.imwrite(out, heatmap)
        print(f"[HeatmapGenerator] Dwell time -> {out}")
        return out

    def scanpath(
        self,
        csv_path:   str,
        background: Optional[str] = None,
        output:     Optional[str] = None,
        max_fixations: int = 30,
    ) -> str:
        """
        Scanpath: secuencia de fijaciones numeradas unidas por flechas.

        Muestra el orden temporal de exploración visual: la fijación 1 es lo
        PRIMERO que miró, la 2 lo siguiente, y así.

        El tamaño de cada círculo es proporcional a la duración de la
        fijación (más grande = miró más tiempo ahí).

        Args:
            max_fixations: cuántas fijaciones dibujar.  Por encima de ~30 el
                           gráfico se vuelve una maraña ilegible.

        Returns:
            Ruta del PNG generado.
        """
        rows    = self._load(csv_path)
        fix_pts = self._extract_fixations(rows)[:max_fixations]
        bg      = self._load_bg(background)

        canvas = bg.copy() if bg is not None else np.ones(
            (self.sh, self.sw, 3), dtype=np.uint8) * 255

        colors = [
            (0, 180, 255),
            (0, 220, 100),
            (255, 100, 0),
        ]

        for i, fix in enumerate(fix_pts):
            cx, cy = int(fix.cx), int(fix.cy)
            r = max(10, min(40, int(fix.duration_ms / 20)))
            color = colors[i % len(colors)]

            if i < len(fix_pts) - 1:
                nx, ny = int(fix_pts[i+1].cx), int(fix_pts[i+1].cy)
                cv2.arrowedLine(canvas, (cx, cy), (nx, ny),
                                (180, 180, 180), 2, tipLength=0.15)

            cv2.circle(canvas, (cx, cy), r, color, 2)
            cv2.circle(canvas, (cx, cy), 4, color, -1)

            cv2.putText(canvas, str(i+1),
                        (cx + r + 4, cy + 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (50, 50, 50), 2)

        # Marcar explícitamente el primer punto mirado
        if fix_pts:
            cx, cy = int(fix_pts[0].cx), int(fix_pts[0].cy)
            cv2.circle(canvas, (cx, cy), 46, (0, 0, 220), 3)

            txt = "1 = LO PRIMERO QUE MIRO"
            (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX,
                                          0.6, 2)
            # Mantener el texto dentro del lienzo: si la primera fijación cae
            # en una esquina, la etiqueta se salía de la imagen.
            tx = int(np.clip(cx - tw // 2, 8, max(8, self.sw - tw - 8)))
            ty = int(np.clip(cy - 56, th + 10, max(th + 10, self.sh - 10)))
            cv2.putText(canvas, txt, (tx, ty),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 220), 2)

        self._draw_legend(
            canvas,
            f"SCANPATH  ·  {len(fix_pts)} fijaciones  ·  "
            f"{os.path.basename(csv_path)}",
            ["El numero indica el ORDEN de la mirada (1 = primero)",
             "El tamano del circulo = cuanto tiempo fijo la vista ahi",
             "Las flechas unen fijaciones consecutivas (sacadas)"])

        out = output or self._out_path(csv_path, "scanpath")
        cv2.imwrite(out, canvas)
        print(f"[HeatmapGenerator] Scanpath -> {out}")
        return out

    # ── Leyenda ──────────────────────────────────────────────────────────

    @staticmethod
    def _draw_legend(canvas: np.ndarray, title: str,
                     lines: List[str]) -> None:
        """
        Dibuja un recuadro blanco con el título y la explicación.

        Se pinta sobre fondo blanco con texto oscuro para que sea legible
        tanto si el fondo del heatmap es blanco como si es una captura de
        pantalla oscura.
        """
        pad   = 12
        hline = 24
        alto  = pad * 2 + hline * (len(lines) + 1)
        ancho = 520
        x0, y0 = 14, 14

        # Recuadro blanco con borde
        cv2.rectangle(canvas, (x0, y0), (x0 + ancho, y0 + alto),
                      (255, 255, 255), -1)
        cv2.rectangle(canvas, (x0, y0), (x0 + ancho, y0 + alto),
                      (60, 60, 60), 2)

        cv2.putText(canvas, title[:60], (x0 + pad, y0 + pad + 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, (20, 20, 20), 2)
        for k, ln in enumerate(lines):
            cv2.putText(canvas, ln, (x0 + pad, y0 + pad + hline * (k + 1) + 15),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.44, (70, 70, 70), 1)

    def all_three(
        self,
        csv_path:   str,
        background: Optional[str] = None,
    ) -> dict:
        """
        Genera los tres tipos de visualización en un solo llamado.

        Returns:
            dict con claves "density", "dwell", "scanpath".
        """
        return {
            "density":  self.fixation_density(csv_path, background, show_markers=True),
            "dwell":    self.dwell_time(csv_path, background, show_markers=True),
            "scanpath": self.scanpath(csv_path, background),
        }

    # ── Internos ──────────────────────────────────────────────────────────

    def _load(self, csv_path: str) -> List[dict]:
        rows = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    rows.append({
                        "Timestamp_ms": float(row["Timestamp_ms"]),
                        "Gaze_X":       float(row["Gaze_X"]),
                        "Gaze_Y":       float(row["Gaze_Y"]),
                        "Event_Type":   row["Event_Type"],
                        "Face_Detected": int(row.get("Face_Detected", 1)),
                    })
                except (ValueError, KeyError):
                    continue
        return rows

    def _extract_fixations(self, rows: List[dict]) -> List[_FixRecord]:
        """
        Agrupa filas consecutivas de tipo 'Fixation' en registros de fijación.

        Sólo se conservan las fijaciones de al menos ``MIN_FIXATION_MS``.  El
        umbral coincide con el del clasificador I-DT: por debajo de ~150 ms no
        hay fijación, hay tránsito.  Sin este filtro un barrido ruidoso genera
        decenas de "fijaciones" de 1-2 muestras que convierten el scanpath en
        una maraña de líneas.
        """
        fixations: List[_FixRecord] = []
        cur_pts:   List[tuple]       = []
        cur_start: Optional[float]   = None

        for i, r in enumerate(rows):
            is_fix = (r["Event_Type"] == "Fixation" and r["Face_Detected"])
            if is_fix:
                if cur_start is None:
                    cur_start = r["Timestamp_ms"]
                cur_pts.append((r["Gaze_X"], r["Gaze_Y"]))
            else:
                if cur_pts and cur_start is not None:
                    dur = r["Timestamp_ms"] - cur_start
                    if dur >= MIN_FIXATION_MS:
                        cx = float(np.mean([p[0] for p in cur_pts]))
                        cy = float(np.mean([p[1] for p in cur_pts]))
                        fixations.append(_FixRecord(cx, cy, dur, cur_start))
                cur_pts   = []
                cur_start = None

        # Cerrar último grupo
        if cur_pts and cur_start is not None and len(rows) > 0:
            last_t = rows[-1]["Timestamp_ms"]
            dur    = last_t - cur_start
            if dur >= MIN_FIXATION_MS:
                cx = float(np.mean([p[0] for p in cur_pts]))
                cy = float(np.mean([p[1] for p in cur_pts]))
                fixations.append(_FixRecord(cx, cy, dur, cur_start))

        return fixations

    def _build_density_map(
        self, pts: List[tuple], weights: List[float]
    ) -> np.ndarray:
        """Construye y suaviza el mapa de densidad 2D."""
        density = np.zeros((self.sh, self.sw), dtype=np.float32)
        for (px, py), w in zip(pts, weights):
            xi = int(np.clip(px, 0, self.sw - 1))
            yi = int(np.clip(py, 0, self.sh - 1))
            density[yi, xi] += float(w)

        # Suavizado Gaussiano
        density = gaussian_filter(density, sigma=self.sigma)

        # Normalizar a [0, 1] usando un percentil alto en vez del máximo.
        # Si se normaliza por el máximo, un único punto muy caliente aplasta
        # toda la escala y el resto del mapa queda uniformemente azul.  Con
        # el percentil 99 la escala se reparte donde está la información.
        nz = density[density > 0]
        if nz.size:
            ref = float(np.percentile(nz, 99.0))
            if ref <= 0:
                ref = float(nz.max())
            density = np.clip(density / ref, 0.0, 1.0)
        return density

    def _render(
        self, density: np.ndarray, bg: Optional[np.ndarray]
    ) -> np.ndarray:
        """Aplica colormap y blend sobre el fondo."""
        # Mapa de color (uint8)
        density_u8 = (density * 255).astype(np.uint8)
        colored    = cv2.applyColorMap(density_u8, self.colormap)

        if bg is None:
            bg = np.ones((self.sh, self.sw, 3), dtype=np.uint8) * 255

        # Máscara: no mezclar zonas con densidad baja.  El umbral es alto a
        # propósito — el desenfoque gaussiano reparte un poco de densidad por
        # toda la pantalla, y con un umbral bajo el heatmap se convierte en
        # una mancha que cubre todo y no comunica nada.
        mask = (density > 0.20).astype(np.float32)
        mask = np.stack([mask, mask, mask], axis=-1)

        # Alpha blend: solo donde hay datos
        result = bg.copy().astype(np.float32)
        alpha  = self.alpha * mask
        result = result * (1. - alpha) + colored.astype(np.float32) * alpha
        return result.clip(0, 255).astype(np.uint8)

    def _load_bg(self, path: Optional[str]) -> Optional[np.ndarray]:
        if path and os.path.exists(path):
            img = cv2.imread(path)
            if img is not None:
                return cv2.resize(img, (self.sw, self.sh))
        return None

    def _draw_markers(
        self, img: np.ndarray, fixations: List[_FixRecord],
        scale_by_duration: bool = False
    ) -> np.ndarray:
        out = img.copy()
        for i, f in enumerate(fixations):
            cx, cy = int(f.cx), int(f.cy)
            if scale_by_duration:
                r = max(8, min(35, int(f.duration_ms / 25)))
            else:
                r = 10
            cv2.circle(out, (cx, cy), r, (255, 255, 255), 1)
            cv2.circle(out, (cx, cy), 3, (255, 255, 255), -1)
        return out

    @staticmethod
    def _out_path(csv_path: str, suffix: str) -> str:
        base = os.path.splitext(csv_path)[0]
        return f"{base}_{suffix}.png"
