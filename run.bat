@echo off
REM ============================================================
REM  Eye-Tracking Suite - lanzador para Windows (doble clic)
REM ============================================================
REM  La primera vez crea un entorno propio (.venv) dentro de esta
REM  carpeta e instala lo necesario (unos minutos, con internet).
REM  Las siguientes veces arranca directamente.
REM
REM  Busca un Python que tenga tkinter (la interfaz grafica): el
REM  "python" que esta primero en el PATH a veces no lo tiene, y
REM  entonces la app moria al arrancar sin decir por que.
REM ============================================================

setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set "NEEDS=import tkinter, cv2, mediapipe, numpy, scipy, screeninfo, webview"

REM --- 1) Ya instalado -----------------------------------------
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "%NEEDS%" >nul 2>&1
    if not errorlevel 1 (
        echo  Usando: .venv
        ".venv\Scripts\python.exe" main_app.py %*
        goto :fin
    )
    REM  .venv de una version anterior: falta algun paquete nuevo
    echo  Actualizando dependencias...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if not errorlevel 1 (
        ".venv\Scripts\python.exe" main_app.py %*
        goto :fin
    )
)

py -3 -c "%NEEDS%" >nul 2>&1
if not errorlevel 1 (
    echo  Usando: py -3
    py -3 main_app.py %*
    goto :fin
)

python -c "%NEEDS%" >nul 2>&1
if not errorlevel 1 (
    echo  Usando: python
    python main_app.py %*
    goto :fin
)

REM --- 2) Primera vez: buscar un Python 3.10-3.13 con tkinter --
REM  (mediapipe publica paquetes para esas versiones)
set "BASE="
for %%V in (3.12 3.13 3.11 3.10 3.14) do (
    if not defined BASE (
        py -%%V -c "import tkinter" >nul 2>&1
        if not errorlevel 1 set "BASE=py -%%V"
    )
)
if not defined BASE (
    python -c "import sys, tkinter; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 set "BASE=python"
)
if not defined BASE goto :sinpython

echo.
echo  Primera vez: instalando lo necesario con %BASE%
echo  (tarda unos minutos y necesita internet; no cierres esta ventana)
echo.
%BASE% -m venv .venv
if errorlevel 1 goto :fallo
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fallo
echo.
echo  Listo. Abriendo el prototipo...
".venv\Scripts\python.exe" main_app.py %*
goto :fin

:fallo
echo.
echo  [X] La instalacion fallo. Revisa la conexion a internet y vuelve
echo      a hacer doble clic en run.bat.
pause
exit /b 1

:sinpython
echo.
echo  [X] No se encontro Python 3.10-3.13 con interfaz grafica (tkinter).
echo.
echo      Instala Python 3.12 desde https://www.python.org/downloads/
echo      marcando "Add python.exe to PATH" y dejando "tcl/tk and IDLE"
echo      activado; despues vuelve a hacer doble clic en run.bat.
echo.
pause
exit /b 1

:fin
if errorlevel 1 pause
exit /b %errorlevel%
