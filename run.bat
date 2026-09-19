@echo off
REM ============================================================
REM  Eye-Tracking Suite - lanzador para Windows
REM ============================================================
REM  Busca un Python que tenga tkinter (la interfaz grafica) Y
REM  las dependencias instaladas. Hace falta comprobarlo porque
REM  el "python" que esta primero en el PATH de esta maquina no
REM  tiene tkinter, y entonces la app moria al arrancar sin decir
REM  por que.
REM ============================================================

setlocal
cd /d "%~dp0"

REM --- 1) El lanzador oficial de Windows -----------------------
py -3 -c "import tkinter, cv2, mediapipe" >nul 2>&1
if not errorlevel 1 (
    echo  Usando: py -3
    py -3 main_app.py %*
    goto :fin
)

REM --- 2) Instalaciones tipicas de python.org ------------------
for %%P in (
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
) do (
    if exist %%P (
        %%P -c "import tkinter, cv2, mediapipe" >nul 2>&1
        if not errorlevel 1 (
            echo  Usando: %%P
            %%P main_app.py %*
            goto :fin
        )
    )
)

REM --- 3) Lo que haya en el PATH -------------------------------
python -c "import tkinter, cv2, mediapipe" >nul 2>&1
if not errorlevel 1 (
    echo  Usando: python (del PATH)
    python main_app.py %*
    goto :fin
)

echo.
echo  [X] No se encontro ningun Python con tkinter + dependencias.
echo.
echo      Instala Python 3.11-3.13 desde python.org marcando la
echo      casilla "tcl/tk and IDLE" en el instalador, y despues:
echo.
echo          pip install -r requirements.txt
echo.
pause
exit /b 1

:fin
if errorlevel 1 pause
exit /b %errorlevel%
