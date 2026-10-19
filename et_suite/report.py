"""
et_suite/report.py  —  Informe individual de una sesión
========================================================
Responde, para UN participante:

    "¿Qué mira primero? ¿Cómo le fue en cada tarea? ¿Qué elementos vio?"

A partir del CSV (y de sus metadatos) calcula y escribe un informe legible:

  1. Calidad de la grabación y veredicto: ¿la sesión es válida?
  2. Resumen por tarea: resultado, tiempo, fijaciones, clics, páginas.
  3. Orden de fijación por tarea (con el AOI en que cayó cada una) y TTFF.
  4. Áreas de interés: ¿lo vio?, TTFF, dwell, fijaciones, visitas.
  5. Mapa de atención 3×3 de la sesión.
  6. Métricas oculométricas globales.

Uso::

    from et_suite.report import session_report
    txt = session_report("sessions/estudio/P01_20261003_101500.csv")
    print(txt)
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

import numpy as np

from .metrics import Analysis, analyze_csv
from .study import Study, StudyError, load_study

_COLS = ["izquierda", "centro", "derecha"]
_ROWS = ["arriba", "medio", "abajo"]

STUDIES_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "studies"))


def _zone_name(cx: float, cy: float, sw: int, sh: int) -> str:
    cxi = min(2, max(0, int(cx / max(sw, 1) * 3)))
    cyi = min(2, max(0, int(cy / max(sh, 1) * 3)))
    return f"{_ROWS[cyi]}-{_COLS[cxi]}"


def _bar(frac: float, width: int = 24) -> str:
    n = int(round(max(0.0, min(1.0, frac)) * width))
    return "█" * n + "·" * (width - n)


def _fmt(v: Optional[float], fmt: str = "{:.0f}", none: str = "—") -> str:
    return none if v is None else fmt.format(v)


def find_study(study_id: Optional[str],
               directory: str = STUDIES_DIR) -> Optional[Study]:
    """Busca en ``studies/`` el estudio con ese id (None si no está)."""
    if not study_id or not os.path.isdir(directory):
        return None
    for fn in sorted(os.listdir(directory)):
        if fn.endswith(".json") and not fn.startswith("_"):
            try:
                st = load_study(os.path.join(directory, fn))
            except StudyError:
                continue
            if st.id == study_id:
                return st
    return None


def session_report(
    csv_path: str,
    screen_w: Optional[int] = None,
    screen_h: Optional[int] = None,
    n_first: int = 10,
    output: Optional[str] = None,
    study: Optional[Study] = None,
    analysis: Optional[Analysis] = None,
) -> str:
    """
    Genera el informe de la sesión.

    Returns:
        El texto del informe (y además lo guarda en un .txt junto al CSV
        salvo que ``output`` indique otra ruta).
    """
    if analysis is None:
        # El estudio se deduce de los metadatos si no se pasa explícito
        if study is None:
            from .session_meta import load_meta
            meta = load_meta(csv_path) or {}
            study = find_study(meta.get("study_id"))
        analysis = analyze_csv(csv_path, study, screen_w, screen_h)

    session = analysis.session
    if not session.rows:
        txt = f"No hay datos utilizables en {csv_path}"
        _write(txt, csv_path, output)
        return txt

    txt = _build(analysis, n_first)
    _write(txt, csv_path, output)
    return txt


# ════════════════════════════════════════════════════════════════════════════

def _build(an: Analysis, n_first: int) -> str:
    s = an.session
    m = an.metrics
    sm = m["session"]
    q = m["quality"]
    sw, sh = s.screen_w, s.screen_h
    L: List[str] = []
    rule = "=" * 74

    L += [rule, "  INFORME DE SESIÓN — EYE TRACKING"]
    if s.meta:
        L.append(f"  Estudio     : {s.meta.get('study_name', '—')}")
        L.append(f"  Participante: {sm['participant']}"
                 + (f"   ·   Grupo: {sm['group']}" if sm["group"] else ""))
    L.append(f"  Archivo     : {os.path.basename(s.csv_path)}")
    L.append(f"  Pantalla    : {sw}×{sh} px")
    L += [rule, ""]

    # ── 1. Calidad ───────────────────────────────────────────────────────
    L.append("── 1. CALIDAD DE LA GRABACIÓN " + "─" * 44)
    if q["valid"]:
        L.append("  ✔ SESIÓN VÁLIDA para el análisis")
    else:
        L.append("  ✘ SESIÓN NO VÁLIDA — se recomienda excluirla:")
        for r in q["reasons"]:
            L.append(f"      · {r}")
    for w in q["warnings"]:
        L.append(f"  ⚠ {w}")
    L.append("")
    L.append(f"  Duración                : {sm['duration_s']:8.2f} s")
    L.append(f"  Muestras                : {sm['n_samples']:8d}")
    L.append(f"  Frecuencia media        : {sm['mean_hz']:8.1f} Hz")
    L.append(f"  Cara detectada          : {sm['face_pct']:7.1f} %")
    L.append(f"  Parpadeos               : {sm['n_blinks']:8d}"
             f"   ({sm['blink_rate_per_min']:.1f} por minuto)")
    if sm.get("cal_rms_px") is not None:
        L.append(f"  Error de calibración    : {sm['cal_rms_px']:7.0f} px"
                 f"   (≈ {_fmt(sm.get('cal_rms_deg'), '{:.1f}')}° de ángulo visual)")
    if sm.get("val_pre_px") is not None:
        L.append(f"  Validación inicial      : {sm['val_pre_px']:7.0f} px"
                 f"   (≈ {_fmt(sm.get('val_pre_deg'), '{:.1f}')}°)")
    if sm.get("val_post_px") is not None:
        L.append(f"  Validación final        : {sm['val_post_px']:7.0f} px"
                 f"   (≈ {_fmt(sm.get('val_post_deg'), '{:.1f}')}°)")
    if sm["panel_fix_ms"] > 0:
        L.append(f"  Tiempo en el panel de tareas (excluido del análisis): "
                 f"{sm['panel_fix_ms'] / 1000:.1f} s")
    cc = sm.get("click_correction") or {}
    if cc.get("applied"):
        L.append(f"  Corrección por clics    : mirada desplazada "
                 f"({cc['dx']:+.0f}, {cc['dy']:+.0f}) px; error en los clics "
                 f"{cc['err_before_px']:.0f} → {cc['err_after_px']:.0f} px "
                 f"({cc['n']} clics)")
    elif cc.get("n"):
        L.append(f"  Corrección por clics    : no aplicada ({cc['n']} clics; "
                 "no mejoraba o eran pocos)")
    if sm["n_web_events"]:
        L.append(f"  Eventos del navegador   : {sm['n_web_events']:8d}"
                 "   (scroll, clics, páginas)")
    L.append("")

    tasks = m["tasks"]
    named = m["has_tasks"]

    # ── 2. Resumen por tarea ─────────────────────────────────────────────
    if named:
        L.append("── 2. RESUMEN POR TAREA " + "─" * 50)
        L.append(f"  {'#':>2}  {'resultado':<15}{'tiempo':>8}{'fijac.':>8}"
                 f"{'sacad.':>8}{'clics':>7}{'págs':>6}{'1ª AOI objetivo':>17}")
        L.append("  " + "-" * 71)
        for t in tasks:
            tt = ("no vista" if t["target_aoi"] and t["target_seen"] is False
                  else _fmt(t["target_ttff_ms"], "{:.0f} ms"))
            if not t["target_aoi"]:
                tt = "n/a"
            L.append(f"  {t['task']:>2}  {t['result_label']:<15}"
                     f"{t['time_on_task_s']:>7.1f}s{t['n_fixations']:>8d}"
                     f"{t['n_saccades']:>8d}"
                     f"{_fmt(t['n_clicks'], '{:d}'):>7}"
                     f"{_fmt(t['n_pages'], '{:d}'):>6}{tt:>17}")
        L.append("")
        for t in tasks:
            if t.get("flow"):
                L.append(f"  Flujo tarea {t['task']}: {t['flow_done']}/"
                         f"{t['flow_total']} pasos del camino esperado")
                for i, p in enumerate(t["flow"], start=1):
                    estado = (f"✓ {p['t_s']:5.1f} s" if p["done"]
                              else "✗ no lo hizo")
                    L.append(f"     {i}. {p['label']:<48} {estado}")
        for t in tasks:
            if len(t.get("hosts") or []) > 1:
                L.append(f"  Sitios tarea {t['task']}: {', '.join(t['hosts'])}")
        for t in tasks:
            if t.get("notes"):
                L.append(f"  Nota tarea {t['task']}: {t['notes']}")
        L.append("")

    # ── 3. ¿Qué miró primero? ────────────────────────────────────────────
    L.append("── 3. ¿QUÉ MIRÓ PRIMERO? (orden de fijación) " + "─" * 31)
    any_fix = False
    for t in tasks:
        fixes = an.fixes_for_task(t["task"])
        head = (f"Tarea {t['task']} — {t['title']}" if named
                else "Sesión completa")
        L.append(f"  {head}")
        if not fixes:
            L.append("    No se detectaron fijaciones. La señal no fue estable.")
            L.append("")
            continue
        any_fix = True
        t0 = (next((x["start_ms"] for x in an.session.meta.get("tasks", [])
                    if x["id"] == t["task"] and x.get("start_ms") is not None),
                   None) if named else None)
        if t0 is None:
            data = s.data_rows
            t0 = data[0]["Timestamp_ms"] if data else fixes[0].start_ms
        L.append(f"    TTFF (tiempo hasta la 1ª fijación): "
                 f"{fixes[0].start_ms - t0:.0f} ms")
        L.append(f"    {'#':>3}  {'t (ms)':>9}  {'posición (x, y)':>18}  "
                 f"{'dur (ms)':>9}  {'zona':<18}AOI")
        L.append("    " + "-" * 68)
        for i, f in enumerate(fixes[:n_first], start=1):
            L.append(f"    {i:>3}  {f.start_ms - t0:>9.0f}  "
                     f"{f'({f.cx:.0f}, {f.cy:.0f})':>18}  {f.dur_ms:>9.0f}  "
                     f"{_zone_name(f.cx, f.cy, sw, sh):<18}"
                     f"{f.primary_aoi or '—'}")
        if len(fixes) > n_first:
            L.append(f"    … y {len(fixes) - n_first} fijaciones más "
                     "(ver scanpath numerado)")
        if t["aoi_sequence"]:
            L.append("    Recorrido por AOI: " + " → ".join(t["aoi_sequence"]))
        L.append("")
    if not any_fix and not tasks:
        L.append("  No se detectaron fijaciones.")
        L.append("")

    # ── 4. Áreas de interés ──────────────────────────────────────────────
    aois = m["aois"]
    if aois:
        L.append("── 4. ÁREAS DE INTERÉS (AOI) " + "─" * 45)
        for t in tasks:
            rows = [a for a in aois if a["task"] == t["task"]]
            if not rows:
                continue
            L.append(f"  Tarea {t['task']} — {t['title']}" if named
                     else "  Sesión completa")
            L.append(f"    {'AOI':<22}{'visto':<7}{'TTFF':>9}{'dwell':>9}"
                     f"{'%tiempo':>9}{'fijac.':>7}{'visitas':>8}")
            L.append("    " + "-" * 71)
            for a in sorted(rows, key=lambda r: (r["order"] is None,
                                                  r["order"] or 0)):
                name = ("★ " if a["is_target"] else "  ") + a["label"][:19]
                L.append(f"    {name:<22}{'sí' if a['seen'] else 'no':<7}"
                         f"{_fmt(a['ttff_ms'], '{:.0f} ms'):>9}"
                         f"{a['dwell_ms'] / 1000:>8.1f}s"
                         f"{a['share_pct']:>8.1f}%{a['n_fix']:>7d}"
                         f"{a['visits']:>8d}"
                         + ("  ⚠ AOI menor que el error de precisión"
                            if a["too_small"] else ""))
            L.append("")
        L.append("  ★ = AOI objetivo de la tarea.  TTFF y dwell se miden desde "
                 "que el participante pulsa «Comenzar».")
        L.append("")

    # ── 5. Mapa de atención 3×3 ──────────────────────────────────────────
    fixes_all = an.fixes
    total_fix_ms = sum(f.dur_ms for f in fixes_all) or 1e-6
    grid_ms = np.zeros((3, 3), dtype=float)
    grid_n = np.zeros((3, 3), dtype=int)
    for f in fixes_all:
        cxi = min(2, max(0, int(f.cx / max(sw, 1) * 3)))
        cyi = min(2, max(0, int(f.cy / max(sh, 1) * 3)))
        grid_ms[cyi, cxi] += f.dur_ms
        grid_n[cyi, cxi] += 1

    L.append("── 5. MAPA DE ATENCIÓN (tiempo de fijación por zona) " + "─" * 23)
    L.append("")
    L.append("               izquierda        centro          derecha")
    for ri, rname in enumerate(_ROWS):
        cells = [f"{100.0 * grid_ms[ri, ci] / total_fix_ms:5.1f}% "
                 f"({grid_n[ri, ci]:>3})" for ci in range(3)]
        L.append(f"  {rname:>7}   " + "  ".join(cells))
    L.append("")
    flat = sorted(((grid_ms[r, c], grid_n[r, c], r, c)
                   for r in range(3) for c in range(3)), reverse=True)
    L.append("  Zonas ordenadas por tiempo de atención:")
    for ms, n, r, c in flat:
        if ms <= 0:
            continue
        frac = ms / total_fix_ms
        L.append(f"    {_ROWS[r] + '-' + _COLS[c]:<18} "
                 f"{frac * 100:5.1f}%  {_bar(frac)}  ({n} fijaciones)")
    L.append("")

    # ── 6. Métricas globales ─────────────────────────────────────────────
    L.append("── 6. MÉTRICAS OCULOMÉTRICAS " + "─" * 46)
    L.append(f"  Fijaciones detectadas   : {sm['n_fixations']:8d}")
    L.append(f"  Sacadas detectadas      : {sm['n_saccades']:8d}")
    if sm["n_fixations"]:
        L.append(f"  Duración media fijación : {sm['mean_fix_ms']:8.0f} ms")
        L.append(f"  Duración mediana        : {sm['median_fix_ms']:8.0f} ms")
        L.append(f"  Fijación más larga      : {sm['max_fix_ms']:8.0f} ms")
        L.append(f"  Tiempo total en fijación: {sm['total_fix_ms'] / 1000:8.2f} s "
                 f"({100.0 * sm['total_fix_ms'] / max(sm['duration_s'] * 1000, 1e-6):.1f}"
                 " % de la sesión)")
    L.append("")
    L.append("  Nota: «Pupil_Size» del CSV es el radio del iris en píxeles de "
             "cámara, NO el diámetro")
    L.append("  real de la pupila; no se reporta como métrica porque con "
             "webcam no es confiable.")
    L.append("")
    L.append(rule)
    return "\n".join(L)


def _write(txt: str, csv_path: str, output: Optional[str]) -> None:
    path = output or f"{os.path.splitext(csv_path)[0]}_informe.txt"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception as e:
        print(f"[report] No se pudo guardar el informe: {e}")
        return
    try:                       # consola cp1252 de Windows: «→» no siempre cabe
        print(f"[report] Informe guardado → {path}")
    except UnicodeEncodeError:
        print(f"[report] Informe guardado: {path}")
