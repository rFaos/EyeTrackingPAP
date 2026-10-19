"""
Prueba de integración de la aplicación completa (necesita pantalla).

Un participante virtual (``FakeTracker``) pasa por TODO el flujo real:
calibración → verificación → consentimiento → 3 tareas (una terminada, una
abandonada, una por tiempo agotado) con eventos del navegador simulados por
HTTP → verificación final → revisión del facilitador → informe, heatmaps y
análisis grupal.  Sólo se acortan los tiempos y se sustituyen los cuadros de
diálogo bloqueantes.

    python -m unittest tests.test_app_flow -v      (desde la raíz del proyecto)
"""

import gc
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request

import tkinter as tk

from helpers import ROOT, FakeTracker

sys.path.insert(0, ROOT)

import main_app                                                  # noqa: E402
from et_suite.calibration_engine import CalibrationEngine        # noqa: E402
from et_suite.gaze_tracker import SCREEN_H, SCREEN_W             # noqa: E402
from et_suite.session_meta import load_meta, load_web            # noqa: E402
from et_suite.validation_engine import ValidationEngine          # noqa: E402

PORT = 4299
SW, SH = SCREEN_W, SCREEN_H


def pos(fx: float, fy: float):
    """Posición en pantalla como fracción (independiente de la resolución)."""
    return (fx * SW, fy * SH)


STUDY = {
    "id": "t_app", "name": "Estudio de integración", "version": "1.0",
    "url": None,
    "tasks": [
        {"id": 1, "title": "Encontrar B", "instruction": "Mira la caja B",
         "target_aoi": "B"},
        {"id": 2, "title": "Encontrar C", "instruction": "Mira la caja C",
         "target_aoi": "C"},
        {"id": 3, "title": "Leer el FAQ", "instruction": "Baja al FAQ",
         "target_aoi": "faq", "time_limit_s": 5},
    ],
    "aois": [
        {"id": "A", "label": "Caja A", "rect": [96, 108, 384, 216]},
        {"id": "B", "label": "Caja B", "rect": [1344, 108, 384, 216]},
        {"id": "C", "label": "Caja C", "rect": [192, 756, 576, 216]},
        {"id": "faq", "label": "FAQ", "selector": "#faq", "tasks": [3]},
    ],
    "quality": {"min_duration_s": 5, "min_sample_hz": 8},
    "capture_screenshots": False,
    "final_validation": True,
    "review_at_end": True,
    "groups": ["A", "B"],
    "bridge": {"port": PORT, "hosts": ["127.0.0.1"]},
}

VP = dict(sx=0, iw=SW, ih=SH - 100, ow=SW, oh=SH, wx=0, wy=0, sw=SW,
          url="http://127.0.0.1/p")


def post(kind: str, **kw) -> dict:
    body = json.dumps({"k": kind, "url": "http://127.0.0.1/p", **kw}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/e", data=body,
                                 method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=3).read())


class AppCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="etapp_")
        self.sdir = os.path.join(self.tmp, "studies")
        os.makedirs(self.sdir)
        with open(os.path.join(self.sdir, "t_app.json"), "w",
                  encoding="utf-8") as f:
            json.dump(STUDY, f)
        # tiempos cortos
        self._saved = (CalibrationEngine.WAIT_SEC, CalibrationEngine.RECORD_SEC,
                       ValidationEngine.WAIT_SEC, ValidationEngine.RECORD_SEC)
        CalibrationEngine.WAIT_SEC, CalibrationEngine.RECORD_SEC = 0.25, 0.5
        ValidationEngine.WAIT_SEC, ValidationEngine.RECORD_SEC = 0.25, 0.5

        self.tr = FakeTracker(hz=30, screen=(SW, SH))
        self.state = {"pos": pos(0.5, 0.5)}

        def gaze():
            eng = self.app._active_engine if hasattr(self, "app") else None
            if eng is not None and not getattr(eng, "_closed", True):
                try:
                    return eng._target_px()
                except Exception:
                    pass
            return self.state["pos"]
        self.tr.gaze_fn = gaze
        self.app = main_app.App(
            tracker=self.tr, session_dir=os.path.join(self.tmp, "sessions"),
            studies_dir=self.sdir, results_dir=os.path.join(self.tmp, "res"),
            hide_main_during_session=False)
        self.tr.start()
        self.msgs = []
        a = self.app
        a._info = lambda t, m: self.msgs.append(("info", t, m))
        a._warn = lambda t, m: self.msgs.append(("warn", t, m))
        a._error = lambda t, m: self.msgs.append(("error", t, m))
        self.yes = True
        a._yesno = lambda t, m: (self.msgs.append(("yesno", t, m)) or self.yes)
        a._consent_dialog = lambda st, code: {"accepted": True, "version": "1",
                                              "at": "2026-10-03T10:00:00"}
        self.review = {"verified": {1: "success", 2: "fail", 3: "fail"},
                       "notes": {1: "ok", 2: "", 3: "se acabó el tiempo"},
                       "session_notes": "prueba automática"}
        a._review_dialog = lambda tasks: self.review

    def tearDown(self):
        try:
            self.app._session_active = False
            self.app._on_close()
        except Exception:
            pass
        self.tr.stop()
        (CalibrationEngine.WAIT_SEC, CalibrationEngine.RECORD_SEC,
         ValidationEngine.WAIT_SEC, ValidationEngine.RECORD_SEC) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.app = None
        gc.collect()      # las variables Tk deben liberarse en el hilo principal

    # ── utilidades ───────────────────────────────────────────────────────
    def pump(self, secs: float = 0.0, until=None, timeout: float = 30.0):
        end = time.time() + (secs if until is None else timeout)
        while time.time() < end:
            self.app.root.update()
            if until is not None and until():
                return True
            time.sleep(0.01)
        if until is not None:
            self.fail(f"timeout esperando condición ({timeout}s); "
                      f"mensajes: {self.msgs[-3:]}")
        return True

    def calibrate(self, code="P01", group="A"):
        a = self.app
        a._var_code.set(code)
        a._var_group.set(group)
        a._var_method.set("polynomial")
        a._do_calibrate()
        self.pump(until=lambda: a._cal_code == code and a._val_pre is not None,
                  timeout=90)


# ════════════════════════════════════════════════════════════════════════════
class TestPreparation(AppCase):
    def test_study_loaded_and_ui_state(self):
        a = self.app
        self.assertEqual(a.study.id, "t_app")
        self.assertEqual(a._var_method.get(), "geometric")   # defecto correcto
        self.assertEqual(str(a._cbo_group.cget("state")), "readonly")
        self.assertEqual(a._var_code.get(), "")       # sin código propuesto
        self.assertIsInstance(a._btn_start, main_app.FlatButton)
        self.assertEqual(a.nb.index("end"), 3)        # Guía, Prueba, Resultados

    def test_cannot_start_without_calibration_or_group(self):
        a = self.app
        a._var_code.set("P01")
        a._var_group.set("")
        a._do_start()
        self.assertFalse(a._session_active)
        warn = [m for m in self.msgs if m[0] == "warn"][-1][2]
        self.assertIn("Calibra", warn)
        self.assertIn("grupo", warn)

    def test_invalid_participant_code_blocks_calibration(self):
        a = self.app
        a._var_code.set("Juan Pérez")
        a._do_calibrate()
        self.assertTrue(any("código" in m[2].lower() for m in self.msgs))
        self.assertIsNone(a._cal_code)

    def test_calibration_then_automatic_validation(self):
        self.calibrate()
        a = self.app
        self.assertTrue(a.calibrator.is_calibrated)
        self.assertLess(a.calibrator.rms_error_px, 60)
        self.assertLess(a._val_pre["mean_px"], 80)
        self.assertEqual(a._val_pre["n_points"], 5)
        blocking, items = a._preflight()
        self.assertEqual(blocking, [])
        self.assertIn("P01", items["cal"][0])

    def test_changing_participant_requires_new_calibration(self):
        self.calibrate("P01")
        self.app._var_code.set("P02")
        blocking, _ = self.app._preflight()
        self.assertTrue(any("Calibra" in b for b in blocking))

    def test_rehearsal_mode_allows_uncalibrated_and_flags_session(self):
        a = self.app
        a._var_code.set("P09")
        a._var_group.set("B")
        a._var_rehearsal.set(True)
        self.assertEqual(a._preflight()[0], [])
        a._do_start()
        self.assertTrue(a._session_active)
        self.pump(0.3)
        a.panel.btn_begin.invoke()
        self.pump(0.6)
        a._finish_session(aborted=True)
        self.pump(until=lambda: not a._finishing, timeout=30)
        meta = load_meta(a._last_csv)
        self.assertTrue(meta["rehearsal"])
        self.assertTrue(meta["aborted"])
        self.assertFalse(meta["quality"]["valid"])
        self.assertTrue(any("ensayo" in r for r in meta["quality"]["reasons"]))


# ════════════════════════════════════════════════════════════════════════════
class TestFullSession(AppCase):
    def test_three_task_session_end_to_end(self):
        a = self.app
        self.calibrate("P01", "A")

        # ── inicio ───────────────────────────────────────────────────────
        a._do_start()
        self.assertTrue(a._session_active)
        self.assertIsNotNone(a.panel)
        self.assertTrue(a._btn_stop.cget("text").endswith("Terminar"))
        self.assertEqual(post("vp", sy=0, **VP)["rec"], True)
        # el puente dice qué tarea está en curso (0 antes de "Comenzar")
        self.assertEqual(post("vp", sy=0, **VP)["task"], 0)

        # ── tarea 1: A → B, termina bien ─────────────────────────────────
        self.pump(0.5)                               # lee el panel
        a.panel.btn_begin.invoke()
        self.assertTrue(a._task_running)
        self.assertEqual(post("vp", sy=0, **VP)["task"], 1)
        self.state["pos"] = pos(0.15, 0.2)
        self.pump(1.2)
        self.state["pos"] = pos(0.75, 0.2)
        self.pump(1.2)
        post("click", x=10, y=20, tag="BUTTON", text="Empezar")
        a.panel.btn_done.invoke()
        self.assertFalse(a._task_running)
        self.assertEqual(a._task_idx, 1)

        # ── tarea 2: sólo mira C y se rinde ──────────────────────────────
        self.state["pos"] = pos(0.5, 0.5)
        self.pump(0.4)
        a.panel.btn_begin.invoke()
        self.state["pos"] = pos(0.2, 0.8)
        self.pump(1.4)
        a.panel.btn_giveup.invoke()

        # ── tarea 3: hay que bajar; se agota el tiempo ───────────────────
        faq = dict(id="faq", x=0.1 * SW, y=1000 + 0.3 * SH, w=0.8 * SW,
                   h=0.5 * SH, vis=True, fixed=False)
        post("layout", aois=[faq])
        post("vp", sy=0, **VP)
        a.panel.btn_begin.invoke()
        self.state["pos"] = pos(0.5, 0.2)
        self.pump(1.0)
        post("vp", sy=1000, **VP)                    # el participante hace scroll
        self.state["pos"] = pos(0.5, 0.65)
        t_task3 = time.time()
        # no pulsa nada: a los 5 s la tarea termina sola y acaba la sesión
        self.pump(until=lambda: not a._session_active, timeout=15)
        self.assertGreaterEqual(time.time() - t_task3, 3.5)

        # ── cierre: verificación final, revisión, análisis ───────────────
        self.pump(until=lambda: (not a._finishing)
                  and a._lbl_session.cget("text").startswith("Sesión guardada"),
                  timeout=60)
        csv_path = a._last_csv
        base = os.path.splitext(csv_path)[0]
        for ext in (".csv", ".meta.json", ".web.json", "_informe.txt"):
            self.assertTrue(os.path.exists(base + ext), ext)
        pngs = [f for f in os.listdir(os.path.dirname(csv_path))
                if f.endswith(".png")]
        self.assertGreaterEqual(len(pngs), 6)

        meta = load_meta(csv_path)
        self.assertEqual(meta["participant"], "P01")
        self.assertEqual(meta["group"], "A")
        self.assertTrue(meta["consent"]["accepted"])
        self.assertFalse(meta.get("rehearsal"))
        self.assertFalse(meta.get("aborted"))
        self.assertLess(meta["calibration"]["rms_px"], 60)
        self.assertIn("pre", meta["validation"])
        self.assertIn("post", meta["validation"])             # verificación final
        outs = [t["outcome"] for t in meta["tasks"]]
        self.assertEqual(outs, ["success", "abandoned", "timeout"])
        self.assertEqual([t["verified"] for t in meta["tasks"]],
                         ["success", "fail", "fail"])
        self.assertEqual(meta["notes"], "prueba automática")
        self.assertTrue(meta["task_panel_rect"])
        self.assertTrue(meta["quality"]["valid"], meta["quality"])
        self.assertTrue(any(e["k"] == "click" for e in load_web(csv_path)))

        # ── el análisis ve lo que pasó ──────────────────────────────────
        from et_suite.metrics import analyze_csv
        an = analyze_csv(csv_path, a.study)
        t = {x["task"]: x for x in an.metrics["tasks"]}
        self.assertTrue(t[1]["target_seen"])                  # B
        self.assertTrue(t[1]["success"])
        self.assertFalse(t[2]["success"])                     # verificado "fail"
        self.assertEqual(t[2]["outcome"], "abandoned")
        self.assertEqual(t[3]["outcome"], "timeout")
        self.assertIn("A", t[1]["aoi_sequence"])
        self.assertEqual(t[1]["aoi_sequence"][:2], ["A", "B"])
        # tras el scroll, el FAQ (por selector, bajo el pliegue) fue visto
        self.assertTrue(t[3]["target_seen"], t[3])
        self.assertGreaterEqual(t[1]["n_clicks"], 1)
        # la calibración quedó marcada como pendiente para el siguiente
        self.assertIsNone(a._cal_code)
        self.assertTrue(a._btn_start.cget("text").endswith("Empezar la prueba"))
        self.assertEqual(a._var_code.get(), "")       # el siguiente escribe el suyo
        self.assertEqual(a._lst.size(), 1)
        self.assertIn("válida", a._lst.get(0))

        # ── análisis grupal desde la interfaz ───────────────────────────
        a._open_path = lambda p: None
        a._do_group()
        self.pump(until=lambda: a._lbl_group.cget("text") != "", timeout=60)
        res = os.path.join(self.tmp, "res", "t_app")
        for f in ("sesiones.csv", "tareas.csv", "aoi.csv", "resumen_grupal.txt"):
            self.assertTrue(os.path.exists(os.path.join(res, f)), f)
        self.assertIn("1 válidas", a._lbl_group.cget("text"))

    def test_emergency_stop_saves_partial_session(self):
        a = self.app
        self.calibrate("P05", "B")
        a._do_start()
        self.pump(0.3)
        a.panel.btn_begin.invoke()
        self.state["pos"] = pos(0.15, 0.2)
        self.pump(1.5)
        a._emergency_stop()                       # _yesno responde sí
        self.pump(until=lambda: not a._finishing
                  and a._lbl_session.cget("text").startswith("Sesión guardada"),
                  timeout=40)
        meta = load_meta(a._last_csv)
        self.assertTrue(meta["aborted"])
        self.assertEqual(meta["tasks"][0]["outcome"], "interrupted")
        self.assertEqual(meta["tasks"][1]["outcome"], None)    # sin hacer
        self.assertNotIn("post", meta["validation"])            # no hay final
        self.assertTrue(os.path.exists(os.path.splitext(a._last_csv)[0]
                                       + "_informe.txt"))

    def test_closing_window_mid_session_saves_data(self):
        a = self.app
        self.calibrate("P06", "A")
        a._do_start()
        self.pump(0.3)
        a.panel.btn_begin.invoke()
        self.pump(1.0)
        csv = a._last_csv
        a._on_close()
        meta = load_meta(csv)
        self.assertTrue(meta["aborted"])
        self.assertGreater(os.path.getsize(csv), 500)

    def test_declined_consent_records_nothing(self):
        a = self.app
        self.calibrate("P07", "A")
        a._consent_dialog = lambda st, code: None
        a._do_start()
        self.assertFalse(a._session_active)
        folder = a._study_session_dir()
        self.assertEqual([f for f in os.listdir(folder)
                          if f.endswith(".csv")], [])

    def test_duplicate_code_asks_for_confirmation(self):
        a = self.app
        self.calibrate("P01", "A")
        a._do_start()
        self.pump(0.3)
        a._finish_session(aborted=True)
        self.pump(until=lambda: not a._finishing
                  and a._lbl_session.cget("text").startswith("Sesión guardada"),
                  timeout=40)
        self.calibrate("P01", "A")                 # mismo código otra vez
        self.yes = False
        a._do_start()
        self.assertFalse(a._session_active)
        self.assertTrue(any(m[1] == "Código repetido" for m in self.msgs))


class TestWidgets(AppCase):
    def test_flat_button_states(self):
        calls = []
        b = main_app.FlatButton(self.app.root, "x", "#145A32",
                                lambda: calls.append(1))
        b.invoke()
        b.configure(state="disabled", text="y")
        b.invoke()
        b.configure(state="normal")
        b.invoke()
        self.assertEqual(len(calls), 2)
        self.assertEqual(b.cget("text"), "y")

    def test_debug_window_does_not_crash_without_frames(self):
        a = self.app
        a._toggle_debug()
        self.pump(0.4)
        a._toggle_debug()
        self.assertIsNone(a._debug_win)

    def test_userscript_copy(self):
        a = self.app
        a._copy_userscript()
        clip = a.root.clipboard_get()
        self.assertIn("==UserScript==", clip)
        # limitado a los dominios del estudio, no a todos los sitios
        self.assertIn("// @match        *://127.0.0.1/*", clip)
        self.assertNotIn("*://*/*", clip.split("==/UserScript==")[0])

    def test_events_from_other_sites_are_ignored(self):
        a = self.app
        self.assertFalse(a._bridge_sink(
            "click", {"url": "https://otro-sitio.com/x", "x": 1, "y": 1}, 0))

    def test_export_zip_for_upload(self):
        import zipfile
        a = self.app
        a._var_code.set("P03")
        folder = a._study_session_dir()
        with open(os.path.join(folder, "P03_20261010_120000.csv"), "w") as f:
            f.write("t\n1\n")
        opened = []
        a._open_path = opened.append
        a._export_zip()
        out_dir = os.path.join(self.tmp, "para_enviar")
        self.assertEqual(opened, [out_dir])
        zips = os.listdir(out_dir)
        self.assertEqual(len(zips), 1)
        self.assertTrue(zips[0].startswith("P03_t_app_"))
        with zipfile.ZipFile(os.path.join(out_dir, zips[0])) as z:
            self.assertIn("t_app/P03_20261010_120000.csv", z.namelist())


if __name__ == "__main__":
    unittest.main()
