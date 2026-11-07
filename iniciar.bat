@echo off
title Taller San Cayetano
cd /d "%~dp0"

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

"venv\Scripts\python.exe" -c "import exportar_excel" >nul 2>nul
if errorlevel 1 (
    echo ERROR: no se encontro exportar_excel.py junto a app.py.
    echo.
    pause
    exit /b 1
)

"venv\Scripts\python.exe" -c "import servicio_respaldos" >nul 2>nul
if errorlevel 1 (
    echo ERROR: no se encontro servicio_respaldos.py junto a app.py.
    echo.
    pause
    exit /b 1
)

"venv\Scripts\python.exe" -c "import servicio_analitica" >nul 2>nul
if errorlevel 1 (
    echo ERROR: no se encontro servicio_analitica.py junto a app.py.
    echo.
    pause
    exit /b 1
)

"venv\Scripts\python.exe" -c "import persistencia" >nul 2>nul
if errorlevel 1 (
    echo ERROR: no se encontro persistencia.py junto a app.py.
    echo.
    pause
    exit /b 1
)

"venv\Scripts\python.exe" -c "import servicio_reportes" >nul 2>nul
if errorlevel 1 (
    echo ERROR: no se encontro servicio_reportes.py junto a app.py.
    echo.
    pause
    exit /b 1
)

"venv\Scripts\python.exe" -c "import servicio_seguridad" >nul 2>nul
if errorlevel 1 (
    echo ERROR: no se encontro servicio_seguridad.py junto a app.py.
    echo.
    pause
    exit /b 1
)

"venv\Scripts\python.exe" -c "import servicio_ordenes" >nul 2>nul
if errorlevel 1 (
    echo ERROR: no se encontro servicio_ordenes.py junto a app.py.
    echo.
    pause
    exit /b 1
)

"venv\Scripts\python.exe" -c "import servicio_inventario" >nul 2>nul
if errorlevel 1 (
    echo ERROR: no se encontro servicio_inventario.py junto a app.py.
    echo.
    pause
    exit /b 1
)

"venv\Scripts\python.exe" verificar_inicio.py
if errorlevel 2 (
    echo.
    pause
    exit /b 0
)
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)

echo.
echo Taller San Cayetano en marcha: http://127.0.0.1:5000
echo NO cierres esta ventana mientras uses el sistema.
echo Para apagarlo, cierra esta ventana.
echo.

rem Espera a /healthz y abre el navegador solo cuando Flask está listo.
start "" /min "%~dp0venv\Scripts\pythonw.exe" "%~dp0abrir_navegador.py"

"venv\Scripts\python.exe" app.py

echo.
echo El sistema se detuvo.
pause
