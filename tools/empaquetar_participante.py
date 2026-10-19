"""
tools/empaquetar_participante.py  —  .zip mínimo para enviar a cada participante
================================================================================
Crea ``dist/EyeTracking_<estudio>.zip`` sólo con lo que hace falta para hacer
la prueba: la app, el estudio, el modelo de caras y los lanzadores.  Sin
pruebas, diagnósticos, código histórico ni datos de otras sesiones.

Por qué conviene usar este .zip en lugar de comprimir la carpeta a mano:
  • Menos archivos = menos cosas que el antivirus revise y menos dudas del
    participante.
  • El participante sólo ve el estudio real (no los ejemplos).
  • ``run.sh`` e ``Iniciar_Mac.command`` llevan permiso de ejecución y saltos
    de línea de Unix dentro del zip.  Si se comprime en Windows con el
    explorador, ese permiso se pierde y en macOS el doble clic falla con
    «permiso denegado».

Uso (desde la carpeta del proyecto)::

    python tools/empaquetar_participante.py
    python tools/empaquetar_participante.py --estudio studies/ventana_amarilla.json
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Archivos y carpetas de la app (rutas relativas al proyecto)
ARCHIVOS = ["main_app.py", "requirements.txt", "face_landmarker.task",
            "run.bat", "run.sh", "Iniciar_Mac.command",
            os.path.join("web", "eyetracking_bridge.user.js")]
CARPETAS = ["et_suite"]
EJECUTABLES = {"run.sh", "Iniciar_Mac.command"}

LEEME = """\
PRUEBA DE SEGUIMIENTO OCULAR · {nombre}
======================================================================

Necesitas: una computadora con cámara web, internet y unos 20 minutos.
Usa tu cuenta UDEM: iniciarás sesión dentro de la ventana del programa.

1. Descomprime este .zip en una carpeta (por ejemplo, en Documentos).
   No lo abras directamente desde dentro del .zip.

2. Abre el programa:

   WINDOWS
     Doble clic en  run.bat
     Si aparece «Windows protegió su PC»: «Más información» →
     «Ejecutar de todas formas».  (Sale porque el archivo viene de
     internet; el programa no instala nada fuera de esta carpeta.)

   macOS
     Abre la app Terminal, escribe  bash  y un espacio, arrastra el archivo
     run.sh a la ventana y pulsa Enter.
     (También puedes hacer doble clic en Iniciar_Mac.command; si macOS
     dice que no puede verificarlo: Ajustes del Sistema → Privacidad y
     seguridad → «Abrir de todos modos».)

   Necesitas Python 3.12 (gratis, https://www.python.org/downloads/).
   En Windows, al instalarlo marca «Add python.exe to PATH».
   La primera vez el programa tarda unos minutos en prepararse.

3. Si te pregunta por la CÁMARA, permítelo.  No se graba video ni fotos:
   la cámara sólo calcula hacia dónde miras.

4. Sigue la pestaña «Guía» del programa.  Al final, pulsa «Preparar
   archivo para enviar» y sube el .zip que se crea a la carpeta de Google
   Drive que te dio el equipo.

Para quitar el programa, borra esta carpeta: no deja nada instalado en
otro lugar.
"""


def _texto_unix(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read().replace(b"\r\n", b"\n")


def empaquetar(estudio: str, salida: str) -> str:
    sys.path.insert(0, HERE)
    from et_suite.study import load_study          # valida antes de enviar
    st = load_study(estudio)
    if st.pending:
        raise SystemExit("El estudio tiene datos sin completar: "
                         + ", ".join(st.pending))

    raiz = f"EyeTracking_{st.id}"
    os.makedirs(salida, exist_ok=True)
    out = os.path.join(salida, f"{raiz}_{time.strftime('%Y%m%d')}.zip")

    rutas = list(ARCHIVOS)
    for c in CARPETAS:
        for d, _sub, fns in os.walk(os.path.join(HERE, c)):
            if "__pycache__" in d:
                continue
            rutas += [os.path.relpath(os.path.join(d, fn), HERE)
                      for fn in fns if fn.endswith(".py")]

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in rutas:
            src = os.path.join(HERE, rel)
            arc = f"{raiz}/{rel.replace(os.sep, '/')}"
            info = zipfile.ZipInfo(arc, time.localtime()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            if os.path.basename(rel) in EJECUTABLES:
                info.external_attr = 0o100755 << 16
                z.writestr(info, _texto_unix(src))
            else:
                info.external_attr = 0o100644 << 16
                with open(src, "rb") as f:
                    z.writestr(info, f.read())
        with open(estudio, "rb") as f:
            extra = {f"studies/{os.path.basename(estudio)}": f.read(),
                     "LEEME.txt": LEEME.format(nombre=st.name)
                     .replace("\n", "\r\n").encode("utf-8")}
        for rel, data in extra.items():
            info = zipfile.ZipInfo(f"{raiz}/{rel}", time.localtime()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, data)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--estudio", default=os.path.join(
        HERE, "studies", "ventana_amarilla.json"))
    ap.add_argument("--salida", default=os.path.join(HERE, "dist"))
    a = ap.parse_args()
    out = empaquetar(a.estudio, a.salida)
    print(f"Listo: {out}  ({os.path.getsize(out) / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
