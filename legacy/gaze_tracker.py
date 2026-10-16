"""
=============================================================================
Gaze Estimation & OS-Level Eye Tracking System para Entornos de Alta Movilidad
Tolerancia Extrema a Movimientos de Cabeza y Reduccion de Jitter
=============================================================================

Arquitectura:
1. OneEuroFilter: Filtro dinámico adaptativo que elimina micro-temblores (jitter)
   en fijaciones estáticas sin inducir lag durante movimientos sacádicos.
2. HeadPoseEstimator: Resuelve el problema PnP (Perspective-n-Point) con cv2.solvePnP
   y un modelo antropométrico 3D canónico para extraer Pitch, Yaw, Roll y Traslación.
3. EyeGazeTracker: Extrae la dinámica del iris relativa a los cantos oculares y
   fusiona la pose cefálica con el vector ocular para compensar el Reflejo
   Vestíbulo-Ocular (VOR).
4. GazeCalibrator: Rutina de calibración interactiva y regresión polinomial multivariada
   de segundo orden con regularización de Tikhonov (Ridge Regression).
5. FaceMeshWrapper: Compatibilidad universal con MediaPipe 1.0+ (tasks) y versiones anteriores (solutions).
6. GazeApplication: Orquestador en tiempo real con ventana de depuración OpenCV
   y control opcional del cursor del sistema operativo (Windows SetCursorPos).
"""

import math
import time
import os
import sys
import urllib.request
import ctypes
from typing import Tuple, List, Optional, Dict
import numpy as np
import cv2
import mediapipe as mp

# Detección de resolución del monitor principal
try:
    import screeninfo
    monitors = screeninfo.get_monitors()
    SCREEN_WIDTH = monitors[0].width if monitors else 1920
    SCREEN_HEIGHT = monitors[0].height if monitors else 1080
except Exception:
    SCREEN_WIDTH = ctypes.windll.user32.GetSystemMetrics(0) if hasattr(ctypes, 'windll') else 1920
    SCREEN_HEIGHT = ctypes.windll.user32.GetSystemMetrics(1) if hasattr(ctypes, 'windll') else 1080


# ============================================================================
# 0. ADAPTADOR UNIVERSAL DE MEDIAPIPE (Tasks API / Solutions API)
# ============================================================================

class FaceMeshWrapper:
    """
    Abstracción transparente para compatibilidad con:
    - MediaPipe >= 1.0.0 (API moderna 'tasks.python.vision.FaceLandmarker')
    - MediaPipe < 0.10.0 (API 'mediapipe.solutions.face_mesh')
    """
    MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task"
    MODEL_FILENAME = "face_landmarker.task"

    def __init__(self):
        self.use_tasks_api = not hasattr(mp, 'solutions')
        
        if self.use_tasks_api:
            # Descargar modelo task si no está presente localmente
            model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.MODEL_FILENAME)
            if not os.path.exists(model_path):
                print(f"[MediaPipe] Descargando modelo de alta precisión '{self.MODEL_FILENAME}'...")
                urllib.request.urlretrieve(self.MODEL_URL, model_path)
                print(f"[MediaPipe] Modelo descargado exitosamente ({os.path.getsize(model_path)} bytes).")

            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision

            base_options = python.BaseOptions(model_asset_path=model_path)
            options = vision.FaceLandmarkerOptions(
                base_options=base_options,
                running_mode=vision.RunningMode.IMAGE,
                num_faces=1,
                min_face_detection_confidence=0.5,
                min_face_presence_confidence=0.5,
                min_tracking_confidence=0.5,
                output_face_blendshapes=False,
                output_facial_transformation_matrixes=False
            )
            self.detector = vision.FaceLandmarker.create_from_options(options)
        else:
            self.detector = mp.solutions.face_mesh.FaceMesh(
                max_num_faces=1,
                refine_landmarks=True,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5
            )

    def process(self, rgb_frame: np.ndarray):
        """Retorna lista de landmarks normalizados (con atributos .x, .y, .z) o None."""
        if self.use_tasks_api:
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
            result = self.detector.detect(mp_image)
            if result.face_landmarks and len(result.face_landmarks) > 0:
                return result.face_landmarks[0]
            return None
        else:
            result = self.detector.process(rgb_frame)
            if result.multi_face_landmarks and len(result.multi_face_landmarks) > 0:
                return result.multi_face_landmarks[0].landmark
            return None


# ============================================================================
# 1. FILTRO DINÁMICO ANTI-JITTER (1€ FILTER - Casiez et al., CHI 2012)
# ============================================================================

class LowPassFilter:
    """Filtro pasa-bajas exponencial de primer orden."""
    def __init__(self, alpha: float = 1.0):
        self.alpha = alpha
        self.s = None

    def reset(self):
        self.s = None

    def filter(self, value: float, alpha: Optional[float] = None) -> float:
        if alpha is not None:
            self.alpha = alpha
        if self.s is None:
            self.s = value
        else:
            self.s = self.alpha * value + (1.0 - self.alpha) * self.s
        return self.s


class OneEuroFilter:
    """
    Implementación rigurosa del 1€ Filter.
    
    A bajas velocidades (fijación ocular), la frecuencia de corte decae a min_cutoff,
    eliminando el micro-temblor de los sensores de la cámara.
    A altas velocidades (sacadas oculares o giros de cabeza), la frecuencia de corte
    aumenta linealmente proporcional a la velocidad, eliminando cualquier lag perceptible.
    """
    def __init__(self, min_cutoff: float = 0.35, beta: float = 0.06, d_cutoff: float = 1.0):
        """
        Args:
            min_cutoff: Frecuencia de corte mínima en Hz (disminuir para mayor estabilidad estática).
            beta: Coeficiente de velocidad para la frecuencia de corte (aumentar para menor lag en sacadas).
            d_cutoff: Frecuencia de corte para la derivada de la señal en Hz.
        """
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)
        self.x_filter = LowPassFilter()
        self.dx_filter = LowPassFilter()
        self.last_time = None
        self.last_x = None

    def _alpha(self, rate: float, cutoff: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        te = 1.0 / rate if rate > 0 else 0.033
        return 1.0 / (1.0 + tau / te)

    def filter(self, x: float, timestamp: Optional[float] = None) -> float:
        if timestamp is None:
            timestamp = time.perf_counter()

        if self.last_time is None:
            self.last_time = timestamp
            self.last_x = x
            return self.x_filter.filter(x)

        dt = timestamp - self.last_time
        self.last_time = timestamp
        if dt <= 1e-6:
            dt = 1e-4

        rate = 1.0 / dt

        # Derivada del valor actual
        dx = (x - self.last_x) / dt
        self.last_x = x

        # Filtrar la derivada con d_cutoff fijo
        a_d = self._alpha(rate, self.d_cutoff)
        edx = self.dx_filter.filter(dx, a_d)

        # Frecuencia de corte adaptativa basada en la magnitud de la velocidad
        cutoff = self.min_cutoff + self.beta * abs(edx)
        a = self._alpha(rate, cutoff)

        return self.x_filter.filter(x, a)

    def reset(self):
        self.x_filter.reset()
        self.dx_filter.reset()
        self.last_time = None
        self.last_x = None


class OneEuroFilter2D:
    """Envoltura 2D para filtrar coordenadas espaciales (X, Y)."""
    def __init__(self, min_cutoff: float = 0.35, beta: float = 0.06, d_cutoff: float = 1.0):
        self.filter_x = OneEuroFilter(min_cutoff, beta, d_cutoff)
        self.filter_y = OneEuroFilter(min_cutoff, beta, d_cutoff)

    def filter(self, x: float, y: float, timestamp: Optional[float] = None) -> Tuple[float, float]:
        fx = self.filter_x.filter(x, timestamp)
        fy = self.filter_y.filter(y, timestamp)
        return fx, fy

    def reset(self):
        self.filter_x.reset()
        self.filter_y.reset()


# ============================================================================
# 2. ESTIMADOR DE POSTURA CEFÁLICA 3D (HEAD POSE ESTIMATOR VIA solvePnP)
# ============================================================================

class HeadPoseEstimator:
    """
    Calcula la orientación cefálica tridimensional (Pitch, Yaw, Roll)
    y traslación mediante cv2.solvePnP acoplado a un modelo antropométrico 3D canónico.
    """
    def __init__(self):
        # Modelo 3D canónico de rostro en coordenadas métricas (milímetros)
        # Puntos de referencia facial:
        #  1: Punta de la nariz
        #  152: Mentón
        #  33: Canto exterior ojo derecho (perspectiva cámara: izq)
        #  263: Canto exterior ojo izquierdo (perspectiva cámara: der)
        #  61: Comisura labial derecha
        #  291: Comisura labial izquierda
        #  10: Glabela / Frente superior
        self.model_points_3d = np.array([
            [0.0,    0.0,    0.0],    # Punta de la nariz (Landmark 1)
            [0.0,   -330.0, -65.0],   # Mentón (Landmark 152)
            [-225.0, 170.0, -135.0],  # Canto exterior ojo derecho (Landmark 33)
            [225.0,  170.0, -135.0],  # Canto exterior ojo izquierdo (Landmark 263)
            [-150.0,-150.0, -125.0],  # Comisura bucal derecha (Landmark 61)
            [150.0, -150.0, -125.0],  # Comisura bucal izquierda (Landmark 291)
            [0.0,    280.0, -100.0]   # Glabela / Frente (Landmark 10)
        ], dtype=np.float64)

        self.landmark_indices = [1, 152, 33, 263, 61, 291, 10]
        self.camera_matrix = None
        self.dist_coeffs = np.zeros((4, 1), dtype=np.float64)

    def _init_camera_matrix(self, img_w: int, img_h: int):
        focal_length = img_w
        center = (img_w / 2.0, img_h / 2.0)
        self.camera_matrix = np.array([
            [focal_length, 0, center[0]],
            [0, focal_length, center[1]],
            [0, 0, 1]
        ], dtype=np.float64)

    def estimate(self, landmarks, img_w: int, img_h: int) -> Optional[Dict]:
        """Resuelve PnP y extrae ángulos de Euler y vector de traslación."""
        if self.camera_matrix is None:
            self._init_camera_matrix(img_w, img_h)

        image_points = []
        for idx in self.landmark_indices:
            pt = landmarks[idx]
            image_points.append([pt.x * img_w, pt.y * img_h])
        image_points = np.array(image_points, dtype=np.float64)

        success, rvec, tvec = cv2.solvePnP(
            self.model_points_3d,
            image_points,
            self.camera_matrix,
            self.dist_coeffs,
            flags=cv2.SOLVEPNP_ITERATIVE
        )

        if not success:
            return None

        # Convertir vector de rotación a matriz de rotación vía Rodrigues
        rmat, _ = cv2.Rodrigues(rvec)

        # Descomposición en ángulos de Euler (Pitch, Yaw, Roll)
        sy = math.sqrt(rmat[0, 0] ** 2 + rmat[1, 0] ** 2)
        singular = sy < 1e-6

        if not singular:
            pitch = math.atan2(rmat[2, 1], rmat[2, 2])
            yaw = math.atan2(-rmat[2, 0], sy)
            roll = math.atan2(rmat[1, 0], rmat[0, 0])
        else:
            pitch = math.atan2(-rmat[1, 2], rmat[1, 1])
            yaw = math.atan2(-rmat[2, 0], sy)
            roll = 0.0

        return {
            "rvec": rvec,
            "tvec": tvec,
            "rmat": rmat,
            "pitch_deg": math.degrees(pitch),
            "yaw_deg": math.degrees(yaw),
            "roll_deg": math.degrees(roll),
            "pitch_rad": pitch,
            "yaw_rad": yaw,
            "roll_rad": roll
        }

    def draw_axes(self, frame: np.ndarray, rvec: np.ndarray, tvec: np.ndarray, length: float = 100.0):
        """Dibuja los ejes de rotación 3D proyectados sobre la nariz del sujeto."""
        axis_points_3d = np.array([
            [0.0, 0.0, 0.0],
            [length, 0.0, 0.0],   # X: Rojo
            [0.0, length, 0.0],   # Y: Verde
            [0.0, 0.0, length]    # Z: Azul
        ], dtype=np.float64)

        img_pts, _ = cv2.projectPoints(axis_points_3d, rvec, tvec, self.camera_matrix, self.dist_coeffs)
        img_pts = img_pts.reshape(-1, 2).astype(int)

        origin = tuple(img_pts[0])
        cv2.line(frame, origin, tuple(img_pts[1]), (0, 0, 255), 2)  # X
        cv2.line(frame, origin, tuple(img_pts[2]), (0, 255, 0), 2)  # Y
        cv2.line(frame, origin, tuple(img_pts[3]), (255, 0, 0), 2)  # Z


# ============================================================================
# 3. RASTREADOR DE MIRADA OCULAR COMBINADA (EYE GAZE TRACKER CON VOR)
# ============================================================================

class EyeGazeTracker:
    """
    Extrae la cinemática ocular fina del iris y la combina con la orientación cefálica
    para formular un Vector de Mirada Compuesto robusto frente a sacudidas de cabeza.
    """
    def __init__(self):
        # Índices anatómicos de FaceMesh
        self.R_IRIS_CENTER = 468
        self.R_CANTHUS_OUTER = 33
        self.R_CANTHUS_INNER = 133
        self.R_LID_TOP = 159
        self.R_LID_BOTTOM = 145

        self.L_IRIS_CENTER = 473
        self.L_CANTHUS_INNER = 362
        self.L_CANTHUS_OUTER = 263
        self.L_LID_TOP = 386
        self.L_LID_BOTTOM = 374

    def _extract_eye_displacement(self, landmarks, iris_idx: int, outer_idx: int, inner_idx: int,
                                 top_idx: int, bottom_idx: int) -> Tuple[float, float, float, float]:
        """Calcula el desplazamiento normalizado del iris respecto al centro ocular."""
        iris = landmarks[iris_idx]
        outer = landmarks[outer_idx]
        inner = landmarks[inner_idx]
        top = landmarks[top_idx]
        bottom = landmarks[bottom_idx]

        eye_center_x = (outer.x + inner.x) / 2.0
        eye_center_y = (top.y + bottom.y) / 2.0

        eye_width = abs(inner.x - outer.x)
        eye_height = abs(bottom.y - top.y)
        if eye_width < 1e-4: eye_width = 1e-4
        if eye_height < 1e-4: eye_height = 1e-4

        dx = (iris.x - eye_center_x) / eye_width
        dy = (iris.y - eye_center_y) / eye_height

        return dx, dy, eye_center_x, eye_center_y

    def extract_composite_features(self, landmarks, head_pose: Dict) -> Dict:
        """
        Formulación del vector de características combinadas con compensación de VOR.
        """
        r_dx, r_dy, r_cx, r_cy = self._extract_eye_displacement(
            landmarks, self.R_IRIS_CENTER, self.R_CANTHUS_OUTER, self.R_CANTHUS_INNER,
            self.R_LID_TOP, self.R_LID_BOTTOM
        )
        l_dx, l_dy, l_cx, l_cy = self._extract_eye_displacement(
            landmarks, self.L_IRIS_CENTER, self.L_CANTHUS_OUTER, self.L_CANTHUS_INNER,
            self.L_LID_TOP, self.L_LID_BOTTOM
        )

        avg_eye_dx = (r_dx + l_dx) / 2.0
        avg_eye_dy = (r_dy + l_dy) / 2.0

        yaw_rad = head_pose["yaw_rad"]
        pitch_rad = head_pose["pitch_rad"]
        roll_rad = head_pose["roll_rad"]
        tvec = head_pose["tvec"].flatten()

        # Fusión VOR directa
        raw_combined_x = avg_eye_dx + 0.70 * yaw_rad
        raw_combined_y = avg_eye_dy + 0.60 * pitch_rad

        return {
            "eye_dx": avg_eye_dx,
            "eye_dy": avg_eye_dy,
            "yaw_rad": yaw_rad,
            "pitch_rad": pitch_rad,
            "roll_rad": roll_rad,
            "tx": tvec[0],
            "ty": tvec[1],
            "tz": tvec[2],
            "raw_combined_x": raw_combined_x,
            "raw_combined_y": raw_combined_y,
            "r_iris": landmarks[self.R_IRIS_CENTER],
            "l_iris": landmarks[self.L_IRIS_CENTER]
        }


# ============================================================================
# 4. CALIBRADOR ESPACIAL (POLYNOMIAL MULTIVARIATE 2ND-ORDER RIDGE REGRESSION)
# ============================================================================

class GazeCalibrator:
    """
    Mapeo espacial de vector compuesto a resolución de pantalla (P_x, P_y)
    mediante regresión polinomial multivariada de 2do orden con regularización Ridge.
    """
    def __init__(self, screen_w: int = SCREEN_WIDTH, screen_h: int = SCREEN_HEIGHT):
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.is_calibrated = False

        # 5 puntos de calibración de alta velocidad (Centro + 4 esquinas)
        self.calib_targets = [
            (0.50, 0.50),  # Centro
            (0.10, 0.10),  # Sup-Izq
            (0.90, 0.10),  # Sup-Der
            (0.90, 0.90),  # Inf-Der
            (0.10, 0.90),  # Inf-Izq
        ]

        self.current_step = 0
        self.collected_samples = []
        self.weights_x = None
        self.weights_y = None
        self.alpha_l2 = 1e-3

    def _polynomial_expansion(self, feat: Dict) -> np.ndarray:
        """Expande variables a espacio cuadrático de 12 dimensiones."""
        ex = feat["eye_dx"]
        ey = feat["eye_dy"]
        yaw = feat["yaw_rad"]
        pitch = feat["pitch_rad"]

        return np.array([
            1.0,
            ex,
            ey,
            yaw,
            pitch,
            ex ** 2,
            ey ** 2,
            ex * ey,
            ex * yaw,
            ey * pitch,
            yaw ** 2,
            pitch ** 2
        ], dtype=np.float64)

    def add_sample(self, feat: Dict, target_screen_pos: Tuple[int, int]):
        poly = self._polynomial_expansion(feat)
        self.collected_samples.append((poly, target_screen_pos))

    def train(self) -> bool:
        if len(self.collected_samples) < 15:
            return False

        X = np.array([sample[0] for sample in self.collected_samples])
        Y = np.array([sample[1] for sample in self.collected_samples])

        num_features = X.shape[1]
        I = np.eye(num_features)
        I[0, 0] = 0.0  # Sin penalización de sesgo

        XtX = np.dot(X.T, X) + self.alpha_l2 * I
        XtY_x = np.dot(X.T, Y[:, 0])
        XtY_y = np.dot(X.T, Y[:, 1])

        try:
            self.weights_x = np.linalg.solve(XtX, XtY_x)
            self.weights_y = np.linalg.solve(XtX, XtY_y)
            self.is_calibrated = True
            return True
        except np.linalg.LinAlgError:
            pinv = np.linalg.pinv(XtX)
            self.weights_x = np.dot(pinv, XtY_x)
            self.weights_y = np.dot(pinv, XtY_y)
            self.is_calibrated = True
            return True

    def predict(self, feat: Dict) -> Tuple[int, int]:
        if not self.is_calibrated:
            cx = self.screen_w / 2.0
            cy = self.screen_h / 2.0
            raw_x = cx + feat["raw_combined_x"] * self.screen_w * 3.5
            raw_y = cy + feat["raw_combined_y"] * self.screen_h * 4.0
            return int(np.clip(raw_x, 0, self.screen_w - 1)), int(np.clip(raw_y, 0, self.screen_h - 1))

        poly = self._polynomial_expansion(feat)
        pred_x = np.dot(poly, self.weights_x)
        pred_y = np.dot(poly, self.weights_y)

        pred_x = int(np.clip(pred_x, 0, self.screen_w - 1))
        pred_y = int(np.clip(pred_y, 0, self.screen_h - 1))
        return pred_x, pred_y

    def reset(self):
        self.is_calibrated = False
        self.current_step = 0
        self.collected_samples.clear()
        self.weights_x = None
        self.weights_y = None


# ============================================================================
# 5. APLICACIÓN PRINCIPAL EN TIEMPO REAL & VISUALIZADOR DEBUG
# ============================================================================

class GazeApplication:
    """Orquestador central en tiempo real a 30+ FPS."""
    def __init__(self, camera_index: int = 0, control_mouse: bool = False):
        self.camera_index = camera_index
        self.control_mouse = control_mouse

        self.cap = cv2.VideoCapture(self.camera_index)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 30)

        # Inicialización del adaptador universal de FaceMesh
        self.face_mesh_wrapper = FaceMeshWrapper()

        self.head_pose = HeadPoseEstimator()
        self.gaze_tracker = EyeGazeTracker()
        self.calibrator = GazeCalibrator(SCREEN_WIDTH, SCREEN_HEIGHT)

        # Filtro OneEuroFilter 2D
        self.cursor_filter = OneEuroFilter2D(min_cutoff=0.35, beta=0.06, d_cutoff=1.0)

        self.is_calibrating = False
        self.calib_step = 0
        self.calib_phase = "WAIT"
        self.phase_start_time = 0.0

        self.prev_time = time.perf_counter()
        self.fps = 30.0

    def start_calibration(self):
        self.calibrator.reset()
        self.is_calibrating = True
        self.calib_step = 0
        self.calib_phase = "WAIT"
        self.phase_start_time = time.perf_counter()

    def run(self):
        print("\n=======================================================")
        print(" SISTEMA DE EYE TRACKING DE ESCRITORIO (ALTA ROBUSTEZ)")
        print(f" Pantalla detectada: {SCREEN_WIDTH}x{SCREEN_HEIGHT}")
        print(" Atajos:")
        print("  [C] : Iniciar Calibración Interactiva (5 Puntos)")
        print("  [M] : Alternar Control del Cursor de Windows")
        print("  [R] : Reiniciar Filtros y Calibración")
        print("  [Q / ESC] : Salir")
        print("=======================================================\n")

        calib_window_name = "Calibracion Gaze (Mira al Circulo)"

        while self.cap.isOpened():
            ret, frame = self.cap.read()
            if not ret:
                print("Error de lectura de cámara.")
                break

            now = time.perf_counter()
            dt = now - self.prev_time
            self.prev_time = now
            if dt > 0:
                self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)

            # Espejo horizontal para vista natural
            frame = cv2.flip(frame, 1)
            h, w, _ = frame.shape

            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            landmarks = self.face_mesh_wrapper.process(rgb_frame)

            filtered_x, filtered_y = SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2

            if landmarks is not None:
                # 1. Postura 3D de cabeza vía solvePnP
                pose = self.head_pose.estimate(landmarks, w, h)

                if pose is not None:
                    self.head_pose.draw_axes(frame, pose["rvec"], pose["tvec"], length=80.0)

                    # 2. Vector de mirada compuesto (VOR)
                    feat = self.gaze_tracker.extract_composite_features(landmarks, pose)

                    # 3. Predicción espacial
                    raw_screen_x, raw_screen_y = self.calibrator.predict(feat)

                    # 4. Filtro dinámico 1€ Filter
                    filtered_x, filtered_y = self.cursor_filter.filter(raw_screen_x, raw_screen_y, now)
                    filtered_x = int(round(filtered_x))
                    filtered_y = int(round(filtered_y))

                    # Control opcional de mouse OS
                    if self.control_mouse and hasattr(ctypes, 'windll') and not self.is_calibrating:
                        ctypes.windll.user32.SetCursorPos(filtered_x, filtered_y)

                    # 5. Visualización debug
                    r_iris_px = (int(feat["r_iris"].x * w), int(feat["r_iris"].y * h))
                    l_iris_px = (int(feat["l_iris"].x * w), int(feat["l_iris"].y * h))
                    cv2.circle(frame, r_iris_px, 3, (0, 255, 255), -1)
                    cv2.circle(frame, l_iris_px, 3, (0, 255, 255), -1)

                    scale_vec = 150.0
                    cv2.line(frame, r_iris_px,
                             (int(r_iris_px[0] + feat["raw_combined_x"] * scale_vec),
                              int(r_iris_px[1] + feat["raw_combined_y"] * scale_vec)),
                             (0, 0, 255), 2)
                    cv2.line(frame, l_iris_px,
                             (int(l_iris_px[0] + feat["raw_combined_x"] * scale_vec),
                              int(l_iris_px[1] + feat["raw_combined_y"] * scale_vec)),
                             (0, 0, 255), 2)

                    # Calibración interactiva
                    if self.is_calibrating:
                        target_norm = self.calibrator.calib_targets[self.calib_step]
                        target_screen_x = int(target_norm[0] * SCREEN_WIDTH)
                        target_screen_y = int(target_norm[1] * SCREEN_HEIGHT)

                        elapsed = now - self.phase_start_time

                        if self.calib_phase == "WAIT":
                            if elapsed >= 1.5:
                                self.calib_phase = "RECORD"
                                self.phase_start_time = time.perf_counter()
                        elif self.calib_phase == "RECORD":
                            self.calibrator.add_sample(feat, (target_screen_x, target_screen_y))
                            if elapsed >= 1.0:
                                self.calib_step += 1
                                if self.calib_step >= len(self.calibrator.calib_targets):
                                    self.calibrator.train()
                                    self.is_calibrating = False
                                    cv2.destroyWindow(calib_window_name)
                                    print("[Calibración] Completada con éxito.")
                                else:
                                    self.calib_phase = "WAIT"
                                    self.phase_start_time = time.perf_counter()

                    # Telemetría en pantalla
                    info_text = f"FPS: {self.fps:.1f} | Head: Y:{pose['yaw_deg']:+.1f} P:{pose['pitch_deg']:+.1f}"
                    cv2.putText(frame, info_text, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

                    gaze_text = f"Gaze Screen: X={filtered_x:4d}, Y={filtered_y:4d}"
                    cv2.putText(frame, gaze_text, (10, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

                    calib_status = "Calibrado: SI" if self.calibrator.is_calibrated else "Calibrado: NO (Pulsa C)"
                    cv2.putText(frame, calib_status, (10, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                                (0, 255, 0) if self.calibrator.is_calibrated else (0, 165, 255), 2)

                    mouse_status = "Mouse OS: ACTIVO (M)" if self.control_mouse else "Mouse OS: INACTIVO (M)"
                    cv2.putText(frame, mouse_status, (10, 115), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                                (255, 200, 0) if self.control_mouse else (180, 180, 180), 2)

            # Ventana interactiva de calibración
            if self.is_calibrating:
                calib_canvas = np.zeros((SCREEN_HEIGHT, SCREEN_WIDTH, 3), dtype=np.uint8)
                target_norm = self.calibrator.calib_targets[self.calib_step]
                tx = int(target_norm[0] * SCREEN_WIDTH)
                ty = int(target_norm[1] * SCREEN_HEIGHT)

                if self.calib_phase == "WAIT":
                    cv2.circle(calib_canvas, (tx, ty), 28, (0, 215, 255), 3)
                    cv2.circle(calib_canvas, (tx, ty), 10, (0, 215, 255), -1)
                    cv2.putText(calib_canvas, f"Ajusta tu mirada al punto ({self.calib_step + 1}/5)",
                                (SCREEN_WIDTH // 2 - 250, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)
                else:
                    cv2.circle(calib_canvas, (tx, ty), 32, (0, 255, 0), -1)
                    cv2.circle(calib_canvas, (tx, ty), 40, (255, 255, 255), 2)
                    cv2.putText(calib_canvas, "Capturando datos de mirada...",
                                (SCREEN_WIDTH // 2 - 220, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)

                cv2.namedWindow(calib_window_name, cv2.WND_PROP_FULLSCREEN)
                cv2.setWindowProperty(calib_window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
                cv2.imshow(calib_window_name, calib_canvas)

            cv2.imshow("Monitor de Eye-Tracking (Head Pose + Iris VOR)", frame)

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q') or key == 27:
                break
            elif key == ord('c'):
                self.start_calibration()
            elif key == ord('m'):
                self.control_mouse = not self.control_mouse
                print(f"Control de cursor del sistema: {'ACTIVADO' if self.control_mouse else 'DESACTIVADO'}")
            elif key == ord('r'):
                self.calibrator.reset()
                self.cursor_filter.reset()
                print("Filtros y calibración reiniciados.")

        self.cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    app = GazeApplication(camera_index=0, control_mouse=False)
    app.run()
