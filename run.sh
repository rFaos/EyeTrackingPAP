#!/usr/bin/env bash
# ============================================================
#  Eye-Tracking Suite - lanzador para macOS / Linux
# ============================================================
#  Abre una Terminal, escribe "bash " (con espacio), arrastra
#  este archivo a la ventana y pulsa Enter.
#
#  La primera vez crea un entorno propio (.venv) dentro de esta
#  carpeta e instala lo necesario (unos minutos, con internet).
#  Las siguientes veces arranca directamente.
#
#      bash run.sh               # lanza la aplicación
#      bash run.sh --camera 1    # opciones de main_app.py
# ============================================================

cd "$(dirname "$0")" || exit 1

export PYTHONIOENCODING=utf-8
NEEDS="import tkinter, cv2, mediapipe, numpy, scipy, screeninfo, webview"
# mediapipe publica paquetes para estas versiones de Python
OK_VERSION="import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)"

# --- 1) Ya instalado -----------------------------------------
CANDIDATES=()
[ -x ".venv/bin/python" ] && CANDIDATES+=(".venv/bin/python")
for c in python3.13 python3.12 python3.11 python3.10 python3.14 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then CANDIDATES+=("$(command -v "$c")"); fi
done
for c in /Library/Frameworks/Python.framework/Versions/3.1[0-3]/bin/python3; do
    [ -x "$c" ] && CANDIDATES+=("$c")
done

for PY in "${CANDIDATES[@]}"; do
    if "$PY" -c "$NEEDS" >/dev/null 2>&1; then
        echo " Usando: $PY"
        if [ "$(uname)" = "Darwin" ]; then
            echo " (macOS: si pide permiso de CÁMARA, acéptalo para la Terminal)"
        fi
        exec "$PY" main_app.py "$@"
    fi
done

# --- 1b) .venv de una versión anterior: falta algún paquete nuevo
if [ -x ".venv/bin/python" ]; then
    echo " Actualizando dependencias..."
    if .venv/bin/python -m pip install -r requirements.txt        && .venv/bin/python -c "$NEEDS" >/dev/null 2>&1; then
        exec .venv/bin/python main_app.py "$@"
    fi
fi

# --- 2) Primera vez: entorno propio + dependencias -----------
for PY in "${CANDIDATES[@]}"; do
    [ "$PY" = ".venv/bin/python" ] && continue
    if "$PY" -c "$OK_VERSION; import tkinter" >/dev/null 2>&1; then
        echo
        echo " Primera vez: instalando lo necesario con $PY"
        echo " (tarda unos minutos y necesita internet; no cierres esta ventana)"
        echo
        if "$PY" -m venv .venv \
           && .venv/bin/python -m pip install --upgrade pip \
           && .venv/bin/python -m pip install -r requirements.txt; then
            echo
            echo " Listo. Abriendo el prototipo…"
            exec .venv/bin/python main_app.py "$@"
        fi
        echo
        echo " [X] La instalación falló. Revisa la conexión a internet y vuelve"
        echo "     a ejecutar:  bash run.sh"
        exit 1
    fi
done

echo
echo " [X] No se encontró Python 3.10–3.13 con interfaz gráfica (tkinter)."
echo
echo "     Instala Python 3.12 desde https://www.python.org/downloads/"
echo "     (el instalador de python.org ya incluye tkinter) y vuelve a"
echo "     ejecutar:  bash run.sh"
exit 1
