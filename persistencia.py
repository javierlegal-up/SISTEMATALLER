"""Esquema SQLite y migraciones versionadas del sistema de taller."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

ULTIMA_VERSION_ESQUEMA = 7

ESQUEMA = """
CREATE TABLE IF NOT EXISTS clientes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre     TEXT NOT NULL,
    documento  TEXT NOT NULL DEFAULT '',
    telefono   TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    direccion  TEXT NOT NULL DEFAULT '',
    creado_en  TEXT NOT NULL DEFAULT (date('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS vehiculos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    patente    TEXT NOT NULL UNIQUE,
    marca      TEXT NOT NULL,
    modelo     TEXT NOT NULL,
    anio       INTEGER,
    color      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS repuestos_servicios (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo            TEXT NOT NULL CHECK (tipo IN ('Repuesto', 'Servicio')),
    nombre          TEXT NOT NULL UNIQUE,
    precio_centavos INTEGER NOT NULL DEFAULT 0 CHECK (precio_centavos >= 0),
    costo_centavos  INTEGER CHECK (costo_centavos >= 0)
);

CREATE TABLE IF NOT EXISTS ordenes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    vehiculo_id      INTEGER NOT NULL REFERENCES vehiculos(id) ON DELETE RESTRICT,
    fallas           TEXT NOT NULL,
    diagnostico      TEXT NOT NULL DEFAULT '',
    notas            TEXT NOT NULL DEFAULT '',
    estado           TEXT NOT NULL DEFAULT 'Pendiente'
                     CHECK (estado IN ('Pendiente', 'En Proceso', 'Completado')),
    kilometraje      INTEGER,
    fecha_ingreso    TEXT NOT NULL,
    fecha_completado TEXT
);

CREATE TABLE IF NOT EXISTS orden_items (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    orden_id                 INTEGER NOT NULL REFERENCES ordenes(id) ON DELETE CASCADE,
    tipo                     TEXT NOT NULL CHECK (tipo IN ('Repuesto', 'Servicio')),
    descripcion              TEXT NOT NULL,
    cantidad                 REAL NOT NULL CHECK (cantidad > 0),
    precio_unitario_centavos INTEGER NOT NULL CHECK (precio_unitario_centavos >= 0),
    subtotal_centavos        INTEGER NOT NULL CHECK (subtotal_centavos >= 0),
    costo_unitario_centavos  INTEGER CHECK (costo_unitario_centavos >= 0),
    costo_total_centavos     INTEGER CHECK (costo_total_centavos >= 0),
    del_cliente              INTEGER NOT NULL DEFAULT 0,  -- 1 = repuesto que trajo el cliente
    falta_precio             INTEGER NOT NULL DEFAULT 0   -- 1 = anotado por un empleado, sin precio aún
);

CREATE TABLE IF NOT EXISTS usuarios (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    nombre     TEXT NOT NULL,
    clave_hash TEXT NOT NULL,
    rol        TEXT NOT NULL CHECK (rol IN ('dueno', 'empleado')),
    activo     INTEGER NOT NULL DEFAULT 1,  -- Los usuarios no se borran: se desactivan.
    ultimo_acceso TEXT,
    clave_cambiada_en TEXT,
    version_sesion INTEGER NOT NULL DEFAULT 1 CHECK (version_sesion > 0),
    debe_cambiar_clave INTEGER NOT NULL DEFAULT 0 CHECK (debe_cambiar_clave IN (0, 1)),
    creado_en  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Quién hizo qué y cuándo. Solo lo ve el dueño.
CREATE TABLE IF NOT EXISTS actividad (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
    momento    TEXT NOT NULL,
    accion     TEXT NOT NULL,
    detalle    TEXT NOT NULL DEFAULT '',
    orden_id   INTEGER REFERENCES ordenes(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_actividad_momento ON actividad(momento);
CREATE INDEX IF NOT EXISTS idx_actividad_orden ON actividad(orden_id);
CREATE INDEX IF NOT EXISTS idx_vehiculos_cliente ON vehiculos(cliente_id);
CREATE INDEX IF NOT EXISTS idx_ordenes_vehiculo ON ordenes(vehiculo_id);
CREATE INDEX IF NOT EXISTS idx_items_orden ON orden_items(orden_id);
"""

# Columnas agregadas después de la primera versión. init_db las suma a una base
# existente sin tocar sus datos. Los costos son lo que le costó al taller, solo
# para uso interno (nunca se imprimen); NULL significa "costo no cargado".
COLUMNAS_AGREGADAS = (
    ("repuestos_servicios", "costo_centavos", "INTEGER CHECK (costo_centavos >= 0)"),
    ("orden_items", "costo_unitario_centavos", "INTEGER CHECK (costo_unitario_centavos >= 0)"),
    ("orden_items", "costo_total_centavos", "INTEGER CHECK (costo_total_centavos >= 0)"),
    ("orden_items", "del_cliente", "INTEGER NOT NULL DEFAULT 0"),
    ("orden_items", "falta_precio", "INTEGER NOT NULL DEFAULT 0"),
)

def migracion_v1(conexion):
    """Aplica las columnas históricas de forma repetible dentro de la transacción."""
    for tabla, columna, definicion in COLUMNAS_AGREGADAS:
        existentes = {fila["name"] for fila in conexion.execute(f"PRAGMA table_info({tabla})")}
        if columna not in existentes:
            conexion.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}")
            if columna == "del_cliente":
                conexion.execute(
                    "UPDATE orden_items SET del_cliente = 1"
                    " WHERE tipo = 'Repuesto' AND subtotal_centavos = 0"
                )


def migracion_v2(conexion):
    """Crea cancelaciones auditadas y movimientos financieros sin edición destructiva."""
    conexion.execute(
        "ALTER TABLE ordenes ADD COLUMN cobro_sin_conciliar INTEGER NOT NULL DEFAULT 0"
    )
    # No inventamos cobros por el hecho de que una orden anterior esté completada.
    conexion.execute(
        "UPDATE ordenes SET cobro_sin_conciliar = 1 WHERE estado = 'Completado'"
    )
    conexion.execute(
        """CREATE TABLE orden_anulaciones (
               orden_id INTEGER PRIMARY KEY REFERENCES ordenes(id) ON DELETE RESTRICT,
               motivo TEXT NOT NULL CHECK (length(trim(motivo)) > 0),
               usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
               anulada_en TEXT NOT NULL
           )"""
    )
    conexion.execute(
        """CREATE TABLE orden_pagos (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               orden_id INTEGER NOT NULL REFERENCES ordenes(id) ON DELETE RESTRICT,
               tipo TEXT NOT NULL CHECK (tipo IN ('cobro', 'reversion', 'apertura')),
               importe_centavos INTEGER NOT NULL CHECK (importe_centavos > 0),
               medio TEXT NOT NULL,
               referencia TEXT NOT NULL DEFAULT '',
               fecha TEXT NOT NULL,
               usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
               revierte_id INTEGER UNIQUE REFERENCES orden_pagos(id) ON DELETE RESTRICT,
               motivo TEXT NOT NULL DEFAULT '',
               creado_en TEXT NOT NULL,
               CHECK ((tipo IN ('cobro', 'apertura') AND revierte_id IS NULL) OR
                      (tipo = 'reversion' AND revierte_id IS NOT NULL))
           )"""
    )
    conexion.execute(
        "CREATE INDEX idx_orden_pagos_fecha ON orden_pagos(fecha, tipo)"
    )
    conexion.execute(
        "CREATE INDEX idx_orden_pagos_orden ON orden_pagos(orden_id, id)"
    )


def migracion_v3(conexion):
    """Agrega compras y stock como libros auditables, sin asignar stock legado."""
    columnas_catalogo = (
        ("sku", "TEXT NOT NULL DEFAULT ''"),
        ("unidad", "TEXT NOT NULL DEFAULT 'unidad'"),
        ("controla_stock", "INTEGER NOT NULL DEFAULT 0 CHECK (controla_stock IN (0, 1))"),
        ("stock_minimo_milesimas", "INTEGER NOT NULL DEFAULT 0 CHECK (stock_minimo_milesimas >= 0)"),
        ("activo", "INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1))"),
    )
    for columna, definicion in columnas_catalogo:
        existentes = {fila["name"] for fila in conexion.execute("PRAGMA table_info(repuestos_servicios)")}
        if columna not in existentes:
            conexion.execute(f"ALTER TABLE repuestos_servicios ADD COLUMN {columna} {definicion}")
    existentes_items = {fila["name"] for fila in conexion.execute("PRAGMA table_info(orden_items)")}
    if "catalogo_id" not in existentes_items:
        conexion.execute(
            "ALTER TABLE orden_items ADD COLUMN catalogo_id INTEGER REFERENCES repuestos_servicios(id) ON DELETE RESTRICT"
        )

    conexion.execute(
        "CREATE UNIQUE INDEX idx_repuestos_sku ON repuestos_servicios(sku COLLATE NOCASE) WHERE trim(sku) <> ''"
    )
    conexion.execute(
        """CREATE TABLE proveedores (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               nombre TEXT NOT NULL COLLATE NOCASE UNIQUE,
               documento TEXT NOT NULL DEFAULT '',
               telefono TEXT NOT NULL DEFAULT '',
               email TEXT NOT NULL DEFAULT '',
               direccion TEXT NOT NULL DEFAULT '',
               activo INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1)),
               creado_en TEXT NOT NULL
           )"""
    )
    conexion.execute("CREATE INDEX idx_proveedores_activo_nombre ON proveedores(activo, nombre COLLATE NOCASE)")
    conexion.execute(
        """CREATE TABLE compras (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               proveedor_id INTEGER NOT NULL REFERENCES proveedores(id) ON DELETE RESTRICT,
               fecha TEXT NOT NULL,
               documento TEXT NOT NULL DEFAULT '',
               nota TEXT NOT NULL DEFAULT '',
               estado TEXT NOT NULL DEFAULT 'Borrador'
                 CHECK (estado IN ('Borrador', 'Parcial', 'Recibida', 'Anulada')),
               usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
               creada_en TEXT NOT NULL,
               anulada_en TEXT,
               motivo_anulacion TEXT NOT NULL DEFAULT ''
           )"""
    )
    conexion.execute(
        "CREATE UNIQUE INDEX idx_compras_documento ON compras(proveedor_id, documento) WHERE trim(documento) <> ''"
    )
    conexion.execute("CREATE INDEX idx_compras_fecha ON compras(fecha, id)")
    conexion.execute(
        """CREATE TABLE compra_items (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               compra_id INTEGER NOT NULL REFERENCES compras(id) ON DELETE RESTRICT,
               articulo_id INTEGER NOT NULL REFERENCES repuestos_servicios(id) ON DELETE RESTRICT,
               cantidad_milesimas INTEGER NOT NULL CHECK (cantidad_milesimas > 0),
               costo_unitario_centavos INTEGER NOT NULL CHECK (costo_unitario_centavos >= 0)
           )"""
    )
    conexion.execute("CREATE INDEX idx_compra_items_compra ON compra_items(compra_id, id)")
    conexion.execute(
        """CREATE TABLE compra_recepciones (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               compra_id INTEGER NOT NULL REFERENCES compras(id) ON DELETE RESTRICT,
               solicitud_id TEXT NOT NULL UNIQUE,
               fecha TEXT NOT NULL,
               motivo TEXT NOT NULL DEFAULT '',
               usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
               creada_en TEXT NOT NULL
           )"""
    )
    conexion.execute("CREATE INDEX idx_recepciones_compra ON compra_recepciones(compra_id, id)")
    conexion.execute(
        """CREATE TABLE compra_recepcion_items (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               recepcion_id INTEGER NOT NULL REFERENCES compra_recepciones(id) ON DELETE RESTRICT,
               compra_item_id INTEGER NOT NULL REFERENCES compra_items(id) ON DELETE RESTRICT,
               cantidad_milesimas INTEGER NOT NULL CHECK (cantidad_milesimas > 0),
               UNIQUE(recepcion_id, compra_item_id)
           )"""
    )
    conexion.execute(
        "CREATE INDEX idx_recepcion_items_compra_item ON compra_recepcion_items(compra_item_id, id)"
    )
    conexion.execute(
        """CREATE TABLE stock_movimientos (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               articulo_id INTEGER NOT NULL REFERENCES repuestos_servicios(id) ON DELETE RESTRICT,
               tipo TEXT NOT NULL CHECK (tipo IN ('recepcion', 'consumo', 'ajuste', 'devolucion', 'reversion')),
               cantidad_milesimas INTEGER NOT NULL CHECK (cantidad_milesimas <> 0),
               costo_unitario_centavos INTEGER CHECK (costo_unitario_centavos IS NULL OR costo_unitario_centavos >= 0),
               recepcion_item_id INTEGER UNIQUE REFERENCES compra_recepcion_items(id) ON DELETE RESTRICT,
               orden_item_id INTEGER REFERENCES orden_items(id) ON DELETE RESTRICT,
               revierte_movimiento_id INTEGER UNIQUE REFERENCES stock_movimientos(id) ON DELETE RESTRICT,
               motivo TEXT NOT NULL DEFAULT '',
               usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
               creado_en TEXT NOT NULL
           )"""
    )
    conexion.execute("CREATE INDEX idx_stock_movimientos_articulo ON stock_movimientos(articulo_id, id)")
    conexion.execute("CREATE INDEX idx_stock_movimientos_fecha ON stock_movimientos(creado_en, tipo)")
    conexion.execute(
        "CREATE INDEX idx_stock_consumo_orden_item ON stock_movimientos(orden_item_id, id) WHERE tipo = 'consumo'"
    )


def migracion_v4(conexion):
    """Permite volver a consumir una línea cuando el movimiento previo se revirtió."""
    conexion.execute("DROP INDEX IF EXISTS idx_stock_consumo_orden_item")
    conexion.execute(
        "CREATE INDEX idx_stock_consumo_orden_item ON stock_movimientos(orden_item_id, id) WHERE tipo = 'consumo'"
    )


def migracion_v5(conexion):
    """Añade libros de egresos, pagos a proveedores y cierres diarios auditables."""
    conexion.execute(
        """CREATE TABLE gastos (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               fecha TEXT NOT NULL,
               categoria TEXT NOT NULL,
               concepto TEXT NOT NULL,
               importe_centavos INTEGER NOT NULL CHECK (importe_centavos > 0),
               medio TEXT NOT NULL,
               referencia TEXT NOT NULL DEFAULT '',
               usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
               creado_en TEXT NOT NULL
           )"""
    )
    conexion.execute("CREATE INDEX idx_gastos_fecha_medio ON gastos(fecha, medio)")
    conexion.execute(
        """CREATE TABLE gasto_reversiones (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               gasto_id INTEGER NOT NULL UNIQUE REFERENCES gastos(id) ON DELETE RESTRICT,
               fecha TEXT NOT NULL,
               motivo TEXT NOT NULL,
               usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
               creada_en TEXT NOT NULL
           )"""
    )
    conexion.execute(
        """CREATE TABLE proveedor_pagos (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               compra_id INTEGER NOT NULL REFERENCES compras(id) ON DELETE RESTRICT,
               tipo TEXT NOT NULL CHECK (tipo IN ('pago', 'reversion')),
               importe_centavos INTEGER NOT NULL CHECK (importe_centavos > 0),
               medio TEXT NOT NULL,
               referencia TEXT NOT NULL DEFAULT '',
               fecha TEXT NOT NULL,
               usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
               revierte_id INTEGER UNIQUE REFERENCES proveedor_pagos(id) ON DELETE RESTRICT,
               motivo TEXT NOT NULL DEFAULT '',
               creado_en TEXT NOT NULL,
               CHECK ((tipo = 'pago' AND revierte_id IS NULL) OR
                      (tipo = 'reversion' AND revierte_id IS NOT NULL))
           )"""
    )
    conexion.execute("CREATE INDEX idx_proveedor_pagos_compra ON proveedor_pagos(compra_id, id)")
    conexion.execute("CREATE INDEX idx_proveedor_pagos_fecha ON proveedor_pagos(fecha, medio)")
    conexion.execute(
        """CREATE TABLE caja_cierres (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               fecha TEXT NOT NULL,
               efectivo_inicial_centavos INTEGER NOT NULL CHECK (efectivo_inicial_centavos >= 0),
               efectivo_esperado_centavos INTEGER NOT NULL,
               efectivo_contado_centavos INTEGER NOT NULL CHECK (efectivo_contado_centavos >= 0),
               diferencia_centavos INTEGER NOT NULL,
               nota TEXT NOT NULL DEFAULT '',
               estado TEXT NOT NULL DEFAULT 'cerrado' CHECK (estado IN ('cerrado', 'reabierto')),
               usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
               creado_en TEXT NOT NULL,
               reabierto_por INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
               reabierto_en TEXT,
               motivo_reapertura TEXT NOT NULL DEFAULT ''
           )"""
    )
    conexion.execute(
        "CREATE UNIQUE INDEX idx_caja_cierre_activo_fecha ON caja_cierres(fecha) WHERE estado = 'cerrado'"
    )
    conexion.execute("CREATE INDEX idx_caja_cierres_fecha ON caja_cierres(fecha, id)")


def migracion_v6(conexion):
    """Agrega agenda de órdenes y conserva líneas reemplazadas con movimientos históricos."""
    columnas_orden = {fila["name"] for fila in conexion.execute("PRAGMA table_info(ordenes)")}
    if "fecha_prometida" not in columnas_orden:
        conexion.execute("ALTER TABLE ordenes ADD COLUMN fecha_prometida TEXT")
    if "prioridad" not in columnas_orden:
        conexion.execute(
            "ALTER TABLE ordenes ADD COLUMN prioridad TEXT NOT NULL DEFAULT 'Normal' "
            "CHECK (prioridad IN ('Baja', 'Normal', 'Alta', 'Urgente'))"
        )
    columnas_items = {fila["name"] for fila in conexion.execute("PRAGMA table_info(orden_items)")}
    if "vigente" not in columnas_items:
        conexion.execute(
            "ALTER TABLE orden_items ADD COLUMN vigente INTEGER NOT NULL DEFAULT 1 "
            "CHECK (vigente IN (0, 1))"
        )
    conexion.execute(
        "CREATE INDEX IF NOT EXISTS idx_ordenes_agenda ON ordenes(estado, fecha_prometida, prioridad)"
    )
    conexion.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_vigentes ON orden_items(orden_id, vigente, id)"
    )


def migracion_v7(conexion):
    """Expira sesiones antiguas y obliga a renovar claves de cuentas ya existentes."""
    columnas = {fila["name"] for fila in conexion.execute("PRAGMA table_info(usuarios)")}
    nuevas = (
        ("ultimo_acceso", "TEXT"),
        ("clave_cambiada_en", "TEXT"),
        ("version_sesion", "INTEGER NOT NULL DEFAULT 1 CHECK (version_sesion > 0)"),
        ("debe_cambiar_clave", "INTEGER NOT NULL DEFAULT 0 CHECK (debe_cambiar_clave IN (0, 1))"),
    )
    for columna, definicion in nuevas:
        if columna not in columnas:
            conexion.execute(f"ALTER TABLE usuarios ADD COLUMN {columna} {definicion}")
            if columna == "debe_cambiar_clave":
                conexion.execute("UPDATE usuarios SET debe_cambiar_clave = 1")
    conexion.execute(
        """CREATE TABLE IF NOT EXISTS intentos_login (
               bucket TEXT PRIMARY KEY,
               fallos INTEGER NOT NULL CHECK (fallos >= 0),
               ventana_desde TEXT NOT NULL,
               bloqueado_hasta TEXT
           )"""
    )

MIGRACIONES = (
    (1, "columnas adicionales de costos y repuestos", migracion_v1),
    (2, "anulaciones y libro inmutable de cobros", migracion_v2),
    (3, "proveedores, compras e inventario trazable", migracion_v3),
    (4, "reconsumo de líneas de órdenes con reversión auditada", migracion_v4),
    (5, "gastos, pagos a proveedores y cierres de caja", migracion_v5),
    (6, "agenda, prioridad y versionado de líneas de órdenes", migracion_v6),
    (7, "sesiones, renovación de claves y limitación de intentos", migracion_v7),
)


class ServicioPersistencia:
    """Aplica el esquema y migraciones usando la configuración viva de Flask."""

    def __init__(self, configuracion, conectar):
        self.configuracion = configuracion
        self.conectar = conectar

    def init_db(self):
        ruta_db = Path(self.configuracion["DATABASE"])
        requiere_migracion = False
        if ruta_db.is_file() and ruta_db.stat().st_size:
            previa = sqlite3.connect(ruta_db)
            try:
                tablas = {
                    fila[0]
                    for fila in previa.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if tablas:
                    tiene_versiones = "schema_migrations" in tablas
                    version = (
                        previa.execute(
                            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                        ).fetchone()[0]
                        if tiene_versiones
                        else 0
                    )
                    requiere_migracion = version < ULTIMA_VERSION_ESQUEMA
            except sqlite3.DatabaseError:
                requiere_migracion = True
            finally:
                previa.close()
        if requiere_migracion:
            self.respaldar_antes_de_migrar(ruta_db)

        conexion = self.conectar()
        try:
            conexion.executescript(ESQUEMA)
            conexion.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       version INTEGER PRIMARY KEY,
                       nombre TEXT NOT NULL,
                       aplicada_en TEXT NOT NULL
                   )"""
            )
            aplicadas = {
                fila[0] for fila in conexion.execute(
                    "SELECT version FROM schema_migrations"
                )
            }
            for version, nombre, aplicar in MIGRACIONES:
                if version in aplicadas:
                    continue
                conexion.execute("BEGIN IMMEDIATE")
                try:
                    aplicar(conexion)
                    conexion.execute(
                        """INSERT INTO schema_migrations
                           (version, nombre, aplicada_en) VALUES (?, ?, ?)""",
                        (version, nombre, datetime.now().isoformat(timespec="seconds")),
                    )
                    conexion.commit()
                except Exception:
                    conexion.rollback()
                    raise
        finally:
            conexion.close()

    def respaldar_antes_de_migrar(self, ruta_db):
        """Guarda una copia íntegra antes de modificar una base existente."""
        carpeta = Path(self.configuracion["RESPALDOS"])
        carpeta.mkdir(parents=True, exist_ok=True)
        destino = carpeta / (
            f"pre_migracion_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.db"
        )
        origen = sqlite3.connect(ruta_db)
        copia = sqlite3.connect(destino)
        try:
            origen.backup(copia)
            integridad = copia.execute("PRAGMA integrity_check").fetchone()[0]
            foranea = copia.execute("PRAGMA foreign_key_check").fetchone()
            copia.commit()
            if integridad != "ok" or foranea is not None:
                raise sqlite3.DatabaseError(
                    "la copia previa a la migración no superó la verificación"
                )
        except Exception:
            copia.close()
            origen.close()
            destino.unlink(missing_ok=True)
            raise
        copia.close()
        origen.close()
        return destino
