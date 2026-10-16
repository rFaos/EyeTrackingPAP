"""
_t_pose.py — ¿Cuánto se rompe la mirada cuando la cabeza se mueve?

Compara cuatro variantes sobre el modelo sintético cabeza-ojo-pantalla:

  A) ACTUAL      : features de iris en imagen + pose, calibrado con la cabeza
                   quieta (es lo que hace hoy la app).
  B) ACTUAL+MOV  : las mismas features, pero calibrando mientras el usuario
                   mueve la cabeza (deja que el polinomio aprenda la pose).
  C) GEOMÉTRICA  : dirección de mirada 3D en coordenadas de cámara, rayada
                   contra el plano de la pantalla, con la pose de solvePnP.
  D) GEOMÉTRICA* : igual que C pero con la pose VERDADERA del simulador.
                   Sirve de techo: separa el error de la matemática del error
                   del estimador de pose.

Todo en píxeles de una pantalla 1920×1080.
"""

from __future__ import annotations

import math
import numpy as np

import synth_gaze as S

# ─── Escenario físico ───────────────────────────────────────────────────────
#
#  Geometría realista de webcam de laptop:
#    · Cámara en el origen, mirando hacia +Z.
#    · Pantalla prácticamente en el plano de la cámara (Z ≈ 25 mm), extendida
#      hacia abajo porque el objetivo va en el bisel superior.
#    · Cara a ~550 mm DELANTE de la pantalla, mirando HACIA la cámara.
#
SCALE      = 5.0                 # unidades del modelo de solvePnP por mm
SCREEN_Z   = 25.0                # plano de la pantalla (mm)
SCR_W_MM   = 530.0               # monitor de 24" aprox.
SCR_H_MM   = 300.0
SCR_Y0_MM  = -140.0              # la pantalla arranca por debajo del objetivo
SW, SH     = 1920, 1080
CAM_W, CAM_H = 640, 480
K = S.make_K(CAM_W, CAM_H, 0.95 * CAM_W)

EYE_R_MM = np.array(S.EYEBALL_R_MM, np.float64)
EYE_L_MM = np.array(S.EYEBALL_L_MM, np.float64)
EYE_RADIUS_MM = S.EYEBALL_RADIUS_MM

HEAD_NEUTRAL = (0.0, 0.0, 550.0)     # nariz a 550 mm de la cámara
EULER_NEUTRAL = (0.0, 0.0, 0.0)

# Rotación base: la cara mira hacia la cámara (+Z_head → −Z_cam)
R_BASE = S.euler_rot(180.0, 0.0, 0.0)

PNP_IDX = S.PNP_IDX


def head_matrix(yaw: float, pitch: float, roll: float) -> np.ndarray:
    """Orientación cefálica completa: base + giro propio (grados)."""
    return R_BASE @ S.euler_rot(yaw, pitch, roll)


def frac_to_mm(fx: float, fy: float):
    return (fx - 0.5) * SCR_W_MM, SCR_Y0_MM + fy * SCR_H_MM


def mm_to_px(x: float, y: float):
    return (x / SCR_W_MM + 0.5) * SW, (y - SCR_Y0_MM) / SCR_H_MM * SH


# ─── Observación sintética ──────────────────────────────────────────────────

def observe(fx: float, fy: float, head_pos, head_euler):
    x, y = frac_to_mm(fx, fy)
    yaw, pitch, roll = head_euler
    return S.sample_frame((x, y), head_pos, head_euler, K,
                          screen_z=SCREEN_Z,
                          head_R=head_matrix(yaw, pitch, roll))


def euler_from_R(R):
    sy = math.sqrt(R[0, 0] ** 2 + R[1, 0] ** 2)
    if sy > 1e-6:
        return (math.degrees(math.atan2(R[2, 1], R[2, 2])),
                math.degrees(math.atan2(-R[2, 0], sy)),
                math.degrees(math.atan2(R[1, 0], R[0, 0])))
    return (math.degrees(math.atan2(-R[1, 2], R[1, 1])),
            math.degrees(math.atan2(-R[2, 0], sy)), 0.0)


# ─── Features variante A/B: las de la app actual ────────────────────────────

def feat_old(lm, R, t) -> dict:
    def disp(iris, out_, in_, top, bot):
        cx = (lm[out_][0] + lm[in_][0]) / 2.0
        cy = (lm[top][1] + lm[bot][1]) / 2.0
        ew = max(abs(lm[in_][0] - lm[out_][0]), 1e-5)
        eh = max(abs(lm[bot][1] - lm[top][1]), 1e-5)
        return (lm[iris][0] - cx) / ew, (lm[iris][1] - cy) / eh

    a = disp(468, 33, 133, 159, 145)
    b = disp(473, 263, 362, 386, 374)
    gx = (a[0] + b[0]) / 2.0
    gy = (a[1] + b[1]) / 2.0

    pitch, yaw, roll = euler_from_R(R)

    nx, ny = lm[1]
    rcx, rcy = lm[234]
    lcx, lcy = lm[454]
    d_r = math.hypot(nx - rcx, ny - rcy)
    d_l = math.hypot(nx - lcx, ny - lcy)
    denom = d_l + d_r
    p_yaw = (d_l - d_r) / denom if denom > 1e-6 else 0.0
    ey = (lm[33][1] + lm[263][1]) / 2.0
    my = (lm[61][1] + lm[291][1]) / 2.0
    span = max(my - ey, 1e-6)
    p_pitch = ((ny - ey) - (my - ny)) / span

    fdx = float(np.mean([v[0] / CAM_W for v in lm.values()])) - 0.5
    fdy = float(np.mean([v[1] / CAM_H for v in lm.values()])) - 0.5

    return dict(gx=gx, gy=gy,
                yaw=yaw / 45.0, pitch=pitch / 45.0, roll=roll / 45.0,
                p_yaw=p_yaw, p_pitch=p_pitch, fdx=fdx, fdy=fdy)


def raw_vec(f: dict) -> np.ndarray:
    return np.array([f["gx"], f["gy"], f["yaw"], f["pitch"], f["roll"],
                     f["p_yaw"], f["p_pitch"], f["fdx"], f["fdy"]], np.float64)


def phi(f: dict) -> np.ndarray:
    gx, gy, yaw, pitch, roll, p_yaw, p_pitch, fdx, fdy = raw_vec(f)
    return np.array([1.0, gx, gy, yaw, pitch, roll, p_yaw, p_pitch, fdx, fdy,
                     gx * gx, gy * gy, gx * gy, gx * yaw, gy * pitch,
                     gx * p_yaw, gy * p_pitch], np.float64)


# ─── Features variante C/D: mirada 3D en coordenadas de cámara ──────────────

def geom_from_pose(lm, R, t_mm, K_use=None):
    """
    Reconstruye, por ojo, el centro del globo ocular y la dirección de mirada
    en coordenadas de CÁMARA.

    El iris está sobre la esfera del globo ocular de radio R.  El rayo que sale
    de la cámara por el píxel del iris corta esa esfera en dos puntos; el más
    cercano a la cámara es el iris visible.  Normalizar (iris − centro) da la
    dirección de mirada, que NO depende de la orientación de la cabeza.
    """
    Ku = K if K_use is None else K_use
    f, cx, cy = Ku[0, 0], Ku[0, 2], Ku[1, 2]

    Cs, ds = [], []
    for iris_idx, eye_mm in ((468, EYE_R_MM), (473, EYE_L_MM)):
        C = R @ eye_mm + t_mm
        px, py = lm[iris_idx]
        d = np.array([(px - cx) / f, (py - cy) / f, 1.0])
        d /= np.linalg.norm(d)

        b = float(C @ d)
        disc = b * b - float(C @ C) + EYE_RADIUS_MM ** 2
        if disc <= 0:
            continue
        lam = b - math.sqrt(disc)          # corte frontal de la esfera
        g = lam * d - C
        n = np.linalg.norm(g)
        if n < 1e-9:
            continue
        Cs.append(C)
        ds.append(g / n)

    if not Cs:
        return None
    E = np.mean(Cs, axis=0)
    d = np.mean(ds, axis=0)
    d /= np.linalg.norm(d)
    return E, d


def feat_geom(lm, R, t):
    return geom_from_pose(lm, R, np.asarray(t, np.float64).reshape(3) / SCALE)


# Tamaños anatómicos del modelo facial (mm)
EYE_W_MM = 30.0        # canto externo ↔ interno
EYE_H_MM = 11.0        # párpado superior ↔ inferior


def feat_hybrid(lm, R, t, K_use=None):
    """
    Variante híbrida: dirección de mirada ROBUSTA + pose para rotarla.

    En vez de reconstruir la dirección desde el píxel absoluto del iris (que
    amplifica el ruido ×12, porque el globo ocular mide 12 mm y un píxel a
    550 mm son ~1 mm), se usa la medida que SÍ es robusta: el desplazamiento
    del iris RELATIVO a las esquinas del ojo, normalizado por el tamaño del
    ojo.  Es un cociente de medidas de imagen, así que el ruido común se
    cancela.

    Ese desplazamiento vive en el sistema de la cabeza (las esquinas son
    solidarias al cráneo), de modo que rotarlo con R lo lleva a coordenadas de
    cámara y da una dirección invariante a la orientación cefálica.
    """
    def disp(iris, out_, in_, top, bot):
        cx = (lm[out_][0] + lm[in_][0]) / 2.0
        cy = (lm[top][1] + lm[bot][1]) / 2.0
        ew = max(abs(lm[in_][0] - lm[out_][0]), 1e-5)
        eh = max(abs(lm[bot][1] - lm[top][1]), 1e-5)
        return (lm[iris][0] - cx) / ew, (lm[iris][1] - cy) / eh

    a_r = disp(468, 33, 133, 159, 145)
    a_l = disp(473, 263, 362, 386, 374)
    ux = (a_r[0] + a_l[0]) / 2.0
    uy = (a_r[1] + a_l[1]) / 2.0

    # x_head = −x_imagen (la base Ry(180°) invierte el eje X), y_head = y_imagen
    sin_x = -ux * EYE_W_MM / EYE_RADIUS_MM
    sin_y = uy * EYE_H_MM / EYE_RADIUS_MM
    sin_x = max(-0.99, min(0.99, sin_x))
    sin_y = max(-0.99, min(0.99, sin_y))
    cos_ = math.sqrt(max(1e-6, 1.0 - sin_x ** 2 - sin_y ** 2))

    g_head = np.array([sin_x, sin_y, cos_])
    g_head /= np.linalg.norm(g_head)
    d = R @ g_head
    d /= np.linalg.norm(d)

    # Posición del ojo (sólo para el paralaje por traslación)
    t_mm = np.asarray(t, np.float64).reshape(3) / SCALE
    E = (R @ EYE_R_MM + t_mm + R @ EYE_L_MM + t_mm) / 2.0
    return E, d


def feat_geom_true(obs):
    """Variante D: usa la pose verdadera del simulador (sin solvePnP)."""
    eb_r, eb_l = obs["eyeballs"]
    g_r, g_l = obs["gaze_true"]
    E = (np.asarray(eb_r) + np.asarray(eb_l)) / 2.0
    d = np.asarray(g_r) + np.asarray(g_l)
    d /= np.linalg.norm(d)
    return E, d


# ─── Ajustes ────────────────────────────────────────────────────────────────

def ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float = 1e-3):
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd < 1e-9] = 1.0
    mu[0], sd[0] = 0.0, 1.0
    Z = (X - mu) / sd
    lam = np.eye(Z.shape[1]) * alpha
    lam[0, 0] = 0.0
    W = np.linalg.solve(Z.T @ Z + lam, Z.T @ y)
    return mu, sd, W


def ridge_pred(X, mu, sd, W):
    return ((X - mu) / sd) @ W


def fit_geom(samples, z_grid, z_fixed=None):
    """
    Ajusta el modelo afín sobre el punto de corte del rayo con el plano de
    pantalla.  Si ``z_fixed`` es None, busca la distancia de pantalla que
    minimiza el RMS; si no, usa esa distancia y sólo ajusta el afín.

    Ojo: con la cabeza QUIETA la distancia de pantalla NO es identificable
    (cualquier Z da un ajuste afín exacto, porque el punto de corte y el punto
    real difieren en una transformación afín).  Sólo se identifica si el
    usuario traslada la cabeza durante la calibración.
    """
    grid = [z_fixed] if z_fixed is not None else z_grid
    best = None
    for Z in grid:
        rows, bx, by = [], [], []
        ok = True
        for E, d, sx, sy in samples:
            if abs(d[2]) < 1e-9:
                ok = False
                break
            lam = (Z - E[2]) / d[2]
            if lam <= 0:
                ok = False
                break
            P = E + lam * d
            rows.append([P[0] / 1000.0, P[1] / 1000.0, 1.0])
            bx.append(sx)
            by.append(sy)
        if not ok or len(rows) < 3:
            continue
        A = np.array(rows)
        bx, by = np.array(bx), np.array(by)
        Wx = np.linalg.lstsq(A, bx, rcond=None)[0]
        Wy = np.linalg.lstsq(A, by, rcond=None)[0]
        rms = math.sqrt(float(np.mean((A @ Wx - bx) ** 2 + (A @ Wy - by) ** 2)))
        if best is None or rms < best[0]:
            best = (rms, Z, Wx, Wy)
    return best


def predict_geom(E, d, Z, Wx, Wy):
    lam = (Z - E[2]) / d[2]
    P = E + lam * d
    a = np.array([P[0] / 1000.0, P[1] / 1000.0, 1.0])
    return float(a @ Wx), float(a @ Wy)


# ─── Recolección ────────────────────────────────────────────────────────────

CAL_F = [0.10, 0.50, 0.90]
CAL_POINTS = [(fx, fy) for fy in CAL_F for fx in CAL_F]

TEST_F = [0.20, 0.35, 0.65, 0.80]
TEST_POINTS = [(fx, fy) for fy in TEST_F for fx in TEST_F]


def collect(points, poses):
    """poses: lista de (head_pos, head_euler)."""
    out = []
    for fx, fy in points:
        for hp, he in poses:
            obs = observe(fx, fy, hp, he)
            lm = obs["landmarks_px"]
            R, t = S.solve_pose(lm, K)
            if R is None:
                continue
            sx, sy = mm_to_px(*frac_to_mm(fx, fy))
            out.append(dict(sx=sx, sy=sy,
                            old=feat_old(lm, R, t),
                            geo=feat_geom(lm, R, t),
                            geo_true=feat_geom_true(obs),
                            pose_err=(R, t, hp, he)))
    return out


# ─── Escenarios de movimiento ───────────────────────────────────────────────

_NEUTRAL = [(HEAD_NEUTRAL, EULER_NEUTRAL)]

_SMALL = [(HEAD_NEUTRAL, (y, p, r))
          for y, p, r in [(8, 0, 0), (-8, 0, 0), (0, 8, 0), (0, -8, 0),
                          (0, 0, 8), (0, 0, -8),
                          (5, 4, 3), (-5, -4, -3)]]

SCENARIOS = [
    ("sin mover nada",       _NEUTRAL),
    ("giro leve  ±8°",       _SMALL),
    ("giro medio ±16°",      [(HEAD_NEUTRAL, (y, p, r))
                              for y, p, r in [(16, 0, 0), (-16, 0, 0),
                                              (0, 14, 0), (0, -14, 0),
                                              (12, 8, 8), (-12, -8, -8)]]),
    ("lateral ±60 mm",       [((dx, 0, 550), EULER_NEUTRAL)
                              for dx in (-60, -30, 30, 60)]),
    ("acercarse/alejarse",   [((0, 0, 550 + dz), EULER_NEUTRAL)
                              for dz in (-80, -40, 40, 80)]),
    ("arriba/abajo ±40 mm",  [((0, dy, 550), EULER_NEUTRAL)
                              for dy in (-40, 40)]),
    ("movimiento combinado", [((45, 20, 480), (12, -8, 6)),
                              ((-45, -20, 620), (-12, 8, -6)),
                              ((30, 0, 450), (18, 0, 0)),
                              ((-30, 0, 650), (-18, 0, 0))]),
]


def jitter_landmarks(lm, rng, sig_ind=0.0, sig_glob=0.0, sig_scale=0.0):
    """
    Perturba landmarks con un modelo realista:

      · ``sig_glob``  — desplazamiento global (toda la cara se mueve junta).
                        Es el componente dominante del ruido de MediaPipe y
                        CANCELA al restar iris − centro del ojo.
      · ``sig_scale`` — cambio de escala global (la cara se acerca/aleja).
      · ``sig_ind``   — ruido independiente por landmark (el que de verdad
                        degrada la estimación de mirada).
    """
    g = rng.normal(0, sig_glob, 2) if sig_glob > 0 else np.zeros(2)
    s = 1.0 + (rng.normal(0, sig_scale) if sig_scale > 0 else 0.0)
    c = np.array([CAM_W / 2.0, CAM_H / 2.0])
    out = {}
    for i, v in lm.items():
        v = np.asarray(v, float)
        v = c + (v - c) * s + g
        if sig_ind > 0:
            v = v + rng.normal(0, sig_ind, 2)
        out[i] = v
    return out


def pipeline_error(K_assumed, noise_px=0.0, seed=0, z_fixed=0.0, variant="geom",
                   noise_spec=None):
    """
    Corre la tubería completa (calibrar + evaluar) con una K asumida y ruido
    de landmarks.  Devuelve (error medio, percentil 90, Z estimada).

    ``variant``: "old" = polinomio actual, "geom" = 3D puro,
                 "hybrid" = dirección robusta + rotación de cabeza.
    ``noise_spec``: (sig_ind, sig_glob, sig_scale); si es None se usa
                 ``noise_px`` como ruido independiente puro.
    """
    rng = np.random.default_rng(seed)
    spec = noise_spec if noise_spec is not None else (noise_px, 0.0, 0.0)

    def get_obs(fx, fy, hp, he):
        o = observe(fx, fy, hp, he)
        if any(s > 0 for s in spec):
            o = dict(o)
            o["landmarks_px"] = jitter_landmarks(o["landmarks_px"], rng, *spec)
        return o

    def features(o):
        lm = o["landmarks_px"]
        R, t = S.solve_pose(lm, K_assumed)
        if R is None:
            return None
        if variant == "geom":
            return geom_from_pose(lm, R, np.asarray(t).reshape(3) / SCALE,
                                  K_use=K_assumed)
        return feat_hybrid(lm, R, t, K_use=K_assumed)

    # ── Calibración ──────────────────────────────────────────────────────
    cal_geo, Xc, sx_c, sy_c = [], [], [], []
    for fx, fy in CAL_POINTS:
        o = get_obs(fx, fy, HEAD_NEUTRAL, EULER_NEUTRAL)
        sx, sy = mm_to_px(*frac_to_mm(fx, fy))
        if variant == "old":
            lm = o["landmarks_px"]
            R, t = S.solve_pose(lm, K_assumed)
            if R is None:
                continue
            Xc.append(phi(feat_old(lm, R, t)))
            sx_c.append(sx)
            sy_c.append(sy)
        else:
            f = features(o)
            if f is None:
                continue
            cal_geo.append((f[0], f[1], sx, sy))

    if variant == "old":
        Xc = np.array(Xc)
        mux, sdx, Wx = ridge_fit(Xc, np.array(sx_c))
        muy, sdy, Wy = ridge_fit(Xc, np.array(sy_c))
    else:
        mdl = fit_geom(cal_geo, np.linspace(-200.0, 600.0, 401),
                       z_fixed=z_fixed)

    # ── Evaluación ───────────────────────────────────────────────────────
    errs = []
    for _name, poses in SCENARIOS:
        for fx, fy in TEST_POINTS:
            for hp, he in poses:
                o = get_obs(fx, fy, hp, he)
                sx, sy = mm_to_px(*frac_to_mm(fx, fy))
                if variant == "old":
                    lm = o["landmarks_px"]
                    R, t = S.solve_pose(lm, K_assumed)
                    if R is None:
                        continue
                    ph = phi(feat_old(lm, R, t))[None, :]
                    gx = ridge_pred(ph, mux, sdx, Wx)[0]
                    gy = ridge_pred(ph, muy, sdy, Wy)[0]
                else:
                    f = features(o)
                    if f is None:
                        continue
                    gx, gy = predict_geom(f[0], f[1], mdl[1], mdl[2], mdl[3])
                errs.append(math.hypot(gx - sx, gy - sy))
    return (float(np.mean(errs)), float(np.percentile(errs, 90)),
            None if variant == "old" else mdl[1])


# ─── Experimento ────────────────────────────────────────────────────────────

def main():
    print("=" * 82)
    print("  ¿CUÁNTO SE ROMPE LA MIRADA AL MOVER LA CABEZA?")
    print("=" * 82)

    neutral = _NEUTRAL
    small = _SMALL
    scenarios = SCENARIOS

    # ── Verificación de solvePnP ─────────────────────────────────────────
    print("\n  [verificación] pose recuperada por solvePnP vs. verdad sintética")
    for fx, fy, hp, he in [(0.5, 0.5, HEAD_NEUTRAL, (0, 0, 0)),
                           (0.5, 0.5, HEAD_NEUTRAL, (12, -8, 6)),
                           (0.5, 0.5, (0, 0, 630), (0, 0, 0))]:
        obs = observe(fx, fy, hp, he)
        R, t = S.solve_pose(obs["landmarks_px"], K)
        R_true = head_matrix(*he)
        t_true = np.asarray(hp) * SCALE          # el modelo va en mm × 5
        ang = math.degrees(math.acos(np.clip(
            (np.trace(R.T @ R_true) - 1) / 2, -1, 1)))
        dt = np.linalg.norm(np.asarray(t).reshape(3) - t_true) / SCALE
        print(f"    euler={str(he):<16} error de rotación {ang:5.2f}°   "
              f"error de posición {dt:6.1f} mm")

    # ── Calibraciones ────────────────────────────────────────────────────
    cal_still = collect(CAL_POINTS, neutral)
    cal_moved = collect(CAL_POINTS, small)

    Xa = np.array([phi(s["old"]) for s in cal_still])
    muA_x, sdA_x, WA_x = ridge_fit(Xa, np.array([s["sx"] for s in cal_still]))
    muA_y, sdA_y, WA_y = ridge_fit(Xa, np.array([s["sy"] for s in cal_still]))

    Xb = np.array([phi(s["old"]) for s in cal_moved])
    muB_x, sdB_x, WB_x = ridge_fit(Xb, np.array([s["sx"] for s in cal_moved]))
    muB_y, sdB_y, WB_y = ridge_fit(Xb, np.array([s["sy"] for s in cal_moved]))

    Z_grid = np.linspace(-200.0, 600.0, 401)

    def geo_pairs(samples, key="geo"):
        return [(s[key][0], s[key][1], s["sx"], s["sy"])
                for s in samples if s[key] is not None]

    # C) 3D con Z estimada, calibrada quieta
    C = fit_geom(geo_pairs(cal_still), Z_grid)
    # D) 3D con Z = 0 fija (la pantalla está en el plano de la cámara)
    D = fit_geom(geo_pairs(cal_still), Z_grid, z_fixed=0.0)
    # E) 3D con Z estimada, calibrada en movimiento
    E = fit_geom(geo_pairs(cal_moved), Z_grid)
    # F) 3D con pose perfecta y Z = 0 (techo alcanzable)
    F = fit_geom(geo_pairs(cal_still, "geo_true"), Z_grid, z_fixed=0.0)

    print(f"\n  Calibración A/B : {len(cal_still)} / {len(cal_moved)} muestras")
    for nm, v in (("C", C), ("D", D), ("E", E), ("F", F)):
        print(f"  Calibración {nm}   : pantalla a {v[1]:6.0f} mm "
              f"(real {SCREEN_Z:.0f}) — RMS {v[0]:.1f} px")

    # ── Evaluación ───────────────────────────────────────────────────────
    print("\n" + "=" * 92)
    print("  ERROR MEDIO EN PANTALLA (px de 1920×1080)")
    print("=" * 92)
    print(f"  {'escenario':<22}{'A actual':>11}{'B +mov':>10}"
          f"{'C 3D~Z':>10}{'D 3D Z=0':>11}{'E 3D mov':>11}{'F techo':>10}")
    print("  " + "-" * 74)

    for name, poses in scenarios:
        data = collect(TEST_POINTS, poses)
        acc = dict(a=0.0, b=0.0, c=0.0, d=0.0, e=0.0, f=0.0)
        n = 0
        for s in data:
            px = ridge_pred(phi(s["old"])[None, :], muA_x, sdA_x, WA_x)[0]
            py = ridge_pred(phi(s["old"])[None, :], muA_y, sdA_y, WA_y)[0]
            acc["a"] += math.hypot(px - s["sx"], py - s["sy"])

            px = ridge_pred(phi(s["old"])[None, :], muB_x, sdB_x, WB_x)[0]
            py = ridge_pred(phi(s["old"])[None, :], muB_y, sdB_y, WB_y)[0]
            acc["b"] += math.hypot(px - s["sx"], py - s["sy"])

            for key, var in (("geo", "c"), ("geo", "d"), ("geo", "e")):
                pass
            if s["geo"] is not None:
                for var, mdl in (("c", C), ("d", D), ("e", E)):
                    gx, gy = predict_geom(s["geo"][0], s["geo"][1],
                                          mdl[1], mdl[2], mdl[3])
                    acc[var] += math.hypot(gx - s["sx"], gy - s["sy"])
            gx, gy = predict_geom(s["geo_true"][0], s["geo_true"][1],
                                  F[1], F[2], F[3])
            acc["f"] += math.hypot(gx - s["sx"], gy - s["sy"])
            n += 1

        print(f"  {name:<22}{acc['a']/n:>11.0f}{acc['b']/n:>10.0f}"
              f"{acc['c']/n:>10.0f}{acc['d']/n:>11.0f}"
              f"{acc['e']/n:>11.0f}{acc['f']/n:>10.0f}")

    print("=" * 92)
    print("  A = app actual, cabeza quieta      B = app actual, calibra moviendo")
    print("  C = 3D con distancia estimada      D = 3D asumiendo pantalla en la cámara")
    print("  E = 3D calibrando en movimiento    F = 3D con pose perfecta (techo)")

    # ── Sensibilidad a la distancia asumida ──────────────────────────────
    print("\n" + "=" * 92)
    print("  SENSIBILIDAD A LA DISTANCIA DE PANTALLA ASUMIDA")
    print("  (error medio en px sobre todos los escenarios)")
    print("=" * 92)
    print(f"  {'Z asumida':>12}{'error medio':>14}{'lateral ±60 mm':>18}")
    print("  " + "-" * 44)
    for z_fix in (-100.0, -50.0, 0.0, 25.0, 50.0, 100.0, 200.0):
        mdl = fit_geom(geo_pairs(cal_still), Z_grid, z_fixed=z_fix)
        tot, ntot, lat, nlat = 0.0, 0, 0.0, 0
        for name, poses in scenarios:
            for s in collect(TEST_POINTS, poses):
                if s["geo"] is None:
                    continue
                gx, gy = predict_geom(s["geo"][0], s["geo"][1],
                                      mdl[1], mdl[2], mdl[3])
                e = math.hypot(gx - s["sx"], gy - s["sy"])
                tot += e
                ntot += 1
                if name == "lateral ±60 mm":
                    lat += e
                    nlat += 1
        print(f"  {z_fix:>10.0f} mm{tot/ntot:>14.0f}{lat/max(nlat,1):>18.0f}")

    # ── Robustez: ruido de landmarks ─────────────────────────────────────
    print("\n" + "=" * 92)
    print("  ROBUSTEZ ANTE RUIDO DE LANDMARKS  (error medio, px)")
    print("  Todos los escenarios de movimiento juntos.")
    print("=" * 92)
    print(f"  {'modelo de ruido':<34}{'A actual':>13}{'D 3D puro':>13}"
          f"{'H híbrido':>13}")
    print("  " + "-" * 73)
    ruidos = [
        ("sin ruido",                          (0.0, 0.0, 0.0)),
        ("independiente 0.3 px / landmark",    (0.3, 0.0, 0.0)),
        ("independiente 1.0 px / landmark",    (1.0, 0.0, 0.0)),
        ("global 1.0 px",                      (0.0, 1.0, 0.0)),
        ("global 1.0 + indep 0.3 px",          (0.3, 1.0, 0.0)),
        ("global 1.0 + indep 0.5 px",          (0.5, 1.0, 0.0)),
        ("global 2.0 + indep 1.0 px",          (1.0, 2.0, 0.0)),
        ("global 1.0 + escala 0.5 %",          (0.0, 1.0, 0.005)),
    ]
    for label, spec in ruidos:
        row = []
        for var in ("old", "geom", "hybrid"):
            m, _, _ = pipeline_error(K, noise_spec=spec, variant=var)
            row.append(f"{m:.0f}")
        print(f"  {label:<34}{row[0]:>13}{row[1]:>13}{row[2]:>13}")

    # ── Sensibilidad a la focal, por variante ────────────────────────────
    print("\n" + "=" * 92)
    print("  SENSIBILIDAD A LA DISTANCIA FOCAL ASUMIDA  (error medio, px)")
    print("=" * 92)
    print(f"  {'variante':<34}{'0.75·W':>14}{'0.85·W':>14}{'0.95·W':>14}"
          f"{'1.10·W':>14}")
    print("  " + "-" * 80)
    variantes = [("A polinomio actual", "old"),
                 ("D 3D puro",          "geom"),
                 ("H híbrido",          "hybrid")]
    for label, var in variantes:
        row = []
        for fk in (0.75, 0.85, 0.95, 1.10):
            m, _, _ = pipeline_error(S.make_K(CAM_W, CAM_H, fk * CAM_W),
                                     noise_px=0.0, variant=var)
            row.append(f"{m:.0f}")
        print(f"  {label:<32}{row[0]:>14}{row[1]:>14}{row[2]:>14}{row[3]:>14}")
    print("  (la focal real del simulador es f = 0.95·W)")

    # ── Híbrido con ruido, escenario por escenario ───────────────────────
    print("\n" + "=" * 92)
    print("  HÍBRIDO ROBUSTO CON 1 px DE RUIDO, POR ESCENARIO")
    print("=" * 92)
    rng = np.random.default_rng(7)

    def obs_noisy(fx, fy, hp, he):
        o = observe(fx, fy, hp, he)
        o = dict(o)
        o["landmarks_px"] = {
            i: np.asarray(v, float) + rng.normal(0, 1.0, 2)
            for i, v in o["landmarks_px"].items()}
        return o

    cal = []
    for fx, fy in CAL_POINTS:
        o = obs_noisy(fx, fy, HEAD_NEUTRAL, EULER_NEUTRAL)
        R, t = S.solve_pose(o["landmarks_px"], K)
        f = feat_hybrid(o["landmarks_px"], R, t)
        sx, sy = mm_to_px(*frac_to_mm(fx, fy))
        cal.append((f[0], f[1], sx, sy))
    mdlH = fit_geom(cal, np.linspace(-200.0, 600.0, 401), z_fixed=0.0)

    print(f"  {'escenario':<24}{'híbrido 1px':>14}")
    print("  " + "-" * 38)
    for name, poses in SCENARIOS:
        tot, n = 0.0, 0
        for fx, fy in TEST_POINTS:
            for hp, he in poses:
                o = obs_noisy(fx, fy, hp, he)
                R, t = S.solve_pose(o["landmarks_px"], K)
                f = feat_hybrid(o["landmarks_px"], R, t)
                sx, sy = mm_to_px(*frac_to_mm(fx, fy))
                gx, gy = predict_geom(f[0], f[1], mdlH[1], mdlH[2], mdlH[3])
                tot += math.hypot(gx - sx, gy - sy)
                n += 1
        print(f"  {name:<24}{tot/max(n,1):>14.0f}")
    print("=" * 92)


if __name__ == "__main__":
    main()
