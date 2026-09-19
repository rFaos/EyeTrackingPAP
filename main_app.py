"""
main_app.py  —  Eye-Tracking Suite — Aplicación Principal
==========================================================
UI Tkinter completa con:
  • Selector de cámara (detección automática + cambio en caliente)
  • Estado en tiempo real del motor (FPS, jitter, cara, cámara)
  • Calibración interactiva 5/9 puntos con control de calidad (RMS)
  • Guardar / cargar calibración
  • Sesión de grabación con overlay transparente *click-through*
    (deja usar el navegador normalmente mientras registra)
  • Heatmaps (densidad / dwell time / scanpath)
  • Informe de sesión: orden de fijación y zonas de atención
  • Servidor TCP para OpenSesame
  • Ventana de debug de cámara

Ejecutar:
    python main_app.py                 # cámara automática, 1280×720
    python main_app.py --camera 1      # forzar cámara 1
    python main_app.py --quality fast  # 640×480
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import List, Optional

import cv2

from et_suite.gaze_tracker import GazeTracker, GazeCalibrator, SCREEN_W, SCREEN_H
from et_suite.calibration_engine import CalibrationEngine
from et_suite.data_logger import DataLogger
from et_suite.experiment_server import ExperimentServer
from et_suite.heatmap_generator import HeatmapGenerator
from et_suite.report import session_report
from et_suite.camera import CameraInfo

SESSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
CAL_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "calibracion.json")
TEST_PAGE   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "pagina_prueba.html")

# Modos de captura ofrecidos en la UI → (ancho, alto, negociar_por_fps)
#
# En ambos casos se mide el rendimiento real de la cámara y se baja de
# resolución automáticamente si no alcanza los FPS mínimos.
QUALITY = {
    "Auto (recomendado)":  (640, 480, True),
    "Alta res. 1280×720":  (1280, 720, True),
}


# ════════════════════════════════════════════════════════════════════════════
#  Overlay transparente always-on-top *click-through*
# ════════════════════════════════════════════════════════════════════════════

class GazeOverlay:
    """
    Ventana transparente con un crosshair de contorno que sigue la mirada.

    Importante: en Windows se marca la ventana como *click-through*
    (``WS_EX_TRANSPARENT``).  Sin esto, un overlay a pantalla completa se
    come todos los clics del ratón y hace imposible usar el navegador
    mientras se graba la sesión — que es justo lo que hay que hacer.
    """

    RADIUS = 24
    INNER  = 5
    COLOR  = "#00FFAA"
    COLOR_UNCAL = "#FFB830"
    COLOR_NOCARA = "#FF4455"
    ALPHA  = 0.85

    def __init__(self, tracker: GazeTracker) -> None:
        self.tracker  = tracker
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._root:   Optional[tk.Tk] = None
        self._canvas: Optional[tk.Canvas] = None
        self._ready   = threading.Event()
        self._hwnd:   Optional[int] = None

    # ── Ciclo de vida ────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._ready.clear()
        self._thread = threading.Thread(target=self._build,
                                        daemon=True, name="GazeOverlay")
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        self._running = False
        root = self._root
        if root:
            try:
                root.after(0, root.quit)
            except Exception:
                pass

    # ── Click-through (Windows) ──────────────────────────────────────────

    def _enable_click_through(self, root: tk.Tk) -> None:
        if sys.platform != "win32":
            return
        try:
            hwnd = root.winfo_id()
            parent = ctypes.windll.user32.GetParent(hwnd)
            if parent:
                hwnd = parent
            self._hwnd = hwnd

            GWL_EXSTYLE       = -20
            WS_EX_LAYERED     = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_TOOLWINDOW  = 0x00000080
            WS_EX_NOACTIVATE  = 0x08000000

            user32 = ctypes.windll.user32
            get_long  = getattr(user32, "GetWindowLongPtrW",
                                user32.GetWindowLongW)
            set_long  = getattr(user32, "SetWindowLongPtrW",
                                user32.SetWindowLongW)
            ex = get_long(hwnd, GWL_EXSTYLE)
            ex |= (WS_EX_LAYERED | WS_EX_TRANSPARENT |
                   WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
            set_long(hwnd, GWL_EXSTYLE, ex)
        except Exception as e:
            print(f"[Overlay] No se pudo activar click-through: {e}")

    # ── Construcción ─────────────────────────────────────────────────────

    def _build(self) -> None:
        r = tk.Tk()
        self._root = r
        r.overrideredirect(True)
        try:
            r.attributes("-topmost", True)
        except Exception:
            pass

        if sys.platform == "win32":
            try:
                r.attributes("-transparentcolor", "black")
            except Exception:
                pass
        elif sys.platform == "darwin":
            try:
                r.attributes("-transparent", True)
            except Exception:
                pass

        try:
            r.attributes("-alpha", self.ALPHA)
        except Exception:
            pass

        r.configure(bg="black")
        r.geometry(f"{SCREEN_W}x{SCREEN_H}+0+0")
        c = tk.Canvas(r, width=SCREEN_W, height=SCREEN_H,
                      bg="black", highlightthickness=0)
        c.pack()
        self._canvas = c

        r.update_idletasks()
        self._enable_click_through(r)

        self._ready.set()
        self._tick()
        try:
            r.mainloop()
        except Exception:
            pass

    def _tick(self) -> None:
        if not self._running:
            if self._root:
                try:
                    self._root.quit()
                except Exception:
                    pass
            return
        s = self.tracker.get_latest()
        if s:
            self._draw(int(s.gaze_x), int(s.gaze_y),
                       s.face_detected, s.calibrated)
        try:
            self._root.after(16, self._tick)      # type: ignore[union-attr]
        except Exception:
            pass

    def _draw(self, x: int, y: int, detected: bool, calibrated: bool) -> None:
        c = self._canvas
        if c is None:
            return
        try:
            c.delete("g")
            if not detected:
                # Sin cara: X roja
                c.create_line(x - 14, y, x + 14, y, fill=self.COLOR_NOCARA,
                              width=3, tags="g")
                c.create_line(x, y - 14, x, y + 14, fill=self.COLOR_NOCARA,
                              width=3, tags="g")
                return

            color = self.COLOR if calibrated else self.COLOR_UNCAL
            R = self.RADIUS
            c.create_oval(x - R, y - R, x + R, y + R,
                          outline=color, width=3, fill="", tags="g")
            half = R // 2
            c.create_line(x - half, y, x + half, y, fill=color, width=1,
                          tags="g")
            c.create_line(x, y - half, x, y + half, fill=color, width=1,
                          tags="g")
            I = self.INNER
            c.create_oval(x - I, y - I, x + I, y + I,
                          fill=color, outline="", tags="g")
            if not calibrated:
                c.create_text(x + R + 46, y, text="SIN CALIBRAR",
                              fill=self.COLOR_UNCAL,
                              font=("Segoe UI", 11, "bold"), tags="g")
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
#  Ventana de Debug de Cámara
# ════════════════════════════════════════════════════════════════════════════

class DebugWindow(threading.Thread):
    def __init__(self, tracker: GazeTracker) -> None:
        super().__init__(daemon=True, name="DebugWindow")
        self.tracker  = tracker
        self._running = True

    def run(self) -> None:
        WIN = "Eye Tracking Debug  [D = cerrar]"
        while self._running:
            frame = self.tracker.get_debug_frame()
            if frame is not None:
                cv2.imshow(WIN, frame)
            key = cv2.waitKey(16) & 0xFF
            if key in (ord("d"), ord("q"), 27):
                break
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
        self._running = False

    def stop(self) -> None:
        self._running = False


# ════════════════════════════════════════════════════════════════════════════
#  Paleta
# ════════════════════════════════════════════════════════════════════════════

BG     = "#0F0F1A"
BG2    = "#161628"
BG3    = "#1E1E35"
ACCENT = "#00FFAA"
FG     = "#E0E0FF"
FG2    = "#9090BB"
RED    = "#FF4455"
AMBER  = "#FFB830"
GREEN  = "#22DD88"


def _btn(parent, text, color, cmd, state="normal", pady=8, width=None):
    kw = dict(text=text, bg=color, fg="white",
              font=("Segoe UI", 10, "bold"),
              relief="flat", cursor="hand2",
              activebackground=color, activeforeground="white",
              state=state, command=cmd, pady=pady)
    if width:
        kw["width"] = width
    b = tk.Button(parent, **kw)
    b.bind("<Enter>", lambda _: b.config(bg=_lighten(color)))
    b.bind("<Leave>", lambda _: b.config(bg=color))
    return b


def _lighten(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = [min(255, int(h[i:i + 2], 16) + 30) for i in (0, 2, 4)]
    return f"#{r:02X}{g:02X}{b:02X}"


# ════════════════════════════════════════════════════════════════════════════
#  Aplicación
# ════════════════════════════════════════════════════════════════════════════

class App:
    SERVER_PORT = 4242

    def __init__(self, camera_index: Optional[int] = 0,
                 quality: str = "Auto (recomendado)") -> None:
        os.makedirs(SESSION_DIR, exist_ok=True)
        self._quality = quality
        qw, qh, qtune = QUALITY.get(quality, (640, 480, True))

        # ── Componentes ──────────────────────────────────────────────────
        self.calibrator = GazeCalibrator(n_points=9, method="geometric")
        self.tracker    = GazeTracker(camera_index=camera_index,
                                      calibrator=self.calibrator,
                                      cam_width=qw, cam_height=qh,
                                      auto_tune=qtune)
        self.logger     = DataLogger(self.tracker, output_dir=SESSION_DIR)
        self.server     = ExperimentServer(self.tracker, self.logger,
                                           port=self.SERVER_PORT)
        self.overlay    = GazeOverlay(self.tracker)
        self.heatmap_gen = HeatmapGenerator(SCREEN_W, SCREEN_H)

        self._debug_win:     Optional[DebugWindow] = None
        self._session_active = False
        self._session_start: Optional[float] = None
        self._last_csv:      Optional[str] = None
        self._server_running = False
        self._timer_id:      Optional[str] = None
        self._probing        = False
        self._cam_infos:     List[CameraInfo] = []

        # ── UI ───────────────────────────────────────────────────────────
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── Motor ────────────────────────────────────────────────────────
        self.tracker.start()

        # Intentar recuperar la última calibración guardada
        if os.path.exists(CAL_FILE):
            if self.calibrator.load(CAL_FILE):
                rms = self.calibrator.rms_error_px
                self._lbl_cal.config(
                    text=f"Calibración: ✓ cargada del disco  "
                         f"(error {rms:.0f} px)" if rms else
                         "Calibración: ✓ cargada del disco",
                    fg=GREEN)

        self._update_status()

    # ── Construcción UI ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.root = tk.Tk()
        self.root.title("Eye-Tracking Suite v2.1")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        W, H = 560, 830
        sx = max(0, (SCREEN_W - W) // 2)
        sy = max(0, (SCREEN_H - H) // 2)
        self.root.geometry(f"{W}x{H}+{sx}+{sy}")

        hdr = tk.Frame(self.root, bg=BG2, pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="👁  Eye-Tracking Suite",
                 font=("Segoe UI", 20, "bold"), fg=ACCENT, bg=BG2).pack()
        tk.Label(hdr, text="Análisis de mirada en tiempo real  ·  v2.1",
                 font=("Segoe UI", 9), fg=FG2, bg=BG2).pack()

        # ── Estado ───────────────────────────────────────────────────────
        st = tk.Frame(self.root, bg=BG, pady=4)
        st.pack(fill="x", padx=18)

        self._lbl_motor = tk.Label(st, text="Motor: iniciando…",
                                   font=("Consolas", 9), fg=ACCENT, bg=BG,
                                   anchor="w")
        self._lbl_motor.pack(fill="x")
        self._lbl_cam = tk.Label(st, text="Cámara: buscando…",
                                 font=("Consolas", 9), fg=AMBER, bg=BG,
                                 anchor="w")
        self._lbl_cam.pack(fill="x")
        self._lbl_cal = tk.Label(st, text="Calibración: NO",
                                 font=("Consolas", 9), fg=AMBER, bg=BG,
                                 anchor="w")
        self._lbl_cal.pack(fill="x")
        self._lbl_session = tk.Label(st, text="Sesión: inactiva",
                                     font=("Consolas", 9), fg=FG2, bg=BG,
                                     anchor="w")
        self._lbl_session.pack(fill="x")
        self._lbl_server = tk.Label(
            st, text=f"Servidor TCP: apagado  ·  puerto {self.SERVER_PORT}",
            font=("Consolas", 9), fg=FG2, bg=BG, anchor="w")
        self._lbl_server.pack(fill="x")

        tk.Frame(self.root, bg="#2A2A44", height=1).pack(fill="x", padx=14, pady=4)

        # ── 1 · Cámara ───────────────────────────────────────────────────
        g1 = self._group(self.root, " 1 · Cámara ")
        g1.pack(fill="x", padx=16, pady=3)

        row = tk.Frame(g1, bg=BG3)
        row.pack(fill="x", padx=10, pady=4)
        tk.Label(row, text="Dispositivo:", fg=FG, bg=BG3,
                 font=("Segoe UI", 10)).pack(side="left")
        self._var_cam = tk.StringVar(value="(automática)")
        self._cbo_cam = ttk.Combobox(row, textvariable=self._var_cam,
                                     width=30, state="readonly",
                                     font=("Segoe UI", 9))
        self._cbo_cam["values"] = ["(automática)"]
        self._cbo_cam.pack(side="left", padx=6)
        self._cbo_cam.bind("<<ComboboxSelected>>", self._on_cam_selected)

        row2 = tk.Frame(g1, bg=BG3)
        row2.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(row2, text="Calidad:", fg=FG, bg=BG3,
                 font=("Segoe UI", 10)).pack(side="left")
        self._var_q = tk.StringVar(value=self._quality)
        for lbl in QUALITY:
            tk.Radiobutton(row2, text=lbl, variable=self._var_q, value=lbl,
                           fg=FG, bg=BG3, selectcolor="#333366",
                           activebackground=BG3, font=("Segoe UI", 9),
                           command=self._on_quality).pack(side="left", padx=4)

        row3 = tk.Frame(g1, bg=BG3)
        row3.pack(fill="x", padx=10, pady=(0, 6))
        self._btn_scan = _btn(row3, "🔍 Detectar cámaras", "#2C3E50",
                              self._do_scan, width=22, pady=5)
        self._btn_scan.pack(side="left", padx=(0, 6))
        _btn(row3, "🎥 Debug", "#2C3E50", self._toggle_debug,
             width=14, pady=5).pack(side="left")

        # ── 2 · Calibración ──────────────────────────────────────────────
        g2 = self._group(self.root, " 2 · Calibración ")
        g2.pack(fill="x", padx=16, pady=3)

        pts_row = tk.Frame(g2, bg=BG3)
        pts_row.pack(fill="x", padx=10, pady=3)
        tk.Label(pts_row, text="Puntos:", fg=FG, bg=BG3,
                 font=("Segoe UI", 10)).pack(side="left")
        self._var_pts = tk.IntVar(value=9)
        for lbl, val in [("9 (Recomendado)", 9), ("5 (Rápido)", 5)]:
            tk.Radiobutton(pts_row, text=lbl, variable=self._var_pts, value=val,
                           fg=FG, bg=BG3, selectcolor="#333366",
                           activebackground=BG3, font=("Segoe UI", 9)
                           ).pack(side="left", padx=5)

        meth_row = tk.Frame(g2, bg=BG3)
        meth_row.pack(fill="x", padx=10, pady=(0, 3))
        tk.Label(meth_row, text="Método:", fg=FG, bg=BG3,
                 font=("Segoe UI", 10)).pack(side="left")
        # IMPORTANTE: el valor por defecto tiene que ser "geometric".
        # Antes esta variable valía "polynomial" y _do_calibrate() hacía
        # ``self.calibrator.method = method``, así que CADA calibración
        # hecha desde la interfaz forzaba el modelo polinómico y el modelo
        # geométrico (el invariante a la cabeza) no se usaba nunca.
        self._var_method = tk.StringVar(value="geometric")
        for lbl, val in [("Geométrico (recomendado)", "geometric"),
                         ("Polinomial", "polynomial"),
                         ("Homografía", "homography")]:
            tk.Radiobutton(meth_row, text=lbl, variable=self._var_method,
                           value=val, fg=FG, bg=BG3, selectcolor="#333366",
                           activebackground=BG3, font=("Segoe UI", 9)
                           ).pack(side="left", padx=5)

        cal_btns = tk.Frame(g2, bg=BG3)
        cal_btns.pack(fill="x", padx=10, pady=6)
        self._btn_cal = _btn(cal_btns, "🎯  Calibrar Ahora", "#0D6E6E",
                             self._do_calibrate, width=20, pady=7)
        self._btn_cal.pack(side="left", padx=(0, 6))
        _btn(cal_btns, "💾 Guardar", "#2C3E50", self._save_cal,
             width=10, pady=7).pack(side="left", padx=(0, 6))
        _btn(cal_btns, "📥 Cargar", "#2C3E50", self._load_cal,
             width=10, pady=7).pack(side="left")

        # ── 3 · Sesión ───────────────────────────────────────────────────
        g3 = self._group(self.root, " 3 · Sesión de Grabación ")
        g3.pack(fill="x", padx=16, pady=3)

        name_row = tk.Frame(g3, bg=BG3)
        name_row.pack(fill="x", padx=10, pady=4)
        tk.Label(name_row, text="Participante:", fg=FG, bg=BG3,
                 font=("Segoe UI", 10)).pack(side="left")
        self._var_name = tk.StringVar(value="")
        tk.Entry(name_row, textvariable=self._var_name, width=20,
                 bg="#22223A", fg=FG, insertbackground=FG,
                 relief="flat", font=("Consolas", 10)).pack(side="left", padx=8)

        self._var_nocal = tk.BooleanVar(value=False)
        tk.Checkbutton(g3, text="Permitir grabar sin calibrar (sólo demo)",
                       variable=self._var_nocal, fg=FG2, bg=BG3,
                       selectcolor="#333366", activebackground=BG3,
                       font=("Segoe UI", 9)).pack(anchor="w", padx=12)

        btns3 = tk.Frame(g3, bg=BG3)
        btns3.pack(fill="x", padx=10, pady=6)
        self._btn_start = _btn(btns3, "▶  Iniciar", "#145A32",
                               self._do_start, width=18, pady=7)
        self._btn_start.pack(side="left", padx=(0, 6))
        self._btn_stop = _btn(btns3, "⬛  Detener", "#7B241C",
                              self._do_stop, state="disabled", width=18, pady=7)
        self._btn_stop.pack(side="left")

        # ── 4 · Análisis ─────────────────────────────────────────────────
        g4 = self._group(self.root, " 4 · Análisis ")
        g4.pack(fill="x", padx=16, pady=3)

        hm_row = tk.Frame(g4, bg=BG3)
        hm_row.pack(fill="x", padx=10, pady=4)
        tk.Label(hm_row, text="Heatmap:", fg=FG, bg=BG3,
                 font=("Segoe UI", 10)).pack(side="left")
        self._var_hm = tk.StringVar(value="all")
        for lbl, val in [("Todos", "all"), ("Densidad", "density"),
                         ("Dwell", "dwell"), ("Scanpath", "scanpath")]:
            tk.Radiobutton(hm_row, text=lbl, variable=self._var_hm, value=val,
                           fg=FG, bg=BG3, selectcolor="#333366",
                           activebackground=BG3, font=("Segoe UI", 9)
                           ).pack(side="left", padx=3)

        ana_btns = tk.Frame(g4, bg=BG3)
        ana_btns.pack(fill="x", padx=10, pady=(0, 7))
        _btn(ana_btns, "🗺  Heatmaps", "#4A235A", self._do_heatmap,
             width=15, pady=7).pack(side="left", padx=(0, 6))
        _btn(ana_btns, "📋  Informe: ¿qué miró primero?", "#1A3A5C",
             self._do_report, width=30, pady=7).pack(side="left")

        # ── 5 · Extras ───────────────────────────────────────────────────
        g5 = self._group(self.root, " 5 · Extras ")
        g5.pack(fill="x", padx=16, pady=3)

        ex = tk.Frame(g5, bg=BG3)
        ex.pack(fill="x", padx=10, pady=7)
        _btn(ex, "🌐 Página de prueba", "#2C3E50", self._open_test_page,
             width=19, pady=6).pack(side="left", padx=(0, 5))
        _btn(ex, "🔌 Servidor TCP", "#1A3A5C", self._toggle_server,
             width=16, pady=6).pack(side="left", padx=(0, 5))
        _btn(ex, "📂 Sesiones", "#2C3E50", self._open_folder,
             width=12, pady=6).pack(side="left")

    def _group(self, parent, title: str) -> tk.LabelFrame:
        return tk.LabelFrame(parent, text=title,
                             font=("Segoe UI", 9, "bold"),
                             fg="#AAAADD", bg=BG3,
                             bd=1, relief="groove")

    # ── Cámara ───────────────────────────────────────────────────────────

    def _do_scan(self) -> None:
        if self._probing:
            return
        self._probing = True
        self._btn_scan.config(state="disabled", text="🔍 Buscando…")
        qw, qh, _ = QUALITY.get(self._var_q.get(), (640, 480, True))

        def work():
            infos = GazeTracker.list_cameras(max_index=6, width=qw, height=qh)
            self.root.after(0, lambda: self._scan_done(infos))

        threading.Thread(target=work, daemon=True, name="CamScan").start()

    def _scan_done(self, infos: List[CameraInfo]) -> None:
        self._cam_infos = infos
        self._probing = False
        self._btn_scan.config(state="normal", text="🔍 Detectar cámaras")
        values = ["(automática)"] + [c.label for c in infos]
        self._cbo_cam["values"] = values
        if not infos:
            messagebox.showwarning(
                "Sin cámaras",
                "No se encontró ninguna cámara que entregue imagen.\n\n"
                "Revisa que:\n"
                "  • No esté siendo usada por Zoom / Teams / OBS\n"
                "  • El permiso de cámara esté activo en Windows\n"
                "  • El cable USB esté bien conectado")
        else:
            messagebox.showinfo("Cámaras detectadas",
                                "Se encontraron:\n\n" +
                                "\n".join(f"  • {c.label}" for c in infos))

    def _on_cam_selected(self, _evt=None) -> None:
        label = self._var_cam.get()
        if label == "(automática)":
            self.tracker.request_camera(None)
            return
        for c in self._cam_infos:
            if c.label == label:
                self.tracker.request_camera(c.index)
                return

    def _on_quality(self) -> None:
        q = self._var_q.get()
        if q == self._quality:
            return
        self._quality = q
        qw, qh, qtune = QUALITY.get(q, (640, 480, True))
        self.tracker.cam_width  = qw
        self.tracker.cam_height = qh
        self.tracker.auto_tune  = qtune
        self.tracker.request_camera(self.tracker.camera_index)
        messagebox.showinfo("Calidad cambiada",
                            "Se reinició la cámara.\n"
                            "Vuelve a calibrar: la calibración depende de la "
                            "resolución de la cámara.")

    # ── Calibración ──────────────────────────────────────────────────────

    def _do_calibrate(self) -> None:
        if self._session_active:
            messagebox.showwarning("Sesión activa",
                                   "Detén la sesión antes de calibrar.")
            return
        if not self.tracker.running or self.tracker.camera_info is None:
            messagebox.showerror(
                "Sin cámara",
                "No hay cámara activa. Pulsa 'Detectar cámaras' o revisa que "
                "ninguna otra aplicación la esté usando.")
            return

        n      = self._var_pts.get()
        method = self._var_method.get()
        self.calibrator.targets = (self.calibrator.TARGETS_9 if n == 9
                                   else self.calibrator.TARGETS_5)
        self.calibrator.method = method

        self._btn_cal_state("disabled")
        self._lbl_cal.config(text="Calibración: en curso…", fg=AMBER)

        def run_cal():
            try:
                eng = CalibrationEngine(tracker=self.tracker,
                                        calibrator=self.calibrator,
                                        on_done=self._on_cal_done,
                                        n_points=n)
                eng.run()
            except Exception as e:
                self.root.after(0, lambda: self._on_cal_done(False))
                self.root.after(0, lambda: messagebox.showerror(
                    "Error en calibración", str(e)))

        threading.Thread(target=run_cal, daemon=True, name="CalibThread").start()

    def _btn_cal_state(self, state: str) -> None:
        try:
            self._btn_cal.config(state=state,
                                 text="🎯  Calibrando…" if state == "disabled"
                                 else "🎯  Calibrar Ahora")
        except Exception:
            pass

    def _on_cal_done(self, success: bool) -> None:
        rms = self.calibrator.rms_error_px
        usado = getattr(self.calibrator, "method", "?")
        etiqueta = {"geometric": "geométrico",
                    "polynomial": "polinómico",
                    "homography": "homografía"}.get(usado, usado)

        if success and rms is not None:
            text = (f"Calibración: ✓ OK  ·  {etiqueta}  ·  "
                    f"error {rms:.0f} px")
            color = GREEN if rms < 250 else AMBER
        elif success:
            text, color = f"Calibración: ✓ OK  ·  {etiqueta}", GREEN
        else:
            text, color = "Calibración: FALLIDA  (reintenta)", RED

        # Si se pidió el modelo geométrico pero se cayó a un respaldo, hay
        # que decirlo: el usuario perdería la invariancia a la cabeza sin
        # enterarse.
        aviso = None
        if success and usado != self._var_method.get():
            aviso = (f"Se pidió el modelo "
                     f"{self._var_method.get()} pero no había datos 3D "
                     f"suficientes y se usó el {etiqueta}.\n\n"
                     f"Eso significa que la mirada se moverá si mueves la "
                     f"cabeza. Reintenta la calibración moviéndote un poco "
                     f"durante los puntos.")

        def apply():
            self._btn_cal_state("normal")
            self._lbl_cal.config(text=text, fg=color)
            if success:
                self.calibrator.save(CAL_FILE)
            if aviso:
                messagebox.showwarning("Calibración degradada", aviso)
        self.root.after(0, apply)

    def _save_cal(self) -> None:
        if not self.calibrator.is_calibrated:
            messagebox.showwarning("Sin calibración",
                                   "Todavía no hay una calibración que guardar.")
            return
        path = filedialog.asksaveasfilename(
            title="Guardar calibración", defaultextension=".json",
            initialfile="calibracion.json",
            filetypes=[("JSON", "*.json")])
        if path and self.calibrator.save(path):
            messagebox.showinfo("Guardado", f"Calibración guardada en:\n{path}")

    def _load_cal(self) -> None:
        path = filedialog.askopenfilename(
            title="Cargar calibración",
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        if self.calibrator.load(path):
            rms = self.calibrator.rms_error_px
            self._lbl_cal.config(
                text=f"Calibración: ✓ cargada  ·  error {rms:.0f} px"
                if rms else "Calibración: ✓ cargada",
                fg=GREEN)
        else:
            messagebox.showerror(
                "No se pudo cargar",
                "El archivo no es válido o se hizo con otra resolución "
                "de pantalla.")

    # ── Sesión ───────────────────────────────────────────────────────────

    def _do_start(self) -> None:
        if self._session_active:
            return
        if self.tracker.camera_info is None:
            messagebox.showerror(
                "Sin cámara",
                "No hay cámara activa: la grabación no tendría datos.\n"
                "Pulsa 'Detectar cámaras' y elige una.")
            return
        if not self.calibrator.is_calibrated and not self._var_nocal.get():
            messagebox.showwarning(
                "Sin calibrar",
                "Calibra el sistema antes de iniciar la sesión.\n\n"
                "Si sólo quieres probar que graba, marca la casilla "
                "'Permitir grabar sin calibrar (sólo demo)'.")
            return

        name = self._var_name.get().strip().replace(" ", "_") or "participante"
        self._session_active = True
        self._session_start  = time.perf_counter()
        self.overlay.start()
        self.logger.start(session_name=name)
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")
        self._lbl_session.config(
            text="Sesión ACTIVA ⏺  ·  ya puedes usar el navegador",
            fg=RED)

    def _do_stop(self) -> None:
        if not self._session_active:
            return
        self._session_active = False
        self.overlay.stop()
        csv_path, stats = self.logger.stop()
        self._last_csv = csv_path
        self._btn_start.config(state="normal")
        self._btn_stop.config(state="disabled")
        self._lbl_session.config(
            text=(f"Sesión guardada ✓  "
                  f"{stats.get('n_samples', 0)} muestras  "
                  f"{stats.get('n_fixations', 0)} fijaciones"),
            fg=GREEN)

        if stats.get("n_samples", 0) == 0:
            messagebox.showwarning(
                "Sesión vacía",
                "No se grabó ninguna muestra.\n\n"
                "Causas típicas:\n"
                "  • La cámara no estaba entregando imagen\n"
                "  • El rostro no se detectó en ningún frame\n"
                "  • La sesión duró menos de un segundo")
            return

        info = (
            f"Sesión guardada ✓\n\n"
            f"Duración:        {stats.get('duration_ms', 0)/1000:.1f} s\n"
            f"Muestras:        {stats.get('n_samples', 0)}\n"
            f"Frec. muestreo:  {stats.get('mean_sample_hz', 0):.1f} Hz\n"
            f"Jitter medio:    {stats.get('mean_jitter_ms', 0):.2f} ms\n"
            f"Fijaciones:      {stats.get('n_fixations', 0)}\n"
            f"Dur. media fix.: {stats.get('mean_fixation_ms', 0):.0f} ms\n"
            f"TTFF:            {stats.get('ttff_ms') or 'N/A'} ms\n"
            f"Cara detectada:  {stats.get('face_detected_pct', 0):.1f} %\n\n"
            f"CSV: {csv_path}"
        )
        messagebox.showinfo("Sesión completada", info)

    # ── Análisis ─────────────────────────────────────────────────────────

    def _pick_csv(self) -> Optional[str]:
        """
        Devuelve el CSV a analizar evitando abrir un diálogo si es posible.

        Orden de preferencia:
          1. La sesión que se acaba de grabar en esta ejecución.
          2. La sesión más reciente de ``sessions/``.
          3. Sólo si no hay ninguna, se pregunta al usuario.

        (Antes se abría el diálogo siempre que no hubiera una sesión recién
        grabada, lo que hacía parecer que el botón "pedía un archivo".)
        """
        if self._last_csv and os.path.exists(self._last_csv):
            return self._last_csv

        try:
            csvs = [
                os.path.join(SESSION_DIR, f)
                for f in os.listdir(SESSION_DIR)
                if f.lower().endswith(".csv")
            ]
            if csvs:
                csvs.sort(key=os.path.getmtime, reverse=True)
                self._last_csv = csvs[0]
                return csvs[0]
        except Exception:
            pass

        return filedialog.askopenfilename(
            title="No hay sesiones guardadas — elige un CSV",
            initialdir=SESSION_DIR,
            filetypes=[("CSV", "*.csv")]) or None

    def _do_heatmap(self) -> None:
        csv_path = self._pick_csv()
        if not csv_path:
            return

        bg_path = filedialog.askopenfilename(
            title="Fondo opcional: captura de la página que mirabas "
                  "(Cancelar = fondo blanco)",
            filetypes=[("Imagen", "*.png *.jpg *.jpeg *.bmp")])
        mode = self._var_hm.get()

        def gen():
            try:
                g = HeatmapGenerator(SCREEN_W, SCREEN_H)
                bg = bg_path or None
                if mode == "all":
                    paths = g.all_three(csv_path, background=bg)
                    out = paths["scanpath"]
                    carpeta = os.path.dirname(out)
                elif mode == "density":
                    out = g.fixation_density(csv_path, background=bg,
                                             show_markers=True)
                    carpeta = None
                elif mode == "dwell":
                    out = g.dwell_time(csv_path, background=bg,
                                       show_markers=True)
                    carpeta = None
                else:
                    out = g.scanpath(csv_path, background=bg)
                    carpeta = None

                def done():
                    if carpeta:
                        self._open_path(carpeta)
                        messagebox.showinfo(
                            "Heatmaps generados",
                            "Se crearon 3 imágenes junto a la sesión:\n"
                            f"{os.path.basename(csv_path)}\n\n"
                            "  · _density.png   → DÓNDE miró (frecuencia)\n"
                            "  · _dwell.png     → CUÁNTO tiempo miró\n"
                            "  · _scanpath.png  → EN QUÉ ORDEN (flechas)\n\n"
                            f"Carpeta: {carpeta}")
                    else:
                        self._open_path(out)
                        messagebox.showinfo("Heatmap", f"Guardado:\n{out}")

                self.root.after(0, done)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "Error", f"No se pudo generar el heatmap:\n{e}"))

        threading.Thread(target=gen, daemon=True).start()

    def _do_report(self) -> None:
        csv_path = self._pick_csv()
        if not csv_path:
            return
        try:
            txt = session_report(csv_path, SCREEN_W, SCREEN_H)
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo generar el informe:\n{e}")
            return

        out = f"{os.path.splitext(csv_path)[0]}_informe.txt"
        win = tk.Toplevel(self.root)
        win.title("Informe de sesión — ¿qué miró primero?")
        win.configure(bg=BG)
        win.geometry("880x620")
        frame = tk.Frame(win, bg=BG)
        frame.pack(fill="both", expand=True, padx=10, pady=10)
        sb = tk.Scrollbar(frame)
        sb.pack(side="right", fill="y")
        txtw = tk.Text(frame, bg="#0A0A14", fg=FG, font=("Consolas", 9),
                       yscrollcommand=sb.set, wrap="none", relief="flat")
        txtw.pack(fill="both", expand=True)
        sb.config(command=txtw.yview)
        txtw.insert("1.0", txt)
        txtw.config(state="disabled")
        tk.Label(win, text=f"Guardado en: {out}", fg=FG2, bg=BG,
                 font=("Segoe UI", 9)).pack(pady=(0, 8))

    # ── Extras ───────────────────────────────────────────────────────────

    @staticmethod
    def _open_path(path: str) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(path)                       # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            print(f"No se pudo abrir {path}: {e}")

    def _open_test_page(self) -> None:
        if not os.path.exists(TEST_PAGE):
            messagebox.showwarning("No encontrada",
                                   f"No existe {TEST_PAGE}")
            return
        self._open_path(TEST_PAGE)

    def _toggle_debug(self) -> None:
        if self._debug_win and self._debug_win.is_alive():
            self._debug_win.stop()
        else:
            self._debug_win = DebugWindow(self.tracker)
            self._debug_win.start()

    def _toggle_server(self) -> None:
        if self._server_running:
            self.server.stop()
            self._server_running = False
            self._lbl_server.config(
                text=f"Servidor TCP: apagado  ·  puerto {self.SERVER_PORT}",
                fg=FG2)
        else:
            self.server.start()
            self._server_running = True
            self._lbl_server.config(
                text=f"Servidor TCP: 🟢 {self.server.host}:{self.server.port}",
                fg=GREEN)

    def _open_folder(self) -> None:
        self._open_path(os.path.abspath(SESSION_DIR))

    # ── Ticker de estado ─────────────────────────────────────────────────

    def _update_status(self) -> None:
        t = self.tracker
        s = t.get_latest()
        face = s.face_detected if s else False
        face_icon = "👤" if face else "❌"

        self._lbl_motor.config(
            text=(f"Motor: {t.fps:4.0f} FPS  ·  Jitter {t.jitter_ms:4.1f} ms  "
                  f"·  {face_icon} {'Cara' if face else 'Sin cara'}"))

        if t.camera_info is not None:
            self._lbl_cam.config(
                text=f"Cámara: ✓ {t.camera_info.label}",
                fg=GREEN)
        elif t.last_error:
            self._lbl_cam.config(text=f"Cámara: ✗ {t.last_error}", fg=RED)
        else:
            self._lbl_cam.config(text=f"Cámara: {t.status}", fg=AMBER)

        if self.calibrator.is_calibrated:
            rms = self.calibrator.rms_error_px
            extra = f"  ·  error {rms:.0f} px" if rms else ""
            self._lbl_cal.config(
                text=f"Calibración: ✓ OK  ·  {self.calibrator.n_samples} "
                     f"muestras{extra}", fg=GREEN)

        if self._session_active and self._session_start:
            elapsed = time.perf_counter() - self._session_start
            m, sec  = divmod(int(elapsed), 60)
            self._lbl_session.config(
                text=f"Sesión ACTIVA ⏺  {m:02d}:{sec:02d}  "
                     f"·  {len(self.logger._rows)} muestras",
                fg=RED)

        self._timer_id = self.root.after(400, self._update_status)

    # ── Cierre ───────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        if self._session_active:
            if not messagebox.askyesno("Sesión activa",
                                       "Hay una sesión activa. "
                                       "¿Detener y salir?"):
                return
            self._do_stop()
        if self._timer_id:
            self.root.after_cancel(self._timer_id)
        self.overlay.stop()
        if self._server_running:
            self.server.stop()
        if self._debug_win:
            self._debug_win.stop()
        self.tracker.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ════════════════════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description="Eye-Tracking Suite")
    ap.add_argument("--camera", type=int, default=0,
                    help="Índice de cámara (por defecto 0; se auto-detecta "
                         "si falla). Usa -1 para detección totalmente "
                         "automática.")
    ap.add_argument("--quality", choices=["auto", "high"],
                    default="auto",
                    help="auto = 640×480 con negociación por FPS reales "
                         "(recomendado); high = intenta 1280×720 y baja "
                         "automáticamente si no alcanza los FPS mínimos")
    args = ap.parse_args()

    cam = None if args.camera is None or args.camera < 0 else args.camera
    quality = {"auto": "Auto (recomendado)",
               "high": "Alta res. 1280×720"}[args.quality]

    try:
        app = App(camera_index=cam, quality=quality)
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("Error al iniciar", f"{type(e).__name__}: {e}")
            r.destroy()
        except Exception:
            print(f"Error al iniciar: {e}")
        return 1

    app.run()
    # Salida dura: MediaPipe y algunos drivers de cámara dejan hilos vivos que
    # impiden que el proceso termine aunque la ventana ya esté cerrada.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
