"""
et_suite/report.py  —  Informe de sesión: orden de fijación y zonas de atención
================================================================================
Responde a la pregunta del experimento:

    "¿Qué mira primero el ojo al ver una página?"

A partir del CSV de una sesión calcula y escribe un informe legible:

  • Orden de fijación — las primeras N fijaciones con su instante, posición,
    duración y zona de pantalla.
  • TTFF (Time To First Fixation) — cuánto tardó en fijar la mirada.
  • Mapa de atención 3×3 — porcentaje de tiempo de fijación por zona,
    dibujado como cuadrícula ASCII, más el ranking de zonas.
  • Métricas globales — fijaciones, sacadas, duración media, % de cara.

Uso::

    from et_suite.report import session_report
    txt = session_report("sessions/ana_20260917.csv", 1920, 1080)
    print(txt)
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import numpy as np

from .heatmap_generator import HeatmapGenerator

_COLS = ["izquierda", "centro", "derecha"]
_ROWS = ["arriba", "medio", "abajo"]


def _zone_name(cx: float, cy: float, sw: int, sh: int) -> str:
    cxi = min(2, max(0, int(cx / max(sw, 1) * 3)))
    cyi = min(2, max(0, int(cy / max(sh, 1) * 3)))
    return f"{_ROWS[cyi]}-{_COLS[cxi]}"


def _bar(frac: float, width: int = 24) -> str:
    n = int(round(max(0.0, min(1.0, frac)) * width))
    return "█" * n + "·" * (width - n)


def session_report(
    csv_path: str,
    screen_w: int = 1920,
    screen_h: int = 1080,
    n_first: int = 10,
    output: Optional[str] = None,
) -> str:
    """
    Genera el informe de la sesión.

    Returns:
        El texto del informe (y además lo guarda en un .txt junto al CSV
        salvo que ``output`` indique otra ruta).
    """
    gen  = HeatmapGenerator(screen_w, screen_h)
    rows = gen._load(csv_path)                      # noqa: SLF001 (mismo paquete)
    if not rows:
        txt = f"No hay datos utilizables en {csv_path}"
        _write(txt, csv_path, output)
        return txt

    fixations = gen._extract_fixations(rows)        # noqa: SLF001

    n_rows      = len(rows)
    face_rows   = [r for r in rows if r["Face_Detected"]]
    t0          = rows[0]["Timestamp_ms"]
    t_end       = rows[-1]["Timestamp_ms"]
    duration_ms = max(t_end - t0, 1e-6)
    hz          = n_rows / (duration_ms / 1000.0)

    ev: Dict[str, int] = {}
    for r in rows:
        ev[r["Event_Type"]] = ev.get(r["Event_Type"], 0) + 1

    total_fix_ms = sum(f.duration_ms for f in fixations) or 1e-6

    # ── Mapa de atención 3×3 ────────────────────────────────────────────
    grid_ms = np.zeros((3, 3), dtype=float)
    grid_n  = np.zeros((3, 3), dtype=int)
    for f in fixations:
        cxi = min(2, max(0, int(f.cx / max(screen_w, 1) * 3)))
        cyi = min(2, max(0, int(f.cy / max(screen_h, 1) * 3)))
        grid_ms[cyi, cxi] += f.duration_ms
        grid_n[cyi, cxi]  += 1

    L: List[str] = []
    L.append("=" * 74)
    L.append("  INFORME DE SESIÓN — EYE TRACKING")
    L.append(f"  Archivo: {os.path.basename(csv_path)}")
    L.append(f"  Pantalla: {screen_w}×{screen_h} px")
    L.append("=" * 74)
    L.append("")
    L.append("── 1. CALIDAD DE LA GRABACIÓN " + "─" * 44)
    L.append(f"  Duración                : {duration_ms/1000:8.2f} s")
    L.append(f"  Muestras                : {n_rows:8d}")
    L.append(f"  Frecuencia media        : {hz:8.1f} Hz")
    L.append(f"  Cara detectada          : {100.0*len(face_rows)/max(n_rows,1):7.1f} %")
    L.append("")
    if hz < 15:
        L.append("  ⚠ Frecuencia baja (<15 Hz): los resultados son orientativos.")
    if len(face_rows) / max(n_rows, 1) < 0.8:
        L.append("  ⚠ Menos del 80 % de frames con cara: revisa iluminación")
        L.append("    y encuadre antes de sacar conclusiones.")
    L.append("")

    L.append("── 2. ¿QUÉ MIRÓ PRIMERO? (orden de fijación) " + "─" * 31)
    if not fixations:
        L.append("  No se detectaron fijaciones. La señal no fue estable.")
    else:
        ttff = fixations[0].start_ms - t0
        L.append(f"  TTFF (tiempo hasta la 1ª fijación): {ttff:.0f} ms")
        L.append("")
        L.append(f"  {'#':>3}  {'t (ms)':>9}  {'posición (x, y)':>18}  "
                 f"{'dur (ms)':>9}  zona")
        L.append("  " + "-" * 70)
        for i, f in enumerate(fixations[:n_first], start=1):
            L.append(f"  {i:>3}  {f.start_ms - t0:>9.0f}  "
                     f"{f'({f.cx:.0f}, {f.cy:.0f})':>18}  "
                     f"{f.duration_ms:>9.0f}  "
                     f"{_zone_name(f.cx, f.cy, screen_w, screen_h)}")
        if len(fixations) > n_first:
            L.append(f"  … y {len(fixations) - n_first} fijaciones más "
                     f"(ver scanpath numerado)")
    L.append("")

    L.append("── 3. MAPA DE ATENCIÓN (tiempo de fijación por zona) " + "─" * 23)
    L.append("")
    L.append("               izquierda        centro          derecha")
    for ri, rname in enumerate(_ROWS):
        cells = []
        for ci in range(3):
            pct = 100.0 * grid_ms[ri, ci] / total_fix_ms
            cells.append(f"{pct:5.1f}% ({grid_n[ri, ci]:>3})")
        L.append(f"  {rname:>7}   " + "  ".join(cells))
    L.append("")

    # Ranking de zonas
    flat = [(grid_ms[r, c], grid_n[r, c], r, c) for r in range(3) for c in range(3)]
    flat.sort(reverse=True)
    L.append("  Zonas ordenadas por tiempo de atención:")
    for ms, n, r, c in flat:
        if ms <= 0:
            continue
        frac = ms / total_fix_ms
        L.append(f"    {_ROWS[r]+'-'+_COLS[c]:<18} "
                 f"{frac*100:5.1f}%  {_bar(frac)}  ({n} fijaciones)")
    L.append("")

    L.append("── 4. MÉTRICAS OCULOMÉTRICAS " + "─" * 46)
    n_fix = len(fixations)
    L.append(f"  Fijaciones detectadas   : {n_fix:8d}")
    L.append(f"  Sacadas detectadas      : {ev.get('Saccade', 0):8d}")
    if fixations:
        durs = [f.duration_ms for f in fixations]
        L.append(f"  Duración media fijación : {np.mean(durs):8.0f} ms")
        L.append(f"  Duración mediana        : {np.median(durs):8.0f} ms")
        L.append(f"  Fijación más larga      : {max(durs):8.0f} ms")
        L.append(f"  Tiempo total en fijación: {total_fix_ms/1000:8.2f} s "
                 f"({100.0*total_fix_ms/duration_ms:.1f} % de la sesión)")
    L.append("")
    L.append("=" * 74)

    txt = "\n".join(L)
    _write(txt, csv_path, output)
    return txt


def _write(txt: str, csv_path: str, output: Optional[str]) -> None:
    path = output or f"{os.path.splitext(csv_path)[0]}_informe.txt"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"[report] Informe guardado → {path}")
    except Exception as e:
        print(f"[report] No se pudo guardar el informe: {e}")
