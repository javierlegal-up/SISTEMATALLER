"""Descarga Tailwind (los estilos del sistema) a static/tailwind.js.

Así el sistema no depende de un servidor ajeno cada vez que se abre una página
y funciona aunque no haya Internet. Ejecutar una vez, con Internet:

    venv\\Scripts\\python.exe descargar_tailwind.py

iniciar.bat lo ejecuta solo si falta el archivo.
"""
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

VERSION = "3.4.17"  # Versión fija: la que usa el sistema. No se actualiza sola.
URLS = (f"https://cdn.tailwindcss.com/{VERSION}", "https://cdn.tailwindcss.com")
DESTINO = Path(__file__).resolve().parent / "static" / "tailwind.js"


def descargar():
    for url in URLS:
        try:
            with urllib.request.urlopen(url, timeout=30) as respuesta:
                contenido = respuesta.read()
        except OSError as error:
            print(f"No se pudo descargar {url}: {error}")
            continue
        # Comprobación básica de que llegó el programa y no una página de error.
        if len(contenido) > 100_000 and b"tailwind" in contenido.lower():
            return url, contenido
        print(f"Lo descargado de {url} no parece Tailwind; se descarta.")
    return None, None


def main():
    url, contenido = descargar()
    if contenido is None:
        print("No se descargó Tailwind. El sistema seguirá usando el de Internet.")
        return 1
    temporal = DESTINO.with_suffix(".tmp")
    temporal.write_bytes(contenido)
    os.replace(temporal, DESTINO)
    print(f"Tailwind guardado en {DESTINO} (desde {url}).")
    print(f"SHA-256: {hashlib.sha256(contenido).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
