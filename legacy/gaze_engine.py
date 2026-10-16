"""
gaze_engine.py  ─  Motor de Eye Tracking en tiempo real
================================================================
Responsable de:
  • Capturar frames de la cámara
  • Detectar el rostro con MediaPipe FaceLandmarker (tasks API)
  • Estimar la pose 3D de la cabeza con cv2.solvePnP
  • Calcular el vector de mirada compuesto (iris + head pose VOR)
  • Filtrar el resultado con 1€ Filter anti-jitter
  • Publicar GazePoint a través de una cola thread-safe

Diseñado para tolerancia extrema a movimientos de cabeza y estabilidad de mirada.
Corre en un hilo de background; no bloquea la UI.
"""

import math, time, os, urllib.request, threading, queue
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, List

import numpy as np
import cv2
import mediapipe as mp

# ───────────────────────── Resolución de pantalla ─────────────────────────
import ctypes
try:
    import screeninfo
    _m = screeninfo.get_monitors()
    SCREEN_W = _m[0].width if _m else 1920
    SCREEN_H = _m[0].height if _m else 1080
except Exception:
    SCREEN_W = ctypes.windll.user32.GetSystemMetrics(0)
    SCREEN_H = ctypes.windll.user32.GetSystemMetrics(1)


# ════════════════════════════════════════════════════════
#  ESTRUCTURAS DE DATOS
# ════════════════════════════════════════════════════════

@dataclass
class GazePoint:
    """Un punto de mirada en coordenadas de pantalla con metadatos."""
    x: float                        # píxeles pantalla
    y: float                        # píxeles pantalla
    timestamp: float                # perf_counter()
    eye_dx: float = 0.0             # desplazamiento iris horizontal normalizado
    eye_dy: float = 0.0             # desplazamiento iris vertical normalizado
    yaw_deg: float = 0.0            # rotación cefálica yaw
    pitch_deg: float = 0.0          # rotación cefálica pitch
    pupil_radius_px: float = 0.0    # radio aproximado del iris en píxeles
    face_detected: bool = True
    raw_feat: dict = field(default_factory=dict)  # features crudos para calibración


# ════════════════════════════════════════════════════════
#  1€ FILTER  (Casiez et al., CHI 2012)
# ════════════════════════════════════════════════════════

class _LPF:
    def __init__(self): self.s = None
    def reset(self): self.s = None
    def __call__(self, x, alpha):
        if self.s is None: self.s = x
        else: self.s = alpha * x + (1. - alpha) * self.s
        return self.s

class OneEuroFilter:
    """Filtro 1€ adaptativo para una dimensión."""
    def __init__(self, min_cutoff=0.5, beta=0.007, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x = _LPF(); self._dx = _LPF()
        self._t = None; self._lx = None

    @staticmethod
    def _alpha(rate, cutoff):
        tau = 1. / (2. * math.pi * cutoff)
        te = 1. / max(rate, 1e-6)
        return 1. / (1. + tau / te)

    def reset(self):
        self._x.reset(); self._dx.reset()
        self._t = None; self._lx = None

    def __call__(self, x, t=None):
        if t is None: t = time.perf_counter()
        if self._t is None:
            self._t = t; self._lx = x
            return self._x(x, 1.0)
        dt = max(t - self._t, 1e-6)
        self._t = t
        rate = 1. / dt
        dx = (x - self._lx) / dt
        self._lx = x
        edx = self._dx(dx, self._alpha(rate, self.d_cutoff))
        fc = self.min_cutoff + self.beta * abs(edx)
        return self._x(x, self._alpha(rate, fc))

class OneEuroFilter2D:
    """Filtro 1€ para un par (x, y)."""
    def __init__(self, min_cutoff=0.5, beta=0.007, d_cutoff=1.0):
        self.fx = OneEuroFilter(min_cutoff, beta, d_cutoff)
        self.fy = OneEuroFilter(min_cutoff, beta, d_cutoff)

    def reset(self):
        self.fx.reset(); self.fy.reset()

    def __call__(self, x, y, t=None):
        return self.fx(x, t), self.fy(y, t)


# ════════════════════════════════════════════════════════
#  MEDIAPIPE WRAPPER  (compatible con tasks API ≥ 1.0)
# ════════════════════════════════════════════════════════

class FaceMeshWrapper:
    MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/"
                 "face_landmarker/face_landmarker/float16/latest/face_landmarker.task")
    MODEL_FILE = "face_landmarker.task"

    def __init__(self, model_dir: str = ""):
        model_path = os.path.join(model_dir or os.path.dirname(os.path.abspath(__file__)), self.MODEL_FILE)
        if not os.path.exists(model_path):
            print(f"[FaceMesh] Descargando modelo → {model_path}")
            urllib.request.urlretrieve(self.MODEL_URL, model_path)
            print(f"[FaceMesh] OK ({os.path.getsize(model_path)//1024} KB)")

        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        opts = mp_vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=model_path),
            running_mode=mp_vision.RunningMode.IMAGE,
            num_faces=1,
            min_face_detection_confidence=0.45,
            min_face_presence_confidence=0.45,
            min_tracking_confidence=0.45,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
        )
        self._det = mp_vision.FaceLandmarker.create_from_options(opts)

    def process(self, rgb: np.ndarray):
        """Devuelve lista de landmarks o None."""
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        res = self._det.detect(img)
        return res.face_landmarks[0] if res.face_landmarks else None


# ════════════════════════════════════════════════════════
#  HEAD POSE ESTIMATOR  (cv2.solvePnP)
# ════════════════════════════════════════════════════════

class HeadPoseEstimator:
    """
    Resuelve Perspective-n-Point para obtener Pitch / Yaw / Roll de la cabeza.

    Modelo canónico en mm (Openface convention):
      Nose tip → origin
      Y crece hacia abajo en imagen → pitch positivo = mirar abajo
      X crece hacia la derecha de la imagen → yaw positivo = girar a la derecha de la cámara
    """
    # (landmark_idx, [X_3d, Y_3d, Z_3d])
    _MODEL_PTS = np.array([
        [  0.0,    0.0,   0.0],   # 1  punta nariz
        [  0.0, -330.0, -65.0],   # 152 mentón
        [-225.0,  170.0,-135.0],  # 33  canto ext ojo derecho (cámara: izq)
        [ 225.0,  170.0,-135.0],  # 263 canto ext ojo izquierdo (cámara: der)
        [-150.0, -150.0,-125.0],  # 61  comisura bucal der
        [ 150.0, -150.0,-125.0],  # 291 comisura bucal izq
    ], dtype=np.float64)
    _IDX = [1, 152, 33, 263, 61, 291]

    def __init__(self):
        self._K = None
        self._dist = np.zeros((4, 1))

    def _init_K(self, w, h):
        f = w  # focal length ≈ width (heurístico para lentes estándar ~60°)
        self._K = np.array([[f, 0, w/2], [0, f, h/2], [0, 0, 1]], dtype=np.float64)

    def estimate(self, lm, w: int, h: int) -> Optional[Dict]:
        if self._K is None: self._init_K(w, h)

        img_pts = np.array([[lm[i].x * w, lm[i].y * h] for i in self._IDX], dtype=np.float64)
        ok, rvec, tvec = cv2.solvePnP(self._MODEL_PTS, img_pts, self._K, self._dist,
                                       flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok: return None

        rmat, _ = cv2.Rodrigues(rvec)
        sy = math.sqrt(rmat[0,0]**2 + rmat[1,0]**2)
        if sy > 1e-6:
            pitch = math.atan2(rmat[2,1], rmat[2,2])
            yaw   = math.atan2(-rmat[2,0], sy)
            roll  = math.atan2(rmat[1,0], rmat[0,0])
        else:
            pitch = math.atan2(-rmat[1,2], rmat[1,1])
            yaw   = math.atan2(-rmat[2,0], sy)
            roll  = 0.0

        return dict(rvec=rvec, tvec=tvec, K=self._K, dist=self._dist,
                    pitch=pitch, yaw=yaw, roll=roll,
                    pitch_deg=math.degrees(pitch),
                    yaw_deg=math.degrees(yaw),
                    roll_deg=math.degrees(roll))

    def draw_axes(self, frame, pose, length=80.0):
        pts, _ = cv2.projectPoints(
            np.float64([[0,0,0],[length,0,0],[0,length,0],[0,0,length]]),
            pose['rvec'], pose['tvec'], pose['K'], pose['dist'])
        pts = pts.reshape(-1, 2).astype(int)
        o = tuple(pts[0])
        cv2.line(frame, o, tuple(pts[1]), (0,0,255), 2)   # X rojo
        cv2.line(frame, o, tuple(pts[2]), (0,255,0), 2)   # Y verde
        cv2.line(frame, o, tuple(pts[3]), (255,0,0), 2)   # Z azul


# ════════════════════════════════════════════════════════
#  EYE GAZE TRACKER  (iris displacement + VOR correction)
# ════════════════════════════════════════════════════════

class EyeGazeTracker:
    """
    Extrae el desplazamiento normalizado del iris dentro del ojo y lo combina
    con la orientación de la cabeza para compensar el Reflejo Vestíbulo-Ocular (VOR).

    Ejes de salida:
      +X → mirada a la DERECHA del usuario
      +Y → mirada ABAJO del usuario
    (compatible con el sistema de coordenadas estándar de pantalla)
    """
    # Índices de landmarks de FaceMesh 3D (refine_landmarks=True)
    # Ojo DERECHO del usuario (aparece a la IZQUIERDA en imagen de cámara sin flip)
    R_IRIS  = 468        # centro del iris
    R_OUT   = 33         # canto exterior
    R_IN    = 133        # canto interior
    R_TOP   = 159        # párpado superior
    R_BOT   = 145        # párpado inferior

    # Ojo IZQUIERDO del usuario (aparece a la DERECHA en imagen sin flip)
    L_IRIS  = 473
    L_IN    = 362
    L_OUT   = 263
    L_TOP   = 386
    L_BOT   = 374

    def _eye_disp(self, lm, iris, out_, in_, top, bot):
        """
        Desplazamiento del iris relativo al centro del ojo,
        normalizado por el ancho y alto del ojo.
        Resultado en [-0.5, +0.5] aproximadamente.
        """
        cx = (lm[out_].x + lm[in_].x) / 2.
        cy = (lm[top].y  + lm[bot].y) / 2.
        ew = max(abs(lm[in_].x - lm[out_].x), 1e-5)
        eh = max(abs(lm[bot].y - lm[top].y),  1e-5)
        dx = (lm[iris].x - cx) / ew
        dy = (lm[iris].y - cy) / eh
        return dx, dy

    def _pupil_px(self, lm, iris, in_, out_, h, w):
        """Radio aproximado del iris en píxeles (distancia canto interno→externo / 2)."""
        r = math.hypot((lm[in_].x - lm[out_].x) * w,
                        (lm[in_].y - lm[out_].y) * h) / 2.
        return r

    def extract(self, lm, pose: Dict, w: int, h: int) -> Dict:
        """
        Devuelve las características brutas de mirada y el vector combinado.

        Head Pose Compensation (VOR-aware):
        ─────────────────────────────────────────────────────────────────
        Al girar la cabeza en ángulo θ_yaw, el VOR compensa haciendo
        rotar el ojo -θ_yaw, de forma que el iris PARECE desplazarse
        aunque el punto de fijación sea el mismo.
        Para compensar sumamos k·θ_yaw al vector del iris:
            g_x = iris_dx + k_yaw * yaw
            g_y = iris_dy + k_pitch * pitch

        Los coeficientes (k_yaw, k_pitch) son empíricos; los valores
        óptimos dependen de la distancia al monitor y serán refinados
        por la calibración (regresión polinomial).

        Nota de signo:
          - iris.x crece a la derecha de la IMAGEN (con flip = a la derecha del usuario)
          - yaw positivo  = cabeza girada a la derecha de la CÁMARA
                          = usuario mira más a la izquierda en imagen sin flip
                          → en imagen con flip: mira a la DERECHA → mismo signo
        """
        r_dx, r_dy = self._eye_disp(lm, self.R_IRIS, self.R_OUT, self.R_IN, self.R_TOP, self.R_BOT)
        l_dx, l_dy = self._eye_disp(lm, self.L_IRIS, self.L_OUT, self.L_IN, self.L_TOP, self.L_BOT)

        # Promedio interocular (reduce varianza ~√2)
        eye_dx = (r_dx + l_dx) / 2.
        eye_dy = (r_dy + l_dy) / 2.

        yaw   = pose['yaw']
        pitch = pose['pitch']

        # Coeficientes de compensación (sintonizados empíricamente)
        # Se mantienen moderados; la calibración ajustará el resto
        K_YAW   = 0.65
        K_PITCH = 0.55

        # Nota: la imagen ya está flippeada horizontalmente en GazeEngine,
        # por eso el signo de iris_dx coincide con la dirección del usuario.
        # pitch positivo = cabeza inclinada abajo → iris sube → iris_dy negativo
        # sumamos -k_pitch * pitch para compensar
        gx = eye_dx + K_YAW   *  yaw
        gy = eye_dy - K_PITCH *  pitch   # signo negativo compensa el pitch

        pupil_r = (self._pupil_px(lm, self.R_IRIS, self.R_IN, self.R_OUT, h, w) +
                   self._pupil_px(lm, self.L_IRIS, self.L_IN, self.L_OUT, h, w)) / 2.

        return dict(
            eye_dx=eye_dx, eye_dy=eye_dy,
            yaw=yaw, pitch=pitch, roll=pose['roll'],
            yaw_deg=pose['yaw_deg'], pitch_deg=pose['pitch_deg'],
            gx=gx, gy=gy,
            pupil_radius_px=pupil_r,
            r_iris=lm[self.R_IRIS],
            l_iris=lm[self.L_IRIS],
        )


# ════════════════════════════════════════════════════════
#  CALIBRADOR  (Polynomial Ridge Regression 2nd order)
# ════════════════════════════════════════════════════════

class GazeCalibrator:
    """
    Mapea (gx, gy) → (screen_x, screen_y) mediante regresión
    polinomial multivariada de 2° orden con regularización Ridge.

    φ(gx, gy, yaw, pitch) ∈ ℝ¹²
    W = (Φᵀ Φ + λI*)⁻¹ Φᵀ Y    ← solución Ridge (Tikhonov)
    """
    # 9 puntos en cuadrícula 3×3 (máxima cobertura espacial)
    TARGETS_9 = [
        (0.10, 0.10), (0.50, 0.10), (0.90, 0.10),
        (0.10, 0.50), (0.50, 0.50), (0.90, 0.50),
        (0.10, 0.90), (0.50, 0.90), (0.90, 0.90),
    ]
    # 5 puntos rápido (alternativo)
    TARGETS_5 = [
        (0.50, 0.50),
        (0.10, 0.10), (0.90, 0.10),
        (0.90, 0.90), (0.10, 0.90),
    ]

    def __init__(self, screen_w=SCREEN_W, screen_h=SCREEN_H, n_points=9, alpha_l2=1e-3):
        self.sw = screen_w
        self.sh = screen_h
        self.targets = self.TARGETS_9 if n_points == 9 else self.TARGETS_5
        self.alpha = alpha_l2
        self.is_calibrated = False
        self._samples: List[Tuple] = []  # (phi, target_x, target_y)
        self.Wx = None
        self.Wy = None

    @staticmethod
    def _phi(feat: Dict) -> np.ndarray:
        gx = feat['gx']; gy = feat['gy']
        yaw = feat['yaw']; pitch = feat['pitch']
        return np.array([
            1., gx, gy, yaw, pitch,
            gx**2, gy**2, gx*gy,
            gx*yaw, gy*pitch, yaw**2, pitch**2
        ])

    def add_sample(self, feat: Dict, tx: int, ty: int):
        self._samples.append((self._phi(feat), float(tx), float(ty)))

    def train(self) -> bool:
        n = len(self._samples)
        if n < 10:
            return False
        X   = np.array([s[0] for s in self._samples])
        Y_x = np.array([s[1] for s in self._samples])
        Y_y = np.array([s[2] for s in self._samples])

        nf = X.shape[1]
        lam = np.eye(nf) * self.alpha
        lam[0, 0] = 0.  # no regularizar el bias

        XtX = X.T @ X + lam
        try:
            self.Wx = np.linalg.solve(XtX, X.T @ Y_x)
            self.Wy = np.linalg.solve(XtX, X.T @ Y_y)
        except np.linalg.LinAlgError:
            pinv = np.linalg.pinv(XtX)
            self.Wx = pinv @ (X.T @ Y_x)
            self.Wy = pinv @ (X.T @ Y_y)

        self.is_calibrated = True
        print(f"[Calibrador] Entrenado con {n} muestras.")
        return True

    def predict(self, feat: Dict) -> Tuple[int, int]:
        phi = self._phi(feat)
        if self.is_calibrated:
            px = float(phi @ self.Wx)
            py = float(phi @ self.Wy)
        else:
            # Heurístico pre-calibración: mapeo directo centrado
            px = self.sw / 2. + feat['gx'] * self.sw * 3.2
            py = self.sh / 2. + feat['gy'] * self.sh * 3.8
        px = int(np.clip(px, 0, self.sw - 1))
        py = int(np.clip(py, 0, self.sh - 1))
        return px, py

    def reset(self):
        self._samples.clear()
        self.Wx = self.Wy = None
        self.is_calibrated = False


# ════════════════════════════════════════════════════════
#  GAZE ENGINE  (hilo de captura + estimación)
# ════════════════════════════════════════════════════════

class GazeEngine:
    """
    Corre en un hilo de background.
    Publica GazePoint en self.gaze_queue (maxsize=2, descarta old frames).
    También expone el último frame debug en self.debug_frame (para visualización).
    """

    def __init__(self, camera_index: int = 0, calibrator: Optional[GazeCalibrator] = None):
        self.calibrator = calibrator or GazeCalibrator()
        self._cam_idx = camera_index

        self._wrapper = FaceMeshWrapper()
        self._head_est = HeadPoseEstimator()
        self._eye_track = EyeGazeTracker()
        self._filter = OneEuroFilter2D(min_cutoff=0.5, beta=0.007, d_cutoff=1.0)

        self.gaze_queue: queue.Queue[GazePoint] = queue.Queue(maxsize=2)
        self.debug_frame: Optional[np.ndarray] = None   # compartido, leer con lock
        self._frame_lock = threading.Lock()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.fps = 0.0

        # Estadísticas
        self.sample_rate = 0.0
        self._sample_times: List[float] = []

    # ─── Interfaz pública ────────────────────────────────

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="GazeEngine")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)

    def get_latest_gaze(self) -> Optional[GazePoint]:
        """Obtiene el GazePoint más reciente (no bloquea)."""
        latest = None
        while not self.gaze_queue.empty():
            try: latest = self.gaze_queue.get_nowait()
            except queue.Empty: break
        return latest

    def get_debug_frame(self) -> Optional[np.ndarray]:
        with self._frame_lock:
            return self.debug_frame.copy() if self.debug_frame is not None else None

    # ─── Loop interno ────────────────────────────────────

    def _loop(self):
        cap = cv2.VideoCapture(self._cam_idx)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 30)

        t_prev = time.perf_counter()

        while self._running and cap.isOpened():
            ok, raw = cap.read()
            if not ok:
                time.sleep(0.01)
                continue

            t_now = time.perf_counter()
            dt = t_now - t_prev
            t_prev = t_now
            self.fps = 0.9 * self.fps + 0.1 / max(dt, 1e-4)

            # Espejo horizontal → imagen "natural" para el usuario
            frame = cv2.flip(raw, 1)
            h, w = frame.shape[:2]

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            lm = self._wrapper.process(rgb)

            gp = GazePoint(x=SCREEN_W/2, y=SCREEN_H/2, timestamp=t_now, face_detected=False)

            if lm is not None:
                pose = self._head_est.estimate(lm, w, h)
                if pose:
                    feat = self._eye_track.extract(lm, pose, w, h)
                    raw_x, raw_y = self.calibrator.predict(feat)
                    fx, fy = self._filter(raw_x, raw_y, t_now)

                    gp = GazePoint(
                        x=fx, y=fy, timestamp=t_now,
                        eye_dx=feat['eye_dx'], eye_dy=feat['eye_dy'],
                        yaw_deg=feat['yaw_deg'], pitch_deg=feat['pitch_deg'],
                        pupil_radius_px=feat['pupil_radius_px'],
                        face_detected=True,
                    )

                    # ── Debug frame ──────────────────────────────────
                    self._head_est.draw_axes(frame, pose)

                    ri = feat['r_iris'];  li = feat['l_iris']
                    rp = (int(ri.x * w), int(ri.y * h))
                    lp = (int(li.x * w), int(li.y * h))
                    cv2.circle(frame, rp, 4, (0, 255, 255), -1)
                    cv2.circle(frame, lp, 4, (0, 255, 255), -1)

                    # Vector de mirada correctamente orientado en el frame
                    sv = 200.
                    # gx >0 = usuario mira derecha → en imagen flippeada → dx positivo OK
                    cv2.arrowedLine(frame, rp,
                        (int(rp[0] + feat['gx']*sv), int(rp[1] + feat['gy']*sv)),
                        (0, 0, 255), 2, tipLength=0.3)
                    cv2.arrowedLine(frame, lp,
                        (int(lp[0] + feat['gx']*sv), int(lp[1] + feat['gy']*sv)),
                        (0, 0, 255), 2, tipLength=0.3)

                    calib_lbl = "Cal: OK" if self.calibrator.is_calibrated else "Cal: NO (ver main)"
                    cv2.putText(frame, calib_lbl, (10, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                                (0,255,0) if self.calibrator.is_calibrated else (0,130,255), 2)

                # HUD base
                cv2.putText(frame, f"FPS:{self.fps:.0f}  Y:{pose.get('yaw_deg',0):+.1f}  P:{pose.get('pitch_deg',0):+.1f}" if lm and pose else f"FPS:{self.fps:.0f}  Sin cara",
                            (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,0), 2)
                cv2.putText(frame, f"Gaze px: {int(gp.x)},{int(gp.y)}",
                            (10, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,255), 2)

            else:
                cv2.putText(frame, f"FPS:{self.fps:.0f}  Sin cara",
                            (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,100,255), 2)

            # Publicar
            with self._frame_lock:
                self.debug_frame = frame

            # Cola con overwrite (siempre el dato más fresco)
            try:
                self.gaze_queue.put_nowait(gp)
            except queue.Full:
                try: self.gaze_queue.get_nowait()
                except queue.Empty: pass
                try: self.gaze_queue.put_nowait(gp)
                except queue.Full: pass

            # Frecuencia de muestreo
            self._sample_times.append(t_now)
            if len(self._sample_times) > 90:
                self._sample_times.pop(0)
            if len(self._sample_times) > 1:
                span = self._sample_times[-1] - self._sample_times[0]
                self.sample_rate = (len(self._sample_times) - 1) / max(span, 1e-4)

        cap.release()
