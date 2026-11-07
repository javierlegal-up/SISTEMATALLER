"""Consultas y composición del reporte trimestral para el taller."""

from __future__ import annotations

import re
from datetime import date, datetime

from servicio_analitica import variacion


METRICAS_EQUIPO = ("gestionadas", "creadas", "cambios", "clientes")
COLORES_GRAFICO = (
    "#dc2626", "#0f172a", "#f59e0b", "#64748b",
    "#10b981", "#6366f1", "#f472b6", "#a16207",
)


def leer_trimestre(texto):
    """Convierte YYYY-Q; usa el trimestre actual si el valor no es válido."""
    encontrado = re.fullmatch(r"(\d{4})-([1-4])", texto or "")
    if encontrado:
        return int(encontrado[1]), int(encontrado[2])
    hoy = date.today()
    return hoy.year, (hoy.month - 1) // 3 + 1


def trimestre_vecino(anio, trimestre, paso):
    """Devuelve el trimestre situado `paso` posiciones antes o después."""
    indice = anio * 4 + trimestre - 1 + paso
    return indice // 4, indice % 4 + 1


def meses_del_trimestre(anio, trimestre, nombres_mes):
    primero = (trimestre - 1) * 3 + 1
    return [
        (f"{anio}-{mes:02d}", f"{nombres_mes[mes - 1]} {anio}")
        for mes in range(primero, primero + 3)
    ]


def _ordenes_ingresadas_por_mes(db, claves):
    return {
        fila["mes"]: fila["total"]
        for fila in db.execute(
            """SELECT substr(o.fecha_ingreso, 1, 7) AS mes, COUNT(*) AS total FROM ordenes o
               WHERE substr(o.fecha_ingreso, 1, 7) BETWEEN ? AND ?
                 AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
               GROUP BY mes""",
            claves,
        )
    }


def _ordenes_completadas_por_mes(db, claves):
    return {
        fila["mes"]: fila
        for fila in db.execute(
            """SELECT substr(o.fecha_completado, 1, 7) AS mes,
                      COUNT(DISTINCT o.id) AS completadas,
                      COALESCE(SUM(i.subtotal_centavos), 0) AS valor_completado,
                      COALESCE(SUM(CASE WHEN i.tipo = 'Servicio' THEN i.subtotal_centavos END), 0) AS mano_obra,
                      COALESCE(SUM(i.costo_total_centavos), 0) AS costo,
                      COUNT(CASE WHEN i.tipo = 'Repuesto' AND i.subtotal_centavos > 0
                                      AND i.costo_total_centavos IS NULL THEN 1 END) AS sin_costo
               FROM ordenes o LEFT JOIN orden_items i ON i.orden_id = o.id AND i.vigente = 1
               WHERE o.estado = 'Completado' AND substr(o.fecha_completado, 1, 7) BETWEEN ? AND ?
                 AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
               GROUP BY mes""",
            claves,
        )
    }


def _cobros_por_mes(db, claves):
    return {
        fila["mes"]: fila["cobrado"]
        for fila in db.execute(
            """SELECT substr(p.fecha, 1, 7) AS mes,
                      COALESCE(SUM(CASE
                        WHEN p.tipo = 'cobro' THEN p.importe_centavos
                        WHEN p.tipo = 'reversion' AND original.tipo = 'cobro' THEN -p.importe_centavos
                        ELSE 0 END), 0) AS cobrado
               FROM orden_pagos p
               LEFT JOIN orden_pagos original ON original.id = p.revierte_id
               JOIN ordenes o ON o.id = p.orden_id
               WHERE substr(p.fecha, 1, 7) BETWEEN ? AND ?
                 AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
               GROUP BY mes""",
            claves,
        )
    }


def _gastos_por_mes(db, claves):
    return {
        fila["mes"]: fila["total"]
        for fila in db.execute(
            """SELECT mes, SUM(importe_centavos) AS total FROM (
                   SELECT substr(fecha, 1, 7) AS mes, importe_centavos FROM gastos
                   WHERE substr(fecha, 1, 7) BETWEEN ? AND ?
                   UNION ALL
                   SELECT substr(r.fecha, 1, 7) AS mes, -g.importe_centavos
                   FROM gasto_reversiones r JOIN gastos g ON g.id = r.gasto_id
                   WHERE substr(r.fecha, 1, 7) BETWEEN ? AND ?
               ) GROUP BY mes""",
            (*claves, *claves),
        )
    }


def _fila_taller(clave, etiqueta, ingresadas, completadas, cobros, gastos):
    datos = completadas.get(clave)
    fila = {
        "etiqueta": etiqueta,
        "ingresadas": ingresadas.get(clave, 0),
        "completadas": datos["completadas"] if datos else 0,
        "cobrado": cobros.get(clave, 0),
        "valor_completado": datos["valor_completado"] if datos else 0,
        "mano_obra": datos["mano_obra"] if datos else 0,
        "costo": datos["costo"] if datos else 0,
        "sin_costo": datos["sin_costo"] if datos else 0,
        "gastos": gastos.get(clave, 0),
    }
    fila["ganancia"] = fila["valor_completado"] - fila["costo"]
    fila["resultado_neto"] = fila["ganancia"] - fila["gastos"]
    return fila


def taller_por_mes(db, meses):
    """Distingue cobros por fecha de caja y trabajo/costo por fecha de cierre."""
    claves = (meses[0][0], meses[-1][0])
    ingresadas = _ordenes_ingresadas_por_mes(db, claves)
    completadas = _ordenes_completadas_por_mes(db, claves)
    cobros = _cobros_por_mes(db, claves)
    gastos = _gastos_por_mes(db, claves)
    return [
        _fila_taller(clave, etiqueta, ingresadas, completadas, cobros, gastos)
        for clave, etiqueta in meses
    ]


def sumar_filas(filas):
    campos = (
        "ingresadas", "completadas", "cobrado", "valor_completado",
        "mano_obra", "costo", "sin_costo", "ganancia", "gastos", "resultado_neto",
    )
    return {campo: sum(fila[campo] for fila in filas) for campo in campos}


def cobros_por_medio(db, meses, medios_pago):
    """Agrupa cobros y reversiones del trimestre por medio de pago."""
    filas = db.execute(
        """SELECT p.medio,
                  SUM(CASE WHEN p.tipo = 'cobro' THEN p.importe_centavos ELSE 0 END) AS cobros,
                  SUM(CASE WHEN p.tipo = 'reversion' AND original.tipo = 'cobro'
                           THEN p.importe_centavos ELSE 0 END) AS reversiones
           FROM orden_pagos p
           LEFT JOIN orden_pagos original ON original.id = p.revierte_id
           JOIN ordenes o ON o.id = p.orden_id
           WHERE (p.tipo = 'cobro' OR (p.tipo = 'reversion' AND original.tipo = 'cobro'))
             AND substr(p.fecha, 1, 7) BETWEEN ? AND ?
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
           GROUP BY p.medio""",
        (meses[0][0], meses[-1][0]),
    ).fetchall()
    por_medio = {fila["medio"]: fila for fila in filas}
    etiquetas = list(medios_pago)
    etiquetas.extend(sorted(set(por_medio) - set(etiquetas)))
    return {
        "etiquetas": etiquetas,
        "cobros": [por_medio[medio]["cobros"] if medio in por_medio else 0 for medio in etiquetas],
        "reversiones": [por_medio[medio]["reversiones"] if medio in por_medio else 0 for medio in etiquetas],
    }


def _consultar_actividad_equipo(db, meses, acciones_produccion):
    marcadores = ", ".join("?" for _ in acciones_produccion)
    condicion = f"""a.accion IN ({marcadores}) AND a.usuario_id IS NOT NULL
                    AND substr(a.momento, 1, 7) BETWEEN ? AND ?"""
    parametros = (*acciones_produccion, meses[0][0], meses[-1][0])
    metricas = """SUM(a.accion = 'Creó la orden') AS creadas,
                  COUNT(DISTINCT a.orden_id) AS gestionadas,
                  SUM(a.accion = 'Cambió el estado') AS cambios,
                  SUM(a.accion = 'Registró un cliente') AS clientes"""
    por_mes = {
        (fila["usuario_id"], fila["mes"]): fila
        for fila in db.execute(
            f"""SELECT a.usuario_id, substr(a.momento, 1, 7) AS mes, {metricas}
                FROM actividad a WHERE {condicion} GROUP BY a.usuario_id, mes""",
            parametros,
        )
    }
    totales = {
        fila["usuario_id"]: fila
        for fila in db.execute(
            f"SELECT a.usuario_id, {metricas} FROM actividad a WHERE {condicion} GROUP BY a.usuario_id",
            parametros,
        )
    }
    return por_mes, totales


def _persona_equipo(usuario, meses, por_mes, totales):
    total = totales.get(usuario["id"])
    if not usuario["activo"] and total is None:
        return None
    mensual = {
        metrica: [
            (por_mes[(usuario["id"], clave)][metrica] or 0)
            if (usuario["id"], clave) in por_mes else 0
            for clave, _ in meses
        ]
        for metrica in METRICAS_EQUIPO
    }
    return {
        "id": usuario["id"],
        "nombre": usuario["nombre"],
        "rol": usuario["rol"],
        "meses": mensual["gestionadas"],
        "mensual": mensual,
        "creadas": total["creadas"] if total else 0,
        "gestionadas": total["gestionadas"] if total else 0,
        "cambios": total["cambios"] if total else 0,
        "clientes": total["clientes"] if total else 0,
    }


def _mejores_por_mes(personas, cantidad_meses):
    mejores = []
    for indice in range(cantidad_meses):
        maximo = max((persona["meses"][indice] for persona in personas), default=0)
        mejores.append([
            persona["nombre"] for persona in personas
            if maximo and persona["meses"][indice] == maximo
        ])
    return mejores


def equipo_por_mes(db, meses, acciones_produccion):
    """Resume actividad productiva por persona y mes."""
    por_mes, totales = _consultar_actividad_equipo(db, meses, acciones_produccion)
    personas = [
        persona
        for usuario in db.execute("SELECT id, nombre, rol, activo FROM usuarios ORDER BY nombre COLLATE NOCASE")
        if (persona := _persona_equipo(usuario, meses, por_mes, totales)) is not None
    ]
    personas.sort(key=lambda persona: (-persona["gestionadas"], -persona["creadas"], persona["nombre"].lower()))
    for posicion, persona in enumerate(personas):
        persona["color"] = COLORES_GRAFICO[posicion % len(COLORES_GRAFICO)]
    return personas, _mejores_por_mes(personas, len(meses))


def _variaciones_reporte(total, anterior):
    return {
        campo: variacion(total[campo], anterior[campo])
        for campo in ("ingresadas", "completadas", "cobrado", "ganancia", "resultado_neto")
    }


def _graficos_reporte(db, meses, taller, personas, medios_pago):
    return {
        "meses": [etiqueta.split()[0] for _, etiqueta in meses],
        "medios": cobros_por_medio(db, meses, medios_pago),
        "personas": [
            {
                "nombre": persona["nombre"],
                "color": persona["color"],
                "mensual": persona["mensual"],
                "totales": {metrica: persona[metrica] for metrica in METRICAS_EQUIPO},
            }
            for persona in personas
        ],
        "taller": {
            campo: [fila[campo] for fila in taller]
            for campo in (
                "ingresadas", "completadas", "cobrado", "valor_completado",
                "mano_obra", "costo", "ganancia", "gastos", "resultado_neto",
            )
        },
    }


def datos_reporte(db, anio, trimestre, *, nombres_mes, medios_pago, acciones_produccion):
    """Compone los datos de pantalla e impresion para un trimestre."""
    meses = meses_del_trimestre(anio, trimestre, nombres_mes)
    taller = taller_por_mes(db, meses)
    total = sumar_filas(taller)
    anterior_meses = meses_del_trimestre(
        *trimestre_vecino(anio, trimestre, -1), nombres_mes
    )
    anterior = sumar_filas(taller_por_mes(db, anterior_meses))
    personas, mejores = equipo_por_mes(db, meses, acciones_produccion)
    con_trabajo = [persona for persona in personas if persona["gestionadas"]]
    trimestre_actual = leer_trimestre(None)
    return {
        "anio": anio,
        "trimestre": trimestre,
        "clave": f"{anio}-{trimestre}",
        "titulo": f"{trimestre}.º trimestre {anio}",
        "meses": [etiqueta for _, etiqueta in meses],
        "taller": taller,
        "total": total,
        "anterior": anterior,
        "variaciones": _variaciones_reporte(total, anterior),
        "personas": personas,
        "mejores": mejores,
        "destacado": con_trabajo[0] if con_trabajo else None,
        "graficos": _graficos_reporte(db, meses, taller, personas, medios_pago),
        "anterior_clave": "{}-{}".format(*trimestre_vecino(anio, trimestre, -1)),
        "siguiente_clave": "{}-{}".format(*trimestre_vecino(anio, trimestre, 1)),
        "actual": trimestre_actual,
        "es_actual": (anio, trimestre) == trimestre_actual,
        "es_futuro": (anio, trimestre) > trimestre_actual,
        "generado": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }
