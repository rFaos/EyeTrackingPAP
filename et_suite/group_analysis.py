"""
et_suite/group_analysis.py  —  Análisis de TODOS los participantes de un estudio
=================================================================================
Junta las sesiones de un estudio y produce, en una carpeta de resultados:

  Tablas (CSV, UTF-8 con BOM: se abren bien en Excel; también SPSS / R):
    sesiones.csv         una fila por participante (calidad + columnas T1_…, T2_…)
    tareas.csv           una fila por participante × tarea
    aoi.csv              una fila por participante × tarea × AOI
    resumen_tareas.csv   n, éxito, y media / DE / mediana por tarea (y por grupo)
    resumen_aoi.csv      % que vio cada AOI, TTFF y dwell (media / DE / mediana)
    resumen_grupal.txt   lo anterior en texto legible

  Imágenes:
    heatmap_tarea<N>_dwell.png / _densidad.png   mapa combinado de todos
    heatmap_tarea<N>_pagina.png                  sobre la página (si hubo scroll)
    aoi_tarea<N>.png                             % de participantes que vio cada AOI

Sólo se usan las sesiones VÁLIDAS (ver ``metrics.assess_quality``) salvo que se
pida lo contrario; las excluidas se listan igualmente en ``sesiones.csv`` con
su motivo, para que la exclusión quede documentada.

Si el estudio tiene grupos / condiciones (campo «Grupo» de cada sesión), las
tablas de resumen incluyen además una fila por grupo.
"""

from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import cv2
import numpy as np

from .heatmap_generator import HeatmapGenerator
from .metrics import Analysis, Fix, analyze_csv
from .session_meta import find_sessions, shots_dir_for
from .study import Study


@dataclass
class GroupResult:
    out_dir: str
    n_sessions: int = 0
    n_valid: int = 0
    n_used: int = 0
    files: List[str] = field(default_factory=list)
    summary_text: str = ""
    analyses: List[Analysis] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════════════════
#  Estadística descriptiva
# ════════════════════════════════════════════════════════════════════════════

def describe(values: Sequence[Optional[float]]) -> Dict[str, Optional[float]]:
    """n, media, desviación estándar (muestral), mediana, mín y máx."""
    v = [float(x) for x in values
         if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if not v:
        return {"n": 0, "mean": None, "sd": None, "median": None,
                "min": None, "max": None}
    return {"n": len(v), "mean": float(np.mean(v)),
            "sd": float(np.std(v, ddof=1)) if len(v) > 1 else None,
            "median": float(np.median(v)),
            "min": float(np.min(v)), "max": float(np.max(v))}


def _r(v, nd: int = 1):
    """Redondeo para escribir en CSV (None → vacío)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return round(v, nd)
    return v


def _write_csv(path: str, header: List[str], rows: List[List]) -> str:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def session_task_screenshot(csv_path: str, task_id: int) -> Optional[str]:
    """Captura guardada al inicio de una tarea (si existe)."""
    p = os.path.join(shots_dir_for(csv_path), f"tarea{task_id}.png")
    return p if os.path.exists(p) else None


# ════════════════════════════════════════════════════════════════════════════
#  Análisis del estudio
# ════════════════════════════════════════════════════════════════════════════

def analyze_study(study: Study, sessions_dir: str, out_dir: str,
                  include_invalid: bool = False, make_images: bool = True,
                  screen_w: Optional[int] = None,
                  screen_h: Optional[int] = None) -> GroupResult:
    """
    Analiza todas las sesiones de ``study`` y escribe los resultados.

    Args:
        sessions_dir:    carpeta donde están las sesiones (se busca dentro).
        out_dir:         carpeta de salida (se crea).
        include_invalid: incluir también las sesiones marcadas no válidas.
    """
    os.makedirs(out_dir, exist_ok=True)
    res = GroupResult(out_dir=out_dir)

    paths = find_sessions(sessions_dir, study.id)
    paths.sort(key=lambda p: os.path.basename(p))
    analyses: List[Analysis] = []
    for p in paths:
        try:
            analyses.append(analyze_csv(p, study))
        except Exception as e:                       # una sesión rota no tumba todo
            print(f"[group] Se omitió {os.path.basename(p)}: {e}")
    res.n_sessions = len(analyses)
    res.n_valid = sum(1 for a in analyses if a.metrics["quality"]["valid"])
    used = [a for a in analyses
            if include_invalid or a.metrics["quality"]["valid"]]
    res.n_used = len(used)
    res.analyses = used

    if not analyses:
        res.summary_text = ("No hay sesiones grabadas para el estudio "
                            f"«{study.name}» en {sessions_dir}.")
        return res

    task_ids = [t.id for t in study.tasks]
    res.files += _write_tables(study, analyses, used, task_ids, out_dir)

    text = _summary_text(study, analyses, used, task_ids, include_invalid)
    p = os.path.join(out_dir, "resumen_grupal.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    res.files.append(p)
    res.summary_text = text

    if make_images and used:
        try:
            res.files += _group_images(study, used, task_ids, out_dir,
                                       screen_w, screen_h)
        except Exception as e:
            print(f"[group] No se pudieron generar las imágenes: {e}")
    return res


# ── tablas ──────────────────────────────────────────────────────────────────

def _groups_of(used: List[Analysis]) -> List[str]:
    gs = sorted({a.session.group for a in used if a.session.group})
    return ["TODOS"] + (gs if len(gs) > 1 else [])


def _select(used: List[Analysis], group: str) -> List[Analysis]:
    return used if group == "TODOS" else [a for a in used
                                          if a.session.group == group]


def _write_tables(study: Study, analyses: List[Analysis],
                  used: List[Analysis], task_ids: List[int],
                  out_dir: str) -> List[str]:
    files: List[str] = []
    used_set = {id(a) for a in used}

    # ── sesiones.csv (una fila por participante) ─────────────────────────
    head = ["participante", "grupo", "archivo", "valida", "usada_en_resumen",
            "motivos_exclusion", "advertencias", "duracion_s", "muestras",
            "frecuencia_hz", "cara_pct", "parpadeos", "parpadeos_por_min",
            "calibracion_px", "calibracion_grados", "validacion_inicial_px",
            "validacion_final_px", "validacion_final_grados", "usa_lentes"]
    per_task = ["resultado", "exito", "tiempo_s", "fijaciones", "sacadas",
                "clics", "paginas", "ttff_objetivo_ms", "dwell_objetivo_ms",
                "primera_aoi", "recorrido_px"]
    for tid in task_ids:
        head += [f"T{tid}_{c}" for c in per_task]
    rows = []
    for a in analyses:
        sm, q = a.metrics["session"], a.metrics["quality"]
        r = [sm["participant"], sm["group"], sm["csv"], q["valid"],
             id(a) in used_set, " | ".join(q["reasons"]),
             " | ".join(q["warnings"]), _r(sm["duration_s"]), sm["n_samples"],
             _r(sm["mean_hz"]), _r(sm["face_pct"]), sm["n_blinks"],
             _r(sm["blink_rate_per_min"]), _r(sm["cal_rms_px"], 0),
             _r(sm["cal_rms_deg"], 2), _r(sm["val_pre_px"], 0),
             _r(sm["val_post_px"], 0), _r(sm["val_post_deg"], 2),
             sm["wears_glasses"]]
        by_task = {t["task"]: t for t in a.metrics["tasks"]}
        for tid in task_ids:
            t = by_task.get(tid)
            if t is None or not t["started"]:
                r += [""] * len(per_task)
                continue
            r += [t["result_label"], t["success"], _r(t["time_on_task_s"]),
                  t["n_fixations"], t["n_saccades"], _r(t["n_clicks"]),
                  _r(t["n_pages"]), _r(t["target_ttff_ms"], 0),
                  _r(t["target_dwell_ms"], 0), t["first_aoi"] or "",
                  _r(t["scanpath_px"], 0)]
        rows.append(r)
    files.append(_write_csv(os.path.join(out_dir, "sesiones.csv"), head, rows))

    # ── tareas.csv ───────────────────────────────────────────────────────
    head = ["participante", "grupo", "valida", "tarea", "titulo", "resultado",
            "exito", "tiempo_s", "fijaciones", "sacadas", "dur_media_fij_ms",
            "recorrido_px", "clics", "primer_clic_s", "paginas",
            "cargas_pagina", "parpadeos", "aoi_objetivo", "objetivo_visto",
            "ttff_objetivo_ms", "dwell_objetivo_ms", "primera_aoi",
            "transiciones_aoi", "secuencia_aoi", "pasos_flujo_hechos",
            "pasos_flujo_total", "flujo_completo", "pasos_flujo", "notas"]
    rows = []
    for a in analyses:
        for t in a.metrics["tasks"]:
            if not t["started"]:
                continue
            rows.append([
                a.session.participant, a.session.group,
                a.metrics["quality"]["valid"], t["task"], t["title"],
                t["result_label"], t["success"], _r(t["time_on_task_s"]),
                t["n_fixations"], t["n_saccades"], _r(t["mean_fix_ms"], 0),
                _r(t["scanpath_px"], 0), _r(t["n_clicks"]),
                _r(t["first_click_s"]), _r(t["n_pages"]),
                _r(t["n_page_loads"]), t["n_blinks"], t["target_aoi"] or "",
                "" if t["target_seen"] is None else int(t["target_seen"]),
                _r(t["target_ttff_ms"], 0), _r(t["target_dwell_ms"], 0),
                t["first_aoi"] or "", t["aoi_transitions"],
                " > ".join(t["aoi_sequence"]),
                t.get("flow_done", ""), t.get("flow_total", ""),
                "" if t.get("flow_complete") is None
                else int(t["flow_complete"]),
                " | ".join(f"{'+' if p['done'] else '-'}{p['id']}"
                           + (f"@{p['t_s']:.1f}s" if p["done"] else "")
                           for p in t.get("flow") or []),
                t.get("notes", "")])
    files.append(_write_csv(os.path.join(out_dir, "tareas.csv"), head, rows))

    # ── aoi.csv ──────────────────────────────────────────────────────────
    head = ["participante", "grupo", "valida", "tarea", "aoi", "etiqueta",
            "es_objetivo", "visto", "ttff_ms", "dwell_ms", "fijaciones",
            "visitas", "revisitas", "orden_descubrimiento", "pct_tiempo"]
    rows = []
    for a in analyses:
        started = {t["task"] for t in a.metrics["tasks"] if t["started"]}
        for x in a.metrics["aois"]:
            if x["task"] not in started:
                continue
            rows.append([x["participant"], x["group"],
                         a.metrics["quality"]["valid"], x["task"], x["aoi"],
                         x["label"], x["is_target"], x["seen"],
                         _r(x["ttff_ms"], 0), _r(x["dwell_ms"], 0),
                         x["n_fix"], x["visits"], x["revisits"],
                         _r(x["order"]), _r(x["share_pct"])])
    files.append(_write_csv(os.path.join(out_dir, "aoi.csv"), head, rows))

    # ── resumen_tareas.csv ───────────────────────────────────────────────
    groups = _groups_of(used)
    head = ["grupo", "tarea", "titulo", "n_participantes", "n_exito",
            "pct_exito",
            "tiempo_media_s", "tiempo_de_s", "tiempo_mediana_s",
            "fijaciones_media", "fijaciones_de", "clics_media", "paginas_media",
            "pct_vio_objetivo", "ttff_objetivo_media_ms",
            "ttff_objetivo_de_ms", "ttff_objetivo_mediana_ms"]
    rows = []
    for g in groups:
        sel = _select(used, g)
        for tid in task_ids:
            ts = [t for a in sel for t in a.metrics["tasks"]
                  if t["task"] == tid and t["started"]]
            if not ts:
                continue
            tm = describe([t["time_on_task_s"] for t in ts])
            fx = describe([t["n_fixations"] for t in ts])
            ck = describe([t["n_clicks"] for t in ts])
            pg = describe([t["n_pages"] for t in ts])
            seen = [t for t in ts if t["target_aoi"]]
            tt = describe([t["target_ttff_ms"] for t in seen
                           if t["target_seen"]])
            n_ok = sum(1 for t in ts if t["success"])
            rows.append([
                g, tid, ts[0]["title"], len(ts), n_ok,
                _r(100.0 * n_ok / len(ts)),
                _r(tm["mean"]), _r(tm["sd"]), _r(tm["median"]),
                _r(fx["mean"]), _r(fx["sd"]), _r(ck["mean"]), _r(pg["mean"]),
                _r(100.0 * sum(1 for t in seen if t["target_seen"])
                   / len(seen)) if seen else "",
                _r(tt["mean"], 0), _r(tt["sd"], 0), _r(tt["median"], 0)])
    files.append(_write_csv(os.path.join(out_dir, "resumen_tareas.csv"),
                            head, rows))

    # ── resumen_flujo.csv: % de participantes que hizo cada paso ──────
    head = ["grupo", "tarea", "titulo", "paso", "descripcion",
            "n_participantes", "n_hizo", "pct_hizo", "segundo_mediana"]
    rows = []
    for g in groups:
        sel = _select(used, g)
        for tid in task_ids:
            ts = [t for a in sel for t in a.metrics["tasks"]
                  if t["task"] == tid and t["started"] and t.get("flow")]
            if not ts:
                continue
            for i, paso in enumerate(ts[0]["flow"]):
                hechos = [t["flow"][i] for t in ts
                          if i < len(t["flow"]) and t["flow"][i]["done"]]
                med = describe([p["t_s"] for p in hechos])["median"]
                rows.append([g, tid, ts[0]["title"], i + 1, paso["label"],
                             len(ts), len(hechos),
                             _r(100.0 * len(hechos) / len(ts)), _r(med)])
    files.append(_write_csv(os.path.join(out_dir, "resumen_flujo.csv"),
                            head, rows))

    # ── resumen_aoi.csv ──────────────────────────────────────────────────
    head = ["grupo", "tarea", "aoi", "etiqueta", "es_objetivo",
            "n_participantes", "n_vio", "pct_vio",
            "ttff_media_ms", "ttff_de_ms", "ttff_mediana_ms",
            "dwell_media_ms", "dwell_de_ms", "dwell_mediana_ms",
            "fijaciones_media", "visitas_media", "revisitas_media"]
    rows = []
    for g in groups:
        sel = _select(used, g)
        for tid in task_ids:
            ids = [x.id for x in study.aois_for_task(tid)]
            for aid in ids:
                xs = []
                for a in sel:
                    started = {t["task"] for t in a.metrics["tasks"]
                               if t["started"]}
                    if tid not in started:
                        continue
                    xs += [x for x in a.metrics["aois"]
                           if x["task"] == tid and x["aoi"] == aid]
                if not xs:
                    continue
                seen = [x for x in xs if x["seen"]]
                tt = describe([x["ttff_ms"] for x in seen])
                dw = describe([x["dwell_ms"] for x in xs])
                rows.append([
                    g, tid, aid, xs[0]["label"], xs[0]["is_target"],
                    len(xs), len(seen), _r(100.0 * len(seen) / len(xs)),
                    _r(tt["mean"], 0), _r(tt["sd"], 0), _r(tt["median"], 0),
                    _r(dw["mean"], 0), _r(dw["sd"], 0), _r(dw["median"], 0),
                    _r(describe([x["n_fix"] for x in xs])["mean"], 2),
                    _r(describe([x["visits"] for x in xs])["mean"], 2),
                    _r(describe([x["revisits"] for x in xs])["mean"], 2)])
    files.append(_write_csv(os.path.join(out_dir, "resumen_aoi.csv"),
                            head, rows))
    return files


# ── texto ───────────────────────────────────────────────────────────────────

def _fm(v: Optional[float], fmt: str = "{:.1f}") -> str:
    return "—" if v is None else fmt.format(v)


def _summary_text(study: Study, analyses: List[Analysis],
                  used: List[Analysis], task_ids: List[int],
                  include_invalid: bool) -> str:
    L: List[str] = []
    rule = "=" * 76
    L += [rule, f"  RESUMEN GRUPAL — {study.name}", rule, ""]
    n_valid = sum(1 for a in analyses if a.metrics["quality"]["valid"])
    L.append(f"  Sesiones grabadas : {len(analyses)}")
    L.append(f"  Sesiones válidas  : {n_valid}")
    L.append(f"  Sesiones usadas   : {len(used)}"
             + ("  (incluye las no válidas)" if include_invalid else ""))
    excl = [a for a in analyses if not a.metrics["quality"]["valid"]]
    if excl:
        L.append("")
        L.append("  Sesiones excluidas por calidad:")
        for a in excl:
            L.append(f"    · {a.session.participant}: "
                     + "; ".join(a.metrics["quality"]["reasons"]))
    L.append("")

    if not used:
        L.append("  No hay sesiones válidas que resumir.")
        return "\n".join(L)

    glasses = sum(1 for a in used if a.metrics["session"]["wears_glasses"])
    cal = describe([a.metrics["session"]["cal_rms_px"] for a in used])
    val = describe([a.metrics["session"]["val_post_px"]
                    or a.metrics["session"]["val_pre_px"] for a in used])
    L.append("  Calidad media de los datos usados:")
    L.append(f"    error de calibración  : {_fm(cal['mean'], '{:.0f}')} px "
             f"(DE {_fm(cal['sd'], '{:.0f}')})")
    L.append(f"    error de validación   : {_fm(val['mean'], '{:.0f}')} px "
             f"(DE {_fm(val['sd'], '{:.0f}')})")
    L.append(f"    frecuencia de muestreo: "
             f"{_fm(describe([a.metrics['session']['mean_hz'] for a in used])['mean'])} Hz")
    L.append(f"    participantes con lentes: {glasses} de {len(used)}")
    L.append("")

    for g in _groups_of(used):
        sel = _select(used, g)
        L.append("─" * 76)
        L.append(f"  {'TODOS LOS PARTICIPANTES' if g == 'TODOS' else 'GRUPO ' + g}"
                 f"  (n = {len(sel)})")
        L.append("─" * 76)
        for tid in task_ids:
            ts = [t for a in sel for t in a.metrics["tasks"]
                  if t["task"] == tid and t["started"]]
            if not ts:
                continue
            tdef = study.task(tid)
            n_ok = sum(1 for t in ts if t["success"])
            tm = describe([t["time_on_task_s"] for t in ts])
            tm_ok = describe([t["time_on_task_s"] for t in ts if t["success"]])
            fx = describe([t["n_fixations"] for t in ts])
            ck = describe([t["n_clicks"] for t in ts])
            L.append("")
            L.append(f"  Tarea {tid} — {tdef.title if tdef else ts[0]['title']}")
            L.append(f"    Éxito           : {n_ok}/{len(ts)} "
                     f"({100.0 * n_ok / len(ts):.0f} %)")
            L.append(f"    Tiempo en tarea : media {_fm(tm['mean'])} s · "
                     f"DE {_fm(tm['sd'])} · mediana {_fm(tm['median'])} s"
                     f"  (sólo exitosas: media {_fm(tm_ok['mean'])} s)")
            L.append(f"    Fijaciones      : media {_fm(fx['mean'])} "
                     f"· DE {_fm(fx['sd'])}")
            if ck["n"]:
                L.append(f"    Clics           : media {_fm(ck['mean'])} "
                         f"· DE {_fm(ck['sd'])}")
            con_flujo = [t for t in ts if t.get("flow")]
            if con_flujo:
                n_c = sum(1 for t in con_flujo if t["flow_complete"])
                L.append(f"    Camino esperado : completo en {n_c}/"
                         f"{len(con_flujo)} participantes")
                for i, paso in enumerate(con_flujo[0]["flow"]):
                    hechos = [t["flow"][i] for t in con_flujo
                              if i < len(t["flow"]) and t["flow"][i]["done"]]
                    med = describe([p["t_s"] for p in hechos])["median"]
                    L.append(f"      {i + 1}. {paso['label'][:44]:<44} "
                             f"{100.0 * len(hechos) / len(con_flujo):4.0f} %"
                             + (f" · mediana {med:.1f} s" if med is not None
                                else ""))
            aoi_rows = []
            for aoi in study.aois_for_task(tid):
                xs = [x for a in sel for x in a.metrics["aois"]
                      if x["task"] == tid and x["aoi"] == aoi.id
                      and any(t["task"] == tid and t["started"]
                              for t in a.metrics["tasks"])]
                if not xs:
                    continue
                seen = [x for x in xs if x["seen"]]
                aoi_rows.append((aoi, xs, seen))
            if aoi_rows:
                L.append(f"    {'AOI':<24}{'lo vieron':>11}{'TTFF med.':>11}"
                         f"{'dwell med.':>12}{'fijac.':>8}")
                for aoi, xs, seen in aoi_rows:
                    tt = describe([x["ttff_ms"] for x in seen])
                    dw = describe([x["dwell_ms"] for x in xs])
                    nm = ("★ " if (tdef and tdef.target_aoi == aoi.id) else "  ") \
                        + aoi.label[:21]
                    L.append(f"    {nm:<24}"
                             f"{f'{len(seen)}/{len(xs)}':>11}"
                             f"{_fm(tt['mean'], '{:.0f}') + ' ms':>11}"
                             f"{_fm((dw['mean'] or 0) / 1000.0) + ' s':>12}"
                             f"{_fm(describe([x['n_fix'] for x in xs])['mean']):>8}")
        L.append("")
    L.append("  ★ = AOI objetivo.  TTFF = tiempo hasta la primera fijación (sólo "
             "entre quienes lo vieron).")
    L.append("  Los datos completos por participante están en sesiones.csv, "
             "tareas.csv y aoi.csv.")
    L.append(rule)
    return "\n".join(L)


# ── imágenes ────────────────────────────────────────────────────────────────

def _to_canvas(f: Fix, sw: int, sh: int, W: int, H: int) -> Fix:
    """Copia de una fijación con la posición reescalada al lienzo común."""
    g = Fix(**{k: getattr(f, k) for k in
               ("start_ms", "dur_ms", "cx", "cy", "task", "n", "px", "py",
                "vx", "vy", "panel")})
    g.cx = f.cx / max(sw, 1) * W
    g.cy = f.cy / max(sh, 1) * H
    return g


def _group_images(study: Study, used: List[Analysis], task_ids: List[int],
                  out_dir: str, screen_w: Optional[int],
                  screen_h: Optional[int]) -> List[str]:
    W = int(screen_w or used[0].session.screen_w)
    H = int(screen_h or used[0].session.screen_h)
    gen = HeatmapGenerator(W, H, sigma_px=study.heatmap_sigma_px)
    files: List[str] = []

    for tid in task_ids:
        tdef = study.task(tid)
        per_p = [[_to_canvas(f, a.session.screen_w, a.session.screen_h, W, H)
                  for f in a.fixes_for_task(tid)] for a in used]
        per_p_raw = [a.fixes_for_task(tid) for a in used]
        if not any(per_p):
            continue

        # Con capturas del navegador: el mapa del grupo sobre la página real
        vistas = gen.group_page_views(
            used, tid, out_dir,
            f"Tarea {tid}: {tdef.title}" if tdef else f"Tarea {tid}")
        if vistas:
            files += vistas
            continue

        # Fondo: el del estudio, o la captura de la primera sesión que la tenga
        bg_path = study.resolve_path(tdef.background) if tdef else None
        if not (bg_path and os.path.exists(bg_path)):
            bg_path = next((p for a in used
                            if (p := session_task_screenshot(
                                a.session.csv_path, tid))), None)
        bg = gen._load_bg(bg_path)                                # noqa: SLF001
        n = sum(1 for p in per_p if p)
        aois = gen.screen_aois(used[0], tid)

        for weighted, name in ((True, "dwell"), (False, "densidad")):
            img = gen.render_group(per_p, (W, H), bg, weighted=weighted,
                                   aois=aois)
            gen._draw_legend(                                     # noqa: SLF001
                img,
                f"{'DWELL TIME' if weighted else 'DENSIDAD'} GRUPAL  ·  "
                f"tarea {tid}  ·  n={n}",
                [(tdef.title if tdef else "")[:60],
                 "Cada participante pesa lo mismo",
                 "Rojo = donde mas miro el grupo"])
            files.append(gen._save(                               # noqa: SLF001
                img, os.path.join(out_dir, f"heatmap_tarea{tid}_{name}.png")))

        # Mapa sobre la página (sólo si hubo scroll con puente web)
        page_fx = [[f for f in fx if f.px is not None] for fx in per_p_raw]
        if any(page_fx):
            pw, ph = gen._page_size(used[0])                      # noqa: SLF001
            for a in used[1:]:
                p2, h2 = gen._page_size(a)                        # noqa: SLF001
                pw, ph = max(pw, p2), max(ph, h2)
            if ph > H * 1.05:
                s = min(1.0, 1600 / max(pw, 1), 7000 / max(ph, 1))
                size = (max(10, int(pw * s)), max(10, int(ph * s)))
                pbg = gen._load_raw(study.resolve_path(               # noqa: SLF001
                    tdef.page_screenshot) if tdef else None)
                img = gen.render_group(page_fx, size, pbg, weighted=True,
                                       use_page=True, scale=s,
                                       aois=gen.page_aois(used[0]))
                gen._draw_legend(                                     # noqa: SLF001
                    img, f"DWELL GRUPAL SOBRE LA PAGINA  ·  tarea {tid}",
                    ["Coordenadas de la pagina (con scroll)"])
                files.append(gen._save(                               # noqa: SLF001
                    img, os.path.join(out_dir,
                                      f"heatmap_tarea{tid}_pagina.png")))

        # Cobertura de AOI (% que lo vio) — AOI con rectángulo de pantalla
        cov = _aoi_coverage_image(gen, study, used, tid, bg, W, H)
        if cov is not None:
            files.append(gen._save(                               # noqa: SLF001
                cov, os.path.join(out_dir, f"aoi_tarea{tid}.png")))
    return files


def _aoi_coverage_image(gen: HeatmapGenerator, study: Study,
                        used: List[Analysis], tid: int,
                        bg: Optional[np.ndarray], W: int, H: int
                        ) -> Optional[np.ndarray]:
    items = []
    for aoi in study.aois_for_task(tid):
        r = aoi.screen_rect(W, H, study.ref_w, study.ref_h)
        if not r:
            continue
        xs = [x for a in used for x in a.metrics["aois"]
              if x["task"] == tid and x["aoi"] == aoi.id]
        if xs:
            items.append((aoi, r, xs))
    if not items:
        return None
    canvas = (bg.copy() if bg is not None
              else np.ones((H, W, 3), dtype=np.uint8) * 255)
    overlay = canvas.copy()
    for aoi, (x0, y0, x1, y1), xs in items:
        pct = 100.0 * sum(1 for x in xs if x["seen"]) / len(xs)
        # rojo (nadie lo vio) → verde (todos)
        col = (int(60 * (1 - pct / 100)), int(60 + 150 * pct / 100),
               int(220 * (1 - pct / 100) + 40))
        cv2.rectangle(overlay, (int(x0), int(y0)), (int(x1), int(y1)), col, -1)
    canvas = cv2.addWeighted(overlay, 0.45, canvas, 0.55, 0)
    for aoi, (x0, y0, x1, y1), xs in items:
        seen = [x for x in xs if x["seen"]]
        pct = 100.0 * len(seen) / len(xs)
        tt = describe([x["ttff_ms"] for x in seen])["mean"]
        cv2.rectangle(canvas, (int(x0), int(y0)), (int(x1), int(y1)),
                      (60, 60, 60), 2)
        lab = f"{aoi.label[:22]}: {pct:.0f}% lo vio"
        if tt is not None:
            lab += f" · TTFF {tt / 1000:.1f}s"
        ty = int(y0) + 22
        cv2.putText(canvas, lab, (int(x0) + 6, ty),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 3)
        cv2.putText(canvas, lab, (int(x0) + 6, ty),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 1)
    tdef = study.task(tid)
    gen._draw_legend(canvas,                                      # noqa: SLF001
                     f"COBERTURA DE AOI  ·  tarea {tid}  ·  n={len(used)}",
                     [(tdef.title if tdef else "")[:60],
                      "Verde = lo vio la mayoria; rojo = casi nadie"])
    return canvas
