"""
main.py  ─  Aplicación de Eye Tracking y Análisis de Mirada
==========================================================================
Flujo de uso:
  1. python main.py
  2. Pantalla de inicio con botones: Calibrar / Iniciar Sesión / Ver Heatmap
  3. [Calibrar] → ventana fullscreen con puntos de calibración
  4. [Iniciar Sesión] → overlay transparente activo + grabación en background
     El usuario usa el navegador o aplicación durante la sesión
  5. [Detener Sesión] → guarda CSV automáticamente
  6. [Ver Heatmap] → genera y abre el PNG del heatmap

También abre una ventana de debug de cámara (opcional, toggle con 'D').
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
import os
import subprocess
import sys
import math
import cv2

from gaze_engine import GazeEngine, GazeCalibrator, SCREEN_W, SCREEN_H
from calibration import CalibrationWindow
from overlay import GazeOverlay
from recorder import GazeRecorder, HeatmapGenerator


# ════════════════════════════════════════════════════════
#  VENTANA DE DEBUG DE CÁMARA (hilo secundario)
# ════════════════════════════════════════════════════════

class DebugWindow(threading.Thread):
    """Ventana OpenCV con el feed de la cámara y los landmarks del motor."""
    def __init__(self, engine: GazeEngine):
        super().__init__(daemon=True, name="DebugWindow")
        self.engine = engine
        self._running = True

    def run(self):
        while self._running:
            frame = self.engine.get_debug_frame()
            if frame is not None:
                cv2.imshow("Eye Tracking Debug (D para cerrar)", frame)
            key = cv2.waitKey(16) & 0xFF
            if key in (ord('q'), ord('d'), 27):
                self._running = False
                break
        cv2.destroyAllWindows()

    def stop(self):
        self._running = False


# ════════════════════════════════════════════════════════
#  APLICACIÓN PRINCIPAL
# ════════════════════════════════════════════════════════

class App:
    SESSION_DIR = "sessions"

    def __init__(self):
        # ── Motor y componentes ──────────────────────────
        self.calibrator = GazeCalibrator(n_points=9)
        self.engine     = GazeEngine(camera_index=0, calibrator=self.calibrator)
        self.overlay    = GazeOverlay(self.engine)
        self.recorder   = GazeRecorder(self.engine, output_dir=self.SESSION_DIR)

        self._debug_win: DebugWindow | None = None
        self._session_active = False
        self._session_start: float | None = None
        self._last_csv: str | None = None
        self._timer_id = None

        # ── Iniciar motor siempre en background ──────────
        self.engine.start()

        # ── UI ───────────────────────────────────────────
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─── Construcción de UI ───────────────────────────────

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Eye Tracking  |  Fabian & Chris")
        self.root.resizable(False, False)
        self.root.configure(bg="#1A1A2E")

        # Icono
        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass

        # Centrar ventana
        W, H = 480, 600
        sx = (SCREEN_W - W) // 2
        sy = (SCREEN_H - H) // 2
        self.root.geometry(f"{W}x{H}+{sx}+{sy}")

        # ── Header ──────────────────────────────────────
        hdr = tk.Frame(self.root, bg="#16213E", pady=18)
        hdr.pack(fill="x")
        tk.Label(hdr, text="👁  Eye Tracking",
                 font=("Arial", 22, "bold"), fg="#E2E2FF", bg="#16213E").pack()
        tk.Label(hdr, text="Sistema de Análisis de Mirada",
                 font=("Arial", 11), fg="#8888AA", bg="#16213E").pack()

        # ── Estado del motor ─────────────────────────────
        status_frame = tk.Frame(self.root, bg="#1A1A2E", pady=8)
        status_frame.pack(fill="x", padx=20)

        self._lbl_fps = tk.Label(status_frame, text="Motor: iniciando…",
                                  font=("Consolas", 11), fg="#44FFAA", bg="#1A1A2E", anchor="w")
        self._lbl_fps.pack(fill="x")
        self._lbl_cal = tk.Label(status_frame,
                                  text="Calibración: NO  (presiona Calibrar)",
                                  font=("Consolas", 11), fg="#FF8844", bg="#1A1A2E", anchor="w")
        self._lbl_cal.pack(fill="x")
        self._lbl_session = tk.Label(status_frame, text="Sesión: inactiva",
                                      font=("Consolas", 11), fg="#AAAACC", bg="#1A1A2E", anchor="w")
        self._lbl_session.pack(fill="x")

        sep = tk.Frame(self.root, bg="#333355", height=1)
        sep.pack(fill="x", padx=12, pady=6)

        # ── Sección calibración ──────────────────────────
        grp_cal = tk.LabelFrame(self.root, text=" 1. Calibración ",
                                 font=("Arial", 11, "bold"),
                                 fg="#CCCCff", bg="#1A1A2E",
                                 bd=1, relief="groove")
        grp_cal.pack(fill="x", padx=16, pady=6)

        self._var_pts = tk.IntVar(value=9)
        pts_frame = tk.Frame(grp_cal, bg="#1A1A2E")
        pts_frame.pack(fill="x", padx=10, pady=4)
        tk.Label(pts_frame, text="Puntos:", fg="#CCCCEE", bg="#1A1A2E",
                 font=("Arial", 10)).pack(side="left")
        tk.Radiobutton(pts_frame, text="9 (Recomendado)", variable=self._var_pts,
                       value=9, fg="#CCCCEE", bg="#1A1A2E",
                       selectcolor="#333366", activebackground="#1A1A2E").pack(side="left", padx=8)
        tk.Radiobutton(pts_frame, text="5 (Rápido)", variable=self._var_pts,
                       value=5, fg="#CCCCEE", bg="#1A1A2E",
                       selectcolor="#333366", activebackground="#1A1A2E").pack(side="left")

        self._btn_cal = self._make_btn(grp_cal, "🎯  Calibrar Ahora",
                                        "#0D7377", self._do_calibrate)
        self._btn_cal.pack(fill="x", padx=10, pady=8)

        # ── Sección sesión ───────────────────────────────
        grp_ses = tk.LabelFrame(self.root, text=" 2. Sesión de Grabación ",
                                 font=("Arial", 11, "bold"),
                                 fg="#CCCCff", bg="#1A1A2E",
                                 bd=1, relief="groove")
        grp_ses.pack(fill="x", padx=16, pady=6)

        name_frame = tk.Frame(grp_ses, bg="#1A1A2E")
        name_frame.pack(fill="x", padx=10, pady=4)
        tk.Label(name_frame, text="Participante / ID:", fg="#CCCCEE", bg="#1A1A2E",
                 font=("Arial", 10)).pack(side="left")
        self._var_name = tk.StringVar(value="")
        tk.Entry(name_frame, textvariable=self._var_name, width=22,
                 bg="#16213E", fg="white", insertbackground="white",
                 relief="flat", font=("Consolas", 10)).pack(side="left", padx=8)

        self._btn_start = self._make_btn(grp_ses, "▶  Iniciar Sesión",
                                          "#145A32", self._do_start_session)
        self._btn_start.pack(fill="x", padx=10, pady=(4, 2))

        self._btn_stop = self._make_btn(grp_ses, "⬛  Detener Sesión",
                                         "#7B241C", self._do_stop_session,
                                         state="disabled")
        self._btn_stop.pack(fill="x", padx=10, pady=(2, 8))

        # ── Sección heatmap ──────────────────────────────
        grp_hm = tk.LabelFrame(self.root, text=" 3. Heatmap ",
                                font=("Arial", 11, "bold"),
                                fg="#CCCCff", bg="#1A1A2E",
                                bd=1, relief="groove")
        grp_hm.pack(fill="x", padx=16, pady=6)

        hm_row = tk.Frame(grp_hm, bg="#1A1A2E")
        hm_row.pack(fill="x", padx=10, pady=4)
        tk.Label(hm_row, text="Modo:", fg="#CCCCEE", bg="#1A1A2E",
                 font=("Arial", 10)).pack(side="left")
        self._var_hm_mode = tk.StringVar(value="fixations")
        for lbl, val in [("Fijaciones", "fixations"), ("Tiempo", "duration"), ("Todos", "all")]:
            tk.Radiobutton(hm_row, text=lbl, variable=self._var_hm_mode,
                           value=val, fg="#CCCCEE", bg="#1A1A2E",
                           selectcolor="#333366", activebackground="#1A1A2E").pack(side="left", padx=4)

        self._btn_hm = self._make_btn(grp_hm, "🗺  Generar Heatmap",
                                       "#4A235A", self._do_heatmap)
        self._btn_hm.pack(fill="x", padx=10, pady=8)

        # ── Extras ──────────────────────────────────────
        extras = tk.Frame(self.root, bg="#1A1A2E")
        extras.pack(fill="x", padx=16, pady=4)
        self._btn_debug = self._make_btn(extras, "🎥  Ventana Debug",
                                          "#2C3E50", self._toggle_debug, width=18)
        self._btn_debug.pack(side="left", padx=(0, 6))
        self._make_btn(extras, "📂  Abrir Carpeta",
                       "#2C3E50", self._open_folder, width=18).pack(side="left")

        # ── Ticker de estado ─────────────────────────────
        self._update_status()

    @staticmethod
    def _make_btn(parent, text, color, cmd, state="normal", width=None):
        kw = dict(text=text, bg=color, fg="white",
                  font=("Arial", 12, "bold"),
                  relief="flat", cursor="hand2",
                  activebackground=color, activeforeground="white",
                  state=state, command=cmd, pady=8)
        if width:
            kw["width"] = width
        return tk.Button(parent, **kw)

    # ─── Acciones ────────────────────────────────────────

    def _do_calibrate(self):
        n = self._var_pts.get()
        self.calibrator.targets = (self.calibrator.TARGETS_9 if n == 9
                                   else self.calibrator.TARGETS_5)

        def run_cal():
            # GazeEngine ya corre; la ventana de calibración usará sus datos
            cal_win = CalibrationWindow(
                engine=self.engine,
                calibrator=self.calibrator,
                on_done=self._on_cal_done,
                n_points=n,
            )
            cal_win.run()

        # Correr en hilo para no bloquear Tkinter principal
        threading.Thread(target=run_cal, daemon=True, name="CalibThread").start()

    def _on_cal_done(self, success: bool):
        if success:
            self.root.after(0, lambda: self._lbl_cal.config(
                text="Calibración: ✓ OK", fg="#44FF88"))
            self.root.after(0, lambda: self._btn_start.config(state="normal"))
        else:
            self.root.after(0, lambda: self._lbl_cal.config(
                text="Calibración: FALLIDA (reintentar)", fg="#FF4444"))

    def _do_start_session(self):
        if self._session_active:
            return
        if not self.calibrator.is_calibrated:
            messagebox.showwarning("Sin calibrar",
                "Por favor calibra primero antes de iniciar una sesión.")
            return

        name = self._var_name.get().strip().replace(" ", "_") or "sesion"
        self._session_active = True
        self._session_start = time.perf_counter()

        self.overlay.start()
        self.recorder.start(session_name=name)

        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")
        self._lbl_session.config(text="Sesión: ACTIVA ⏺", fg="#FF4444")

    def _do_stop_session(self):
        if not self._session_active:
            return
        self._session_active = False

        self.overlay.stop()
        self._last_csv = self.recorder.stop()

        stats = self.recorder.get_stats()
        self._btn_start.config(state="normal")
        self._btn_stop.config(state="disabled")
        self._lbl_session.config(
            text=f"Sesión detenida ✓  ({stats.get('n_samples',0)} muestras, "
                 f"{stats.get('n_fixations',0)} fijaciones)",
            fg="#AAAACC")

        info = (
            f"Sesión guardada ✓\n\n"
            f"Duración: {stats.get('duration_s',0):.1f}s\n"
            f"Muestras: {stats.get('n_samples',0)}\n"
            f"Frec. muestreo: {stats.get('sample_rate_hz',0):.1f} Hz\n"
            f"Fijaciones: {stats.get('n_fixations',0)}\n"
            f"Duración media fijación: {stats.get('mean_fix_duration_ms',0):.0f}ms\n"
            f"Cara detectada: {stats.get('face_detected_pct',0):.1f}%\n\n"
            f"CSV: {self._last_csv}"
        )
        messagebox.showinfo("Sesión completada", info)

    def _do_heatmap(self):
        # Elegir CSV
        if self._last_csv and os.path.exists(self._last_csv):
            csv_path = self._last_csv
        else:
            csv_path = filedialog.askopenfilename(
                title="Selecciona el CSV de sesión",
                initialdir=self.SESSION_DIR,
                filetypes=[("CSV files", "*.csv")])
            if not csv_path:
                return

        # Fondo opcional (screenshot)
        bg = filedialog.askopenfilename(
            title="Imagen de fondo (opcional, cancela para fondo blanco)",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp")])

        mode = self._var_hm_mode.get()

        def gen():
            try:
                gen_hm = HeatmapGenerator(SCREEN_W, SCREEN_H)
                out_path = gen_hm.generate(
                    csv_path=csv_path,
                    background_path=bg if bg else None,
                    mode=mode,
                    sigma_px=55.0,
                    alpha=0.70,
                )
                self.root.after(0, lambda: self._open_file(out_path))
                self.root.after(0, lambda: messagebox.showinfo(
                    "Heatmap generado", f"Guardado en:\n{out_path}"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "Error", f"No se pudo generar el heatmap:\n{e}"))

        threading.Thread(target=gen, daemon=True, name="HeatmapThread").start()
        messagebox.showinfo("Generando…", "Generando heatmap, espera un momento…")

    def _toggle_debug(self):
        if self._debug_win and self._debug_win.is_alive():
            self._debug_win.stop()
        else:
            self._debug_win = DebugWindow(self.engine)
            self._debug_win.start()

    def _open_folder(self):
        folder = os.path.abspath(self.SESSION_DIR)
        os.makedirs(folder, exist_ok=True)
        self._open_file(folder)

    @staticmethod
    def _open_file(path: str):
        try:
            os.startfile(path)
        except Exception:
            subprocess.Popen(["explorer", path])

    # ─── Status ticker ───────────────────────────────────

    def _update_status(self):
        fps = self.engine.fps
        sr  = self.engine.sample_rate
        self._lbl_fps.config(
            text=f"Motor: {fps:.0f} FPS  |  Muestreo: {sr:.0f} Hz")

        if self.calibrator.is_calibrated:
            self._lbl_cal.config(text="Calibración: ✓ OK", fg="#44FF88")

        if self._session_active and self._session_start:
            elapsed = time.perf_counter() - self._session_start
            m, s = divmod(int(elapsed), 60)
            self._lbl_session.config(
                text=f"Sesión ACTIVA ⏺  {m:02d}:{s:02d}", fg="#FF4444")

        self._timer_id = self.root.after(500, self._update_status)

    # ─── Cierre ──────────────────────────────────────────

    def _on_close(self):
        if self._session_active:
            if not messagebox.askyesno("Sesión activa",
                    "Hay una sesión activa. ¿Deseas detenerla y salir?"):
                return
            self._do_stop_session()

        if self._timer_id:
            self.root.after_cancel(self._timer_id)

        self.overlay.stop()
        self.engine.stop()
        self.root.destroy()

    # ─── Arranque ────────────────────────────────────────

    def run(self):
        self.root.mainloop()


# ════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = App()
    app.run()
