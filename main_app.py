"""
main_app.py  —  Eye-Tracking Suite v3 · Aplicación principal
=============================================================
Aplicación para evaluar un sitio web con participantes: cada persona hace las
tareas definidas en un estudio (``studies/*.json``) mientras la cámara web
estima hacia dónde mira.  Al terminar se obtienen métricas por tarea y por
elemento de la página, informes y mapas de calor, y un análisis del grupo.

Pestañas:
  • Guía        : pasos, permisos y problemas frecuentes.
  • Prueba      : los 5 pasos del participante en orden (código, sitio,
                  calibración, tareas y envío).
  • Resultados  : informe HTML con heatmaps sobre lo que vio cada
                  participante, revisión de tareas y análisis grupal.

Durante la sesión el participante ve sólo el navegador integrado (una
ventana propia del prototipo con el sitio del estudio; no hace falta Chrome
ni Tampermonkey) y un panel pequeño con la tarea (Comenzar / Terminé / No
pude).  La ventana del facilitador se
minimiza.  Funciona en Windows y macOS (Tk siempre en el hilo principal).

Ejecutar:
    python main_app.py                  # cámara automática
    python main_app.py --camera 1       # forzar cámara 1
    python main_app.py --estudios studies/ejemplos   # práctica con la demo
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
import zipfile
from tkinter import messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from et_suite.calibration_engine import CalibrationEngine
from et_suite.camera import CameraInfo
from et_suite.data_logger import DataLogger
from et_suite.display import detect_screen_width_cm
from et_suite.gaze_tracker import (SCREEN_H, SCREEN_W, GazeCalibrator,
                                   GazeTracker)
from et_suite.group_analysis import analyze_study
from et_suite.heatmap_generator import HeatmapGenerator
from et_suite.metrics import analyze_csv
from et_suite.navegador import NavegadorIntegrado
from et_suite.report import session_report
from et_suite.report_html import write_html
from et_suite.screenshots import capture_screen
from et_suite.session_meta import (environment_info, find_sessions, load_meta,
                                   new_meta, now_iso,
                                   save_meta, shots_dir_for,
                                   task_outcome_label, valid_participant_code)
from et_suite.study import Study, StudyError, load_study, scan_studies
from et_suite.validation_engine import ValidationEngine
from et_suite.web_bridge import WebBridge

# En Windows la consola puede ser cp1252: un print() con «→» lanzaba
# UnicodeEncodeError y cortaba la acción en curso.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(HERE, "sessions")
STUDIES_DIR = os.path.join(HERE, "studies")
RESULTS_DIR = os.path.join(HERE, "resultados")
TEST_PAGE = os.path.join(HERE, "pagina_prueba.html")
USERSCRIPT = os.path.join(HERE, "web", "eyetracking_bridge.user.js")

# Modos de captura → (ancho, alto, negociar_por_fps)
QUALITY = {
    "Auto (recomendado)": (640, 480, True),
    "Alta res. 1280×720": (1280, 720, True),
}

# ── tipografías por sistema ─────────────────────────────────────────────────
if sys.platform == "darwin":
    UI, MONO = "Helvetica Neue", "Menlo"
elif sys.platform == "win32":
    UI, MONO = "Segoe UI", "Consolas"
else:
    UI, MONO = "DejaVu Sans", "DejaVu Sans Mono"

# ── paleta (cálida y clara) ─────────────────────────────────────────────────
BG = "#FFFBF0"                 # fondo crema
CARD = "#FFFFFF"               # tarjetas
HEADER = "#FFF1C2"             # franja superior
TAB = "#F8EBC6"                # pestañas sin seleccionar
BORDER = "#EFDFAE"             # bordes suaves
YELLOW = "#F5C242"             # botón principal
SOFT = "#FFF3D1"               # botón secundario
LEAF = "#4E9F5D"               # botón de confirmar
CORAL = "#E8735A"              # botón de cancelar / detener
ACCENT = "#A86B00"             # títulos
FG, FG2 = "#3B3226", "#8A7D68"  # texto principal y secundario
RED, AMBER, GREEN = "#C0392B", "#B7700A", "#2F8F4E"
BG2, BG3 = HEADER, CARD
TINT = {GREEN: "#E4F3E8", AMBER: "#FFF0CF", RED: "#FBE1DD"}

START_DELAY_MS = 1500      # espera tras abrir el sitio antes de contar la tarea
SHOT_DELAY_MS = 2000       # captura de pantalla tras empezar la tarea


# ════════════════════════════════════════════════════════════════════════════
#  Widgets
# ════════════════════════════════════════════════════════════════════════════

def _rgb(hex_color: str) -> Tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _shade(hex_color: str, d: int) -> str:
    r, g, b = [max(0, min(255, c + d)) for c in _rgb(hex_color)]
    return f"#{r:02X}{g:02X}{b:02X}"


def _is_light(hex_color: str) -> bool:
    r, g, b = _rgb(hex_color)
    return 0.299 * r + 0.587 * g + 0.114 * b > 170


class FlatButton(tk.Label):
    """
    Botón plano con color propio.

    ``tk.Button`` ignora ``bg`` y ``fg`` en macOS (Aqua), así que los botones
    quedaban blancos con letras blancas.  Una etiqueta con un clic enlazado
    se ve igual en Windows y en macOS.
    """

    def __init__(self, parent, text: str, color: str,
                 command: Optional[Callable[[], None]] = None,
                 state: str = "normal", width: Optional[int] = None,
                 pady: int = 8, font=None) -> None:
        kw = dict(text=text, bg=color, fg=self._ink(color), padx=14,
                  pady=pady, font=font or (UI, 10, "bold"), cursor="hand2")
        if width:
            kw["width"] = width
        tk.Label.__init__(self, parent, **kw)
        self._color = color
        self._cmd = command
        self._state = state
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda _e: self._paint(hover=True))
        self.bind("<Leave>", lambda _e: self._paint())
        self._paint()

    @staticmethod
    def _ink(color: str) -> str:
        return FG if _is_light(color) else "white"

    def _paint(self, hover: bool = False) -> None:
        if self._state == "disabled":
            tk.Label.configure(self, bg="#F1ECE0", fg="#B8AE9C",
                               cursor="arrow")
        else:
            c = self._color
            if hover:
                c = _shade(c, -16 if _is_light(c) else 22)
            tk.Label.configure(self, bg=c, fg=self._ink(self._color),
                               cursor="hand2")

    def _click(self, _e=None) -> None:
        if self._state != "disabled" and self._cmd:
            self._cmd()

    def invoke(self) -> None:
        self._click()

    def configure(self, cnf=None, **kw):                   # type: ignore[override]
        if "state" in kw:
            self._state = kw.pop("state")
        if "command" in kw:
            self._cmd = kw.pop("command")
        if "color" in kw:
            self._color = kw.pop("color")
        if cnf or kw:
            tk.Label.configure(self, cnf or {}, **kw)
        self._paint()

    config = configure


# ════════════════════════════════════════════════════════════════════════════
#  Panel de tareas (lo único que ve el participante además del navegador)
# ════════════════════════════════════════════════════════════════════════════

class TaskPanel:
    """Ventana pequeña siempre visible con la tarea en curso."""

    def __init__(self, root: tk.Tk, study: Study,
                 on_begin: Callable[[], None],
                 on_done: Callable[[], None],
                 on_giveup: Callable[[], None],
                 on_abort: Callable[[], None]) -> None:
        self.study = study
        self.win = tk.Toplevel(root)
        self.win.title("Tarea")
        self.win.configure(bg=CARD, highlightbackground=YELLOW,
                           highlightthickness=2)
        self.win.resizable(False, False)
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            pass
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)   # no se cierra
        self.win.bind("<Control-Shift-Q>", lambda _e: on_abort())

        self._begin, self._done, self._giveup = on_begin, on_done, on_giveup

        self._head = tk.Label(self.win, text="", bg=CARD, fg=ACCENT,
                              font=(UI, 11, "bold"), anchor="w")
        self._head.pack(fill="x", padx=18, pady=(12, 0))
        self._instr = tk.Label(self.win, text="", bg=CARD, fg=FG,
                               font=(UI, 15), justify="left", anchor="w",
                               wraplength=760)
        self._instr.pack(fill="x", padx=18, pady=(4, 2))
        self._timer = tk.Label(self.win, text="", bg=CARD, fg=FG2,
                               font=(UI, 10), anchor="w")
        self._timer.pack(fill="x", padx=18)
        row = tk.Frame(self.win, bg=CARD)
        row.pack(fill="x", padx=18, pady=(6, 12))
        self.btn_begin = FlatButton(row, "▶  Comenzar tarea", YELLOW,
                                    self._begin, pady=7, width=22)
        self.btn_done = FlatButton(row, "✔  Terminé la tarea", LEAF,
                                   self._done, pady=7, width=22)
        self.btn_giveup = FlatButton(row, "✖  No pude / me rindo", CORAL,
                                     self._giveup, pady=7, width=22)
        self._place_window()

    # ── posición ─────────────────────────────────────────────────────────

    def _place_window(self) -> None:
        pos = self.study.task_panel_position
        w = 560 if pos == "bottom-right" else min(900, SCREEN_W - 80)
        self._instr.config(wraplength=w - 40)
        self._timer.config(wraplength=w - 40, justify="left")
        # Alto según el contenido: con instrucciones de 2–3 líneas o con el
        # escalado de pantalla de Windows (125–150 %) un alto fijo dejaba
        # los botones fuera de la ventana y el participante no veía
        # «Comenzar».
        self.win.update_idletasks()
        h = max(self.win.winfo_reqheight(), 120)
        if pos == "top":
            x, y = (SCREEN_W - w) // 2, 40
        elif pos == "bottom-right":
            x, y = SCREEN_W - w - 20, SCREEN_H - h - 90
        else:
            x, y = (SCREEN_W - w) // 2, SCREEN_H - h - 90
        self.win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")

    def rect(self) -> List[float]:
        """Rectángulo en pantalla [x, y, ancho, alto] (con margen para el
        borde y la barra de título)."""
        self.win.update_idletasks()
        x, y = self.win.winfo_rootx(), self.win.winfo_rooty()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        return [float(x - 8), float(y - 40), float(w + 16), float(h + 48)]

    # ── estados ──────────────────────────────────────────────────────────

    def _buttons(self, *shown: FlatButton) -> None:
        for b in (self.btn_begin, self.btn_done, self.btn_giveup):
            b.pack_forget()
        for b in shown:
            b.pack(side="left", padx=(0, 10))

    def show_task(self, idx: int, total: int, task) -> None:
        self._head.config(text=f"Tarea {idx + 1} de {total}  ·  {task.title}")
        self._instr.config(text=task.instruction)
        lim = (f"  Tiempo máximo: {int(task.time_limit_s)} s."
               if task.time_limit_s else "")
        self._timer.config(text="Lee la tarea y, cuando estés listo/a, pulsa "
                           "«Comenzar tarea»: el sitio vuelve a la página de "
                           "inicio y empieza a contar el tiempo." + lim,
                           fg=FG2)
        self.btn_begin.configure(state="normal", text="▶  Comenzar tarea")
        self._buttons(self.btn_begin)
        self._place_window()

    def show_loading(self) -> None:
        self.btn_begin.configure(state="disabled", text="Abriendo el sitio…")
        self._timer.config(text="Abriendo el sitio web…", fg=AMBER)

    def show_running(self) -> None:
        self._timer.config(text="", fg=FG2)
        self._buttons(self.btn_done, self.btn_giveup)
        self._place_window()

    def set_timer(self, elapsed_s: float, limit_s: Optional[float]) -> None:
        if limit_s:
            left = max(0, int(limit_s - elapsed_s))
            self._timer.config(
                text=f"Tiempo restante: {left // 60}:{left % 60:02d}",
                fg=RED if left <= 15 else FG2)
        else:
            e = int(elapsed_s)
            self._timer.config(text=f"Tiempo: {e // 60}:{e % 60:02d}", fg=FG2)

    def close(self) -> None:
        try:
            self.win.destroy()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
#  Overlay opcional de la mirada (sólo Windows: necesita click-through)
# ════════════════════════════════════════════════════════════════════════════

class GazeOverlay:
    """
    Cruz que sigue la mirada.  Está DESACTIVADA por defecto: ver la propia
    mirada hace que el participante la corrija y sesga los datos.  Sólo se
    ofrece en Windows, donde la ventana puede ser *click-through*; en macOS no
    es posible sin bloquear los clics del participante.
    """

    def __init__(self, root: tk.Tk, tracker) -> None:
        self.root, self.tracker = root, tracker
        self.win: Optional[tk.Toplevel] = None
        self.canvas: Optional[tk.Canvas] = None
        self._running = False

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    def start(self) -> None:
        if not self.available():
            return
        w = tk.Toplevel(self.root)
        self.win = w
        w.overrideredirect(True)
        try:
            w.attributes("-topmost", True)
            w.attributes("-transparentcolor", "black")
        except Exception:
            pass
        w.configure(bg="black")
        w.geometry(f"{SCREEN_W}x{SCREEN_H}+0+0")
        self.canvas = tk.Canvas(w, width=SCREEN_W, height=SCREEN_H,
                                bg="black", highlightthickness=0)
        self.canvas.pack()
        w.update_idletasks()
        self._click_through(w)
        self._running = True
        self._tick()

    @staticmethod
    def _click_through(w: tk.Toplevel) -> None:
        try:
            hwnd = w.winfo_id()
            user32 = ctypes.windll.user32            # type: ignore[attr-defined]
            parent = user32.GetParent(hwnd)
            hwnd = parent or hwnd
            get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            ex = get_long(hwnd, -20)
            set_long(hwnd, -20, ex | 0x00080000 | 0x00000020 | 0x00000080
                     | 0x08000000)
        except Exception as e:
            print(f"[Overlay] No se pudo activar click-through: {e}")

    def _tick(self) -> None:
        if not self._running or self.canvas is None:
            return
        s = self.tracker.get_latest()
        c = self.canvas
        try:
            c.delete("g")
            if s is not None:
                x, y = int(s.gaze_x), int(s.gaze_y)
                col = "#00FFAA" if s.face_detected else "#FF4455"
                c.create_oval(x - 22, y - 22, x + 22, y + 22, outline=col,
                              width=3, tags="g")
                c.create_oval(x - 4, y - 4, x + 4, y + 4, fill=col,
                              outline="", tags="g")
            self.win.after(33, self._tick)           # type: ignore[union-attr]
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None


# ════════════════════════════════════════════════════════════════════════════
#  Aplicación
# ════════════════════════════════════════════════════════════════════════════

class App:
    def __init__(self, camera_index: Optional[int] = 0,
                 quality: str = "Auto (recomendado)",
                 study_id: Optional[str] = None,
                 tracker=None,
                 session_dir: str = SESSION_DIR,
                 studies_dir: str = STUDIES_DIR,
                 results_dir: str = RESULTS_DIR,
                 hide_main_during_session: bool = True,
                 start_bridge: bool = True,
                 rehearsal: bool = False) -> None:
        self.session_dir = session_dir
        self.studies_dir = studies_dir
        self.results_dir = results_dir
        self._hide_main = hide_main_during_session
        self._rehearsal_default = rehearsal
        os.makedirs(session_dir, exist_ok=True)
        self._quality = quality
        qw, qh, qtune = QUALITY.get(quality, (640, 480, True))

        # ── Componentes ──────────────────────────────────────────────────
        self._injected = tracker is not None
        if tracker is None:
            self.calibrator = GazeCalibrator(n_points=9, method="geometric")
            self.tracker = GazeTracker(camera_index=camera_index,
                                       calibrator=self.calibrator,
                                       cam_width=qw, cam_height=qh,
                                       auto_tune=qtune)
        else:
            self.tracker = tracker
            self.calibrator = getattr(tracker, "calibrator", None) \
                or GazeCalibrator(n_points=9, method="geometric")
            tracker.calibrator = self.calibrator
        self.logger = DataLogger(self.tracker, output_dir=session_dir,
                                 screen_w=SCREEN_W, screen_h=SCREEN_H)
        self.bridge: Optional[WebBridge] = None
        self.overlay: Optional[GazeOverlay] = None
        self.panel: Optional[TaskPanel] = None
        self.nav: Optional[NavegadorIntegrado] = None   # navegador del estudio

        # ── Estado ───────────────────────────────────────────────────────
        self.study: Optional[Study] = None
        self._studies: Dict[str, Study] = {}
        self._study_paths: Dict[str, str] = {}
        self._bad_studies: List[Tuple[str, str]] = []
        self._session_active = False
        self._finishing = False
        self._session_start: Optional[float] = None
        self._last_csv: Optional[str] = None
        self._timer_id: Optional[str] = None
        self._probing = False
        self._cam_infos: List[CameraInfo] = []
        self._debug_win: Optional[tk.Toplevel] = None
        self._debug_img = None
        self._ui_q: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._sessions_paths: List[str] = []

        self._active_engine = None                # calibración/validación
        self._cal_code: Optional[str] = None      # participante calibrado
        self._cal_info: Dict = {}
        self._val_pre: Optional[Dict] = None
        self._width_cm: Optional[float] = None
        self._width_src = ""

        self._tasks_log: List[Dict] = []
        self._task_idx = 0
        self._task_running = False
        self._task_t0 = 0.0
        self._meta: Dict = {}
        self._panel_rect: Optional[List[float]] = None

        # ── UI ───────────────────────────────────────────────────────────
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── Motor y servicios ────────────────────────────────────────────
        if not self._injected:
            self.tracker.start()
        self._reload_studies(select=study_id)
        if start_bridge:
            self._ensure_bridge()
        self._pump_ui_queue()
        self._update_status()

    # ════════════════════════════════════════════════════════════════════
    #  Diálogos (métodos para poder sustituirlos en las pruebas)
    # ════════════════════════════════════════════════════════════════════

    def _info(self, title: str, msg: str) -> None:
        messagebox.showinfo(title, msg, parent=self.root)

    def _warn(self, title: str, msg: str) -> None:
        messagebox.showwarning(title, msg, parent=self.root)

    def _error(self, title: str, msg: str) -> None:
        messagebox.showerror(title, msg, parent=self.root)

    def _yesno(self, title: str, msg: str) -> bool:
        return bool(messagebox.askyesno(title, msg, parent=self.root))

    def _post(self, fn: Callable[[], None]) -> None:
        """Ejecuta ``fn`` en el hilo principal (seguro desde otros hilos)."""
        self._ui_q.put(fn)

    def _pump_ui_queue(self) -> None:
        try:
            while True:
                fn = self._ui_q.get_nowait()
                try:
                    fn()
                except Exception as e:
                    print(f"[UI] error en callback: {e}")
        except queue.Empty:
            pass
        try:
            self.root.after(80, self._pump_ui_queue)
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════════════
    #  Construcción de la interfaz
    # ════════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        self.root = tk.Tk()
        self.root.title("Eye-Tracking · Pruebas de usabilidad")
        self.root.configure(bg=BG)
        W = 680
        H = min(820, max(620, SCREEN_H - 120))
        self.root.geometry(f"{W}x{H}+{max(0, (SCREEN_W - W) // 2)}+"
                           f"{max(0, (SCREEN_H - H) // 2 - 20)}")
        self.root.minsize(620, 580)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0,
                        tabmargins=(12, 8, 12, 0))
        style.configure("TNotebook.Tab", background=TAB, foreground=FG2,
                        padding=(22, 8), font=(UI, 11, "bold"),
                        borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", CARD)],
                  foreground=[("selected", ACCENT)])
        style.configure("TCombobox", fieldbackground=CARD, background=SOFT,
                        foreground=FG, arrowcolor=ACCENT,
                        bordercolor=BORDER, lightcolor=CARD,
                        darkcolor=BORDER)
        style.map("TCombobox",
                  fieldbackground=[("readonly", CARD), ("disabled", BG)],
                  foreground=[("readonly", FG), ("disabled", FG2)])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", SOFT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", FG)

        hdr = tk.Frame(self.root, bg=HEADER, pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Eye-Tracking", font=(UI, 20, "bold"),
                 fg=FG, bg=HEADER).pack()
        tk.Label(hdr, text="Pruebas de usabilidad con seguimiento ocular",
                 font=(UI, 10), fg=FG2, bg=HEADER).pack()
        tk.Frame(self.root, bg=YELLOW, height=3).pack(fill="x")

        st = tk.Frame(self.root, bg=BG)
        st.pack(pady=(10, 0))
        self._lbl_cam = self._pill(st)
        self._lbl_motor = self._pill(st)
        self._lbl_web = self._pill(st)

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.tab_guide = tk.Frame(self.nb, bg=BG)
        self.tab_test = tk.Frame(self.nb, bg=BG)
        self.tab_ana = tk.Frame(self.nb, bg=BG)
        self.nb.add(self.tab_guide, text="Guía")
        self.nb.add(self.tab_test, text="Prueba")
        self.nb.add(self.tab_ana, text="Resultados (equipo)")

        self._scrollers: Dict[str, Tuple[tk.Canvas, tk.Scrollbar]] = {}
        self._build_guide(self.tab_guide)
        self._build_test(self._scroll_area(self.tab_test))
        self._build_analysis(self._scroll_area(self.tab_ana))
        self.root.bind_all("<MouseWheel>", self._on_wheel, add="+")

    def _scroll_area(self, tab: tk.Frame) -> tk.Frame:
        """Contenido de una pestaña con barra de desplazamiento sólo si no
        cabe (pantallas pequeñas o secciones avanzadas abiertas)."""
        canvas = tk.Canvas(tab, bg=BG, highlightthickness=0, bd=0)
        bar = tk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        item = canvas.create_window(0, 0, window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)

        def refresh(_e=None) -> None:
            canvas.itemconfigure(item, width=canvas.winfo_width())
            canvas.configure(scrollregion=(0, 0, inner.winfo_reqwidth(),
                                           inner.winfo_reqheight()))
            if inner.winfo_reqheight() > canvas.winfo_height() + 1:
                if not bar.winfo_ismapped():
                    bar.pack(side="right", fill="y", before=canvas)
            elif bar.winfo_ismapped():
                bar.pack_forget()
                canvas.yview_moveto(0.0)
        inner.bind("<Configure>", refresh)
        canvas.bind("<Configure>", refresh)
        self._scrollers[str(tab)] = (canvas, bar)
        return inner

    def _on_wheel(self, e) -> None:
        try:
            if e.widget.winfo_toplevel() is not self.root:
                return
            canvas, bar = self._scrollers.get(self.nb.select(), (None, None))
        except Exception:
            return
        if canvas is None or not bar.winfo_ismapped():
            return
        step = -e.delta if sys.platform == "darwin" else -e.delta / 120
        canvas.yview_scroll(int(step) or (-1 if e.delta > 0 else 1), "units")

    # ── piezas reutilizables ────────────────────────────────────────────

    @staticmethod
    def _pill(parent) -> tk.Label:
        lbl = tk.Label(parent, text="", font=(UI, 9, "bold"), padx=10,
                       pady=3, bg=TINT[AMBER], fg=AMBER)
        lbl.pack(side="left", padx=4)
        return lbl

    @staticmethod
    def _set_pill(lbl: tk.Label, text: str, color: str) -> None:
        lbl.config(text=text, fg=color, bg=TINT.get(color, "#F1ECE0"))

    @staticmethod
    def _group(parent, title: str) -> tk.Frame:
        g = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                     highlightthickness=1)
        g.pack(fill="x", padx=12, pady=6)
        tk.Label(g, text=title, font=(UI, 11, "bold"), fg=ACCENT, bg=CARD,
                 anchor="w").pack(fill="x", padx=12, pady=(10, 4))
        return g

    @staticmethod
    def _text(parent, text: str = "", small: bool = False, **kw) -> tk.Label:
        bg = kw.pop("bg", CARD)
        return tk.Label(parent, text=text, fg=kw.pop("fg", FG2 if small
                                                     else FG),
                        bg=bg, font=(UI, 9 if small else 10), anchor="w",
                        justify="left", **kw)

    @staticmethod
    def _radio(parent, text, var, value, command=None) -> tk.Radiobutton:
        r = tk.Radiobutton(parent, text=text, variable=var, value=value,
                           fg=FG, bg=CARD, selectcolor=CARD,
                           activebackground=CARD, activeforeground=FG,
                           highlightthickness=0, font=(UI, 10),
                           command=command)
        r.pack(side="left", padx=4)
        return r

    @staticmethod
    def _check(parent, text, var, bg=CARD, **kw) -> tk.Checkbutton:
        return tk.Checkbutton(parent, text=text, variable=var, fg=FG, bg=bg,
                              selectcolor=CARD, activebackground=bg,
                              activeforeground=FG, highlightthickness=0,
                              font=(UI, 10), anchor="w", justify="left", **kw)

    @staticmethod
    def _entry(parent, var, width: Optional[int] = None,
               font=None) -> tk.Entry:
        kw = {"width": width} if width else {}
        return tk.Entry(parent, textvariable=var, bg=CARD, fg=FG,
                        insertbackground=FG, relief="flat",
                        highlightthickness=1, highlightbackground=BORDER,
                        highlightcolor=YELLOW, font=font or (UI, 10), **kw)

    @staticmethod
    def _collapsible(parent, title: str) -> tk.Frame:
        """Sección oculta por defecto que se abre con un clic."""
        head = tk.Label(parent, text=f"▸  {title}", fg=FG2, bg=BG,
                        font=(UI, 10), cursor="hand2", anchor="w")
        head.pack(fill="x", padx=16, pady=(8, 2))
        body = tk.Frame(parent, bg=BG)

        def toggle(_e=None) -> None:
            if body.winfo_ismapped():
                body.pack_forget()
                head.config(text=f"▸  {title}")
            else:
                body.pack(fill="x", after=head)
                head.config(text=f"▾  {title}")
        head.bind("<Button-1>", toggle)
        return body

    # ── Pestaña «Prueba»: los 5 pasos en orden ─────────────────────────

    def _step(self, parent, n: int, title: str, hint: str) -> tk.Frame:
        """Tarjeta de un paso numerado.  Devuelve el cuerpo."""
        g = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                     highlightthickness=2)
        g.pack(fill="x", padx=12, pady=5)
        lbl = tk.Label(g, text=f"{n}.  {title}", font=(UI, 12, "bold"),
                       fg=ACCENT, bg=CARD, anchor="w")
        lbl.pack(fill="x", padx=12, pady=(10, 0))
        if hint:
            self._text(g, hint, small=True, wraplength=580
                       ).pack(fill="x", padx=12, pady=(2, 4))
        self._steps.append((g, lbl, f"{n}.  {title}"))
        return g

    def _build_test(self, tab: tk.Frame) -> None:
        self._steps: List[Tuple[tk.Frame, tk.Label, str]] = []
        self._chk_labels: Dict[str, tk.Label] = {}

        # Estudio (una línea; selector sólo si hay varios)
        g = tk.Frame(tab, bg=BG)
        g.pack(fill="x", padx=14, pady=(4, 0))
        self._lbl_study_name = tk.Label(g, text="", fg=FG, bg=BG,
                                        font=(UI, 10, "bold"), anchor="w",
                                        justify="left", wraplength=600)
        self._var_study = tk.StringVar()
        self._cbo_study = ttk.Combobox(g, textvariable=self._var_study,
                                       state="readonly", width=48,
                                       font=(UI, 10))
        self._cbo_study.bind("<<ComboboxSelected>>",
                             lambda _e: self._on_study_selected())
        self._lbl_study = self._text(g, small=True, wraplength=600, bg=BG)
        self._lbl_study.pack(fill="x")
        self._chk_labels["study"] = self._text(g, small=True, bg=BG)

        # 1 · Código
        g = self._step(tab, 1, "Escribe tu código de participante",
                       "El que te dio el equipo (por ejemplo P07). Nunca tu "
                       "nombre.")
        row = tk.Frame(g, bg=CARD)
        row.pack(fill="x", padx=12)
        self._text(row, "Código").pack(side="left")
        self._var_code = tk.StringVar(value="")
        self._entry(row, self._var_code, 7, (UI, 12, "bold")
                    ).pack(side="left", padx=8, ipady=3)
        self._lbl_group_hdr = self._text(row, "Grupo")
        self._var_group = tk.StringVar(value="")
        self._cbo_group = ttk.Combobox(row, textvariable=self._var_group,
                                       state="disabled", width=12,
                                       font=(UI, 10), values=[])
        self._var_glasses = tk.BooleanVar(value=False)
        self._check(g, "Uso lentes", self._var_glasses
                    ).pack(fill="x", padx=12, pady=(4, 0))
        self._chk_labels["code"] = self._text(g)
        self._chk_labels["code"].pack(fill="x", padx=12, pady=(2, 10))

        # 2 · Sitio e inicio de sesión
        g = self._step(tab, 2, "Abre el sitio e inicia sesión con tu cuenta",
                       "Se abre una ventana del prototipo con el sitio. Entra "
                       "con tu cuenta UDEM: en la página de inicio de sesión "
                       "no se registra nada. Deja esa ventana abierta.")
        FlatButton(g, "🌐  Abrir el sitio", YELLOW, self._open_study_site,
                   pady=7, width=22, font=(UI, 11, "bold")
                   ).pack(anchor="w", padx=12, pady=(4, 2))
        self._chk_labels["web"] = self._text(g, wraplength=580)
        self._chk_labels["web"].pack(fill="x", padx=12, pady=(2, 10))

        # 3 · Calibración
        g = self._step(tab, 3, "Calibra la mirada",
                       "Cara iluminada de frente, a unos 60 cm de la pantalla. "
                       "Mira cada punto que aparezca SIN mover la cabeza "
                       "(≈ 1 minuto). Al final se comprueba la precisión.")
        row = tk.Frame(g, bg=CARD)
        row.pack(fill="x", padx=12, pady=(4, 2))
        self._btn_cal = FlatButton(row, "🎯  Calibrar", YELLOW,
                                   self._do_calibrate, pady=7, width=22,
                                   font=(UI, 11, "bold"))
        self._btn_cal.pack(side="left")
        FlatButton(row, "🎥  Ver cámara", SOFT, self._toggle_debug,
                   pady=7).pack(side="left", padx=8)
        self._chk_labels["camera"] = self._text(g, wraplength=580)
        self._chk_labels["camera"].pack(fill="x", padx=12)
        self._lbl_cal = self._text(g, "Calibración: pendiente", fg=FG2)
        self._lbl_cal.pack(fill="x", padx=12)
        self._lbl_val = self._text(g, "Verificación: pendiente", fg=FG2)
        self._lbl_val.pack(fill="x", padx=12, pady=(0, 10))
        # El preflight también escribe aquí (no se muestran: _lbl_cal/_val
        # ya dan el mismo dato con más detalle)
        self._chk_labels["cal"] = self._text(g)
        self._chk_labels["val"] = self._text(g)

        # Valores fijos: 9 puntos y modelo geométrico (invariante a la
        # cabeza).  Con "polynomial" el modelo geométrico no se usaba nunca.
        self._var_q = tk.StringVar(value=self._quality)
        self._var_pts = tk.IntVar(value=9)
        self._var_method = tk.StringVar(value="geometric")

        # 4 · Tareas
        g = self._step(tab, 4, "Haz las 3 tareas",
                       "Lee y acepta el consentimiento. Esta ventana se "
                       "esconde y abajo aparece un panel con cada tarea: "
                       "léela, pulsa «Comenzar tarea», hazla en el sitio y "
                       "pulsa «Terminé» (o «No pude»).")
        row = tk.Frame(g, bg=CARD)
        row.pack(fill="x", padx=12, pady=(4, 6))
        self._btn_start = FlatButton(row, "▶  Empezar la prueba", YELLOW,
                                     self._do_start, pady=8, width=22,
                                     font=(UI, 11, "bold"))
        self._btn_start.pack(side="left", padx=(0, 8))
        self._btn_stop = FlatButton(row, "■  Terminar", CORAL,
                                    self._do_stop, state="disabled",
                                    pady=8, width=12,
                                    font=(UI, 11, "bold"))
        self._btn_stop.pack(side="left")
        self._lbl_session = tk.Label(g, text="Todavía no empieza", fg=FG2,
                                     bg=CARD, font=(UI, 10, "bold"),
                                     anchor="w")
        self._lbl_session.pack(fill="x", padx=12)
        self._lbl_session2 = self._text(g, small=True)
        self._lbl_session2.pack(fill="x", padx=12, pady=(0, 10))

        # 5 · Enviar
        g = self._step(tab, 5, "Envía tus datos",
                       "Crea un .zip con tu código y súbelo a la carpeta de "
                       "Google Drive que te dio el equipo.")
        FlatButton(g, "📦  Preparar archivo para enviar", SOFT,
                   self._export_zip, pady=7).pack(anchor="w", padx=12,
                                                  pady=(4, 12))

        self._var_rehearsal = tk.BooleanVar(value=self._rehearsal_default)
        cam = self._collapsible(tab, "¿No se detecta la cámara?")
        g = self._group(cam, "Cámara")
        row = tk.Frame(g, bg=CARD)
        row.pack(fill="x", padx=12, pady=(0, 10))
        self._var_cam = tk.StringVar(value="(automática)")
        self._cbo_cam = ttk.Combobox(row, textvariable=self._var_cam,
                                     width=32, state="readonly",
                                     font=(UI, 9), values=["(automática)"])
        self._cbo_cam.pack(side="left")
        self._cbo_cam.bind("<<ComboboxSelected>>", self._on_cam_selected)
        self._btn_scan = FlatButton(row, "🔍 Buscar cámaras", SOFT,
                                    self._do_scan, pady=3)
        self._btn_scan.pack(side="left", padx=6)
        link = tk.Label(tab, text="❔  Ver la guía (permisos y problemas)",
                        fg=ACCENT, bg=BG, font=(UI, 10, "underline"),
                        cursor="hand2")
        link.pack(anchor="w", padx=16, pady=(6, 8))
        link.bind("<Button-1>", lambda _e: self.nb.select(self.tab_guide))

    def _paint_steps(self, items: Dict[str, Tuple[str, str]]) -> None:
        """Marca con ✓ los pasos hechos y resalta el que toca."""
        if not getattr(self, "_steps", None):
            return
        cal_txt, cal_col = items.get("cal", ("✗", RED))
        hecho = [
            items.get("code", ("", RED))[1] == GREEN,
            items.get("web", ("", RED))[1] == GREEN,
            cal_col in (GREEN, AMBER) and not str(cal_txt).startswith("✗")
            and "ensayo" not in str(cal_txt),
            bool(self._last_csv) and not self._session_active,
            False,
        ]
        actual = next((i for i, h in enumerate(hecho) if not h), 4)
        for i, (g, lbl, base) in enumerate(self._steps):
            if hecho[i]:
                lbl.config(text="✓ " + base, fg=GREEN)
                g.config(highlightbackground=BORDER)
            else:
                lbl.config(text=base, fg=ACCENT)
                g.config(highlightbackground=YELLOW if i == actual
                         else BORDER)

    # ── Pestaña Guía ────────────────────────────────────────────────────

    def _build_guide(self, tab: tk.Frame) -> None:
        bar = tk.Scrollbar(tab)
        bar.pack(side="right", fill="y")
        txt = tk.Text(tab, wrap="word", bg=BG, fg=FG, relief="flat", bd=0,
                      padx=22, pady=12, highlightthickness=0, cursor="arrow",
                      font=(UI, 10), spacing1=2, spacing3=2,
                      yscrollcommand=bar.set)
        txt.pack(fill="both", expand=True)
        bar.config(command=txt.yview)
        txt.tag_configure("h", font=(UI, 12, "bold"), foreground=ACCENT,
                          spacing1=14, spacing3=4)
        txt.tag_configure("p", lmargin1=10, lmargin2=26)
        txt.tag_configure("note", foreground=FG2, font=(UI, 9),
                          lmargin1=10, lmargin2=10)
        for tag, line in self._guide_lines():
            txt.insert("end", line + "\n", tag)
        txt.config(state="disabled")

    @staticmethod
    def _guide_lines() -> List[Tuple[str, str]]:
        mac = sys.platform == "darwin"
        L: List[Tuple[str, str]] = [
            ("h", "Antes de empezar"),
            ("p", "•  Necesitas internet y la cámara web de la computadora. "
                  "El sitio se abre dentro del prototipo: no hace falta "
                  "Chrome ni instalar extensiones."),
            ("p", "•  Siéntate con buena luz de frente (sin ventanas detrás) "
                  "y a unos 60 cm de la pantalla."),
            ("p", "•  Cierra Zoom, Teams, Meet o cualquier programa que use "
                  "la cámara."),
            ("p", "•  Si cierras esta ventana, vuelve a abrirla con "
                  + ("doble clic en Iniciar_Mac.command (o «bash run.sh» "
                     "en la Terminal, desde la carpeta del prototipo)."
                     if mac else "doble clic en run.bat.")),
            ("h", "1 · Permiso de cámara"),
        ]
        if mac:
            L += [
                ("p", "•  Si aparece «Terminal quiere acceder a la cámara», "
                      "pulsa Permitir."),
                ("p", "•  Si macOS no dejó abrir Iniciar_Mac.command («no se "
                      "puede verificar»): Ajustes del Sistema → Privacidad y "
                      "seguridad → «Abrir de todos modos», o usa «bash "
                      "run.sh» en la Terminal."),
                ("p", "•  Si arriba dice «Sin cámara»: Ajustes del Sistema → "
                      "Privacidad y seguridad → Cámara → activa Terminal. "
                      "Cierra el prototipo y vuelve a abrirlo."),
            ]
        else:
            L += [
                ("p", "•  Configuración de Windows → Privacidad y seguridad → "
                      "Cámara → activa «Acceso a la cámara» y «Permitir que "
                      "las aplicaciones de escritorio accedan a la cámara»."),
            ]
        L += [
            ("p", "•  Si tu antivirus avisa que «python» quiere usar la "
                  "cámara, permítelo: es este prototipo."),
            ("h", "2 · La prueba (pestaña «Prueba», de arriba abajo)"),
            ("p", "1.  Escribe tu código de participante."),
            ("p", "2.  «Abrir el sitio» e inicia sesión con tu cuenta en esa "
                  "ventana. El prototipo no lee ni guarda tu contraseña: en "
                  "la página de inicio de sesión no registra nada."),
            ("p", "3.  «Calibrar»: mira cada punto sin mover la cabeza."),
            ("p", "4.  «Empezar la prueba»: acepta el consentimiento y sigue "
                  "el panel de abajo. En cada tarea: léela → «Comenzar "
                  "tarea» → hazla en el sitio → «Terminé» (o «No pude»). "
                  "Usa sólo la ventana del sitio. Para parar: Ctrl+Mayús+Q."),
            ("p", "5.  «Preparar archivo para enviar» y sube el .zip a la "
                  "carpeta de Google Drive del equipo."),
            ("p", "El paso que toca está marcado en amarillo y cada paso "
                  "hecho aparece con ✓. Al terminar, la ventana del sitio se "
                  "cierra sola y tu sesión se borra de esta computadora; "
                  "puedes borrar la carpeta del prototipo: no queda nada "
                  "instalado fuera de ella."),
            ("h", "Si algo falla"),
            ("p", "•  «Sin cámara»: cierra los programas que usen la cámara, "
                  "revisa el paso 1 y, en la pestaña Prueba, abre «¿No se detecta "
                  "la cámara?»."),
            ("p", "•  «Navegador sin datos»: inicia sesión en la ventana del "
                  "sitio y déjala al frente (no minimizada)."),
            ("p", "•  La ventana del sitio no se abre (Windows): instala "
                  "«Microsoft Edge WebView2 Runtime» desde la página de "
                  "Microsoft (Windows 11 ya lo trae)."),
            ("p", "•  Error de verificación alto: mejora la luz, acércate o "
                  "aléjate un poco y repite la calibración."),
            ("h", "Privacidad"),
            ("note", "No se graba video, fotos ni audio: la cámara sólo "
                     "calcula hacia dónde miras. Tus datos se guardan en "
                     "esta carpeta, sin tu nombre, hasta que tú los subes. "
                     "El prototipo no abre ningún puerto de red ni envía "
                     "nada por internet: sólo carga el sitio del estudio."),
        ]
        return L

    # ── Pestaña 3 · Resultados ──────────────────────────────────────────

    def _build_analysis(self, tab: tk.Frame) -> None:
        g = self._group(tab, "Sesiones grabadas")
        frame = tk.Frame(g, bg=CARD)
        frame.pack(fill="x", padx=12)
        sb = tk.Scrollbar(frame)
        sb.pack(side="right", fill="y")
        self._lst = tk.Listbox(frame, height=7, bg=CARD, fg=FG,
                               selectbackground=SOFT, selectforeground=FG,
                               font=(MONO, 10), relief="flat",
                               highlightthickness=1,
                               highlightbackground=BORDER,
                               highlightcolor=YELLOW,
                               exportselection=False, yscrollcommand=sb.set)
        self._lst.pack(fill="x", expand=True)
        sb.config(command=self._lst.yview)
        row = tk.Frame(g, bg=CARD)
        row.pack(fill="x", padx=12, pady=(8, 10))
        FlatButton(row, "🌐 Ver informe (con heatmaps)", YELLOW,
                   self._do_heatmaps, pady=6).pack(side="left", padx=(0, 6))
        FlatButton(row, "📋 Texto", SOFT, self._do_report,
                   pady=6).pack(side="left", padx=(0, 6))
        FlatButton(row, "✏ Revisar tareas", SOFT, self._do_review,
                   pady=6).pack(side="left", padx=(0, 6))
        FlatButton(row, "↻", SOFT, self._refresh_sessions, pady=6,
                   width=2).pack(side="left")

        g = self._group(tab, "Todo el grupo")
        row = tk.Frame(g, bg=CARD)
        row.pack(fill="x", padx=12)
        self._btn_group = FlatButton(row, "👥  Análisis grupal", YELLOW,
                                     self._do_group, pady=8, width=20,
                                     font=(UI, 11, "bold"))
        self._btn_group.pack(side="left", padx=(0, 6))
        FlatButton(row, "📂 Resultados", SOFT,
                   lambda: self._open_path(self._results_for_study()),
                   pady=8).pack(side="left")
        self._var_incl_invalid = tk.BooleanVar(value=False)
        self._check(g, "Incluir sesiones excluidas por calidad",
                    self._var_incl_invalid).pack(fill="x", padx=12,
                                                 pady=(6, 0))
        self._lbl_group = self._text(g, small=True, wraplength=580)
        self._lbl_group.pack(fill="x", padx=12, pady=(2, 10))

    # ════════════════════════════════════════════════════════════════════
    #  Estudios
    # ════════════════════════════════════════════════════════════════════

    def _reload_studies(self, select: Optional[str] = None) -> None:
        keep = select or (self.study.id if self.study else None)
        ok, bad = scan_studies(self.studies_dir)
        self._bad_studies = bad
        self._studies, self._study_paths = {}, {}
        for sid, path in ok.items():
            try:
                self._studies[sid] = load_study(path)
                self._study_paths[sid] = path
            except StudyError as e:
                self._bad_studies.append((os.path.basename(path), str(e)))
        labels = [s.name for s in self._studies.values()]
        self._cbo_study["values"] = labels
        if len(labels) > 1:
            self._lbl_study_name.pack_forget()
            self._cbo_study.pack(fill="x", padx=12, before=self._lbl_study)
        else:
            self._cbo_study.pack_forget()
            self._lbl_study_name.pack(fill="x", padx=12,
                                      before=self._lbl_study)
        ids = list(self._studies)
        if keep in self._studies:
            self._cbo_study.current(ids.index(keep))
        elif ids:
            self._cbo_study.current(0)
        else:
            self._var_study.set("")
        self._on_study_selected()
        if self._bad_studies and select is None and not self._session_active:
            first = self._bad_studies[0]
            self._warn("Estudios con errores",
                       f"{len(self._bad_studies)} archivo(s) de estudio no "
                       f"se pudieron cargar:\n\n{first[1]}")

    def _on_study_selected(self) -> None:
        ids = list(self._studies)
        idx = self._cbo_study.current()
        self.study = self._studies[ids[idx]] if 0 <= idx < len(ids) else None
        st = self.study
        if st is None:
            msg = "No hay estudios válidos en la carpeta «studies»."
            if self._bad_studies:
                msg += "\n" + "\n".join(f"✗ {n}: {e.splitlines()[0]}"
                                        for n, e in self._bad_studies)
            self._lbl_study_name.config(text="")
            self._lbl_study.config(text=msg, fg=RED)
            self._show_groups([])
            self._refresh_sessions()
            return
        self._lbl_study_name.config(text=st.name)
        site = urlparse(st.url).netloc if st.url else ""
        self._lbl_study.config(
            text=(f"{len(st.tasks)} tareas"
                  + (f"  ·  {site}" if site else "")
                  + (f"\n⚠ {len(self._bad_studies)} estudio(s) con errores "
                     "ignorados (ver la consola)" if self._bad_studies
                     else "")), fg=FG2)
        self._show_groups(st.groups)
        self._refresh_sessions()

    def _show_groups(self, groups: List[str]) -> None:
        """El selector de grupo sólo aparece si el estudio define grupos."""
        if groups:
            self._cbo_group.config(state="readonly", values=groups)
            if self._var_group.get() not in groups:
                self._var_group.set("")
            self._lbl_group_hdr.pack(side="left", padx=(14, 0))
            self._cbo_group.pack(side="left", padx=6)
        else:
            self._cbo_group.config(state="disabled", values=[])
            self._var_group.set("")
            self._lbl_group_hdr.pack_forget()
            self._cbo_group.pack_forget()

    def _study_session_dir(self) -> str:
        d = os.path.join(self.session_dir,
                         self.study.id if self.study else "")
        os.makedirs(d, exist_ok=True)
        return d

    def _results_for_study(self) -> str:
        d = os.path.join(self.results_dir, self.study.id if self.study else "")
        os.makedirs(d, exist_ok=True)
        return d

    # ════════════════════════════════════════════════════════════════════
    #  Puente web
    # ════════════════════════════════════════════════════════════════════

    def _bridge_config(self) -> dict:
        st = self.study
        try:
            task = self.logger.current_task if self._session_active else 0
        except Exception:
            task = 0
        return {
            "rec": bool(self._session_active),
            "task": task,
            "study": st.id if st else "",
            "aois": st.bridge_config() if st else [],
            "hosts": st.allowed_hosts() if st else [],
            # Capturas de la página del estudio (sólo durante las tareas)
            "capture": bool(st and st.page_capture and self._session_active),
            "shots_dir": (shots_dir_for(self._last_csv)
                          if self._session_active and self._last_csv else ""),
        }

    def _bridge_sink(self, kind: str, payload: dict, t_ms: float) -> bool:
        hosts = self.study.allowed_hosts() if self.study else []
        host = urlparse(str(payload.get("url") or "")).hostname
        if hosts and host and host not in hosts:
            return False                  # evento de un sitio ajeno al estudio
        if kind == "vp":
            self._last_vp = payload
        return self.logger.web_event(kind, payload, t_ms)

    def _sitio_fuera_de_pantalla(self) -> bool:
        """La ventana del sitio está en OTRO monitor: la cámara sólo mapea
        la mirada a la pantalla principal (donde se calibró)."""
        vp = getattr(self, "_last_vp", None) or {}
        try:
            sw, sh = float(vp.get("sw") or 0), float(vp.get("sh") or 0)
            x = float(vp.get("wx") or 0) + float(vp.get("iw") or 0) / 2
            y = float(vp.get("wy") or 0) + float(vp.get("ih") or 0) / 2
        except (TypeError, ValueError):
            return False
        return sw > 0 and sh > 0 and not (0 <= x < sw and 0 <= y < sh)

    def _bridge_needs_server(self) -> bool:
        """
        El servidor HTTP local sólo hace falta para páginas propias servidas
        en 127.0.0.1 (la página de prueba).  Con un sitio real (el portal) el
        navegador integrado entrega los eventos por su propio canal y no se
        abre ningún puerto: nada que el firewall o el antivirus revisen.
        """
        hosts = self.study.allowed_hosts() if self.study else []
        return any(h in ("127.0.0.1", "localhost") for h in hosts)

    def _ensure_bridge(self) -> bool:
        listen = self._bridge_needs_server()
        b = self.bridge
        if b is not None and b.is_running and (b.listening or not listen):
            return True
        if b is not None:
            b.stop()
        port = self.study.bridge_port if self.study else 4243
        static = {"/pagina_prueba.html": TEST_PAGE,
                  "/web/eyetracking_bridge.user.js": USERSCRIPT}
        self.bridge = WebBridge(self._bridge_sink, self._bridge_config,
                                port=port, static_files=static)
        return self.bridge.start(listen=listen)

    def _on_nav_evento(self, ev: dict) -> None:
        """
        Eventos del navegador integrado (hilo lector).  De las páginas de
        dominios AJENOS al estudio (p. ej. Trayecto UDEM o la encuesta) sólo
        se registra el dominio, para saber si el participante llegó; ahí no
        se inyecta nada.
        """
        if ev.get("tipo") != "pagina" or ev.get("inyectado"):
            return
        host = urlparse(str(ev.get("url") or "")).hostname
        if not host or not self._session_active:
            return
        try:
            self.logger.web_event("nav", {"url": f"https://{host}/",
                                          "externo": True})
        except Exception:
            pass

    def _bridge_state(self) -> Tuple[str, str]:
        """(texto, color) del estado del puente."""
        b = self.bridge
        if b is None or not b.is_running:
            err = b.last_error if b else "no iniciado"
            return f"✗ Navegador: puente apagado ({err})", RED
        age = b.seconds_since_last_event()
        if age is not None and age < 6:
            if self._sitio_fuera_de_pantalla():
                return ("⚠ La ventana del sitio está en otro monitor: "
                        "muévela a la pantalla principal", AMBER)
            return "✓ Sitio abierto y conectado", GREEN
        if age is not None:
            if self.nav is not None and not self.nav.vivo:
                return ("⚠ Cerraste la ventana del sitio: vuelve a "
                        "pulsar «Abrir el sitio»", AMBER)
            return ("⚠ Sin datos del sitio: inicia sesión y deja la "
                    "ventana del sitio abierta (no minimizada)", AMBER)
        nav = self.nav
        if nav is not None and nav.vivo:
            host = urlparse(nav.url_actual or "").hostname or ""
            permitidos = self.study.allowed_hosts() if self.study else []
            es_login = host.startswith(("login.", "sso.", "auth.", "idp."))
            if host and not es_login and permitidos and host not in permitidos:
                # Diagnóstico: el sitio cargó en un dominio que el estudio
                # no tiene en «bridge.hosts», así que ahí no se registra.
                return (f"⚠ La ventana está en «{host}», que no está en la "
                        "lista del estudio (bridge.hosts): ahí no se "
                        "registra nada. Avísale al equipo.", AMBER)
            if host and nav.inyectado_actual and nav.salida:
                return (f"⚠ El sitio cargó pero no envía datos: "
                        f"{nav.salida[-1][:120]}", AMBER)
            return ("⚠ Ventana abierta: inicia sesión con tu cuenta. "
                    "Se pondrá en verde cuando cargue el sitio."
                    + (f" (ahora en {host})" if host else ""), AMBER)
        return ("Todavía no abres el sitio: pulsa «Abrir el sitio».",
                AMBER)

    # ════════════════════════════════════════════════════════════════════
    #  Cámara
    # ════════════════════════════════════════════════════════════════════

    def _do_scan(self) -> None:
        if self._probing:
            return
        self._probing = True
        self._btn_scan.configure(state="disabled", text="🔍 Buscando…")
        qw, qh, _ = QUALITY.get(self._var_q.get(), (640, 480, True))

        def work():
            infos = GazeTracker.list_cameras(max_index=6, width=qw, height=qh)
            self._post(lambda: self._scan_done(infos))
        threading.Thread(target=work, daemon=True, name="CamScan").start()

    def _scan_done(self, infos: List[CameraInfo]) -> None:
        self._cam_infos = infos
        self._probing = False
        self._btn_scan.configure(state="normal", text="🔍 Detectar")
        self._cbo_cam["values"] = ["(automática)"] + [c.label for c in infos]
        if not infos:
            hint = ("  • Cierra Zoom / Teams / OBS si usan la cámara\n"
                    "  • Revisa el permiso de cámara del sistema\n")
            if sys.platform == "darwin":
                hint += ("  • macOS: Ajustes del Sistema → Privacidad y "
                         "seguridad → Cámara → permite Terminal / Cursor / "
                         "VS Code y reinicia la aplicación\n")
            self._warn("Sin cámaras",
                       "No se encontró ninguna cámara con imagen.\n\n" + hint)
        else:
            self._info("Cámaras detectadas", "Se encontraron:\n\n" + "\n".join(
                f"  • {c.label}" for c in infos))

    def _on_cam_selected(self, _evt=None) -> None:
        label = self._var_cam.get()
        if label == "(automática)":
            self.tracker.request_camera(None)
            return
        for c in self._cam_infos:
            if c.label == label:
                self.tracker.request_camera(c.index)
                return

    def _toggle_debug(self) -> None:
        """Vista de la cámara dentro de Tk (cv2.imshow no es seguro en hilos)."""
        if self._debug_win is not None:
            try:
                self._debug_win.destroy()
            except Exception:
                pass
            self._debug_win = None
            return
        w = tk.Toplevel(self.root)
        w.title("Vista de la cámara")
        w.configure(bg=BG)
        lbl = tk.Label(w, bg=BG, text="Esperando imagen…", fg=FG2)
        lbl.pack()
        self._debug_win = w
        w.protocol("WM_DELETE_WINDOW", self._toggle_debug)
        self._debug_tick(lbl)

    def _debug_tick(self, lbl: tk.Label) -> None:
        if self._debug_win is None:
            return
        try:
            frame = self.tracker.get_debug_frame()
            if frame is not None:
                import cv2
                ok, buf = cv2.imencode(".png", frame)
                if ok:
                    self._debug_img = tk.PhotoImage(
                        data=base64.b64encode(buf.tobytes()).decode("ascii"))
                    lbl.config(image=self._debug_img, text="")
            self.root.after(120, lambda: self._debug_tick(lbl))
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════════════
    #  Calibración y verificación
    # ════════════════════════════════════════════════════════════════════

    def _screen_width_cm(self) -> float:
        if self.study and self.study.screen_width_cm:
            return float(self.study.screen_width_cm)
        if self._width_cm is None:
            self._width_cm, self._width_src = detect_screen_width_cm()
        return self._width_cm

    def _width_source(self) -> str:
        if self.study and self.study.screen_width_cm:
            return "estudio"
        self._screen_width_cm()
        return self._width_src

    def _camera_ok(self) -> bool:
        return bool(getattr(self.tracker, "running", False)
                    and getattr(self.tracker, "camera_info", None) is not None)

    def _do_calibrate(self) -> None:
        if self._session_active:
            self._warn("Sesión activa", "Termina la sesión antes de calibrar.")
            return
        if not self._camera_ok():
            self._error("Sin cámara",
                        "No hay cámara activa. Pulsa «Detectar» o revisa que "
                        "ninguna otra aplicación la esté usando.")
            return
        code = self._var_code.get().strip()
        if not valid_participant_code(code):
            self._warn("Código de participante",
                       "Escribe un código válido (letras, números, - y _; "
                       "sin espacios). Ejemplo: P01.")
            return
        n = int(self._var_pts.get())
        self.calibrator.targets = (self.calibrator.TARGETS_9 if n == 9
                                   else self.calibrator.TARGETS_5)
        self.calibrator.method = self._var_method.get()
        self._btn_cal.configure(state="disabled", text="🎯  Calibrando…")
        self._lbl_cal.config(text="Calibración: en curso…", fg=AMBER)
        self._cal_code = None
        self._val_pre = None
        self._lbl_val.config(text="Verificación: pendiente", fg=AMBER)
        self._cal_target_code = code
        self._cal_target_points = n
        try:
            eng = CalibrationEngine(tracker=self.tracker,
                                    calibrator=self.calibrator,
                                    on_done=self._on_cal_done, n_points=n)
            self._active_engine = eng
            eng.run(master=self.root)        # Toplevel en el hilo principal
        except Exception as e:
            self._on_cal_done(False)
            self._error("Error en la calibración", str(e))

    def _on_cal_done(self, success: bool) -> None:
        self._btn_cal.configure(state="normal", text="🎯  Calibrar y verificar")
        if not success:
            self._lbl_cal.config(text="Calibración: FALLIDA o cancelada "
                                 "(repite)", fg=RED)
            return
        rms = self.calibrator.rms_error_px
        used = getattr(self.calibrator, "method", "?")
        label = {"geometric": "geométrico", "polynomial": "polinómico",
                 "homography": "homografía"}.get(used, used)
        limit = self.study.quality.max_cal_rms_px if self.study else 300.0
        col = GREEN if (rms is not None and rms < limit * 0.8) else AMBER
        if rms is not None and rms > limit:
            col = RED
        self._cal_code = getattr(self, "_cal_target_code", None)
        self._cal_info = {
            "rms_px": float(rms) if rms is not None else None,
            "method": used, "n_samples": int(self.calibrator.n_samples),
            "n_points": getattr(self, "_cal_target_points", 9),
            "at": now_iso(),
        }
        self._lbl_cal.config(
            text=f"Calibración: ✓ {label} · "
                 + (f"error {rms:.0f} px" if rms is not None else "")
                 + (f" · ⚠ supera el máximo del estudio ({limit:.0f} px): "
                    "repite" if (rms is not None and rms > limit) else ""),
            fg=col)
        if used != self._var_method.get():
            self._warn("Calibración degradada",
                       f"Se pidió el modelo {self._var_method.get()} pero se "
                       f"usó el {label} por falta de datos 3D. La mirada "
                       "se moverá si el participante mueve la cabeza. "
                       "Repite la calibración con la cabeza algo móvil.")
        # Verificación automática con puntos distintos a los de calibración
        self.root.after(400, self._run_pre_validation)

    def _run_validation(self, title: str,
                        cb: Callable[[Optional[Dict]], None]) -> None:
        eng = ValidationEngine(
            self.tracker, on_done=cb, title=title,
            screen_w=SCREEN_W, screen_h=SCREEN_H,
            screen_w_cm=self._screen_width_cm(),
            distance_cm=self.study.distance_cm if self.study else 60.0)
        self._active_engine = eng
        eng.run(master=self.root)

    def _run_pre_validation(self) -> None:
        if self._session_active:
            return
        self._lbl_val.config(text="Verificación: en curso…", fg=AMBER)
        try:
            self._run_validation("Verificación de precisión",
                                 self._on_pre_validation)
        except Exception as e:
            self._lbl_val.config(text=f"Verificación: error ({e})", fg=RED)

    def _on_pre_validation(self, result: Optional[Dict]) -> None:
        self._val_pre = result
        if result is None:
            self._lbl_val.config(text="Verificación: omitida o sin datos "
                                 "suficientes (repite la calibración)", fg=RED)
            return
        limit = self.study.quality.max_validation_px if self.study else 350.0
        px = result["mean_px"]
        col = GREEN if px < limit * 0.7 else (AMBER if px <= limit else RED)
        self._lbl_val.config(
            text=f"Verificación: {px:.0f} px (≈ {result['mean_deg']:.1f}° de "
                 "ángulo visual)"
                 + ("" if px <= limit else f" · ⚠ supera {limit:.0f} px: "
                    "repite la calibración"), fg=col)

    # ════════════════════════════════════════════════════════════════════
    #  Lista de comprobaciones y estado
    # ════════════════════════════════════════════════════════════════════

    def _preflight(self) -> Tuple[List[str], Dict[str, Tuple[str, str]]]:
        """(problemas bloqueantes, {clave: (texto, color)})"""
        blocking: List[str] = []
        items: Dict[str, Tuple[str, str]] = {}
        st = self.study
        rehearsal = bool(self._var_rehearsal.get())

        if st is None:
            blocking.append("No hay un estudio válido seleccionado.")
            items["study"] = ("✗ Estudio: ninguno válido", RED)
        else:
            items["study"] = (f"✓ Estudio: {st.name} ({len(st.tasks)} tareas)",
                              GREEN)
            if st.pending:
                items["study"] = (
                    f"⚠ Estudio: {st.name} · falta completar en su JSON: "
                    + ", ".join(st.pending[:4])
                    + (f" y {len(st.pending) - 4} más"
                       if len(st.pending) > 4 else ""), AMBER)

        if self._camera_ok():
            items["camera"] = ("✓ Cámara activa", GREEN)
        else:
            blocking.append("No hay cámara activa.")
            err = getattr(self.tracker, "last_error", None)
            items["camera"] = (f"✗ Cámara: {err or 'no activa'}", RED)

        code = self._var_code.get().strip()
        if not code:
            blocking.append("Escribe el código del participante "
                            "(paso 1).")
            items["code"] = ("✗ Participante: falta escribir el código", RED)
        elif not valid_participant_code(code):
            blocking.append("El código de participante no es válido.")
            items["code"] = ("✗ Participante: código inválido (usa sólo "
                             "letras y números, p. ej. P07)", RED)
        elif st and st.groups and not self._var_group.get().strip():
            blocking.append("Elige el grupo del participante.")
            items["code"] = (f"✗ Participante {code}: falta elegir el grupo",
                             RED)
        else:
            items["code"] = (f"✓ Participante: {code}"
                             + (f" · grupo {self._var_group.get()}"
                                if self._var_group.get() else ""), GREEN)

        if self.calibrator.is_calibrated and self._cal_code == code:
            rms = self.calibrator.rms_error_px
            limit = st.quality.max_cal_rms_px if st else 300.0
            bad = rms is not None and rms > limit
            items["cal"] = (f"{'⚠' if bad else '✓'} Calibración de {code}: "
                            + (f"{rms:.0f} px" if rms is not None else "ok")
                            + (" (supera el máximo; se pedirá confirmar)"
                               if bad else ""), AMBER if bad else GREEN)
        elif rehearsal:
            items["cal"] = ("⚠ Sin calibrar (modo ensayo)", AMBER)
        else:
            blocking.append("Calibra con este participante (paso 3).")
            items["cal"] = (f"✗ Calibración: falta calibrar a {code or '—'}",
                            RED)

        if self._val_pre is not None and self._cal_code == code:
            items["val"] = (f"✓ Verificación: {self._val_pre['mean_px']:.0f} px",
                            GREEN)
        else:
            items["val"] = ("⚠ Verificación de precisión: no hecha", AMBER)

        items["web"] = self._bridge_state()
        return blocking, items

    def _update_status(self) -> None:
        t = self.tracker
        s = t.get_latest()
        face = bool(s.face_detected) if s else False
        ci = getattr(t, "camera_info", None)
        if ci is not None:
            self._set_pill(self._lbl_cam, "● Cámara lista", GREEN)
        elif getattr(t, "last_error", None):
            self._set_pill(self._lbl_cam, "● Sin cámara", RED)
        else:
            self._set_pill(self._lbl_cam, "● Cámara: iniciando…", AMBER)
        self._set_pill(self._lbl_motor,
                       "● Cara detectada" if face else "● Sin cara",
                       GREEN if face else AMBER)
        _txt, col = self._bridge_state()
        self._set_pill(self._lbl_web, {GREEN: "● Navegador conectado",
                                       RED: "● Navegador: error"}.get(
                                           col, "● Navegador sin datos"), col)

        _blocking, items = self._preflight()
        for key, (text, color) in items.items():
            self._chk_labels[key].config(text=text, fg=color)
        self._paint_steps(items)

        if self._session_active and self._session_start:
            el = int(time.perf_counter() - self._session_start)
            n = self.logger.n_rows
            self._lbl_session.config(
                text=f"Sesión ACTIVA ⏺ {el // 60:02d}:{el % 60:02d} · "
                     f"{n} filas", fg=RED)
            task = (self.study.tasks[self._task_idx].title
                    if self.study and self._task_idx < len(self.study.tasks)
                    else "—")
            self._lbl_session2.config(
                text=f"Tarea {min(self._task_idx + 1, len(self._tasks_log))}"
                     f"/{len(self._tasks_log)}: {task}"
                     + (" (en curso)" if self._task_running else " (esperando)"),
                fg=FG2)
        self._timer_id = self.root.after(400, self._update_status)

    # ════════════════════════════════════════════════════════════════════
    #  Sesión
    # ════════════════════════════════════════════════════════════════════

    def _consent_dialog(self, study: Study, code: str) -> Optional[Dict]:
        """Consentimiento informado.  ``None`` si el participante no acepta."""
        win = tk.Toplevel(self.root)
        win.title("Consentimiento informado")
        win.configure(bg=BG)
        W, H = 760, min(720, SCREEN_H - 80)
        win.geometry(f"{W}x{H}+{max(0, (SCREEN_W - W) // 2)}+"
                     f"{max(0, (SCREEN_H - H) // 2 - 30)}")
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        tk.Label(win, text="Consentimiento informado", font=(UI, 16, "bold"),
                 fg=ACCENT, bg=BG).pack(pady=(14, 2))
        tk.Label(win, text=f"{study.name}  ·  participante {code}",
                 font=(UI, 10), fg=FG2, bg=BG).pack()
        box = tk.Frame(win, bg=BG)
        txt = tk.Text(box, wrap="word", bg=CARD, fg=FG, font=(UI, 12),
                      relief="flat", padx=16, pady=14, height=14,
                      highlightthickness=1, highlightbackground=BORDER)
        bar = tk.Scrollbar(box, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", study.consent_text)
        txt.config(state="disabled")
        win.after(50, lambda: txt.yview_moveto(0.0))
        agree = tk.BooleanVar(value=False)
        result: Dict = {"value": None}
        row = tk.Frame(win, bg=BG)
        row.pack(side="bottom", pady=(0, 14))
        btn_ok = FlatButton(row, "Acepto y comenzar", LEAF,
                            lambda: (result.update(value=True), win.destroy()),
                            state="disabled", pady=8, width=22)
        btn_ok.pack(side="left", padx=6)
        FlatButton(row, "No acepto", CORAL, win.destroy, pady=8,
                   width=14).pack(side="left", padx=6)

        def toggle() -> None:
            btn_ok.configure(state="normal" if agree.get() else "disabled")
        self._check(win, "He leído y comprendido la información y otorgo mi "
                    "consentimiento para participar.",
                    agree, bg=BG, command=toggle
                    ).pack(side="bottom", pady=(0, 6))
        box.pack(fill="both", expand=True, padx=16, pady=10)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.grab_set()
        self.root.wait_window(win)
        if result["value"]:
            return {"accepted": True, "version": study.consent_version,
                    "at": now_iso()}
        return None

    def _do_start(self) -> None:
        if self._session_active or self._finishing:
            return
        blocking, _ = self._preflight()
        if blocking:
            self._warn("No se puede iniciar la sesión",
                       "\n".join(f"• {b}" for b in blocking))
            return
        st = self.study
        assert st is not None
        code = self._var_code.get().strip()
        rehearsal = bool(self._var_rehearsal.get()) and not (
            self.calibrator.is_calibrated and self._cal_code == code)

        # Calibración fuera de los límites del estudio → decide el facilitador
        rms = self.calibrator.rms_error_px
        if (not rehearsal and rms is not None
                and rms > st.quality.max_cal_rms_px):
            if not self._yesno(
                    "Calibración con error alto",
                    f"El error de calibración es {rms:.0f} px (máximo del "
                    f"estudio: {st.quality.max_cal_rms_px:.0f} px).\n"
                    "La sesión se marcará como excluida del análisis.\n\n"
                    "¿Continuar de todos modos?"):
                return
        if (self._val_pre is not None and self._val_pre["mean_px"]
                > st.quality.max_validation_px):
            if not self._yesno(
                    "Verificación con error alto",
                    f"La verificación midió {self._val_pre['mean_px']:.0f} px "
                    f"(máximo {st.quality.max_validation_px:.0f} px).\n"
                    "¿Continuar de todos modos?"):
                return
        folder = self._study_session_dir()
        if any(f.startswith(code + "_") and f.endswith(".csv")
               for f in os.listdir(folder)):
            if not self._yesno("Código repetido",
                               f"Ya existe una sesión con el código {code}.\n"
                               "¿Grabar otra de todos modos?"):
                return
        if (st.show_gaze_overlay and not GazeOverlay.available()):
            self._warn("Overlay no disponible",
                       "Mostrar la mirada sólo funciona en Windows; se "
                       "continúa sin overlay.")

        consent = self._consent_dialog(st, code)
        if consent is None:
            self._info("Sesión cancelada", "El participante no aceptó el "
                       "consentimiento. No se grabó nada.")
            return

        # ── meta de la sesión ────────────────────────────────────────────
        ci = getattr(self.tracker, "camera_info", None)
        env = environment_info(
            SCREEN_W, SCREEN_H, self._screen_width_cm(), self._width_source(),
            st.distance_cm,
            getattr(ci, "label", "") if ci else "",
            f"{getattr(ci, 'width', '')}x{getattr(ci, 'height', '')}"
            if ci else "")
        self._meta = new_meta(
            st.id, st.name, st.version, code, self._var_group.get().strip(),
            wears_glasses=bool(self._var_glasses.get()),
            consent=consent, env=env)
        self._meta["calibration"] = dict(self._cal_info) if not rehearsal else {}
        self._meta["rehearsal"] = rehearsal
        self._meta["validation"] = {}
        if self._val_pre is not None and not rehearsal:
            self._meta["validation"]["pre"] = self._val_pre

        # ── arranque ─────────────────────────────────────────────────────
        self._ensure_bridge()
        if self.nav is not None:
            self.nav.hosts_visitados.clear()
            if self.nav.url_actual:
                h = urlparse(self.nav.url_actual).hostname
                if h:
                    self.nav.hosts_visitados.add(h)
        self.logger.start(session_name=code, output_dir=folder)
        self._last_csv = self.logger.csv_path
        self._session_active = True
        self._finishing = False
        self._session_start = time.perf_counter()
        self._task_idx = 0
        self._task_running = False
        self._tasks_log = [{"id": t.id, "title": t.title, "start_ms": None,
                            "end_ms": None, "outcome": None}
                           for t in st.tasks]
        self._btn_start.configure(state="disabled")
        self._btn_stop.configure(state="normal")

        if st.show_gaze_overlay and GazeOverlay.available():
            self.overlay = GazeOverlay(self.root, self.tracker)
            self.overlay.start()
            self._meta["overlay_shown"] = True

        self.panel = TaskPanel(self.root, st, self._task_begin,
                               lambda: self._task_end("success"),
                               lambda: self._task_end("abandoned"),
                               self._emergency_stop)
        self.panel.show_task(0, len(st.tasks), st.tasks[0])
        self._panel_rect = self.panel.rect()
        if self._hide_main:
            try:
                self.root.iconify()
            except Exception:
                pass
        self.root.after(250, self._tick_task)

    # ── tareas ───────────────────────────────────────────────────────────

    def _task_begin(self) -> None:
        if not self._session_active or self._task_running or self.panel is None:
            return
        st = self.study
        assert st is not None
        t = st.tasks[self._task_idx]
        if t.start_url:
            self.panel.show_loading()
            self._abrir_en_navegador(t.start_url)
            idx = self._task_idx
            self.root.after(START_DELAY_MS, lambda: self._task_go(idx))
        else:
            self._task_go(self._task_idx)

    def _task_go(self, idx: int) -> None:
        if (not self._session_active or self._task_running
                or idx != self._task_idx or self.panel is None):
            return
        st = self.study
        assert st is not None
        t = st.tasks[idx]
        t_ms = self.logger.start_task(t.id)
        self._tasks_log[idx]["start_ms"] = t_ms
        self._task_running = True
        self._task_t0 = time.perf_counter()
        self.panel.show_running()
        self._panel_rect = self.panel.rect()
        if st.capture_screenshots and self._last_csv:
            self.root.after(SHOT_DELAY_MS, lambda: self._take_screenshot(idx))

    def _take_screenshot(self, idx: int) -> None:
        if not (self._session_active and self._task_running
                and idx == self._task_idx and self.study and self._last_csv):
            return
        tid = self.study.tasks[idx].id
        path = os.path.join(shots_dir_for(self._last_csv), f"tarea{tid}.png")
        threading.Thread(target=capture_screen, args=(path,),
                         daemon=True, name="Screenshot").start()

    def _task_end(self, outcome: str, advance: bool = True) -> None:
        if not (self._session_active and self._task_running):
            return
        st = self.study
        assert st is not None
        t = st.tasks[self._task_idx]
        t_ms = self.logger.end_task(t.id, outcome)
        rec = self._tasks_log[self._task_idx]
        rec["end_ms"], rec["outcome"] = t_ms, outcome
        self._task_running = False
        if not advance:
            return
        self._task_idx += 1
        if self._task_idx >= len(st.tasks):
            self._finish_session()
        elif self.panel is not None:
            self.panel.show_task(self._task_idx, len(st.tasks),
                                 st.tasks[self._task_idx])

    def _tick_task(self) -> None:
        if not self._session_active:
            return
        if self._task_running and self.study and self.panel is not None:
            t = self.study.tasks[self._task_idx]
            el = time.perf_counter() - self._task_t0
            self.panel.set_timer(el, t.time_limit_s)
            if t.time_limit_s and el >= t.time_limit_s:
                self._task_end("timeout")
        if self._session_active:
            self.root.after(250, self._tick_task)

    def _emergency_stop(self) -> None:
        if self._session_active and self._yesno(
                "Interrumpir sesión", "¿Interrumpir la sesión ahora?"):
            self._finish_session(aborted=True)

    def _do_stop(self) -> None:
        if not self._session_active:
            return
        if self._yesno("Terminar sesión",
                       "¿Terminar la sesión ahora? Las tareas pendientes "
                       "quedarán sin realizar."):
            self._finish_session(aborted=True)

    # ── cierre de la sesión ──────────────────────────────────────────────

    def _finish_session(self, aborted: bool = False) -> None:
        if not self._session_active or self._finishing:
            return
        self._finishing = True
        if self._task_running:
            self._task_end("interrupted", advance=False)
        if self.panel is not None:
            try:
                self._panel_rect = self.panel.rect()
            except Exception:
                pass
            self.panel.close()
            self.panel = None
        if self.overlay is not None:
            self.overlay.stop()
            self.overlay = None
        self._cerrar_navegador()
        try:
            self.root.deiconify()
            self.root.lift()
        except Exception:
            pass

        # Se detiene la grabación ANTES de la verificación final: esos
        # puntos no son parte de las tareas.
        csv_path, stats = self.logger.stop()
        self._session_active = False
        self._btn_stop.configure(state="disabled")
        self._lbl_session.config(text="Sesión terminada · procesando…",
                                 fg=AMBER)
        self._lbl_session2.config(text="")
        self._last_csv = csv_path

        st = self.study
        do_val = (st is not None and st.final_validation and not aborted
                  and stats.get("n_samples", 0) > 0
                  and not self._meta.get("rehearsal"))
        if do_val:
            try:
                self._info("Última comprobación",
                           "Ahora aparecerán 5 puntos: mira cada uno, sin "
                           "mover la cabeza. Dura unos 15 segundos.")
                self._run_validation(
                    "Verificación final de precisión",
                    lambda r: self._complete_session(csv_path, stats, r,
                                                     aborted))
                return
            except Exception as e:
                print(f"[sesión] no se pudo hacer la verificación final: {e}")
        self._complete_session(csv_path, stats, None, aborted)

    def _review_dialog(self, tasks: List[Dict]) -> Optional[Dict]:
        """El facilitador confirma si cada tarea fue realmente correcta."""
        win = tk.Toplevel(self.root)
        win.title("Revisión de tareas")
        win.configure(bg=BG)
        win.geometry("760x%d+%d+%d" % (
            min(640, 190 + 92 * max(1, len(tasks))),
            max(0, (SCREEN_W - 760) // 2), 60))
        tk.Label(win, text="Revisión de las tareas (facilitador)",
                 font=(UI, 15, "bold"), fg=ACCENT, bg=BG).pack(pady=(12, 0))
        tk.Label(win, text="El participante dijo si terminó; tú confirmas "
                 "si lo hizo bien.", font=(UI, 9), fg=FG2, bg=BG).pack()
        vars_: Dict[int, tk.StringVar] = {}
        notes: Dict[int, tk.StringVar] = {}
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=8)
        for t in tasks:
            f = tk.LabelFrame(body, text=f" {t['id']} · {t['title']} ",
                              bg=CARD, fg=ACCENT, font=(UI, 10, "bold"),
                              bd=1, relief="solid")
            f.pack(fill="x", pady=4)
            reported = task_outcome_label(t.get("outcome")) \
                if t.get("start_ms") else "no realizada"
            tk.Label(f, text=f"El participante: {reported}", bg=BG3, fg=FG2,
                     font=(UI, 9)).pack(anchor="w", padx=8)
            row = tk.Frame(f, bg=BG3)
            row.pack(fill="x", padx=8, pady=(0, 4))
            default = t.get("verified") or (
                "success" if t.get("outcome") == "success" else "fail")
            v = tk.StringVar(value=default)
            vars_[t["id"]] = v
            for lbl, val in (("✔ Correcta", "success"),
                             ("✖ Incorrecta / incompleta", "fail")):
                self._radio(row, lbl, v, val)
            tk.Label(row, text="Notas:", bg=BG3, fg=FG2, font=(UI, 9)
                     ).pack(side="left", padx=(12, 2))
            nv = tk.StringVar(value=t.get("notes", ""))
            notes[t["id"]] = nv
            self._entry(row, nv, 28).pack(side="left")
        tk.Label(win, text="Observaciones de la sesión:", bg=BG, fg=FG2,
                 font=(UI, 9)).pack(anchor="w", padx=16)
        sess_notes = tk.StringVar(value=self._meta.get("notes", ""))
        self._entry(win, sess_notes).pack(fill="x", padx=16, ipady=3)
        out: Dict = {"value": None}

        def save() -> None:
            out["value"] = {
                "verified": {i: v.get() for i, v in vars_.items()},
                "notes": {i: n.get().strip() for i, n in notes.items()},
                "session_notes": sess_notes.get().strip()}
            win.destroy()
        FlatButton(win, "Guardar revisión", LEAF, save, pady=8,
                   width=20).pack(pady=12)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.grab_set()
        self.root.wait_window(win)
        return out["value"]

    def _complete_session(self, csv_path: str, stats: Dict,
                          post_val: Optional[Dict], aborted: bool) -> None:
        meta = self._meta
        meta["ended_at"] = now_iso()
        meta["aborted"] = bool(aborted)
        meta["tasks"] = [dict(t) for t in self._tasks_log]
        meta["task_panel_rect"] = self._panel_rect
        if post_val is not None:
            meta.setdefault("validation", {})["post"] = post_val
        meta["logger_stats"] = {k: v for k, v in stats.items()
                                if isinstance(v, (int, float, str, bool))}

        if not csv_path or stats.get("n_samples", 0) == 0:
            self._finishing = False
            self._btn_start.configure(state="normal")
            self._lbl_session.config(text="Sesión vacía (sin muestras)",
                                     fg=RED)
            self._warn("Sesión vacía",
                       "No se grabó ninguna muestra. Revisa que la cámara "
                       "vea la cara del participante.")
            return

        if not aborted and self.study is not None and \
                self.study.review_at_end:
            rev = self._review_dialog(meta["tasks"])
            if rev:
                for t in meta["tasks"]:
                    t["verified"] = rev["verified"].get(t["id"])
                    t["notes"] = rev["notes"].get(t["id"], "")
                meta["notes"] = rev["session_notes"]
        save_meta(csv_path, meta)
        self._lbl_session.config(text="Procesando resultados…", fg=AMBER)

        study = self.study

        def work() -> None:
            try:
                an, _txt, files = self._make_outputs(csv_path, study)
                q = an.metrics["quality"]
                meta["quality"] = q
                save_meta(csv_path, meta)
                self._post(lambda: self._session_done(csv_path, an, files, q))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._post(lambda err=e: self._session_done_error(csv_path, err))
        threading.Thread(target=work, daemon=True, name="Outputs").start()

    def _session_done_error(self, csv_path: str, err: Exception) -> None:
        self._finishing = False
        self._btn_start.configure(state="normal")
        self._lbl_session.config(text="Sesión guardada (error al analizar)",
                                 fg=AMBER)
        self._warn("Sesión guardada",
                   f"Los datos están en:\n{csv_path}\n\nPero falló el "
                   f"análisis automático:\n{err}")

    def _session_done(self, csv_path: str, an, files: List[str],
                      quality: Dict) -> None:
        self._finishing = False
        self._btn_start.configure(state="normal")
        self._refresh_sessions()
        self._var_code.set("")
        self._cal_code = None            # el siguiente participante recalibra
        self._val_pre = None
        self._lbl_cal.config(text="Calibración: pendiente (nuevo participante)",
                             fg=AMBER)
        self._lbl_val.config(text="Verificación: pendiente", fg=AMBER)
        lines = ["¡Gracias! La sesión se guardó.", ""]
        if quality["valid"]:
            lines.append("✓ Los datos tienen buena calidad.")
        else:
            lines.append("⚠ Los datos tienen poca calidad:")
            lines += [f"   · {r}" for r in quality["reasons"]]
        lines += ["", "Último paso (5): pulsa «Preparar "
                  "archivo para enviar» y sube el .zip a la carpeta de "
                  "Google Drive del estudio."]
        fuera = self._hosts_no_registrados()
        if fuera:
            lines += ["", "Nota para el equipo: también se visitó "
                      + ", ".join(fuera) + ", que no está en «bridge.hosts» "
                      "del estudio (ahí no se registran scroll ni clics). "
                      "Si es parte de las tareas, agrégalo."]
        self._lbl_session.config(
            text="Sesión guardada ✓ " + ("(válida)" if quality["valid"]
                                         else "(EXCLUIDA)"),
            fg=GREEN if quality["valid"] else AMBER)
        self._info("Sesión completada", "\n".join(lines))

    def _hosts_no_registrados(self) -> List[str]:
        """Dominios visitados fuera del estudio (sin contar el login)."""
        st = self.study
        permitidos = set(st.allowed_hosts()) if st else set()
        visitados = self._meta.get("web_hosts_visitados") or []
        return [h for h in visitados
                if permitidos and h not in permitidos
                and not h.startswith(("login.", "sso.", "auth."))
                and h not in ("127.0.0.1", "localhost")]

    # ════════════════════════════════════════════════════════════════════
    #  Análisis
    # ════════════════════════════════════════════════════════════════════

    def _make_outputs(self, csv_path: str, study: Optional[Study]):
        """Analiza, escribe informe y heatmaps.  Devuelve (an, texto, pngs)."""
        an = analyze_csv(csv_path, study)
        txt = session_report(csv_path, analysis=an)
        sw, sh = an.session.screen_w, an.session.screen_h
        gen = HeatmapGenerator(
            sw, sh, sigma_px=study.heatmap_sigma_px if study else 55.0)
        bgs: Dict[int, str] = {}
        shots = shots_dir_for(csv_path)
        if study:
            for t in study.tasks:
                shot = os.path.join(shots, f"tarea{t.id}.png")
                if os.path.exists(shot):
                    bgs[t.id] = shot
                elif t.background:
                    p = study.resolve_path(t.background)
                    if p and os.path.exists(p):
                        bgs[t.id] = p
        files = gen.session_images(an, os.path.splitext(csv_path)[0], bgs)
        try:
            write_html(an, files, csv_path)
        except Exception as e:
            print(f"[informe] no se pudo escribir el HTML: {e}")
        return an, txt, files

    def _refresh_sessions(self) -> None:
        self._lst.delete(0, "end")
        self._sessions_paths = []
        if self.study is None:
            return
        paths = find_sessions(self._study_session_dir(), self.study.id)
        for p in paths:
            m = load_meta(p) or {}
            q = m.get("quality") or {}
            if not q:
                tag = "sin analizar"
            elif q.get("valid"):
                tag = "✓ válida"
            else:
                tag = "✗ excluida"
            if m.get("rehearsal"):
                tag += " · ensayo"
            when = (m.get("started_at") or "")[:16].replace("T", " ")
            self._lst.insert("end", f"{m.get('participant', '?'):<8} {when:<17}"
                                    f" {tag}")
            self._sessions_paths.append(p)

    def _selected_csv(self) -> Optional[str]:
        sel = self._lst.curselection()
        if sel:
            return self._sessions_paths[sel[0]]
        if self._sessions_paths:
            return self._sessions_paths[0]
        self._info("Sin sesiones", "Todavía no hay sesiones grabadas para "
                   "este estudio.")
        return None

    def _show_text_window(self, title: str, txt: str, footer: str = "") -> None:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG)
        win.geometry("900x640")
        frame = tk.Frame(win, bg=BG)
        frame.pack(fill="both", expand=True, padx=10, pady=10)
        sb = tk.Scrollbar(frame)
        sb.pack(side="right", fill="y")
        t = tk.Text(frame, bg=CARD, fg=FG, font=(MONO, 10), wrap="none",
                    relief="flat", padx=12, pady=10, highlightthickness=1,
                    highlightbackground=BORDER, yscrollcommand=sb.set)
        t.pack(fill="both", expand=True)
        sb.config(command=t.yview)
        t.insert("1.0", txt)
        t.config(state="disabled")
        if footer:
            tk.Label(win, text=footer, fg=FG2, bg=BG, font=(UI, 9)
                     ).pack(pady=(0, 8))

    def _do_report(self) -> None:
        p = self._selected_csv()
        if not p:
            return
        try:
            txt = session_report(p, study=self.study)
        except Exception as e:
            self._error("Error", f"No se pudo generar el informe:\n{e}")
            return
        self._show_text_window("Informe de sesión", txt,
                               f"Guardado en: {os.path.splitext(p)[0]}_informe.txt")

    def _do_heatmaps(self) -> None:
        p = self._selected_csv()
        if not p:
            return

        def work() -> None:
            try:
                _an, _t, files = self._make_outputs(p, self.study)
                html_path = f"{os.path.splitext(p)[0]}_informe.html"
                self._post(lambda: self._open_path(
                    html_path if os.path.exists(html_path)
                    else os.path.dirname(p)))
            except Exception as e:
                self._post(lambda err=e: self._error(
                    "Error", f"No se pudo generar:\n{err}"))
        threading.Thread(target=work, daemon=True).start()

    def _do_review(self) -> None:
        p = self._selected_csv()
        if not p:
            return
        meta = load_meta(p)
        if not meta or not meta.get("tasks"):
            self._info("Sin tareas", "Esta sesión no tiene tareas que revisar.")
            return
        self._meta = meta
        rev = self._review_dialog(meta["tasks"])
        if not rev:
            return
        for t in meta["tasks"]:
            t["verified"] = rev["verified"].get(t["id"])
            t["notes"] = rev["notes"].get(t["id"], "")
        meta["notes"] = rev["session_notes"]
        try:
            an = analyze_csv(p, self.study)
            meta["quality"] = an.metrics["quality"]
        except Exception:
            pass
        save_meta(p, meta)
        self._refresh_sessions()
        self._info("Revisión guardada",
                   "Vuelve a generar el informe y el análisis grupal para ver "
                   "los cambios.")

    def _do_group(self) -> None:
        if self.study is None:
            return
        study = self.study
        out = self._results_for_study()
        incl = bool(self._var_incl_invalid.get())
        self._btn_group.configure(state="disabled", text="Analizando…")

        def work() -> None:
            try:
                res = analyze_study(study, self._study_session_dir(), out,
                                    include_invalid=incl)
                self._post(lambda: self._group_done(res, out))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._post(lambda err=e: (self._btn_group.configure(
                    state="normal", text="👥  Análisis grupal"),
                    self._error("Error", f"Falló el análisis grupal:\n{err}")))
        threading.Thread(target=work, daemon=True, name="Group").start()

    def _group_done(self, res, out: str) -> None:
        self._btn_group.configure(state="normal", text="👥  Análisis grupal")
        self._lbl_group.config(
            text=f"{res.n_sessions} sesiones · {res.n_valid} válidas · "
                 f"{res.n_used} usadas · archivos en {out}", fg=GREEN)
        self._show_text_window("Análisis grupal", res.summary_text,
                               f"CSV, imágenes y resumen en: {out}")
        self._open_path(out)

    # ════════════════════════════════════════════════════════════════════
    #  Herramientas
    # ════════════════════════════════════════════════════════════════════

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

    def _open_study_site(self) -> None:
        st = self.study
        if st is None or not st.url:
            self._warn("Sin sitio", "El estudio no indica la dirección del "
                       "sitio («url» en su archivo JSON).")
            return
        self._abrir_en_navegador(st.url)

    def _abrir_en_navegador(self, url: str) -> bool:
        """
        Abre ``url`` en el navegador integrado (o navega ahí si ya está
        abierto).  Si pywebview no está instalado, cae al navegador del
        sistema, que sólo registra datos con Tampermonkey.
        """
        self._ensure_bridge()
        st = self.study
        port = self.bridge.port if self.bridge is not None else (
            st.bridge_port if st else 4243)
        hosts = st.allowed_hosts() if st else []
        if not NavegadorIntegrado.disponible():
            self._warn("Navegador integrado no disponible",
                       "Falta el paquete «pywebview». Cierra el prototipo y "
                       "vuelve a abrirlo con run.bat / run.sh para que se "
                       "instale.\n\nMientras tanto se abre el navegador "
                       "normal, que NO registra scroll ni clics.")
            webbrowser.open(url)
            return False
        if self.nav is not None and (self.nav.port != port
                                     or self.nav.hosts != hosts
                                     or self.nav.bridge is not self.bridge):
            self.nav.cerrar()
            self.nav = None
        if self.nav is None:
            self.nav = NavegadorIntegrado(HERE, USERSCRIPT, port, hosts,
                                          titulo=st.name if st else "Estudio",
                                          on_evento=self._on_nav_evento,
                                          bridge=self.bridge)
        if self.nav.abrir(url):
            return True
        detalle = "\n".join(self.nav.salida[-6:])
        self._warn("No se pudo abrir el navegador",
                   "La ventana del sitio no arrancó. En Windows hace falta "
                   "«Microsoft Edge WebView2 Runtime» (viene con Windows 11)."
                   + (f"\n\nDetalle:\n{detalle}" if detalle else ""))
        webbrowser.open(url)
        return False

    def _cerrar_navegador(self) -> None:
        """Cierra la ventana del sitio: se borra la sesión iniciada."""
        if self.nav is not None:
            self._meta["web_hosts_visitados"] = sorted(self.nav.hosts_visitados)
            self.nav.cerrar()

    def _userscript_for_study(self, code: str) -> str:
        """Limita el script a los dominios del estudio en lugar de todos los
        sitios (``@match *://*/*``)."""
        hosts = self.study.allowed_hosts() if self.study else []
        if not hosts:
            return code
        lines = "\n".join(f"// @match        *://{h}/*" for h in hosts)
        return re.sub(r"^// @match\s+\*://\*/\*$", lambda _m: lines, code,
                      count=1, flags=re.M)

    def _export_zip(self) -> None:
        """Comprime los datos del estudio en un .zip listo para subir."""
        folder = self._study_session_dir()
        csvs = [f for f in os.listdir(folder) if f.endswith(".csv")]
        if not csvs:
            self._warn("Sin datos", "Todavía no hay ninguna sesión guardada "
                       "en esta computadora.")
            return
        sid = self.study.id if self.study else "estudio"
        codes = sorted({f.split("_", 1)[0] for f in csvs})
        out_dir = os.path.join(os.path.dirname(os.path.abspath(
            self.session_dir)), "para_enviar")
        os.makedirs(out_dir, exist_ok=True)
        name = (f"{'_'.join(codes)}_{sid}_"
                f"{time.strftime('%Y%m%d_%H%M%S')}.zip")
        out = os.path.join(out_dir, name)
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for root, _dirs, fns in os.walk(folder):
                    for fn in fns:
                        p = os.path.join(root, fn)
                        z.write(p, os.path.join(
                            sid, os.path.relpath(p, folder)))
        except OSError as e:
            self._error("Error", f"No se pudo crear el archivo:\n{e}")
            return
        self._open_path(out_dir)
        self._info("Archivo listo", f"Se creó el archivo:\n{name}\n\n"
                   "Súbelo a la carpeta de Google Drive que te dio el equipo "
                   "del estudio.")

    def _copy_userscript(self) -> None:
        try:
            with open(USERSCRIPT, encoding="utf-8") as f:
                code = f.read()
        except OSError as e:
            self._error("No encontrado", str(e))
            return
        code = self._userscript_for_study(code)
        self.root.clipboard_clear()
        self.root.clipboard_append(code)
        self.root.update()
        self._info("Script copiado",
                   "El script del navegador está en el portapapeles.\n\n"
                   "1. Instala la extensión Tampermonkey y activa «Permitir "
                   "scripts de usuario» (ver la pestaña Guía).\n"
                   "2. Tampermonkey → Crear un script nuevo.\n"
                   "3. Borra el contenido, pega (Ctrl/Cmd+V) y guarda.\n"
                   "4. Pulsa «Abrir el sitio del estudio»: arriba debe "
                   "aparecer «● Navegador conectado».")

    # ════════════════════════════════════════════════════════════════════
    #  Cierre
    # ════════════════════════════════════════════════════════════════════

    def _on_close(self) -> None:
        if self._session_active:
            if not self._yesno("Sesión activa",
                               "Hay una sesión activa. ¿Interrumpirla y "
                               "salir?"):
                return
            # Cierre síncrono: se guarda lo grabado sin diálogos
            if self._task_running:
                self._task_end("interrupted", advance=False)
            csv_path, stats = self.logger.stop()
            self._session_active = False
            try:
                self._meta["ended_at"] = now_iso()
                self._meta["aborted"] = True
                self._meta["tasks"] = [dict(t) for t in self._tasks_log]
                self._meta["task_panel_rect"] = self._panel_rect
                if csv_path:
                    save_meta(csv_path, self._meta)
            except Exception as e:
                print(f"[cierre] no se pudo guardar el meta: {e}")
        if self._timer_id:
            try:
                self.root.after_cancel(self._timer_id)
            except Exception:
                pass
        if self.panel is not None:
            self.panel.close()
        if self.overlay is not None:
            self.overlay.stop()
        if self.nav is not None:
            self.nav.cerrar()
        if self.bridge is not None:
            self.bridge.stop()
        if not self._injected:
            self.tracker.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ════════════════════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description="Eye-Tracking Suite v3")
    ap.add_argument("--camera", type=int, default=0,
                    help="Índice de cámara (por defecto 0; se auto-detecta si "
                         "falla). -1 = detección automática.")
    ap.add_argument("--quality", choices=["auto", "high"], default="auto",
                    help="auto = 640×480 con negociación por FPS reales "
                         "(recomendado); high = intenta 1280×720.")
    ap.add_argument("--study", default=None,
                    help="id del estudio a preseleccionar (carpeta studies/)")
    ap.add_argument("--estudios", default=STUDIES_DIR,
                    help="carpeta de estudios (p. ej. studies/ejemplos para "
                         "practicar con la página de prueba)")
    ap.add_argument("--ensayo", action="store_true",
                    help="modo ensayo para el equipo: permite grabar sin "
                         "calibrar; la sesión queda marcada y no entra en el "
                         "análisis grupal")
    args = ap.parse_args()

    cam = None if args.camera is None or args.camera < 0 else args.camera
    quality = {"auto": "Auto (recomendado)",
               "high": "Alta res. 1280×720"}[args.quality]

    try:
        app = App(camera_index=cam, quality=quality, study_id=args.study,
                  studies_dir=os.path.abspath(args.estudios),
                  rehearsal=args.ensayo)
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
