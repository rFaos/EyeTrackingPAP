"""
synth_gaze.py  —  Modelo sintético cabeza-ojo-pantalla para validar features.

Construye una cabeza 3D con ojos esféricos, proyecta los landmarks a una
cámara virtual, corre solvePnP igual que la app y calcula las features de
mirada.  Sirve para medir CUÁNTO se desvía la mirada estimada cuando la
cabeza se mueve, sin depender de que haya alguien frente a la cámara.

Modelo facial en mm (Y hacia abajo, Z hacia fuera de la cara):
    escala del modelo de solvePnP = 5 unidades por mm
"""

from __future__ import annotations

import math
from typing import Dict, Tuple

import cv2
import numpy as np

# ─────────────────────────────────────────────────────────────────────────────
#  Modelo facial sintético (mm, relativo a la punta de la nariz)
# ─────────────────────────────────────────────────────────────────────────────

FACE_MM: Dict[int, Tuple[float, float, float]] = {
    1:   (0.0,    0.0,    0.0),     # punta de la nariz
    152: (0.0,   66.0,  -13.0),     # mentón
    33:  (-45.0, -34.0,  -27.0),    # canto externo ojo derecho (imagen izq.)
    263: ( 45.0, -34.0,  -27.0),    # canto externo ojo izquierdo
    61:  (-30.0,  30.0,  -25.0),    # comisura bucal derecha
    291: ( 30.0,  30.0,  -25.0),    # comisura bucal izquierda
    133: (-15.0, -34.0,  -27.0),    # canto interno ojo derecho
    362: ( 15.0, -34.0,  -27.0),    # canto interno ojo izquierdo
    159: (-30.0, -39.5,  -27.0),    # párpado superior derecho
    145: (-30.0, -28.5,  -27.0),    # párpado inferior derecho
    386: ( 30.0, -39.5,  -27.0),    # párpado superior izquierdo
    374: ( 30.0, -28.5,  -27.0),    # párpado inferior izquierdo
    234: (-55.0, -15.0,  -15.0),    # mejilla derecha
    454: ( 55.0, -15.0,  -15.0),    # mejilla izquierda
}

# Centros de los globos oculares (10 mm por detrás del plano de los cantos)
EYEBALL_R_MM = (-30.0, -34.0, -37.0)
EYEBALL_L_MM = ( 30.0, -34.0, -37.0)
EYEBALL_RADIUS_MM = 12.0

# Los 6 puntos que usa solvePnP
PNP_IDX = [1, 152, 33, 263, 61, 291]
PNP_MODEL = np.array([
    [0.0, 0.0, 0.0], [0.0, 330.0, -65.0],
    [-225.0, -170.0, -135.0], [225.0, -170.0, -135.0],
    [-150.0, 150.0, -125.0], [150.0, 150.0, -125.0],
], dtype=np.float64)   # = FACE_MM * 5


def euler_rot(yaw_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    """R = Rz(roll) · Ry(yaw) · Rx(pitch), en grados."""
    y, p, r = map(math.radians, (yaw_deg, pitch_deg, roll_deg))
    Rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)],
                   [0, math.sin(p), math.cos(p)]])
    Ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0],
                   [-math.sin(y), 0, math.cos(y)]])
    Rz = np.array([[math.cos(r), -math.sin(r), 0],
                   [math.sin(r), math.cos(r), 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def make_K(w: int, h: int, f: float) -> np.ndarray:
    return np.array([[f, 0, w / 2.0], [0, f, h / 2.0], [0, 0, 1.0]], np.float64)


def project(P_world: np.ndarray, K: np.ndarray) -> np.ndarray:
    """Proyección perspectiva. P_world: (N,3) en mm."""
    P = np.atleast_2d(P_world).astype(np.float64)
    z = np.maximum(P[:, 2], 1e-6)
    u = K[0, 0] * P[:, 0] / z + K[0, 2]
    v = K[1, 1] * P[:, 1] / z + K[1, 2]
    return np.stack([u, v], axis=1)


def head_to_world(P_head, R_head, head_pos) -> np.ndarray:
    P = np.atleast_2d(np.asarray(P_head, np.float64))
    return (R_head @ P.T).T + np.asarray(head_pos, np.float64)


def sample_frame(
    screen_target: Tuple[float, float],
    head_pos: Tuple[float, float, float],
    head_euler: Tuple[float, float, float],
    K: np.ndarray,
    screen_z: float = 600.0,
    head_R: np.ndarray = None,
) -> Dict:
    """
    Genera una observación sintética: landmarks proyectados + pose real.

    La cabeza está en ``head_pos`` (mm, coordenadas de cámara) con orientación
    ``head_euler`` y los ojos miran al punto ``screen_target`` del monitor,
    que está en el plano Z = screen_z.

    ``head_R`` permite imponer la matriz de rotación completa.  Es necesario
    para el caso realista: la cabeza está DELANTE de la cámara mirando HACIA
    ella, lo que exige la rotación base ``Ry(180°)``.  Sin ella la cara
    quedaría mirando al vacío (y el iris en la cara oculta del globo ocular).
    """
    R_head = (np.asarray(head_R, np.float64) if head_R is not None
              else euler_rot(*head_euler))
    T_world = np.array([screen_target[0], screen_target[1], screen_z], np.float64)

    # Globos oculares en coordenadas de cámara
    eb_r = head_to_world(EYEBALL_R_MM, R_head, head_pos)[0]
    eb_l = head_to_world(EYEBALL_L_MM, R_head, head_pos)[0]

    # Dirección de mirada verdadera y centro del iris sobre la esfera
    g_r = T_world - eb_r
    g_r /= np.linalg.norm(g_r)
    g_l = T_world - eb_l
    g_l /= np.linalg.norm(g_l)
    iris_r_w = eb_r + EYEBALL_RADIUS_MM * g_r
    iris_l_w = eb_l + EYEBALL_RADIUS_MM * g_l

    # Landmarks a proyectar
    pts_head = {i: np.array(v, np.float64) for i, v in FACE_MM.items()}
    pts_head[468] = (np.linalg.inv(R_head) @ (iris_r_w - np.asarray(head_pos)))
    pts_head[473] = (np.linalg.inv(R_head) @ (iris_l_w - np.asarray(head_pos)))

    idx = list(pts_head.keys())
    world = np.array([head_to_world(pts_head[i], R_head, head_pos)[0] for i in idx])
    px = project(world, K)
    landmarks_px = {i: px[k] for k, i in enumerate(idx)}

    return dict(landmarks_px=landmarks_px, R_head=R_head, head_pos=head_pos,
                eyeballs=(eb_r, eb_l), gaze_true=(g_r, g_l), K=K)


# ─────────────────────────────────────────────────────────────────────────────
#  Features
# ─────────────────────────────────────────────────────────────────────────────

def solve_pose(landmarks_px: Dict, K: np.ndarray):
    """solvePnP igual que en la app."""
    pts2d = np.array([landmarks_px[i] for i in PNP_IDX], np.float64)
    dist = np.zeros((4, 1))
    ok, rvec, tvec = cv2.solvePnP(PNP_MODEL, pts2d, K, dist,
                                  flags=cv2.SOLVEPNP_SQPNP)
    if not ok:
        return None, None
    R, _ = cv2.Rodrigues(rvec)
    return R, tvec.reshape(3)


def feat_old(lm: Dict) -> Tuple[float, float]:
    """Feature actual: desplazamiento del iris en la IMAGEN, normalizado."""
    def disp(iris, out_, in_, top, bot):
        cx = (lm[out_][0] + lm[in_][0]) / 2.0
        cy = (lm[top][1] + lm[bot][1]) / 2.0
        ew = max(abs(lm[in_][0] - lm[out_][0]), 1e-5)
        eh = max(abs(lm[bot][1] - lm[top][1]), 1e-5)
        return (lm[iris][0] - cx) / ew, (lm[iris][1] - cy) / eh
    a = disp(468, 33, 133, 159, 145)
    b = disp(473, 263, 362, 386, 374)
    return (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0


# Constantes del modelo 3D en unidades del modelo de solvePnP (mm × 5)
EYE_CENTER_R = np.array([-150.0, -170.0, -135.0])
EYE_CENTER_L = np.array([ 150.0, -170.0, -135.0])
EYE_WIDTH_MODEL = 150.0        # 30 mm
EYEBALL_R_MODEL = 60.0         # 12 mm


def feat_new(lm: Dict, R: np.ndarray, t: np.ndarray, K: np.ndarray
             ) -> Tuple[float, float]:
    """
    Feature propuesta: dirección de mirada 3D en coordenadas de CÁMARA.

    Para cada ojo:
      1. Se sitúa el centro del globo ocular en 3D (modelo facial + pose).
      2. Se lanza un rayo desde la cámara por el píxel del iris.
      3. Se corta el rayo con el plano del ojo (normal = eje Z de la cabeza).
      4. El vector centro-globo → iris, expresado en el sistema de la cabeza,
         da la dirección de mirada relativa a la cabeza.
      5. Se rota a coordenadas de cámara y se proyecta como (x/z, y/z).

    Al estar todo en coordenadas de cámara, mover o girar la cabeza NO cambia
    el resultado mientras se siga mirando al mismo punto.
    """
    ex, ey, ez = R[:, 0], R[:, 1], R[:, 2]
    cx, cy = K[0, 2], K[1, 2]
    f = K[0, 0]

    us, vs = [], []
    for iris_idx, eye_model in ((468, EYE_CENTER_R), (473, EYE_CENTER_L)):
        # Centro del globo ocular en coordenadas de cámara
        C = R @ eye_model + t

        # Rayo por el píxel del iris
        px, py = lm[iris_idx]
        d = np.array([(px - cx) / f, (py - cy) / f, 1.0])
        d /= np.linalg.norm(d)

        # Corte con el plano del ojo
        denom = float(d @ ez)
        if abs(denom) < 1e-6:
            continue
        s = float(C @ ez) / denom
        if s <= 0:
            continue
        iris_3d = s * d

        # Offset en el sistema de la cabeza, normalizado por el ancho del ojo
        off = iris_3d - C
        nx = float(off @ ex) / EYE_WIDTH_MODEL
        ny = float(off @ ey) / EYE_WIDTH_MODEL

        # Dirección de mirada = centro del globo → iris (el globo está
        # detrás del plano del ojo, de ahí el término en Z)
        k = EYEBALL_R_MODEL / EYE_WIDTH_MODEL
        gaze_cam = ex * nx + ey * ny + ez * k
        n = np.linalg.norm(gaze_cam)
        if n < 1e-9:
            continue
        gaze_cam /= n
        if gaze_cam[2] <= 1e-6:
            continue
        us.append(gaze_cam[0] / gaze_cam[2])
        vs.append(gaze_cam[1] / gaze_cam[2])

    if not us:
        return float("nan"), float("nan")
    return float(np.mean(us)), float(np.mean(vs))
