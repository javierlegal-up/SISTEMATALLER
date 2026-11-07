"""Detecta una instancia activa o un puerto 5000 ocupado antes de iniciar Flask."""

import json
import socket
import sys
import urllib.error
import urllib.request
import webbrowser


URL = "http://127.0.0.1:5000"


def verificar():
    try:
        with urllib.request.urlopen(URL + "/healthz", timeout=1) as respuesta:
            estado = json.loads(respuesta.read().decode("utf-8"))
            if respuesta.status == 200 and estado.get("status") == "ok":
                webbrowser.open(URL)
                print("El sistema ya estaba en marcha; abrí la ventana existente.")
                return 2
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as servidor:
        servidor.settimeout(0.5)
        if servidor.connect_ex(("127.0.0.1", 5000)) == 0:
            print("El puerto 5000 está ocupado por otro programa. Cierra esa aplicación e inténtalo otra vez.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(verificar())
