"""
et_suite/report_html.py  —  Informe de una sesión en HTML (para el equipo)
=========================================================================
Una página que se abre con doble clic en cualquier navegador, pensada para
leerse sin saber de eye tracking:

  • arriba, si los datos sirven (calidad) y por qué;
  • por cada tarea: resultado, tiempo, los PASOS DEL FLUJO esperado (cuáles
    hizo y en qué segundo), si vio el objetivo, y las imágenes de mapa de
    calor y recorrido de la mirada sobre la página que vio.

Las imágenes se enlazan por nombre (están en la misma carpeta), así que la
carpeta de la sesión se puede mover o comprimir sin romper el informe.
"""

from __future__ import annotations

import html
import os
from typing import List, Optional

from .metrics import Analysis

_CSS = """
:root { --fg:#1d1d1f; --fg2:#5f6368; --bd:#e3e3e3; --ok:#1e8e3e; --bad:#c5221f;
        --warn:#b06000; --acc:#f2b600; --bg:#fafafa; }
* { box-sizing:border-box; }
body { margin:0; font:15px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;
       color:var(--fg); background:var(--bg); }
main { max-width:1100px; margin:0 auto; padding:24px 16px 60px; }
h1 { font-size:24px; margin:0 0 4px; }
h2 { font-size:19px; margin:0 0 8px; }
.sub { color:var(--fg2); margin:0 0 20px; }
.card { background:#fff; border:1px solid var(--bd); border-radius:10px;
        padding:18px 20px; margin:0 0 18px; }
.top { border-top:5px solid var(--acc); }
.badge { display:inline-block; padding:2px 10px; border-radius:99px;
         font-weight:600; font-size:13px; }
.b-ok { background:#e6f4ea; color:var(--ok); }
.b-bad { background:#fce8e6; color:var(--bad); }
.b-warn { background:#fef7e0; color:var(--warn); }
.kpis { display:flex; flex-wrap:wrap; gap:10px 28px; margin:10px 0 4px; }
.kpi b { display:block; font-size:20px; }
.kpi span { color:var(--fg2); font-size:13px; }
ol.flow { list-style:none; padding:0; margin:10px 0; counter-reset:p; }
ol.flow li { padding:6px 10px; border-left:4px solid var(--bd); margin:4px 0;
             background:#fbfbfb; }
ol.flow li.ok { border-color:var(--ok); }
ol.flow li.no { border-color:var(--bad); color:var(--fg2); }
ol.flow .t { float:right; color:var(--fg2); font-variant-numeric:tabular-nums; }
.imgs { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
        gap:12px; margin-top:12px; }
.imgs a { display:block; border:1px solid var(--bd); border-radius:8px;
          overflow:hidden; background:#fff; }
.imgs img { width:100%; height:340px; object-fit:cover; object-position:top;
            display:block; }
.imgs figcaption { padding:6px 10px; font-size:13px; color:var(--fg2); }
ul.w { margin:6px 0 0 18px; padding:0; color:var(--fg2); }
.muted { color:var(--fg2); font-size:13px; }
"""


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt_s(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.1f} s"


def build_html(an: Analysis, images: List[str]) -> str:
    m = an.metrics
    sm = m["session"]
    q = m["quality"]
    meta = an.session.meta or {}
    title = an.study.name if an.study else "Sesión"
    part = meta.get("participant") or an.session.participant
    when = (meta.get("started_at") or "")[:16].replace("T", " ")

    out = [f"<!doctype html><html lang='es'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Informe {_e(part)}</title><style>{_CSS}</style></head>"
           f"<body><main>"
           f"<h1>Informe de la sesión · {_e(part)}</h1>"
           f"<p class='sub'>{_e(title)} · {_e(when)}</p>"]

    # ── calidad ──────────────────────────────────────────────────────────
    badge = ("<span class='badge b-ok'>Datos válidos</span>" if q["valid"]
             else "<span class='badge b-bad'>Datos excluidos</span>")
    err = sm.get("val_post_px") or sm.get("val_pre_px") or sm.get("cal_rms_px")
    out.append("<section class='card top'><h2>¿Sirven estos datos? "
               f"{badge}</h2><div class='kpis'>"
               f"<div class='kpi'><b>{sm['face_pct']:.0f} %</b>"
               "<span>tiempo con la cara detectada</span></div>"
               f"<div class='kpi'><b>{'—' if err is None else f'{err:.0f} px'}</b>"
               "<span>error de precisión de la mirada</span></div>"
               f"<div class='kpi'><b>{sm['mean_hz']:.0f} Hz</b>"
               "<span>muestras por segundo</span></div>"
               f"<div class='kpi'><b>{sm['duration_s'] / 60:.1f} min</b>"
               "<span>duración</span></div></div>")
    notas = [("✗ " + r) for r in q["reasons"]] + [("⚠ " + w)
                                                  for w in q["warnings"]]
    cc = sm.get("click_correction") or {}
    if cc.get("applied"):
        notas.append(f"✓ Mirada corregida con {cc['n']} clics: estaba "
                     f"desplazada ({cc['dx']:+.0f}, {cc['dy']:+.0f}) px; el "
                     f"error en los clics bajó de {cc['err_before_px']:.0f} a "
                     f"{cc['err_after_px']:.0f} px.")
    if notas:
        out.append("<ul class='w'>" + "".join(f"<li>{_e(n)}</li>"
                                              for n in notas) + "</ul>")
    out.append("<p class='muted'>Con webcam la mirada tiene un error típico de "
               "100–250 px: los mapas indican zonas, no palabras exactas.</p>"
               "</section>")

    # ── tareas ───────────────────────────────────────────────────────────
    nombres = {os.path.basename(p): p for p in images}
    for t in m["tasks"]:
        if not m["has_tasks"]:
            break
        tid = t["task"]
        res = t["result_label"]
        cls = "b-ok" if t["success"] else ("b-warn" if t["verified"] is None
                                            and t["outcome"] == "success"
                                            else "b-bad")
        out.append(f"<section class='card'><h2>Tarea {tid} · {_e(t['title'])} "
                   f"<span class='badge {cls}'>{_e(res)}</span></h2>")
        if t["outcome"] == "success" and t["verified"] is None:
            out.append("<p class='muted'>El participante dijo que la terminó; "
                       "falta que el equipo confirme si fue correcta "
                       "(Resultados → Revisar tareas).</p>")
        tgt = ("—" if not t["target_aoi"] else
               ("no lo vio" if t["target_seen"] is False else
                _fmt_s((t["target_ttff_ms"] or 0) / 1000.0)))
        out.append("<div class='kpis'>"
                   f"<div class='kpi'><b>{t['time_on_task_s']:.1f} s</b>"
                   "<span>tiempo en la tarea</span></div>"
                   f"<div class='kpi'><b>{_e(tgt)}</b>"
                   "<span>hasta mirar el objetivo</span></div>"
                   f"<div class='kpi'><b>{_e(t['n_clicks'] if t['n_clicks'] is not None else '—')}</b>"
                   "<span>clics</span></div>"
                   f"<div class='kpi'><b>{t['n_fixations']}</b>"
                   "<span>fijaciones de la mirada</span></div></div>")
        if t.get("flow"):
            out.append(f"<p><b>Camino esperado:</b> hizo {t['flow_done']} de "
                       f"{t['flow_total']} pasos"
                       + (" ✓" if t["flow_complete"] else "") + "</p>"
                       "<ol class='flow'>")
            for i, p in enumerate(t["flow"], start=1):
                out.append(f"<li class='{'ok' if p['done'] else 'no'}'>"
                           f"{'✓' if p['done'] else '✗'} {i}. {_e(p['label'])}"
                           f"<span class='t'>{_fmt_s(p['t_s']) if p['done'] else 'no lo hizo'}"
                           "</span></li>")
            out.append("</ol>")
        if len(t.get("hosts") or []) > 1:
            out.append(f"<p class='muted'>Sitios visitados: "
                       f"{_e(', '.join(t['hosts']))}</p>")
        figs = sorted(n for n in nombres if f"_tarea{tid}_" in n)
        if figs:
            out.append("<div class='imgs'>")
            for n in figs:
                cap = ("Mapa de calor: dónde y cuánto miró" if "calor" in n
                       or "dwell" in n else
                       "Recorrido: en qué orden miró" if "recorrido" in n
                       or "scanpath" in n else n)
                out.append(f"<figure style='margin:0'><a href='{_e(n)}' "
                           f"target='_blank'><img src='{_e(n)}' alt='{_e(cap)}'>"
                           f"<figcaption>{_e(cap)}</figcaption></a></figure>")
            out.append("</div>")
        else:
            out.append("<p class='muted'>Sin imágenes: no hubo fijaciones "
                       "en esta tarea.</p>")
        out.append("</section>")

    out.append("<p class='muted'>Haz clic en una imagen para verla completa. "
               "Generado por el prototipo de eye tracking.</p></main></body></html>")
    return "".join(out)


def write_html(an: Analysis, images: List[str], csv_path: str) -> str:
    path = f"{os.path.splitext(csv_path)[0]}_informe.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_html(an, images))
    return path
