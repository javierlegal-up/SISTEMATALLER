"""Reglas puras de períodos y series temporales para la analítica del ERP."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal


NOMBRES_MES = ("Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")
PERIODOS_ANALITICA = {"7d": 7, "30d": 30, "90d": 90, "12m": 365}


def variacion(actual, anterior):
    """Cambio porcentual contra el período anterior; None si antes no hubo nada."""
    if not anterior:
        return None
    return round((actual - anterior) * 100 / abs(anterior))


def rango_analitica(argumentos):
    """Valida rangos breves para análisis y crea una comparación de igual duración."""
    hoy = date.today()
    periodo = argumentos.get("periodo", "30d")
    if periodo == "personalizado":
        try:
            desde = date.fromisoformat(argumentos.get("desde", ""))
            hasta = date.fromisoformat(argumentos.get("hasta", ""))
            dias = (hasta - desde).days + 1
            if desde > hasta or hasta > hoy or dias > 366:
                raise ValueError("rango fuera de límites")
        except ValueError:
            periodo = "30d"
    if periodo != "personalizado":
        dias = PERIODOS_ANALITICA.get(periodo, 30)
        periodo = periodo if periodo in PERIODOS_ANALITICA else "30d"
        hasta = hoy
        desde = hoy - timedelta(days=dias - 1)
    anterior_hasta = desde - timedelta(days=1)
    anterior_desde = anterior_hasta - timedelta(days=dias - 1)
    return {
        "periodo": periodo,
        "desde": desde,
        "hasta": hasta,
        "dias": dias,
        "anterior_desde": anterior_desde,
        "anterior_hasta": anterior_hasta,
    }


def agrupar_serie_analitica(
    rango, por_trabajo, por_cobros, por_gastos, por_pagos, por_ingresos
):
    """Rellena períodos vacíos y no supera 14 marcas para mantener legible el gráfico."""
    desde, hasta = rango["desde"], rango["hasta"]
    dias = rango["dias"]
    buckets = []
    cursor = desde
    while cursor <= hasta:
        if dias <= 14:
            limite = cursor
        elif dias <= 120:
            limite = min(cursor + timedelta(days=6), hasta)
        else:
            limite = min(date(cursor.year, cursor.month, 1) + timedelta(days=31), hasta)
            while limite.month != cursor.month or limite.year != cursor.year:
                limite -= timedelta(days=1)
        if dias <= 14:
            etiqueta = cursor.strftime("%d/%m")
        elif dias <= 120:
            etiqueta = f"{cursor.day:02d}–{limite.day:02d}"
        else:
            etiqueta = NOMBRES_MES[cursor.month - 1]
        buckets.append({"desde": cursor, "hasta": limite, "etiqueta": etiqueta})
        cursor = limite + timedelta(days=1)

    series = {
        "trabajo": [], "mano_obra": [], "repuestos": [], "costo": [],
        "cobrado": [], "gastos": [], "pagos_proveedor": [], "flujo_caja": [],
        "ingresadas": [], "completadas": [], "margen": [], "resultado": [],
    }
    titulos = []
    for bucket in buckets:
        fechas = (
            bucket["desde"] + timedelta(days=offset)
            for offset in range((bucket["hasta"] - bucket["desde"]).days + 1)
        )
        total = {clave: 0 for clave in series}
        for dia in fechas:
            clave = dia.isoformat()
            trabajo = por_trabajo.get(clave, {})
            total["trabajo"] += trabajo.get("trabajo", 0)
            total["mano_obra"] += trabajo.get("mano_obra", 0)
            total["repuestos"] += trabajo.get("repuestos", 0)
            total["costo"] += trabajo.get("costo", 0)
            total["completadas"] += trabajo.get("completadas", 0)
            total["cobrado"] += por_cobros.get(clave, 0)
            total["gastos"] += por_gastos.get(clave, 0)
            total["pagos_proveedor"] += por_pagos.get(clave, 0)
            total["ingresadas"] += por_ingresos.get(clave, 0)
        total["flujo_caja"] = total["cobrado"] - total["gastos"] - total["pagos_proveedor"]
        total["margen"] = total["trabajo"] - total["costo"]
        total["resultado"] = total["margen"] - total["gastos"]
        for clave in series:
            series[clave].append(total[clave])
        titulos.append(
            f"{bucket['desde'].strftime('%d/%m/%Y')} – "
            f"{bucket['hasta'].strftime('%d/%m/%Y')}"
        )
    return {
        "etiquetas": [bucket["etiqueta"] for bucket in buckets],
        "titulos": titulos,
        "segmentos": [
            {"desde": bucket["desde"].isoformat(), "hasta": bucket["hasta"].isoformat()}
            for bucket in buckets
        ],
        **series,
    }


def resumen_analitica_periodo(db, desde, hasta):
    """Calcula producción y movimientos usando la fecha económica de cada uno."""
    trabajo = db.execute(
        """SELECT COUNT(DISTINCT o.id) AS ordenes,
                  COALESCE(SUM(i.subtotal_centavos), 0) AS valor,
                  COALESCE(SUM(CASE WHEN i.tipo = 'Servicio' THEN i.subtotal_centavos ELSE 0 END), 0) AS mano_obra,
                  COALESCE(SUM(CASE WHEN i.tipo = 'Repuesto' THEN i.subtotal_centavos ELSE 0 END), 0) AS repuestos,
                  COALESCE(SUM(i.costo_total_centavos), 0) AS costo,
                  COUNT(CASE WHEN i.tipo = 'Repuesto' AND i.subtotal_centavos > 0
                                  AND i.costo_total_centavos IS NULL THEN 1 END) AS sin_costo
           FROM ordenes o LEFT JOIN orden_items i ON i.orden_id = o.id AND i.vigente = 1
           WHERE o.estado = 'Completado' AND o.fecha_completado BETWEEN ? AND ?
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)""",
        (desde, hasta),
    ).fetchone()
    ingresadas = db.execute(
        """SELECT COUNT(*) AS total FROM ordenes o
           WHERE o.fecha_ingreso BETWEEN ? AND ?
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)""",
        (desde, hasta),
    ).fetchone()["total"]
    cobrado = db.execute(
        """SELECT COALESCE(SUM(CASE
                    WHEN p.tipo = 'cobro' THEN p.importe_centavos
                    WHEN p.tipo = 'reversion' AND original.tipo = 'cobro' THEN -p.importe_centavos
                    ELSE 0 END), 0) AS total
           FROM orden_pagos p LEFT JOIN orden_pagos original ON original.id = p.revierte_id
           JOIN ordenes o ON o.id = p.orden_id
           WHERE p.fecha BETWEEN ? AND ?
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)""",
        (desde, hasta),
    ).fetchone()["total"]
    gastos = gasto_neto_periodo(db, desde, hasta)
    pagos_proveedor = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN pp.tipo = 'reversion' THEN -pp.importe_centavos
                                    ELSE pp.importe_centavos END), 0) AS total
           FROM proveedor_pagos pp JOIN compras c ON c.id = pp.compra_id
           WHERE pp.fecha BETWEEN ? AND ? AND c.estado != 'Anulada'""",
        (desde, hasta),
    ).fetchone()["total"]
    valor = trabajo["valor"]
    margen = valor - trabajo["costo"]
    return {
        "ordenes_ingresadas": ingresadas,
        "ordenes_completadas": trabajo["ordenes"],
        "trabajo": valor,
        "mano_obra": trabajo["mano_obra"],
        "repuestos": trabajo["repuestos"],
        "costo_consumido": trabajo["costo"],
        "sin_costo": trabajo["sin_costo"],
        "cobrado": cobrado,
        "gastos": gastos,
        "pagos_proveedor": pagos_proveedor,
        "margen": margen,
        "resultado_estimado": margen - gastos,
        "flujo_caja": cobrado - gastos - pagos_proveedor,
        "ticket_promedio": int(
            (Decimal(valor) / Decimal(trabajo["ordenes"])).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        if trabajo["ordenes"] else 0,
        "margen_porcentaje": round(margen * 100 / valor, 1) if valor else None,
    }


def gasto_neto_periodo(db, desde, hasta):
    return db.execute(
        """SELECT COALESCE(SUM(importe), 0) AS total FROM (
               SELECT importe_centavos AS importe FROM gastos WHERE fecha BETWEEN ? AND ?
               UNION ALL
               SELECT -g.importe_centavos FROM gasto_reversiones r
               JOIN gastos g ON g.id = r.gasto_id WHERE r.fecha BETWEEN ? AND ?
           )""",
        (desde, hasta, desde, hasta),
    ).fetchone()["total"]


def serie_analitica(db, rango):
    """Arma puntos comparables para gráficos, agrupados por día, semana o mes."""
    desde, hasta = rango["desde"], rango["hasta"]
    inicio, fin = desde.isoformat(), hasta.isoformat()
    por_trabajo = {
        fila["fecha"]: dict(fila)
        for fila in db.execute(
            """SELECT o.fecha_completado AS fecha,
                      COALESCE(SUM(i.subtotal_centavos), 0) AS trabajo,
                      COALESCE(SUM(CASE WHEN i.tipo = 'Servicio' THEN i.subtotal_centavos ELSE 0 END), 0) AS mano_obra,
                      COALESCE(SUM(CASE WHEN i.tipo = 'Repuesto' THEN i.subtotal_centavos ELSE 0 END), 0) AS repuestos,
                      COALESCE(SUM(i.costo_total_centavos), 0) AS costo,
                      COUNT(DISTINCT o.id) AS completadas
               FROM ordenes o LEFT JOIN orden_items i ON i.orden_id = o.id AND i.vigente = 1
               WHERE o.estado = 'Completado' AND o.fecha_completado BETWEEN ? AND ?
                 AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
               GROUP BY o.fecha_completado""",
            (inicio, fin),
        )
    }
    por_cobros = {
        fila["fecha"]: fila["total"]
        for fila in db.execute(
            """SELECT p.fecha, SUM(CASE
                        WHEN p.tipo = 'cobro' THEN p.importe_centavos
                        WHEN p.tipo = 'reversion' AND original.tipo = 'cobro' THEN -p.importe_centavos
                        ELSE 0 END) AS total
               FROM orden_pagos p LEFT JOIN orden_pagos original ON original.id = p.revierte_id
               JOIN ordenes o ON o.id = p.orden_id
               WHERE p.fecha BETWEEN ? AND ?
                 AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
               GROUP BY p.fecha""",
            (inicio, fin),
        )
    }
    por_gastos = {
        fila["fecha"]: fila["total"]
        for fila in db.execute(
            """SELECT fecha, SUM(importe) AS total FROM (
                   SELECT fecha, importe_centavos AS importe FROM gastos WHERE fecha BETWEEN ? AND ?
                   UNION ALL
                   SELECT r.fecha, -g.importe_centavos FROM gasto_reversiones r
                   JOIN gastos g ON g.id = r.gasto_id WHERE r.fecha BETWEEN ? AND ?
               ) GROUP BY fecha""",
            (inicio, fin, inicio, fin),
        )
    }
    por_pagos_proveedor = {
        fila["fecha"]: fila["total"]
        for fila in db.execute(
            """SELECT pp.fecha, SUM(CASE WHEN pp.tipo = 'reversion' THEN -pp.importe_centavos
                                         ELSE pp.importe_centavos END) AS total
               FROM proveedor_pagos pp JOIN compras c ON c.id = pp.compra_id
               WHERE pp.fecha BETWEEN ? AND ? AND c.estado != 'Anulada'
               GROUP BY pp.fecha""",
            (inicio, fin),
        )
    }
    por_ingresos = {
        fila["fecha"]: fila["total"]
        for fila in db.execute(
            """SELECT o.fecha_ingreso AS fecha, COUNT(*) AS total FROM ordenes o
               WHERE o.fecha_ingreso BETWEEN ? AND ?
                 AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
               GROUP BY o.fecha_ingreso""",
            (inicio, fin),
        )
    }
    return agrupar_serie_analitica(
        rango, por_trabajo, por_cobros, por_gastos, por_pagos_proveedor, por_ingresos
    )


def estado_analitica_actual(db, *, select_orden, importe_linea_compra_centavos, dias_atraso):
    """Calcula saldos corrientes, antigüedad de cuentas y valuación del stock activo."""
    ordenes = db.execute(
        select_orden + " WHERE o.estado = 'Completado' AND o.cobro_sin_conciliar = 0"
        + " AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)"
    ).fetchall()
    cuentas = {"por_cobrar": 0, "vencido_30": 0, "vencido_60": 0, "vencido_90": 0}
    hoy = date.today()
    for orden in ordenes:
        saldo = max(orden["total_repuestos"] + orden["total_mano_obra"] - orden["pagado_centavos"], 0)
        if saldo:
            cuentas["por_cobrar"] += saldo
            antiguedad = max((hoy - date.fromisoformat(orden["fecha_completado"])).days, 0)
            if antiguedad > 30:
                cuentas["vencido_30"] += saldo
            if antiguedad > 60:
                cuentas["vencido_60"] += saldo
            if antiguedad > 90:
                cuentas["vencido_90"] += saldo
    cuentas["historicas_sin_conciliar"] = db.execute(
        """SELECT COUNT(*) FROM ordenes o WHERE o.estado = 'Completado' AND o.cobro_sin_conciliar = 1
           AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)"""
    ).fetchone()[0]

    pagado_por_compra = {
        fila["compra_id"]: fila["total"]
        for fila in db.execute(
            """SELECT compra_id, SUM(CASE WHEN tipo = 'reversion' THEN -importe_centavos
                                           ELSE importe_centavos END) AS total
               FROM proveedor_pagos GROUP BY compra_id"""
        )
    }
    recibidos_por_compra = {}
    for linea in db.execute(
        """SELECT ci.compra_id, ci.costo_unitario_centavos,
                  COALESCE((SELECT SUM(ri.cantidad_milesimas + COALESCE(rev.cantidad_milesimas, 0))
                            FROM compra_recepcion_items ri
                            JOIN stock_movimientos original ON original.recepcion_item_id = ri.id
                            LEFT JOIN stock_movimientos rev ON rev.revierte_movimiento_id = original.id
                            WHERE ri.compra_item_id = ci.id), 0) AS recibidas,
                  c.estado
           FROM compra_items ci JOIN compras c ON c.id = ci.compra_id"""
    ):
        if linea["estado"] == "Anulada":
            continue
        recibidos_por_compra.setdefault(linea["compra_id"], 0)
        recibidos_por_compra[linea["compra_id"]] += importe_linea_compra_centavos(
            linea["recibidas"], linea["costo_unitario_centavos"]
        )
    cuentas["por_pagar"] = sum(
        max(total - pagado_por_compra.get(compra_id, 0), 0)
        for compra_id, total in recibidos_por_compra.items()
    )

    articulos = db.execute(
        """SELECT r.id, r.nombre, r.sku, r.unidad, r.stock_minimo_milesimas,
                  COALESCE(SUM(m.cantidad_milesimas), 0) AS stock_milesimas,
                  CASE WHEN COALESCE(SUM(m.cantidad_milesimas), 0) > 0
                       THEN CAST(ROUND(CAST(SUM(m.cantidad_milesimas * COALESCE(m.costo_unitario_centavos, 0)) AS REAL)
                                      / SUM(m.cantidad_milesimas)) AS INTEGER)
                       ELSE COALESCE(r.costo_centavos, 0) END AS costo_promedio
           FROM repuestos_servicios r LEFT JOIN stock_movimientos m ON m.articulo_id = r.id
           WHERE r.tipo = 'Repuesto' AND r.controla_stock = 1 AND r.activo = 1
           GROUP BY r.id ORDER BY r.nombre COLLATE NOCASE"""
    ).fetchall()
    stock_bajo = []
    valor_stock = 0
    for fila in articulos:
        cantidad = fila["stock_milesimas"]
        valor = int((Decimal(cantidad) * Decimal(fila["costo_promedio"]) / 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        valor_stock += valor
        minimo = fila["stock_minimo_milesimas"]
        if cantidad <= 0 or (minimo > 0 and cantidad <= minimo):
            stock_bajo.append({
                **dict(fila), "valor_stock": valor,
                "sin_stock": cantidad <= 0,
                "cantidad": Decimal(cantidad) / 1000,
                "minimo": Decimal(minimo) / 1000,
            })
    stock_bajo.sort(key=lambda articulo: (not articulo["sin_stock"], articulo["stock_milesimas"], articulo["nombre"].casefold()))
    estados = {fila["estado"]: fila["total"] for fila in db.execute(
        """SELECT o.estado, COUNT(*) AS total FROM ordenes o WHERE NOT EXISTS
           (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id) GROUP BY o.estado"""
    )}
    atrasadas = db.execute(
        """SELECT COUNT(*) FROM ordenes o WHERE o.estado != 'Completado' AND o.fecha_ingreso < ?
           AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)""",
        ((hoy - timedelta(days=dias_atraso)).isoformat(),),
    ).fetchone()[0]
    return {
        "cuentas": cuentas,
        "stock": {
            "articulos": len(articulos),
            "valor_centavos": valor_stock,
            "sin_stock": sum(1 for articulo in stock_bajo if articulo["sin_stock"]),
            "bajo_minimo": len(stock_bajo),
            "alertas": stock_bajo[:8],
        },
        "ordenes": {
            "pendientes": estados.get("Pendiente", 0),
            "en_proceso": estados.get("En Proceso", 0),
            "atrasadas": atrasadas,
        },
    }


def datos_analitica(db, rango, *, select_orden, importe_linea_compra_centavos, dias_atraso):
    """Construye el resumen completo de analítica desde una conexión explícita."""
    periodo = resumen_analitica_periodo(db, rango["desde"].isoformat(), rango["hasta"].isoformat())
    anterior = resumen_analitica_periodo(
        db, rango["anterior_desde"].isoformat(), rango["anterior_hasta"].isoformat()
    )
    variaciones = {
        campo: variacion(periodo[campo], anterior[campo])
        for campo in (
            "trabajo", "cobrado", "costo_consumido", "gastos",
            "resultado_estimado", "flujo_caja", "ordenes_completadas",
        )
    }
    serie = serie_analitica(db, rango)
    desde, hasta = rango["desde"].isoformat(), rango["hasta"].isoformat()
    medios = db.execute(
        """SELECT p.medio,
                  SUM(CASE WHEN p.tipo = 'cobro' THEN p.importe_centavos
                           WHEN p.tipo = 'reversion' AND original.tipo = 'cobro' THEN -p.importe_centavos
                           ELSE 0 END) AS neto
           FROM orden_pagos p LEFT JOIN orden_pagos original ON original.id = p.revierte_id
           JOIN ordenes o ON o.id = p.orden_id
           WHERE p.fecha BETWEEN ? AND ?
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
           GROUP BY p.medio HAVING neto != 0 ORDER BY neto DESC""",
        (desde, hasta),
    ).fetchall()
    rubros = db.execute(
        """SELECT categoria, SUM(importe) AS total FROM (
               SELECT categoria, importe_centavos AS importe FROM gastos WHERE fecha BETWEEN ? AND ?
               UNION ALL
               SELECT g.categoria, -g.importe_centavos FROM gasto_reversiones r
               JOIN gastos g ON g.id = r.gasto_id WHERE r.fecha BETWEEN ? AND ?
           ) GROUP BY categoria HAVING total != 0 ORDER BY total DESC""",
        (desde, hasta, desde, hasta),
    ).fetchall()
    trabajos = db.execute(
        """SELECT i.tipo, i.descripcion, SUM(i.subtotal_centavos) AS importe,
                  SUM(i.cantidad) AS cantidad, COUNT(DISTINCT o.id) AS ordenes
           FROM ordenes o JOIN orden_items i ON i.orden_id = o.id AND i.vigente = 1
           WHERE o.estado = 'Completado' AND o.fecha_completado BETWEEN ? AND ?
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
           GROUP BY i.tipo, i.descripcion ORDER BY importe DESC, i.descripcion COLLATE NOCASE LIMIT 8""",
        (desde, hasta),
    ).fetchall()
    estado = estado_analitica_actual(
        db,
        select_orden=select_orden,
        importe_linea_compra_centavos=importe_linea_compra_centavos,
        dias_atraso=dias_atraso,
    )
    fechas = {
        "desde": desde, "hasta": hasta,
        "anterior_desde": rango["anterior_desde"].isoformat(),
        "anterior_hasta": rango["anterior_hasta"].isoformat(),
    }
    return {
        "periodo": rango["periodo"],
        "dias": rango["dias"],
        "fechas": fechas,
        "actual": periodo,
        "anterior": anterior,
        "variaciones": variaciones,
        "estado": estado,
        "serie": serie,
        "medios": [{"nombre": fila["medio"], "valor": fila["neto"]} for fila in medios],
        "rubros": [{"nombre": fila["categoria"], "valor": fila["total"]} for fila in rubros],
        "trabajos": [dict(fila) for fila in trabajos],
        "generado": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }
