"""Consultas y reglas de stock, recepción de compras y saldos de proveedor."""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP


def importe_linea_compra_centavos(cantidad_milesimas, costo_unitario_centavos):
    """Redondea a centavos cada renglón con criterio comercial half-up."""
    producto = int(cantidad_milesimas) * int(costo_unitario_centavos)
    return (producto + 500) // 1000


def stock_articulo(db, articulo_id):
    return db.execute(
        "SELECT COALESCE(SUM(cantidad_milesimas), 0) AS cantidad FROM stock_movimientos WHERE articulo_id = ?",
        (articulo_id,),
    ).fetchone()["cantidad"]


def costo_promedio_articulo(db, articulo_id):
    fila = db.execute(
        """SELECT SUM(m.cantidad_milesimas) AS cantidad,
                  SUM(m.cantidad_milesimas * COALESCE(m.costo_unitario_centavos, 0)) AS valor
           FROM stock_movimientos m WHERE m.articulo_id = ?""",
        (articulo_id,),
    ).fetchone()
    if fila["cantidad"] and fila["cantidad"] > 0:
        return int(
            (Decimal(fila["valor"]) / Decimal(fila["cantidad"])).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    precio = db.execute(
        "SELECT costo_centavos FROM repuestos_servicios WHERE id = ?", (articulo_id,)
    ).fetchone()
    return (precio["costo_centavos"] or 0) if precio else 0


def insertar_movimiento_stock(
    db, articulo_id, tipo, cantidad_milesimas, costo_centavos=None, motivo="",
    orden_item_id=None, recepcion_item_id=None, revierte_id=None,
    usuario_id=None, creado_en=None,
):
    if cantidad_milesimas == 0:
        raise ValueError("El movimiento debe cambiar la existencia.")
    cursor = db.execute(
        """INSERT INTO stock_movimientos
           (articulo_id, tipo, cantidad_milesimas, costo_unitario_centavos,
            recepcion_item_id, orden_item_id, revierte_movimiento_id, motivo, usuario_id, creado_en)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            articulo_id, tipo, cantidad_milesimas, costo_centavos, recepcion_item_id,
            orden_item_id, revierte_id, motivo, usuario_id,
            creado_en or datetime.now().isoformat(timespec="seconds"),
        ),
    )
    return cursor.lastrowid


def cantidades_recibidas(db, compra_item_id):
    fila = db.execute(
        """SELECT COALESCE(SUM(ri.cantidad_milesimas + COALESCE(rev.cantidad_milesimas, 0)), 0) AS cantidad
           FROM compra_recepcion_items ri
           JOIN stock_movimientos original ON original.recepcion_item_id = ri.id
           LEFT JOIN stock_movimientos rev ON rev.revierte_movimiento_id = original.id
           WHERE ri.compra_item_id = ?""",
        (compra_item_id,),
    ).fetchone()
    return fila["cantidad"]


def actualizar_estado_compra(db, compra_id):
    lineas = db.execute(
        "SELECT id, cantidad_milesimas FROM compra_items WHERE compra_id = ?", (compra_id,)
    ).fetchall()
    if not lineas:
        estado = "Borrador"
    else:
        total = sum(linea["cantidad_milesimas"] for linea in lineas)
        recibida = sum(cantidades_recibidas(db, linea["id"]) for linea in lineas)
        estado = "Borrador" if not recibida else "Recibida" if recibida >= total else "Parcial"
    db.execute("UPDATE compras SET estado = ? WHERE id = ? AND estado != 'Anulada'", (estado, compra_id))
    return estado


def articulos_de_compra(db, compra_id):
    filas = db.execute(
        """SELECT ci.*, r.nombre AS articulo, r.sku, r.unidad,
                  COALESCE((SELECT SUM(ri.cantidad_milesimas + COALESCE(rev.cantidad_milesimas, 0))
                            FROM compra_recepcion_items ri
                            JOIN stock_movimientos mov ON mov.recepcion_item_id = ri.id
                            LEFT JOIN stock_movimientos rev ON rev.revierte_movimiento_id = mov.id
                            WHERE ri.compra_item_id = ci.id), 0) AS recibida_milesimas
           FROM compra_items ci JOIN repuestos_servicios r ON r.id = ci.articulo_id
           WHERE ci.compra_id = ? ORDER BY ci.id""",
        (compra_id,),
    ).fetchall()
    return [
        {**dict(fila), "pendiente_milesimas": max(fila["cantidad_milesimas"] - fila["recibida_milesimas"], 0)}
        for fila in filas
    ]


def importes_pedidos_compra(db, compra_ids):
    """Devuelve importes pedidos por compra, sumando líneas redondeadas."""
    ids = list(dict.fromkeys(int(compra_id) for compra_id in compra_ids))
    importes = dict.fromkeys(ids, 0)
    for inicio in range(0, len(ids), 500):
        grupo = ids[inicio:inicio + 500]
        marcadores = ",".join("?" for _ in grupo)
        filas = db.execute(
            f"SELECT compra_id, cantidad_milesimas, costo_unitario_centavos "
            f"FROM compra_items WHERE compra_id IN ({marcadores})",
            grupo,
        ).fetchall()
        for fila in filas:
            importes[fila["compra_id"]] += importe_linea_compra_centavos(
                fila["cantidad_milesimas"], fila["costo_unitario_centavos"]
            )
    return importes


def cuenta_compra(db, compra_id):
    lineas = db.execute(
        """SELECT ci.costo_unitario_centavos,
                  COALESCE((SELECT SUM(ri.cantidad_milesimas + COALESCE(rev.cantidad_milesimas, 0))
                            FROM compra_recepcion_items ri
                            JOIN stock_movimientos original ON original.recepcion_item_id = ri.id
                            LEFT JOIN stock_movimientos rev ON rev.revierte_movimiento_id = original.id
                            WHERE ri.compra_item_id = ci.id), 0) AS recibida_milesimas
           FROM compra_items ci WHERE ci.compra_id = ?""",
        (compra_id,),
    ).fetchall()
    total = sum(
        importe_linea_compra_centavos(linea["recibida_milesimas"], linea["costo_unitario_centavos"])
        for linea in lineas
    )
    pagos = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN tipo = 'reversion' THEN -importe_centavos
                                    ELSE importe_centavos END), 0) AS total
           FROM proveedor_pagos WHERE compra_id = ?""",
        (compra_id,),
    ).fetchone()["total"]
    return {"total": total, "pagado": pagos, "saldo": max(total - pagos, 0)}
