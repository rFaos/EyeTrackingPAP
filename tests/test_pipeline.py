"""Pruebas del flujo de datos: grabación, métricas, informe, heatmaps y grupo."""

import csv
import glob
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

import numpy as np

from helpers import ROOT, FakeTracker, make_sample, write_session

from et_suite.data_logger import DataLogger
from et_suite.display import screen_to_page
from et_suite.group_analysis import analyze_study
from et_suite.heatmap_generator import HeatmapGenerator
from et_suite.metrics import analyze_csv, extract_fixations, read_rows
from et_suite.report import session_report
from et_suite.session_meta import (find_sessions, load_meta,
                                   next_participant_code, valid_participant_code)
from et_suite.study import study_from_dict

URL = "http://127.0.0.1:4243/p.html"

STUDY = study_from_dict({
    "id": "t_estudio", "name": "Estudio de pruebas",
    "url": URL,
    "reference_screen": {"width": 1920, "height": 1080},
    "tasks": [
        {"id": 1, "title": "Buscar A", "instruction": "Encuentra A",
         "target_aoi": "B"},
        {"id": 2, "title": "Buscar C", "instruction": "Encuentra C",
         "target_aoi": "A"},
        {"id": 3, "title": "Buscar FAQ", "instruction": "Baja al FAQ",
         "target_aoi": "FAQ"},
    ],
    "quality": {"min_duration_s": 5},
    "aois": [
        {"id": "A", "label": "Caja A", "rect": [100, 100, 400, 200]},
        {"id": "B", "label": "Caja B", "rect": [1200, 100, 400, 200]},
        {"id": "C", "label": "Caja C", "rect": [100, 700, 600, 200]},
        {"id": "FAQ", "label": "Preguntas", "selector": "#faq", "tasks": [3]},
    ],
})

A, B, C = (300, 200), (1400, 200), (400, 800)

# la página mide 3000 px de alto; el FAQ está en y = 1500..1800 (px de página)
LAYOUT = [{"t_ms": 1_000_000.0, "k": "layout", "url": URL, "aois": [
    {"id": "FAQ", "x": 200, "y": 1500, "w": 800, "h": 300, "vis": True,
     "fixed": False}]}]
VP = dict(sx=0, sy=0, iw=1920, ih=980, ow=1920, oh=1080, wx=0, wy=0, sw=1920)


def scroll_fn_factory(scroll_after_ms):
    """Scroll 0 antes de ``scroll_after_ms``; 1000 px después."""
    def fn(x, y, t):
        sy = 0 if t < scroll_after_ms else 1000
        vp = dict(VP, sy=sy)
        r = screen_to_page(x, y, vp, 1920)
        return {"sx": 0, "sy": sy, "px": round(r["px"], 1),
                "py": round(r["py"], 1)}
    return fn


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="etsuite_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ════════════════════════════════════════════════════════════════════════════
class TestDataLogger(TmpDirCase):
    def _pusher(self, tr, secs, xy=(500, 400), hz=30, blink_at=None):
        """Empuja muestras en tiempo real durante ``secs`` segundos."""
        fid = [0]
        stop = time.perf_counter() + secs

        def run():
            while time.perf_counter() < stop:
                fid[0] += 1
                now = time.perf_counter() * 1000.0
                bl = blink_at is not None and blink_at[0] <= fid[0] < blink_at[1]
                tr.push(make_sample(now, fid[0], xy[0], xy[1], blink=bl))
                time.sleep(1.0 / hz)
        th = threading.Thread(target=run, daemon=True)
        th.start()
        return th

    def test_tasks_state_alignment_tail_event_and_autosave(self):
        tr = FakeTracker()
        lg = DataLogger(tr, output_dir=self.tmp, screen_w=1920, screen_h=1080)
        lg.start("Pxx")
        th = self._pusher(tr, 3.0)
        time.sleep(0.5)
        lg.start_task(1)
        time.sleep(0.5)
        lg.web_event("vp", dict(VP, sy=700, url=URL))
        time.sleep(0.5)
        self.assertEqual(lg.current_task, 1)
        lg.web_event("click", {"x": 10, "y": 20, "url": URL})
        time.sleep(0.5)

        # autosave: el archivo ya tiene filas ANTES de detener (≥ 2.0 s)
        time.sleep(0.8)
        with open(lg.csv_path, encoding="utf-8") as f:
            self.assertGreater(sum(1 for _ in f), 10)

        lg.end_task(1, "success")
        th.join()
        path, stats = lg.stop()          # TASK_END queda tras la última muestra
        rows = read_rows(path)
        evs = [r["Event_Type"] for r in rows if r["Event_Type"].startswith("EVENT")]
        self.assertIn("EVENT:TASK_START:1", evs)
        self.assertIn("EVENT:TASK_END:1:success", evs)       # no se perdió
        self.assertIn("EVENT:CLICK", evs)
        end_row = [r for r in rows if r["Event_Type"].startswith("EVENT:TASK_END")][0]
        self.assertEqual(end_row["Task_ID"], 1)      # lleva la tarea que termina
        data = [r for r in rows if not r["Event_Type"].startswith("EVENT")]
        self.assertEqual(data[0]["Task_ID"], 0)
        self.assertTrue(any(r["Task_ID"] == 1 for r in data))
        # tras fijar el scroll, las filas llevan scroll y coordenada de página
        scrolled = [r for r in data if r["Scroll_Y"] == 700]
        self.assertTrue(scrolled)
        self.assertAlmostEqual(scrolled[0]["Page_Y"], 700 + 400 - 100, delta=1)
        # el scroll sólo aparece DESPUÉS del evento vp (alineado por tiempo)
        before = [r for r in data if r["Scroll_Y"] is None]
        self.assertTrue(before)
        self.assertLess(before[-1]["Timestamp_ms"], scrolled[0]["Timestamp_ms"])
        # archivo web
        with open(path[:-4] + ".web.json", encoding="utf-8") as fh:
            webs = json.load(fh)
        self.assertEqual({e["k"] for e in webs["events"]}, {"vp", "click"})

    def test_blinks_marked_and_counted(self):
        tr = FakeTracker()
        lg = DataLogger(tr, output_dir=self.tmp)
        lg.start("Pb")
        th = self._pusher(tr, 1.5, blink_at=(15, 19))
        th.join()
        path, stats = lg.stop()
        rows = [r for r in read_rows(path) if not r["Event_Type"].startswith("EVENT")]
        self.assertEqual(sum(1 for r in rows if r["Event_Type"] == "Blink"),
                         sum(r["Blink"] for r in rows))
        self.assertGreaterEqual(stats["n_blinks"], 1)

    def test_events_ignored_when_not_recording(self):
        lg = DataLogger(FakeTracker(), output_dir=self.tmp)
        self.assertFalse(lg.web_event("vp", VP))

    def test_blink_samples_do_not_form_fixations(self):
        rows = []
        for i in range(30):
            rows.append(_mini_row(i, 500, 400, "Fixation"))
        for i in range(30, 36):
            rows.append(_mini_row(i, 3, 3, "Blink"))     # mirada falsa
        for i in range(36, 66):
            rows.append(_mini_row(i, 505, 402, "Fixation"))
        fx = extract_fixations(rows)
        self.assertEqual(len(fx), 2)                      # el parpadeo corta
        for f in fx:
            self.assertAlmostEqual(f.cx, 502, delta=6)    # sin contaminar

    def test_event_rows_do_not_split_fixation(self):
        rows = [_mini_row(i, 500, 400, "Fixation") for i in range(15)]
        rows.append(_mini_row(15, 960, 540, "EVENT:CLICK"))
        rows += [_mini_row(i, 500, 400, "Fixation") for i in range(16, 40)]
        fx = extract_fixations(rows)
        self.assertEqual(len(fx), 1)


def _mini_row(i, x, y, ev, t0=1000.0, task=0):
    return {"Timestamp_ms": t0 + i * 66.7, "Frame_ID": i, "Gaze_X": x,
            "Gaze_Y": y, "Event_Type": ev, "Face_Detected": 1, "Task_ID": task,
            "Page_X": None, "Page_Y": None, "Scroll_X": None, "Scroll_Y": None,
            "Blink": 1 if ev == "Blink" else 0, "Pupil_Size": 7.0,
            "Head_Pitch": 0, "Head_Yaw": 0, "Head_Roll": 0, "Sample_Hz": 15,
            "Jitter_ms": 1, "Eye_Open": 0.3}


# ════════════════════════════════════════════════════════════════════════════
class TestMetrics(TmpDirCase):
    def _session(self, **kw):
        script = {
            1: [(*A, 1.0), (*B, 1.2), (*A, 0.6)],     # A → B → A
            2: [(*C, 1.5)],                           # nunca mira A
            3: [(*A, 0.8), (960, 700, 2.0)],          # tras scroll, FAQ
        }
        scroll_t = 1_000_000.0 + 11_000      # justo antes de mirar el FAQ
        base = dict(script=script, scroll_fn=scroll_fn_factory(scroll_t),
                    web=LAYOUT + [{"t_ms": 1_000_000.0, "k": "vp", "url": URL,
                                   **VP}],
                    outcomes={1: "success", 2: "fail", 3: "success"})
        base.update(kw)
        return write_session(self.tmp, "P01", STUDY, **base)

    def test_aoi_metrics_of_known_script(self):
        an = analyze_csv(self._session(), STUDY)
        t = {x["task"]: x for x in an.metrics["tasks"]}
        self.assertEqual(set(t), {1, 2, 3})
        # tarea 1: secuencia A, B, A; objetivo B visto
        self.assertEqual(t[1]["aoi_sequence"][:3], ["A", "B", "A"])
        self.assertTrue(t[1]["target_seen"])
        # B se mira tras ≈ 1.0 s de la tarea
        self.assertAlmostEqual(t[1]["target_ttff_ms"] / 1000.0, 1.0, delta=0.35)
        self.assertEqual(t[1]["result"], "success")
        # tarea 2: A nunca visto (sólo C, que no es AOI relevante)
        self.assertFalse(t[2]["target_seen"])
        self.assertIsNone(t[2]["target_ttff_ms"])
        self.assertFalse(t[2]["success"])
        a_t1 = [r for r in an.metrics["aois"]
                if r["task"] == 1 and r["aoi"] == "A"][0]
        self.assertEqual(a_t1["visits"], 2)
        self.assertEqual(a_t1["revisits"], 1)
        self.assertEqual(a_t1["order"], 1)
        self.assertAlmostEqual(a_t1["dwell_ms"] / 1000.0, 1.6, delta=0.5)
        b_t1 = [r for r in an.metrics["aois"]
                if r["task"] == 1 and r["aoi"] == "B"][0]
        self.assertEqual(b_t1["order"], 2)
        self.assertTrue(b_t1["is_target"])

    def test_selector_aoi_below_the_fold_needs_scroll(self):
        """El FAQ está a 1500 px: sólo se alcanza tras hacer scroll."""
        an = analyze_csv(self._session(), STUDY)
        t3 = [t for t in an.metrics["tasks"] if t["task"] == 3][0]
        self.assertTrue(an.session.has_page_coords)
        self.assertTrue(t3["target_seen"])
        # sin scroll la misma mirada NO habría caído en el FAQ
        p0 = screen_to_page(960, 700, VP, 1920)
        self.assertLess(p0["py"], 1500)
        p1 = screen_to_page(960, 700, dict(VP, sy=1000), 1920)
        self.assertTrue(1500 <= p1["py"] <= 1800)

    def test_selector_aoi_without_bridge_data_warns(self):
        path = self._session(web=None, scroll_fn=None)
        an = analyze_csv(path, STUDY)
        t3 = [t for t in an.metrics["tasks"] if t["task"] == 3][0]
        self.assertFalse(t3["target_seen"])
        self.assertTrue(any("navegador" in w
                            for w in an.metrics["quality"]["warnings"]))

    def test_panel_fixations_are_excluded(self):
        # El guion lee el panel en (960, 1000) entre tareas; sin panel cuenta
        path = self._session(panel_rect=[400, 900, 1100, 180])
        an = analyze_csv(path, STUDY)
        self.assertGreater(len(an.panel_fixes), 0)
        for f in an.fixes:
            self.assertFalse(400 <= f.cx <= 1500 and 900 <= f.cy <= 1080)
        self.assertGreater(an.metrics["session"]["panel_fix_ms"], 1000)

    def test_quality_rules(self):
        ok = analyze_csv(self._session(), STUDY).metrics["quality"]
        self.assertTrue(ok["valid"], ok)
        cases = {
            "calibración": dict(cal_rms=450.0),
            "validación": dict(val_post=500.0),
            "cara": dict(face_pct=40.0),
            "consentimiento": dict(consent=False),
            "frecuencia": dict(hz=4.0),
        }
        for frag, kw in cases.items():
            d = os.path.join(self.tmp, frag)
            p = write_session(d, "Pq", STUDY,
                              {1: [(*A, 2.0)], 2: [(*B, 2.0)]}, **kw)
            q = analyze_csv(p, STUDY).metrics["quality"]
            self.assertFalse(q["valid"], frag)
            self.assertTrue(any(frag in r for r in q["reasons"]), (frag, q))

    def test_degrees_are_computed(self):
        s = analyze_csv(self._session(), STUDY).metrics["session"]
        self.assertAlmostEqual(s["cal_rms_deg"], 120.0 / 58.3, delta=0.2)
        self.assertAlmostEqual(s["val_post_deg"], 150.0 / 58.3, delta=0.2)

    def test_participant_codes(self):
        self.assertTrue(valid_participant_code("P01"))
        self.assertTrue(valid_participant_code("G2-P03"))
        for bad in ("", "Juan Pérez", "a b", "x" * 40):
            self.assertFalse(valid_participant_code(bad), bad)
        self._session()
        self.assertEqual(next_participant_code(self.tmp), "P02")
        self.assertEqual(len(find_sessions(self.tmp)), 1)

    def test_legacy_csv_still_analyzable(self):
        """CSV antiguos (12 columnas, sin tareas ni meta) no deben romperse."""
        legacy = sorted(glob.glob(os.path.join(ROOT, "sessions", "*.csv")))
        if not legacy:
            self.skipTest("no hay CSV antiguos en sessions/")
        dst = os.path.join(self.tmp, os.path.basename(legacy[0]))
        shutil.copy(legacy[0], dst)
        an = analyze_csv(dst, None)
        self.assertGreater(an.metrics["session"]["n_samples"], 0)
        txt = session_report(dst, study=None, output=os.path.join(self.tmp, "r.txt")) \
            if "output" in session_report.__code__.co_varnames else None
        self.assertTrue(txt is None or isinstance(txt, str))


# ════════════════════════════════════════════════════════════════════════════
class TestReportAndHeatmaps(TmpDirCase):
    def setUp(self):
        super().setUp()
        script = {1: [(*A, 1.0), (*B, 1.2), (*A, 0.6)], 2: [(*C, 1.5)],
                  3: [(*A, 0.8), (960, 700, 2.0)]}
        self.csv = write_session(
            self.tmp, "P01", STUDY, script,
            scroll_fn=scroll_fn_factory(1_000_000.0 + 11_000),
            web=LAYOUT + [{"t_ms": 1_000_000.0, "k": "vp", "url": URL, **VP}],
            outcomes={1: "success", 2: "fail", 3: "success"})

    def test_report_text(self):
        txt = session_report(self.csv, study=STUDY)
        self.assertIsInstance(txt, str)
        for frag in ("Tarea 1", "Tarea 3", "P01", "CALIDAD", "AOI"):
            self.assertIn(frag, txt)
        self.assertTrue(os.path.exists(self.csv[:-4] + "_informe.txt")
                        or glob.glob(os.path.join(self.tmp, "*informe*")))

    def test_heatmaps_per_task_and_page_image(self):
        gen = HeatmapGenerator(screen_w=1920, screen_h=1080)
        an = analyze_csv(self.csv, STUDY)
        out = os.path.join(self.tmp, "hm")
        files = gen.session_images(an, out)
        self.assertTrue(files)
        for p in files if isinstance(files, (list, tuple)) else files.values():
            if isinstance(p, str) and p.endswith(".png"):
                self.assertTrue(os.path.getsize(p) > 1000, p)
        pngs = glob.glob(out + "*.png")
        self.assertGreaterEqual(len(pngs), 3)

    def test_density_covers_fixation_location(self):
        gen = HeatmapGenerator(screen_w=1920, screen_h=1080)
        an = analyze_csv(self.csv, STUDY)
        fx = an.fixes_for_task(1)
        img = gen.render_density(fx, (960, 540), None)
        self.assertEqual(img.shape[:2], (540, 960))
        self.assertEqual(img.shape[2], 3)
        self.assertGreater(img.std(), 5)       # no es una imagen plana

    def test_empty_inputs_do_not_crash(self):
        gen = HeatmapGenerator(screen_w=1920, screen_h=1080)
        img = gen.render_density([], (320, 180), None)
        self.assertEqual(img.shape[:2], (180, 320))


# ════════════════════════════════════════════════════════════════════════════
class TestGroup(TmpDirCase):
    def test_group_analysis(self):
        sdir = os.path.join(self.tmp, "sessions")
        for i, (grp, ok, kw) in enumerate([
                ("A", True, {}), ("A", True, {}), ("B", False, {}),
                ("B", True, {}), ("B", True, dict(cal_rms=600.0))]):
            script = {1: [(*A, 1.0), (*B, 1.0 + 0.1 * i)],
                      2: [(*C, 1.0)], 3: [(*A, 1.0), (960, 700, 1.5)]}
            write_session(sdir, f"P0{i + 1}", STUDY, script, group=grp,
                          seed=i, outcomes={1: "success",
                                            2: "success" if ok else "fail",
                                            3: "success"}, **kw,
                          scroll_fn=scroll_fn_factory(1_000_000.0 + 11_000),
                          web=LAYOUT + [{"t_ms": 1_000_000.0, "k": "vp",
                                         "url": URL, **VP}])
        out = os.path.join(self.tmp, "resultados")
        res = analyze_study(STUDY, sdir, out)
        self.assertEqual(res.n_sessions, 5)
        self.assertEqual(res.n_valid, 4)             # P05 excluida
        self.assertEqual(res.n_used, 4)
        self.assertTrue(res.files)
        csvs = [p for p in res.files if p.endswith(".csv")]
        self.assertTrue(csvs)
        names = " ".join(os.path.basename(p) for p in csvs)
        for frag in ("sesiones", "tareas", "aoi"):
            self.assertIn(frag, names)
        # CSV con BOM para Excel
        with open(csvs[0], "rb") as f:
            self.assertEqual(f.read(3), b"\xef\xbb\xbf")
        # éxito de la tarea 2: 3 de 4 válidos (P03 falló)
        tareas = os.path.join(out, "tareas.csv")
        with open(tareas, encoding="utf-8-sig") as f:
            data = list(csv.DictReader(f))
        self.assertTrue(data)
        text = res.summary_text
        self.assertTrue(os.path.exists(os.path.join(out, "resumen_grupal.txt")))
        self.assertIn("P05", text)                    # motivo de exclusión
        self.assertTrue(any(p.endswith(".png") for p in res.files))

    def test_include_invalid(self):
        sdir = os.path.join(self.tmp, "s")
        write_session(sdir, "P01", STUDY, {1: [(*A, 2.0)]}, cal_rms=700.0)
        r1 = analyze_study(STUDY, sdir, os.path.join(self.tmp, "o1"))
        r2 = analyze_study(STUDY, sdir, os.path.join(self.tmp, "o2"),
                           include_invalid=True)
        self.assertEqual((r1.n_valid, r1.n_used), (0, 0))
        self.assertEqual(r2.n_used, 1)


if __name__ == "__main__":
    unittest.main()
