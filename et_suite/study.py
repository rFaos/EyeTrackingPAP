"""
et_suite/study.py  —  Definición de un estudio (tareas + áreas de interés)
===========================================================================
Un estudio es un archivo JSON dentro de ``studies/``.  Ahí vive TODO lo que
cambia de una prueba a otra, de modo que el código del prototipo no se toca
al cambiar de sitio web:

  • las tareas que hará el participante (texto, tiempo límite, objetivo),
  • las áreas de interés (AOI) de la página: nombre y posición,
  • el texto del consentimiento informado,
  • los umbrales de calidad para excluir sesiones malas.

Cómo se define un AOI (cada uno admite UNA de estas formas):

  "selector": "nav"                 selector CSS.  El puente web
                                    (``web_bridge``) mide el elemento en la
                                    página real del participante, aunque haya
                                    scroll o cambie el tamaño de la ventana.
  "text": "Mis Solicitudes"         texto visible (o aria-label, title,
                                    placeholder) del elemento; no hace falta
                                    ver el HTML.  Sin importar mayúsculas ni
                                    acentos.  Una lista ["Apps", "Accesos"]
                                    vale si aparece cualquiera.
  "opens": "Trayecto UDEM"          el BOTÓN que abre el menú donde está ese
                                    texto (íconos sin texto, como el de
                                    cuadrícula).  Con "selector" a la vez, se
                                    sube desde ese texto al contenedor más
                                    cercano que cumpla el selector (p. ej.
                                    "section, table": todo el panel).
  "rect": [x, y, ancho, alto]       rectángulo manual.  Con "space":
        "viewport" → píxeles de pantalla a la resolución de referencia
                     (sirve cuando la página cabe sin scroll)
        "page"     → píxeles CSS de la página completa (requiere el puente)

Un AOI con "tasks": [1, 2] sólo se evalúa en esas tareas; sin "tasks" vale
para todas.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

STUDY_SCHEMA = 1

DEFAULT_CONSENT = (
    "Estás invitado/a a participar en una prueba de usabilidad. Se te pedirá "
    "realizar algunas tareas en un sitio web mientras la cámara web de esta "
    "computadora estima hacia dónde miras en la pantalla.\n\n"
    "• NO se graba ni se guarda video ni fotografías de tu cara: la cámara "
    "sólo se usa para calcular la posición de tu mirada en tiempo real.\n"
    "• Se guardarán únicamente coordenadas de mirada, tiempos y tu código de "
    "participante (sin tu nombre).\n"
    "• Los datos se usarán sólo con fines académicos y de forma agregada.\n"
    "• Tu participación es voluntaria: puedes detener la prueba en cualquier "
    "momento sin consecuencia alguna.\n"
    "• Se evalúa el sitio web, no tu desempeño."
)


# ════════════════════════════════════════════════════════════════════════════
#  Estructuras
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Task:
    id: int
    title: str
    instruction: str
    target_aoi: Optional[str] = None      # AOI que debería encontrar
    time_limit_s: Optional[float] = None  # None = sin límite
    start_url: Optional[str] = None       # se abre al pulsar "Comenzar"
    background: Optional[str] = None      # imagen de fondo para heatmaps
    page_screenshot: Optional[str] = None  # captura de la página completa
    flow: List["FlowStep"] = field(default_factory=list)  # camino esperado


# Tipos de paso de un flujo (``"flow"`` de una tarea).  Cada paso tiene UNA
# de estas claves; se evalúan en orden, cada uno después del anterior:
#   "click": aoi       clic sobre ese AOI (o sobre su texto)
#   "visible": aoi     el AOI aparece en pantalla (p. ej. se abre un menú)
#   "scroll_to": aoi   el participante desplaza la página hasta verlo
#   "look": aoi        fija la mirada en el AOI
#   "navigate": true   se carga otra página (cambia la URL)
#   "leave_site": true se abre un sitio de OTRO dominio (p. ej. Trayecto UDEM)
FLOW_KINDS = ("click", "visible", "scroll_to", "look", "navigate", "leave_site")


@dataclass
class FlowStep:
    id: str
    label: str
    kind: str                      # uno de FLOW_KINDS
    aoi: Optional[str] = None


@dataclass
class AOIDef:
    id: str
    label: str
    tasks: Optional[List[int]] = None     # None = todas las tareas
    selector: Optional[str] = None
    text: Optional[List[str]] = None      # texto visible del elemento (alguno)
    opens: Optional[str] = None           # botón que abre el menú con ese texto
    rect: Optional[Tuple[float, float, float, float]] = None
    space: str = "viewport"               # "viewport" | "page"
    url_contains: Optional[str] = None    # sólo vale en URLs que lo incluyan

    def applies_to(self, task_id: int) -> bool:
        return self.tasks is None or task_id in self.tasks

    def screen_rect(self, screen_w: float, screen_h: float,
                    ref_w: float, ref_h: float
                    ) -> Optional[Tuple[float, float, float, float]]:
        """Rect (x0, y0, x1, y1) de un AOI "viewport" en píxeles de pantalla."""
        if self.rect is None or self.space != "viewport":
            return None
        sx, sy = screen_w / max(ref_w, 1.0), screen_h / max(ref_h, 1.0)
        x, y, w, h = self.rect
        return (x * sx, y * sy, (x + w) * sx, (y + h) * sy)


@dataclass
class QualityRules:
    """Criterios para marcar una sesión como válida o excluirla."""
    min_face_pct: float = 80.0        # % de muestras con cara
    min_sample_hz: float = 8.0        # por debajo se excluye
    warn_sample_hz: float = 12.0      # por debajo se advierte
    max_cal_rms_px: float = 300.0     # error de calibración
    max_validation_px: float = 350.0  # error medido en la validación
    min_duration_s: float = 20.0
    min_tasks_done: int = 1           # tareas con datos
    max_blink_rate: float = 60.0      # parpadeos/min (señal de mala detección)


@dataclass
class Study:
    id: str
    name: str
    path: str = ""
    version: str = "1.0"
    url: Optional[str] = None
    description: str = ""
    consent_text: str = DEFAULT_CONSENT
    consent_version: str = "1"
    ref_w: int = 1920
    ref_h: int = 1080
    screen_width_cm: Optional[float] = None
    distance_cm: float = 60.0
    tasks: List[Task] = field(default_factory=list)
    aois: List[AOIDef] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    quality: QualityRules = field(default_factory=QualityRules)
    aoi_margin_px: float = 0.0
    min_fixation_ms: float = 150.0
    heatmap_sigma_px: float = 55.0
    click_correction: bool = True   # corregir la deriva con los clics
    show_gaze_overlay: bool = False
    task_panel_position: str = "bottom"       # bottom | top | bottom-right
    capture_screenshots: bool = True
    # Capturas de la página desde el navegador integrado (fondo de heatmaps)
    page_capture: bool = True
    final_validation: bool = True
    review_at_end: bool = False   # ventana de revisión del facilitador al final
    bridge_port: int = 4243
    bridge_hosts: List[str] = field(default_factory=list)
    # Datos marcados «REEMPLAZAR» que el equipo todavía no completó
    pending: List[str] = field(default_factory=list)

    # ── consultas ────────────────────────────────────────────────────────

    def task(self, task_id: int) -> Optional[Task]:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def aoi(self, aoi_id: str) -> Optional[AOIDef]:
        for a in self.aois:
            if a.id == aoi_id:
                return a
        return None

    def aois_for_task(self, task_id: int) -> List[AOIDef]:
        return [a for a in self.aois if a.applies_to(task_id)]

    def bridge_config(self) -> List[dict]:
        """AOI por selector/texto, en el formato que consume el script del navegador."""
        return [{"id": a.id, "selector": a.selector, "text": a.text,
                 "opens": a.opens, "url_contains": a.url_contains}
                for a in self.aois if a.selector or a.text or a.opens]

    def allowed_hosts(self) -> List[str]:
        """Dominios donde el script del navegador debe activarse."""
        hosts = list(self.bridge_hosts)
        if self.url:
            h = urlparse(self.url).hostname
            if h:
                hosts.append(h)
        return sorted(set(hosts))

    def resolve_path(self, rel: Optional[str]) -> Optional[str]:
        """Ruta absoluta de un archivo referenciado desde el JSON."""
        if not rel:
            return None
        if os.path.isabs(rel):
            return rel
        base = os.path.dirname(self.path) if self.path else "."
        return os.path.normpath(os.path.join(base, rel))


# ════════════════════════════════════════════════════════════════════════════
#  Carga y validación
# ════════════════════════════════════════════════════════════════════════════

class StudyError(ValueError):
    """El archivo del estudio es inválido; el mensaje lista todos los problemas."""


_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def _num(d: dict, key: str, default, errors: List[str], ctx: str,
         minimum: Optional[float] = None):
    v = d.get(key, default)
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        errors.append(f"{ctx}: '{key}' debe ser un número")
        return default
    if minimum is not None and v < minimum:
        errors.append(f"{ctx}: '{key}' debe ser ≥ {minimum}")
    return v


def study_from_dict(d: dict, path: str = "") -> Study:
    errors: List[str] = []

    sid = str(d.get("id", "")).strip()
    if not _ID_RE.match(sid):
        errors.append("'id' es obligatorio (letras, números, - y _; máx. 40)")
    name = str(d.get("name", sid)).strip() or sid

    st = Study(id=sid, name=name, path=path)
    st.version = str(d.get("version", "1.0"))
    st.url = d.get("url") or None
    st.description = str(d.get("description", ""))
    if d.get("consent_text"):
        st.consent_text = str(d["consent_text"])
    st.consent_version = str(d.get("consent_version", "1"))

    ref = d.get("reference_screen", {}) or {}
    st.ref_w = int(_num(ref, "width", 1920, errors, "reference_screen", 100) or 1920)
    st.ref_h = int(_num(ref, "height", 1080, errors, "reference_screen", 100) or 1080)

    disp = d.get("display", {}) or {}
    st.screen_width_cm = _num(disp, "width_cm", None, errors, "display", 5)
    st.distance_cm = float(_num(disp, "distance_cm", 60.0, errors, "display", 20) or 60.0)

    # ── tareas ───────────────────────────────────────────────────────────
    raw_tasks = d.get("tasks") or []
    if not raw_tasks:
        errors.append("'tasks' debe tener al menos una tarea")
    seen_ids = set()
    for i, t in enumerate(raw_tasks, start=1):
        ctx = f"tasks[{i}]"
        try:
            tid = int(t.get("id", i))
        except (TypeError, ValueError):
            errors.append(f"{ctx}: 'id' debe ser un entero")
            continue
        if tid <= 0:
            errors.append(f"{ctx}: 'id' debe ser ≥ 1 (0 está reservado)")
        if tid in seen_ids:
            errors.append(f"{ctx}: id {tid} repetido")
        seen_ids.add(tid)
        flow: List[FlowStep] = []
        for j, fs in enumerate(t.get("flow") or [], start=1):
            kinds = [k for k in FLOW_KINDS if k in fs]
            if len(kinds) != 1:
                errors.append(f"{ctx}.flow[{j}]: usa una (y sólo una) de "
                              + ", ".join(FLOW_KINDS))
                continue
            kind = kinds[0]
            aoi = fs[kind] if kind in ("click", "visible", "scroll_to",
                                       "look") else None
            flow.append(FlowStep(id=str(fs.get("id") or f"paso{j}"),
                                 label=str(fs.get("label") or f"Paso {j}"),
                                 kind=kind, aoi=str(aoi) if aoi else None))
        title = str(t.get("title", f"Tarea {tid}")).strip()
        instr = str(t.get("instruction", "")).strip()
        if not instr:
            errors.append(f"{ctx}: falta 'instruction'")
        st.tasks.append(Task(
            id=tid, title=title, instruction=instr,
            target_aoi=t.get("target_aoi") or None,
            time_limit_s=_num(t, "time_limit_s", None, errors, ctx, 5),
            start_url=t.get("start_url") or None,
            background=t.get("background") or None,
            page_screenshot=t.get("page_screenshot") or None,
            flow=flow,
        ))

    # ── AOI ──────────────────────────────────────────────────────────────
    aoi_ids = set()
    for i, a in enumerate(d.get("aois") or [], start=1):
        ctx = f"aois[{i}]"
        aid = str(a.get("id", "")).strip()
        if not _ID_RE.match(aid):
            errors.append(f"{ctx}: 'id' inválido (letras, números, - y _)")
            continue
        if aid in aoi_ids:
            errors.append(f"{ctx}: id '{aid}' repetido")
        aoi_ids.add(aid)
        sel = a.get("selector") or None
        raw_text = a.get("text")
        if isinstance(raw_text, (list, tuple)):
            text = [str(x).strip() for x in raw_text if str(x).strip()] or None
        else:
            text = [str(raw_text).strip()] if str(raw_text or "").strip() else None
        rect = a.get("rect")
        opens = str(a.get("opens") or "").strip() or None
        if sel is None and text is None and rect is None and opens is None:
            errors.append(f"{ctx} ('{aid}'): define 'selector', 'text' o 'rect'")
        rect_t = None
        if rect is not None:
            try:
                r = [float(v) for v in rect]
                if len(r) != 4 or r[2] <= 0 or r[3] <= 0:
                    raise ValueError
                rect_t = (r[0], r[1], r[2], r[3])
            except (TypeError, ValueError):
                errors.append(f"{ctx} ('{aid}'): 'rect' debe ser "
                              "[x, y, ancho, alto] con ancho y alto > 0")
        space = str(a.get("space", "viewport")).lower()
        if space not in ("viewport", "page"):
            errors.append(f"{ctx} ('{aid}'): 'space' debe ser viewport o page")
            space = "viewport"
        tasks = a.get("tasks")
        if tasks is not None:
            try:
                tasks = [int(x) for x in tasks]
            except (TypeError, ValueError):
                errors.append(f"{ctx} ('{aid}'): 'tasks' debe ser una lista de enteros")
                tasks = None
        st.aois.append(AOIDef(
            id=aid, label=str(a.get("label", aid)), tasks=tasks,
            selector=sel, text=text, opens=opens, rect=rect_t, space=space,
            url_contains=a.get("url_contains") or None))

    # referencias cruzadas
    for t in st.tasks:
        if t.target_aoi and t.target_aoi not in aoi_ids:
            errors.append(f"tarea {t.id}: target_aoi '{t.target_aoi}' "
                          "no existe en 'aois'")
    for t in st.tasks:
        for fs in t.flow:
            if fs.aoi and fs.aoi not in aoi_ids:
                errors.append(f"tarea {t.id}: el paso '{fs.id}' usa el AOI "
                              f"'{fs.aoi}', que no existe en 'aois'")
    for a in st.aois:
        for tid in (a.tasks or []):
            if tid not in seen_ids:
                errors.append(f"AOI '{a.id}': hace referencia a la tarea "
                              f"{tid}, que no existe")

    # ── calidad / análisis / opciones ────────────────────────────────────
    q = d.get("quality", {}) or {}
    rules = QualityRules()
    for key in ("min_face_pct", "min_sample_hz", "warn_sample_hz",
                "max_cal_rms_px", "max_validation_px", "min_duration_s",
                "max_blink_rate"):
        v = _num(q, key, getattr(rules, key), errors, "quality", 0)
        setattr(rules, key, float(v))
    rules.min_tasks_done = int(_num(q, "min_tasks_done",
                                    rules.min_tasks_done, errors,
                                    "quality", 0) or 0)
    st.quality = rules

    an = d.get("analysis", {}) or {}
    st.aoi_margin_px = float(_num(an, "aoi_margin_px", 0.0, errors, "analysis", 0) or 0.0)
    st.min_fixation_ms = float(_num(an, "min_fixation_ms", 150.0, errors, "analysis", 50) or 150.0)
    st.heatmap_sigma_px = float(_num(an, "heatmap_sigma_px", 55.0, errors, "analysis", 5) or 55.0)
    st.click_correction = bool(an.get("click_correction", True))

    st.show_gaze_overlay = bool(d.get("show_gaze_overlay", False))
    panel = d.get("task_panel", {}) or {}
    pos = str(panel.get("position", "bottom")).lower()
    if pos not in ("bottom", "top", "bottom-right"):
        errors.append("task_panel.position debe ser bottom, top o bottom-right")
        pos = "bottom"
    st.task_panel_position = pos
    st.capture_screenshots = bool(d.get("capture_screenshots", True))
    st.page_capture = bool(d.get("page_capture", True))
    st.final_validation = bool(d.get("final_validation", True))
    st.review_at_end = bool(d.get("review_at_end", False))

    br = d.get("bridge", {}) or {}
    st.bridge_port = int(_num(br, "port", 4243, errors, "bridge", 1024) or 4243)
    st.bridge_hosts = [str(h) for h in (br.get("hosts") or [])]

    g = d.get("groups") or []
    st.groups = [str(x) for x in g]

    # Marcadores sin completar: no impiden cargar el estudio (sirve para
    # pilotos), pero la app los muestra para que no lleguen al participante.
    if "REEMPLAZAR" in str(st.url or ""):
        st.pending.append("url del sitio")
    for t in st.tasks:
        if "REEMPLAZAR" in f"{t.title} {t.instruction} {t.start_url or ''}":
            st.pending.append(f"tarea {t.id}")
    for a in st.aois:
        if "REEMPLAZAR" in f"{a.selector or ''} {' '.join(a.text or [])}":
            st.pending.append(f"AOI «{a.id}»")
    if any("REEMPLAZAR" in h for h in st.bridge_hosts):
        st.pending.append("bridge.hosts")

    if errors:
        raise StudyError("Estudio inválido"
                         + (f" ({os.path.basename(path)})" if path else "")
                         + ":\n  • " + "\n  • ".join(errors))
    return st


def load_study(path: str) -> Study:
    """Lee y valida un estudio.  Lanza ``StudyError`` con TODOS los problemas."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except json.JSONDecodeError as e:
        raise StudyError(f"{os.path.basename(path)} no es un JSON válido: {e}")
    except OSError as e:
        raise StudyError(f"No se pudo leer {path}: {e}")
    if not isinstance(d, dict):
        raise StudyError(f"{os.path.basename(path)}: se esperaba un objeto JSON")
    return study_from_dict(d, path)


def scan_studies(directory: str) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """
    Explora una carpeta de estudios.

    Returns:
        ({id_del_estudio: ruta} de los válidos,
         [(archivo, mensaje_de_error)] de los que tienen problemas).
    Los archivos cuyo nombre empieza por ``_`` (plantillas) se ignoran.
    """
    ok: Dict[str, str] = {}
    bad: List[Tuple[str, str]] = []
    if not os.path.isdir(directory):
        return ok, bad
    for fn in sorted(os.listdir(directory)):
        if not fn.lower().endswith(".json") or fn.startswith("_"):
            continue
        p = os.path.join(directory, fn)
        try:
            ok[load_study(p).id] = p
        except StudyError as e:
            bad.append((fn, str(e)))
    return ok, bad


def list_studies(directory: str) -> Dict[str, str]:
    """``{id_del_estudio: ruta}`` de los estudios válidos de una carpeta."""
    return scan_studies(directory)[0]
