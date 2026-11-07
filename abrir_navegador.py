"""Espera al endpoint local de salud y abre el sistema cuando está listo."""

import time
import urllib.error
import urllib.request
import webbrowser


URL = "http://127.0.0.1:5000"


for _ in range(120):
    try:
        with urllib.request.urlopen(URL + "/healthz", timeout=1) as respuesta:
            if respuesta.status == 200:
                webbrowser.open(URL)
                break
    except (urllib.error.URLError, TimeoutError):
        time.sleep(0.5)
