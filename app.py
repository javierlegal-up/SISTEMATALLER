"""Sistema web para taller mecánico: clientes, vehículos, órdenes de trabajo y catálogo.

Ejecutar con:  python app.py   y abrir http://127.0.0.1:5000
"""
import os
import re
import secrets
import shutil
import sqlite3
import string
import threading
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
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

ESTADOS = ("Pendiente", "En Proceso", "Completado")
TIPOS = ("Repuesto", "Servicio")  # "Servicio" es lo que se suma como mano de obra.
MAXIMO_IMPORTE = Decimal("999999999")
MAXIMA_CANTIDAD = Decimal("100000")
DIAS_ATRASO = 7  # Una orden abierta con más días que estos se marca como atrasada.
ATRASADAS_EN_PANEL = 4  # Cuántas órdenes atrasadas se muestran en el panel.
POR_PAGINA = 15  # Filas por página en las listas, para evitar el scroll largo.

# Usuarios. El dueño ve todo; el empleado registra autos y trabajos, pero no ve
# precios, costos ni el panel, no borra nada y no puede completar órdenes.
NOMBRES_ROL = {"dueno": "Dueño", "empleado": "Empleado"}
LARGO_MINIMO_CLAVE = 4
# En el registro de actividad, lo hecho antes de esta hora es del turno mañana.
HORA_CAMBIO_TURNO = 14
NOMBRES_MES = ("Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")

# Los importes se guardan como enteros en centavos para evitar errores de redondeo.
# orden_items copia descripción y precio: si luego cambia el catálogo, las órdenes
# ya emitidas no se alteran.
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
        "EXISTS (SELECT 1 FROM orden_items i WHERE i.orden_id = o.id AND i.falta_precio = 1)",
        "o.fecha_ingreso, o.id",
    ),
}

SELECT_ORDEN = """
    SELECT o.*,
           v.patente, v.marca, v.modelo, v.anio, v.color, v.cliente_id,
           c.nombre AS cliente_nombre, c.documento AS cliente_documento,
           c.telefono AS cliente_telefono, c.email AS cliente_email,
           c.direccion AS cliente_direccion,
           COALESCE((SELECT SUM(i.subtotal_centavos) FROM orden_items i
                     WHERE i.orden_id = o.id AND i.tipo = 'Repuesto'), 0) AS total_repuestos,
           COALESCE((SELECT SUM(i.subtotal_centavos) FROM orden_items i
                     WHERE i.orden_id = o.id AND i.tipo = 'Servicio'), 0) AS total_mano_obra,
           (SELECT COUNT(*) FROM orden_items i
            WHERE i.orden_id = o.id AND i.falta_precio = 1) AS lineas_sin_precio
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
    RESPALDOS_EXTRA=list(CARPETAS_EXTRA),
    BUSCAR_PENDRIVE=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


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


def init_db():
    conexion = conectar()
    try:
        conexion.executescript(ESQUEMA)
        for tabla, columna, definicion in COLUMNAS_AGREGADAS:
            existentes = {fila["name"] for fila in conexion.execute(f"PRAGMA table_info({tabla})")}
            if columna not in existentes:
                conexion.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}")
                if columna == "del_cliente":
                    # Antes, un repuesto con precio 0 era uno que trajo el cliente.
                    conexion.execute(
                        "UPDATE orden_items SET del_cliente = 1"
                        " WHERE tipo = 'Repuesto' AND subtotal_centavos = 0"
                    )
        conexion.commit()
    finally:
        conexion.close()


# ---------------------------------------------------------------------------
# Usuarios, permisos y registro de actividad
# ---------------------------------------------------------------------------
def crear_usuario(conexion, usuario, nombre, clave, rol):
    """Guarda un usuario con la contraseña cifrada (nunca se guarda tal cual)."""
    return conexion.execute(
        "INSERT INTO usuarios (usuario, nombre, clave_hash, rol) VALUES (?, ?, ?, ?)",
        (usuario, nombre, generate_password_hash(clave), rol),
    ).lastrowid


def es_dueno():
    usuario = g.get("usuario")
    return usuario is not None and usuario["rol"] == "dueno"


def solo_dueno(vista):
    """Restringe una pantalla o acción al dueño."""

    @wraps(vista)
    def envoltura(*args, **kwargs):
        if not es_dueno():
            abort(403)
        return vista(*args, **kwargs)

    return envoltura


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
def crear_respaldo(actualizar=False):
    """Crea la copia del día. Devuelve su ruta, o None si ya existía y no se pidió actualizarla.

    Hay una copia por día (se conservan las de días anteriores). Con actualizar=True
    se rehace la de hoy para incluir lo último que se cargó. Una base dañada nunca
    reemplaza una copia buena: la copia se revisa antes de guardarse.
    """
    carpeta = Path(app.config["RESPALDOS"])
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / f"taller_{date.today().isoformat()}.db"
    if destino.exists() and not actualizar:
        return None

    # Se escribe con otro nombre para que una copia interrumpida nunca pase por válida.
    temporal = destino.with_suffix(".tmp")
    origen = sqlite3.connect(app.config["DATABASE"])
    copia = sqlite3.connect(temporal)
    try:
        origen.backup(copia)
        integridad = copia.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        copia.close()
        origen.close()
    if integridad != "ok":
        temporal.unlink()
        raise sqlite3.DatabaseError(f"la base de datos parece dañada: {integridad}")
    os.replace(temporal, destino)

    for antiguo in sorted(carpeta.glob(PATRON_RESPALDO))[:-MAXIMO_RESPALDOS]:
        antiguo.unlink()
    return destino


def carpetas_en_pendrives():
    """Carpetas CARPETA_PENDRIVE en la raíz de cualquier unidad conectada (D: a Z:)."""
    if os.name != "nt" or not app.config["BUSCAR_PENDRIVE"]:
        return []
    import ctypes

    # Evita el cartel "No hay disco en la unidad" al revisar lectores de tarjetas vacíos.
    ctypes.windll.kernel32.SetErrorMode(0x0001)
    encontradas = []
    for letra in string.ascii_uppercase[3:]:
        carpeta = Path(f"{letra}:\\") / CARPETA_PENDRIVE
        try:
            if carpeta.is_dir():
                encontradas.append(carpeta)
        except OSError:
            continue
    return encontradas


def archivo_estado_externo():
    return Path(app.config["RESPALDOS"]) / ".ultima_copia_externa"


def copiar_a_destinos_externos(archivo):
    """Copia un respaldo al pendrive y a las carpetas extra. Devuelve (copiados, errores)."""
    copiados, errores = [], []
    destinos = [Path(carpeta) for carpeta in app.config["RESPALDOS_EXTRA"]] + carpetas_en_pendrives()
    for carpeta in destinos:
        try:
            carpeta.mkdir(parents=True, exist_ok=True)
            temporal = carpeta / (archivo.name + ".tmp")
            shutil.copy2(archivo, temporal)
            os.replace(temporal, carpeta / archivo.name)
            for antiguo in sorted(carpeta.glob(PATRON_RESPALDO))[:-MAXIMO_RESPALDOS]:
                antiguo.unlink()
            copiados.append(carpeta)
        except OSError as error:
            errores.append(f"{carpeta}: {error}")
    if copiados:
        archivo_estado_externo().write_text(datetime.now().isoformat(timespec="minutes"), encoding="utf-8")
    return copiados, errores


def ultima_copia_externa():
    """Fecha y hora de la última copia fuera de la compu, o None si nunca hubo."""
    try:
        return datetime.fromisoformat(archivo_estado_externo().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


# Evita que dos pedidos simultáneos escriban la misma copia a la vez.
candado_respaldo = threading.Lock()
ultimo_respaldo_automatico = None


def respaldar(actualizar=False):
    """Copia local y, si hay dónde, copia externa. Devuelve (copiados, errores_externos)."""
    with candado_respaldo:
        destino = crear_respaldo(actualizar)
        if destino is None:
            return [], []
        return copiar_a_destinos_externos(destino)


def respaldar_sin_fallar(actualizar=False):
    """Un respaldo fallido no debe impedir trabajar, pero sí avisar. Devuelve si salió bien."""
    try:
        _copiados, errores = respaldar(actualizar)
    except (OSError, sqlite3.Error) as error:
        print(f"ATENCIÓN: no se pudo crear la copia de seguridad: {error}")
        return False
    for error in errores:
        print(f"ATENCIÓN: no se pudo copiar al pendrive o a la nube: {error}")
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
    try:
        copiados, errores = respaldar(actualizar=True)
    except (OSError, sqlite3.Error) as error:
        flash(f"No se pudo crear la copia de seguridad: {error}", "error")
        return redirect(url_for("inicio"))
    for error in errores:
        flash(f"No se pudo copiar a {error}", "error")
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
    return redirect(url_for("inicio"))


def ultimo_respaldo():
    """Fecha ISO de la copia más reciente, o None si no hay ninguna."""
    archivos = sorted(Path(app.config["RESPALDOS"]).glob(PATRON_RESPALDO))
    return archivos[-1].stem.removeprefix("taller_") if archivos else None


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
        "TIPOS": TIPOS,
        "TALLER": TALLER,
        "usuario": g.get("usuario"),
        "es_dueno": es_dueno(),
        "NOMBRES_ROL": NOMBRES_ROL,
        "nav_conteos": conteos_por_estado(),
        # Solo tienen contenido en el modo simulación (ver simulacion.py).
        "SIMULACION": app.config.get("SIMULACION", False),
        "guia": app.config.get("GUIA", {}).get(request.endpoint),
    }


# Pantallas que se pueden ver sin haber iniciado sesión.
RUTAS_PUBLICAS = {"login", "configuracion_inicial", "static"}


def pagina_de_inicio():
    """El dueño empieza en el panel; el empleado, en las órdenes abiertas."""
    return url_for("inicio") if es_dueno() else url_for("ordenes", estado="abiertas")


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
        if g.usuario is None:
            session.pop("usuario_id", None)  # Usuario desactivado: se cierra su sesión.

    if g.usuario is None and request.endpoint not in RUTAS_PUBLICAS:
        if request.path.startswith("/api/"):
            return jsonify(error="La sesión se cerró. Vuelve a iniciar sesión."), 401
        return redirect(url_for("login"))
    return None


@app.errorhandler(403)
def sin_permiso(_error):
    return render_template("sin_permiso.html"), 403


def leer_datos_usuario(form, pedir_usuario=True):
    """Valida nombre, usuario y contraseña de un formulario de usuario."""
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
            session.clear()
            session["usuario_id"] = usuario_id
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
        fila = get_db().execute(
            "SELECT * FROM usuarios WHERE usuario = ? AND activo = 1", (usuario_escrito,)
        ).fetchone()
        if fila is not None and check_password_hash(fila["clave_hash"], request.form.get("clave", "")):
            session.clear()
            session["usuario_id"] = fila["id"]
            g.usuario = fila
            registrar("Inició sesión")
            return redirect(pagina_de_inicio())
        error = "Usuario o contraseña incorrectos."
    return render_template("login.html", error=error, usuario_escrito=usuario_escrito)


@app.route("/logout", methods=["POST"])
def logout():
    registrar("Cerró sesión")
    session.clear()
    return redirect(url_for("login"))


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
                "UPDATE usuarios SET clave_hash = ? WHERE id = ?",
                (generate_password_hash(clave), usuario_id),
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
        db.execute("UPDATE usuarios SET activo = ? WHERE id = ?", (0 if usuario["activo"] else 1, usuario_id))
    accion = "Desactivó un usuario" if usuario["activo"] else "Reactivó un usuario"
    registrar(accion, usuario["nombre"])
    flash(f"{accion}: {usuario['nombre']}.", "ok")
    return redirect(url_for("usuarios"))


@app.route("/actividad")
@solo_dueno
def actividad():
    """Registro de quién hizo qué, filtrable por persona y por día."""
    usuario_id = request.args.get("usuario", type=int)
    dia = request.args.get("fecha", "").strip()
    condiciones, parametros = [], []
    if usuario_id:
        condiciones.append("a.usuario_id = ?")
        parametros.append(usuario_id)
    try:
        date.fromisoformat(dia)
    except ValueError:
        dia = ""
    if dia:
        condiciones.append("substr(a.momento, 1, 10) = ?")
        parametros.append(dia)
    filtro = " WHERE " + " AND ".join(condiciones) if condiciones else ""

    db = get_db()
    total = db.execute("SELECT COUNT(*) AS total FROM actividad a" + filtro, parametros).fetchone()["total"]
    paginacion = paginar(total)
    filas = db.execute(
        """SELECT a.*, u.nombre AS usuario_nombre
           FROM actividad a LEFT JOIN usuarios u ON u.id = a.usuario_id"""
        + filtro
        + " ORDER BY a.momento DESC, a.id DESC LIMIT ? OFFSET ?",
        (*parametros, POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    personas = db.execute("SELECT id, nombre FROM usuarios ORDER BY nombre COLLATE NOCASE").fetchall()
    return render_template(
        "actividad.html",
        registros=filas,
        personas=personas,
        usuario_id=usuario_id,
        dia=dia,
        paginacion=paginacion,
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


def precio_para_empleado(guardada, del_cliente):
    """Precio, costo y "falta precio" de una línea que guardó un empleado.

    El empleado no ve ni envía precios. Una línea que ya existía conserva lo que puso
    el dueño; una nueva queda "sin precio" para que el dueño la complete. Si el
    repuesto lo trae el cliente, no lleva precio.
    """
    if del_cliente:
        return 0, None, False
    if guardada is None or guardada["del_cliente"]:
        return 0, None, True
    return (
        guardada["precio_unitario_centavos"],
        guardada["costo_unitario_centavos"],
        bool(guardada["falta_precio"]),
    )


def leer_orden(form, lineas_guardadas=None):
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
    }
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
    try:
        date.fromisoformat(orden["fecha_ingreso"])
    except ValueError:
        errores.append("La fecha de ingreso no es válida.")

    lineas, items = [], []
    # Campos opcionales: se leen aparte para que un formulario que no los tenga siga funcionando.
    costos = form.getlist("item_costo")
    precios = form.getlist("item_precio")
    ids = form.getlist("item_id")
    marcas_cliente = form.getlist("item_cliente")
    filas = zip(form.getlist("item_tipo"), form.getlist("item_descripcion"), form.getlist("item_cantidad"))
    for indice, (tipo, descripcion, cantidad) in enumerate(filas):
        numero = indice + 1
        precio = precios[indice].strip() if indice < len(precios) else ""
        costo = costos[indice].strip() if indice < len(costos) else ""
        item_id = ids[indice] if indice < len(ids) else ""
        del_cliente = (marcas_cliente[indice] if indice < len(marcas_cliente) else "0") == "1"
        descripcion = descripcion.strip()
        if not descripcion and not precio:
            continue  # Fila que quedó en blanco.
        linea = {
            "item_id": item_id,
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
        linea["falta_precio"] = falta
        items.append(
            {
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
    for fila in db.execute("SELECT estado, COUNT(*) AS total FROM ordenes GROUP BY estado"):
        conteos[fila["estado"]] = fila["total"]
    conteos["sin_precio"] = db.execute(
        "SELECT COUNT(DISTINCT orden_id) AS total FROM orden_items WHERE falta_precio = 1"
    ).fetchone()["total"]
    return conteos


def paginar(total):
    """Datos de paginación de una lista, conservando los filtros de la dirección actual."""
    paginas = max(-(-total // POR_PAGINA), 1)
    pagina = min(max(request.args.get("pagina", 1, type=int), 1), paginas)

    def enlace(numero):
        argumentos = {**request.args.to_dict(), "pagina": numero}
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
           WHERE o.estado != 'Completado'"""
    ).fetchone()["total"]

    meses = ultimos_meses()
    filas = {
        fila["mes"]: fila
        for fila in db.execute(
            """SELECT substr(o.fecha_completado, 1, 7) AS mes,
                      COUNT(DISTINCT o.id) AS ordenes,
                      COALESCE(SUM(CASE WHEN i.tipo = 'Repuesto' THEN i.subtotal_centavos END), 0) AS repuestos,
                      COALESCE(SUM(CASE WHEN i.tipo = 'Servicio' THEN i.subtotal_centavos END), 0) AS mano_obra
               FROM ordenes o LEFT JOIN orden_items i ON i.orden_id = o.id
               WHERE o.estado = 'Completado' AND o.fecha_completado >= ?
               GROUP BY mes""",
            (f"{meses[0][0]}-01",),
        )
    }
    return {
        "conteos": conteos,
        "abiertas": conteos["Pendiente"] + conteos["En Proceso"],
        "atrasadas": atrasadas,
        "valor_abierto": valor_abierto,
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


def preparar_fila(fila):
    """Agrega a una orden lo que necesita para mostrarse en las listas."""
    orden = dict(fila)
    posicion = ESTADOS.index(orden["estado"])
    orden["total"] = orden["total_repuestos"] + orden["total_mano_obra"]
    orden["dias"] = max((date.today() - date.fromisoformat(orden["fecha_ingreso"])).days, 0)
    orden["atrasada"] = (
        orden["estado"] != "Completado" and orden["fecha_ingreso"] < fecha_limite_atraso()
    )
    orden["anterior"] = ESTADOS[posicion - 1] if posicion > 0 else None
    orden["siguiente"] = ESTADOS[posicion + 1] if posicion < len(ESTADOS) - 1 else None
    return orden


@app.route("/")
def inicio():
    if not es_dueno():
        return redirect(pagina_de_inicio())  # El panel con los números es solo del dueño.
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
        dias_atraso=DIAS_ATRASO,
        respaldo_ok=respaldo_ok,
        ultimo_respaldo=ultimo_respaldo(),
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
    ordenes = db.execute(
        SELECT_ORDEN + " WHERE v.cliente_id = ? ORDER BY o.id DESC", (cliente_id,)
    ).fetchall()
    return render_template(
        "cliente_detalle.html", cliente=cliente, vehiculos=vehiculos, ordenes=ordenes
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
                  (SELECT COUNT(*) FROM ordenes o WHERE o.vehiculo_id = v.id) AS total_ordenes,
                  (SELECT MAX(o.fecha_ingreso) FROM ordenes o WHERE o.vehiculo_id = v.id) AS ultima_visita,
                  COALESCE((SELECT SUM(i.subtotal_centavos)
                            FROM orden_items i JOIN ordenes o ON o.id = i.orden_id
                            WHERE o.vehiculo_id = v.id AND o.estado = 'Completado'), 0) AS total_completado
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
           FROM ordenes o LEFT JOIN orden_items i ON i.orden_id = o.id
           WHERE o.vehiculo_id = ?""",
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
    orden = get_db().execute(SELECT_ORDEN + " WHERE o.id = ?", (orden_id,)).fetchone()
    if orden is None:
        abort(404)
    return orden


def obtener_items(orden_id):
    return get_db().execute(
        "SELECT * FROM orden_items WHERE orden_id = ? ORDER BY id", (orden_id,)
    ).fetchall()


def describir_orden(orden_id):
    """Texto corto para el registro de actividad, por ejemplo "OT-00012 · AB123CD · Toyota Corolla"."""
    orden = obtener_orden_o_404(orden_id)
    return f"{filtro_num_ot(orden_id)} · {orden['patente']} · {orden['marca']} {orden['modelo']}"


def fecha_completado_para(estado, anterior=None):
    if estado != "Completado":
        return None
    return anterior or date.today().isoformat()


def guardar_items(db, orden_id, items):
    db.execute("DELETE FROM orden_items WHERE orden_id = ?", (orden_id,))
    for item in items:
        db.execute(
            """INSERT INTO orden_items
               (orden_id, tipo, descripcion, cantidad, precio_unitario_centavos, subtotal_centavos,
                costo_unitario_centavos, costo_total_centavos, del_cliente, falta_precio)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                orden_id,
                item["tipo"],
                item["descripcion"],
                item["cantidad"],
                item["precio"],
                item["subtotal"],
                item["costo"],
                item["costo_total"],
                int(item["del_cliente"]),
                int(item["falta_precio"]),
            ),
        )


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
            "nombre": fila["nombre"],
            "tipo": fila["tipo"],
            "precio": filtro_importe_input(fila["precio_centavos"]) if dueno else "",
            "costo": importe_opcional(fila["costo_centavos"]) if dueno else "",
        }
        for fila in db.execute("SELECT * FROM repuestos_servicios ORDER BY nombre COLLATE NOCASE")
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
    busqueda = request.args.get("q", "").strip()

    condiciones, parametros = [], []
    if condicion_vista:
        condiciones.append(condicion_vista)
    if estado == "atrasadas":
        parametros.append(fecha_limite_atraso())
    if busqueda:
        patron = f"%{busqueda}%"
        condiciones.append("(c.nombre LIKE ? OR v.patente LIKE ? OR v.marca LIKE ? OR v.modelo LIKE ?)")
        parametros += [patron, f"%{normalizar_patente(busqueda)}%", patron, patron]
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
        paginacion=paginacion,
    )


@app.route("/ordenes/nueva", methods=["GET", "POST"])
def orden_nueva():
    if request.method == "GET":
        orden = {
            "vehiculo_id": request.args.get("vehiculo_id", ""),
            "estado": "Pendiente",
            "fecha_ingreso": date.today().isoformat(),
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
                                    kilometraje, fecha_ingreso, fecha_completado)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(orden["vehiculo_id"]),
                orden["fallas"],
                orden["diagnostico"],
                orden["notas"],
                orden["estado"],
                int(orden["kilometraje"]) if orden["kilometraje"] else None,
                orden["fecha_ingreso"],
                fecha_completado_para(orden["estado"]),
            ),
        )
        orden_id = cursor.lastrowid
        guardar_items(db, orden_id, items)
    registrar("Creó la orden", describir_orden(orden_id), orden_id)
    flash(f"Orden {filtro_num_ot(orden_id)} creada.", "ok")
    return redirect(url_for("orden_detalle", orden_id=orden_id))


def resumir_costos(items):
    """Costo interno de una orden y cuántos repuestos cobrados no tienen el costo cargado.

    Un costo vacío cuenta como 0. Solo se avisa si el repuesto se cobró: uno que trajo
    el cliente (precio 0) no tiene costo para el taller.
    """
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


@app.route("/ordenes/<int:orden_id>")
def orden_detalle(orden_id):
    orden = obtener_orden_o_404(orden_id)
    items = obtener_items(orden_id)
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
        costos=resumir_costos(items),
        historial=historial,
    )


@app.route("/ordenes/<int:orden_id>/editar", methods=["GET", "POST"])
def orden_editar(orden_id):
    existente = obtener_orden_o_404(orden_id)
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
            }
            for item in obtener_items(orden_id)
        ]
        orden = dict(existente)
        if orden["kilometraje"] is None:
            orden["kilometraje"] = ""
        return render_orden_form(orden, lineas, [], orden_id)

    # Si guarda un empleado, las líneas que ya existían conservan los precios del dueño.
    guardadas = None if es_dueno() else {str(item["id"]): item for item in obtener_items(orden_id)}
    orden, lineas, items, errores = leer_orden(request.form, guardadas)
    if errores:
        return render_orden_form(orden, lineas, errores, orden_id)

    db = get_db()
    with db:
        db.execute(
            """UPDATE ordenes
               SET vehiculo_id = ?, fallas = ?, diagnostico = ?, notas = ?, estado = ?,
                   kilometraje = ?, fecha_ingreso = ?, fecha_completado = ?
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
    if estado not in ESTADOS:
        return "Estado no válido."
    if not es_dueno() and "Completado" in (estado, orden["estado"]):
        return "Solo el dueño puede completar una orden o reabrir una completada."
    if estado == "Completado" and orden["lineas_sin_precio"]:
        return f"Antes de completar {filtro_num_ot(orden['id'])} falta poner precios: edita la orden."
    return None


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


@app.route("/ordenes/<int:orden_id>/eliminar", methods=["POST"])
@solo_dueno
def orden_eliminar(orden_id):
    detalle = describir_orden(orden_id)
    db = get_db()
    with db:
        db.execute("DELETE FROM ordenes WHERE id = ?", (orden_id,))
    registrar("Eliminó la orden", detalle)
    flash(f"Orden {filtro_num_ot(orden_id)} eliminada.", "ok")
    return redirect(url_for("ordenes"))


@app.route("/ordenes/<int:orden_id>/imprimir")
@solo_dueno
def orden_imprimir(orden_id):
    return render_template(
        "orden_imprimir.html", orden=obtener_orden_o_404(orden_id), items=obtener_items(orden_id)
    )


# ---------------------------------------------------------------------------
# Catálogo de repuestos y servicios
# ---------------------------------------------------------------------------
@app.route("/catalogo", methods=["GET", "POST"])
@solo_dueno
def catalogo():
    db = get_db()
    errores = []
    editar_id = request.args.get("editar", type=int)

    if request.method == "POST":
        tipo = request.form.get("tipo", "")
        nombre = request.form.get("nombre", "").strip()
        precio_texto = request.form.get("precio", "").strip()
        costo_texto = request.form.get("costo", "").strip()
        if tipo not in TIPOS:
            errores.append("Selecciona si es un repuesto o un servicio.")
        if not nombre:
            errores.append("El nombre es obligatorio.")
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
        if not errores:
            try:
                with db:
                    if editar_id:
                        db.execute(
                            """UPDATE repuestos_servicios
                               SET tipo = ?, nombre = ?, precio_centavos = ?, costo_centavos = ?
                               WHERE id = ?""",
                            (tipo, nombre, precio, costo, editar_id),
                        )
                    else:
                        db.execute(
                            """INSERT INTO repuestos_servicios
                               (tipo, nombre, precio_centavos, costo_centavos)
                               VALUES (?, ?, ?, ?)""",
                            (tipo, nombre, precio, costo),
                        )
            except sqlite3.IntegrityError:
                errores.append(f"Ya existe «{nombre}» en el catálogo.")
            else:
                registrar("Actualizó la lista de precios", nombre)
                flash("Catálogo actualizado.", "ok")
                return redirect(url_for("catalogo"))
        item = {"tipo": tipo, "nombre": nombre, "precio": precio_texto, "costo": costo_texto}
    elif editar_id:
        fila = obtener_o_404("repuestos_servicios", editar_id)
        item = {
            "tipo": fila["tipo"],
            "nombre": fila["nombre"],
            "precio": filtro_importe_input(fila["precio_centavos"]),
            "costo": importe_opcional(fila["costo_centavos"]),
        }
    else:
        item = {"tipo": "Repuesto"}

    total = db.execute("SELECT COUNT(*) AS total FROM repuestos_servicios").fetchone()["total"]
    paginacion = paginar(total)
    filas = db.execute(
        "SELECT * FROM repuestos_servicios ORDER BY tipo, nombre COLLATE NOCASE LIMIT ? OFFSET ?",
        (POR_PAGINA, paginacion["offset"]),
    ).fetchall()
    return render_template(
        "catalogo.html",
        catalogo=filas,
        item=item,
        editar_id=editar_id,
        errores=errores,
        paginacion=paginacion,
    )


@app.route("/catalogo/<int:item_id>/eliminar", methods=["POST"])
@solo_dueno
def catalogo_eliminar(item_id):
    item = obtener_o_404("repuestos_servicios", item_id)
    db = get_db()
    with db:
        db.execute("DELETE FROM repuestos_servicios WHERE id = ?", (item_id,))
    registrar("Eliminó de la lista de precios", item["nombre"])
    flash("Elemento eliminado del catálogo.", "ok")
    return redirect(url_for("catalogo"))


init_db()

if __name__ == "__main__":
    # Al abrir, se actualiza la copia de hoy: incluye lo último de la sesión anterior.
    respaldar_sin_fallar(actualizar=True)
    # Solo accesible desde esta computadora, nunca desde la red.
    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
