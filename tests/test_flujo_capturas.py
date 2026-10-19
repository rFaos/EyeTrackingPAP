"""Pruebas del flujo de tareas (pasos del diagrama) y de los heatmaps sobre
las capturas del navegador integrado."""

import os
import shutil
import tempfile
import unittest

import cv2
import numpy as np

from helpers import write_session

from et_suite.heatmap_generator import HeatmapGenerator
from et_suite.metrics import analyze_csv
from et_suite.session_meta import shots_dir_for
from et_suite.study import StudyError, study_from_dict

URL = "https://portal.test/"
T0 = 1_000_000.0
# write_session: 2 s de lectura del panel antes de cada tarea
T1 = T0 + 2000          # inicio tarea 1 (dura 2 s)
T2 = T1 + 2000 + 2000   # inicio tarea 2 (dura 2 s)

STUDY = study_from_dict({
    "id": "flujo", "url": URL,
    "tasks": [
        {"id": 1, "instruction": "Entra a Trayecto", "target_aoi": "target",
         "flow": [
             {"id": "c1", "label": "Clic en cuadrícula", "click": "menu_btn"},
             {"id": "v1", "label": "Se abre el menú", "visible": "menu"},
             {"id": "c2", "label": "Clic en Trayecto", "click": "target"},
             {"id": "s1", "label": "Sale a Trayecto", "leave_site": True}]},
        {"id": 2, "instruction": "Mis solicitudes", "target_aoi": "panel",
         "flow": [
             {"id": "b1", "label": "Baja al panel", "scroll_to": "panel"},
             {"id": "m1", "label": "Mira el panel", "look": "panel"},
             {"id": "n1", "label": "Cambia de página", "navigate": True}]},
    ],
    "aois": [
        {"id": "menu_btn", "text": "Apps", "selector": "header button"},
        {"id": "menu", "text": "Trayecto UDEM", "selector": "[role=dialog]"},
        {"id": "target", "text": "Trayecto UDEM", "selector": "a"},
        {"id": "panel", "text": "Mis Solicitudes", "selector": "section"},
    ],
})


def _layout(t, menu_vis):
    return {"t_ms": t, "k": "layout", "url": URL, "pw": 1920, "ph": 4000,
            "aois": [
                {"id": "menu_btn", "x": 80, "y": 30, "w": 60, "h": 40,
                 "fixed": False, "vis": True},
                {"id": "menu", "x": 1600, "y": 60, "w": 300, "h": 200,
                 "fixed": True, "vis": menu_vis},
                {"id": "target", "x": 1620, "y": 100, "w": 200, "h": 40,
                 "fixed": True, "vis": menu_vis},
                {"id": "panel", "x": 0, "y": 3000, "w": 1900, "h": 600,
                 "fixed": False, "vis": True}]}


def _vp(t, sy):
    return {"t_ms": t, "k": "vp", "url": URL, "sx": 0, "sy": sy, "iw": 1920,
            "ih": 980, "ow": 1920, "oh": 1080, "wx": 0, "wy": 0, "sw": 1920,
            "sh": 1080, "dpr": 1, "pw": 1920, "ph": 4000, "mx": 0, "my": 100}


def _shot(t, task, n, sy):
    return {"t_ms": t, "k": "shot", "file": f"t{task}_{n:03d}.jpg",
            "task": task, "url": URL, "sx": 0, "sy": sy, "iw": 1920,
            "ih": 980, "dpr": 1}


WEB = [
    _vp(T0, 0), _layout(T0, False),
    {"t_ms": T1 + 500, "k": "click", "url": URL, "x": 100, "y": 50,
     "px": 100, "py": 50, "tag": "button", "txt": ""},
    _layout(T1 + 800, True),
    _shot(T1 + 900, 1, 1, 0),
    {"t_ms": T1 + 1200, "k": "click", "url": URL, "x": 1700, "y": 120,
     "px": 1700, "py": 120, "tag": "a", "txt": "TU Trayecto UDEM"},
    {"t_ms": T1 + 1500, "k": "nav", "url": "https://otro.com/",
     "externo": True},
    {"t_ms": T2 - 500, "k": "nav", "url": URL},
    _layout(T2 - 400, False),
    _vp(T2 + 100, 0), _shot(T2 + 200, 2, 1, 0),
    _vp(T2 + 480, 2800), _shot(T2 + 700, 2, 2, 2800),
]


def _scroll(x, y, t):
    sy = 2800 if t >= T2 + 480 else 0
    return {"sx": 0, "sy": sy, "px": x, "py": y - 100 + sy}


class TestFlujoYCapturas(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="etflujo_")
        self.csv = write_session(
            self.tmp, "P01", STUDY,
            {1: [(110, 150, 1.0), (1700, 220, 1.0)],
             2: [(960, 300, 0.5), (960, 600, 1.5)]},
            web=WEB, scroll_fn=_scroll, noise=8.0)
        d = shots_dir_for(self.csv)
        os.makedirs(d)
        for name, col in (("t1_001.jpg", (200, 120, 40)),
                          ("t2_001.jpg", (40, 160, 40)),
                          ("t2_002.jpg", (40, 40, 200))):
            img = np.full((980, 1920, 3), col, dtype=np.uint8)
            cv2.imwrite(os.path.join(d, name), img)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_flow_steps_detected_in_order(self):
        an = analyze_csv(self.csv, STUDY)
        t = {x["task"]: x for x in an.metrics["tasks"]}
        f1 = t[1]["flow"]
        self.assertEqual([p["done"] for p in f1], [True] * 4, f1)
        tiempos = [p["t_s"] for p in f1]
        self.assertEqual(tiempos, sorted(tiempos))
        self.assertAlmostEqual(tiempos[0], 0.5, delta=0.1)
        self.assertTrue(t[1]["flow_complete"])
        f2 = {p["id"]: p for p in t[2]["flow"]}
        self.assertTrue(f2["b1"]["done"])
        self.assertTrue(f2["m1"]["done"])
        self.assertFalse(f2["n1"]["done"])          # no cambió de página
        self.assertEqual((t[2]["flow_done"], t[2]["flow_total"]), (2, 3))
        self.assertFalse(t[2]["flow_complete"])

    def test_heatmaps_over_captured_pages(self):
        an = analyze_csv(self.csv, STUDY)
        gen = HeatmapGenerator(1920, 1080)
        files = gen.session_images(an, os.path.splitext(self.csv)[0])
        names = sorted(os.path.basename(f) for f in files)
        self.assertIn("P01_20261003_100000_tarea1_vista1_calor.png", names)
        self.assertIn("P01_20261003_100000_tarea2_vista1_recorrido.png", names)
        # con capturas ya no se generan los mapas sobre fondo blanco
        self.assertFalse(any(n.endswith("_density.png") for n in names))
        img = cv2.imread([f for f in files
                          if f.endswith("tarea2_vista1_calor.png")][0])
        # página reconstruida: arriba la captura verde, abajo la roja (scroll)
        h = img.shape[0]
        self.assertGreater(img[h // 6 + 100, 50][1], 120)   # verde
        self.assertGreater(img[h - 30, 50][2], 150)         # rojo (BGR)

    def test_group_heatmap_over_page_and_flow_summary(self):
        from et_suite.group_analysis import analyze_study
        out = os.path.join(self.tmp, "res")
        analyze_study(STUDY, self.tmp, out, include_invalid=True)
        names = os.listdir(out)
        self.assertIn("heatmap_tarea2_vista1_grupo.png", names)
        with open(os.path.join(out, "resumen_flujo.csv"),
                  encoding="utf-8-sig") as f:
            txt = f.read()
        self.assertIn("Baja al panel", txt)
        with open(os.path.join(out, "resumen_grupal.txt"),
                  encoding="utf-8") as f:
            self.assertIn("Camino esperado", f.read())

    def test_html_report_lists_flow_and_images(self):
        from et_suite.report_html import write_html
        an = analyze_csv(self.csv, STUDY)
        files = HeatmapGenerator(1920, 1080).session_images(
            an, os.path.splitext(self.csv)[0])
        with open(write_html(an, files, self.csv), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("Clic en cuadrícula", html)
        self.assertIn("hizo 4 de 4 pasos", html)
        self.assertIn("_tarea1_vista1_calor.png", html)

    def test_click_correction_removes_constant_offset(self):
        # Mirada desplazada (+300, -250) px respecto de 4 clics: se corrige
        clicks = [(400, 300), (1400, 300), (400, 800), (1400, 800)]
        script = {1: [(x + 300, y + 100 - 250, 1.0) for x, y in clicks]}
        web = [_vp(T0, 0)]
        t = T1
        for x, y in clicks:
            t += 1000
            web.append({"t_ms": t - 60, "k": "click", "url": URL,
                        "x": x, "y": y, "px": x, "py": y, "txt": ""})
        d = os.path.join(self.tmp, "corr")
        csv = write_session(d, "P02", STUDY, script, web=web, noise=10.0)
        an = analyze_csv(csv, STUDY)
        cc = an.metrics["session"]["click_correction"]
        self.assertTrue(cc["applied"], cc)
        self.assertAlmostEqual(cc["dx"], 300, delta=40)
        self.assertAlmostEqual(cc["dy"], -250, delta=40)
        self.assertLess(cc["err_after_px"], 60)
        # las fijaciones quedan donde se hizo clic (en pantalla)
        fx = sorted(an.fixes_for_task(1), key=lambda f: f.start_ms)
        self.assertAlmostEqual(fx[0].cx, 400, delta=50)
        self.assertAlmostEqual(fx[0].cy, 300 + 100, delta=50)

    def test_flow_with_unknown_aoi_is_rejected(self):
        with self.assertRaises(StudyError):
            study_from_dict({"id": "x", "tasks": [
                {"id": 1, "instruction": "i",
                 "flow": [{"click": "no_existe"}]}]})


if __name__ == "__main__":
    unittest.main()
