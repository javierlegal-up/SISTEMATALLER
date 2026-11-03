@echo off
title Sistema del Taller
cd /d "%~dp0"

set "PROGRAMA=app.py"
set "PUERTO=5000"
if /i "%~1"=="demo" set "PROGRAMA=simulacion.py"
if /i "%~1"=="demo" set "PUERTO=5001"
if /i "%~1"=="demo" title Simulacion del Taller

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"

if not exist "venv\Scripts\python.exe" (
    if not defined PY (
        echo ERROR: no se encontro Python en esta computadora.
        echo Instalalo desde https://www.python.org/downloads/
        echo Durante la instalacion marca la casilla: Add python.exe to PATH
        echo.
        pause
        exit /b 1
    )
    echo Primera ejecucion: preparando el sistema. Espera un momento...
    %PY% -m venv venv
)

if not exist "venv\Scripts\python.exe" (
    echo ERROR: no se pudo crear el entorno de Python.
    echo.
    pause
    exit /b 1
)

"venv\Scripts\python.exe" -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo Instalando dependencias. Se necesita conexion a Internet...
    "venv\Scripts\python.exe" -m pip install -r requirements.txt
)

"venv\Scripts\python.exe" -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo ERROR: no se pudieron instalar las dependencias. Revisa tu conexion a Internet.
    echo.
    pause
    exit /b 1
)

echo.
if /i "%~1"=="demo" echo MODO SIMULACION: datos inventados. Tus datos reales no se tocan.
echo Sistema del Taller en marcha: http://127.0.0.1:%PUERTO%
echo NO cierres esta ventana mientras uses el sistema.
echo Para apagarlo, cierra esta ventana.
echo.

rem Espera unos segundos a que arranque el servidor y abre el navegador.
start "" /min cmd /c "ping -n 4 127.0.0.1 >nul & start http://127.0.0.1:%PUERTO%"

"venv\Scripts\python.exe" %PROGRAMA%

echo.
echo El sistema se detuvo.
pause
