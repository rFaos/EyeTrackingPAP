"""_t_gui.py — Comprobación de arranque de la app (sin interacción)."""
from __future__ import annotations

import sys
import time


def log(msg: str) -> None:
    print(f"[{time.perf_counter():7.2f}s] {msg}", flush=True)


log("importando main_app…")
import main_app                                                    # noqa: E402
log("importado")

log("creando App()…")
app = main_app.App(camera_index=None)
log("App() creada")


def probe() -> None:
    cal = app.calibrator
    log("GUI OK")
    log(f"  calibrador : {cal.method}  |  calibrado: {cal.is_calibrated}")
    log(f"  tracker    : {app.tracker.status}")
    log(f"  camara     : {app.tracker.camera_info}")
    s = app.tracker.get_latest()
    if s is not None:
        f = s.raw_feat or {}
        log(f"  muestra    : cara={s.face_detected} geom_ok={f.get('geom_ok')}")
        if f.get("geom_ok"):
            log(f"  mirada d=({f['dx']:+.3f},{f['dy']:+.3f},{f['dz']:+.3f})  "
                f"ojo E=({f['Ex']:.0f},{f['Ey']:.0f},{f['Ez']:.0f}) mm")
    log("cerrando…")
    app.root.destroy()


log("programando cierre a 7 s y entrando en mainloop")
app.root.after(7000, probe)
app.root.mainloop()
log("mainloop terminado — cierre correcto")
sys.exit(0)
