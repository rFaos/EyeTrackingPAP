"""
_t_noise.py — Mide el ruido REAL de los landmarks de MediaPipe.

Captura una secuencia en vivo y descompone el movimiento de los landmarks en:
  · componente global  (toda la cara se desplaza junta) — se cancela al
    restar iris − centro del ojo, no degrada la mirada;
  · componente de escala — tampoco degrada la mirada;
  · componente INDEPENDIENTE por landmark — esta es la que sí importa.

Con esa cifra se puede predecir el error de mirada esperado.
"""

from __future__ import annotations

import os, sys                                                      # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
import time
import numpy as np
import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_py
from mediapipe.tasks.python import vision as mp_vis

CAM_W, CAM_H = 640, 480
N_FRAMES = 240

# Landmarks cuyo ruido independiente importa para la mirada
IRIS = [468, 473]
EYE_LM = [33, 133, 159, 145, 263, 362, 386, 374]

opts = mp_vis.FaceLandmarkerOptions(
    base_options=mp_py.BaseOptions(model_asset_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "face_landmarker.task")),
    running_mode=mp_vis.RunningMode.VIDEO,
    num_faces=1,
    min_face_detection_confidence=0.4,
    min_face_presence_confidence=0.4,
    min_tracking_confidence=0.4,
)
det = mp_vis.FaceLandmarker.create_from_options(opts)

cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_W)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_H)
time.sleep(1.2)
for _ in range(12):
    cap.read()

print("Capturando... mantén la cabeza lo más quieta que puedas.", flush=True)
frames = []
t0 = time.perf_counter()
while len(frames) < N_FRAMES and time.perf_counter() - t0 < 20.0:
    ok, f = cap.read()
    if not ok:
        continue
    r = det.detect_for_video(
        mp.Image(image_format=mp.ImageFormat.SRGB,
                 data=cv2.cvtColor(f, cv2.COLOR_BGR2RGB)),
        int((time.perf_counter() - t0) * 1000))
    if not r.face_landmarks:
        continue
    lm = r.face_landmarks[0]
    pts = {}
    for i in range(468):
        pts[i] = (lm[i].x * CAM_W, lm[i].y * CAM_H)
    for k, i in enumerate(IRIS):
        if len(lm) > i:
            pts[i] = (lm[i].x * CAM_W, lm[i].y * CAM_H)
    frames.append(pts)

cap.release()
det.close()

n = len(frames)
print(f"  frames con cara: {n}  ({n / (time.perf_counter() - t0):.1f} fps)\n")
if n < 30:
    print("  Muy pocos frames con cara. Acércate a la cámara y reintenta.")
    raise SystemExit(1)

# ── Movimiento de cada landmark entre frames consecutivos ──────────────────
all_idx = sorted(frames[0].keys())
X = np.array([[frames[k][i][0] for i in all_idx] for k in range(n)])
Y = np.array([[frames[k][i][1] for i in all_idx] for k in range(n)])

dX = np.diff(X, axis=0)
dY = np.diff(Y, axis=0)
Xc, Yc = X[:-1], Y[:-1]          # posición en el frame de origen del salto

# Componente global: media del desplazamiento de todos los landmarks
gX = dX.mean(axis=1, keepdims=True)
gY = dY.mean(axis=1, keepdims=True)

# Componente de escala: regresión del desplazamiento radial sobre el radio
cx, cy = X.mean(axis=0), Y.mean(axis=0)
r = np.hypot(Xc - cx, Yc - cy)
r = np.where(r < 1e-6, 1e-6, r)
sX = (dX * (Xc - cx) / r ** 2).mean(axis=1, keepdims=True)
sY = (dY * (Yc - cy) / r ** 2).mean(axis=1, keepdims=True)

resX = dX - gX - sX * (Xc - cx)
resY = dY - gY - sY * (Yc - cy)

# σ del ruido independiente = σ(residual)/√2  (dos muestras independientes)
sig_all = math.sqrt(float(np.mean(resX ** 2 + resY ** 2)) / 2.0)
sig_glob = math.sqrt(float(np.mean(gX ** 2 + gY ** 2)) / 2.0)
sig_scale = math.sqrt(float(np.mean(sX ** 2 + sY ** 2)) / 2.0)

print("=" * 74)
print("  DESCOMPOSICIÓN DEL MOVIMIENTO DE LANDMARKS (px por frame)")
print("=" * 74)
print(f"  componente global   (cancela en la mirada) : {sig_glob:6.3f} px")
print(f"  componente de escala(cancela en la mirada) : {sig_scale * 100:6.3f} %")
print(f"  componente INDEPENDIENTE (degrada mirada)  : {sig_all:6.3f} px")

idx_of = {i: k for k, i in enumerate(all_idx)}


def per_lm(indices):
    vals = []
    for i in indices:
        k = idx_of[i]
        vals.append(math.sqrt(float(np.mean(resX[:, k] ** 2 +
                                            resY[:, k] ** 2)) / 2.0))
    return float(np.mean(vals))


print(f"\n  ruido independiente en los iris   : {per_lm(IRIS):6.3f} px")
print(f"  ruido independiente en los párpados: {per_lm(EYE_LM):6.3f} px")

# ── Jitter de las features de mirada ───────────────────────────────────────
def iris_disp(pts, iris, out_, in_, top, bot):
    c_x = (pts[out_][0] + pts[in_][0]) / 2.0
    c_y = (pts[top][1] + pts[bot][1]) / 2.0
    ew = max(abs(pts[in_][0] - pts[out_][0]), 1e-5)
    eh = max(abs(pts[bot][1] - pts[top][1]), 1e-5)
    return (pts[iris][0] - c_x) / ew, (pts[iris][1] - c_y) / eh


ux, uy = [], []
for p in frames:
    a = iris_disp(p, 468, 33, 133, 159, 145)
    b = iris_disp(p, 473, 263, 362, 386, 374)
    ux.append((a[0] + b[0]) / 2.0)
    uy.append((a[1] + b[1]) / 2.0)

ux, uy = np.array(ux), np.array(uy)
# jitter de alta frecuencia = desviación de la diferencia entre frames
jux = float(np.std(np.diff(ux))) / math.sqrt(2)
juy = float(np.std(np.diff(uy))) / math.sqrt(2)

# El iris recorre ±~0.17 al mirar de esquina a esquina (≈1920 px)
SLOPE_PX = 1920.0 / 0.34
print("\n" + "=" * 74)
print("  ERROR DE MIRADA IMPLÍCITO (px de pantalla, 1920×1080)")
print("=" * 74)
print(f"  jitter de la feature de iris : {jux:.4f} / {juy:.4f}")
print(f"  → error de mirada esperado   : "
      f"{math.hypot(jux, juy) * SLOPE_PX:6.0f} px  (sin filtrar)")
print(f"  → tras filtro One-Euro (≈×4) : "
      f"{math.hypot(jux, juy) * SLOPE_PX / 4:6.0f} px")
print("=" * 74)
