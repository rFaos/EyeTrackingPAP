"""
validacion.py  —  Comprobación del sistema completo
====================================================
Ejecuta las cinco validaciones que respaldan el funcionamiento del
eye-tracker.  No necesita que haya nadie delante de la cámara para las
pruebas 3, 4 y 5; las 1 y 2 sí.

    python validacion.py

Salida esperada (resumen):
    [OK] cámara detectada y entregando imagen
    [OK] motor en vivo con features válidas
    [OK] regresión de calibración con error < 120 px
    [OK] I-DT localiza las 6 fijaciones de la señal sintética
    [OK] informe de sesión generado
"""

from __future__ import annotations

import csv
import glob
import math
import os
import sys
import threading
import time

import numpy as np

from et_suite.data_logger import DataLogger

OK, FAIL = "OK ", "FALLA"
results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"  [{OK if ok else FAIL}] {name}" + (f"  — {detail}" if detail else ""))


# ════════════════════════════════════════════════════════════════════════════
print("=" * 72)
print(" 1. DETECCIÓN DE CÁMARAS")
print("=" * 72)
# ════════════════════════════════════════════════════════════════════════════
from et_suite.camera import probe_cameras

infos = probe_cameras(max_index=4, width=640, height=480)
for c in infos:
    print(f"       {c.label}   (brillo {c.brightness:.0f})")
check("se encontró al menos una cámara", len(infos) > 0,
      f"{len(infos)} encontradas")
if not infos:
    print("\n  Sin cámara no se puede continuar.")
    sys.exit(1)
check("alguna cámara alcanza los FPS mínimos",
      any(c.measured_fps >= 12 for c in infos),
      ", ".join(f"{c.measured_fps:.0f} fps" for c in infos))

# ════════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print(" 2. MOTOR DE MIRADA EN VIVO")
print("=" * 72)
# ════════════════════════════════════════════════════════════════════════════
from et_suite.gaze_tracker import GazeTracker, GazeCalibrator, SCREEN_W, SCREEN_H

cal = GazeCalibrator(n_points=9, method="polynomial")
tr = GazeTracker(camera_index=None, calibrator=cal)
tr.start()
tr.ready.wait(timeout=20)
time.sleep(4)
print(f"       {tr.status}")

n = faces = feats = 0
xs, ys, poses, eyes = [], [], [], []
t0 = time.perf_counter()
while time.perf_counter() - t0 < 5:
    s = tr.get_latest()
    if s:
        n += 1
        faces += int(s.face_detected)
        if s.face_detected and s.raw_feat:
            feats += 1
            xs.append(s.gaze_x_raw); ys.append(s.gaze_y_raw)
            poses.append((s.head_pitch, s.head_yaw, s.head_roll))
            eyes.append((s.raw_feat["gx"], s.raw_feat["gy"]))
    time.sleep(0.02)

fps = n / 5.0
print(f"       {n} muestras · {faces} con cara · {feats} con features · "
      f"{fps:.1f} fps")
check("el motor entrega muestras", n > 10, f"{n} muestras")
check("los FPS son suficientes para I-DT", fps >= 12, f"{fps:.1f} fps")

if feats:
    P = np.array(poses)
    E = np.array(eyes)
    print(f"       pose  pitch={P[:,0].mean():+7.1f}  yaw={P[:,1].mean():+7.1f}  "
          f"roll={P[:,2].mean():+7.1f}")
    print(f"       iris  dx={E[:,0].mean():+.4f}  dy={E[:,1].mean():+.4f}")
    check("la pose cefálica es fisiológicamente plausible",
          abs(P[:, 0].mean()) < 45 and abs(P[:, 1].mean()) < 50,
          "pitch/yaw dentro de rango")
    RX, RY = np.array(xs), np.array(ys)
    sat = float(((RX < 3) | (RX > SCREEN_W - 4) |
                 (RY < 3) | (RY > SCREEN_H - 4)).mean())
    check("la vista previa no satura en las esquinas", sat < 0.5,
          f"{100*sat:.1f} % saturado")
else:
    print("       (no hay cara delante de la cámara: se omite esta parte)")
    check("rostro detectado (opcional)", False,
          "nadie frente a la cámara durante la prueba")

# ── 2b · El grabador NO debe perder muestras ─────────────────────────────
# Regresión del bug más grave encontrado: la GUI y el grabador vaciaban la
# MISMA cola, así que competían y se perdía ~la mitad de las muestras
# (7.3 Hz grabados de 15 Hz reales).  Con el histórico no destructivo, el
# grabador ve la secuencia completa aunque la GUI consulte a 60 Hz.
if n > 10:
    _stop_poll = threading.Event()

    def _gui_poll() -> None:
        while not _stop_poll.is_set():
            tr.get_latest()
            time.sleep(1 / 60.0)

    threading.Thread(target=_gui_poll, daemon=True).start()
    _log = DataLogger(tr, output_dir="sessions")
    _log.start(session_name="_validacion")
    time.sleep(5)
    _csv_path, _ = _log.stop()
    _stop_poll.set()

    with open(_csv_path, encoding="utf-8") as _f:
        _rows = [r for r in csv.DictReader(_f)
                 if not r["Event_Type"].startswith("EVENT")]
    _fids = [int(r["Frame_ID"]) for r in _rows]
    _esperados = (max(_fids) - min(_fids) + 1) if _fids else 0
    _perdidos = (1.0 - len(_rows) / _esperados) if _esperados else 1.0

    print(f"       grabadas {len(_rows)} de {_esperados} muestras "
          f"producidas ({100 * _perdidos:.1f} % perdidas) "
          f"con la GUI consultando a 60 Hz")
    check("el grabador no pierde muestras", _perdidos < 0.03,
          f"{100 * _perdidos:.1f} % perdidas")

    try:
        os.remove(_csv_path)
    except OSError:
        pass

tr.stop()

# ════════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print(" 3. REGRESIÓN DE CALIBRACIÓN (verdad conocida)")
print("=" * 72)
# ════════════════════════════════════════════════════════════════════════════
rng = np.random.default_rng(0)
c = GazeCalibrator(screen_w=1920, screen_h=1080, n_points=9,
                   method="polynomial")
for step, (txn, tyn) in enumerate(c.TARGETS_9):
    tx, ty = txn * 1920, tyn * 1080
    gx0, gy0 = (txn - 0.5) * 0.30, (tyn - 0.5) * 0.22
    for _ in range(50):
        c.add_sample(dict(
            gx=gx0 + rng.normal(0, 0.010), gy=gy0 + rng.normal(0, 0.010),
            yaw=gx0 * 0.5 + rng.normal(0, 0.03),
            pitch=gy0 * 0.4 + rng.normal(0, 0.03), roll=0.0,
            p_yaw=gx0 * 0.8, p_pitch=gy0 * 0.7, fdx=0.0, fdy=0.0),
            tx, ty, step)
trained = c.train()
errs = []
for txn, tyn in c.TARGETS_9:
    gx, gy = (txn - 0.5) * 0.30, (tyn - 0.5) * 0.22
    px, py = c.predict(dict(gx=gx, gy=gy, yaw=gx * 0.5, pitch=gy * 0.4,
                            roll=0.0, p_yaw=gx * 0.8, p_pitch=gy * 0.7,
                            fdx=0.0, fdy=0.0))
    errs.append(math.hypot(px - txn * 1920, py - tyn * 1080))
print(f"       RMS {c.rms_error_px:.1f} px · error por punto "
      f"{np.mean(errs):.0f} px (máx {max(errs):.0f})")
check("la calibración entrena", trained, f"{c.n_samples} muestras")
check("el error de predicción es < 120 px", np.mean(errs) < 120,
      f"{np.mean(errs):.0f} px")
check("la calibración se guarda y se recupera",
      c.save("_validacion_cal.json") and
      GazeCalibrator(n_points=9, method="polynomial").load(
          "_validacion_cal.json"))
# Los parámetros guardados sólo valen para el modelo con el que se
# ajustaron: cargar un archivo polinómico en un calibrador geométrico debe
# rechazarse en vez de degradar la app en silencio.
check("una calibración de OTRO modelo se rechaza",
      not GazeCalibrator(n_points=9, method="geometric").load(
          "_validacion_cal.json"),
      "no se cuela un modelo incompatible")
os.remove("_validacion_cal.json")

# ── La app debe calibrar con el modelo geométrico por defecto ────────────
# Regresión del bug más traicionero: la interfaz arrancaba con
# "polynomial" y _do_calibrate() hacía ``calibrator.method = método``,
# así que TODA calibración hecha desde la GUI forzaba el modelo antiguo y
# el invariante a la cabeza nunca llegaba a usarse.
import re as _re
with open("main_app.py", encoding="utf-8") as _f:
    _src = _f.read()
_m = _re.search(r'_var_method\s*=\s*tk\.StringVar\(value="([^"]+)"\)', _src)
_default_ui = _m.group(1) if _m else "?"
check("la interfaz calibra con el modelo geométrico por defecto",
      _default_ui == "geometric",
      f"método por defecto en la GUI: '{_default_ui}'")
check("el modelo geométrico se puede elegir en la interfaz",
      '"geometric"' in _src and "Geométrico" in _src,
      "hay un botón de radio para el modelo geométrico")

# ════════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print(" 3b. INVARIANCIA A LA CABEZA — código de producción")
print("=" * 72)
# ════════════════════════════════════════════════════════════════════════════
# Se simula una cabeza 3D con ojos esféricos frente a una pantalla, se
# proyectan los landmarks a una cámara virtual y se meten por las MISMAS
# clases que usa la app (_HeadPoseEstimator, _EyeGazeExtractor,
# GazeCalibrator).  Así se comprueba el código real, no un prototipo aparte.
try:
    import synth_gaze as SG
    from et_suite.gaze_tracker import _HeadPoseEstimator, _EyeGazeExtractor

    CW, CH = 640, 480
    KS = SG.make_K(CW, CH, 0.95 * CW)
    SC_Z, SC_W_MM, SC_H_MM, SC_Y0 = 25.0, 530.0, 300.0, -140.0
    R_BASE = SG.euler_rot(180.0, 0.0, 0.0)

    class _LM:
        __slots__ = ("x", "y")

        def __init__(self, x: float, y: float) -> None:
            self.x, self.y = x, y

    def synth_lm(fx, fy, head_pos, euler, rng=None, noise=0.0):
        x = (fx - 0.5) * SC_W_MM
        y = SC_Y0 + fy * SC_H_MM
        obs = SG.sample_frame((x, y), head_pos, euler, KS, screen_z=SC_Z,
                              head_R=R_BASE @ SG.euler_rot(*euler))
        lm = [_LM(0.5, 0.5) for _ in range(478)]
        for i, (u, v) in obs["landmarks_px"].items():
            if rng is not None and noise > 0:
                u = float(u) + rng.normal(0, noise)
                v = float(v) + rng.normal(0, noise)
            lm[i] = _LM(float(u) / CW, float(v) / CH)
        return lm

    _hp = _HeadPoseEstimator()
    _ex = _EyeGazeExtractor()

    def _feat(lm):
        return _ex.extract(lm, _hp.estimate(lm, CW, CH))

    CAL_F = [0.10, 0.50, 0.90]
    CAL_PTS = [(a, b) for b in CAL_F for a in CAL_F]
    TST_F = [0.20, 0.35, 0.65, 0.80]
    TST_PTS = [(a, b) for b in TST_F for a in TST_F]

    STILL = [((0.0, 0.0, 550.0), (0.0, 0.0, 0.0))]
    MOVES = [
        ("sin mover nada",       STILL),
        ("giro leve  ±8°",       [((0, 0, 550), (y, p, r)) for y, p, r in
                                  [(8, 0, 0), (-8, 0, 0), (0, 8, 0),
                                   (0, -8, 0), (0, 0, 8), (0, 0, -8)]]),
        ("giro medio ±16°",      [((0, 0, 550), (y, p, r)) for y, p, r in
                                  [(16, 0, 0), (-16, 0, 0), (0, 14, 0),
                                   (0, -14, 0)]]),
        ("lateral ±60 mm",       [((d, 0, 550), (0, 0, 0))
                                  for d in (-60, -30, 30, 60)]),
        ("acercarse/alejarse",   [((0, 0, 550 + d), (0, 0, 0))
                                  for d in (-80, -40, 40, 80)]),
        ("arriba/abajo ±40 mm",  [((0, d, 550), (0, 0, 0)) for d in (-40, 40)]),
        ("movimiento combinado", [((45, 20, 480), (12, -8, 6)),
                                  ((-45, -20, 620), (-12, 8, -6)),
                                  ((30, 0, 450), (18, 0, 0)),
                                  ((-30, 0, 650), (-18, 0, 0))]),
    ]

    _rng_cal = np.random.default_rng(11)

    def _collect(pts, poses, reps=1, rng=None, noise=0.0):
        out = []
        for fx, fy in pts:
            for hp, he in poses:
                for _ in range(reps):
                    lm = synth_lm(fx, fy, hp, he, rng, noise)
                    out.append((_feat(lm), fx * 1920.0, fy * 1080.0))
        return out

    # Calibración: cabeza quieta, como en la app (6 repeticiones por punto,
    # con el ruido de landmarks medido en vivo ≈ 1 px).
    _cal_samples = _collect(CAL_PTS, STILL, reps=6,
                            rng=_rng_cal, noise=1.0)
    cal_new = GazeCalibrator(n_points=9, method="geometric")
    cal_old = GazeCalibrator(n_points=9, method="polynomial")
    for i, (f, tx, ty) in enumerate(_cal_samples):
        tid = i // 6
        cal_new.add_sample(f, tx, ty, tid)
        cal_old.add_sample(f, tx, ty, tid)
    ok_new = cal_new.train()
    ok_old = cal_old.train()
    z_txt = ("n/d" if cal_new._z_plane is None
             else f"{cal_new._z_plane:+.0f} mm")
    rms_new = ("n/d" if cal_new.rms_error_px is None
               else f"{cal_new.rms_error_px:.0f} px")
    print(f"       calibración geométrica : {ok_new}  "
          f"(plano Z = {z_txt}, RMS {rms_new})")
    print(f"       calibración polinómica : {ok_old}  "
          f"(RMS {cal_old.rms_error_px:.0f} px)")
    print()
    print(f"       {'escenario':<24}{'antes':>12}{'ahora':>12}{'mejora':>10}")
    print("       " + "-" * 56)

    worst_old, worst_new = 0.0, 0.0
    for name, poses in MOVES:
        eo = en = 0.0
        data = _collect(TST_PTS, poses)
        for f, tx, ty in data:
            px, py = cal_old.predict(f)
            eo += math.hypot(px - tx, py - ty)
            px, py = cal_new.predict(f)
            en += math.hypot(px - tx, py - ty)
        eo /= len(data)
        en /= len(data)
        worst_old = max(worst_old, eo)
        worst_new = max(worst_new, en)
        factor = (eo / en) if en > 1e-6 else float("inf")
        print(f"       {name:<24}{eo:>11.0f}{en:>12.0f}"
              f"{factor:>9.0f}×")

    # Con el ruido real de landmarks (≈1 px medido en vivo)
    eo = en = 0.0
    cnt = 0
    for _name, poses in MOVES:
        for f, tx, ty in _collect(TST_PTS, poses, reps=2,
                                  rng=np.random.default_rng(29), noise=1.0):
            px, py = cal_old.predict(f)
            eo += math.hypot(px - tx, py - ty)
            px, py = cal_new.predict(f)
            en += math.hypot(px - tx, py - ty)
            cnt += 1
    eo /= cnt
    en /= cnt
    print("       " + "-" * 56)
    print(f"       {'con ruido real (1 px)':<24}{eo:>11.0f}{en:>12.0f}"
          f"{(eo/en if en > 1e-6 else 0):>9.0f}×")

    check("la calibración geométrica entrena", ok_new)
    check("el giro leve (±8°) no desvía la mirada más de 150 px",
          worst_new < 150,
          f"peor caso {worst_new:.0f} px (antes {worst_old:.0f} px)")
    check("el modelo geométrico supera al polinómico al mover la cabeza",
          worst_new < worst_old / 10,
          f"{worst_new:.0f} px frente a {worst_old:.0f} px")
except Exception as exc:                                   # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("validación de invariancia a la cabeza", False, str(exc))

# ════════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print(" 4. CLASIFICADOR DE FIJACIONES — I-VT vs I-DT")
print("=" * 72)
# ════════════════════════════════════════════════════════════════════════════
from et_suite.pupillometry import IDTClassifier, IVTClassifier

FS, DT, NOISE = 15.0, 1 / 15.0, 60.0
FIX_POS = [(300, 200), (1500, 200), (960, 540),
           (300, 900), (1600, 850), (960, 120)]
rng = np.random.default_rng(7)
track, t = [], 0.0
for fx, fy in FIX_POS:
    for _ in range(int(0.6 / DT)):
        track.append((fx + rng.normal(0, NOISE), fy + rng.normal(0, NOISE), t))
        t += DT
    for _ in range(2):
        track.append((fx + rng.normal(0, 400), fy + rng.normal(0, 400), t))
        t += DT
print(f"       señal: {len(track)} muestras a {FS:.0f} fps, "
      f"{len(FIX_POS)} fijaciones reales, ruido {NOISE:.0f} px")


def locate(clf) -> int:
    clf.reset()
    for px, py, ts in track:
        clf.update(px, py, ts)
    clf.flush(track[-1][2])
    hits = 0
    for f in clf.fixations:
        if any(abs(f.cx - fx) < 150 and abs(f.cy - fy) < 150
               for fx, fy in FIX_POS):
            hits += 1
    return hits


ivt_hits = locate(IVTClassifier(vel_thresh_px_s=80.0, min_duration_ms=100.0))
idt_hits = locate(IDTClassifier())
print(f"       I-VT (umbral antiguo 80 px/s): {ivt_hits}/{len(FIX_POS)}")
print(f"       I-DT (nuevo, sigma 200 px):    {idt_hits}/{len(FIX_POS)}")
check("I-DT localiza las 6 fijaciones", idt_hits == len(FIX_POS),
      f"{idt_hits}/{len(FIX_POS)}")
check("I-DT es mejor que el I-VT anterior", idt_hits > ivt_hits,
      f"{idt_hits} vs {ivt_hits}")

# ════════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print(" 5. INFORME DE SESIÓN")
print("=" * 72)
# ════════════════════════════════════════════════════════════════════════════
csvs = sorted(glob.glob(os.path.join("sessions", "*.csv")))
if csvs:
    from et_suite.report import session_report
    txt = session_report(csvs[0], 1920, 1080)
    check("el informe se genera", len(txt) > 200, os.path.basename(csvs[0]))
    check("incluye el orden de fijación", "QUÉ MIRÓ PRIMERO" in txt)
    check("incluye el mapa de atención", "MAPA DE ATENCIÓN" in txt)
else:
    print("       (no hay CSV en sessions/)")

# ════════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
n_ok = sum(1 for _, ok in results if ok)
print(f" RESULTADO: {n_ok}/{len(results)} comprobaciones superadas")
print("=" * 72)
for name, ok in results:
    print(f"  [{'OK ' if ok else 'FALLA'}] {name}")
sys.exit(0 if n_ok == len(results) else 1)
