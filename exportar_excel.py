"""Exporta los datos del taller a un libro XLSX sin dependencias externas."""

from __future__ import annotations

import math
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_NS_XML = "http://www.w3.org/XML/1998/namespace"

ET.register_namespace("", _NS_MAIN)
ET.register_namespace("r", _NS_REL)

# La clave_hash y cualquier otra columna de autenticación quedan fuera del Excel.
_TABLAS = (
    ("clientes", "Clientes"),
    ("vehiculos", "Vehículos"),
    ("repuestos_servicios", "Catálogo"),
    ("ordenes", "Órdenes"),
    ("orden_items", "Detalle de órdenes"),
    ("orden_pagos", "Cobros"),
    ("orden_anulaciones", "Anulaciones"),
    ("proveedores", "Proveedores"),
    ("compras", "Compras"),
    ("compra_items", "Detalle de compras"),
    ("compra_recepciones", "Recepciones"),
    ("compra_recepcion_items", "Detalle de recepciones"),
    ("stock_movimientos", "Movimientos de stock"),
    ("proveedor_pagos", "Pagos a proveedores"),
    ("gastos", "Gastos"),
    ("gasto_reversiones", "Reversiones de gastos"),
    ("caja_cierres", "Cierres de caja"),
    ("usuarios", "Usuarios"),
    ("actividad", "Actividad"),
)
_CAMPOS_SENSIBLES = ("clave", "password", "secret", "token", "hash")
_MAX_FILAS_EXCEL = 1_048_576


def _nombre_hoja(nombre, usados):
    limpio = "".join("_" if caracter in "[]:*?/\\" else caracter for caracter in nombre)[:31]
    base = limpio or "Datos"
    candidato = base
    numero = 2
    while candidato.casefold() in usados:
        sufijo = f" {numero}"
        candidato = f"{base[:31 - len(sufijo)]}{sufijo}"
        numero += 1
    usados.add(candidato.casefold())
    return candidato


def _referencia_columna(numero):
    letras = []
    while numero:
        numero, resto = divmod(numero - 1, 26)
        letras.append(chr(65 + resto))
    return "".join(reversed(letras))


def _limpiar_texto(valor):
    texto = str(valor)
    return "".join(
        caracter
        for caracter in texto
        if ord(caracter) in (0x09, 0x0A, 0x0D)
        or 0x20 <= ord(caracter) <= 0xD7FF
        or 0xE000 <= ord(caracter) <= 0xFFFD
        or 0x10000 <= ord(caracter) <= 0x10FFFF
    )


def _agregar_celda(fila, referencia, valor):
    celda = ET.SubElement(fila, f"{{{_NS_MAIN}}}c", {"r": referencia})
    if valor is None:
        return
    if isinstance(valor, bool):
        ET.SubElement(celda, f"{{{_NS_MAIN}}}v").text = "1" if valor else "0"
    elif isinstance(valor, int):
        ET.SubElement(celda, f"{{{_NS_MAIN}}}v").text = str(valor)
    elif isinstance(valor, float) and math.isfinite(valor):
        ET.SubElement(celda, f"{{{_NS_MAIN}}}v").text = repr(valor)
    else:
        celda.set("t", "inlineStr")
        contenido = ET.SubElement(celda, f"{{{_NS_MAIN}}}is")
        texto = ET.SubElement(contenido, f"{{{_NS_MAIN}}}t", {f"{{{_NS_XML}}}space": "preserve"})
        texto.text = _limpiar_texto(valor)


def _xml_hoja(encabezados, filas):
    raiz = ET.Element(f"{{{_NS_MAIN}}}worksheet")
    datos = ET.SubElement(raiz, f"{{{_NS_MAIN}}}sheetData")

    fila_encabezados = ET.SubElement(datos, f"{{{_NS_MAIN}}}row", {"r": "1"})
    for indice, encabezado in enumerate(encabezados, start=1):
        _agregar_celda(fila_encabezados, f"{_referencia_columna(indice)}1", encabezado)

    for numero_fila, valores in enumerate(filas, start=2):
        fila = ET.SubElement(datos, f"{{{_NS_MAIN}}}row", {"r": str(numero_fila)})
        for indice, valor in enumerate(valores, start=1):
            _agregar_celda(fila, f"{_referencia_columna(indice)}{numero_fila}", valor)

    return ET.tostring(raiz, encoding="utf-8", xml_declaration=True)


def _hojas_desde_base(origen):
    ruta = Path(origen)
    if not ruta.is_file():
        raise FileNotFoundError(f"No existe la base de datos: {ruta}")

    conexion = sqlite3.connect(str(ruta))
    conexion.row_factory = sqlite3.Row
    try:
        conexion.execute("BEGIN")
        existentes = {
            fila[0]
            for fila in conexion.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        hojas = []
        usados = set()
        for tabla, titulo in _TABLAS:
            if tabla not in existentes:
                continue
            columnas = [fila[1] for fila in conexion.execute(f'PRAGMA table_info("{tabla}")')]
            if tabla == "usuarios":
                columnas = [
                    columna
                    for columna in columnas
                    if not any(palabra in columna.casefold() for palabra in _CAMPOS_SENSIBLES)
                ]
            if not columnas:
                continue

            nombres = ", ".join(f'"{columna}"' for columna in columnas)
            filas = conexion.execute(f'SELECT {nombres} FROM "{tabla}" ORDER BY 1').fetchall()
            if len(filas) + 1 > _MAX_FILAS_EXCEL:
                raise ValueError(f"La hoja {titulo} supera el límite de filas de Excel.")
            hoja = _nombre_hoja(titulo, usados)
            hojas.append((hoja, columnas, filas))

        if not hojas:
            raise sqlite3.DatabaseError("La base de datos no contiene tablas exportables del taller.")
        return hojas
    finally:
        conexion.close()


def _contenido_libro(hojas):
    tipos = ET.Element(f"{{{_NS_CONTENT_TYPES}}}Types")
    ET.SubElement(
        tipos,
        f"{{{_NS_CONTENT_TYPES}}}Default",
        {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"},
    )
    ET.SubElement(tipos, f"{{{_NS_CONTENT_TYPES}}}Default", {"Extension": "xml", "ContentType": "application/xml"})
    ET.SubElement(
        tipos,
        f"{{{_NS_CONTENT_TYPES}}}Override",
        {
            "PartName": "/xl/workbook.xml",
            "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        },
    )
    for indice in range(1, len(hojas) + 1):
        ET.SubElement(
            tipos,
            f"{{{_NS_CONTENT_TYPES}}}Override",
            {
                "PartName": f"/xl/worksheets/sheet{indice}.xml",
                "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
            },
        )

    relaciones_raiz = ET.Element(f"{{{_NS_PACKAGE_REL}}}Relationships")
    ET.SubElement(
        relaciones_raiz,
        f"{{{_NS_PACKAGE_REL}}}Relationship",
        {
            "Id": "rId1",
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
            "Target": "xl/workbook.xml",
        },
    )

    libro = ET.Element(f"{{{_NS_MAIN}}}workbook")
    lista_hojas = ET.SubElement(libro, f"{{{_NS_MAIN}}}sheets")
    relaciones_libro = ET.Element(f"{{{_NS_PACKAGE_REL}}}Relationships")
    for indice, (nombre, _encabezados, _filas) in enumerate(hojas, start=1):
        ET.SubElement(
            lista_hojas,
            f"{{{_NS_MAIN}}}sheet",
            {"name": nombre, "sheetId": str(indice), f"{{{_NS_REL}}}id": f"rId{indice}"},
        )
        ET.SubElement(
            relaciones_libro,
            f"{{{_NS_PACKAGE_REL}}}Relationship",
            {
                "Id": f"rId{indice}",
                "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
                "Target": f"worksheets/sheet{indice}.xml",
            },
        )

    return {
        "[Content_Types].xml": ET.tostring(tipos, encoding="utf-8", xml_declaration=True),
        "_rels/.rels": ET.tostring(relaciones_raiz, encoding="utf-8", xml_declaration=True),
        "xl/workbook.xml": ET.tostring(libro, encoding="utf-8", xml_declaration=True),
        "xl/_rels/workbook.xml.rels": ET.tostring(relaciones_libro, encoding="utf-8", xml_declaration=True),
    }


def exportar(origen, destino):
    """Crea un libro XLSX con las tablas operativas y devuelve la ruta resultante.

    Los hashes de contraseña y demás campos de autenticación no se exportan.
    Las celdas de texto se guardan como texto literal, nunca como fórmulas.
    """
    hojas = _hojas_desde_base(origen)
    archivos = _contenido_libro(hojas)
    for indice, (_nombre, encabezados, filas) in enumerate(hojas, start=1):
        archivos[f"xl/worksheets/sheet{indice}.xml"] = _xml_hoja(encabezados, filas)

    ruta = Path(destino)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(prefix=f"{ruta.name}.", suffix=".tmp", dir=ruta.parent)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporal, "w", compression=zipfile.ZIP_DEFLATED) as libro:
            for nombre, contenido in archivos.items():
                libro.writestr(nombre, contenido)
        os.replace(temporal, ruta)
    except Exception:
        try:
            os.unlink(temporal)
        except OSError:
            pass
        raise
    return ruta
