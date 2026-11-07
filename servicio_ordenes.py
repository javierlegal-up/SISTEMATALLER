"""Reglas de negocio puras para órdenes de trabajo."""
from datetime import date


def precio_para_empleado(guardada, del_cliente):
    """Preserva precios existentes y marca los repuestos sin precio como pendientes."""
    if del_cliente:
        return 0, None, False
    if guardada is None or guardada["del_cliente"]:
        return 0, None, True
    return (
        guardada["precio_unitario_centavos"],
        guardada["costo_unitario_centavos"],
        bool(guardada["falta_precio"]),
    )


def fecha_completado_para(estado, anterior=None, hoy=None):
    """Conserva la fecha original al editar una orden que sigue completada."""
    if estado != "Completado":
        return None
    return anterior or (hoy or date.today()).isoformat()


def problema_cambio_estado(orden, estado, estados, es_dueno, numero_orden):
    """Devuelve el motivo por el que una transición de estado no está permitida."""
    if orden.get("anulada"):
        return "No se puede cambiar el estado de una orden anulada."
    if estado not in estados:
        return "Estado no válido."
    if not es_dueno and "Completado" in (estado, orden["estado"]):
        return "Solo el dueño puede completar una orden o reabrir una completada."
    if estado == "Completado" and orden["lineas_sin_precio"]:
        return f"Antes de completar {numero_orden(orden['id'])} falta poner precios: edita la orden."
    return None


def resumir_costos(items):
    """Suma costos internos y cuenta repuestos cobrados sin costo informado."""
    return {
        "costo": sum(item["costo_total_centavos"] or 0 for item in items),
        "sin_costo": sum(
            1
            for item in items
            if item["tipo"] == "Repuesto"
            and item["subtotal_centavos"] > 0
            and item["costo_total_centavos"] is None
        ),
    }
