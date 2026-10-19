"""
et_suite/metrics.py  —  Métricas por sesión, por tarea y por elemento (AOI)
=============================================================================
Es el único sitio donde se calculan métricas.  El informe individual, los
heatmaps y el análisis grupal consumen el resultado de ``analyze_session``,
así que los números son idénticos en todos lados.

Flujo::

    session  = load_session("sessions/estudio/P01_2026....csv", study)
    analysis = analyze_session(session)
    analysis.metrics["tasks"][0]["time_on_task_s"]

Qué calcula, por tarea:
    resultado y tiempo en tarea · fijaciones, sacadas, duración de fijación ·
    recorrido de la mirada · parpadeos · clics y páginas (con puente web) ·
    por AOI: ¿lo vio?, TTFF, dwell, fijaciones, visitas y revisitas, orden de
    descubrimiento y % del tiempo · AOI objetivo de la tarea · secuencia de
    AOI recorrida.

Todos los tiempos de "TTFF" se miden desde el instante en que el participante
pulsa "Comenzar" en la tarea.
"""

from __future__ import annotations

import bisect
import csv
import math
import os
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import numpy as np

from .display import detect_screen_width_cm, px_to_deg
from .session_meta import load_meta, load_web, task_outcome_label
from .study import QualityRules, Study

# Constantes del análisis
MIN_FIXATION_MS = 150.0   # coincide con el umbral del clasificador I-DT
MAX_GAP_MS = 400.0        # un hueco mayor entre muestras corta la fijación

SUCCESS = "success"


# ════════════════════════════════════════════════════════════════════════════
#  Estructuras
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Fix:
    """Una fijación ya agrupada (varias muestras consecutivas)."""
    start_ms: float
    dur_ms: float
    cx: float                    # posición media en pantalla (px)
    cy: float
    task: int = 0
    n: int = 0
    px: Optional[float] = None   # posición en la PÁGINA (px CSS)
    py: Optional[float] = None
    vx: Optional[float] = None   # posición dentro del área visible del navegador
    vy: Optional[float] = None
    panel: bool = False          # cayó sobre el panel de tareas
    aois: List[str] = field(default_factory=list)   # AOI que la contienen,
                                                     # del más pequeño al mayor

    @property
    def end_ms(self) -> float:
        return self.start_ms + self.dur_ms

    @property
    def primary_aoi(self) -> Optional[str]:
        return self.aois[0] if self.aois else None


@dataclass
class Session:
    csv_path: str
    rows: List[dict]
    meta: Dict
    web: List[dict]
    study: Optional[Study]
    screen_w: int
    screen_h: int

    @property
    def participant(self) -> str:
        return str(self.meta.get("participant")
                   or os.path.basename(self.csv_path).split("_")[0])

    @property
    def group(self) -> str:
        return str(self.meta.get("group") or "")

    @property
    def data_rows(self) -> List[dict]:
        return [r for r in self.rows if not r["Event_Type"].startswith("EVENT")]

    @property
    def has_page_coords(self) -> bool:
        return any(r["Page_X"] is not None for r in self.rows)


@dataclass
class Analysis:
    session: Session
    study: Optional[Study]
    fixes: List[Fix]            # sin las del panel de tareas
    panel_fixes: List[Fix]
    metrics: Dict
    resolver: "AOIResolver"

    def fixes_for_task(self, task_id: int) -> List[Fix]:
        if task_id == 0 and not self.metrics.get("has_tasks"):
            return list(self.fixes)
        return [f for f in self.fixes if f.task == task_id]


# ════════════════════════════════════════════════════════════════════════════
#  Carga de datos
# ════════════════════════════════════════════════════════════════════════════

def _f(row: dict, key: str) -> Optional[float]:
    v = row.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def read_rows(csv_path: str) -> List[dict]:
    """
    Lee un CSV de sesión.  Acepta los CSV antiguos (12 columnas) y los
    nuevos (19); lo que falte se rellena con valores neutros.
    """
    rows: List[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ev = r["Event_Type"]
                rows.append({
                    "Timestamp_ms": float(r["Timestamp_ms"]),
                    "Gaze_X": float(r["Gaze_X"]),
                    "Gaze_Y": float(r["Gaze_Y"]),
                    "Event_Type": ev,
                    "Face_Detected": int(float(r.get("Face_Detected") or 1)),
                    "Sample_Hz": _f(r, "Sample_Hz") or 0.0,
                    "Task_ID": int(float(r.get("Task_ID") or 0)),
                    "Scroll_X": _f(r, "Scroll_X"),
                    "Scroll_Y": _f(r, "Scroll_Y"),
                    "Page_X": _f(r, "Page_X"),
                    "Page_Y": _f(r, "Page_Y"),
                    "Blink": int(float(r.get("Blink") or 0)),
                })
            except (ValueError, KeyError):
                continue
    return rows


def load_session(csv_path: str, study: Optional[Study] = None,
                 screen_w: Optional[int] = None,
                 screen_h: Optional[int] = None) -> Session:
    """Carga CSV + metadatos + eventos web de una sesión."""
    meta = load_meta(csv_path) or {}
    env = meta.get("env", {}) or {}
    sw = int(screen_w or env.get("screen_w") or 1920)
    sh = int(screen_h or env.get("screen_h") or 1080)
    return Session(csv_path=csv_path, rows=read_rows(csv_path), meta=meta,
                   web=load_web(csv_path), study=study,
                   screen_w=sw, screen_h=sh)


# ════════════════════════════════════════════════════════════════════════════
#  Fijaciones
# ════════════════════════════════════════════════════════════════════════════

def _median_dt_ms(rows: List[dict]) -> float:
    ts = [r["Timestamp_ms"] for r in rows
          if not r["Event_Type"].startswith("EVENT")]
    if len(ts) < 3:
        return 66.0
    d = np.diff(ts)
    d = d[(d > 0) & (d < 500)]
    return float(np.median(d)) if d.size else 66.0


def extract_fixations(rows: List[dict], min_ms: float = MIN_FIXATION_MS,
                      max_gap_ms: float = MAX_GAP_MS) -> List[Fix]:
    """
    Agrupa muestras consecutivas "Fixation" en fijaciones.

    • Las filas EVENT y los parpadeos se ignoran sin romper la fijación
      (antes, un evento marcado dentro de una fijación la partía en dos).
    • Una fijación termina cuando cambia la tarea, cuando la muestra deja de
      ser fijación o cuando hay un hueco temporal grande (cara perdida).
    • Sólo se conservan las de al menos ``min_ms``: por debajo de ~150 ms el
      ojo está en tránsito.
    """
    dt = _median_dt_ms(rows)
    fixes: List[Fix] = []
    cur: Optional[List[dict]] = None

    def close() -> None:
        nonlocal cur
        if not cur:
            cur = None
            return
        start = cur[0]["Timestamp_ms"]
        dur = cur[-1]["Timestamp_ms"] - start + dt
        if dur >= min_ms:
            pg = [r for r in cur if r["Page_X"] is not None
                  and r["Page_Y"] is not None]
            px = py = vx = vy = None
            if len(pg) >= max(1, len(cur) // 2):
                px = float(np.mean([r["Page_X"] for r in pg]))
                py = float(np.mean([r["Page_Y"] for r in pg]))
                vx = float(np.mean([r["Page_X"] - (r["Scroll_X"] or 0.0)
                                    for r in pg]))
                vy = float(np.mean([r["Page_Y"] - (r["Scroll_Y"] or 0.0)
                                    for r in pg]))
            fixes.append(Fix(
                start_ms=start, dur_ms=dur,
                cx=float(np.mean([r["Gaze_X"] for r in cur])),
                cy=float(np.mean([r["Gaze_Y"] for r in cur])),
                task=cur[0]["Task_ID"], n=len(cur),
                px=px, py=py, vx=vx, vy=vy))
        cur = None

    for r in rows:
        et = r["Event_Type"]
        if et.startswith("EVENT") or et == "Blink":
            continue
        if et == "Fixation" and r["Face_Detected"]:
            if cur is not None and (
                    r["Task_ID"] != cur[0]["Task_ID"]
                    or r["Timestamp_ms"] - cur[-1]["Timestamp_ms"] > max_gap_ms):
                close()
            if cur is None:
                cur = []
            cur.append(r)
        else:
            close()
    close()
    return fixes


# ════════════════════════════════════════════════════════════════════════════
#  Resolución de AOI (con scroll)
# ════════════════════════════════════════════════════════════════════════════

class AOIResolver:
    """
    Decide en qué AOI cae cada fijación.

    Maneja los tres tipos de AOI del estudio:
      • rect + "viewport" — rectángulo en pantalla (escalado a la resolución)
      • rect + "page"     — rectángulo en la página (necesita puente web)
      • selector          — el puente web midió el elemento real; se usa la
                            última medición anterior a la fijación y la URL
                            que estaba abierta en ese momento
    """

    def __init__(self, study: Optional[Study], session: Session) -> None:
        self.study = study
        self.sw, self.sh = session.screen_w, session.screen_h
        self.margin = study.aoi_margin_px if study else 0.0

        # layout por URL: lista ordenada de (t_ms, {aoi_id: medición})
        self._layouts: Dict[str, Tuple[List[float], List[Dict[str, dict]]]] = {}
        # línea de tiempo de URL
        ts_url: List[Tuple[float, str]] = []
        tmp: Dict[str, List[Tuple[float, Dict[str, dict]]]] = {}
        for e in session.web:
            url = str(e.get("url", ""))
            t = float(e.get("t_ms", 0.0))
            if e.get("k") == "layout":
                m = {a["id"]: a for a in e.get("aois", []) if "id" in a}
                tmp.setdefault(url, []).append((t, m))
            if e.get("k") in ("vp", "nav", "layout") and url:
                ts_url.append((t, url))
        for url, lst in tmp.items():
            lst.sort(key=lambda p: p[0])
            self._layouts[url] = ([p[0] for p in lst], [p[1] for p in lst])
        ts_url.sort(key=lambda p: p[0])
        self._url_t = [p[0] for p in ts_url]
        self._url_v = [p[1] for p in ts_url]

    def url_at(self, t_ms: float) -> Optional[str]:
        i = bisect.bisect_right(self._url_t, t_ms) - 1
        if i < 0:
            # antes del primer evento: la primera URL conocida
            return self._url_v[0] if self._url_v else None
        return self._url_v[i]

    def layout_at(self, url: Optional[str], t_ms: float) -> Dict[str, dict]:
        if url is None or url not in self._layouts:
            return {}
        ts, ms = self._layouts[url]
        i = bisect.bisect_right(ts, t_ms) - 1
        if i < 0:
            i = 0                # aún no había medición: la primera sirve
        return ms[i]

    def _inside(self, x: Optional[float], y: Optional[float],
                x0: float, y0: float, x1: float, y1: float
                ) -> Optional[float]:
        """Área si (x, y) está dentro (con margen); None si no."""
        if x is None or y is None:
            return None
        m = self.margin
        if x0 - m <= x <= x1 + m and y0 - m <= y <= y1 + m:
            return max(1.0, (x1 - x0) * (y1 - y0))
        return None

    def resolve(self, fix: Fix, task_id: int) -> List[str]:
        """AOI que contienen la fijación, ordenados del menor al mayor área."""
        if self.study is None:
            return []
        url = self.url_at(fix.start_ms)
        hits: List[Tuple[float, str]] = []
        for a in self.study.aois_for_task(task_id):
            if a.url_contains and url and a.url_contains not in url:
                continue
            area: Optional[float] = None
            if a.rect is not None and a.space == "viewport":
                r = a.screen_rect(self.sw, self.sh,
                                  self.study.ref_w, self.study.ref_h)
                if r:
                    area = self._inside(fix.cx, fix.cy, *r)
            elif a.rect is not None and a.space == "page":
                x, y, w, h = a.rect
                area = self._inside(fix.px, fix.py, x, y, x + w, y + h)
            elif a.selector or a.text or a.opens:
                m = self.layout_at(url, fix.start_ms).get(a.id)
                if m and m.get("vis", True):
                    x, y, w, h = (float(m["x"]), float(m["y"]),
                                  float(m["w"]), float(m["h"]))
                    if m.get("fixed"):
                        area = self._inside(fix.vx, fix.vy, x, y, x + w, y + h)
                    else:
                        area = self._inside(fix.px, fix.py, x, y, x + w, y + h)
            if area is not None:
                hits.append((area, a.id))
        hits.sort()
        return [h[1] for h in hits]

    def aoi_size_px(self, aoi_id: str) -> Optional[float]:
        """Lado menor (px) del AOI, para avisar si es menor que el error."""
        if self.study is None:
            return None
        a = self.study.aoi(aoi_id)
        if a is None:
            return None
        if a.rect is not None:
            w, h = a.rect[2], a.rect[3]
            if a.space == "viewport":
                w *= self.sw / max(self.study.ref_w, 1)
                h *= self.sh / max(self.study.ref_h, 1)
            return float(min(w, h))
        sizes = []
        for _ts, ms in self._layouts.values():
            for m in ms:
                if aoi_id in m and m[aoi_id].get("vis", True):
                    sizes.append(min(m[aoi_id]["w"], m[aoi_id]["h"]))
        return float(np.median(sizes)) if sizes else None


# ════════════════════════════════════════════════════════════════════════════
#  Métricas
# ════════════════════════════════════════════════════════════════════════════

def _runs(rows: List[dict], pred) -> int:
    """Cuántas rachas consecutivas de filas cumplen ``pred``."""
    n, prev = 0, False
    for r in rows:
        cur = pred(r)
        if cur and not prev:
            n += 1
        prev = cur
    return n


def _task_windows(session: Session, rows: List[dict]) -> List[dict]:
    """
    Lista de tareas con su ventana temporal.  Prioridad: metadatos (marcas
    exactas del botón) > columna Task_ID del CSV > estudio (sin datos).
    """
    t_first = rows[0]["Timestamp_ms"] if rows else 0.0
    t_last = rows[-1]["Timestamp_ms"] if rows else 0.0
    out: List[dict] = []

    meta_tasks = session.meta.get("tasks") or []
    if meta_tasks:
        for t in meta_tasks:
            tid = int(t["id"])
            s = t.get("start_ms")
            e = t.get("end_ms")
            if s is None:               # tarea que nunca se inició
                trows = []
            else:
                trows = [r for r in rows if r["Task_ID"] == tid]
            out.append({
                "id": tid, "title": t.get("title", f"Tarea {tid}"),
                "start_ms": s, "end_ms": e if e is not None else
                (trows[-1]["Timestamp_ms"] if trows else s),
                "outcome": t.get("outcome") or ("skipped" if s is None else "interrupted"),
                "verified": t.get("verified"),
                "notes": t.get("notes", ""),
            })
        return out

    ids = sorted({r["Task_ID"] for r in rows if r["Task_ID"] > 0})
    for tid in ids:
        trows = [r for r in rows if r["Task_ID"] == tid]
        title = f"Tarea {tid}"
        if session.study and session.study.task(tid):
            title = session.study.task(tid).title        # type: ignore[union-attr]
        out.append({"id": tid, "title": title,
                    "start_ms": trows[0]["Timestamp_ms"],
                    "end_ms": trows[-1]["Timestamp_ms"],
                    "outcome": None, "verified": None, "notes": ""})
    if not out:                          # CSV antiguo / sin tareas
        out.append({"id": 0, "title": "Sesión completa",
                    "start_ms": t_first, "end_ms": t_last,
                    "outcome": None, "verified": None, "notes": ""})
    return out


def _stat_fix(fixes: List[Fix]) -> Dict:
    durs = [f.dur_ms for f in fixes]
    return {
        "n_fixations": len(fixes),
        "mean_fix_ms": float(np.mean(durs)) if durs else None,
        "median_fix_ms": float(np.median(durs)) if durs else None,
        "max_fix_ms": float(np.max(durs)) if durs else None,
        "total_fix_ms": float(np.sum(durs)) if durs else 0.0,
    }


def _scanpath_px(fixes: List[Fix]) -> float:
    return float(sum(math.hypot(b.cx - a.cx, b.cy - a.cy)
                     for a, b in zip(fixes, fixes[1:])))


# ════════════════════════════════════════════════════════════════════════════
#  Corrección de la deriva con los clics
# ════════════════════════════════════════════════════════════════════════════
#
# Con webcam, si el participante se acomoda distinto después de calibrar (se
# acerca, toma el ratón, se recarga), la mirada estimada queda DESPLAZADA de
# forma casi constante.  Al hacer clic la persona mira lo que pulsa, así que
# cada clic es un punto de calibración gratuito (la misma idea que usa
# WebGazer).  Se estima el desplazamiento mediano mirada−clic y se resta a
# toda la sesión.  Sólo se aplica si hay suficientes clics y si de verdad
# reduce el error en esos clics; el informe dice cuánto se corrigió.

CLICK_MIN_PAIRS = 3
CLICK_WIN_MS = (600.0, 50.0)      # mirada entre 600 y 50 ms antes del clic
CLICK_OUTLIER_PX = 350.0


def click_correction(session: Session, apply: bool = True) -> Dict:
    """
    Estima (y aplica sobre ``session.rows``) la corrección por clics.

    Devuelve ``{"applied", "n", "dx", "dy", "err_before_px",
    "err_after_px"}``; ``applied`` es False si no hubo clics suficientes o
    si la corrección no mejoraba.
    """
    from .display import content_origin
    info: Dict = {"applied": False, "n": 0, "dx": 0.0, "dy": 0.0,
                  "err_before_px": None, "err_after_px": None}
    if getattr(session, "_click_corr", None) is not None:
        return session._click_corr                       # ya aplicada
    if not session.web:
        return info
    muestras = [r for r in session.rows if r["Face_Detected"]
                and not r["Event_Type"].startswith("EVENT")
                and r["Event_Type"] != "Blink"]
    ts = [r["Timestamp_ms"] for r in muestras]
    vp = None
    k_px = 1.0
    pares: List[Tuple[float, float, float, float]] = []
    for e in sorted(session.web, key=lambda e: e.get("t_ms", 0.0)):
        kind = e.get("k")
        if kind == "vp":
            vp = e
            continue
        if kind != "click" or vp is None:
            continue
        x, y = e.get("x"), e.get("y")
        if x is None or y is None or (not x and not y):   # clic de teclado
            continue
        try:
            css_w = float(vp.get("sw") or 0)
            k_px = session.screen_w / css_w if css_w > 0 else 1.0
            ox, oy = content_origin(vp)
            tx, ty = (float(x) + ox) * k_px, (float(y) + oy) * k_px
        except (TypeError, ValueError):
            continue
        t = float(e.get("t_ms", 0.0))
        i0 = bisect.bisect_left(ts, t - CLICK_WIN_MS[0])
        i1 = bisect.bisect_right(ts, t - CLICK_WIN_MS[1])
        g = muestras[i0:i1]
        if len(g) < 3:
            continue
        gx = float(np.median([r["Gaze_X"] for r in g]))
        gy = float(np.median([r["Gaze_Y"] for r in g]))
        pares.append((tx, ty, gx, gy))
    if len(pares) < CLICK_MIN_PAIRS:
        info["n"] = len(pares)
        return info
    p = np.array(pares)
    d = p[:, 2:] - p[:, :2]
    med = np.median(d, axis=0)
    keep = np.hypot(*(d - med).T) < CLICK_OUTLIER_PX
    if keep.sum() < CLICK_MIN_PAIRS:
        info["n"] = int(keep.sum())
        return info
    off = np.median(d[keep], axis=0)
    antes = float(np.mean(np.hypot(*d[keep].T)))
    despues = float(np.mean(np.hypot(*(d[keep] - off).T)))
    info.update(n=int(keep.sum()), dx=float(off[0]), dy=float(off[1]),
                err_before_px=antes, err_after_px=despues)
    if despues >= antes * 0.85 or not apply:
        return info                                    # no compensa
    dx, dy = float(off[0]), float(off[1])
    for r in session.rows:
        r["Gaze_X"] -= dx
        r["Gaze_Y"] -= dy
        if r["Page_X"] is not None and r["Page_Y"] is not None:
            r["Page_X"] -= dx / k_px
            r["Page_Y"] -= dy / k_px
    info["applied"] = True
    session._click_corr = info
    return info


# ════════════════════════════════════════════════════════════════════════════
#  Flujo de la tarea (¿siguió el camino esperado?)
# ════════════════════════════════════════════════════════════════════════════

def _norm_txt(s) -> str:
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split())


def _sin_hash(url: Optional[str]) -> str:
    return str(url or "").split("#")[0]


def evaluate_flow(steps, study: Optional[Study], session: Session,
                  resolver: "AOIResolver", tfix: List[Fix],
                  s_ms: Optional[float], e_ms: Optional[float]) -> List[Dict]:
    """
    Evalúa los pasos del flujo esperado de una tarea con lo que registró el
    navegador (clics, diseño, scroll, páginas) y la mirada.

    Cada paso se busca DESPUÉS del último paso cumplido, de modo que se ve
    tanto qué hizo como en qué orden.  Devuelve, por paso:
    ``{"id", "label", "kind", "aoi", "done", "t_s"}`` (t_s desde «Comenzar»).
    """
    if s_ms is None or e_ms is None:
        return [{"id": st.id, "label": st.label, "kind": st.kind,
                 "aoi": st.aoi, "done": False, "t_s": None} for st in steps]
    web = sorted((e for e in session.web if s_ms <= e.get("t_ms", -1) <= e_ms),
                 key=lambda e: e["t_ms"])
    hosts = set(study.allowed_hosts()) if study else set()
    start_url = _sin_hash(resolver.url_at(s_ms))

    def rect(aoi: str, url: Optional[str], t: float) -> Optional[dict]:
        m = resolver.layout_at(url, t).get(aoi)
        return m if m and m.get("vis", True) else None

    def hit(st, t0: float) -> Optional[float]:
        a_def = study.aoi(st.aoi) if (study and st.aoi) else None
        if st.kind == "click":
            textos = [_norm_txt(x) for x in (a_def.text or [])] if a_def else []
            for e in web:
                if e["k"] != "click" or e["t_ms"] < t0:
                    continue
                m = rect(st.aoi, e.get("url"), e["t_ms"])
                if m:
                    x, y = ((e.get("x"), e.get("y")) if m.get("fixed")
                            else (e.get("px"), e.get("py")))
                    if (x is not None and y is not None
                            and m["x"] - 4 <= x <= m["x"] + m["w"] + 4
                            and m["y"] - 4 <= y <= m["y"] + m["h"] + 4):
                        return e["t_ms"]
                txt = _norm_txt(e.get("txt"))
                if txt and any(tx and tx in txt for tx in textos):
                    return e["t_ms"]
        elif st.kind == "visible":
            for e in web:
                if e["k"] == "layout" and e["t_ms"] >= t0 and any(
                        m.get("id") == st.aoi and m.get("vis", True)
                        for m in e.get("aois", [])):
                    return e["t_ms"]
        elif st.kind == "scroll_to":
            for e in web:
                if e["k"] != "vp" or e["t_ms"] < t0:
                    continue
                m = rect(st.aoi, e.get("url"), e["t_ms"])
                if not m:
                    continue
                if m.get("fixed"):
                    return e["t_ms"]
                sy, ih = float(e.get("sy") or 0), float(e.get("ih") or 0)
                arriba, abajo = max(m["y"], sy), min(m["y"] + m["h"], sy + ih)
                if abajo - arriba >= min(120.0, m["h"] * 0.5):
                    return e["t_ms"]
        elif st.kind == "look":
            for f in tfix:
                if f.start_ms >= t0 and st.aoi in f.aois:
                    return f.start_ms
        elif st.kind == "navigate":
            for e in web:
                if (e["k"] == "nav" and e["t_ms"] >= t0
                        and _sin_hash(e.get("url")) != start_url):
                    return e["t_ms"]
        elif st.kind == "leave_site":
            for e in web:
                if e["k"] != "nav" or e["t_ms"] < t0:
                    continue
                h = urlparse(str(e.get("url") or "")).hostname or ""
                if e.get("externo") or (hosts and h and h not in hosts
                                        and h not in ("127.0.0.1", "localhost")):
                    return e["t_ms"]
        return None

    out: List[Dict] = []
    t_prev = s_ms
    for st in steps:
        t = hit(st, t_prev)
        if t is not None:
            t_prev = t
        out.append({"id": st.id, "label": st.label, "kind": st.kind,
                    "aoi": st.aoi, "done": t is not None,
                    "t_s": ((t - s_ms) / 1000.0) if t is not None else None})
    return out


def analyze_session(session: Session,
                    study: Optional[Study] = None) -> Analysis:
    """Calcula todas las métricas de una sesión."""
    study = study or session.study
    session.study = study
    rows = session.rows
    data = session.data_rows
    min_fix = study.min_fixation_ms if study else MIN_FIXATION_MS

    resolver = AOIResolver(study, session)
    corr = click_correction(session, apply=(study.click_correction
                                            if study else True))
    all_fixes = extract_fixations(rows, min_ms=min_fix)

    # Fijaciones sobre el panel de tareas: se apartan (el participante lee
    # la instrucción; no es "mirar el sitio web").
    panel = session.meta.get("task_panel_rect")
    fixes: List[Fix] = []
    panel_fixes: List[Fix] = []
    for f in all_fixes:
        if panel and (panel[0] <= f.cx <= panel[0] + panel[2]
                      and panel[1] <= f.cy <= panel[1] + panel[3]):
            f.panel = True
            panel_fixes.append(f)
        else:
            fixes.append(f)

    windows = _task_windows(session, data)
    has_tasks = any(w["id"] > 0 for w in windows)
    t0 = data[0]["Timestamp_ms"] if data else 0.0
    t_end = data[-1]["Timestamp_ms"] if data else 0.0
    duration_s = max(t_end - t0, 0.0) / 1000.0

    # Resolver AOI de cada fijación según su tarea
    for f in fixes:
        tid = f.task if has_tasks else 0
        f.aois = resolver.resolve(f, tid) if (tid > 0 or not has_tasks) else []

    # ── Calidad de la grabación ──────────────────────────────────────────
    n = len(data)
    face_pct = 100.0 * sum(r["Face_Detected"] for r in data) / n if n else 0.0
    hz = n / duration_s if duration_s > 0 else 0.0
    n_blinks = _runs(data, lambda r: r["Event_Type"] == "Blink")
    minutes = max(duration_s / 60.0, 1e-9)

    cal = session.meta.get("calibration", {}) or {}
    val = session.meta.get("validation", {}) or {}
    env = session.meta.get("env", {}) or {}
    width_cm = (study.screen_width_cm if study and study.screen_width_cm
                else env.get("screen_width_cm"))
    if not width_cm:
        width_cm, _ = detect_screen_width_cm()
    dist_cm = (study.distance_cm if study else env.get("distance_cm")) or 60.0

    def to_deg(px: Optional[float]) -> Optional[float]:
        if px is None:
            return None
        return px_to_deg(px, session.screen_w, width_cm, dist_cm)

    pre = val.get("pre") or {}
    post = val.get("post") or {}
    session_m: Dict = {
        "participant": session.participant,
        "group": session.group,
        "study_id": session.meta.get("study_id") or (study.id if study else ""),
        "csv": os.path.basename(session.csv_path),
        "duration_s": duration_s,
        "n_samples": n,
        "mean_hz": hz,
        "face_pct": face_pct,
        "n_blinks": n_blinks,
        "blink_rate_per_min": n_blinks / minutes if duration_s > 0 else 0.0,
        "cal_rms_px": cal.get("rms_px"),
        "cal_rms_deg": to_deg(cal.get("rms_px")),
        "val_pre_px": pre.get("mean_px"),
        "val_pre_deg": pre.get("mean_deg") if pre.get("mean_deg") is not None
        else to_deg(pre.get("mean_px")),
        "val_post_px": post.get("mean_px"),
        "val_post_deg": post.get("mean_deg") if post.get("mean_deg") is not None
        else to_deg(post.get("mean_px")),
        "wears_glasses": bool(session.meta.get("wears_glasses", False)),
        "has_page_coords": session.has_page_coords,
        "n_web_events": len(session.web),
        "panel_fix_ms": float(sum(f.dur_ms for f in panel_fixes)),
        "n_saccades": sum(1 for r in data if r["Event_Type"] == "Saccade"),
    }
    session_m["click_correction"] = corr
    session_m.update(_stat_fix(fixes))
    session_m["ttff_ms"] = (fixes[0].start_ms - t0) if fixes else None

    # ── Por tarea ────────────────────────────────────────────────────────
    task_ms: List[Dict] = []
    aoi_ms: List[Dict] = []
    clicks_all = [e for e in session.web if e.get("k") == "click"]
    navs_all = [e for e in session.web if e.get("k") in ("nav", "vp")]

    for w in windows:
        tid = w["id"]
        s_ms, e_ms = w["start_ms"], w["end_ms"]
        if tid == 0:                                  # sesión sin tareas
            trows, tfix = data, list(fixes)
        else:
            trows = [r for r in data if r["Task_ID"] == tid]
            tfix = [f for f in fixes if f.task == tid]

        if s_ms is not None and e_ms is not None:
            time_s = max(e_ms - s_ms, 0.0) / 1000.0
        else:
            time_s = 0.0
        outcome = w["outcome"]
        verified = w["verified"]
        result = verified or outcome
        t_def = study.task(tid) if study else None

        tm: Dict = {
            "task": tid, "title": w["title"],
            "outcome": outcome, "verified": verified, "result": result,
            "result_label": task_outcome_label(result),
            "success": result == SUCCESS,
            "time_on_task_s": time_s,
            "started": s_ms is not None,
            "time_limit_s": t_def.time_limit_s if t_def else None,
            "n_samples": len(trows),
            "face_pct": (100.0 * sum(r["Face_Detected"] for r in trows)
                         / len(trows)) if trows else 0.0,
            "n_saccades": sum(1 for r in trows if r["Event_Type"] == "Saccade"),
            "n_blinks": _runs(trows, lambda r: r["Event_Type"] == "Blink"),
            "scanpath_px": _scanpath_px(tfix),
            "notes": w.get("notes", ""),
        }
        tm["blink_rate_per_min"] = (tm["n_blinks"] / (time_s / 60.0)
                                    if time_s > 0 else 0.0)
        tm.update(_stat_fix(tfix))
        tm["mean_saccade_px"] = (tm["scanpath_px"] / (len(tfix) - 1)
                                 if len(tfix) > 1 else None)
        tm["first_fix_ms"] = ((tfix[0].start_ms - s_ms)
                              if (tfix and s_ms is not None) else None)

        # Proceso web (clics y páginas)
        if s_ms is not None and e_ms is not None:
            ck = [e for e in clicks_all if s_ms <= e["t_ms"] <= e_ms]
            pages = {resolver.url_at(s_ms)} if resolver.url_at(s_ms) else set()
            pages |= {str(e.get("url")) for e in navs_all
                      if s_ms <= e["t_ms"] <= e_ms and e.get("url")}
            tm["n_clicks"] = len(ck) if session.web else None
            tm["first_click_s"] = ((min(e["t_ms"] for e in ck) - s_ms) / 1000.0
                                   if ck else None)
            tm["n_pages"] = len(pages) if session.web else None
            # Dominios por los que pasó (p. ej. si llegó a Trayecto UDEM)
            tm["hosts"] = sorted({urlparse(u).hostname or "" for u in pages}
                                 - {""}) if session.web else []
            tm["n_page_loads"] = sum(1 for e in session.web
                                     if e.get("k") == "nav"
                                     and s_ms <= e["t_ms"] <= e_ms) \
                if session.web else None
        else:
            tm["n_clicks"] = tm["n_pages"] = tm["n_page_loads"] = None
            tm["hosts"] = []
            tm["first_click_s"] = None

        # Por AOI
        relevant = (study.aois_for_task(tid) if study else [])
        total_fix = tm["total_fix_ms"] or 0.0
        seq: List[str] = []
        first_seen: Dict[str, float] = {}
        for f in tfix:
            if f.primary_aoi and (not seq or seq[-1] != f.primary_aoi):
                seq.append(f.primary_aoi)
        rank = 0
        order: List[str] = []
        for f in tfix:
            for a_id in f.aois:
                if a_id not in first_seen:
                    first_seen[a_id] = f.start_ms
                    order.append(a_id)
        for a in relevant:
            hit = [f for f in tfix if a.id in f.aois]
            visits, prev_hit = 0, False
            for f in tfix:
                cur = a.id in f.aois
                if cur and not prev_hit:
                    visits += 1
                prev_hit = cur
            size = resolver.aoi_size_px(a.id)
            err = session_m["val_post_px"] or session_m["val_pre_px"] \
                or session_m["cal_rms_px"]
            seen = bool(hit)
            row = {
                "participant": session.participant, "group": session.group,
                "task": tid, "aoi": a.id, "label": a.label,
                "is_target": bool(t_def and t_def.target_aoi == a.id),
                "seen": seen,
                "ttff_ms": ((hit[0].start_ms - s_ms)
                            if (seen and s_ms is not None) else None),
                "dwell_ms": float(sum(f.dur_ms for f in hit)),
                "n_fix": len(hit),
                "visits": visits,
                "revisits": max(0, visits - 1),
                "order": (order.index(a.id) + 1) if a.id in order else None,
                "share_pct": (100.0 * sum(f.dur_ms for f in hit) / total_fix
                              if total_fix > 0 else 0.0),
                "size_px": size,
                "too_small": bool(size is not None and err
                                  and size < 2.0 * float(err)),
            }
            aoi_ms.append(row)
            if row["is_target"]:
                tm["target_aoi"] = a.id
                tm["target_seen"] = seen
                tm["target_ttff_ms"] = row["ttff_ms"]
                tm["target_dwell_ms"] = row["dwell_ms"]
                tm["target_fix"] = row["n_fix"]

        tm.setdefault("target_aoi", t_def.target_aoi if t_def else None)
        tm.setdefault("target_seen", None)
        tm.setdefault("target_ttff_ms", None)
        tm.setdefault("target_dwell_ms", None)
        tm.setdefault("target_fix", None)
        tm["aoi_sequence"] = seq
        tm["aoi_transitions"] = max(0, len(seq) - 1)
        tm["first_aoi"] = seq[0] if seq else None

        # Flujo esperado (pasos del diagrama de la tarea)
        steps = t_def.flow if t_def else []
        tm["flow"] = (evaluate_flow(steps, study, session, resolver, tfix,
                                    s_ms, e_ms) if steps else [])
        tm["flow_done"] = sum(1 for p in tm["flow"] if p["done"])
        tm["flow_total"] = len(tm["flow"])
        tm["flow_complete"] = (tm["flow_done"] == tm["flow_total"]
                               if tm["flow"] else None)
        task_ms.append(tm)

    metrics: Dict = {
        "has_tasks": has_tasks,
        "session": session_m,
        "tasks": task_ms,
        "aois": aoi_ms,
    }
    metrics["quality"] = assess_quality(
        session, session_m, task_ms,
        study.quality if study else QualityRules())

    return Analysis(session=session, study=study, fixes=fixes,
                    panel_fixes=panel_fixes, metrics=metrics,
                    resolver=resolver)


# ════════════════════════════════════════════════════════════════════════════
#  Calidad / exclusión
# ════════════════════════════════════════════════════════════════════════════

def assess_quality(session: Session, sm: Dict, tasks: List[Dict],
                   rules: QualityRules) -> Dict:
    """
    Decide si la sesión es utilizable.

    Returns:
        {"valid": bool, "reasons": [motivos de exclusión],
         "warnings": [avisos que no excluyen]}
    """
    reasons: List[str] = []
    warnings: List[str] = []
    meta = session.meta

    if meta and not (meta.get("consent") or {}).get("accepted"):
        reasons.append("sin consentimiento informado registrado")

    if meta.get("rehearsal"):
        reasons.append("sesión de ensayo (grabada sin calibrar o de práctica)")
    if meta.get("aborted"):
        warnings.append("la sesión se interrumpió antes de terminar todas "
                        "las tareas")

    if sm["n_samples"] == 0:
        reasons.append("la sesión no tiene muestras")
    else:
        if sm["face_pct"] < rules.min_face_pct:
            reasons.append(f"cara detectada sólo en {sm['face_pct']:.0f} % "
                           f"de las muestras (mínimo {rules.min_face_pct:.0f} %)")
        if sm["mean_hz"] < rules.min_sample_hz:
            reasons.append(f"frecuencia de muestreo {sm['mean_hz']:.1f} Hz "
                           f"(mínimo {rules.min_sample_hz:.0f} Hz)")
        elif sm["mean_hz"] < rules.warn_sample_hz:
            warnings.append(f"frecuencia de muestreo baja "
                            f"({sm['mean_hz']:.1f} Hz)")
        if sm["duration_s"] < rules.min_duration_s:
            reasons.append(f"sesión muy corta ({sm['duration_s']:.0f} s; "
                           f"mínimo {rules.min_duration_s:.0f} s)")
        if sm["blink_rate_per_min"] > rules.max_blink_rate:
            warnings.append(f"parpadeos/min muy altos "
                            f"({sm['blink_rate_per_min']:.0f}): revisar "
                            "iluminación o lentes")

    cal = sm.get("cal_rms_px")
    if cal is None:
        if meta:
            warnings.append("la sesión no registra el error de calibración")
    elif cal > rules.max_cal_rms_px:
        reasons.append(f"error de calibración {cal:.0f} px "
                       f"(máximo {rules.max_cal_rms_px:.0f} px)")

    # La validación más reciente manda: refleja la precisión al final.
    v_px = sm.get("val_post_px")
    v_lbl = "al final"
    if v_px is None:
        v_px, v_lbl = sm.get("val_pre_px"), "tras calibrar"
    if v_px is None:
        if meta:
            warnings.append("sin validación de precisión")
    elif v_px > rules.max_validation_px:
        reasons.append(f"error de validación {v_lbl}: {v_px:.0f} px "
                       f"(máximo {rules.max_validation_px:.0f} px)")
    pre, post = sm.get("val_pre_px"), sm.get("val_post_px")
    if pre is not None and post is not None and post > pre * 1.6 + 40:
        warnings.append(f"la precisión empeoró durante la sesión "
                        f"({pre:.0f} → {post:.0f} px)")

    with_data = [t for t in tasks if t["n_fixations"] > 0]
    if meta.get("tasks") and len(with_data) < rules.min_tasks_done:
        reasons.append(f"sólo {len(with_data)} tarea(s) con fijaciones "
                       f"(mínimo {rules.min_tasks_done})")
    low = [t["task"] for t in tasks if t["started"] and t["n_samples"] > 10
           and t["face_pct"] < rules.min_face_pct]
    if low:
        warnings.append("poca detección de cara en la(s) tarea(s) "
                        + ", ".join(map(str, low)))

    if sm.get("has_page_coords") is False and session.study and any(
            (a.selector or a.text or a.opens) or a.space == "page" for a in session.study.aois):
        warnings.append("el estudio usa AOI por selector/página pero no "
                        "llegaron datos del navegador (¿script no instalado?): "
                        "esos AOI no se pudieron evaluar")
    elif session.web and session.study:
        # AOI por selector/texto que el navegador nunca encontró: casi
        # siempre es un selector o texto mal escrito en el estudio.
        seen = {m.get("id") for e in session.web if e.get("k") == "layout"
                for m in (e.get("aois") or [])}
        started = {t["task"] for t in tasks if t["started"]}
        missing = [a.id for a in session.study.aois
                   if (a.selector or a.text or a.opens) and a.id not in seen
                   and (a.tasks is None or started & set(a.tasks))]
        if missing:
            warnings.append("AOI que el navegador nunca encontró en la página "
                            "(revisa su 'selector'/'text' en el estudio): "
                            + ", ".join(missing))

    return {"valid": not reasons, "reasons": reasons, "warnings": warnings}


# ════════════════════════════════════════════════════════════════════════════
#  Atajo
# ════════════════════════════════════════════════════════════════════════════

def analyze_csv(csv_path: str, study: Optional[Study] = None,
                screen_w: Optional[int] = None,
                screen_h: Optional[int] = None) -> Analysis:
    return analyze_session(load_session(csv_path, study, screen_w, screen_h),
                           study)
