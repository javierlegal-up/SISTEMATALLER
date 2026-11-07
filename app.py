"""Sistema web para taller mecánico: clientes, vehículos, órdenes de trabajo y catálogo.

Ejecutar con:  python app.py   y abrir http://127.0.0.1:5000
"""
import os
import re
import secrets
import shutil
import sqlite3
import threading
import csv
import io
import logging
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash
from flask.logging import default_handler

import exportar_excel
from servicio_respaldos import ServicioRespaldos, verificar_archivo_base as verificar_base_respaldo
from servicio_analitica import (
    NOMBRES_MES, PERIODOS_ANALITICA, agrupar_serie_analitica, rango_analitica,
    variacion, resumen_analitica_periodo, gasto_neto_periodo, serie_analitica,
    estado_analitica_actual as _estado_analitica_actual,
    datos_analitica as _datos_analitica,
)
from servicio_reportes import (
    METRICAS_EQUIPO, COLORES_GRAFICO, leer_trimestre, trimestre_vecino, sumar_filas,
    meses_del_trimestre as _meses_del_trimestre,
    taller_por_mes as _taller_por_mes,
    cobros_por_medio as _cobros_por_medio,
    equipo_por_mes as _equipo_por_mes,
    datos_reporte as _datos_reporte,
)
from persistencia import (
    COLUMNAS_AGREGADAS, ESQUEMA, MIGRACIONES, ULTIMA_VERSION_ESQUEMA,
    ServicioPersistencia, migracion_v1, migracion_v2, migracion_v3,
    migracion_v4, migracion_v5, migracion_v6, migracion_v7,
)
from servicio_seguridad import (
    ACCION_POR_VISTA, ACCIONES_ERP, PERMISOS_POR_ROL, NOMBRES_ROL,
    LARGO_MINIMO_CLAVE, MAXIMO_INTENTOS_LOGIN, MAXIMO_INTENTOS_IP,
    VENTANA_INTENTOS_LOGIN, DURACION_BLOQUEO_LOGIN,
    crear_usuario as _crear_usuario,
    leer_datos_usuario as _leer_datos_usuario,
    tiene_permiso as _tiene_permiso,
    parsear_momento as _parsear_momento,
    buckets_login as _buckets_login,
    login_bloqueado, registrar_fallo_login,
)
from servicio_ordenes import (
    precio_para_empleado, fecha_completado_para, resumir_costos,
    problema_cambio_estado as _problema_cambio_estado,
)
from servicio_inventario import (
    importe_linea_compra_centavos,
    stock_articulo as _stock_articulo,
    costo_promedio_articulo as _costo_promedio_articulo,
    insertar_movimiento_stock as _insertar_movimiento_stock,
    cantidades_recibidas as _cantidades_recibidas,
    actualizar_estado_compra as _actualizar_estado_compra,
    articulos_de_compra as _articulos_de_compra,
    importes_pedidos_compra as _importes_pedidos_compra,
    cuenta_compra as _cuenta_compra,
)

from flask import (
    Flask,
    abort,
    flash,
    g,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

BASE_DIR = Path(__file__).resolve().parent

# Datos que aparecen en el comprobante impreso. Edítalos con los de tu taller.
TALLER = {
    "nombre": "Taller San Cayetano",
    "direccion": "Dirección del taller",
    "telefono": "Teléfono del taller",
    "email": "",
}

# Carpeta de las copias de seguridad. Para protegerte si se daña la computadora,
# apunta a una carpeta sincronizada, por ejemplo:
# CARPETA_RESPALDOS = Path(r"C:\Users\Esteban\OneDrive\RespaldosTaller")
CARPETA_RESPALDOS = BASE_DIR / "respaldos"
MAXIMO_RESPALDOS = 30  # Se guarda uno por día: 30 copias son 30 días de uso.

# Copias FUERA de la compu (lo que protege si se rompe el disco o la compu entera):
# - Pendrive: crea una carpeta con este nombre en la raíz del pendrive. Mientras esté
#   conectado, el sistema le copia los respaldos solo, sin importar la letra (E:, F:...).
CARPETA_PENDRIVE = "RespaldoTaller"
# - Nube u otro disco: agrega aquí carpetas fijas, por ejemplo una de Google Drive u OneDrive:
#   CARPETAS_EXTRA = [r"C:\Users\Taller\OneDrive\RespaldoTaller"]
CARPETAS_EXTRA = []
# Mientras se trabaja, la copia del día se actualiza como mucho cada este tiempo.
INTERVALO_RESPALDO = timedelta(minutes=15)
# El panel avisa al dueño si pasan estos días sin una copia fuera de la compu.
DIAS_AVISO_COPIA_EXTERNA = 3
PATRON_RESPALDO = "taller_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].db"
# Además de la copia .db (la que sirve para restaurar), se guarda una copia en Excel
# para leer o imprimir. Va en la carpeta "respaldos_excel", junto a "respaldos".
PATRON_EXCEL = "taller_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].xlsx"

ESTADOS = ("Pendiente", "En Proceso", "Completado")
TIPOS = ("Repuesto", "Servicio")  # "Servicio" es lo que se suma como mano de obra.
MAXIMO_IMPORTE = Decimal("999999999")
MAXIMA_CANTIDAD = Decimal("100000")
DIAS_ATRASO = 7  # Una orden abierta con más días que estos se marca como atrasada.
ATRASADAS_EN_PANEL = 4  # Cuántas órdenes atrasadas se muestran en el panel.
MAS_FACTURADO_EN_PANEL = 5  # Cuántos repuestos/servicios del mes se muestran en el panel.
POR_PAGINA = 15  # Filas por página en las listas, para evitar el scroll largo.

# Usuarios. El dueño ve todo; el empleado registra autos y trabajos, pero no ve
# precios, costos ni el panel, no borra nada y no puede completar órdenes.
PRIORIDADES_ORDEN = ("Baja", "Normal", "Alta", "Urgente")
# En el registro de actividad, lo hecho antes de esta hora es del turno mañana.
HORA_CAMBIO_TURNO = 14
# Acciones de producción: las que muestra Actividad y cuentan para el rendimiento.
# Las administrativas (borrar, usuarios, lista de precios, copias) se guardan igual,
# para saber quién borró algo, pero solo se ven al marcar "Incluir acciones administrativas".
ACCIONES_PRODUCCION = (
    "Creó la orden",
    "Editó la orden",
    "Cambió el estado",
    "Registró un cliente",
    "Agregó un vehículo",
)
# Los importes se guardan como enteros en centavos para evitar errores de redondeo.
# orden_items copia descripción y precio: si luego cambia el catálogo, las órdenes
# ya emitidas no se alteran.




CONTAR_ORDENES = """
    SELECT COUNT(*) AS total
    FROM ordenes o
    JOIN vehiculos v ON v.id = o.vehiculo_id
    JOIN clientes c ON c.id = v.cliente_id
"""

# Secciones de órdenes: valor de ?estado= -> (título, condición SQL, orden SQL).
# Las abiertas salen de la más antigua a la más nueva, para atender primero lo que más espera.
VISTAS_ORDENES = {
    "": ("Todas las órdenes", None, "o.id DESC"),
    "Pendiente": ("Pendientes", "o.estado = 'Pendiente'", "o.fecha_ingreso, o.id"),
    "En Proceso": ("En proceso", "o.estado = 'En Proceso'", "o.fecha_ingreso, o.id"),
    "Completado": ("Completadas", "o.estado = 'Completado'", "o.fecha_completado DESC, o.id DESC"),
    "abiertas": ("Órdenes abiertas", "o.estado != 'Completado'", "o.fecha_ingreso, o.id"),
    "atrasadas": (
        "Órdenes atrasadas",
        "o.estado != 'Completado' AND o.fecha_ingreso < ?",
        "o.fecha_ingreso, o.id",
    ),
    "sin_precio": (
        "Falta poner precio",
        "EXISTS (SELECT 1 FROM orden_items i WHERE i.orden_id = o.id AND i.vigente = 1 AND i.falta_precio = 1)",
        "o.fecha_ingreso, o.id",
    ),
    "anuladas": (
        "Órdenes anuladas",
        "EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)",
        "o.id DESC",
    ),
}

SELECT_ORDEN = """
    SELECT o.*,
           v.patente, v.marca, v.modelo, v.anio, v.color, v.cliente_id,
           c.nombre AS cliente_nombre, c.documento AS cliente_documento,
           c.telefono AS cliente_telefono, c.email AS cliente_email,
           c.direccion AS cliente_direccion,
           COALESCE((SELECT SUM(i.subtotal_centavos) FROM orden_items i
                     WHERE i.orden_id = o.id AND i.vigente = 1 AND i.tipo = 'Repuesto'), 0) AS total_repuestos,
           COALESCE((SELECT SUM(i.subtotal_centavos) FROM orden_items i
                     WHERE i.orden_id = o.id AND i.vigente = 1 AND i.tipo = 'Servicio'), 0) AS total_mano_obra,
           (SELECT COUNT(*) FROM orden_items i
            WHERE i.orden_id = o.id AND i.vigente = 1 AND i.falta_precio = 1) AS lineas_sin_precio,
           COALESCE((SELECT SUM(CASE WHEN p.tipo = 'reversion' THEN -p.importe_centavos
                                     ELSE p.importe_centavos END)
                     FROM orden_pagos p WHERE p.orden_id = o.id), 0) AS pagado_centavos,
           EXISTS(SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id) AS anulada,
           o.cobro_sin_conciliar
    FROM ordenes o
    JOIN vehiculos v ON v.id = o.vehiculo_id
    JOIN clientes c ON c.id = v.cliente_id
"""


def cargar_secret_key():
    """Genera la clave de sesión la primera vez y la reutiliza después."""
    ruta = BASE_DIR / ".secret_key"
    if not ruta.exists():
        ruta.write_text(secrets.token_hex(32), encoding="utf-8")
    return ruta.read_text(encoding="utf-8").strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=cargar_secret_key(),
    DATABASE=os.environ.get("TALLER_DB", str(BASE_DIR / "taller.db")),
    RESPALDOS=os.environ.get("TALLER_RESPALDOS", str(CARPETA_RESPALDOS)),
    LOG_DIR=os.environ.get("TALLER_LOGS", str(BASE_DIR / "logs")),
    RESPALDOS_EXTRA=list(CARPETAS_EXTRA),
    BUSCAR_PENDRIVE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_REFRESH_EACH_REQUEST=True,
    SESSION_IDLE_TIMEOUT=timedelta(hours=8),
    MAX_CONTENT_LENGTH=100 * 1024 * 1024,
)
app.config["RESPALDOS_EXCEL"] = os.environ.get(
    "TALLER_RESPALDOS_EXCEL", str(Path(app.config["RESPALDOS"]).parent / "respaldos_excel")
)


def configurar_registro_tecnico():
    """Activa un registro local rotativo para diagnósticos, separado de la base del taller."""
    ruta = Path(app.config["LOG_DIR"]) / "taller.log"
    if any(getattr(handler, "_taller_log_path", None) == str(ruta) for handler in app.logger.handlers):
        return
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            ruta, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
    except OSError:
        app.logger.error("No se pudo inicializar el archivo de registro técnico local.")
        return
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler._taller_log_path = str(ruta)
    app.logger.setLevel(logging.INFO)
    app.logger.addHandler(handler)
    if default_handler in app.logger.handlers:
        app.logger.removeHandler(default_handler)
    app.logger.propagate = False


configurar_registro_tecnico()


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
def conectar():
    conexion = sqlite3.connect(app.config["DATABASE"])
    conexion.row_factory = sqlite3.Row
    # SQLite no aplica las claves foráneas salvo que se active en cada conexión.
    conexion.execute("PRAGMA foreign_keys = ON")
    return conexion


def get_db():
    if "db" not in g:
        g.db = conectar()
    return g.db


@app.teardown_appcontext
def cerrar_db(_error=None):
    conexion = g.pop("db", None)
    if conexion is not None:
        conexion.close()


servicio_persistencia = ServicioPersistencia(app.config, conectar)


def init_db():
    return servicio_persistencia.init_db()

def respaldar_antes_de_migrar(ruta_db):
    return servicio_persistencia.respaldar_antes_de_migrar(ruta_db)


# ---------------------------------------------------------------------------
# Usuarios, permisos y registro de actividad
# ---------------------------------------------------------------------------
def tiene_permiso(accion, usuario=None):
    """Consulta la matriz central de permisos del ERP por acción y rol."""
    if usuario is None and has_request_context():
        usuario = g.get("usuario")
    return _tiene_permiso(accion, usuario)


def requiere_permiso(accion):
    """Protege una ruta con la acción declarada en la matriz de roles."""
    def decorar(vista):
        @wraps(vista)
        def envoltura(*args, **kwargs):
            if not tiene_permiso(accion):
                abort(403)
            return vista(*args, **kwargs)
        return envoltura
    return decorar


def crear_usuario(conexion, usuario, nombre, clave, rol):
    """Guarda un usuario con la contraseña cifrada (nunca se guarda tal cual)."""
    return _crear_usuario(conexion, usuario, nombre, clave, rol)


def leer_datos_usuario(form, pedir_usuario=True):
    """Adaptador compatible para las rutas que administran cuentas."""
    return _leer_datos_usuario(form, pedir_usuario)


def es_dueno():
    return tiene_permiso("ver_finanzas")


def solo_dueno(vista):
    """Compatibilidad para rutas existentes: cada acción se resuelve desde la matriz."""
    accion = ACCION_POR_VISTA.get(vista.__name__, "administrar_sistema")
    return requiere_permiso(accion)(vista)


def registrar(accion, detalle="", orden_id=None):
    """Anota en el registro de actividad quién hizo qué y cuándo."""
    usuario = g.get("usuario")
    db = get_db()
    with db:
        db.execute(
            "INSERT INTO actividad (usuario_id, momento, accion, detalle, orden_id) VALUES (?, ?, ?, ?, ?)",
            (
                usuario["id"] if usuario else None,
                datetime.now().isoformat(timespec="seconds"),
                accion,
                detalle,
                orden_id,
            ),
        )


# ---------------------------------------------------------------------------
# Copias de seguridad
# ---------------------------------------------------------------------------
TABLAS_BASE_ERP = {"clientes", "vehiculos", "repuestos_servicios", "ordenes", "orden_items", "usuarios"}

servicio_respaldos = ServicioRespaldos(
    app.config,
    app.logger,
    exportar_excel,
    MAXIMO_RESPALDOS,
    PATRON_RESPALDO,
    PATRON_EXCEL,
    CARPETA_PENDRIVE,
)
candado_respaldo = threading.Lock()
ultimo_respaldo_automatico = None


def crear_respaldo(actualizar=False):
    return servicio_respaldos.crear_respaldo(actualizar)


def crear_respaldo_excel(origen=None):
    return servicio_respaldos.crear_respaldo_excel(origen)


def carpetas_en_pendrives():
    return servicio_respaldos.carpetas_en_pendrives()


def archivo_estado_externo():
    return servicio_respaldos.archivo_estado_externo()


def copiar_a_destinos_externos(archivo, excel=None):
    return servicio_respaldos.copiar_a_destinos_externos(archivo, excel)


def ultima_copia_externa():
    return servicio_respaldos.ultima_copia_externa()


def verificar_archivo_base(ruta, exigir_version_actual=False):
    return verificar_base_respaldo(
        ruta, TABLAS_BASE_ERP, ULTIMA_VERSION_ESQUEMA, exigir_version_actual
    )


def ultimo_respaldo():
    return servicio_respaldos.ultimo_respaldo()


def ultimo_excel():
    return servicio_respaldos.ultimo_excel()


@app.route("/restaurar", methods=["GET", "POST"])
@solo_dueno
def restaurar():
    if request.method == "GET":
        return render_template(
            "restaurar.html",
            ultimo_respaldo=ultimo_respaldo(),
            ultimo_excel=ultimo_excel(),
            cantidad_respaldos=len(list(Path(app.config["RESPALDOS"]).glob(PATRON_RESPALDO))),
        )
    archivo = request.files.get("archivo")
    if archivo is None or not archivo.filename:
        flash("Selecciona una copia de base de datos .db, .sqlite o .sqlite3.", "error")
        return redirect(url_for("restaurar"))
    if Path(archivo.filename).suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
        flash("Solo se aceptan copias de base de datos .db, .sqlite o .sqlite3. El XLSX es solo para lectura.", "error")
        return redirect(url_for("restaurar"))

    carpeta = Path(app.config["RESPALDOS"])
    carpeta.mkdir(parents=True, exist_ok=True)
    temporal = carpeta / f"restaurar_{secrets.token_hex(12)}.db"
    carpeta_migraciones = carpeta / f"migraciones_restaurar_{secrets.token_hex(8)}"
    seguridad = carpeta / f"pre_restauracion_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.db"
    original_db, original_respaldos = app.config["DATABASE"], app.config["RESPALDOS"]
    try:
        archivo.save(temporal)
        verificar_archivo_base(temporal)
        app.config.update(DATABASE=str(temporal), RESPALDOS=str(carpeta_migraciones))
        try:
            init_db()
        finally:
            app.config.update(DATABASE=original_db, RESPALDOS=original_respaldos)
        datos = verificar_archivo_base(temporal, exigir_version_actual=True)

        copia_seguridad = sqlite3.connect(seguridad)
        try:
            get_db().backup(copia_seguridad)
            if copia_seguridad.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("no se pudo verificar la copia de seguridad previa")
        finally:
            copia_seguridad.close()
        origen = sqlite3.connect(temporal)
        try:
            origen.backup(get_db())
        except Exception:
            respaldo = sqlite3.connect(seguridad)
            try:
                respaldo.backup(get_db())
            finally:
                respaldo.close()
            raise
        finally:
            origen.close()
        verificar_integridad = get_db().execute("PRAGMA integrity_check").fetchone()[0]
        if verificar_integridad != "ok" or get_db().execute("PRAGMA foreign_key_check").fetchone():
            respaldo = sqlite3.connect(seguridad)
            try:
                respaldo.backup(get_db())
            finally:
                respaldo.close()
            raise sqlite3.DatabaseError("la base restaurada no superó la verificación final")

        total = sum(
            get_db().execute(f'SELECT COUNT(*) FROM "{tabla}"').fetchone()[0]
            for tabla in ("clientes", "vehiculos", "ordenes", "orden_items", "proveedores", "compras", "gastos")
            if tabla in datos["tablas"]
        )
        id_usuario = session.get("usuario_id")
        usuario = get_db().execute(
            "SELECT id FROM usuarios WHERE id = ? AND activo = 1", (id_usuario,)
        ).fetchone()
        if usuario is None:
            session.clear()
            flash("Base restaurada y verificada. Inicia sesión con un usuario de la copia.", "ok")
            return redirect(url_for("login"))
        flash(f"Base restaurada y verificada: {total} registros revisados. Se guardó una copia previa en {seguridad.name}.", "ok")
        return redirect(url_for("inicio"))
    except (OSError, sqlite3.Error, ValueError):
        app.logger.exception("No se pudo restaurar la copia de seguridad")
        flash(
            "No se restauró la base. La copia seleccionada no es válida o no pudo verificarse. "
            "Revisa el registro técnico si el problema continúa.",
            "error",
        )
        return redirect(url_for("restaurar"))
    finally:
        app.config.update(DATABASE=original_db, RESPALDOS=original_respaldos)
        temporal.unlink(missing_ok=True)
        shutil.rmtree(carpeta_migraciones, ignore_errors=True)


def respaldar(actualizar=False):
    """Copia local y, si hay dónde, copia externa. Devuelve (copiados, errores_externos)."""
    with candado_respaldo:
        destino = crear_respaldo(actualizar)
        if destino is None:
            return [], []
        try:
            excel = crear_respaldo_excel(destino)
        except Exception:  # Sin Excel se sigue: la copia .db es la importante.
            app.logger.exception("No se pudo generar el Excel adjunto al respaldo")
            excel = None
        return copiar_a_destinos_externos(destino, excel)


def respaldar_sin_fallar(actualizar=False):
    """Un respaldo fallido no debe impedir trabajar, pero sí avisar. Devuelve si salió bien."""
    try:
        _copiados, errores = respaldar(actualizar)
    except (OSError, sqlite3.Error):
        app.logger.exception("No se pudo crear la copia de seguridad automática")
        return False
    for error in errores:
        app.logger.warning("No se pudo copiar el respaldo a %s", error)
    return True


@app.after_request
def respaldo_tras_cambios(respuesta):
    """Después de guardar algo, actualiza la copia del día (como mucho cada INTERVALO_RESPALDO).

    Así, si la compu se apaga o se rompe, se pierde como mucho el último rato de trabajo.
    """
    global ultimo_respaldo_automatico
    if request.method != "POST" or respuesta.status_code >= 400 or app.config.get("TESTING"):
        return respuesta
    ahora = datetime.now()
    if ultimo_respaldo_automatico is None or ahora - ultimo_respaldo_automatico >= INTERVALO_RESPALDO:
        ultimo_respaldo_automatico = ahora
        respaldar_sin_fallar(actualizar=True)
    return respuesta


@app.route("/respaldo", methods=["POST"])
@solo_dueno
def respaldo_ahora():
    destino_vuelta = url_for("restaurar") if request.form.get("volver") == "restaurar" else url_for("inicio")
    try:
        copiados, errores = respaldar(actualizar=True)
    except (OSError, sqlite3.Error):
        app.logger.exception("No se pudo crear la copia de seguridad solicitada")
        flash("No se pudo crear la copia de seguridad. Revisa el registro técnico si el problema continúa.", "error")
        return redirect(destino_vuelta)
    if errores:
        flash(
            "No se pudo copiar a una ubicación externa. Comprueba que la unidad esté conectada "
            "y que tenga espacio disponible.",
            "error",
        )
    if copiados:
        lugares = ", ".join(str(carpeta) for carpeta in copiados)
        registrar("Hizo una copia de seguridad", lugares)
        flash(f"Copia guardada en la compu y en: {lugares}.", "ok")
    else:
        flash(
            "Copia guardada solo en la compu. Para tener una copia fuera, conecta el pendrive "
            f"(con la carpeta {CARPETA_PENDRIVE}) y vuelve a pulsar el botón.",
            "error",
        )
    return redirect(destino_vuelta)


@app.route("/respaldo/excel")
@solo_dueno
def respaldo_excel():
    """Genera ahora el Excel con todos los datos y lo descarga (también queda en respaldos_excel)."""
    try:
        with candado_respaldo:
            excel = crear_respaldo_excel()
    except (OSError, sqlite3.Error):
        app.logger.exception("No se pudo generar la exportación Excel solicitada")
        flash("No se pudo crear el Excel. Revisa el registro técnico si el problema continúa.", "error")
        destino = url_for("restaurar") if request.args.get("volver") == "restaurar" else url_for("inicio")
        return redirect(destino)
    registrar("Descargó los datos en Excel", excel.name)
    return send_file(excel, as_attachment=True, download_name=excel.name)


# ---------------------------------------------------------------------------
# Seguridad y datos comunes de las plantillas
# ---------------------------------------------------------------------------
@app.before_request
def verificar_csrf():
    if request.method != "POST" or app.config.get("TESTING"):
        return
    esperado = session.get("csrf_token", "")
    recibido = request.form.get("csrf_token", "")
    if not esperado or not secrets.compare_digest(esperado.encode(), recibido.encode()):
        abort(400, "El formulario caducó. Vuelve atrás, recarga la página e inténtalo de nuevo.")


@app.context_processor
def datos_plantillas():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return {
        "csrf_token": session["csrf_token"],
        "ESTADOS": ESTADOS,
        "PRIORIDADES_ORDEN": PRIORIDADES_ORDEN,
        "TIPOS": TIPOS,
        "TALLER": TALLER,
        "usuario": g.get("usuario"),
        "es_dueno": es_dueno(),
        "NOMBRES_ROL": NOMBRES_ROL,
        "LARGO_MINIMO_CLAVE": LARGO_MINIMO_CLAVE,
        "nav_conteos": conteos_por_estado(),
        "hoy_iso": date.today().isoformat(),
        "MEDIOS_PAGO": MEDIOS_PAGO,
        "RUBROS_GASTO": RUBROS_GASTO,
    }


# Pantallas que se pueden ver sin haber iniciado sesión.
RUTAS_PUBLICAS = {"login", "configuracion_inicial", "healthz", "static"}


@app.route("/healthz")
def healthz():
    try:
        db = get_db()
        version = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
        db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error:
        return jsonify(status="error", database="unavailable"), 503
    return jsonify(status="ok", database="ok", schema_version=version)


def pagina_de_inicio():
    """Todos los roles entran a su panel, con datos acordes a sus permisos."""
    return url_for("inicio")


@app.before_request
def cargar_usuario():
    """Identifica al usuario y exige iniciar sesión. La primera vez, pide crear al dueño."""
    g.usuario = None
    db = get_db()
    if db.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone() is None:
        if request.endpoint not in ("configuracion_inicial", "static"):
            return redirect(url_for("configuracion_inicial"))
        return None

    usuario_id = session.get("usuario_id")
    if usuario_id is not None:
        g.usuario = db.execute(
            "SELECT * FROM usuarios WHERE id = ? AND activo = 1", (usuario_id,)
        ).fetchone()
        if g.usuario is None or session.get("version_sesion") != g.usuario["version_sesion"]:
            session.clear()  # Desactivación, renovación de clave o versión de sesión antigua.
            g.usuario = None
        else:
            ultima_actividad = _parsear_momento(session.get("actividad_sesion"))
            ahora = datetime.now()
            if ultima_actividad is None or ahora - ultima_actividad > app.config["SESSION_IDLE_TIMEOUT"]:
                session.clear()
                g.usuario = None
            else:
                session.permanent = True
                if request.endpoint != "static":
                    session["actividad_sesion"] = ahora.isoformat(timespec="seconds")

    if (
        g.usuario is not None
        and g.usuario["debe_cambiar_clave"]
        and request.endpoint not in ("mi_clave", "logout", "static", "healthz")
    ):
        return redirect(url_for("mi_clave"))

    if g.usuario is None and request.endpoint not in RUTAS_PUBLICAS:
        if request.path.startswith("/api/"):
            return jsonify(error="La sesión se cerró. Vuelve a iniciar sesión."), 401
        return redirect(url_for("login"))
    return None


@app.errorhandler(403)
def sin_permiso(_error):
    return render_template("sin_permiso.html"), 403


@app.route("/configuracion-inicial", methods=["GET", "POST"])
def configuracion_inicial():
    """Primera vez que se abre el sistema: se crea la cuenta del dueño."""
    db = get_db()
    if db.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone() is not None:
        return redirect(url_for("login"))
    datos, errores = {}, []
    if request.method == "POST":
        datos, clave, errores = leer_datos_usuario(request.form)
        if not errores:
            with db:
                usuario_id = crear_usuario(db, datos["usuario"], datos["nombre"], clave, "dueno")
                ahora = datetime.now().isoformat(timespec="seconds")
                db.execute("UPDATE usuarios SET ultimo_acceso = ? WHERE id = ?", (ahora, usuario_id))
            session.clear()
            session["usuario_id"] = usuario_id
            session["version_sesion"] = 1
            session["actividad_sesion"] = ahora
            session.permanent = True
            g.usuario = db.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
            registrar("Creó la cuenta del dueño")
            flash("Cuenta creada. Ahora puedes crear los usuarios de tus empleados en Usuarios.", "ok")
            return redirect(url_for("inicio"))
    return render_template("configuracion_inicial.html", datos=datos, errores=errores)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.usuario is not None:
        return redirect(pagina_de_inicio())
    error, usuario_escrito = None, ""
    if request.method == "POST":
        usuario_escrito = request.form.get("usuario", "").strip()
        db = get_db()
        ahora = datetime.now()
        buckets = _buckets_login(usuario_escrito, request.remote_addr)
        if login_bloqueado(db, buckets, ahora):
            error = "Demasiados intentos. Espera 15 minutos antes de volver a ingresar."
        else:
            fila = db.execute(
                "SELECT * FROM usuarios WHERE usuario = ? AND activo = 1", (usuario_escrito,)
            ).fetchone()
            clave_valida = fila is not None and check_password_hash(
                fila["clave_hash"], request.form.get("clave", "")
            )
            if clave_valida:
                with db:
                    db.execute(
                        "UPDATE usuarios SET ultimo_acceso = ? WHERE id = ?",
                        (ahora.isoformat(timespec="seconds"), fila["id"]),
                    )
                    db.execute("DELETE FROM intentos_login WHERE bucket = ?", (buckets[0][0],))
                session.clear()
                session["usuario_id"] = fila["id"]
                session["version_sesion"] = fila["version_sesion"]
                session["actividad_sesion"] = ahora.isoformat(timespec="seconds")
                session.permanent = True
                g.usuario = fila
                destino = "mi_clave" if fila["debe_cambiar_clave"] else "inicio"
                return redirect(url_for(destino))
            if registrar_fallo_login(db, buckets, ahora):
                error = "Demasiados intentos. Espera 15 minutos antes de volver a ingresar."
            else:
                error = "Usuario o contraseña incorrectos."
    return render_template("login.html", error=error, usuario_escrito=usuario_escrito)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/mi-clave", methods=["GET", "POST"])
def mi_clave():
    """Permite al usuario renovar una clave temporal o cambiar la propia."""
    datos, errores = {}, []
    if request.method == "POST":
        datos, clave, errores = leer_datos_usuario(request.form, pedir_usuario=False)
        if not errores and check_password_hash(g.usuario["clave_hash"], clave):
            errores.append("La nueva contraseña debe ser distinta de la actual.")
        if errores:
            return render_template("mi_clave.html", errores=errores)

        db = get_db()
        ahora = datetime.now().isoformat(timespec="seconds")
        with db:
            db.execute(
                """UPDATE usuarios SET clave_hash = ?, clave_cambiada_en = ?,
                     debe_cambiar_clave = 0, version_sesion = version_sesion + 1
                   WHERE id = ?""",
                (generate_password_hash(clave), ahora, g.usuario["id"]),
            )
        g.usuario = db.execute("SELECT * FROM usuarios WHERE id = ?", (g.usuario["id"],)).fetchone()
        session["version_sesion"] = g.usuario["version_sesion"]
        session["actividad_sesion"] = ahora
        session.permanent = True
        registrar("Cambió una contraseña", g.usuario["nombre"])
        flash("Contraseña actualizada. Ya puedes continuar.", "ok")
        return redirect(pagina_de_inicio())
    return render_template("mi_clave.html", errores=errores)


@app.route("/usuarios", methods=["GET", "POST"])
@solo_dueno
def usuarios():
    db = get_db()
    datos, errores = {"rol": "empleado"}, []
    if request.method == "POST":
        datos, clave, errores = leer_datos_usuario(request.form)
        if not errores:
            try:
                with db:
                    crear_usuario(db, datos["usuario"], datos["nombre"], clave, datos["rol"])
            except sqlite3.IntegrityError:
                errores.append(f"Ya existe el usuario «{datos['usuario']}».")
            else:
                registrar("Creó un usuario", f"{datos['nombre']} ({NOMBRES_ROL[datos['rol']]})")
                flash(f"Usuario {datos['usuario']} creado.", "ok")
                return redirect(url_for("usuarios"))
    filas = db.execute(
        """SELECT u.*, (SELECT MAX(a.momento) FROM actividad a WHERE a.usuario_id = u.id) AS ultima_actividad
           FROM usuarios u ORDER BY u.activo DESC, u.rol, u.nombre COLLATE NOCASE"""
    ).fetchall()
    return render_template("usuarios.html", usuarios=filas, datos=datos, errores=errores)


@app.route("/usuarios/<int:usuario_id>/clave", methods=["POST"])
@solo_dueno
def usuario_cambiar_clave(usuario_id):
    usuario = obtener_o_404("usuarios", usuario_id)
    _datos, clave, errores = leer_datos_usuario(request.form, pedir_usuario=False)
    if errores:
        for error in errores:
            flash(f"{usuario['nombre']}: {error}", "error")
    else:
        db = get_db()
        with db:
            db.execute(
                """UPDATE usuarios SET clave_hash = ?, clave_cambiada_en = ?,
                     version_sesion = version_sesion + 1, debe_cambiar_clave = 1 WHERE id = ?""",
                (generate_password_hash(clave), datetime.now().isoformat(timespec="seconds"), usuario_id),
            )
        registrar("Cambió una contraseña", usuario["nombre"])
        flash(f"Contraseña de {usuario['nombre']} cambiada.", "ok")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/<int:usuario_id>/activo", methods=["POST"])
@solo_dueno
def usuario_activar(usuario_id):
    """Activa o desactiva un usuario. No se borran, para conservar quién hizo qué."""
    usuario = obtener_o_404("usuarios", usuario_id)
    db = get_db()
    if usuario["activo"]:
        duenos_activos = db.execute(
            "SELECT COUNT(*) AS total FROM usuarios WHERE rol = 'dueno' AND activo = 1"
        ).fetchone()["total"]
        if usuario_id == g.usuario["id"]:
            flash("No puedes desactivar tu propio usuario.", "error")
            return redirect(url_for("usuarios"))
        if usuario["rol"] == "dueno" and duenos_activos <= 1:
            flash("Debe quedar al menos un dueño activo.", "error")
            return redirect(url_for("usuarios"))
    with db:
        db.execute(
            "UPDATE usuarios SET activo = ?, version_sesion = version_sesion + 1 WHERE id = ?",
            (0 if usuario["activo"] else 1, usuario_id),
        )
    accion = "Desactivó un usuario" if usuario["activo"] else "Reactivó un usuario"
    registrar(accion, usuario["nombre"])
    flash(f"{accion}: {usuario['nombre']}.", "ok")
    return redirect(url_for("usuarios"))


def leer_fecha(texto, por_defecto):
    try:
        return date.fromisoformat(texto or "")
    except ValueError:
        return por_defecto


def rango_de_mes(meses_atras=0):
    """Primer y último día de un mes (el actual termina hoy)."""
    hoy = date.today()
    anio, mes = divmod(hoy.year * 12 + hoy.month - 1 - meses_atras, 12)
    inicio = date(anio, mes + 1, 1)
    siguiente = date(anio + (mes == 11), (mes + 1) % 12 + 1, 1)
    return inicio, min(siguiente - timedelta(days=1), hoy)


@app.route("/actividad")
@solo_dueno
def actividad():
    """Trabajo de cada persona en un período: métricas arriba y el detalle abajo."""
    usuario_id = request.args.get("usuario", type=int)
    incluir_admin = request.args.get("todo") == "1"
    inicio_mes, _ = rango_de_mes()
    desde = leer_fecha(request.args.get("desde"), inicio_mes)  # Por defecto, el mes en curso.
    hasta = leer_fecha(request.args.get("hasta"), date.today())
    if desde > hasta:
        desde, hasta = hasta, desde

    marcadores = ", ".join("?" for _ in ACCIONES_PRODUCCION)
    # Condiciones sobre la actividad: período y, salvo que se pida, solo producción.
    periodo = ["substr(a.momento, 1, 10) >= ?", "substr(a.momento, 1, 10) <= ?"]
    parametros_periodo = [desde.isoformat(), hasta.isoformat()]
    produccion = periodo + [f"a.accion IN ({marcadores})"]
    parametros_produccion = parametros_periodo + list(ACCIONES_PRODUCCION)

    condiciones = list(produccion if not incluir_admin else periodo)
    parametros = list(parametros_produccion if not incluir_admin else parametros_periodo)
    if usuario_id:
        condiciones.append("a.usuario_id = ?")
        parametros.append(usuario_id)
    filtro = " WHERE " + " AND ".join(condiciones)

    db = get_db()
    # Rendimiento por persona (siempre solo producción). Las personas activas aparecen
    # aunque no hayan hecho nada en el período, para que también se vea un cero.
    filtro_persona = " AND u.id = ?" if usuario_id else ""
    rendimiento = db.execute(
        f"""SELECT u.id, u.nombre, u.rol,
                   COUNT(a.id) AS acciones,
                   COALESCE(SUM(a.accion = 'Creó la orden'), 0) AS creadas,
                   COUNT(DISTINCT a.orden_id) AS gestionadas,
                   COALESCE(SUM(a.accion = 'Cambió el estado'), 0) AS cambios,
                   COALESCE(SUM(a.accion = 'Registró un cliente'), 0) AS clientes
            FROM usuarios u
            LEFT JOIN actividad a ON a.usuario_id = u.id AND {" AND ".join(produccion)}
            WHERE 1 = 1{filtro_persona}
            GROUP BY u.id
            HAVING u.activo = 1 OR COUNT(a.id) > 0
            ORDER BY creadas DESC, gestionadas DESC, u.nombre COLLATE NOCASE""",
        (*parametros_produccion, *([usuario_id] if usuario_id else [])),
    ).fetchall()
    # Órdenes distintas en total (una orden tocada por dos personas cuenta una vez).
    gestionadas = db.execute(
        f"""SELECT COUNT(DISTINCT a.orden_id) AS total FROM actividad a
            WHERE {" AND ".join(produccion)}{" AND a.usuario_id = ?" if usuario_id else ""}""",
        (*parametros_produccion, *([usuario_id] if usuario_id else [])),
    ).fetchone()["total"]
    metricas = {
        "creadas": sum(fila["creadas"] for fila in rendimiento),
        "gestionadas": gestionadas,
        "cambios": sum(fila["cambios"] for fila in rendimiento),
        "clientes": sum(fila["clientes"] for fila in rendimiento),
    }

    total = db.execute("SELECT COUNT(*) AS total FROM actividad a" + filtro, parametros).fetchone()["total"]
    paginacion = paginar(total)
    filas = db.execute(
        """SELECT a.*, u.nombre AS usuario_nombre
           FROM actividad a LEFT JOIN usuarios u ON u.id = a.usuario_id"""
        + filtro
        + " ORDER BY a.momento DESC, a.id DESC LIMIT ? OFFSET ?",
        (*parametros, POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    personas = db.execute(
        "SELECT id, nombre, rol FROM usuarios ORDER BY rol DESC, nombre COLLATE NOCASE"
    ).fetchall()
    persona = next((p for p in personas if p["id"] == usuario_id), None)
    return render_template(
        "actividad.html",
        registros=filas,
        personas=personas,
        persona=persona,
        usuario_id=usuario_id,
        desde=desde,
        hasta=hasta,
        incluir_admin=incluir_admin,
        rendimiento=rendimiento,
        metricas=metricas,
        este_mes=rango_de_mes(0),
        mes_anterior=rango_de_mes(1),
        paginacion=paginacion,
    )


# ---------------------------------------------------------------------------
# Reporte trimestral (para el dueño): gráficos por mes y versión para PDF
# ---------------------------------------------------------------------------
def meses_del_trimestre(anio, trimestre):
    return _meses_del_trimestre(anio, trimestre, NOMBRES_MES)


def taller_por_mes(meses):
    return _taller_por_mes(get_db(), meses)


def cobros_por_medio(meses):
    return _cobros_por_medio(get_db(), meses, MEDIOS_PAGO)


def equipo_por_mes(meses):
    return _equipo_por_mes(get_db(), meses, ACCIONES_PRODUCCION)


def datos_reporte(anio, trimestre):
    """Conserva el contrato de app.py y delega las consultas trimestrales."""
    return _datos_reporte(
        get_db(),
        anio,
        trimestre,
        nombres_mes=NOMBRES_MES,
        medios_pago=MEDIOS_PAGO,
        acciones_produccion=ACCIONES_PRODUCCION,
    )


@app.route("/reportes")
@solo_dueno
def reportes():
    """Reporte trimestral: cómo le fue al taller y a cada persona, mes a mes."""
    reporte = datos_reporte(*leer_trimestre(request.args.get("trimestre")))
    if reporte["es_futuro"]:
        return redirect(url_for("reportes"))
    return render_template("reportes.html", reporte=reporte)


@app.route("/reportes/imprimir")
@solo_dueno
def reporte_imprimir():
    """Versión A4 del reporte, para imprimir o guardar como PDF desde el navegador."""
    reporte = datos_reporte(*leer_trimestre(request.args.get("trimestre")))
    return render_template("reporte_imprimir.html", reporte=reporte)


# ---------------------------------------------------------------------------
# Analítica de negocio: actividad, rentabilidad estimada y flujo real de caja
# ---------------------------------------------------------------------------
def estado_analitica_actual(db):
    """Adaptador compatible para los calculos actuales de cuentas y stock."""
    return _estado_analitica_actual(
        db,
        select_orden=SELECT_ORDEN,
        importe_linea_compra_centavos=importe_linea_compra_centavos,
        dias_atraso=DIAS_ATRASO,
    )


def datos_analitica(rango):
    """Conserva la interfaz de app.py y delega la consulta al servicio."""
    return _datos_analitica(
        get_db(),
        rango,
        select_orden=SELECT_ORDEN,
        importe_linea_compra_centavos=importe_linea_compra_centavos,
        dias_atraso=DIAS_ATRASO,
    )


@app.route("/analitica")
@solo_dueno
def analitica():
    rango = rango_analitica(request.args)
    datos = datos_analitica(rango)
    return render_template("analitica.html", analitica=datos)


@app.route("/analitica/detalle")
@solo_dueno
def analitica_detalle():
    """Abre los registros fuente que componen un indicador del período."""
    tipo = request.args.get("tipo", "").strip()
    titulos = {
        "produccion": "Órdenes completadas",
        "costos": "Costos consumidos en órdenes",
        "cobros": "Cobros y reversiones",
        "gastos": "Gastos y reversiones",
        "resultado": "Detalle del resultado estimado",
        "margen": "Detalle del margen antes de gastos",
        "flujo": "Movimientos que componen el flujo de caja",
        "movimientos": "Producción y movimientos del período",
        "ordenes": "Órdenes ingresadas y completadas",
        "trabajos": "Ventas por trabajo o repuesto",
    }
    if tipo not in titulos:
        abort(404)

    rango = rango_analitica(request.args)
    desde, hasta = rango["desde"].isoformat(), rango["hasta"].isoformat()
    filtro_medio = request.args.get("medio", "").strip()
    if filtro_medio not in MEDIOS_PAGO:
        filtro_medio = ""
    filtro_rubro = request.args.get("categoria", "").strip()
    if filtro_rubro not in RUBROS_GASTO:
        filtro_rubro = ""
    descripcion = request.args.get("descripcion", "").strip()[:120]
    item_tipo = request.args.get("item_tipo", "").strip()
    if item_tipo not in ("Repuesto", "Servicio"):
        item_tipo = ""

    produccion = """SELECT o.fecha_completado AS fecha, 'Trabajo terminado' AS movimiento,
               'OT-' || printf('%05d', o.id) AS documento, c.nombre AS cliente,
               v.patente, 'Orden completada' AS concepto,
               COALESCE(SUM(i.subtotal_centavos), 0) AS importe_centavos,
               o.id AS orden_id, NULL AS compra_id, NULL AS gasto_id, '' AS referencia
        FROM ordenes o JOIN vehiculos v ON v.id = o.vehiculo_id
        JOIN clientes c ON c.id = v.cliente_id
        LEFT JOIN orden_items i ON i.orden_id = o.id AND i.vigente = 1
        WHERE o.estado = 'Completado' AND o.fecha_completado BETWEEN ? AND ?
          AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
        GROUP BY o.id"""
    parametros_produccion = [desde, hasta]

    costos = """SELECT o.fecha_completado AS fecha, 'Costo consumido' AS movimiento,
               'OT-' || printf('%05d', o.id) AS documento, c.nombre AS cliente,
               v.patente, i.descripcion AS concepto,
               COALESCE(i.costo_total_centavos, 0) AS importe_centavos,
               o.id AS orden_id, NULL AS compra_id, NULL AS gasto_id,
               CASE WHEN i.costo_total_centavos IS NULL THEN 'Costo pendiente de cargar' ELSE '' END AS referencia
        FROM ordenes o JOIN vehiculos v ON v.id = o.vehiculo_id
        JOIN clientes c ON c.id = v.cliente_id
        JOIN orden_items i ON i.orden_id = o.id AND i.vigente = 1
        WHERE o.estado = 'Completado' AND o.fecha_completado BETWEEN ? AND ?
          AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
          AND (i.costo_total_centavos IS NOT NULL OR (i.tipo = 'Repuesto' AND i.subtotal_centavos > 0))"""
    parametros_costos = [desde, hasta]
    costos_resultado = costos.replace(
        "COALESCE(i.costo_total_centavos, 0) AS importe_centavos",
        "-COALESCE(i.costo_total_centavos, 0) AS importe_centavos",
    )

    cobros = """SELECT p.fecha,
               CASE WHEN p.tipo = 'reversion' THEN 'Reversión de cobro' ELSE 'Cobro de orden' END AS movimiento,
               'OT-' || printf('%05d', o.id) AS documento, c.nombre AS cliente,
               v.patente, p.medio AS concepto,
               CASE WHEN p.tipo = 'reversion' AND original.tipo = 'cobro'
                    THEN -p.importe_centavos ELSE p.importe_centavos END AS importe_centavos,
               o.id AS orden_id, NULL AS compra_id, NULL AS gasto_id,
               COALESCE(NULLIF(p.motivo, ''), p.referencia, '') AS referencia
        FROM orden_pagos p LEFT JOIN orden_pagos original ON original.id = p.revierte_id
        JOIN ordenes o ON o.id = p.orden_id JOIN vehiculos v ON v.id = o.vehiculo_id
        JOIN clientes c ON c.id = v.cliente_id
        WHERE p.fecha BETWEEN ? AND ?
          AND (p.tipo = 'cobro' OR (p.tipo = 'reversion' AND original.tipo = 'cobro'))
          AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)"""
    parametros_cobros = [desde, hasta]
    if filtro_medio:
        cobros += " AND p.medio = ?"
        parametros_cobros.append(filtro_medio)

    gastos = """SELECT g.fecha, 'Gasto' AS movimiento,
               'G-' || printf('%05d', g.id) AS documento, '' AS cliente, '' AS patente,
               g.categoria || ' · ' || g.concepto AS concepto,
               g.importe_centavos AS importe_centavos, NULL AS orden_id, NULL AS compra_id,
               g.id AS gasto_id, g.referencia AS referencia
        FROM gastos g WHERE g.fecha BETWEEN ? AND ?
        UNION ALL
        SELECT r.fecha, 'Reversión de gasto' AS movimiento,
               'G-' || printf('%05d', g.id) AS documento, '' AS cliente, '' AS patente,
               g.categoria || ' · ' || g.concepto AS concepto,
               -g.importe_centavos AS importe_centavos, NULL AS orden_id, NULL AS compra_id,
               g.id AS gasto_id, r.motivo AS referencia
        FROM gasto_reversiones r JOIN gastos g ON g.id = r.gasto_id WHERE r.fecha BETWEEN ? AND ?"""
    parametros_gastos = [desde, hasta, desde, hasta]
    if filtro_rubro:
        gastos = gastos.replace("FROM gastos g WHERE", "FROM gastos g WHERE g.categoria = ? AND")
        gastos = gastos.replace("FROM gasto_reversiones r JOIN gastos g ON g.id = r.gasto_id WHERE",
                                "FROM gasto_reversiones r JOIN gastos g ON g.id = r.gasto_id WHERE g.categoria = ? AND")
        parametros_gastos = [filtro_rubro, desde, hasta, filtro_rubro, desde, hasta]
    # Para el resultado/flujo, un gasto resta y su reversión suma.
    consulta_gasto, consulta_reversion = gastos.split("UNION ALL", 1)
    gastos_resultado = (
        consulta_gasto.replace(
            "g.importe_centavos AS importe_centavos", "-g.importe_centavos AS importe_centavos", 1
        )
        + " UNION ALL "
        + consulta_reversion.replace(
            "-g.importe_centavos AS importe_centavos", "g.importe_centavos AS importe_centavos", 1
        )
    )

    pagos_proveedor = """SELECT pp.fecha,
               CASE WHEN pp.tipo = 'reversion' THEN 'Reversión de pago a proveedor' ELSE 'Pago a proveedor' END AS movimiento,
               'C-' || printf('%05d', c.id) AS documento, p.nombre AS cliente, '' AS patente,
               pp.medio AS concepto,
               CASE WHEN pp.tipo = 'reversion' THEN pp.importe_centavos ELSE -pp.importe_centavos END AS importe_centavos,
               NULL AS orden_id, c.id AS compra_id, NULL AS gasto_id,
               COALESCE(NULLIF(pp.motivo, ''), pp.referencia, '') AS referencia
        FROM proveedor_pagos pp JOIN compras c ON c.id = pp.compra_id
        JOIN proveedores p ON p.id = c.proveedor_id
        WHERE pp.fecha BETWEEN ? AND ? AND c.estado != 'Anulada'"""
    parametros_pagos_proveedor = [desde, hasta]

    ordenes_ingresadas = """SELECT o.fecha_ingreso AS fecha, 'Orden ingresada' AS movimiento,
               'OT-' || printf('%05d', o.id) AS documento, c.nombre AS cliente,
               v.patente, o.fallas AS concepto, 0 AS importe_centavos,
               o.id AS orden_id, NULL AS compra_id, NULL AS gasto_id, o.estado AS referencia
        FROM ordenes o JOIN vehiculos v ON v.id = o.vehiculo_id
        JOIN clientes c ON c.id = v.cliente_id
        WHERE o.fecha_ingreso BETWEEN ? AND ?
          AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)"""

    trabajos = """SELECT o.fecha_completado AS fecha, i.tipo AS movimiento,
               'OT-' || printf('%05d', o.id) AS documento, c.nombre AS cliente,
               v.patente, i.descripcion AS concepto, i.subtotal_centavos AS importe_centavos,
               o.id AS orden_id, NULL AS compra_id, NULL AS gasto_id,
               printf('Cantidad: %.3f', i.cantidad) AS referencia
        FROM ordenes o JOIN vehiculos v ON v.id = o.vehiculo_id
        JOIN clientes c ON c.id = v.cliente_id
        JOIN orden_items i ON i.orden_id = o.id AND i.vigente = 1
        WHERE o.estado = 'Completado' AND o.fecha_completado BETWEEN ? AND ?
          AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)"""

    consulta = None
    parametros = []
    if tipo == "produccion":
        consulta, parametros = produccion, parametros_produccion
    elif tipo == "costos":
        consulta, parametros = costos, parametros_costos
    elif tipo == "cobros":
        consulta, parametros = cobros, parametros_cobros
    elif tipo == "gastos":
        consulta, parametros = gastos, parametros_gastos
    elif tipo == "resultado":
        consulta = " UNION ALL ".join((produccion, costos_resultado, gastos_resultado))
        parametros = parametros_produccion + parametros_costos + parametros_gastos
    elif tipo == "margen":
        consulta = " UNION ALL ".join((produccion, costos_resultado))
        parametros = parametros_produccion + parametros_costos
    elif tipo == "flujo":
        consulta = " UNION ALL ".join((cobros, gastos_resultado, pagos_proveedor))
        parametros = parametros_cobros + parametros_gastos + parametros_pagos_proveedor
    elif tipo == "movimientos":
        consulta = " UNION ALL ".join((produccion, cobros, gastos_resultado, pagos_proveedor))
        parametros = parametros_produccion + parametros_cobros + parametros_gastos + parametros_pagos_proveedor
    elif tipo == "ordenes":
        completadas = produccion.replace("'Trabajo terminado'", "'Orden completada'").replace(
            "COALESCE(SUM(i.subtotal_centavos), 0) AS importe_centavos", "0 AS importe_centavos"
        ).replace("'Orden completada' AS concepto", "'Trabajo terminado' AS concepto")
        consulta = " UNION ALL ".join((ordenes_ingresadas, completadas))
        parametros = [desde, hasta] + parametros_produccion
    elif tipo == "trabajos":
        consulta = trabajos
        parametros = [desde, hasta]
        if descripcion:
            consulta += " AND i.descripcion = ?"
            parametros.append(descripcion)
        if item_tipo:
            consulta += " AND i.tipo = ?"
            parametros.append(item_tipo)
    if filtro_medio and tipo == "cobros":
        titulos[tipo] += f" · {filtro_medio}"
    if filtro_rubro and tipo == "gastos":
        titulos[tipo] += f" · {filtro_rubro}"
    if descripcion and tipo == "trabajos":
        titulos[tipo] += f" · {descripcion}"

    db = get_db()
    resumen = db.execute(
        f"SELECT COUNT(*) AS cantidad, COALESCE(SUM(importe_centavos), 0) AS total FROM ({consulta})",
        parametros,
    ).fetchone()
    paginacion = paginar(resumen["cantidad"])
    filas = db.execute(
        f"SELECT * FROM ({consulta}) ORDER BY fecha DESC, documento DESC LIMIT ? OFFSET ?",
        (*parametros, POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    filas = [dict(fila) for fila in filas]
    for fila in filas:
        if fila["orden_id"]:
            fila["url"] = url_for("orden_detalle", orden_id=fila["orden_id"])
        elif fila["compra_id"]:
            fila["url"] = url_for("compra_detalle", compra_id=fila["compra_id"])
        elif fila["gasto_id"]:
            fila["url"] = url_for("gastos", gasto_id=fila["gasto_id"]) + f"#gasto-{fila['gasto_id']}"
        else:
            fila["url"] = ""

    total_label = {
        "produccion": "Trabajo terminado",
        "costos": "Costo registrado",
        "cobros": "Cobro neto",
        "gastos": "Gasto neto",
        "resultado": "Resultado estimado",
        "margen": "Margen antes de gastos",
        "flujo": "Flujo neto de caja",
        "trabajos": "Importe de las líneas",
    }.get(tipo, "")
    return render_template(
        "analitica_detalle.html", tipo=tipo, titulo=titulos[tipo],
        desde=desde, hasta=hasta, filas=filas, paginacion=paginacion,
        cantidad=resumen["cantidad"], total_centavos=resumen["total"],
        total_label=total_label, mostrar_total=tipo not in ("movimientos", "ordenes"),
        filtros={"medio": filtro_medio, "categoria": filtro_rubro,
                 "descripcion": descripcion, "item_tipo": item_tipo},
        hoy_iso=date.today().isoformat(),
    )


# ---------------------------------------------------------------------------
# Formato para mostrar datos
# ---------------------------------------------------------------------------
@app.template_filter("momento")
def filtro_momento(iso):
    return datetime.fromisoformat(iso).strftime("%d/%m/%Y %H:%M") if iso else ""


@app.template_filter("turno")
def filtro_turno(iso):
    return "Mañana" if datetime.fromisoformat(iso).hour < HORA_CAMBIO_TURNO else "Tarde"


@app.template_filter("dinero")
def filtro_dinero(centavos):
    texto = f"{Decimal(centavos or 0) / 100:,.2f}"
    return "$" + texto.replace(",", "§").replace(".", ",").replace("§", ".")


@app.template_filter("importe_input")
def filtro_importe_input(centavos):
    return f"{Decimal(centavos or 0) / 100:.2f}"


def importe_opcional(centavos):
    """Como importe_input, pero un costo no cargado (None) queda como campo vacío."""
    return "" if centavos is None else filtro_importe_input(centavos)


@app.template_filter("fecha")
def filtro_fecha(iso):
    if not iso:
        return ""
    return date.fromisoformat(iso).strftime("%d/%m/%Y")


@app.template_filter("num_ot")
def filtro_num_ot(orden_id):
    return f"OT-{orden_id:05d}"


@app.template_filter("cantidad")
def filtro_cantidad(valor):
    return f"{valor:.10g}".replace(".", ",")


@app.template_filter("cantidad_stock")
def filtro_cantidad_stock(milesimas):
    valor = Decimal(milesimas or 0) / 1000
    return f"{valor:g}".replace(".", ",")


# ---------------------------------------------------------------------------
# Lectura y validación de formularios
# ---------------------------------------------------------------------------
def a_decimal(texto):
    """Convierte texto a un Decimal no negativo. Lanza ValueError si no es válido."""
    try:
        valor = Decimal(str(texto).strip().replace(",", "."))
    except InvalidOperation as error:
        raise ValueError("número inválido") from error
    if not valor.is_finite() or valor < 0 or valor > MAXIMO_IMPORTE:
        raise ValueError("número fuera de rango")
    return valor


def a_centavos(valor):
    return int((valor * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def a_milesimas(texto, permitir_cero=False):
    """Convierte cantidades de inventario a enteros de milésimas, sin redondeo silencioso."""
    valor = a_decimal(texto)
    if (valor == 0 and not permitir_cero) or valor > MAXIMA_CANTIDAD:
        raise ValueError("cantidad fuera de rango")
    if valor != valor.quantize(Decimal("0.001")):
        raise ValueError("usa hasta tres decimales")
    return int(valor * 1000)


def stock_articulo(articulo_id, db=None):
    return _stock_articulo(db or get_db(), articulo_id)


def costo_promedio_articulo(articulo_id, db=None):
    return _costo_promedio_articulo(db or get_db(), articulo_id)


def insertar_movimiento_stock(
    db, articulo_id, tipo, cantidad_milesimas, costo_centavos=None, motivo="",
    orden_item_id=None, recepcion_item_id=None, revierte_id=None,
):
    usuario = g.get("usuario")
    return _insertar_movimiento_stock(
        db, articulo_id, tipo, cantidad_milesimas, costo_centavos, motivo,
        orden_item_id, recepcion_item_id, revierte_id,
        usuario_id=usuario["id"] if usuario else None,
        creado_en=datetime.now().isoformat(timespec="seconds"),
    )


def normalizar_patente(texto):
    return re.sub(r"[\s\-.]", "", texto or "").upper()


def leer_cliente(form):
    datos = {
        campo: form.get(campo, "").strip()
        for campo in ("nombre", "documento", "telefono", "email", "direccion")
    }
    errores = []
    if not datos["nombre"]:
        errores.append("El nombre del cliente es obligatorio.")
    if datos["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", datos["email"]):
        errores.append("El email no tiene un formato válido.")
    return datos, errores


def leer_vehiculo(patente, marca, modelo, anio, color):
    datos = {
        "patente": normalizar_patente(patente),
        "marca": (marca or "").strip(),
        "modelo": (modelo or "").strip(),
        "anio": (anio or "").strip(),
        "color": (color or "").strip(),
    }
    errores = []
    etiqueta = datos["patente"] or "sin patente"
    if not re.fullmatch(r"[A-Z0-9]{4,10}", datos["patente"]):
        errores.append(f"Vehículo ({etiqueta}): la patente debe tener de 4 a 10 letras o números.")
    if not datos["marca"] or not datos["modelo"]:
        errores.append(f"Vehículo ({etiqueta}): la marca y el modelo son obligatorios.")
    if datos["anio"]:
        anio_maximo = date.today().year + 1
        if not datos["anio"].isdecimal() or not 1900 <= int(datos["anio"]) <= anio_maximo:
            errores.append(f"Vehículo ({etiqueta}): el año debe estar entre 1900 y {anio_maximo}.")
    return datos, errores


def anio_para_db(datos):
    return int(datos["anio"]) if datos["anio"] else None


def rango_de_caja():
    hoy = date.today()
    desde_defecto = hoy.replace(day=1).isoformat()
    hasta_defecto = hoy.isoformat()
    desde_texto = request.args.get("desde", desde_defecto)
    hasta_texto = request.args.get("hasta", hasta_defecto)
    try:
        desde = date.fromisoformat(desde_texto).isoformat()
        hasta = date.fromisoformat(hasta_texto).isoformat()
    except ValueError:
        desde, hasta = desde_defecto, hasta_defecto
    if desde > hasta:
        desde, hasta = hasta, desde
    return desde, hasta


def consulta_movimientos_caja():
    """Consulta común para el libro paginado y la exportación completa del período."""
    return """SELECT p.id, p.fecha, 'orden' AS origen, o.id AS orden_id, NULL AS compra_id,
                      c.nombre || ' · ' || v.patente AS detalle,
                      CASE WHEN p.tipo = 'reversion' THEN 'Reversión de cobro' ELSE 'Cobro de orden' END AS tipo_label,
                      p.medio, u.nombre AS usuario_nombre,
                      CASE WHEN p.tipo = 'reversion' THEN -p.importe_centavos ELSE p.importe_centavos END AS importe_neto,
                      COALESCE(NULLIF(p.motivo, ''), p.referencia, '') AS referencia
               FROM orden_pagos p LEFT JOIN orden_pagos original ON original.id = p.revierte_id
               JOIN ordenes o ON o.id = p.orden_id JOIN vehiculos v ON v.id = o.vehiculo_id
               JOIN clientes c ON c.id = v.cliente_id LEFT JOIN usuarios u ON u.id = p.usuario_id
               WHERE p.fecha BETWEEN ? AND ?
                 AND (p.tipo = 'cobro' OR (p.tipo = 'reversion' AND original.tipo = 'cobro'))
               UNION ALL
               SELECT g.id, g.fecha, 'gasto', NULL, NULL, g.concepto,
                      'Gasto · ' || g.categoria, g.medio, u.nombre, -g.importe_centavos, g.referencia
               FROM gastos g LEFT JOIN usuarios u ON u.id = g.usuario_id WHERE g.fecha BETWEEN ? AND ?
               UNION ALL
               SELECT r.id, r.fecha, 'gasto', NULL, NULL, g.concepto,
                      'Reversión de gasto', g.medio, u.nombre, g.importe_centavos, r.motivo
               FROM gasto_reversiones r JOIN gastos g ON g.id = r.gasto_id
               LEFT JOIN usuarios u ON u.id = r.usuario_id WHERE r.fecha BETWEEN ? AND ?
               UNION ALL
               SELECT pp.id, pp.fecha, 'proveedor', NULL, c.id,
                      p.nombre || ' · C-' || printf('%05d', c.id),
                      CASE WHEN pp.tipo = 'reversion' THEN 'Reversión de pago a proveedor' ELSE 'Pago a proveedor' END,
                      pp.medio, u.nombre,
                      CASE WHEN pp.tipo = 'reversion' THEN pp.importe_centavos ELSE -pp.importe_centavos END,
                      COALESCE(NULLIF(pp.motivo, ''), pp.referencia, '')
               FROM proveedor_pagos pp JOIN compras c ON c.id = pp.compra_id
               JOIN proveedores p ON p.id = c.proveedor_id LEFT JOIN usuarios u ON u.id = pp.usuario_id
               WHERE pp.fecha BETWEEN ? AND ?"""


@app.route("/caja")
@solo_dueno
def caja():
    """Libro consolidado de ingresos, gastos y pagos reales, por fecha."""
    db = get_db()
    desde, hasta = rango_de_caja()
    union = consulta_movimientos_caja()
    parametros = (desde, hasta) * 4
    busqueda = request.args.get("q", "").strip()[:120]
    consulta_filtrada = "SELECT * FROM (" + union + ")"
    parametros_filtro = list(parametros)
    if busqueda:
        patron = f"%{busqueda}%"
        consulta_filtrada += " WHERE fecha LIKE ? OR origen LIKE ? OR detalle LIKE ? OR tipo_label LIKE ? OR referencia LIKE ? OR medio LIKE ? OR usuario_nombre LIKE ? OR CAST(orden_id AS TEXT) LIKE ? OR CAST(compra_id AS TEXT) LIKE ?"
        parametros_filtro.extend([patron] * 9)
    total = db.execute(
        "SELECT COUNT(*) AS total FROM (" + consulta_filtrada + ")", parametros_filtro
    ).fetchone()["total"]
    paginacion = paginar(total)
    movimientos = db.execute(
        consulta_filtrada + " ORDER BY fecha DESC, origen, id DESC LIMIT ? OFFSET ?",
        (*parametros_filtro, POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    totales = db.execute(
        "SELECT medio, SUM(importe_neto) AS total, COUNT(*) AS movimientos FROM (" + union + ") GROUP BY medio ORDER BY medio",
        parametros,
    ).fetchall()
    cierre = db.execute(
        "SELECT * FROM caja_cierres WHERE fecha = ? ORDER BY id DESC LIMIT 1", (hasta,)
    ).fetchone()
    cierre_previo = db.execute(
        "SELECT * FROM caja_cierres WHERE estado = 'cerrado' AND fecha < ? ORDER BY fecha DESC, id DESC LIMIT 1",
        (hasta,),
    ).fetchone()
    movimientos_desde = (
        (date.fromisoformat(cierre_previo["fecha"]) + timedelta(days=1)).isoformat()
        if cierre_previo else hasta
    )
    total_cierres = db.execute("SELECT COUNT(*) AS total FROM caja_cierres").fetchone()["total"]
    paginacion_cierres = paginar(total_cierres, "pagina_cierres")
    cierres = db.execute(
        """SELECT cc.*, u.nombre AS usuario_nombre, ru.nombre AS reabierto_por_nombre
           FROM caja_cierres cc LEFT JOIN usuarios u ON u.id = cc.usuario_id
           LEFT JOIN usuarios ru ON ru.id = cc.reabierto_por
           ORDER BY cc.fecha DESC, cc.id DESC LIMIT ? OFFSET ?""",
        (POR_PAGINA, paginacion_cierres["offset"]),
    ).fetchall()
    return render_template(
        "caja.html", movimientos=movimientos, totales=totales, desde=desde,
        hasta=hasta, paginacion=paginacion,
        total_neto=sum(fila["total"] for fila in totales),
        cierre=cierre, cierre_previo=cierre_previo,
        cierres=cierres, paginacion_cierres=paginacion_cierres,
        movimientos_desde=movimientos_desde,
        efectivo_dia=efectivo_neto_periodo(db, movimientos_desde, hasta),
        busqueda=busqueda,
    )


@app.route("/caja/exportar.csv")
@solo_dueno
def caja_exportar_csv():
    """Descarga todos los movimientos del rango indicado, sin límite de paginación."""
    desde, hasta = rango_de_caja()
    filas = get_db().execute(
        "SELECT * FROM (" + consulta_movimientos_caja() + ") ORDER BY fecha, origen, id",
        (desde, hasta) * 4,
    ).fetchall()
    salida = io.StringIO(newline="")
    escritor = csv.writer(salida, delimiter=";")
    escritor.writerow(("Fecha", "Origen", "Detalle", "Movimiento", "Medio", "Registró", "Importe neto (centavos)", "Referencia"))
    def campo_csv(valor):
        texto = str(valor or "")
        return "'" + texto if texto.lstrip().startswith(("=", "+", "-", "@")) else texto

    for fila in filas:
        escritor.writerow((
            fila["fecha"], campo_csv(fila["origen"]), campo_csv(fila["detalle"]),
            campo_csv(fila["tipo_label"]), campo_csv(fila["medio"]),
            campo_csv(fila["usuario_nombre"]), fila["importe_neto"], campo_csv(fila["referencia"]),
        ))
    contenido = io.BytesIO(("\ufeff" + salida.getvalue()).encode("utf-8"))
    return send_file(
        contenido,
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name=f"caja_{desde}_{hasta}.csv",
    )


def caja_fecha_cerrada(fecha_texto, db=None):
    db = db or get_db()
    return db.execute(
        "SELECT 1 FROM caja_cierres WHERE fecha = ? AND estado = 'cerrado' LIMIT 1",
        (fecha_texto,),
    ).fetchone() is not None


def efectivo_neto_periodo(db, desde, hasta):
    cobros = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN p.tipo = 'reversion' THEN -p.importe_centavos
                                    ELSE p.importe_centavos END), 0) AS total
           FROM orden_pagos p LEFT JOIN orden_pagos original ON original.id = p.revierte_id
           WHERE p.fecha BETWEEN ? AND ? AND p.medio = 'Efectivo'
             AND (p.tipo = 'cobro' OR (p.tipo = 'reversion' AND original.tipo = 'cobro'))""",
        (desde, hasta),
    ).fetchone()["total"]
    gastos = db.execute(
        """SELECT COALESCE((SELECT -SUM(importe_centavos) FROM gastos WHERE fecha BETWEEN ? AND ? AND medio = 'Efectivo'), 0)
                  + COALESCE((SELECT SUM(g.importe_centavos) FROM gasto_reversiones r
                              JOIN gastos g ON g.id = r.gasto_id
                              WHERE r.fecha BETWEEN ? AND ? AND g.medio = 'Efectivo'), 0) AS total""",
        (desde, hasta, desde, hasta),
    ).fetchone()["total"]
    pagos = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN tipo = 'reversion' THEN importe_centavos
                                    ELSE -importe_centavos END), 0) AS total
           FROM proveedor_pagos WHERE fecha BETWEEN ? AND ? AND medio = 'Efectivo'""",
        (desde, hasta),
    ).fetchone()["total"]
    return cobros + gastos + pagos


def efectivo_neto_fecha(db, fecha_texto):
    return efectivo_neto_periodo(db, fecha_texto, fecha_texto)


@app.route("/caja/cerrar", methods=["POST"])
@solo_dueno
def caja_cerrar():
    fecha_texto = request.form.get("fecha", "").strip()
    nota = request.form.get("nota", "").strip()
    apertura_texto = request.form.get("efectivo_inicial", "").strip()
    try:
        fecha_texto = date.fromisoformat(fecha_texto).isoformat()
        contado = a_centavos(a_decimal(request.form.get("efectivo_contado", "")))
        apertura_form = a_centavos(a_decimal(apertura_texto)) if apertura_texto else None
    except ValueError:
        flash("Revisa la fecha y los importes del cierre de caja.", "error")
        return redirect(url_for("caja"))
    if fecha_texto > date.today().isoformat() or len(nota) > 500:
        flash("La fecha no puede ser futura y la nota admite hasta 500 caracteres.", "error")
        return redirect(url_for("caja", desde=fecha_texto, hasta=fecha_texto))
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    if caja_fecha_cerrada(fecha_texto, db):
        db.rollback()
        flash("Ese día ya está cerrado. Reabre el cierre con motivo antes de modificarlo.", "error")
        return redirect(url_for("caja", desde=fecha_texto, hasta=fecha_texto))
    posterior = db.execute(
        "SELECT fecha FROM caja_cierres WHERE estado = 'cerrado' AND fecha > ? ORDER BY fecha LIMIT 1",
        (fecha_texto,),
    ).fetchone()
    if posterior:
        db.rollback()
        flash(
            f"Reabre primero los cierres posteriores, empezando por {filtro_fecha(posterior['fecha'])}.",
            "error",
        )
        return redirect(url_for("caja", desde=fecha_texto, hasta=fecha_texto))
    anterior = db.execute(
        "SELECT fecha, efectivo_contado_centavos FROM caja_cierres WHERE estado = 'cerrado' AND fecha < ? ORDER BY fecha DESC, id DESC LIMIT 1",
        (fecha_texto,),
    ).fetchone()
    reapertura = db.execute(
        "SELECT efectivo_inicial_centavos FROM caja_cierres WHERE fecha = ? AND estado = 'reabierto' ORDER BY id DESC LIMIT 1",
        (fecha_texto,),
    ).fetchone()
    if anterior is None and reapertura is None and apertura_form is None:
        db.rollback()
        flash("Indica el efectivo de apertura del primer cierre.", "error")
        return redirect(url_for("caja", desde=fecha_texto, hasta=fecha_texto))
    apertura = (
        anterior["efectivo_contado_centavos"] if anterior
        else reapertura["efectivo_inicial_centavos"] if reapertura
        else apertura_form
    )
    movimientos_desde = (
        (date.fromisoformat(anterior["fecha"]) + timedelta(days=1)).isoformat()
        if anterior else fecha_texto
    )
    esperado = apertura + efectivo_neto_periodo(db, movimientos_desde, fecha_texto)
    if esperado < 0:
        db.rollback()
        flash("El efectivo esperado queda negativo; revisa los movimientos.", "error")
        return redirect(url_for("caja", desde=fecha_texto, hasta=fecha_texto))
    with db:
        cierre_id = db.execute(
            """INSERT INTO caja_cierres
               (fecha, efectivo_inicial_centavos, efectivo_esperado_centavos,
                efectivo_contado_centavos, diferencia_centavos, nota, usuario_id, creado_en)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (fecha_texto, apertura, esperado, contado, contado - esperado, nota,
             g.usuario["id"], datetime.now().isoformat(timespec="seconds")),
        ).lastrowid
    registrar("Cerró caja", f"Cierre {fecha_texto} · diferencia {contado - esperado} centavos")
    flash(f"Cierre de caja guardado. Diferencia: {filtro_dinero(contado - esperado)}.", "ok")
    return redirect(url_for("caja", desde=fecha_texto, hasta=fecha_texto))


@app.route("/caja/cierres/<int:cierre_id>/reabrir", methods=["POST"])
@solo_dueno
def caja_reabrir(cierre_id):
    motivo = request.form.get("motivo", "").strip()
    if len(motivo) < 5 or len(motivo) > 500:
        flash("Indica el motivo para reabrir el cierre (entre 5 y 500 caracteres).", "error")
        return redirect(url_for("caja"))
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    cierre = db.execute(
        "SELECT * FROM caja_cierres WHERE id = ? AND estado = 'cerrado'", (cierre_id,)
    ).fetchone()
    if cierre is None:
        db.rollback()
        abort(404)
    posterior = db.execute(
        "SELECT fecha FROM caja_cierres WHERE estado = 'cerrado' AND fecha > ? ORDER BY fecha LIMIT 1",
        (cierre["fecha"],),
    ).fetchone()
    if posterior:
        db.rollback()
        flash(
            f"Reabre primero los cierres posteriores, empezando por {filtro_fecha(posterior['fecha'])}.",
            "error",
        )
        return redirect(url_for("caja", desde=cierre["fecha"], hasta=cierre["fecha"]))
    with db:
        db.execute(
            """UPDATE caja_cierres SET estado = 'reabierto', reabierto_por = ?, reabierto_en = ?,
               motivo_reapertura = ? WHERE id = ?""",
            (g.usuario["id"], datetime.now().isoformat(timespec="seconds"), motivo, cierre_id),
        )
    registrar("Reabrió un cierre de caja", f"{cierre['fecha']} · {motivo}")
    flash("Cierre reabierto; se conserva en el historial.", "ok")
    return redirect(url_for("caja", desde=cierre["fecha"], hasta=cierre["fecha"]))


@app.route("/cuentas-por-cobrar")
@solo_dueno
def cuentas_cobrar():
    """Órdenes terminadas con saldo verificable; históricas desconocidas van aparte."""
    db = get_db()
    filas = db.execute(
        SELECT_ORDEN
        + " WHERE o.estado = 'Completado' AND o.cobro_sin_conciliar = 0"
        + " AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)"
        + " ORDER BY o.fecha_completado, o.id"
    ).fetchall()
    pendientes = []
    busqueda = request.args.get("q", "").strip()[:120].casefold()
    for fila in filas:
        orden = preparar_fila(fila)
        texto_busqueda = " ".join(str(orden.get(campo) or "") for campo in (
            "id", "cliente_nombre", "patente", "marca", "modelo", "fecha_completado"
        )).casefold()
        if orden["saldo_centavos"] > 0 and (not busqueda or busqueda in texto_busqueda):
            pendientes.append(orden)
    historial = db.execute(
        """SELECT COUNT(*) AS total FROM ordenes o
           WHERE o.estado = 'Completado' AND o.cobro_sin_conciliar = 1
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)"""
    ).fetchone()["total"]
    paginacion = paginar(len(pendientes))
    return render_template(
        "cuentas_cobrar.html",
        ordenes=pendientes[paginacion["offset"]:paginacion["offset"] + POR_PAGINA],
        saldo_total=sum(o["saldo_centavos"] for o in pendientes),
        historicas_sin_conciliar=historial, paginacion=paginacion, busqueda=busqueda,
    )


@app.route("/gastos", methods=["GET", "POST"])
@solo_dueno
def gastos():
    db = get_db()
    errores = []
    datos = {
        "fecha": request.form.get("fecha", date.today().isoformat()).strip(),
        "categoria": request.form.get("categoria", "Otros").strip(),
        "concepto": request.form.get("concepto", "").strip(),
        "importe": request.form.get("importe", "").strip(),
        "medio": request.form.get("medio", "").strip(),
        "referencia": request.form.get("referencia", "").strip(),
    }
    if request.method == "POST":
        try:
            fecha_gasto = date.fromisoformat(datos["fecha"])
            importe = a_centavos(a_decimal(datos["importe"]))
        except ValueError:
            errores.append("Revisa la fecha y el importe del gasto.")
            fecha_gasto, importe = None, 0
        if datos["categoria"] not in RUBROS_GASTO:
            errores.append("Selecciona un rubro válido.")
        if not datos["concepto"] or len(datos["concepto"]) > 180:
            errores.append("El concepto es obligatorio y admite hasta 180 caracteres.")
        if not datos["referencia"] or len(datos["referencia"]) > 120:
            # Referencia opcional; solo limita el largo.
            if len(datos["referencia"]) > 120:
                errores.append("La referencia admite hasta 120 caracteres.")
        if datos["medio"] not in MEDIOS_PAGO:
            errores.append("Selecciona un medio de pago válido.")
        if importe <= 0:
            errores.append("El importe debe ser mayor que cero.")
        if fecha_gasto and fecha_gasto > date.today():
            errores.append("La fecha del gasto no puede ser futura.")
        if not errores:
            db.execute("BEGIN IMMEDIATE")
            if caja_fecha_cerrada(fecha_gasto.isoformat(), db):
                db.rollback()
                flash("La fecha corresponde a una caja cerrada. Reabre el cierre para modificarla.", "error")
            else:
                with db:
                    gasto_id = db.execute(
                        """INSERT INTO gastos
                           (fecha, categoria, concepto, importe_centavos, medio, referencia, usuario_id, creado_en)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (fecha_gasto.isoformat(), datos["categoria"], datos["concepto"], importe,
                         datos["medio"], datos["referencia"], g.usuario["id"],
                         datetime.now().isoformat(timespec="seconds")),
                    ).lastrowid
                registrar("Registró un gasto", f"{datos['categoria']} · {datos['concepto']} · {importe} centavos")
                flash(f"Gasto registrado (movimiento {gasto_id}).", "ok")
                return redirect(url_for("gastos"))
    busqueda = request.args.get("q", "").strip()[:120]
    estado = request.args.get("estado", "todos").strip()
    gasto_id = request.args.get("gasto_id", type=int)
    gasto_id = gasto_id if gasto_id and gasto_id > 0 else None
    if estado not in ("todos", "vigentes", "revertidos"):
        estado = "todos"
    desde = request.args.get("desde", "").strip()[:10]
    hasta = request.args.get("hasta", "").strip()[:10]
    try:
        if desde:
            date.fromisoformat(desde)
        if hasta:
            date.fromisoformat(hasta)
        if desde and hasta and desde > hasta:
            raise ValueError
    except ValueError:
        desde, hasta = "", ""
        flash("El rango de fechas no es válido. Se muestran todos los períodos.", "error")
    condiciones = []
    parametros = []
    if gasto_id:
        condiciones.append("g.id = ?")
        parametros.append(gasto_id)
    if busqueda:
        patron = f"%{busqueda}%"
        condiciones.append(
            "(g.concepto LIKE ? OR g.categoria LIKE ? OR g.referencia LIKE ? OR g.medio LIKE ? OR u.nombre LIKE ?)"
        )
        parametros.extend((patron, patron, patron, patron, patron))
    if estado == "vigentes":
        condiciones.append("r.id IS NULL")
    elif estado == "revertidos":
        condiciones.append("r.id IS NOT NULL")
    if desde:
        condiciones.append("g.fecha >= ?")
        parametros.append(desde)
    if hasta:
        condiciones.append("g.fecha <= ?")
        parametros.append(hasta)
    where = " WHERE " + " AND ".join(condiciones) if condiciones else ""
    joins = """ FROM gastos g LEFT JOIN usuarios u ON u.id = g.usuario_id
                LEFT JOIN gasto_reversiones r ON r.gasto_id = g.id"""
    total = db.execute("SELECT COUNT(*) AS total" + joins + where, parametros).fetchone()["total"]
    paginacion = paginar(total)
    movimientos = db.execute(
        """SELECT g.*, u.nombre AS usuario_nombre, r.id AS reversion_id,
                  r.fecha AS fecha_reversion, r.motivo AS motivo_reversion"""
        + joins + where + " ORDER BY g.fecha DESC, g.id DESC LIMIT ? OFFSET ?",
        (*parametros, POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    total_gastos = db.execute("SELECT COALESCE(SUM(importe_centavos), 0) AS total FROM gastos").fetchone()["total"]
    total_reversiones = db.execute("SELECT COUNT(*) AS total FROM gasto_reversiones").fetchone()["total"]
    return render_template(
        "gastos.html", datos=datos, errores=errores, movimientos=movimientos,
        rubros=RUBROS_GASTO, total_gastos=total_gastos,
        reversos=total_reversiones, paginacion=paginacion,
        busqueda=busqueda, estado=estado, desde=desde, hasta=hasta,
    )


@app.route("/gastos/<int:gasto_id>/revertir", methods=["POST"])
@solo_dueno
def gasto_revertir(gasto_id):
    motivo = request.form.get("motivo", "").strip()
    if len(motivo) < 5 or len(motivo) > 500:
        flash("Indica el motivo de reversión (entre 5 y 500 caracteres).", "error")
        return redirect(url_for("gastos"))
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    gasto = db.execute("SELECT * FROM gastos WHERE id = ?", (gasto_id,)).fetchone()
    if gasto is None:
        db.rollback()
        abort(404)
    if caja_fecha_cerrada(date.today().isoformat(), db):
        db.rollback()
        flash("Reabre la caja de hoy antes de registrar esta reversión.", "error")
    elif db.execute("SELECT 1 FROM gasto_reversiones WHERE gasto_id = ?", (gasto_id,)).fetchone():
        db.rollback()
        flash("Este gasto ya tiene una reversión registrada.", "error")
    else:
        with db:
            db.execute(
                """INSERT INTO gasto_reversiones (gasto_id, fecha, motivo, usuario_id, creada_en)
                   VALUES (?, ?, ?, ?, ?)""",
                (gasto_id, date.today().isoformat(), motivo, g.usuario["id"],
                 datetime.now().isoformat(timespec="seconds")),
            )
        registrar("Revirtió un gasto", f"Gasto {gasto_id} · {motivo}")
        flash("Reversión registrada; el gasto original permanece en el historial.", "ok")
    return redirect(url_for("gastos"))


def leer_orden(form, lineas_guardadas=None, orden_guardada=None):
    """Lee el formulario de la orden.

    lineas_guardadas: solo cuando guarda un empleado. Son las líneas que la orden ya
    tenía (por id), para conservar sus precios. Con None, los precios vienen del formulario.
    """
    orden = {
        "vehiculo_id": form.get("vehiculo_id", "").strip(),
        "fallas": form.get("fallas", "").strip(),
        "diagnostico": form.get("diagnostico", "").strip(),
        "notas": form.get("notas", "").strip(),
        "estado": form.get("estado", ""),
        "kilometraje": form.get("kilometraje", "").strip(),
        "fecha_ingreso": form.get("fecha_ingreso", "").strip(),
        "fecha_prometida": "",
        "prioridad": "Normal",
    }
    if es_dueno():
        orden["fecha_prometida"] = form.get(
            "fecha_prometida", (orden_guardada["fecha_prometida"] if orden_guardada else "") or ""
        ).strip()
        orden["prioridad"] = form.get(
            "prioridad", (orden_guardada["prioridad"] if orden_guardada else "Normal") or "Normal"
        ).strip()
    elif orden_guardada:
        orden["fecha_prometida"] = orden_guardada["fecha_prometida"] or ""
        orden["prioridad"] = orden_guardada["prioridad"] or "Normal"
    errores = []

    vehiculo = None
    if orden["vehiculo_id"].isdecimal():
        vehiculo = get_db().execute(
            "SELECT id FROM vehiculos WHERE id = ?", (int(orden["vehiculo_id"]),)
        ).fetchone()
    if vehiculo is None:
        errores.append("Selecciona un cliente y un vehículo.")
    if not orden["fallas"]:
        errores.append("Anota las fallas reportadas.")
    if orden["estado"] not in ESTADOS:
        errores.append("El estado de la reparación no es válido.")
    elif orden["estado"] == "Completado" and not es_dueno():
        errores.append("Solo el dueño puede marcar una orden como Completado.")
    if orden["kilometraje"] and not (
        orden["kilometraje"].isdecimal() and len(orden["kilometraje"]) <= 9
    ):
        errores.append("El kilometraje debe ser un número entero de hasta 9 dígitos.")
    fecha_ingreso_valida = None
    try:
        fecha_ingreso_valida = date.fromisoformat(orden["fecha_ingreso"])
    except ValueError:
        errores.append("La fecha de ingreso no es válida.")
    if orden["prioridad"] not in PRIORIDADES_ORDEN:
        errores.append("Selecciona una prioridad válida para la orden.")
    if orden["fecha_prometida"]:
        try:
            fecha_prometida_valida = date.fromisoformat(orden["fecha_prometida"])
            if fecha_ingreso_valida and fecha_prometida_valida < fecha_ingreso_valida:
                errores.append("La fecha prometida no puede ser anterior al ingreso.")
        except ValueError:
            errores.append("La fecha prometida no es válida.")

    lineas, items = [], []
    # Campos opcionales: se leen aparte para que un formulario que no los tenga siga funcionando.
    costos = form.getlist("item_costo")
    precios = form.getlist("item_precio")
    ids = form.getlist("item_id")
    catalogo_ids = form.getlist("item_catalogo_id")
    marcas_cliente = form.getlist("item_cliente")
    filas = zip(form.getlist("item_tipo"), form.getlist("item_descripcion"), form.getlist("item_cantidad"))
    for indice, (tipo, descripcion, cantidad) in enumerate(filas):
        numero = indice + 1
        precio = precios[indice].strip() if indice < len(precios) else ""
        costo = costos[indice].strip() if indice < len(costos) else ""
        item_id = ids[indice] if indice < len(ids) else ""
        catalogo_id_texto = catalogo_ids[indice].strip() if indice < len(catalogo_ids) else ""
        del_cliente = (marcas_cliente[indice] if indice < len(marcas_cliente) else "0") == "1"
        descripcion = descripcion.strip()
        if not descripcion and not precio:
            continue  # Fila que quedó en blanco.
        linea = {
            "item_id": item_id,
            "catalogo_id": catalogo_id_texto,
            "tipo": tipo,
            "descripcion": descripcion,
            "cantidad": cantidad,
            "precio": precio,
            "costo": costo,
            "del_cliente": del_cliente,
            "falta_precio": False,
        }
        lineas.append(linea)
        # Los errores nombran la línea por su descripción: con dos tablas, un número confunde.
        etiqueta = f"«{descripcion}»" if descripcion else f"Línea {numero} (sin descripción)"
        try:
            valor_cantidad = a_decimal(cantidad)
            if valor_cantidad == 0 or valor_cantidad > MAXIMA_CANTIDAD:
                raise ValueError("cantidad fuera de rango")
            if lineas_guardadas is not None:
                precio_c, costo_c, falta = precio_para_empleado(lineas_guardadas.get(item_id), del_cliente)
            elif del_cliente:
                precio_c, costo_c, falta = 0, None, False
            else:
                # Precio vacío = todavía sin precio: el dueño lo completa antes de cerrar la orden.
                falta = not precio
                precio_c = a_centavos(a_decimal(precio)) if precio else 0
                costo_c = None
                if costo:
                    try:
                        costo_c = a_centavos(a_decimal(costo))
                    except ValueError:
                        errores.append(f"{etiqueta}: el costo no es válido.")
                        continue
        except ValueError:
            errores.append(f"{etiqueta}: revisa la cantidad y el precio.")
            continue
        if tipo not in TIPOS or not descripcion:
            errores.append(f"{etiqueta}: falta la descripción o el tipo.")
            continue
        catalogo_id = None
        if catalogo_id_texto:
            articulo = None
            if catalogo_id_texto.isdecimal() and tipo == "Repuesto" and not del_cliente:
                articulo = get_db().execute(
                    """SELECT id, nombre FROM repuestos_servicios
                       WHERE id = ? AND tipo = 'Repuesto' AND controla_stock = 1
                         AND (activo = 1 OR EXISTS (
                             SELECT 1 FROM orden_items previa
                             WHERE previa.id = ? AND previa.vigente = 1
                               AND previa.catalogo_id = repuestos_servicios.id
                         ))""",
                    (int(catalogo_id_texto), int(item_id) if item_id.isdecimal() else -1),
                ).fetchone()
            if articulo is None or articulo["nombre"] != descripcion:
                errores.append(f"{etiqueta}: selecciona un repuesto inventariable válido del catálogo.")
                continue
            catalogo_id = articulo["id"]
            linea["catalogo_id"] = str(catalogo_id)
        linea["falta_precio"] = falta
        items.append(
            {
                "id": item_id,
                "tipo": tipo,
                "descripcion": descripcion,
                "cantidad": float(valor_cantidad),
                "precio": precio_c,
                "subtotal": a_centavos(valor_cantidad * Decimal(precio_c) / 100),
                "costo": costo_c,
                "costo_total": (
                    a_centavos(valor_cantidad * Decimal(costo_c) / 100) if costo_c is not None else None
                ),
                "del_cliente": del_cliente,
                "falta_precio": falta,
                "catalogo_id": catalogo_id,
            }
        )

    if orden["estado"] == "Completado":
        sin_precio = [f"«{item['descripcion']}»" for item in items if item["falta_precio"]]
        if sin_precio:
            errores.append(
                f"Para completar la orden falta poner el precio de: {', '.join(sin_precio)}."
            )
    return orden, lineas, items, errores


# ---------------------------------------------------------------------------
# Inicio
# ---------------------------------------------------------------------------
def fecha_limite_atraso():
    return (date.today() - timedelta(days=DIAS_ATRASO)).isoformat()


def conteos_por_estado():
    db = get_db()
    conteos = {estado: 0 for estado in ESTADOS}
    for fila in db.execute(
        """SELECT o.estado, COUNT(*) AS total FROM ordenes o
           WHERE NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
           GROUP BY o.estado"""
    ):
        conteos[fila["estado"]] = fila["total"]
    conteos["anuladas"] = db.execute(
        "SELECT COUNT(*) AS total FROM orden_anulaciones"
    ).fetchone()["total"]
    conteos["sin_precio"] = db.execute(
        "SELECT COUNT(DISTINCT orden_id) AS total FROM orden_items WHERE vigente = 1 AND falta_precio = 1"
    ).fetchone()["total"]
    return conteos


def paginar(total, parametro="pagina"):
    """Datos de paginación de una lista, conservando los filtros de la dirección actual."""
    paginas = max(-(-total // POR_PAGINA), 1)
    pagina = min(max(request.args.get(parametro, 1, type=int), 1), paginas)

    def enlace(numero):
        argumentos = {**request.args.to_dict(), parametro: numero}
        return url_for(request.endpoint, **(request.view_args or {}), **argumentos)

    return {
        "pagina": pagina,
        "paginas": paginas,
        "total": total,
        "offset": (pagina - 1) * POR_PAGINA,
        "url_anterior": enlace(pagina - 1) if pagina > 1 else None,
        "url_siguiente": enlace(pagina + 1) if pagina < paginas else None,
    }


def ultimos_meses(cantidad=6):
    """Lista de (clave 'AAAA-MM', etiqueta) de los últimos meses, del más viejo al actual."""
    hoy = date.today()
    actual = hoy.year * 12 + hoy.month - 1
    meses = []
    for indice in range(actual - cantidad + 1, actual + 1):
        anio, mes = divmod(indice, 12)
        meses.append((f"{anio}-{mes + 1:02d}", f"{NOMBRES_MES[mes]} {anio}"))
    return meses


def resumen_panel():
    """Cifras del panel. Los importes van en centavos; el navegador les da formato."""
    db = get_db()
    conteos = conteos_por_estado()

    atrasadas = db.execute(
        "SELECT COUNT(*) AS total FROM ordenes WHERE estado != 'Completado' AND fecha_ingreso < ?",
        (fecha_limite_atraso(),),
    ).fetchone()["total"]
    valor_abierto = db.execute(
        """SELECT COALESCE(SUM(i.subtotal_centavos), 0) AS total
           FROM orden_items i JOIN ordenes o ON o.id = i.orden_id
           WHERE i.vigente = 1 AND o.estado != 'Completado'
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)"""
    ).fetchone()["total"]
    saldo_por_cobrar = db.execute(
        """SELECT COALESCE(SUM(MAX(total_centavos - pagado_centavos, 0)), 0) AS total
           FROM (
             SELECT o.id,
                    COALESCE(SUM(i.subtotal_centavos), 0) AS total_centavos,
                    COALESCE((SELECT SUM(CASE WHEN p.tipo = 'reversion' THEN -p.importe_centavos
                                              ELSE p.importe_centavos END)
                              FROM orden_pagos p WHERE p.orden_id = o.id), 0) AS pagado_centavos
             FROM ordenes o LEFT JOIN orden_items i ON i.orden_id = o.id AND i.vigente = 1
             WHERE o.estado = 'Completado' AND o.cobro_sin_conciliar = 0
               AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
             GROUP BY o.id
           )"""
    ).fetchone()["total"]

    meses = ultimos_meses()
    filas = {
        fila["mes"]: fila
        for fila in db.execute(
            """SELECT substr(o.fecha_completado, 1, 7) AS mes,
                      COUNT(DISTINCT o.id) AS ordenes,
                      COALESCE(SUM(CASE WHEN i.tipo = 'Repuesto' THEN i.subtotal_centavos END), 0) AS repuestos,
                      COALESCE(SUM(CASE WHEN i.tipo = 'Servicio' THEN i.subtotal_centavos END), 0) AS mano_obra
               FROM ordenes o LEFT JOIN orden_items i ON i.orden_id = o.id AND i.vigente = 1
               WHERE o.estado = 'Completado' AND o.fecha_completado >= ?
                 AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
               GROUP BY mes""",
            (f"{meses[0][0]}-01",),
        )
    }
    hoy = date.today().isoformat()
    movimiento_hoy = db.execute(
        """SELECT COALESCE(SUM(fecha_ingreso = ?), 0) AS ingresadas,
                  COALESCE(SUM(estado = 'Completado' AND fecha_completado = ?), 0) AS completadas
           FROM ordenes""",
        (hoy, hoy),
    ).fetchone()
    # Lo que más se facturó en las órdenes completadas este mes (sin repuestos del cliente).
    mas_facturado = [
        dict(fila)
        for fila in db.execute(
            """SELECT i.descripcion, i.tipo,
                      COUNT(DISTINCT o.id) AS ordenes,
                      SUM(i.subtotal_centavos) AS total
           FROM orden_items i JOIN ordenes o ON o.id = i.orden_id
           WHERE i.vigente = 1 AND o.estado = 'Completado' AND substr(o.fecha_completado, 1, 7) = ?
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
                 AND i.del_cliente = 0 AND i.subtotal_centavos > 0
               GROUP BY i.tipo, i.descripcion
               ORDER BY total DESC, ordenes DESC
               LIMIT ?""",
            (meses[-1][0], MAS_FACTURADO_EN_PANEL),
        )
    ]
    return {
        "conteos": conteos,
        "abiertas": conteos["Pendiente"] + conteos["En Proceso"],
        "atrasadas": atrasadas,
        "valor_abierto": valor_abierto,
        "saldo_por_cobrar": saldo_por_cobrar,
        "ingresadas_hoy": movimiento_hoy["ingresadas"],
        "completadas_hoy": movimiento_hoy["completadas"],
        "mas_facturado": mas_facturado,
        "meses": [
            {
                "etiqueta": etiqueta,
                "ordenes": filas[clave]["ordenes"] if clave in filas else 0,
                "repuestos": filas[clave]["repuestos"] if clave in filas else 0,
                "mano_obra": filas[clave]["mano_obra"] if clave in filas else 0,
            }
            for clave, etiqueta in meses
        ],
    }


def resumen_operativo_panel():
    """Indicadores y serie de órdenes sin datos de importes para el rol empleado."""
    db = get_db()
    conteos = conteos_por_estado()
    atrasadas = db.execute(
        """SELECT COUNT(*) AS total FROM ordenes o
           WHERE o.estado != 'Completado' AND o.fecha_ingreso < ?
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)""",
        (fecha_limite_atraso(),),
    ).fetchone()["total"]
    meses = ultimos_meses()
    desde = f"{meses[0][0]}-01"
    ingresadas = {
        fila["mes"]: fila["total"]
        for fila in db.execute(
            """SELECT substr(o.fecha_ingreso, 1, 7) AS mes, COUNT(*) AS total
               FROM ordenes o WHERE o.fecha_ingreso >= ?
                 AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
               GROUP BY mes""",
            (desde,),
        )
    }
    completadas = {
        fila["mes"]: fila["total"]
        for fila in db.execute(
            """SELECT substr(o.fecha_completado, 1, 7) AS mes, COUNT(*) AS total
               FROM ordenes o WHERE o.estado = 'Completado' AND o.fecha_completado >= ?
                 AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
               GROUP BY mes""",
            (desde,),
        )
    }
    hoy = date.today().isoformat()
    hoy_fila = db.execute(
        """SELECT SUM(o.fecha_ingreso = ?) AS ingresadas,
                  SUM(o.estado = 'Completado' AND o.fecha_completado = ?) AS completadas
           FROM ordenes o WHERE NOT EXISTS (
             SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id
           )""",
        (hoy, hoy),
    ).fetchone()
    return {
        "conteos": conteos,
        "atrasadas": atrasadas,
        "ingresadas_hoy": hoy_fila["ingresadas"] or 0,
        "completadas_hoy": hoy_fila["completadas"] or 0,
        "meses": [
            {"etiqueta": etiqueta, "ingresadas": ingresadas.get(clave, 0),
             "completadas": completadas.get(clave, 0)}
            for clave, etiqueta in meses
        ],
    }


def ganancias_panel():
    """Separa cobros reales, valor de trabajo terminado y margen estimado."""
    db = get_db()
    hoy = date.today()
    inicio_semana = (hoy - timedelta(days=hoy.weekday())).isoformat()
    inicio_mes = hoy.replace(day=1).isoformat()
    desde = min(inicio_semana, inicio_mes)  # La semana puede empezar en el mes anterior.

    filas = db.execute(
        """SELECT o.id AS orden_id, o.fecha_completado, o.vehiculo_id,
                  v.patente, v.marca, v.modelo,
                  COALESCE(SUM(CASE WHEN i.tipo = 'Servicio' THEN i.subtotal_centavos END), 0) AS mano_obra,
                  COALESCE(SUM(i.subtotal_centavos), 0) AS valor_completado,
                  COALESCE(SUM(i.costo_total_centavos), 0) AS costo,
                  COUNT(CASE WHEN i.tipo = 'Repuesto' AND i.subtotal_centavos > 0
                                  AND i.costo_total_centavos IS NULL THEN 1 END) AS sin_costo
           FROM ordenes o
           JOIN vehiculos v ON v.id = o.vehiculo_id
           LEFT JOIN orden_items i ON i.orden_id = o.id AND i.vigente = 1
           WHERE o.estado = 'Completado' AND o.fecha_completado >= ?
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
           GROUP BY o.id""",
        (desde,),
    ).fetchall()

    def periodo_vacio():
        return {
            "ordenes": 0,
            "cobrado": 0,
            "valor_completado": 0,
            "costo": 0,
            "mano_obra": 0,
            "ganancia": 0,
            "sin_costo": 0,
        }

    semana, mes, vehiculos = periodo_vacio(), periodo_vacio(), {}
    for fila in filas:
        ganancia = fila["valor_completado"] - fila["costo"]
        periodos = []
        if fila["fecha_completado"] >= inicio_semana:
            periodos.append(semana)
        if fila["fecha_completado"] >= inicio_mes:
            periodos.append(mes)
            vehiculo = vehiculos.setdefault(fila["vehiculo_id"], {
                "id": fila["vehiculo_id"],
                "patente": fila["patente"],
                "descripcion": f"{fila['marca']} {fila['modelo']}".strip(),
                "ordenes": 0, "valor_completado": 0, "ganancia": 0, "ganancia_semana": 0,
            })
            vehiculo["ordenes"] += 1
            vehiculo["valor_completado"] += fila["valor_completado"]
            vehiculo["ganancia"] += ganancia
            if fila["fecha_completado"] >= inicio_semana:
                vehiculo["ganancia_semana"] += ganancia
        for periodo in periodos:
            periodo["ordenes"] += 1
            periodo["valor_completado"] += fila["valor_completado"]
            periodo["costo"] += fila["costo"]
            periodo["mano_obra"] += fila["mano_obra"]
            periodo["ganancia"] += ganancia
            periodo["sin_costo"] += fila["sin_costo"]

    cobros = db.execute(
        """SELECT p.fecha, p.tipo, p.importe_centavos, original.tipo AS tipo_original
           FROM orden_pagos p
           LEFT JOIN orden_pagos original ON original.id = p.revierte_id
           JOIN ordenes o ON o.id = p.orden_id
           WHERE p.fecha >= ? AND p.fecha <= ?
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)
           ORDER BY p.id""",
        (desde, hoy.isoformat()),
    ).fetchall()
    for movimiento in cobros:
        if movimiento["tipo"] == "cobro":
            importe = movimiento["importe_centavos"]
        elif movimiento["tipo"] == "reversion" and movimiento["tipo_original"] == "cobro":
            importe = -movimiento["importe_centavos"]
        else:
            continue  # Los saldos iniciales y sus reversiones no son caja del período.
        if movimiento["fecha"] >= inicio_semana:
            semana["cobrado"] += importe
        if movimiento["fecha"] >= inicio_mes:
            mes["cobrado"] += importe

    return {
        "semana": semana,
        "mes": mes,
        "inicio_semana": inicio_semana,
        "mes_clave": inicio_mes[:7],
        "vehiculos": sorted(vehiculos.values(), key=lambda v: v["ganancia"], reverse=True),
    }


def preparar_fila(fila):
    """Agrega a una orden lo que necesita para mostrarse en las listas."""
    orden = dict(fila)
    posicion = ESTADOS.index(orden["estado"])
    orden["total"] = orden["total_repuestos"] + orden["total_mano_obra"]
    orden["saldo_centavos"] = (
        None
        if orden["cobro_sin_conciliar"]
        else max(orden["total"] - orden["pagado_centavos"], 0)
    )
    orden["dias"] = max((date.today() - date.fromisoformat(orden["fecha_ingreso"])).days, 0)
    orden["atrasada"] = (
        orden["estado"] != "Completado" and orden["fecha_ingreso"] < fecha_limite_atraso()
    )
    orden["vencida"] = bool(
        orden.get("fecha_prometida")
        and orden["estado"] != "Completado"
        and orden["fecha_prometida"] < date.today().isoformat()
    )
    orden["vence_hoy"] = bool(
        orden.get("fecha_prometida") == date.today().isoformat()
        and orden["estado"] != "Completado"
    )
    orden["anterior"] = ESTADOS[posicion - 1] if posicion > 0 else None
    orden["siguiente"] = ESTADOS[posicion + 1] if posicion < len(ESTADOS) - 1 else None
    return orden


@app.route("/")
def inicio():
    if not es_dueno():
        return render_template(
            "index_empleado.html",
            resumen=resumen_operativo_panel(),
            dias_atraso=DIAS_ATRASO,
        )
    atrasadas = [
        preparar_fila(fila)
        for fila in get_db().execute(
            SELECT_ORDEN
            + " WHERE o.estado != 'Completado' AND o.fecha_ingreso < ?"
            + " ORDER BY o.fecha_ingreso, o.id LIMIT ?",
            (fecha_limite_atraso(), ATRASADAS_EN_PANEL),
        )
    ]
    # También aquí, por si el sistema queda abierto de un día para otro.
    respaldo_ok = respaldar_sin_fallar()
    copia_externa = ultima_copia_externa()
    return render_template(
        "index.html",
        atrasadas=atrasadas,
        resumen=resumen_panel(),
        ganancias=ganancias_panel(),
        dias_atraso=DIAS_ATRASO,
        respaldo_ok=respaldo_ok,
        ultimo_respaldo=ultimo_respaldo(),
        ultimo_excel=ultimo_excel(),
        carpeta_excel=app.config["RESPALDOS_EXCEL"],
        copia_externa=copia_externa.strftime("%d/%m/%Y %H:%M") if copia_externa else None,
        aviso_copia_externa=(
            copia_externa is None
            or datetime.now() - copia_externa > timedelta(days=DIAS_AVISO_COPIA_EXTERNA)
        ),
        dias_aviso_copia_externa=DIAS_AVISO_COPIA_EXTERNA,
        carpeta_pendrive=CARPETA_PENDRIVE,
    )


# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------
def obtener_o_404(tabla, registro_id):
    # "tabla" siempre viene del código, nunca de datos del usuario.
    fila = get_db().execute(f"SELECT * FROM {tabla} WHERE id = ?", (registro_id,)).fetchone()
    if fila is None:
        abort(404)
    return fila


@app.route("/clientes")
def clientes():
    busqueda = request.args.get("q", "").strip()
    condicion = ""
    parametros = ()
    if busqueda:
        patron = f"%{busqueda}%"
        patron_patente = f"%{normalizar_patente(busqueda)}%"
        condicion = """
            WHERE c.nombre LIKE ? OR c.telefono LIKE ? OR c.documento LIKE ?
               OR c.id IN (SELECT cliente_id FROM vehiculos WHERE patente LIKE ?)
        """
        parametros = (patron, patron, patron, patron_patente)

    db = get_db()
    total = db.execute(
        "SELECT COUNT(*) AS total FROM clientes c" + condicion, parametros
    ).fetchone()["total"]
    paginacion = paginar(total)
    filas = db.execute(
        """SELECT c.*, COUNT(v.id) AS total_vehiculos,
                  COALESCE(GROUP_CONCAT(v.patente, ', '), '') AS patentes
           FROM clientes c
           LEFT JOIN vehiculos v ON v.cliente_id = c.id"""
        + condicion
        + " GROUP BY c.id ORDER BY c.nombre COLLATE NOCASE LIMIT ? OFFSET ?",
        (*parametros, POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    return render_template("clientes.html", clientes=filas, busqueda=busqueda, paginacion=paginacion)


@app.route("/clientes/nuevo", methods=["GET", "POST"])
def cliente_nuevo():
    if request.method == "GET":
        return render_template(
            "cliente_form.html", cliente={}, vehiculos=[{}], errores=[], edicion=False
        )

    cliente, errores = leer_cliente(request.form)
    vehiculos = []
    filas = zip(
        request.form.getlist("patente"),
        request.form.getlist("marca"),
        request.form.getlist("modelo"),
        request.form.getlist("anio"),
        request.form.getlist("color"),
    )
    for fila in filas:
        if not any(valor.strip() for valor in fila):
            continue  # Fila que quedó en blanco.
        datos, errores_vehiculo = leer_vehiculo(*fila)
        vehiculos.append(datos)
        errores.extend(errores_vehiculo)

    if not vehiculos:
        errores.append("Registra al menos un vehículo.")
    patentes = [v["patente"] for v in vehiculos]
    if len(patentes) != len(set(patentes)):
        errores.append("Hay patentes repetidas en el formulario.")

    db = get_db()
    if not errores:
        try:
            with db:
                cursor = db.execute(
                    """INSERT INTO clientes (nombre, documento, telefono, email, direccion)
                       VALUES (:nombre, :documento, :telefono, :email, :direccion)""",
                    cliente,
                )
                cliente_id = cursor.lastrowid
                for vehiculo in vehiculos:
                    db.execute(
                        """INSERT INTO vehiculos (cliente_id, patente, marca, modelo, anio, color)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            cliente_id,
                            vehiculo["patente"],
                            vehiculo["marca"],
                            vehiculo["modelo"],
                            anio_para_db(vehiculo),
                            vehiculo["color"],
                        ),
                    )
        except sqlite3.IntegrityError:
            errores.append("Alguna de las patentes ya está registrada en otro vehículo.")
        else:
            registrar("Registró un cliente", f"{cliente['nombre']} · {', '.join(patentes)}")
            flash("Cliente registrado correctamente.", "ok")
            return redirect(url_for("cliente_detalle", cliente_id=cliente_id))

    return render_template(
        "cliente_form.html",
        cliente=cliente,
        vehiculos=vehiculos or [{}],
        errores=errores,
        edicion=False,
    )


@app.route("/clientes/<int:cliente_id>")
def cliente_detalle(cliente_id):
    db = get_db()
    cliente = obtener_o_404("clientes", cliente_id)
    vehiculos = db.execute(
        "SELECT * FROM vehiculos WHERE cliente_id = ? ORDER BY patente", (cliente_id,)
    ).fetchall()
    filas_ordenes = db.execute(
        SELECT_ORDEN + " WHERE v.cliente_id = ? ORDER BY o.id DESC", (cliente_id,)
    ).fetchall()
    ordenes = [preparar_fila(fila) for fila in filas_ordenes]
    ordenes_activas = [
        orden for orden in ordenes
        if orden["estado"] != "Completado" and not orden["anulada"]
    ]
    saldo_pendiente_centavos = sum(
        orden["saldo_centavos"] or 0 for orden in ordenes
        if orden["estado"] == "Completado" and not orden["anulada"]
    )
    return render_template(
        "cliente_detalle.html", cliente=cliente, vehiculos=vehiculos, ordenes=ordenes,
        ordenes_activas=ordenes_activas, saldo_pendiente_centavos=saldo_pendiente_centavos,
    )


@app.route("/clientes/<int:cliente_id>/editar", methods=["GET", "POST"])
def cliente_editar(cliente_id):
    existente = obtener_o_404("clientes", cliente_id)
    if request.method == "GET":
        return render_template(
            "cliente_form.html", cliente=dict(existente), vehiculos=[], errores=[], edicion=True
        )

    cliente, errores = leer_cliente(request.form)
    if errores:
        cliente["id"] = cliente_id
        return render_template(
            "cliente_form.html", cliente=cliente, vehiculos=[], errores=errores, edicion=True
        )
    db = get_db()
    with db:
        db.execute(
            """UPDATE clientes
               SET nombre = :nombre, documento = :documento, telefono = :telefono,
                   email = :email, direccion = :direccion
               WHERE id = :id""",
            {**cliente, "id": cliente_id},
        )
    registrar("Editó un cliente", cliente["nombre"])
    flash("Cliente actualizado.", "ok")
    return redirect(url_for("cliente_detalle", cliente_id=cliente_id))


@app.route("/clientes/<int:cliente_id>/eliminar", methods=["POST"])
@solo_dueno
def cliente_eliminar(cliente_id):
    cliente = obtener_o_404("clientes", cliente_id)
    db = get_db()
    tiene_ordenes = db.execute(
        """SELECT 1 FROM ordenes o JOIN vehiculos v ON v.id = o.vehiculo_id
           WHERE v.cliente_id = ? LIMIT 1""",
        (cliente_id,),
    ).fetchone()
    if tiene_ordenes:
        flash("No se puede eliminar: el cliente tiene órdenes de trabajo.", "error")
        return redirect(url_for("cliente_detalle", cliente_id=cliente_id))
    with db:
        db.execute("DELETE FROM clientes WHERE id = ?", (cliente_id,))
    registrar("Eliminó un cliente", cliente["nombre"])
    flash("Cliente eliminado.", "ok")
    return redirect(url_for("clientes"))


# ---------------------------------------------------------------------------
# Vehículos
# ---------------------------------------------------------------------------
def leer_vehiculo_de_form(form):
    return leer_vehiculo(
        form.get("patente"), form.get("marca"), form.get("modelo"), form.get("anio"), form.get("color")
    )


@app.route("/clientes/<int:cliente_id>/vehiculos/nuevo", methods=["POST"])
def vehiculo_nuevo(cliente_id):
    obtener_o_404("clientes", cliente_id)
    datos, errores = leer_vehiculo_de_form(request.form)
    db = get_db()
    if not errores:
        try:
            with db:
                db.execute(
                    """INSERT INTO vehiculos (cliente_id, patente, marca, modelo, anio, color)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        cliente_id,
                        datos["patente"],
                        datos["marca"],
                        datos["modelo"],
                        anio_para_db(datos),
                        datos["color"],
                    ),
                )
        except sqlite3.IntegrityError:
            errores.append(f"La patente {datos['patente']} ya está registrada.")
    if errores:
        for error in errores:
            flash(error, "error")
    else:
        registrar("Agregó un vehículo", f"{datos['patente']} · {datos['marca']} {datos['modelo']}")
        flash(f"Vehículo {datos['patente']} agregado.", "ok")
    return redirect(url_for("cliente_detalle", cliente_id=cliente_id))


@app.route("/vehiculos")
def vehiculos():
    busqueda = request.args.get("q", "").strip()
    condicion = ""
    parametros = ()
    if busqueda:
        patron = f"%{busqueda}%"
        condicion = """
            WHERE c.nombre LIKE ? OR v.patente LIKE ? OR v.marca LIKE ? OR v.modelo LIKE ?
        """
        parametros = (patron, f"%{normalizar_patente(busqueda)}%", patron, patron)

    db = get_db()
    total = db.execute(
        """SELECT COUNT(*) AS total
           FROM vehiculos v JOIN clientes c ON c.id = v.cliente_id"""
        + condicion,
        parametros,
    ).fetchone()["total"]
    paginacion = paginar(total)
    filas = db.execute(
        """SELECT v.*, c.nombre AS cliente_nombre,
                  (SELECT COUNT(*) FROM ordenes o WHERE o.vehiculo_id = v.id
                   AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)) AS total_ordenes,
                  (SELECT MAX(o.fecha_ingreso) FROM ordenes o WHERE o.vehiculo_id = v.id
                   AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)) AS ultima_visita,
                  COALESCE((SELECT SUM(i.subtotal_centavos)
                            FROM orden_items i JOIN ordenes o ON o.id = i.orden_id
                            WHERE o.vehiculo_id = v.id AND o.estado = 'Completado' AND i.vigente = 1
                              AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)), 0) AS total_completado
           FROM vehiculos v JOIN clientes c ON c.id = v.cliente_id"""
        + condicion
        + " ORDER BY v.patente LIMIT ? OFFSET ?",
        (*parametros, POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    return render_template("vehiculos.html", vehiculos=filas, busqueda=busqueda, paginacion=paginacion)


@app.route("/vehiculos/<int:vehiculo_id>")
def vehiculo_detalle(vehiculo_id):
    """Historial del vehículo: todo lo que se le hizo y cuánto suma."""
    db = get_db()
    vehiculo = db.execute(
        """SELECT v.*, c.nombre AS cliente_nombre, c.telefono AS cliente_telefono
           FROM vehiculos v JOIN clientes c ON c.id = v.cliente_id
           WHERE v.id = ?""",
        (vehiculo_id,),
    ).fetchone()
    if vehiculo is None:
        abort(404)

    totales = db.execute(
        """SELECT COUNT(DISTINCT o.id) AS ordenes,
                  COALESCE(SUM(CASE WHEN o.estado = 'Completado' AND i.tipo = 'Repuesto'
                                    THEN i.subtotal_centavos END), 0) AS repuestos,
                  COALESCE(SUM(CASE WHEN o.estado = 'Completado' AND i.tipo = 'Servicio'
                                    THEN i.subtotal_centavos END), 0) AS mano_obra,
                  COALESCE(SUM(CASE WHEN o.estado != 'Completado'
                                    THEN i.subtotal_centavos END), 0) AS en_curso,
                  COALESCE(SUM(CASE WHEN o.estado = 'Completado'
                                    THEN i.costo_total_centavos END), 0) AS costo,
                  COUNT(CASE WHEN o.estado = 'Completado' AND i.tipo = 'Repuesto'
                                  AND i.subtotal_centavos > 0
                                  AND i.costo_total_centavos IS NULL THEN 1 END) AS sin_costo
           FROM ordenes o LEFT JOIN orden_items i ON i.orden_id = o.id AND i.vigente = 1
           WHERE o.vehiculo_id = ?
             AND NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)""",
        (vehiculo_id,),
    ).fetchone()

    total_lineas = db.execute(
        """SELECT COUNT(*) AS total
           FROM orden_items i JOIN ordenes o ON o.id = i.orden_id
           WHERE o.vehiculo_id = ?""",
        (vehiculo_id,),
    ).fetchone()["total"]
    paginacion = paginar(total_lineas)
    lineas = db.execute(
        """SELECT i.*, o.estado, o.kilometraje,
                  EXISTS(SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id) AS anulada,
                  COALESCE(o.fecha_completado, o.fecha_ingreso) AS fecha_trabajo
           FROM orden_items i JOIN ordenes o ON o.id = i.orden_id
           WHERE o.vehiculo_id = ?
           ORDER BY fecha_trabajo DESC, o.id DESC, i.id
           LIMIT ? OFFSET ?""",
        (vehiculo_id, POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    return render_template(
        "vehiculo_detalle.html",
        vehiculo=vehiculo,
        totales=totales,
        lineas=lineas,
        paginacion=paginacion,
    )


@app.route("/vehiculos/<int:vehiculo_id>/editar", methods=["GET", "POST"])
def vehiculo_editar(vehiculo_id):
    existente = obtener_o_404("vehiculos", vehiculo_id)
    if request.method == "GET":
        return render_template("vehiculo_form.html", vehiculo=dict(existente), errores=[])

    datos, errores = leer_vehiculo_de_form(request.form)
    if not errores:
        db = get_db()
        try:
            with db:
                db.execute(
                    """UPDATE vehiculos
                       SET patente = ?, marca = ?, modelo = ?, anio = ?, color = ?
                       WHERE id = ?""",
                    (
                        datos["patente"],
                        datos["marca"],
                        datos["modelo"],
                        anio_para_db(datos),
                        datos["color"],
                        vehiculo_id,
                    ),
                )
        except sqlite3.IntegrityError:
            errores.append(f"La patente {datos['patente']} ya está registrada.")
        else:
            registrar("Editó un vehículo", datos["patente"])
            flash("Vehículo actualizado.", "ok")
            return redirect(url_for("cliente_detalle", cliente_id=existente["cliente_id"]))

    datos.update(id=vehiculo_id, cliente_id=existente["cliente_id"])
    return render_template("vehiculo_form.html", vehiculo=datos, errores=errores)


@app.route("/vehiculos/<int:vehiculo_id>/eliminar", methods=["POST"])
@solo_dueno
def vehiculo_eliminar(vehiculo_id):
    vehiculo = obtener_o_404("vehiculos", vehiculo_id)
    db = get_db()
    try:
        with db:
            db.execute("DELETE FROM vehiculos WHERE id = ?", (vehiculo_id,))
    except sqlite3.IntegrityError:
        flash("No se puede eliminar: el vehículo tiene órdenes de trabajo.", "error")
    else:
        registrar("Eliminó un vehículo", vehiculo["patente"])
        flash(f"Vehículo {vehiculo['patente']} eliminado.", "ok")
    return redirect(url_for("cliente_detalle", cliente_id=vehiculo["cliente_id"]))


# ---------------------------------------------------------------------------
# Órdenes de trabajo
# ---------------------------------------------------------------------------
def obtener_orden_o_404(orden_id):
    fila = get_db().execute(SELECT_ORDEN + " WHERE o.id = ?", (orden_id,)).fetchone()
    if fila is None:
        abort(404)
    orden = dict(fila)
    orden["total"] = orden["total_repuestos"] + orden["total_mano_obra"]
    orden["saldo_centavos"] = (
        None
        if orden["cobro_sin_conciliar"]
        else max(orden["total"] - orden["pagado_centavos"], 0)
    )
    orden["anulacion"] = get_db().execute(
        """SELECT a.*, u.nombre AS usuario_nombre FROM orden_anulaciones a
           LEFT JOIN usuarios u ON u.id = a.usuario_id WHERE a.orden_id = ?""",
        (orden_id,),
    ).fetchone()
    return orden


def obtener_items(orden_id):
    return get_db().execute(
        """SELECT i.*, r.sku, r.unidad, COALESCE(r.controla_stock, 0) AS controla_stock,
                  COALESCE((SELECT SUM(m.cantidad_milesimas) FROM stock_movimientos m
                            WHERE m.articulo_id = r.id), 0) AS stock_actual_milesimas,
                  (SELECT m.id FROM stock_movimientos m
                   WHERE m.orden_item_id = i.id AND m.tipo = 'consumo'
                     AND (SELECT SUM(act.cantidad_milesimas + COALESCE((
                              SELECT SUM(rev.cantidad_milesimas) FROM stock_movimientos rev
                              WHERE rev.revierte_movimiento_id = act.id
                          ), 0)) FROM stock_movimientos act WHERE act.orden_item_id = i.id) < 0
                   ORDER BY m.id DESC LIMIT 1) AS consumo_activo_id
           FROM orden_items i LEFT JOIN repuestos_servicios r ON r.id = i.catalogo_id
           WHERE i.orden_id = ? AND i.vigente = 1 ORDER BY i.id""",
        (orden_id,),
    ).fetchall()


def obtener_pagos(orden_id):
    """Devuelve los cobros y reversiones, enlazando cada cobro con su eventual reverso."""
    return get_db().execute(
        """SELECT p.*, u.nombre AS usuario_nombre, r.id AS reversion_id
           FROM orden_pagos p
           LEFT JOIN usuarios u ON u.id = p.usuario_id
           LEFT JOIN orden_pagos r ON r.revierte_id = p.id
           WHERE p.orden_id = ? ORDER BY p.id""",
        (orden_id,),
    ).fetchall()


def describir_orden(orden_id):
    """Texto corto para el registro de actividad, por ejemplo "OT-00012 · AB123CD · Toyota Corolla"."""
    orden = obtener_orden_o_404(orden_id)
    return f"{filtro_num_ot(orden_id)} · {orden['patente']} · {orden['marca']} {orden['modelo']}"


def guardar_items(db, orden_id, items):
    existentes = {
        fila["id"]: fila
        for fila in db.execute(
            "SELECT * FROM orden_items WHERE orden_id = ? AND vigente = 1", (orden_id,)
        )
    }
    usados = set()
    for item in items:
        articulo_id = item.get("catalogo_id")
        datos = (
            item["tipo"], item["descripcion"], item["cantidad"], item["precio"],
            item["subtotal"], item["costo"], item["costo_total"],
            int(item["del_cliente"]), int(item["falta_precio"]),
            articulo_id,
        )
        item_id = item.get("id")
        if item_id and str(item_id).isdecimal() and int(item_id) in existentes:
            item_id = int(item_id)
            previa = existentes[item_id]
            movimiento = db.execute(
                """SELECT costo_unitario_centavos FROM stock_movimientos
                   WHERE orden_item_id = ? AND tipo = 'consumo'
                     AND NOT EXISTS (SELECT 1 FROM stock_movimientos rev
                                     WHERE rev.revierte_movimiento_id = stock_movimientos.id)
                   ORDER BY id DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
            tiene_historial = db.execute(
                "SELECT 1 FROM stock_movimientos WHERE orden_item_id = ? LIMIT 1", (item_id,)
            ).fetchone()
            datos_previos = (
                previa["tipo"], previa["descripcion"], previa["cantidad"],
                previa["precio_unitario_centavos"], previa["subtotal_centavos"],
                previa["costo_unitario_centavos"], previa["costo_total_centavos"],
                previa["del_cliente"], previa["falta_precio"], previa["catalogo_id"],
            )
            if tiene_historial and not movimiento and datos != datos_previos:
                db.execute("UPDATE orden_items SET vigente = 0 WHERE id = ?", (item_id,))
                db.execute(
                    """INSERT INTO orden_items
                       (orden_id, tipo, descripcion, cantidad, precio_unitario_centavos, subtotal_centavos,
                        costo_unitario_centavos, costo_total_centavos, del_cliente, falta_precio, catalogo_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (orden_id, *datos),
                )
                usados.add(item_id)
                continue
            if movimiento:
                costo = movimiento["costo_unitario_centavos"] or 0
                costo_total = a_centavos(Decimal(str(item["cantidad"])) * Decimal(costo) / 100)
                datos = (*datos[:5], costo, costo_total, *datos[7:])
            db.execute(
                """UPDATE orden_items
                   SET tipo = ?, descripcion = ?, cantidad = ?, precio_unitario_centavos = ?,
                       subtotal_centavos = ?, costo_unitario_centavos = ?, costo_total_centavos = ?,
                       del_cliente = ?, falta_precio = ?, catalogo_id = ?
                   WHERE id = ? AND orden_id = ?""",
                (*datos, item_id, orden_id),
            )
            usados.add(item_id)
        else:
            db.execute(
                """INSERT INTO orden_items
                   (orden_id, tipo, descripcion, cantidad, precio_unitario_centavos, subtotal_centavos,
                    costo_unitario_centavos, costo_total_centavos, del_cliente, falta_precio, catalogo_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (orden_id, *datos),
            )
    for item_id in set(existentes) - usados:
        movimiento = db.execute(
            "SELECT 1 FROM stock_movimientos WHERE orden_item_id = ? LIMIT 1", (item_id,)
        ).fetchone()
        if movimiento:
            db.execute(
                "UPDATE orden_items SET vigente = 0 WHERE id = ? AND orden_id = ?",
                (item_id, orden_id),
            )
        else:
            db.execute("DELETE FROM orden_items WHERE id = ? AND orden_id = ?", (item_id, orden_id))


def render_orden_form(orden, lineas, errores, orden_id=None):
    db = get_db()
    clientes_con_vehiculo = db.execute(
        """SELECT DISTINCT c.id, c.nombre FROM clientes c
           JOIN vehiculos v ON v.cliente_id = c.id
           ORDER BY c.nombre COLLATE NOCASE"""
    ).fetchall()
    vehiculos = [
        {
            "id": fila["id"],
            "cliente_id": fila["cliente_id"],
            "texto": f"{fila['patente']} — {fila['marca']} {fila['modelo']}",
        }
        for fila in db.execute("SELECT * FROM vehiculos ORDER BY patente")
    ]
    # El empleado recibe solo los nombres de la lista de precios, nunca los importes.
    dueno = es_dueno()
    catalogo = [
        {
            "id": fila["id"],
            "nombre": fila["nombre"],
            "tipo": fila["tipo"],
            "controla_stock": bool(fila["controla_stock"]),
            "precio": filtro_importe_input(fila["precio_centavos"]) if dueno else "",
            "costo": importe_opcional(fila["costo_centavos"]) if dueno else "",
        }
        for fila in db.execute(
            "SELECT * FROM repuestos_servicios WHERE activo = 1 ORDER BY nombre COLLATE NOCASE"
        )
    ]
    return render_template(
        "orden_form.html",
        orden=orden,
        lineas=lineas,
        errores=errores,
        orden_id=orden_id,
        clientes=clientes_con_vehiculo,
        vehiculos=vehiculos,
        catalogo=catalogo,
    )


@app.route("/ordenes")
def ordenes():
    estado = request.args.get("estado", "")
    if estado not in VISTAS_ORDENES:
        estado = ""
    titulo, condicion_vista, orden_sql = VISTAS_ORDENES[estado]
    if estado == "anuladas" and not es_dueno():
        abort(403)
    busqueda = request.args.get("q", "").strip()
    prioridad = request.args.get("prioridad", "")
    if prioridad not in PRIORIDADES_ORDEN:
        prioridad = ""
    vencimiento = request.args.get("vencimiento", "")
    if vencimiento not in ("vencida", "hoy", "programada", "sin_fecha"):
        vencimiento = ""

    condiciones, parametros = [], []
    if estado != "anuladas":
        condiciones.append(
            "NOT EXISTS (SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id)"
        )
    if condicion_vista:
        condiciones.append(condicion_vista)
    if estado == "atrasadas":
        parametros.append(fecha_limite_atraso())
    if busqueda:
        patron = f"%{busqueda}%"
        condiciones.append(
            "(c.nombre LIKE ? OR c.telefono LIKE ? OR v.patente LIKE ? OR v.marca LIKE ? "
            "OR v.modelo LIKE ? OR o.fallas LIKE ? OR o.diagnostico LIKE ?)"
        )
        parametros += [
            patron, patron, f"%{normalizar_patente(busqueda)}%", patron, patron, patron, patron,
        ]
    if prioridad:
        condiciones.append("o.prioridad = ?")
        parametros.append(prioridad)
    if vencimiento:
        condiciones.append("o.estado != 'Completado'")
        if vencimiento == "vencida":
            condiciones.append("o.fecha_prometida < ?")
            parametros.append(date.today().isoformat())
        elif vencimiento == "hoy":
            condiciones.append("o.fecha_prometida = ?")
            parametros.append(date.today().isoformat())
        elif vencimiento == "programada":
            condiciones.append("o.fecha_prometida > ?")
            parametros.append(date.today().isoformat())
        else:
            condiciones.append("o.fecha_prometida IS NULL")
    filtro = " WHERE " + " AND ".join(condiciones) if condiciones else ""

    db = get_db()
    total = db.execute(CONTAR_ORDENES + filtro, parametros).fetchone()["total"]
    paginacion = paginar(total)
    # orden_sql viene de VISTAS_ORDENES, nunca de datos del usuario.
    filas = db.execute(
        SELECT_ORDEN + filtro + f" ORDER BY {orden_sql} LIMIT ? OFFSET ?",
        (*parametros, POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    return render_template(
        "ordenes.html",
        ordenes=[preparar_fila(fila) for fila in filas],
        estado=estado,
        titulo=titulo,
        busqueda=busqueda,
        prioridad=prioridad,
        vencimiento=vencimiento,
        paginacion=paginacion,
    )


@app.route("/ordenes/nueva", methods=["GET", "POST"])
def orden_nueva():
    if request.method == "GET":
        orden = {
            "vehiculo_id": request.args.get("vehiculo_id", ""),
            "estado": "Pendiente",
            "fecha_ingreso": date.today().isoformat(),
            "fecha_prometida": "",
            "prioridad": "Normal",
        }
        return render_orden_form(orden, [], [])

    # Un empleado no envía precios: todas sus líneas nuevas quedan "sin precio".
    orden, lineas, items, errores = leer_orden(request.form, None if es_dueno() else {})
    if errores:
        return render_orden_form(orden, lineas, errores)

    db = get_db()
    with db:
        cursor = db.execute(
            """INSERT INTO ordenes (vehiculo_id, fallas, diagnostico, notas, estado,
                                    kilometraje, fecha_ingreso, fecha_completado,
                                    fecha_prometida, prioridad)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(orden["vehiculo_id"]),
                orden["fallas"],
                orden["diagnostico"],
                orden["notas"],
                orden["estado"],
                int(orden["kilometraje"]) if orden["kilometraje"] else None,
                orden["fecha_ingreso"],
                fecha_completado_para(orden["estado"]),
                orden["fecha_prometida"] or None,
                orden["prioridad"],
            ),
        )
        orden_id = cursor.lastrowid
        guardar_items(db, orden_id, items)
    registrar("Creó la orden", describir_orden(orden_id), orden_id)
    flash(f"Orden {filtro_num_ot(orden_id)} creada.", "ok")
    return redirect(url_for("orden_detalle", orden_id=orden_id))


def validar_lineas_con_stock(orden_id, items):
    """Impide alterar líneas con consumos activos; las ya revertidas pueden reemplazarse."""
    db = get_db()
    movidas = db.execute(
        """SELECT i.* FROM orden_items i
           WHERE i.orden_id = ? AND i.vigente = 1 AND EXISTS (
               SELECT 1 FROM stock_movimientos m WHERE m.orden_item_id = i.id
                 AND m.tipo = 'consumo'
                 AND NOT EXISTS (SELECT 1 FROM stock_movimientos rev
                                 WHERE rev.revierte_movimiento_id = m.id)
           )""",
        (orden_id,),
    ).fetchall()
    por_id = {str(item.get("id", "")): item for item in items}
    errores = []
    for previa in movidas:
        nueva = por_id.get(str(previa["id"]))
        if nueva is None:
            errores.append(
                f"No quites «{previa['descripcion']}»: tiene historial de movimientos de stock."
            )
            continue
        catalogo_id = (
            int(nueva["catalogo_id"])
            if str(nueva.get("catalogo_id", "")).isdecimal()
            and nueva["tipo"] == "Repuesto"
            and not nueva["del_cliente"]
            else None
        )
        try:
            cantidad = a_milesimas(str(nueva["cantidad"]))
        except ValueError:
            cantidad = None
        if (
            nueva["tipo"] != previa["tipo"]
            or nueva["descripcion"] != previa["descripcion"]
            or cantidad != a_milesimas(str(previa["cantidad"]))
            or bool(nueva["del_cliente"]) != bool(previa["del_cliente"])
            or catalogo_id != previa["catalogo_id"]
        ):
            errores.append(
                f"Reversa el movimiento de «{previa['descripcion']}» antes de cambiar su artículo o cantidad."
            )
    return errores


@app.route("/ordenes/<int:orden_id>")
def orden_detalle(orden_id):
    orden = obtener_orden_o_404(orden_id)
    db = get_db()
    items = obtener_items(orden_id)
    lineas_reemplazadas = db.execute(
        "SELECT * FROM orden_items WHERE orden_id = ? AND vigente = 0 ORDER BY id DESC",
        (orden_id,),
    ).fetchall()
    pagos = obtener_pagos(orden_id)
    historial = []
    if es_dueno():
        historial = get_db().execute(
            """SELECT a.*, u.nombre AS usuario_nombre
               FROM actividad a LEFT JOIN usuarios u ON u.id = a.usuario_id
               WHERE a.orden_id = ? ORDER BY a.momento, a.id""",
            (orden_id,),
        ).fetchall()
    return render_template(
        "orden_detalle.html",
        orden=orden,
        items=items,
        lineas_reemplazadas=lineas_reemplazadas,
        pagos=pagos,
        costos=resumir_costos(items),
        historial=historial,
    )


@app.route("/ordenes/<int:orden_id>/editar", methods=["GET", "POST"])
def orden_editar(orden_id):
    existente = obtener_orden_o_404(orden_id)
    if existente["anulada"]:
        flash("Una orden anulada se conserva en el historial y no se puede editar.", "error")
        return redirect(url_for("orden_detalle", orden_id=orden_id))
    if existente["estado"] == "Completado" and not es_dueno():
        abort(403)  # Una orden completada solo la modifica el dueño.
    if request.method == "GET":
        lineas = [
            {
                "item_id": str(item["id"]),
                "tipo": item["tipo"],
                "descripcion": item["descripcion"],
                "cantidad": f"{item['cantidad']:.10g}",
                # Una línea sin precio se muestra vacía, no en 0, para que se note.
                "precio": "" if item["falta_precio"] else filtro_importe_input(item["precio_unitario_centavos"]),
                "costo": importe_opcional(item["costo_unitario_centavos"]),
                "del_cliente": bool(item["del_cliente"]),
                "falta_precio": bool(item["falta_precio"]),
                "catalogo_id": item["catalogo_id"] or "",
            }
            for item in obtener_items(orden_id)
        ]
        orden = dict(existente)
        if orden["kilometraje"] is None:
            orden["kilometraje"] = ""
        return render_orden_form(orden, lineas, [], orden_id)

    # Si guarda un empleado, las líneas que ya existían conservan los precios del dueño.
    guardadas = None if es_dueno() else {str(item["id"]): item for item in obtener_items(orden_id)}
    orden, lineas, items, errores = leer_orden(request.form, guardadas, existente)
    if not errores:
        errores.extend(validar_lineas_con_stock(orden_id, items))
    if errores:
        return render_orden_form(orden, lineas, errores, orden_id)

    db = get_db()
    with db:
        db.execute(
            """UPDATE ordenes
               SET vehiculo_id = ?, fallas = ?, diagnostico = ?, notas = ?, estado = ?,
                   kilometraje = ?, fecha_ingreso = ?, fecha_completado = ?,
                   fecha_prometida = ?, prioridad = ?
               WHERE id = ?""",
            (
                int(orden["vehiculo_id"]),
                orden["fallas"],
                orden["diagnostico"],
                orden["notas"],
                orden["estado"],
                int(orden["kilometraje"]) if orden["kilometraje"] else None,
                orden["fecha_ingreso"],
                fecha_completado_para(orden["estado"], existente["fecha_completado"]),
                orden["fecha_prometida"] or None,
                orden["prioridad"],
                orden_id,
            ),
        )
        guardar_items(db, orden_id, items)
    detalle = describir_orden(orden_id)
    if orden["estado"] != existente["estado"]:
        detalle += f" · estado: {existente['estado']} → {orden['estado']}"
    registrar("Editó la orden", detalle, orden_id)
    flash("Orden actualizada.", "ok")
    return redirect(url_for("orden_detalle", orden_id=orden_id))


def problema_cambio_estado(orden, estado):
    """Motivo por el que no se puede pasar la orden a ese estado, o None si se puede."""
    return _problema_cambio_estado(orden, estado, ESTADOS, es_dueno(), filtro_num_ot)


def guardar_estado(orden, estado):
    db = get_db()
    with db:
        db.execute(
            "UPDATE ordenes SET estado = ?, fecha_completado = ? WHERE id = ?",
            (estado, fecha_completado_para(estado, orden["fecha_completado"]), orden["id"]),
        )
    registrar(
        "Cambió el estado",
        f"{describir_orden(orden['id'])} · {orden['estado']} → {estado}",
        orden["id"],
    )


@app.route("/ordenes/<int:orden_id>/estado", methods=["POST"])
def orden_cambiar_estado(orden_id):
    existente = obtener_orden_o_404(orden_id)
    estado = request.form.get("estado", "")
    if estado not in ESTADOS:
        abort(400, "Estado no válido.")
    problema = problema_cambio_estado(existente, estado)
    if problema:
        flash(problema, "error")
    else:
        guardar_estado(existente, estado)
        flash(f"Estado cambiado a {estado}.", "ok")
    return redirect(url_for("orden_detalle", orden_id=orden_id))


@app.route("/api/ordenes/<int:orden_id>/estado", methods=["POST"])
def api_orden_estado(orden_id):
    """Cambio de estado desde una lista: devuelve la fila y los contadores actualizados."""
    existente = obtener_orden_o_404(orden_id)
    estado = request.form.get("estado", "")
    problema = problema_cambio_estado(existente, estado)
    if problema:
        return jsonify(error=problema), 400
    guardar_estado(existente, estado)
    orden = preparar_fila(obtener_orden_o_404(orden_id))
    return jsonify(
        fila=render_template("_fila_orden.html", o=orden),
        conteos=conteos_por_estado(),
    )


@app.route("/ordenes/<int:orden_id>/rapida")
def orden_rapida(orden_id):
    """Fragmento HTML con el resumen de la orden para la ventana de las listas."""
    return render_template(
        "_orden_rapida.html", orden=obtener_orden_o_404(orden_id), items=obtener_items(orden_id)
    )


MEDIOS_PAGO = ("Efectivo", "Transferencia", "Débito", "Crédito", "Otro")
RUBROS_GASTO = ("Alquiler", "Servicios", "Sueldos", "Impuestos", "Herramientas", "Mantenimiento", "Otros")


def total_pagado_orden(orden_id):
    return get_db().execute(
        """SELECT COALESCE(SUM(CASE WHEN tipo = 'reversion' THEN -importe_centavos
                                    ELSE importe_centavos END), 0) AS total
           FROM orden_pagos WHERE orden_id = ?""",
        (orden_id,),
    ).fetchone()["total"]


@app.route("/ordenes/<int:orden_id>/pagos", methods=["POST"])
@solo_dueno
def orden_pago_nuevo(orden_id):
    get_db().execute("SELECT id FROM ordenes WHERE id = ?", (orden_id,)).fetchone() or abort(404)
    medio = request.form.get("medio", "").strip()
    referencia = request.form.get("referencia", "").strip()
    texto_fecha = request.form.get("fecha", date.today().isoformat()).strip()
    try:
        importe = a_centavos(a_decimal(request.form.get("importe", "")))
        fecha_pago = date.fromisoformat(texto_fecha)
    except ValueError:
        flash("Revisa el importe y la fecha del cobro.", "error")
        return redirect(url_for("orden_detalle", orden_id=orden_id))
    if importe <= 0:
        flash("El importe del cobro debe ser mayor que cero.", "error")
    elif medio not in MEDIOS_PAGO:
        flash("Selecciona un medio de pago válido.", "error")
    elif fecha_pago > date.today():
        flash("La fecha del cobro no puede ser futura.", "error")
    elif len(referencia) > 120:
        flash("La referencia admite hasta 120 caracteres.", "error")
    else:
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        if caja_fecha_cerrada(fecha_pago.isoformat(), db):
            db.rollback()
            flash("La fecha corresponde a una caja cerrada. Reabre el cierre para registrar el cobro.", "error")
        else:
            orden = obtener_orden_o_404(orden_id)
            saldo = orden["total"] - total_pagado_orden(orden_id)
            if orden["anulada"]:
                db.rollback()
                flash("No se puede cobrar una orden anulada.", "error")
            elif orden["cobro_sin_conciliar"]:
                db.rollback()
                flash("Primero concilia los cobros históricos de esta orden.", "error")
            elif importe > saldo:
                db.rollback()
                flash("El importe supera el saldo pendiente de la orden.", "error")
            else:
                try:
                    db.execute(
                        """INSERT INTO orden_pagos
                           (orden_id, tipo, importe_centavos, medio, referencia, fecha, usuario_id, creado_en)
                           VALUES (?, 'cobro', ?, ?, ?, ?, ?, ?)""",
                        (
                            orden_id,
                            importe,
                            medio,
                            referencia,
                            fecha_pago.isoformat(),
                            g.usuario["id"],
                            datetime.now().isoformat(timespec="seconds"),
                        ),
                    )
                    db.commit()
                except sqlite3.Error:
                    db.rollback()
                    app.logger.exception("No se pudo registrar un cobro para orden_id=%s", orden_id)
                    flash("No se pudo guardar el cobro. Vuelve a intentar.", "error")
                    return redirect(url_for("orden_detalle", orden_id=orden_id))
                registrar("Registró un cobro", f"{filtro_num_ot(orden_id)} · {medio} · {importe} centavos", orden_id)
                flash("Cobro registrado.", "ok")
    return redirect(url_for("orden_detalle", orden_id=orden_id))


@app.route("/ordenes/<int:orden_id>/pagos/<int:pago_id>/revertir", methods=["POST"])
@solo_dueno
def orden_pago_revertir(orden_id, pago_id):
    motivo = request.form.get("motivo", "").strip()
    if len(motivo) < 5 or len(motivo) > 500:
        flash("Indica el motivo de la reversión (entre 5 y 500 caracteres).", "error")
        return redirect(url_for("orden_detalle", orden_id=orden_id))
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    orden = db.execute("SELECT id FROM ordenes WHERE id = ?", (orden_id,)).fetchone()
    pago = db.execute(
        "SELECT * FROM orden_pagos WHERE id = ? AND orden_id = ?", (pago_id, orden_id)
    ).fetchone()
    if orden is None or pago is None:
        db.rollback()
        abort(404)
    if pago["tipo"] not in ("cobro", "apertura"):
        db.rollback()
        flash("Solo se puede revertir un movimiento de cobro o saldo inicial.", "error")
    elif db.execute("SELECT 1 FROM orden_pagos WHERE revierte_id = ?", (pago_id,)).fetchone():
        db.rollback()
        flash("Ese movimiento ya tiene una reversión registrada.", "error")
    elif caja_fecha_cerrada(date.today().isoformat(), db):
        db.rollback()
        flash("Reabre la caja de hoy antes de registrar esta reversión.", "error")
    else:
        try:
            db.execute(
                """INSERT INTO orden_pagos
                   (orden_id, tipo, importe_centavos, medio, referencia, fecha,
                    usuario_id, revierte_id, motivo, creado_en)
                   VALUES (?, 'reversion', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    orden_id,
                    pago["importe_centavos"],
                    pago["medio"],
                    pago["referencia"],
                    date.today().isoformat(),
                    g.usuario["id"],
                    pago_id,
                    motivo,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            app.logger.exception("No se pudo registrar una reversión de cobro para orden_id=%s", orden_id)
            flash("No se pudo revertir el cobro. Vuelve a intentar.", "error")
            return redirect(url_for("orden_detalle", orden_id=orden_id))
        registrar("Revirtió un cobro", f"{filtro_num_ot(orden_id)} · {motivo}", orden_id)
        flash("Reversión registrada. El cobro original permanece en el historial.", "ok")
    return redirect(url_for("orden_detalle", orden_id=orden_id))


@app.route("/ordenes/<int:orden_id>/conciliar", methods=["POST"])
@solo_dueno
def orden_cobros_conciliar(orden_id):
    orden = obtener_orden_o_404(orden_id)
    motivo = request.form.get("motivo", "").strip()
    try:
        pagado_historico = a_centavos(a_decimal(request.form.get("importe_pagado", "")))
    except ValueError:
        flash("Indica cuánto estaba pagado antes de comenzar a registrar cobros.", "error")
        return redirect(url_for("orden_detalle", orden_id=orden_id))
    if orden["anulada"]:
        flash("No se puede conciliar una orden anulada.", "error")
    elif not orden["cobro_sin_conciliar"]:
        flash("La orden ya está conciliada.", "error")
    elif pagado_historico > orden["total"]:
        flash("El cobro histórico no puede superar el total de la orden.", "error")
    elif len(motivo) < 5 or len(motivo) > 500:
        flash("Indica el motivo de conciliación (entre 5 y 500 caracteres).", "error")
    else:
        db = get_db()
        with db:
            if pagado_historico:
                db.execute(
                    """INSERT INTO orden_pagos
                       (orden_id, tipo, importe_centavos, medio, referencia, fecha,
                        usuario_id, motivo, creado_en)
                       VALUES (?, 'apertura', ?, 'Saldo histórico', '', ?, ?, ?, ?)""",
                    (
                        orden_id,
                        pagado_historico,
                        date.today().isoformat(),
                        g.usuario["id"],
                        motivo,
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
            db.execute("UPDATE ordenes SET cobro_sin_conciliar = 0 WHERE id = ?", (orden_id,))
        registrar(
            "Concilió el saldo de una orden",
            f"{filtro_num_ot(orden_id)} · {pagado_historico} centavos · {motivo}",
            orden_id,
        )
        flash("Saldo histórico conciliado; el ajuste no se cuenta como cobro de hoy.", "ok")
    return redirect(url_for("orden_detalle", orden_id=orden_id))


@app.route("/ordenes/<int:orden_id>/anular", methods=["POST"])
@solo_dueno
def orden_anular(orden_id):
    get_db().execute("SELECT id FROM ordenes WHERE id = ?", (orden_id,)).fetchone() or abort(404)
    motivo = request.form.get("motivo", "").strip()
    if len(motivo) < 5 or len(motivo) > 500:
        flash("Indica el motivo de anulación (entre 5 y 500 caracteres).", "error")
        return redirect(url_for("orden_detalle", orden_id=orden_id))
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    orden = obtener_orden_o_404(orden_id)
    if orden["anulada"]:
        db.rollback()
        flash("La orden ya está anulada.", "error")
    elif orden["cobro_sin_conciliar"]:
        db.rollback()
        flash("Primero concilia los cobros históricos de esta orden.", "error")
    elif total_pagado_orden(orden_id) > 0:
        db.rollback()
        flash("Reversa los cobros antes de anular esta orden.", "error")
    elif db.execute(
        """SELECT 1 FROM orden_items i WHERE i.orden_id = ? AND i.vigente = 1 AND (
               SELECT COALESCE(SUM(m.cantidad_milesimas + COALESCE((
                   SELECT SUM(rev.cantidad_milesimas) FROM stock_movimientos rev
                   WHERE rev.revierte_movimiento_id = m.id
               ), 0)), 0) FROM stock_movimientos m
               WHERE m.orden_item_id = i.id
           ) < 0 LIMIT 1""",
        (orden_id,),
    ).fetchone():
        db.rollback()
        flash("Reversa los consumos de repuestos desde Inventario antes de anular esta orden.", "error")
    else:
        db.execute(
            """INSERT INTO orden_anulaciones (orden_id, motivo, usuario_id, anulada_en)
               VALUES (?, ?, ?, ?)""",
            (orden_id, motivo, g.usuario["id"], datetime.now().isoformat(timespec="seconds")),
        )
        db.commit()
        registrar("Anuló la orden", f"{describir_orden(orden_id)} · {motivo}", orden_id)
        flash("Orden anulada. Sus datos permanecen en el historial.", "ok")
    return redirect(url_for("orden_detalle", orden_id=orden_id))


@app.route("/ordenes/<int:orden_id>/eliminar", methods=["POST"])
@solo_dueno
def orden_eliminar(orden_id):
    obtener_orden_o_404(orden_id)
    abort(410, "Las órdenes se anulan con un motivo y se conservan en el historial.")


@app.route("/ordenes/<int:orden_id>/imprimir")
@solo_dueno
def orden_imprimir(orden_id):
    return render_template(
        "orden_imprimir.html", orden=obtener_orden_o_404(orden_id), items=obtener_items(orden_id)
    )


# ---------------------------------------------------------------------------
# Proveedores, compras e inventario
# ---------------------------------------------------------------------------
def leer_proveedor(form):
    datos = {
        campo: form.get(campo, "").strip()
        for campo in ("nombre", "documento", "telefono", "email", "direccion")
    }
    errores = []
    if not datos["nombre"]:
        errores.append("El nombre del proveedor es obligatorio.")
    if len(datos["nombre"]) > 120 or len(datos["documento"]) > 32:
        errores.append("Revisa el largo del nombre o documento del proveedor.")
    if len(datos["telefono"]) > 40 or len(datos["email"]) > 160 or len(datos["direccion"]) > 240:
        errores.append("Revisa los datos de contacto del proveedor.")
    if datos["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", datos["email"]):
        errores.append("El email del proveedor no tiene un formato válido.")
    return datos, errores


@app.route("/proveedores", methods=["GET", "POST"])
@solo_dueno
def proveedores():
    db = get_db()
    editar_id = request.args.get("editar", type=int)
    busqueda = request.args.get("q", "").strip()[:120]
    estado = request.args.get("estado", "todos").strip()
    if estado not in ("todos", "activos", "archivados"):
        estado = "todos"
    errores = []
    if request.method == "POST":
        datos, errores = leer_proveedor(request.form)
        try:
            if not errores:
                with db:
                    if editar_id:
                        db.execute(
                            """UPDATE proveedores SET nombre = ?, documento = ?, telefono = ?,
                               email = ?, direccion = ? WHERE id = ?""",
                            (*datos.values(), editar_id),
                        )
                    else:
                        db.execute(
                            """INSERT INTO proveedores (nombre, documento, telefono, email, direccion, creado_en)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (*datos.values(), datetime.now().isoformat(timespec="seconds")),
                        )
        except sqlite3.IntegrityError:
            errores.append("Ya existe un proveedor con ese nombre.")
        else:
            if not errores:
                registrar("Actualizó proveedores", datos["nombre"])
                flash("Proveedor guardado.", "ok")
                return redirect(url_for("proveedores"))
        item = datos
    elif editar_id:
        fila = db.execute("SELECT * FROM proveedores WHERE id = ?", (editar_id,)).fetchone()
        if fila is None:
            abort(404)
        item = dict(fila)
    else:
        item = {"nombre": "", "documento": "", "telefono": "", "email": "", "direccion": ""}
    condiciones = []
    parametros = []
    if busqueda:
        patron = f"%{busqueda}%"
        condiciones.append("(p.nombre LIKE ? OR p.documento LIKE ? OR p.telefono LIKE ? OR p.email LIKE ?)")
        parametros.extend((patron, patron, patron, patron))
    if estado == "activos":
        condiciones.append("p.activo = 1")
    elif estado == "archivados":
        condiciones.append("p.activo = 0")
    where = " WHERE " + " AND ".join(condiciones) if condiciones else ""
    total = db.execute("SELECT COUNT(*) AS total FROM proveedores p" + where, parametros).fetchone()["total"]
    paginacion = paginar(total)
    filas = db.execute(
        """SELECT p.*, COUNT(DISTINCT c.id) AS compras_total
           FROM proveedores p LEFT JOIN compras c ON c.proveedor_id = p.id
           """ + where + " GROUP BY p.id ORDER BY p.activo DESC, p.nombre COLLATE NOCASE LIMIT ? OFFSET ?",
        (*parametros, POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    return render_template(
        "proveedores.html", proveedores=filas, item=item, editar_id=editar_id, errores=errores,
        paginacion=paginacion, busqueda=busqueda, estado=estado,
    )


@app.route("/proveedores/<int:proveedor_id>")
@solo_dueno
def proveedor_detalle(proveedor_id):
    db = get_db()
    proveedor = db.execute("SELECT * FROM proveedores WHERE id = ?", (proveedor_id,)).fetchone()
    if proveedor is None:
        abort(404)
    filas = db.execute(
        """SELECT c.*,
                  (SELECT COUNT(*) FROM compra_items i WHERE i.compra_id = c.id) AS lineas
           FROM compras c WHERE c.proveedor_id = ? ORDER BY c.fecha DESC, c.id DESC""",
        (proveedor_id,),
    ).fetchall()
    importes_pedidos = importes_pedidos_compra([fila["id"] for fila in filas], db)
    compras = []
    recibido_total = pagado_total = saldo_total = 0
    for fila in filas:
        finanzas = cuenta_compra(fila["id"], db)
        recibido_total += finanzas["total"]
        pagado_total += finanzas["pagado"]
        saldo_total += finanzas["saldo"]
        compras.append({
            **dict(fila), **finanzas,
            "valor_pedido_centavos": importes_pedidos.get(fila["id"], 0),
        })
    paginacion = paginar(len(compras))
    visibles = compras[paginacion["offset"]:paginacion["offset"] + POR_PAGINA]
    return render_template(
        "proveedor_detalle.html", proveedor=proveedor, compras=visibles,
        paginacion=paginacion, compras_total=len(compras),
        recibido_total=recibido_total, pagado_total=pagado_total, saldo_total=saldo_total,
    )


@app.route("/proveedores/<int:proveedor_id>/estado", methods=["POST"])
@solo_dueno
def proveedor_cambiar_estado(proveedor_id):
    proveedor = obtener_o_404("proveedores", proveedor_id)
    activo = 0 if proveedor["activo"] else 1
    with get_db():
        get_db().execute("UPDATE proveedores SET activo = ? WHERE id = ?", (activo, proveedor_id))
    registrar("Cambió estado de proveedor", proveedor["nombre"])
    flash("Proveedor actualizado.", "ok")
    return redirect(url_for("proveedores"))


def cantidades_recibidas(compra_item_id, db=None):
    return _cantidades_recibidas(db or get_db(), compra_item_id)


def actualizar_estado_compra(db, compra_id):
    return _actualizar_estado_compra(db, compra_id)


def articulos_de_compra(compra_id, db=None):
    return _articulos_de_compra(db or get_db(), compra_id)


def importes_pedidos_compra(compra_ids, db=None):
    """Devuelve el importe pedido por compra, sumando líneas redondeadas a centavos."""
    return _importes_pedidos_compra(db or get_db(), compra_ids)


def cuenta_compra(compra_id, db=None):
    return _cuenta_compra(db or get_db(), compra_id)


@app.route("/compras")
@solo_dueno
def compras():
    db = get_db()
    busqueda = request.args.get("q", "").strip()[:120]
    estado = request.args.get("estado", "todos").strip()
    if estado not in ("Borrador", "Parcial", "Recibida", "Anulada"):
        estado = "todos"
    condiciones = []
    parametros = []
    if busqueda:
        patron = f"%{busqueda}%"
        condiciones.append("(CAST(c.id AS TEXT) LIKE ? OR c.documento LIKE ? OR p.nombre LIKE ?)")
        parametros.extend((patron, patron, patron))
    if estado != "todos":
        condiciones.append("c.estado = ?")
        parametros.append(estado)
    where = " WHERE " + " AND ".join(condiciones) if condiciones else ""
    total = db.execute(
        "SELECT COUNT(*) AS total FROM compras c JOIN proveedores p ON p.id = c.proveedor_id" + where,
        parametros,
    ).fetchone()["total"]
    paginacion = paginar(total)
    filas = db.execute(
        """SELECT c.*, p.nombre AS proveedor,
                  (SELECT COUNT(*) FROM compra_items i WHERE i.compra_id = c.id) AS lineas
           FROM compras c JOIN proveedores p ON p.id = c.proveedor_id""" + where +
        " ORDER BY c.id DESC LIMIT ? OFFSET ?",
        (*parametros, POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    importes_pedidos = importes_pedidos_compra([fila["id"] for fila in filas], db)
    compras_visibles = [
        {**dict(fila), "valor_centavos": importes_pedidos.get(fila["id"], 0)}
        for fila in filas
    ]
    return render_template(
        "compras.html", compras=compras_visibles, paginacion=paginacion, busqueda=busqueda, estado=estado,
    )


@app.route("/cuentas-por-pagar")
@solo_dueno
def cuentas_pagar():
    db = get_db()
    busqueda = request.args.get("q", "").strip()[:120].casefold()
    compras_pendientes = db.execute(
        """SELECT c.id, c.fecha, c.documento, c.estado, p.nombre AS proveedor
           FROM compras c JOIN proveedores p ON p.id = c.proveedor_id
           WHERE c.estado NOT IN ('Borrador', 'Anulada') ORDER BY c.fecha, c.id"""
    ).fetchall()
    pendientes = []
    for compra in compras_pendientes:
        cuenta = cuenta_compra(compra["id"], db)
        if cuenta["saldo"] > 0:
            fila = {**dict(compra), **cuenta}
            texto_busqueda = " ".join(str(fila.get(campo) or "") for campo in (
                "id", "fecha", "documento", "estado", "proveedor"
            )).casefold()
            if not busqueda or busqueda in texto_busqueda:
                pendientes.append(fila)
    paginacion = paginar(len(pendientes))
    return render_template(
        "cuentas_pagar.html",
        compras=pendientes[paginacion["offset"]:paginacion["offset"] + POR_PAGINA],
        saldo_total=sum(c["saldo"] for c in pendientes),
        paginacion=paginacion, busqueda=busqueda,
    )


@app.route("/compras/nueva", methods=["GET", "POST"])
@solo_dueno
def compra_nueva():
    db = get_db()
    proveedores_activos = db.execute(
        "SELECT id, nombre FROM proveedores WHERE activo = 1 ORDER BY nombre COLLATE NOCASE"
    ).fetchall()
    articulos_activos = db.execute(
        """SELECT id, sku, nombre, unidad, costo_centavos FROM repuestos_servicios
           WHERE activo = 1 AND tipo = 'Repuesto' AND controla_stock = 1
           ORDER BY nombre COLLATE NOCASE"""
    ).fetchall()
    errores = []
    datos = {
        "proveedor_id": request.form.get("proveedor_id", request.args.get("proveedor_id", "")),
        "fecha": request.form.get("fecha", date.today().isoformat()),
        "documento": request.form.get("documento", "").strip(),
        "nota": request.form.get("nota", "").strip(),
    }
    lineas_form = []
    if request.method == "POST":
        proveedor = db.execute(
            "SELECT id FROM proveedores WHERE id = ? AND activo = 1", (datos["proveedor_id"],)
        ).fetchone()
        if proveedor is None:
            errores.append("Selecciona un proveedor activo.")
        try:
            date.fromisoformat(datos["fecha"])
        except ValueError:
            errores.append("La fecha de compra no es válida.")
        if len(datos["documento"]) > 100 or len(datos["nota"]) > 500:
            errores.append("El documento admite 100 caracteres y la nota 500.")
        articulos_form = request.form.getlist("articulo_id")
        cantidades_form = request.form.getlist("cantidad")
        costos_form = request.form.getlist("costo_unitario")
        if not (len(articulos_form) == len(cantidades_form) == len(costos_form)):
            errores.append("Revisa las líneas de la compra.")
        lineas = []
        for articulo_texto, cantidad_texto, costo_texto in zip(articulos_form, cantidades_form, costos_form):
            if not articulo_texto and not cantidad_texto and not costo_texto:
                continue
            try:
                articulo_id = int(articulo_texto)
                articulo = db.execute(
                    """SELECT id FROM repuestos_servicios
                       WHERE id = ? AND tipo = 'Repuesto' AND controla_stock = 1 AND activo = 1""",
                    (articulo_id,),
                ).fetchone()
                cantidad = a_milesimas(cantidad_texto)
                costo = a_centavos(a_decimal(costo_texto))
                if articulo is None:
                    raise ValueError("artículo no inventariable")
            except (ValueError, TypeError):
                errores.append("Cada línea requiere artículo inventariable, cantidad y costo válidos.")
                continue
            lineas.append((articulo_id, cantidad, costo))
            lineas_form.append({"articulo_id": articulo_texto, "cantidad": cantidad_texto, "costo_unitario": costo_texto})
        if not lineas:
            errores.append("Agrega al menos un artículo a la compra.")
        if not errores:
            try:
                with db:
                    cursor = db.execute(
                        """INSERT INTO compras (proveedor_id, fecha, documento, nota, usuario_id, creada_en)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            int(datos["proveedor_id"]), datos["fecha"], datos["documento"], datos["nota"],
                            g.usuario["id"], datetime.now().isoformat(timespec="seconds"),
                        ),
                    )
                    compra_id = cursor.lastrowid
                    db.executemany(
                        """INSERT INTO compra_items (compra_id, articulo_id, cantidad_milesimas, costo_unitario_centavos)
                           VALUES (?, ?, ?, ?)""",
                        [(compra_id, *linea) for linea in lineas],
                    )
            except sqlite3.IntegrityError:
                errores.append("Ese número de documento ya está registrado para el proveedor.")
            else:
                registrar("Creó una compra", f"Compra {compra_id} · {datos['documento'] or 'sin documento'}")
                flash("Compra en borrador creada. Todavía no modificó existencias.", "ok")
                return redirect(url_for("compra_detalle", compra_id=compra_id))
    if not lineas_form:
        lineas_form = [{"articulo_id": "", "cantidad": "", "costo_unitario": ""}]
    return render_template(
        "compra_form.html", errores=errores, datos=datos, lineas=lineas_form,
        proveedores=proveedores_activos, articulos=articulos_activos,
    )


@app.route("/compras/<int:compra_id>")
@solo_dueno
def compra_detalle(compra_id):
    db = get_db()
    compra = db.execute(
        """SELECT c.*, p.nombre AS proveedor, p.documento AS proveedor_documento
           FROM compras c JOIN proveedores p ON p.id = c.proveedor_id WHERE c.id = ?""",
        (compra_id,),
    ).fetchone()
    if compra is None:
        abort(404)
    lineas = articulos_de_compra(compra_id, db)
    compra = {
        **dict(compra),
        "importe_pedido_centavos": sum(
            importe_linea_compra_centavos(linea["cantidad_milesimas"], linea["costo_unitario_centavos"])
            for linea in lineas
        ),
    }
    recepciones = db.execute(
        """SELECT r.*, u.nombre AS usuario_nombre,
                  COALESCE(SUM(ri.cantidad_milesimas), 0) AS cantidad_milesimas
           FROM compra_recepciones r LEFT JOIN compra_recepcion_items ri ON ri.recepcion_id = r.id
           LEFT JOIN usuarios u ON u.id = r.usuario_id WHERE r.compra_id = ?
           GROUP BY r.id ORDER BY r.id DESC""",
        (compra_id,),
    ).fetchall()
    pagos = db.execute(
        """SELECT pp.*, u.nombre AS usuario_nombre, rev.id AS reversion_id
           FROM proveedor_pagos pp LEFT JOIN usuarios u ON u.id = pp.usuario_id
           LEFT JOIN proveedor_pagos rev ON rev.revierte_id = pp.id
           WHERE pp.compra_id = ? ORDER BY pp.id DESC""",
        (compra_id,),
    ).fetchall()
    return render_template(
        "compra_detalle.html", compra=compra, lineas=lineas,
        recepciones=recepciones, solicitud_id=secrets.token_urlsafe(24),
        finanzas=cuenta_compra(compra_id, db), pagos=pagos,
    )


@app.route("/compras/<int:compra_id>/pagos", methods=["POST"])
@solo_dueno
def proveedor_pago_nuevo(compra_id):
    referencia = request.form.get("referencia", "").strip()
    medio = request.form.get("medio", "").strip()
    try:
        importe = a_centavos(a_decimal(request.form.get("importe", "")))
        fecha_pago = date.fromisoformat(request.form.get("fecha", date.today().isoformat()).strip())
    except ValueError:
        flash("Revisa el importe y la fecha del pago.", "error")
        return redirect(url_for("compra_detalle", compra_id=compra_id))
    if importe <= 0 or medio not in MEDIOS_PAGO or len(referencia) > 120:
        flash("Indica un importe, medio y referencia válidos.", "error")
        return redirect(url_for("compra_detalle", compra_id=compra_id))
    if fecha_pago > date.today():
        flash("La fecha del pago no puede ser futura.", "error")
    else:
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        compra = db.execute("SELECT * FROM compras WHERE id = ?", (compra_id,)).fetchone()
        if compra is None:
            db.rollback()
            abort(404)
        if caja_fecha_cerrada(fecha_pago.isoformat(), db):
            db.rollback()
            flash("La fecha corresponde a una caja cerrada. Reabre el cierre para registrar el movimiento.", "error")
        else:
            finanzas = cuenta_compra(compra_id, db)
            if compra["estado"] in ("Borrador", "Anulada"):
                db.rollback()
                flash("Solo puedes pagar una compra con artículos recibidos.", "error")
            elif importe > finanzas["saldo"]:
                db.rollback()
                flash("El pago supera el saldo pendiente de la compra.", "error")
            else:
                with db:
                    pago_id = db.execute(
                        """INSERT INTO proveedor_pagos
                           (compra_id, tipo, importe_centavos, medio, referencia, fecha, usuario_id, creado_en)
                           VALUES (?, 'pago', ?, ?, ?, ?, ?, ?)""",
                        (compra_id, importe, medio, referencia, fecha_pago.isoformat(), g.usuario["id"],
                         datetime.now().isoformat(timespec="seconds")),
                    ).lastrowid
                registrar("Pagó a un proveedor", f"Compra {compra_id} · {importe} centavos · {medio}")
                flash(f"Pago registrado (movimiento {pago_id}).", "ok")
    return redirect(url_for("compra_detalle", compra_id=compra_id))


@app.route("/compras/<int:compra_id>/pagos/<int:pago_id>/revertir", methods=["POST"])
@solo_dueno
def proveedor_pago_revertir(compra_id, pago_id):
    motivo = request.form.get("motivo", "").strip()
    if len(motivo) < 5 or len(motivo) > 500:
        flash("Indica el motivo de la reversión (entre 5 y 500 caracteres).", "error")
        return redirect(url_for("compra_detalle", compra_id=compra_id))
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    pago = db.execute(
        "SELECT * FROM proveedor_pagos WHERE id = ? AND compra_id = ? AND tipo = 'pago'",
        (pago_id, compra_id),
    ).fetchone()
    if pago is None:
        db.rollback()
        abort(404)
    if db.execute("SELECT 1 FROM proveedor_pagos WHERE revierte_id = ?", (pago_id,)).fetchone():
        db.rollback()
        flash("Ese pago ya tiene una reversión registrada.", "error")
    elif caja_fecha_cerrada(date.today().isoformat()):
        db.rollback()
        flash("Reabre la caja de hoy antes de registrar la reversión.", "error")
    else:
        with db:
            db.execute(
                """INSERT INTO proveedor_pagos
                   (compra_id, tipo, importe_centavos, medio, referencia, fecha, usuario_id,
                    revierte_id, motivo, creado_en)
                   VALUES (?, 'reversion', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (compra_id, pago["importe_centavos"], pago["medio"], pago["referencia"],
                 date.today().isoformat(), g.usuario["id"], pago_id, motivo,
                 datetime.now().isoformat(timespec="seconds")),
            )
        registrar("Revirtió un pago a proveedor", f"Compra {compra_id} · {motivo}")
        flash("Reversión registrada; el pago original permanece en el historial.", "ok")
    return redirect(url_for("compra_detalle", compra_id=compra_id))


@app.route("/compras/<int:compra_id>/recibir", methods=["POST"])
@solo_dueno
def compra_recibir(compra_id):
    db = get_db()
    solicitud_id = request.form.get("solicitud_id", "").strip()
    motivo = request.form.get("motivo", "").strip()
    if not solicitud_id or len(solicitud_id) > 100 or len(motivo) > 300:
        flash("Revisa la referencia de recepción.", "error")
        return redirect(url_for("compra_detalle", compra_id=compra_id))
    ids = request.form.getlist("compra_item_id")
    cantidades = request.form.getlist("cantidad_recibir")
    if len(ids) != len(cantidades):
        flash("Revisa las cantidades recibidas.", "error")
        return redirect(url_for("compra_detalle", compra_id=compra_id))
    try:
        lineas_pedidas = []
        for linea_id, cantidad_texto in zip(ids, cantidades):
            if not cantidad_texto.strip():
                continue
            cantidad = a_milesimas(cantidad_texto)
            lineas_pedidas.append((int(linea_id), cantidad))
    except ValueError:
        flash("Cada cantidad recibida debe ser mayor que cero y tener hasta tres decimales.", "error")
        return redirect(url_for("compra_detalle", compra_id=compra_id))
    if not lineas_pedidas:
        flash("Indica al menos una cantidad recibida.", "error")
        return redirect(url_for("compra_detalle", compra_id=compra_id))

    db.execute("BEGIN IMMEDIATE")
    recibida_previa = db.execute(
        "SELECT compra_id FROM compra_recepciones WHERE solicitud_id = ?", (solicitud_id,)
    ).fetchone()
    if recibida_previa:
        db.rollback()
        if recibida_previa["compra_id"] == compra_id:
            flash("Esta recepción ya se registró; las existencias no se duplicaron.", "ok")
            return redirect(url_for("compra_detalle", compra_id=compra_id))
        abort(409)
    compra = db.execute("SELECT * FROM compras WHERE id = ?", (compra_id,)).fetchone()
    if compra is None:
        db.rollback()
        abort(404)
    if compra["estado"] in ("Recibida", "Anulada"):
        db.rollback()
        flash("La compra no admite más recepciones.", "error")
        return redirect(url_for("compra_detalle", compra_id=compra_id))

    lineas_validas = []
    for linea_id, cantidad in lineas_pedidas:
        linea = db.execute(
            "SELECT * FROM compra_items WHERE id = ? AND compra_id = ?", (linea_id, compra_id)
        ).fetchone()
        if linea is None or cantidad > linea["cantidad_milesimas"] - cantidades_recibidas(linea_id, db):
            db.rollback()
            flash("La cantidad recibida supera lo pendiente o la línea no pertenece a esta compra.", "error")
            return redirect(url_for("compra_detalle", compra_id=compra_id))
        lineas_validas.append((linea, cantidad))
    try:
        with db:
            recepcion_id = db.execute(
                """INSERT INTO compra_recepciones
                   (compra_id, solicitud_id, fecha, motivo, usuario_id, creada_en)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    compra_id, solicitud_id, date.today().isoformat(), motivo, g.usuario["id"],
                    datetime.now().isoformat(timespec="seconds"),
                ),
            ).lastrowid
            for linea, cantidad in lineas_validas:
                recepcion_item_id = db.execute(
                    """INSERT INTO compra_recepcion_items
                       (recepcion_id, compra_item_id, cantidad_milesimas) VALUES (?, ?, ?)""",
                    (recepcion_id, linea["id"], cantidad),
                ).lastrowid
                articulo_id = linea["articulo_id"]
                insertar_movimiento_stock(
                    db, articulo_id, "recepcion", cantidad, linea["costo_unitario_centavos"],
                    motivo or f"Recepción de compra {compra_id}", recepcion_item_id=recepcion_item_id,
                )
                db.execute(
                    "UPDATE repuestos_servicios SET costo_centavos = ? WHERE id = ?",
                    (costo_promedio_articulo(articulo_id, db), articulo_id),
                )
            estado = actualizar_estado_compra(db, compra_id)
    except sqlite3.IntegrityError:
        db.rollback()
        app.logger.exception("No se pudo registrar una recepción para compra_id=%s", compra_id)
        flash("No se pudo registrar la recepción; recarga la compra e inténtalo de nuevo.", "error")
        return redirect(url_for("compra_detalle", compra_id=compra_id))
    cantidad_total = sum(cantidad for _, cantidad in lineas_validas)
    registrar("Recibió artículos", f"Compra {compra_id} · {filtro_cantidad_stock(cantidad_total)} unidades en total")
    flash(f"Recepción registrada. Estado de compra: {estado.lower()}.", "ok")
    return redirect(url_for("compra_detalle", compra_id=compra_id))


@app.route("/compras/<int:compra_id>/anular", methods=["POST"])
@solo_dueno
def compra_anular(compra_id):
    compra = get_db().execute("SELECT * FROM compras WHERE id = ?", (compra_id,)).fetchone()
    if compra is None:
        abort(404)
    motivo = request.form.get("motivo", "").strip()
    if compra["estado"] != "Borrador":
        flash("Una compra con recepciones requiere revertir sus movimientos antes de corregirla.", "error")
    elif len(motivo) < 5 or len(motivo) > 500:
        flash("Indica el motivo de anulación (entre 5 y 500 caracteres).", "error")
    else:
        with get_db():
            get_db().execute(
                "UPDATE compras SET estado = 'Anulada', anulada_en = ?, motivo_anulacion = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), motivo, compra_id),
            )
        registrar("Anuló una compra", f"Compra {compra_id} · {motivo}")
        flash("Compra anulada; su historial se conserva.", "ok")
    return redirect(url_for("compra_detalle", compra_id=compra_id))


@app.route("/inventario")
@solo_dueno
def inventario():
    db = get_db()
    articulos = db.execute(
        """SELECT r.*, COALESCE(SUM(m.cantidad_milesimas), 0) AS stock_milesimas,
                  COUNT(m.id) AS movimientos_total, MAX(m.creado_en) AS ultimo_movimiento,
                  CASE WHEN COALESCE(SUM(m.cantidad_milesimas), 0) > 0
                       THEN CAST(ROUND(
                           CAST(SUM(m.cantidad_milesimas * COALESCE(m.costo_unitario_centavos, 0)) AS REAL)
                           / SUM(m.cantidad_milesimas)
                       ) AS INTEGER)
                       ELSE COALESCE(r.costo_centavos, 0) END AS costo_promedio_centavos
           FROM repuestos_servicios r LEFT JOIN stock_movimientos m ON m.articulo_id = r.id
           WHERE r.tipo = 'Repuesto' AND r.controla_stock = 1
           GROUP BY r.id ORDER BY r.activo DESC, r.nombre COLLATE NOCASE"""
    ).fetchall()
    articulos_vista = []
    valor_stock_total_centavos = 0
    articulos_sin_movimiento = 0
    for fila in articulos:
        articulo = dict(fila)
        articulo["valor_stock_centavos"] = int(
            (Decimal(articulo["stock_milesimas"]) * Decimal(articulo["costo_promedio_centavos"]) / 1000)
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        if articulo["activo"]:
            valor_stock_total_centavos += articulo["valor_stock_centavos"]
            if not articulo["movimientos_total"]:
                articulos_sin_movimiento += 1
        articulos_vista.append(articulo)
    busqueda_movimientos = request.args.get("q", "").strip()[:120]
    where_movimientos = ""
    parametros_movimientos = []
    if busqueda_movimientos:
        patron = f"%{busqueda_movimientos}%"
        where_movimientos = " WHERE r.nombre LIKE ? OR r.sku LIKE ? OR m.tipo LIKE ? OR m.motivo LIKE ? OR u.nombre LIKE ?"
        parametros_movimientos.extend((patron, patron, patron, patron, patron))
    total_movimientos = db.execute(
        """SELECT COUNT(*) AS total FROM stock_movimientos m
           JOIN repuestos_servicios r ON r.id = m.articulo_id
           LEFT JOIN usuarios u ON u.id = m.usuario_id""" + where_movimientos,
        parametros_movimientos,
    ).fetchone()["total"]
    paginacion_movimientos = paginar(total_movimientos)
    movimientos = db.execute(
        """SELECT m.*, r.nombre AS articulo, r.sku, r.unidad, u.nombre AS usuario_nombre,
                  EXISTS(SELECT 1 FROM stock_movimientos rev
                         WHERE rev.revierte_movimiento_id = m.id) AS revertido
           FROM stock_movimientos m JOIN repuestos_servicios r ON r.id = m.articulo_id
           LEFT JOIN usuarios u ON u.id = m.usuario_id""" + where_movimientos +
        " ORDER BY m.id DESC LIMIT ? OFFSET ?",
        (*parametros_movimientos, POR_PAGINA, paginacion_movimientos["offset"]),
    ).fetchall()
    return render_template(
        "inventario.html", articulos=articulos_vista, movimientos=movimientos,
        valor_stock_total_centavos=valor_stock_total_centavos,
        articulos_sin_movimiento=articulos_sin_movimiento,
        busqueda_movimientos=busqueda_movimientos,
        paginacion_movimientos=paginacion_movimientos,
    )


@app.route("/inventario/articulos/<int:articulo_id>/ajustar", methods=["POST"])
@solo_dueno
def inventario_ajustar(articulo_id):
    motivo = request.form.get("motivo", "").strip()
    try:
        contado = a_milesimas(request.form.get("cantidad", ""), permitir_cero=True)
    except ValueError:
        flash("La cantidad contada debe ser un número con hasta tres decimales.", "error")
        return redirect(url_for("inventario"))
    if len(motivo) < 5 or len(motivo) > 500:
        flash("Indica el motivo del ajuste (entre 5 y 500 caracteres).", "error")
        return redirect(url_for("inventario"))
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    articulo = db.execute(
        """SELECT id, nombre, costo_centavos FROM repuestos_servicios
           WHERE id = ? AND controla_stock = 1 AND activo = 1""",
        (articulo_id,),
    ).fetchone()
    if articulo is None:
        db.rollback()
        abort(404)
    actual = stock_articulo(articulo_id, db)
    diferencia = contado - actual
    if diferencia:
        costo = costo_promedio_articulo(articulo_id, db)
        with db:
            insertar_movimiento_stock(
                db, articulo_id, "ajuste", diferencia, costo, motivo,
            )
        registrar("Ajustó existencias", f"{articulo['nombre']} · {motivo} · {actual} → {contado} milésimas")
        flash("Ajuste de inventario registrado.", "ok")
    else:
        db.rollback()
        flash("El conteo coincide con el saldo; no hacía falta generar un movimiento.", "ok")
    return redirect(url_for("inventario"))


@app.route("/inventario/movimientos/<int:movimiento_id>/revertir", methods=["POST"])
@solo_dueno
def inventario_revertir(movimiento_id):
    motivo = request.form.get("motivo", "").strip()
    if len(motivo) < 5 or len(motivo) > 500:
        flash("Indica el motivo de reversión (entre 5 y 500 caracteres).", "error")
        return redirect(url_for("inventario"))
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    movimiento = db.execute(
        "SELECT * FROM stock_movimientos WHERE id = ?", (movimiento_id,)
    ).fetchone()
    if movimiento is None:
        db.rollback()
        abort(404)
    if movimiento["tipo"] == "reversion" or db.execute(
        "SELECT 1 FROM stock_movimientos WHERE revierte_movimiento_id = ?", (movimiento_id,)
    ).fetchone():
        db.rollback()
        flash("Ese movimiento ya fue revertido o no admite reversión.", "error")
        return redirect(url_for("inventario"))
    diferencia = -movimiento["cantidad_milesimas"]
    actual = stock_articulo(movimiento["articulo_id"], db)
    if actual + diferencia < 0:
        db.rollback()
        flash("No se puede revertir: el stock actual quedaría negativo.", "error")
        return redirect(url_for("inventario"))
    if movimiento["recepcion_item_id"]:
        compra_item = db.execute(
            """SELECT ci.id AS compra_item_id, ci.compra_id, ci.costo_unitario_centavos
               FROM compra_recepcion_items ri
               JOIN compra_items ci ON ci.id = ri.compra_item_id WHERE ri.id = ?""",
            (movimiento["recepcion_item_id"],),
        ).fetchone()
        finanzas = cuenta_compra(compra_item["compra_id"], db)
        cantidad_actual = cantidades_recibidas(compra_item["compra_item_id"], db)
        cantidad_posterior = cantidad_actual + diferencia
        costo_linea = compra_item["costo_unitario_centavos"] or 0
        total_linea_actual = importe_linea_compra_centavos(cantidad_actual, costo_linea)
        total_linea_posterior = importe_linea_compra_centavos(cantidad_posterior, costo_linea)
        total_compra_posterior = (
            finanzas["total"] - total_linea_actual + total_linea_posterior
        )
        if finanzas["pagado"] > max(total_compra_posterior, 0):
            db.rollback()
            flash("Reversa primero los pagos que superan el nuevo saldo de la compra.", "error")
            return redirect(url_for("inventario"))
    with db:
        insertar_movimiento_stock(
            db, movimiento["articulo_id"], "reversion", diferencia,
            movimiento["costo_unitario_centavos"], motivo, revierte_id=movimiento_id,
        )
        if movimiento["tipo"] == "consumo" and movimiento["orden_item_id"]:
            linea = db.execute(
                "SELECT cantidad, catalogo_id FROM orden_items WHERE id = ?",
                (movimiento["orden_item_id"],),
            ).fetchone()
            costo_repuesto = costo_promedio_articulo(movimiento["articulo_id"], db)
            db.execute(
                """UPDATE orden_items SET costo_unitario_centavos = ?, costo_total_centavos = ?
                   WHERE id = ?""",
                (
                    costo_repuesto,
                    a_centavos(Decimal(str(linea["cantidad"])) * Decimal(costo_repuesto) / 100),
                    movimiento["orden_item_id"],
                ),
            )
        db.execute(
            "UPDATE repuestos_servicios SET costo_centavos = ? WHERE id = ?",
            (costo_promedio_articulo(movimiento["articulo_id"], db), movimiento["articulo_id"]),
        )
        if movimiento["recepcion_item_id"]:
            compra_item_id = db.execute(
                "SELECT compra_item_id FROM compra_recepcion_items WHERE id = ?",
                (movimiento["recepcion_item_id"],),
            ).fetchone()["compra_item_id"]
            compra_id = db.execute(
                "SELECT compra_id FROM compra_items WHERE id = ?", (compra_item_id,)
            ).fetchone()["compra_id"]
            actualizar_estado_compra(db, compra_id)
    registrar("Revirtió movimiento de stock", f"Movimiento {movimiento_id} · {motivo}")
    flash("Reversión registrada; el movimiento original permanece en el historial.", "ok")
    return redirect(url_for("inventario"))


@app.route("/ordenes/<int:orden_id>/items/<int:item_id>/consumir", methods=["POST"])
@solo_dueno
def orden_item_consumir(orden_id, item_id):
    motivo = request.form.get("motivo", "").strip()
    if len(motivo) < 5 or len(motivo) > 300:
        flash("Indica dónde se instaló o cómo se utilizó el repuesto.", "error")
        return redirect(url_for("orden_detalle", orden_id=orden_id))
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    orden = db.execute(
        """SELECT o.id FROM ordenes o WHERE o.id = ? AND NOT EXISTS (
               SELECT 1 FROM orden_anulaciones a WHERE a.orden_id = o.id
           )""",
        (orden_id,),
    ).fetchone()
    if orden is None:
        db.rollback()
        abort(404)
    item = db.execute(
        """SELECT i.*, r.nombre AS articulo, r.unidad, r.controla_stock, r.costo_centavos
           FROM orden_items i JOIN repuestos_servicios r ON r.id = i.catalogo_id
           WHERE i.id = ? AND i.orden_id = ? AND i.vigente = 1""",
        (item_id, orden_id),
    ).fetchone()
    if item is None or item["tipo"] != "Repuesto" or not item["controla_stock"] or item["del_cliente"]:
        db.rollback()
        flash("Esta línea no está vinculada a un repuesto inventariable del taller.", "error")
        return redirect(url_for("orden_detalle", orden_id=orden_id))
    if item["falta_precio"]:
        db.rollback()
        flash("Primero completa el precio de esta línea.", "error")
        return redirect(url_for("orden_detalle", orden_id=orden_id))
    activo = db.execute(
        """SELECT 1 FROM stock_movimientos m WHERE m.orden_item_id = ? AND m.tipo = 'consumo'
           AND (SELECT SUM(act.cantidad_milesimas + COALESCE((
                    SELECT SUM(rev.cantidad_milesimas) FROM stock_movimientos rev
                    WHERE rev.revierte_movimiento_id = act.id
                ), 0)) FROM stock_movimientos act
                WHERE act.orden_item_id = m.orden_item_id) < 0 LIMIT 1""",
        (item_id,),
    ).fetchone()
    if activo:
        db.rollback()
        flash("El consumo de esta línea ya está registrado.", "ok")
        return redirect(url_for("orden_detalle", orden_id=orden_id))
    try:
        cantidad = a_milesimas(str(item["cantidad"]))
    except ValueError:
        db.rollback()
        flash("La cantidad supera la precisión del inventario (máximo tres decimales).", "error")
        return redirect(url_for("orden_detalle", orden_id=orden_id))
    actual = stock_articulo(item["catalogo_id"], db)
    if cantidad > actual:
        db.rollback()
        flash(
            f"No alcanza el stock de {item['articulo']}: disponibles {filtro_cantidad_stock(actual)} {item['unidad']}.",
            "error",
        )
        return redirect(url_for("orden_detalle", orden_id=orden_id))
    costo = costo_promedio_articulo(item["catalogo_id"], db)
    with db:
        movimiento_id = insertar_movimiento_stock(
            db, item["catalogo_id"], "consumo", -cantidad, costo, motivo, orden_item_id=item_id,
        )
        costo_total = int(
            (Decimal(cantidad) * Decimal(costo) / 1000)
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        db.execute(
            "UPDATE orden_items SET costo_unitario_centavos = ?, costo_total_centavos = ? WHERE id = ?",
            (costo, costo_total, item_id),
        )
    registrar(
        "Registró consumo de repuesto",
        f"{filtro_num_ot(orden_id)} · {item['articulo']} · {filtro_cantidad_stock(cantidad)} {item['unidad']}",
        orden_id,
    )
    flash(f"Consumo registrado (movimiento {movimiento_id}).", "ok")
    return redirect(url_for("orden_detalle", orden_id=orden_id))


# ---------------------------------------------------------------------------
# Catálogo de repuestos y servicios
# ---------------------------------------------------------------------------
@app.route("/catalogo", methods=["GET", "POST"])
@solo_dueno
def catalogo():
    db = get_db()
    errores = []
    editar_id = request.args.get("editar", type=int)
    busqueda = request.args.get("q", "").strip()[:120]
    estado = request.args.get("estado", "activos").strip()
    if estado not in ("activos", "archivados", "todos"):
        estado = "activos"

    if request.method == "POST":
        tipo = request.form.get("tipo", "")
        nombre = request.form.get("nombre", "").strip()
        precio_texto = request.form.get("precio", "").strip()
        costo_texto = request.form.get("costo", "").strip()
        sku = request.form.get("sku", "").strip().upper()
        unidad = request.form.get("unidad", "unidad").strip() or "unidad"
        controla_stock = request.form.get("controla_stock") == "1"
        stock_minimo_texto = request.form.get("stock_minimo", "0").strip() or "0"
        if tipo not in TIPOS:
            errores.append("Selecciona si es un repuesto o un servicio.")
        if not nombre:
            errores.append("El nombre es obligatorio.")
        if len(sku) > 40:
            errores.append("El SKU admite hasta 40 caracteres.")
        if not unidad or len(unidad) > 20:
            errores.append("La unidad admite entre 1 y 20 caracteres.")
        if controla_stock and tipo != "Repuesto":
            errores.append("Solo los repuestos pueden controlar existencias.")
        try:
            precio = a_centavos(a_decimal(precio_texto))
        except ValueError:
            errores.append("El precio no es válido.")
        costo = None
        if costo_texto:
            try:
                costo = a_centavos(a_decimal(costo_texto))
            except ValueError:
                errores.append("El costo no es válido.")
        try:
            stock_minimo = a_milesimas(stock_minimo_texto, permitir_cero=True)
        except ValueError:
            errores.append("La existencia mínima debe ser un número con hasta tres decimales.")
        if editar_id:
            previo = db.execute(
                "SELECT * FROM repuestos_servicios WHERE id = ?", (editar_id,)
            ).fetchone()
            if previo is None:
                abort(404)
            tiene_referencias = db.execute(
                """SELECT 1 WHERE
                     EXISTS (SELECT 1 FROM stock_movimientos WHERE articulo_id = ?)
                     OR EXISTS (SELECT 1 FROM compra_items WHERE articulo_id = ?)
                     OR EXISTS (SELECT 1 FROM orden_items WHERE catalogo_id = ?)""",
                (editar_id, editar_id, editar_id),
            ).fetchone()
            if tiene_referencias and (
                tipo != previo["tipo"]
                or nombre != previo["nombre"]
                or sku != previo["sku"]
                or unidad != previo["unidad"]
                or int(controla_stock) != previo["controla_stock"]
            ):
                errores.append(
                    "Este artículo tiene historial de stock o referencias en compras y órdenes. Conserva su nombre, SKU, tipo, unidad y control de existencias; archívalo y crea otro para cambiar esos datos."
                )
        if not errores:
            try:
                with db:
                    if editar_id:
                        db.execute(
                            """UPDATE repuestos_servicios
                               SET tipo = ?, nombre = ?, precio_centavos = ?, costo_centavos = ?,
                                   sku = ?, unidad = ?, controla_stock = ?, stock_minimo_milesimas = ?
                               WHERE id = ?""",
                            (tipo, nombre, precio, costo, sku, unidad, int(controla_stock), stock_minimo, editar_id),
                        )
                    else:
                        db.execute(
                            """INSERT INTO repuestos_servicios
                               (tipo, nombre, precio_centavos, costo_centavos, sku, unidad,
                                controla_stock, stock_minimo_milesimas)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (tipo, nombre, precio, costo, sku, unidad, int(controla_stock), stock_minimo),
                        )
            except sqlite3.IntegrityError:
                errores.append("Ya existe un nombre o SKU igual en el catálogo.")
            else:
                registrar("Actualizó el catálogo", f"{nombre} · SKU {sku or 'sin SKU'}")
                flash("Catálogo actualizado.", "ok")
                return redirect(url_for("catalogo", estado=estado if editar_id else "activos"))
        item = {
            "tipo": tipo, "nombre": nombre, "precio": precio_texto, "costo": costo_texto,
            "sku": sku, "unidad": unidad, "controla_stock": controla_stock,
            "stock_minimo": stock_minimo_texto,
        }
    elif editar_id:
        fila = obtener_o_404("repuestos_servicios", editar_id)
        item = {
            "tipo": fila["tipo"],
            "nombre": fila["nombre"],
            "precio": filtro_importe_input(fila["precio_centavos"]),
            "costo": importe_opcional(fila["costo_centavos"]),
            "sku": fila["sku"],
            "unidad": fila["unidad"],
            "controla_stock": bool(fila["controla_stock"]),
            "stock_minimo": f"{Decimal(fila['stock_minimo_milesimas']) / 1000:g}",
        }
    else:
        item = {
            "tipo": "Repuesto", "sku": "", "unidad": "unidad",
            "controla_stock": False, "stock_minimo": "0",
        }

    condicion = {
        "activos": " WHERE r.activo = 1",
        "archivados": " WHERE r.activo = 0",
        "todos": " WHERE 1 = 1",
    }[estado]
    parametros = []
    if busqueda:
        patron = f"%{busqueda}%"
        condicion += " AND (r.sku COLLATE NOCASE = ? OR r.nombre LIKE ? OR r.sku LIKE ? OR r.unidad LIKE ?)"
        parametros.extend((busqueda, patron, patron, patron))
    total = db.execute(
        "SELECT COUNT(*) AS total FROM repuestos_servicios r" + condicion, parametros
    ).fetchone()["total"]
    paginacion = paginar(total)
    orden = (
        " ORDER BY CASE WHEN r.sku COLLATE NOCASE = ? THEN 0 ELSE 1 END, r.tipo, r.nombre COLLATE NOCASE"
        if busqueda else " ORDER BY r.tipo, r.nombre COLLATE NOCASE"
    )
    parametros_filas = [*parametros]
    if busqueda:
        parametros_filas.append(busqueda)
    filas = db.execute(
        """SELECT r.*,
                  COALESCE((SELECT SUM(m.cantidad_milesimas) FROM stock_movimientos m
                            WHERE m.articulo_id = r.id), 0) AS stock_milesimas
           FROM repuestos_servicios r""" + condicion + orden + " LIMIT ? OFFSET ?",
        (*parametros_filas, POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    return render_template(
        "catalogo.html",
        catalogo=filas,
        item=item,
        editar_id=editar_id,
        errores=errores,
        paginacion=paginacion,
        busqueda=busqueda,
        estado=estado,
        cantidad_archivados=db.execute(
            "SELECT COUNT(*) AS total FROM repuestos_servicios WHERE activo = 0"
        ).fetchone()["total"],
    )


@app.route("/catalogo/<int:item_id>/eliminar", methods=["POST"])
@solo_dueno
def catalogo_eliminar(item_id):
    item = obtener_o_404("repuestos_servicios", item_id)
    db = get_db()
    with db:
        db.execute("UPDATE repuestos_servicios SET activo = 0 WHERE id = ?", (item_id,))
    registrar("Archivó un artículo del catálogo", item["nombre"])
    flash("Elemento archivado; el historial de compras y stock se conserva.", "ok")
    return redirect(url_for("catalogo", estado=request.form.get("estado", "activos")))


@app.route("/catalogo/<int:item_id>/reactivar", methods=["POST"])
@solo_dueno
def catalogo_reactivar(item_id):
    item = obtener_o_404("repuestos_servicios", item_id)
    with get_db():
        get_db().execute("UPDATE repuestos_servicios SET activo = 1 WHERE id = ?", (item_id,))
    registrar("Reactivó un artículo del catálogo", item["nombre"])
    flash("Elemento reactivado; conserva su historial de compras y stock.", "ok")
    return redirect(url_for("catalogo"))


init_db()

if __name__ == "__main__":
    # Al abrir, se actualiza la copia de hoy: incluye lo último de la sesión anterior.
    respaldar_sin_fallar(actualizar=True)
    # Solo accesible desde esta computadora, nunca desde la red.
    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
