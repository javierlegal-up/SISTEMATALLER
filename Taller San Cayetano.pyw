"""Abre el sistema del taller sin la ventana negra de iniciar.bat.

- Doble clic en este archivo la primera vez: crea el ícono "Taller San Cayetano"
  en el escritorio. Después usa ese ícono.
- Muestra una ventanita con dos botones: "Abrir el sistema" y "Apagar el sistema".
- Si el sistema ya estaba abierto, solo abre el navegador.
- Los errores quedan anotados en taller.log, en esta misma carpeta.
"""
import ctypes
import os
import socket
import struct
import subprocess
import sys
import threading
import webbrowser
import zlib
from pathlib import Path

NOMBRE = "Taller San Cayetano"
CARPETA = Path(__file__).resolve().parent
PUERTO = 5000
URL = f"http://127.0.0.1:{PUERTO}"
VENV_PYTHONW = CARPETA / "venv" / "Scripts" / "pythonw.exe"
ICONO = CARPETA / "static" / "taller.ico"
SIN_VENTANA = 0x08000000  # CREATE_NO_WINDOW


# ---------------------------------------------------------------------------
# Ícono: se dibuja una sola vez (engranaje blanco sobre fondo rojo, como el logo).
# ---------------------------------------------------------------------------
def _pixel(x, y, tam):
    """Color RGBA del punto (x, y) en un ícono de tam x tam, en coordenadas del logo (48x48)."""
    import math
    k = tam / 48
    px, py = x / k, y / k
    # Fondo: cuadrado redondeado.
    radio = 11
    cx = min(max(px, radio), 48 - radio)
    cy = min(max(py, radio), 48 - radio)
    if (px - cx) ** 2 + (py - cy) ** 2 > radio ** 2:
        return None
    dx, dy = px - 24, py - 24
    r = math.hypot(dx, dy)
    blanco = False
    if 9.5 <= r <= 12.5:  # Aro.
        blanco = True
    elif 11.5 <= r <= 16.5:  # Dientes: 8, como el trazo punteado del logo.
        angulo = (math.atan2(dy, dx) + math.pi) % (2 * math.pi / 8)
        blanco = angulo < (4.4 / 11) * (2 * math.pi / 8)
    elif 2.5 <= r <= 5.5:  # Eje central.
        blanco = True
    return (255, 255, 255, 255) if blanco else (0xC8, 0x37, 0x2D, 255)


def _png(tam):
    muestras = 3  # Suavizado de bordes.
    filas = bytearray()
    for y in range(tam):
        filas.append(0)
        for x in range(tam):
            suma = [0, 0, 0, 0]
            for sy in range(muestras):
                for sx in range(muestras):
                    color = _pixel(x + (sx + 0.5) / muestras, y + (sy + 0.5) / muestras, tam)
                    if color:
                        for i in range(4):
                            suma[i] += color[i] if i == 3 else color[i] * color[3] / 255
            alfa = suma[3]
            if alfa:
                filas += bytes(int(suma[i] * 255 / alfa) for i in range(3))
            else:
                filas += b"\0\0\0"
            filas.append(int(alfa / muestras ** 2))

    def bloque(tipo, datos):
        return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos))

    return (
        b"\x89PNG\r\n\x1a\n"
        + bloque(b"IHDR", struct.pack(">IIBBBBB", tam, tam, 8, 6, 0, 0, 0))
        + bloque(b"IDAT", zlib.compress(bytes(filas), 9))
        + bloque(b"IEND", b"")
    )


def crear_icono():
    if ICONO.exists():
        return
    imagenes = [(tam, _png(tam)) for tam in (16, 32, 48, 256)]
    cabecera = struct.pack("<HHH", 0, 1, len(imagenes))
    posicion = 6 + 16 * len(imagenes)
    entradas, datos = b"", b""
    for tam, png in imagenes:
        entradas += struct.pack("<BBBBHHII", tam % 256, tam % 256, 0, 0, 1, 32, len(png), posicion)
        posicion += len(png)
        datos += png
    ICONO.write_bytes(cabecera + entradas + datos)


# ---------------------------------------------------------------------------
# Acceso directo en el escritorio.
# ---------------------------------------------------------------------------
def carpeta_escritorio():
    ruta = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, ruta)  # Escritorio (también con OneDrive).
    return Path(ruta.value)


def crear_acceso_directo():
    acceso = carpeta_escritorio() / f"{NOMBRE}.lnk"
    if acceso.exists():
        return
    comando = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:T_ACCESO);"
        "$s.TargetPath = $env:T_PROGRAMA; $s.Arguments = '\"' + $env:T_SCRIPT + '\"';"
        "$s.WorkingDirectory = $env:T_CARPETA; $s.IconLocation = $env:T_ICONO;"
        f"$s.Description = 'Abrir el sistema {NOMBRE}'; $s.Save()"
    )
    entorno = {
        **os.environ,
        "T_ACCESO": str(acceso),
        "T_PROGRAMA": str(VENV_PYTHONW),
        "T_SCRIPT": str(Path(__file__).resolve()),
        "T_CARPETA": str(CARPETA),
        "T_ICONO": str(ICONO),
    }
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", comando],
        env=entorno, creationflags=SIN_VENTANA, check=False,
    )


# ---------------------------------------------------------------------------
# Servidor y ventanita de control.
# ---------------------------------------------------------------------------
def ya_esta_abierto():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PUERTO)) == 0


def abrir_navegador():
    webbrowser.open(URL)


def main():
    os.chdir(CARPETA)

    # Hay que usar el Python del sistema (venv), que tiene Flask instalado.
    if not VENV_PYTHONW.exists():
        # Primera vez en esta compu: iniciar.bat prepara todo (necesita la ventana para mostrar avisos).
        os.startfile(CARPETA / "iniciar.bat")
        return
    if Path(sys.executable).resolve().parent != VENV_PYTHONW.parent.resolve():
        subprocess.Popen([str(VENV_PYTHONW), str(Path(__file__).resolve())], cwd=CARPETA)
        return

    # Sin consola: lo que se imprima va a taller.log.
    registro = open(CARPETA / "taller.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = registro

    try:
        crear_icono()
        crear_acceso_directo()
    except Exception as error:  # El ícono no es imprescindible para usar el sistema.
        print("No se pudo crear el ícono del escritorio:", error)

    if ya_esta_abierto():
        abrir_navegador()
        return

    if not (CARPETA / "static" / "tailwind.js").exists():
        # Copia local de los estilos; mientras no esté, el sistema usa la de Internet.
        def bajar_tailwind():
            try:
                import descargar_tailwind
                descargar_tailwind.main()
            except Exception as error:
                print("No se pudo descargar Tailwind:", error)

        threading.Thread(target=bajar_tailwind, daemon=True).start()

    import tkinter as tk
    from tkinter import messagebox

    try:
        from werkzeug.serving import make_server
        import app as sistema

        sistema.respaldar_sin_fallar(actualizar=True)
        servidor = make_server("127.0.0.1", PUERTO, sistema.app, threaded=True)
    except Exception as error:
        print("No se pudo iniciar el sistema:", repr(error))
        raiz = tk.Tk()
        raiz.withdraw()
        messagebox.showerror(NOMBRE, f"No se pudo iniciar el sistema.\n\n{error}\n\nDetalle en taller.log.")
        return

    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    threading.Timer(1.0, abrir_navegador).start()

    ventana = tk.Tk()
    ventana.title(NOMBRE)
    ventana.resizable(False, False)
    ventana.configure(bg="white", padx=24, pady=20)
    try:
        ventana.iconbitmap(str(ICONO))
    except tk.TclError:
        pass

    tk.Label(ventana, text=NOMBRE, font=("Segoe UI", 14, "bold"), bg="white", fg="#1e293b").pack(anchor="w")
    tk.Label(
        ventana, text="El sistema está encendido.\nPuedes minimizar esta ventana, pero no la cierres mientras lo uses.",
        font=("Segoe UI", 10), bg="white", fg="#475569", justify="left",
    ).pack(anchor="w", pady=(4, 16))

    botones = tk.Frame(ventana, bg="white")
    botones.pack(fill="x")

    def apagar():
        if messagebox.askyesno(NOMBRE, "¿Apagar el sistema?\nNadie podrá usarlo hasta que lo vuelvas a abrir."):
            servidor.shutdown()
            ventana.destroy()

    tk.Button(
        botones, text="Abrir el sistema", command=abrir_navegador, font=("Segoe UI", 10, "bold"),
        bg="#c8372d", fg="white", activebackground="#a82c23", activeforeground="white",
        relief="flat", padx=16, pady=6, cursor="hand2",
    ).pack(side="left")
    tk.Button(
        botones, text="Apagar el sistema", command=apagar, font=("Segoe UI", 10),
        bg="#f1f5f9", fg="#334155", relief="flat", padx=16, pady=6, cursor="hand2",
    ).pack(side="left", padx=(8, 0))

    ventana.protocol("WM_DELETE_WINDOW", apagar)
    ventana.mainloop()


if __name__ == "__main__":
    main()
