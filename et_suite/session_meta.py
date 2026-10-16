"""
et_suite/session_meta.py  —  Metadatos de cada sesión
=======================================================
Junto a cada CSV de mirada se guarda un ``<sesion>.meta.json`` con lo que el
CSV no puede contener: quién participó (sólo un CÓDIGO, nunca el nombre),
si aceptó el consentimiento, con qué equipo, cuánto error tuvo la
calibración, cómo terminó cada tarea y si la sesión es válida para el
análisis.

Archivos de una sesión (todos con el mismo prefijo)::

    P01_20261003_101500.csv         mirada, muestra a muestra
    P01_20261003_101500.meta.json   metadatos (este módulo)
    P01_20261003_101500.web.json    eventos del navegador (si hubo puente)
    P01_20261003_101500_shots/      capturas de pantalla por tarea
    P01_20261003_101500_informe.txt informe generado
    P01_20261003_101500_*.png       heatmaps generados
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import re
import sys
from typing import Dict, List, Optional

META_SCHEMA = 2

# Un código de participante válido: nada con espacios ni acentos, para que no
# se cuelen nombres reales por accidente y sea seguro como nombre de archivo.
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,19}$")

TASK_OUTCOMES = ("success", "abandoned", "timeout", "interrupted", "skipped")
OUTCOME_LABELS = {
    "success": "completada",
    "abandoned": "abandonada",
    "timeout": "tiempo agotado",
    "interrupted": "interrumpida",
    "skipped": "no realizada",
    "fail": "fallida",          # veredicto del facilitador (verified)
}


# ── rutas hermanas ──────────────────────────────────────────────────────────

def _base(csv_path: str) -> str:
    return os.path.splitext(csv_path)[0]


def meta_path_for(csv_path: str) -> str:
    return _base(csv_path) + ".meta.json"


def web_path_for(csv_path: str) -> str:
    return _base(csv_path) + ".web.json"


def shots_dir_for(csv_path: str) -> str:
    return _base(csv_path) + "_shots"


# ── construcción ────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def valid_participant_code(code: str) -> bool:
    return bool(CODE_RE.match(code or ""))


def next_participant_code(directory: str, prefix: str = "P") -> str:
    """Siguiente código libre (P01, P02…) mirando las sesiones ya grabadas."""
    used = set()
    if os.path.isdir(directory):
        for fn in os.listdir(directory):
            m = re.match(rf"^{re.escape(prefix)}(\d+)_", fn)
            if m:
                used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"{prefix}{n:02d}"


def environment_info(screen_w: int, screen_h: int,
                     screen_width_cm: Optional[float],
                     screen_cm_source: str, distance_cm: float,
                     camera_label: str = "", camera_res: str = "") -> Dict:
    return {
        "os": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
        "screen_w": screen_w, "screen_h": screen_h,
        "screen_width_cm": screen_width_cm,
        "screen_cm_source": screen_cm_source,
        "distance_cm": distance_cm,
        "camera": camera_label, "camera_res": camera_res,
    }


def new_meta(study_id: str, study_name: str, study_version: str,
             participant: str, group: str = "", wears_glasses: bool = False,
             consent: Optional[Dict] = None, env: Optional[Dict] = None) -> Dict:
    return {
        "schema": META_SCHEMA,
        "study_id": study_id,
        "study_name": study_name,
        "study_version": study_version,
        "participant": participant,
        "group": group,
        "wears_glasses": bool(wears_glasses),
        "consent": consent or {"accepted": False},
        "started_at": now_iso(),
        "ended_at": None,
        "env": env or {},
        "calibration": {},
        "validation": {},
        "tasks": [],
        "task_panel_rect": None,
        "overlay_shown": False,
        "notes": "",
        "quality": {},
        "csv": "",
    }


# ── persistencia ────────────────────────────────────────────────────────────

def save_meta(csv_path: str, meta: Dict) -> str:
    """Escribe el .meta.json junto al CSV.  Devuelve la ruta."""
    path = meta_path_for(csv_path)
    meta = dict(meta)
    meta["csv"] = os.path.basename(csv_path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)            # escritura atómica
    return path


def load_meta(csv_path: str) -> Optional[Dict]:
    """Lee el .meta.json de una sesión; ``None`` si no existe o está roto."""
    path = meta_path_for(csv_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def load_web(csv_path: str) -> List[Dict]:
    """Eventos del navegador de una sesión (lista vacía si no hubo)."""
    path = web_path_for(csv_path)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        ev = d.get("events", []) if isinstance(d, dict) else []
        return [e for e in ev if isinstance(e, dict)]
    except (OSError, json.JSONDecodeError):
        return []


def find_sessions(directory: str, study_id: Optional[str] = None) -> List[str]:
    """
    Rutas de los CSV de sesión de una carpeta (recursivo un nivel), de la
    más reciente a la más antigua.  Con ``study_id`` sólo las de ese estudio.
    """
    out: List[str] = []
    if not os.path.isdir(directory):
        return out
    for root, _dirs, files in os.walk(directory):
        for fn in files:
            if not fn.lower().endswith(".csv"):
                continue
            p = os.path.join(root, fn)
            if study_id is not None:
                m = load_meta(p)
                if not m or m.get("study_id") != study_id:
                    continue
            out.append(p)
    out.sort(key=os.path.getmtime, reverse=True)
    return out


def task_outcome_label(outcome: Optional[str]) -> str:
    return OUTCOME_LABELS.get(outcome or "", outcome or "—")
