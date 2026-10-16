"""
_t_live.py — Verificación en vivo del modelo geométrico.

Comprueba con la cámara real:
  · que la extracción 3D produce valores físicamente sensatos
    (ojo a ~500-600 mm, mirada apuntando hacia la pantalla);
  · cuánto ruido temporal tiene cada variante, convertido a píxeles de
    pantalla, para saber qué se va a ver en la app.
"""

from __future__ import annotations

import os, sys                                                      # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
import time
import numpy as np

from et_suite.gaze_tracker import GazeCalibrator, GazeTracker, SCREEN_W, SCREEN_H

N = 260

print("=" * 76)
print("  VERIFICACIÓN EN VIVO DEL MODELO GEOMÉTRICO")
print("=" * 76)
print("  Siéntate como lo harías normalmente y mira a la pantalla.\n")

cal = GazeCalibrator(n_points=9, method="geometric")
tr = GazeTracker(camera_index=None, calibrator=cal)
tr.start()
time.sleep(8)

print(f"  cámara : {tr.camera_info}")
print(f"  estado : {tr.status}\n")

samples = []
t0 = time.perf_counter()
while len(samples) < N and time.perf_counter() - t0 < 30:
    s = tr.get_latest()
    if s and s.face_detected and s.raw_feat:
        samples.append(s.raw_feat)
    time.sleep(0.01)
tr.stop()

n = len(samples)
print(f"  muestras: {n}")
if n < 40:
    print("  Muy pocas muestras. Acércate a la cámara y reintenta.")
    raise SystemExit(1)

geo_ok = sum(1 for f in samples if f.get("geom_ok"))
print(f"  extracción 3D disponible: {geo_ok}/{n}  "
      f"({100.0 * geo_ok / n:.0f} %)\n")

if geo_ok < 10:
    print("  Sin datos 3D: la pose no es válida. Revisa que la cara se vea bien.")
    raise SystemExit(1)

E = np.array([[f["Ex"], f["Ey"], f["Ez"]] for f in samples if f.get("geom_ok")])
d = np.array([[f["dx"], f["dy"], f["dz"]] for f in samples if f.get("geom_ok")])
ang = np.array([[f["yaw_deg"], f["pitch_deg"], f["roll_deg"]] for f in samples])

print("  ── Pose cefálica (grados) ──────────────────────────────────")
print(f"     yaw   {ang[:,0].mean():+7.1f} ± {ang[:,0].std():4.1f}   "
      f"pitch {ang[:,1].mean():+7.1f} ± {ang[:,1].std():4.1f}   "
      f"roll  {ang[:,2].mean():+7.1f} ± {ang[:,2].std():4.1f}")

print("\n  ── Posición del ojo en coordenadas de cámara (mm) ──────────")
print(f"     x {E[:,0].mean():+8.1f} ± {E[:,0].std():5.1f}")
print(f"     y {E[:,1].mean():+8.1f} ± {E[:,1].std():5.1f}")
print(f"     z {E[:,2].mean():+8.1f} ± {E[:,2].std():5.1f}   "
      f"← distancia al objetivo (lo normal son 400-800 mm)")

print("\n  ── Dirección de mirada (unitaria) ───────────────────────────")
print(f"     dx {d[:,0].mean():+7.3f} ± {d[:,0].std():5.3f}")
print(f"     dy {d[:,1].mean():+7.3f} ± {d[:,1].std():5.3f}")
print(f"     dz {d[:,2].mean():+7.3f} ± {d[:,2].std():5.3f}   "
      f"← negativo = mira hacia la cámara")

# ── Ruido temporal → error en pantalla ─────────────────────────────────────
def jitter(x):
    """σ del ruido de alta frecuencia (diferencia entre frames / √2)."""
    return np.std(np.diff(x, axis=0), axis=0) / math.sqrt(2.0)

jE, jd = jitter(E), jitter(d)
j_ang = jitter(ang)
px_per_rad = SCREEN_W / (2.0 * math.tan(math.radians(55.0) / 2.0))

gx = np.array([f["gx"] for f in samples])
gy = np.array([f["gy"] for f in samples])
j_gx, j_gy = jitter(gx.reshape(-1, 1))[0], jitter(gy.reshape(-1, 1))[0]
# El iris recorre ±~0.17 al mirar de esquina a esquina de la pantalla
old_px = math.hypot(j_gx, j_gy) / 0.34 * SCREEN_W

geo_px = float(np.hypot(jd[0], jd[1]) * px_per_rad)

print("\n  ── Ruido temporal de la pose (σ por frame) ─────────────────")
print(f"     yaw {j_ang[0]:5.2f}°   pitch {j_ang[1]:5.2f}°   roll {j_ang[2]:5.2f}°")

print("\n  ── Ruido temporal (σ por frame, sin filtrar) ───────────────")
print(f"     posición del ojo    : {float(np.hypot(jE[0], jE[1])):6.2f} mm")
print(f"     dirección de mirada : {math.degrees(math.atan(float(np.hypot(jd[0], jd[1])))):6.2f}°")
print(f"     feature de iris 2D  : {math.hypot(j_gx, j_gy):6.4f}")

print("\n  ── Error de mirada equivalente en pantalla ─────────────────")
print(f"     iris 2D (lo actual) : {old_px:6.0f} px")
print(f"     modelo geométrico   : {geo_px:6.0f} px")
print(f"     geométrico filtrado : {geo_px / 4:6.0f} px  (1€ reduce ~4×)")

# ── ¿Se puede entrenar la calibración geométrica? ──────────────────────────
print("\n  ── Prueba del entrenamiento geométrico ─────────────────────")
targets = cal.targets
for i, f in enumerate(samples[:200]):
    tx = int((0.5 + 0.35 * math.sin(i / 40.0)) * SCREEN_W)
    ty = int((0.5 + 0.30 * math.cos(i / 55.0)) * SCREEN_H)
    cal.add_sample(f, tx, ty, target_id=i % len(targets))
ok = cal.train()
print(f"     train() → {ok}   RMS = "
      f"{cal.rms_error_px if cal.rms_error_px is None else round(cal.rms_error_px, 1)} px")
print(f"     plano de pantalla Z = {cal._z_plane:+.0f} mm")
px, py = cal.predict(samples[-1])
print(f"     predict() → ({px}, {py})")

print("\n" + "=" * 76)
