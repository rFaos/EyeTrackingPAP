"""Pruebas de los módulos base: geometría, estudio, parpadeos y puente web."""

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request

from helpers import ROOT, FakeTracker, make_sample          # noqa: F401

from et_suite.display import (content_origin, px_per_degree, px_to_deg,
                              screen_to_page)
from et_suite.pupillometry import BlinkDetector
from et_suite.study import StudyError, load_study, scan_studies, study_from_dict
from et_suite.web_bridge import WebBridge

STUDIES = os.path.join(ROOT, "studies")


class TestDisplay(unittest.TestCase):
    def test_pixels_per_degree(self):
        # Pantalla 34.5 cm / 1920 px a 60 cm: ≈ 58 px por grado
        ppd = px_per_degree(1920, 34.5, 60)
        self.assertAlmostEqual(ppd, 58.3, delta=0.5)
        self.assertAlmostEqual(px_to_deg(ppd, 1920, 34.5, 60), 1.0, places=6)

    def test_degenerate_inputs_are_nan_not_crash(self):
        self.assertTrue(px_per_degree(0, 34, 60) != px_per_degree(0, 34, 60))

    def test_screen_to_page_chrome_mac(self):
        # macOS: barra superior de 100 px, sin bordes laterales
        vp = dict(sx=0, sy=200, iw=1920, ih=980, ow=1920, oh=1080,
                  wx=0, wy=0, sw=1920)
        self.assertEqual(content_origin(vp), (0.0, 100.0))
        r = screen_to_page(500, 400, vp, 1920)
        self.assertAlmostEqual(r["vx"], 500)
        self.assertAlmostEqual(r["vy"], 300)         # 400 - 100 de barra
        self.assertAlmostEqual(r["py"], 500)         # + 200 de scroll

    def test_screen_to_page_windows_borders(self):
        # Windows: bordes de 8 px a los lados y abajo; barra de 80 px
        vp = dict(sx=0, sy=0, iw=1904, ih=968, ow=1920, oh=1056,
                  wx=-8, wy=-8, sw=1920)
        ox, oy = content_origin(vp)
        self.assertAlmostEqual(ox, 0.0)              # -8 + 8
        self.assertAlmostEqual(oy, -8 + (1056 - 968 - 8))   # 72

    def test_screen_to_page_zoom_and_scaling(self):
        # zoom 150 %: el navegador ve una pantalla de 1280 px CSS
        vp = dict(sx=0, sy=0, iw=1280, ih=650, ow=1280, oh=720,
                  wx=0, wy=0, sw=1280)
        r = screen_to_page(900, 600, vp, 1920)
        self.assertAlmostEqual(r["vx"], 600)
        self.assertAlmostEqual(r["vy"], 400 - 70)

    def test_firefox_exact_origin_wins(self):
        vp = dict(sx=10, sy=20, iw=1000, ih=800, ow=1000, oh=900, wx=0, wy=0,
                  sw=1000, mx=5, my=77)
        r = screen_to_page(105, 177, vp, 1000)
        self.assertAlmostEqual(r["vx"], 100)
        self.assertAlmostEqual(r["py"], 100 + 20)

    def test_no_viewport_returns_none(self):
        self.assertIsNone(screen_to_page(1, 2, None, 1920))


class TestStudy(unittest.TestCase):
    def test_demo_and_template_are_valid(self):
        demo = load_study(os.path.join(STUDIES, "ejemplos",
                                       "demo_pagina_prueba.json"))
        self.assertEqual(len(demo.tasks), 3)
        self.assertEqual(demo.task(3).target_aoi, "faq_calibracion")
        self.assertIn("127.0.0.1", demo.allowed_hosts())
        tpl = load_study(os.path.join(STUDIES, "_plantilla_estudio.json"))
        self.assertEqual(len(tpl.tasks), 3)

    def test_scan_ignores_templates(self):
        ok, bad = scan_studies(STUDIES)
        # el participante sólo ve el estudio real; los ejemplos van aparte
        self.assertEqual(list(ok), ["ventana_amarilla_2026"])
        self.assertNotIn("udem_usabilidad_2026", ok)
        self.assertEqual(bad, [])
        ok, bad = scan_studies(os.path.join(STUDIES, "ejemplos"))
        self.assertIn("demo_pagina_prueba", ok)
        self.assertEqual(bad, [])

    def test_all_errors_are_reported_together(self):
        with self.assertRaises(StudyError) as cm:
            study_from_dict({
                "id": "mal id!", "tasks": [{"id": 1}, {"id": 1,
                                                       "instruction": "x"}],
                "aois": [{"id": "a"}, {"id": "b", "rect": [0, 0, -5, 10]},
                         {"id": "c", "selector": "x", "tasks": [9]}],
            })
        msg = str(cm.exception)
        for frag in ("'id' es obligatorio", "falta 'instruction'", "repetido",
                     "define 'selector' o 'rect'", "ancho y alto > 0",
                     "tarea 9"):
            self.assertIn(frag, msg)

    def test_target_aoi_must_exist(self):
        with self.assertRaises(StudyError) as cm:
            study_from_dict({"id": "x", "tasks": [
                {"id": 1, "instruction": "i", "target_aoi": "nope"}]})
        self.assertIn("target_aoi", str(cm.exception))

    def test_invalid_json_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            f.write("{no es json")
        try:
            with self.assertRaises(StudyError):
                load_study(f.name)
        finally:
            os.remove(f.name)

    def test_aoi_scaling_to_other_resolution(self):
        st = study_from_dict({
            "id": "x", "reference_screen": {"width": 1920, "height": 1080},
            "tasks": [{"id": 1, "instruction": "i"}],
            "aois": [{"id": "a", "rect": [192, 108, 384, 216]}]})
        r = st.aoi("a").screen_rect(1280, 720, 1920, 1080)
        self.assertEqual(r, (128.0, 72.0, 384.0, 216.0))


class _LM:
    def __init__(self, x, y):
        self.x, self.y = x, y


def _eye_landmarks(aperture: float):
    """478 landmarks con los 12 puntos de párpados a una apertura dada."""
    lm = [_LM(0.5, 0.5) for _ in range(478)]
    for eye, cx in ((BlinkDetector.R_EYE, 0.40), (BlinkDetector.L_EYE, 0.60)):
        p1, p2, p3, p4, p5, p6 = eye
        w = 0.06
        lm[p1] = _LM(cx - w / 2, 0.5)
        lm[p4] = _LM(cx + w / 2, 0.5)
        h = aperture * w                       # EAR = (2h)/(2w) = h/w
        lm[p2] = _LM(cx - w / 6, 0.5 - h / 2)
        lm[p3] = _LM(cx + w / 6, 0.5 - h / 2)
        lm[p6] = _LM(cx - w / 6, 0.5 + h / 2)
        lm[p5] = _LM(cx + w / 6, 0.5 + h / 2)
    return lm


class TestBlink(unittest.TestCase):
    def test_ear_value(self):
        bd = BlinkDetector()
        self.assertAlmostEqual(bd.ear(_eye_landmarks(0.30), 1000, 1000), 0.30,
                               places=3)

    def test_detects_blink_and_recovers(self):
        bd = BlinkDetector()
        t, flags = 0.0, []
        seq = [0.30] * 30 + [0.15, 0.08, 0.12] + [0.30] * 20
        for ap in seq:
            ear = bd.ear(_eye_landmarks(ap), 1000, 1000)
            flags.append(bd.update(ear, t))
            t += 1 / 15
        self.assertEqual(sum(flags), 3)
        self.assertEqual(flags[30:33], [True, True, True])
        self.assertFalse(any(flags[:30]) or any(flags[33:]))

    def test_participant_with_narrow_eyes_is_not_always_blinking(self):
        bd = BlinkDetector()
        flags = [bd.update(bd.ear(_eye_landmarks(0.18), 1000, 1000), i / 15)
                 for i in range(80)]
        self.assertFalse(any(flags))

    def test_long_closure_does_not_poison_baseline(self):
        bd = BlinkDetector()
        for i in range(30):
            bd.update(bd.ear(_eye_landmarks(0.30), 1000, 1000), i / 15)
        flags = [bd.update(bd.ear(_eye_landmarks(0.05), 1000, 1000),
                           2 + i / 15) for i in range(45)]    # 3 s cerrado
        self.assertTrue(all(flags))                            # sigue contando
        self.assertFalse(bd.update(bd.ear(_eye_landmarks(0.30), 1000, 1000),
                                   5.1))


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.got = []
        self.cfg = {"rec": True, "task": 2, "aois": [{"id": "x"}],
                    "hosts": ["127.0.0.1"]}
        self.page = tempfile.NamedTemporaryFile("w", suffix=".html",
                                                delete=False)
        self.page.write("<h1>hola</h1>")
        self.page.close()
        self.br = WebBridge(lambda k, p, t: self.got.append((k, p)) or True,
                            lambda: self.cfg, port=0,
                            static_files={"/p.html": self.page.name})
        self.assertTrue(self.br.start())
        self.base = f"http://127.0.0.1:{self.br.port}"

    def tearDown(self):
        self.br.stop()
        os.remove(self.page.name)

    def _post(self, body: bytes, path="/e"):
        req = urllib.request.Request(self.base + path, data=body,
                                     method="POST")
        return urllib.request.urlopen(req, timeout=3)

    def test_ping_and_config_with_cors(self):
        r = urllib.request.urlopen(self.base + "/ping", timeout=3)
        self.assertEqual(json.loads(r.read()), {"ok": True})
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")
        r = urllib.request.urlopen(self.base + "/config", timeout=3)
        self.assertEqual(json.loads(r.read())["task"], 2)

    def test_event_post_reaches_sink_and_acks_state(self):
        r = self._post(json.dumps({"k": "vp", "sy": 300}).encode())
        ack = json.loads(r.read())
        self.assertTrue(ack["ok"])
        self.assertTrue(ack["rec"])
        self.assertEqual(ack["task"], 2)
        self.assertEqual(self.got[0][0], "vp")
        self.assertEqual(self.got[0][1]["sy"], 300)
        self.assertEqual(self.br.n_events, 1)

    def test_event_via_get_fallback(self):
        q = urllib.parse.quote(json.dumps({"k": "click", "x": 1}))
        r = urllib.request.urlopen(f"{self.base}/e?d={q}", timeout=3)
        self.assertTrue(json.loads(r.read())["ok"])
        self.assertEqual(self.got[0][0], "click")

    def test_rejects_garbage(self):
        for body in (b"no json", json.dumps({"k": "hack"}).encode()):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self._post(body)
            self.assertEqual(cm.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post(b"x" * (300 * 1024))
        self.assertEqual(cm.exception.code, 413)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/etc/passwd", timeout=3)
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual(self.got, [])

    def test_preflight_for_private_network_access(self):
        req = urllib.request.Request(self.base + "/e", method="OPTIONS")
        r = urllib.request.urlopen(req, timeout=3)
        self.assertEqual(r.status, 204)
        self.assertEqual(r.headers["Access-Control-Allow-Private-Network"],
                         "true")

    def test_static_only_whitelisted(self):
        r = urllib.request.urlopen(self.base + "/p.html", timeout=3)
        self.assertIn(b"hola", r.read())
        self.assertIn("text/html", r.headers["Content-Type"])

    def test_sink_exception_does_not_break_server(self):
        self.br.sink = lambda *_a: 1 / 0
        r = self._post(json.dumps({"k": "vp"}).encode())
        self.assertTrue(json.loads(r.read())["ok"])
        self.assertIn("error", self.br.last_error)

    def test_port_in_use_is_reported_not_raised(self):
        b2 = WebBridge(lambda *a: True, lambda: {}, port=self.br.port)
        self.assertFalse(b2.start())
        self.assertIn("ocupado", b2.last_error)


if __name__ == "__main__":
    unittest.main()
