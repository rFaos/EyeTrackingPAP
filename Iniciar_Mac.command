#!/bin/bash
# ============================================================
#  Eye-Tracking Suite - lanzador para macOS (doble clic)
# ============================================================
#  Hace lo mismo que «bash run.sh», pero con doble clic.
#  Si macOS no lo deja abrir: clic derecho > Abrir.
#  Si dice "permiso denegado", en la Terminal:
#      chmod +x Iniciar_Mac.command
# ============================================================
cd "$(dirname "$0")" || exit 1
bash run.sh "$@" || read -r -p " Se cerró con un error. Pulsa Enter para salir..."
