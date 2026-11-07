"""Reglas de acceso, credenciales y protección contra intentos de inicio."""
import re
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash


NOMBRES_ROL = {"dueno": "Dueño (administrador)", "empleado": "Empleado"}
LARGO_MINIMO_CLAVE = 12
MAXIMO_INTENTOS_LOGIN = 5
MAXIMO_INTENTOS_IP = 20
VENTANA_INTENTOS_LOGIN = timedelta(minutes=15)
DURACION_BLOQUEO_LOGIN = timedelta(minutes=15)

ACCION_POR_VISTA = {
    "restaurar": "restaurar_datos",
    "respaldo_ahora": "crear_respaldo",
    "respaldo_excel": "exportar_datos",
    "usuarios": "administrar_usuarios",
    "usuario_cambiar_clave": "administrar_usuarios",
    "usuario_activar": "administrar_usuarios",
    "actividad": "ver_actividad",
    "reportes": "ver_finanzas",
    "reporte_imprimir": "ver_finanzas",
    "analitica": "ver_finanzas",
    "analitica_detalle": "ver_finanzas",
    "caja": "ver_finanzas",
    "caja_exportar_csv": "exportar_datos",
    "caja_cerrar": "cerrar_caja",
    "caja_reabrir": "reabrir_caja",
    "cuentas_cobrar": "ver_finanzas",
    "gastos": "registrar_gasto",
    "gasto_revertir": "revertir_gasto",
    "cliente_eliminar": "eliminar_cliente",
    "vehiculo_eliminar": "eliminar_vehiculo",
    "orden_pago_nuevo": "cobrar_orden",
    "orden_pago_revertir": "revertir_cobro",
    "orden_cobros_conciliar": "conciliar_cobros",
    "orden_anular": "anular_orden",
    "orden_eliminar": "eliminar_orden",
    "orden_imprimir": "imprimir_documento",
    "proveedores": "gestionar_proveedores",
    "proveedor_detalle": "ver_finanzas",
    "proveedor_cambiar_estado": "gestionar_proveedores",
    "compras": "gestionar_compras",
    "cuentas_pagar": "ver_finanzas",
    "compra_nueva": "gestionar_compras",
    "compra_detalle": "ver_finanzas",
    "proveedor_pago_nuevo": "pagar_proveedor",
    "proveedor_pago_revertir": "revertir_pago_proveedor",
    "compra_recibir": "recibir_compra",
    "compra_anular": "anular_compra",
    "inventario": "ver_inventario",
    "inventario_ajustar": "ajustar_stock",
    "inventario_revertir": "revertir_stock",
    "orden_item_consumir": "consumir_repuesto",
    "catalogo": "gestionar_catalogo",
    "catalogo_eliminar": "gestionar_catalogo",
    "catalogo_reactivar": "gestionar_catalogo",
}
ACCIONES_ERP = frozenset(ACCION_POR_VISTA.values()) | frozenset({
    "administrar_sistema", "ver_finanzas", "ver_operacion", "ver_anuladas",
    "crear_cliente", "editar_cliente", "crear_vehiculo", "editar_vehiculo",
    "crear_orden", "editar_orden", "cambiar_estado_orden", "completar_orden",
})
PERMISOS_POR_ROL = {
    "dueno": ACCIONES_ERP,
    "empleado": frozenset({
        "ver_operacion", "crear_cliente", "editar_cliente", "crear_vehiculo",
        "editar_vehiculo", "crear_orden", "editar_orden", "cambiar_estado_orden",
    }),
}


def tiene_permiso(accion, usuario):
    """Evalúa una acción para un registro de usuario sin depender de Flask."""
    return usuario is not None and accion in PERMISOS_POR_ROL.get(usuario["rol"], frozenset())


def crear_usuario(conexion, usuario, nombre, clave, rol):
    """Inserta un usuario almacenando únicamente el hash de su contraseña."""
    return conexion.execute(
        """INSERT INTO usuarios
           (usuario, nombre, clave_hash, rol, clave_cambiada_en)
           VALUES (?, ?, ?, ?, ?)""",
        (usuario, nombre, generate_password_hash(clave), rol,
         datetime.now().isoformat(timespec="seconds")),
    ).lastrowid


def leer_datos_usuario(form, pedir_usuario=True):
    """Valida los datos de alta o cambio de contraseña de una cuenta."""
    datos = {
        "nombre": form.get("nombre", "").strip(),
        "usuario": form.get("usuario", "").strip(),
        "rol": form.get("rol", "empleado"),
    }
    clave = form.get("clave", "")
    errores = []
    if pedir_usuario:
        if not datos["nombre"]:
            errores.append("Escribe el nombre de la persona.")
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,30}", datos["usuario"]):
            errores.append("El usuario debe tener de 3 a 30 letras o números, sin espacios.")
        if datos["rol"] not in NOMBRES_ROL:
            errores.append("Elige si es dueño o empleado.")
    if len(clave) < LARGO_MINIMO_CLAVE:
        errores.append(f"La contraseña debe tener al menos {LARGO_MINIMO_CLAVE} caracteres.")
    elif clave != form.get("clave2", ""):
        errores.append("Las dos contraseñas no coinciden.")
    return datos, clave, errores


def parsear_momento(texto):
    try:
        return datetime.fromisoformat(texto) if texto else None
    except (TypeError, ValueError):
        return None


def buckets_login(usuario_escrito, direccion_ip):
    usuario = usuario_escrito.strip().casefold()[:80] or "(vacio)"
    ip = (direccion_ip or "desconocida").strip()[:80]
    return ((f"usuario:{usuario}", MAXIMO_INTENTOS_LOGIN), (f"ip:{ip}", MAXIMO_INTENTOS_IP))


def login_bloqueado(db, buckets, ahora):
    for bucket, _limite in buckets:
        fila = db.execute(
            "SELECT bloqueado_hasta FROM intentos_login WHERE bucket = ?", (bucket,)
        ).fetchone()
        bloqueado_hasta = parsear_momento(fila["bloqueado_hasta"]) if fila else None
        if bloqueado_hasta is not None and bloqueado_hasta > ahora:
            return True
    return False


def registrar_fallo_login(db, buckets, ahora):
    """Suma fallos por cuenta y origen y devuelve si se aplicó un bloqueo temporal."""
    bloqueado = False
    with db:
        for bucket, limite in buckets:
            fila = db.execute(
                "SELECT fallos, ventana_desde, bloqueado_hasta FROM intentos_login WHERE bucket = ?",
                (bucket,),
            ).fetchone()
            ventana = parsear_momento(fila["ventana_desde"]) if fila else None
            bloqueo_existente = parsear_momento(fila["bloqueado_hasta"]) if fila else None
            if bloqueo_existente is not None and bloqueo_existente > ahora:
                bloqueado = True
                continue
            if ventana is None or ahora - ventana >= VENTANA_INTENTOS_LOGIN:
                ventana, fallos = ahora, 1
            else:
                fallos = fila["fallos"] + 1
            hasta = ahora + DURACION_BLOQUEO_LOGIN if fallos >= limite else None
            bloqueado = bloqueado or hasta is not None
            db.execute(
                """INSERT INTO intentos_login (bucket, fallos, ventana_desde, bloqueado_hasta)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(bucket) DO UPDATE SET fallos = excluded.fallos,
                     ventana_desde = excluded.ventana_desde,
                     bloqueado_hasta = excluded.bloqueado_hasta""",
                (bucket, fallos, ventana.isoformat(timespec="seconds"),
                 hasta.isoformat(timespec="seconds") if hasta else None),
            )
        db.execute(
            "DELETE FROM intentos_login WHERE ventana_desde < ? AND bloqueado_hasta IS NULL",
            ((ahora - timedelta(days=2)).isoformat(timespec="seconds"),),
        )
    return bloqueado
