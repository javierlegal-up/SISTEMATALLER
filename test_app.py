"""Prueba del recorrido completo. Ejecutar con:  python -m unittest -v

Usa una base de datos temporal: no toca taller.db.
"""
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from werkzeug.security import generate_password_hash

CARPETA_TEMPORAL = tempfile.mkdtemp()
os.environ["TALLER_DB"] = os.path.join(CARPETA_TEMPORAL, "prueba.db")
os.environ["TALLER_RESPALDOS"] = os.path.join(CARPETA_TEMPORAL, "respaldos")
os.environ["TALLER_LOGS"] = os.path.join(CARPETA_TEMPORAL, "logs")

from app import (  # noqa: E402
    ESQUEMA,
    LARGO_MINIMO_CLAVE,
    MAXIMO_RESPALDOS,
    POR_PAGINA,
    app,
    caja_fecha_cerrada,
    conectar,
    cobros_por_medio,
    cuenta_compra,
    crear_respaldo,
    crear_usuario,
    init_db,
    meses_del_trimestre,
    resumen_panel,
    ganancias_panel,
    ultimo_respaldo,
)

# Las pruebas nunca deben copiar al pendrive real de la compu donde se corren.
app.config.update(BUSCAR_PENDRIVE=False, RESPALDOS_EXTRA=[])

# Un dueño y un empleado para las pruebas.
_conexion = conectar()
with _conexion:
    DUENO_ID = crear_usuario(_conexion, "dueno", "Dueño de Prueba", "clave-dueno-2026", "dueno")
    EMPLEADO_ID = crear_usuario(_conexion, "juan", "Juan Empleado", "clave-juan-2026", "empleado")
_conexion.close()


def tearDownModule():
    for handler in list(app.logger.handlers):
        if getattr(handler, "_taller_log_path", None):
            handler.flush()
            handler.close()
            app.logger.removeHandler(handler)
    shutil.rmtree(CARPETA_TEMPORAL, ignore_errors=True)


def datos_cliente(nombre, *patentes):
    return {
        "nombre": nombre,
        "patente": list(patentes),
        "marca": ["Toyota"] * len(patentes),
        "modelo": ["Corolla"] * len(patentes),
        "anio": ["2018"] * len(patentes),
        "color": ["Gris"] * len(patentes),
    }


class RecorridoCompleto(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        conexion = conectar()
        with conexion:
            # Algunos recorridos restauran una base legada sobre la base temporal común.
            # Reponer estas dos cuentas aquí mantiene cada prueba independiente.
            cuentas_base = (
                (DUENO_ID, "dueno", "Dueño de Prueba", "clave-dueno-2026", "dueno"),
                (EMPLEADO_ID, "juan", "Juan Empleado", "clave-juan-2026", "empleado"),
            )
            for id_usuario, nombre_usuario, nombre, clave, rol in cuentas_base:
                conexion.execute(
                    """INSERT INTO usuarios (id, usuario, nombre, clave_hash, rol)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET usuario = excluded.usuario,
                         nombre = excluded.nombre, clave_hash = excluded.clave_hash,
                         rol = excluded.rol, activo = 1, debe_cambiar_clave = 0""",
                    (id_usuario, nombre_usuario, nombre, generate_password_hash(clave), rol),
                )
            conexion.execute("DELETE FROM intentos_login")
            conexion.execute(
                "UPDATE usuarios SET debe_cambiar_clave = 0 WHERE id IN (?, ?)",
                (DUENO_ID, EMPLEADO_ID),
            )
        conexion.close()
        self.cliente = app.test_client()
        self.entrar_como(DUENO_ID)  # Salvo que la prueba diga otra cosa, entra el dueño.

    def entrar_como(self, usuario_id):
        conexion = conectar()
        try:
            version = conexion.execute(
                "SELECT version_sesion FROM usuarios WHERE id = ?", (usuario_id,)
            ).fetchone()["version_sesion"]
        finally:
            conexion.close()
        with self.cliente.session_transaction() as sesion:
            sesion["usuario_id"] = usuario_id
            sesion["version_sesion"] = version
            sesion["actividad_sesion"] = datetime.now().isoformat(timespec="seconds")

    def crear_orden(self, vehiculo_id, **extra):
        datos = {
            "vehiculo_id": vehiculo_id,
            "fallas": "Ruido al frenar",
            "estado": "Pendiente",
            "fecha_ingreso": "2026-11-02",
            "item_tipo": ["Repuesto", "Servicio"],
            "item_descripcion": ["Pastillas de freno", "Cambio de pastillas"],
            "item_cantidad": ["2", "1"],
            "item_precio": ["1500.50", "3000"],
        }
        datos.update(extra)
        return self.cliente.post("/ordenes/nueva", data=datos, follow_redirects=True)

    def test_01_paginas_vacias_cargan(self):
        for ruta in (
            "/", "/analitica", "/clientes", "/clientes/nuevo", "/ordenes", "/ordenes/nueva",
            "/catalogo", "/caja", "/cuentas-por-cobrar", "/cuentas-por-pagar",
            "/gastos", "/restaurar", "/healthz", "/static/tailwind.css",
        ):
            with self.subTest(ruta=ruta):
                respuesta = self.cliente.get(ruta)
                self.assertEqual(respuesta.status_code, 200)
                respuesta.close()

    def test_02_cliente_con_varios_vehiculos(self):
        respuesta = self.cliente.post(
            "/clientes/nuevo", data=datos_cliente("Ana Díaz", "ab-12 cd", "XYZ789"), follow_redirects=True
        )
        texto = respuesta.get_data(as_text=True)
        self.assertIn("Ana Díaz", texto)
        self.assertIn("AB12CD", texto)  # La patente se normaliza.
        self.assertIn("XYZ789", texto)

    def test_03_patente_duplicada_no_crea_cliente(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Luis Paz", "DUP123"))
        respuesta = self.cliente.post("/clientes/nuevo", data=datos_cliente("Otro Cliente", "DUP123"))
        texto = respuesta.get_data(as_text=True)
        self.assertIn("ya está registrada", texto)
        self.assertNotIn("Otro Cliente", self.cliente.get("/clientes").get_data(as_text=True))

    def test_04_orden_calcula_totales_e_imprime(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Eva Soto", "ORD111"))
        respuesta = self.crear_orden(self.vehiculo_id("ORD111"))
        texto = respuesta.get_data(as_text=True)
        self.assertIn("$3.001,00", texto)  # Repuestos: 2 x 1500,50
        self.assertIn("$3.000,00", texto)  # Mano de obra
        self.assertIn("$6.001,00", texto)  # Total

        orden_id = self.ultima_orden_id()
        impresion = self.cliente.get(f"/ordenes/{orden_id}/imprimir").get_data(as_text=True)
        self.assertIn(f"OT-{orden_id:05d}", impresion)
        self.assertIn("Pastillas de freno", impresion)
        self.assertIn("$6.001,00", impresion)
        self.assertEqual(self.cliente.get(f"/ordenes/{orden_id}/editar").status_code, 200)

    def test_05_cambio_de_estado_registra_fecha(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Iván Ruiz", "EST222"))
        self.crear_orden(self.vehiculo_id("EST222"))
        orden_id = self.ultima_orden_id()
        texto = self.cliente.post(
            f"/ordenes/{orden_id}/estado", data={"estado": "Completado"}, follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("Completado:", texto)
        respuesta = self.cliente.post(f"/ordenes/{orden_id}/estado", data={"estado": "Inventado"})
        self.assertEqual(respuesta.status_code, 400)

    def test_06_no_se_elimina_vehiculo_con_ordenes(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Sol Vega", "BLO333"))
        vehiculo_id = self.vehiculo_id("BLO333")
        self.crear_orden(vehiculo_id)
        texto = self.cliente.post(
            f"/vehiculos/{vehiculo_id}/eliminar", follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("No se puede eliminar", texto)
        self.assertIn("BLO333", texto)

    def test_07_orden_invalida_conserva_lo_escrito(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Tea Mora", "INV444"))
        texto = self.crear_orden(
            self.vehiculo_id("INV444"), fallas="", item_precio=["NaN", "-5"]
        ).get_data(as_text=True)
        self.assertIn("Anota las fallas", texto)
        self.assertIn("«Pastillas de freno»: revisa la cantidad y el precio", texto)
        self.assertIn("«Cambio de pastillas»: revisa la cantidad y el precio", texto)
        self.assertIn("Pastillas de freno", texto)

    def test_08_catalogo(self):
        datos = {"tipo": "Repuesto", "nombre": "Filtro de aceite", "precio": "850,5"}
        texto = self.cliente.post("/catalogo", data=datos, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Filtro de aceite", texto)
        self.assertIn("$850,50", texto)
        self.assertIn("Ya existe", self.cliente.post("/catalogo", data=datos).get_data(as_text=True))

    def test_09_csrf_rechaza_post_sin_token(self):
        app.config["TESTING"] = False
        try:
            self.assertEqual(self.cliente.post("/catalogo", data={}).status_code, 400)
        finally:
            app.config["TESTING"] = True

    def test_10_respaldo_diario_con_rotacion(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Rita Luna", "RES555"))
        carpeta = Path(app.config["RESPALDOS"])
        carpeta.mkdir(parents=True, exist_ok=True)
        hoy = carpeta / f"taller_{date.today().isoformat()}.db"
        hoy.unlink(missing_ok=True)
        for anio in range(35):
            (carpeta / f"taller_19{anio:02d}-01-01.db").touch()
        (carpeta / "taller_copia-manual.db").touch()

        self.assertEqual(crear_respaldo(), hoy)
        self.assertIsNone(crear_respaldo())  # Solo una copia por día.

        antiguos_y_hoy = list(carpeta.glob("taller_[0-9]*.db"))
        self.assertEqual(len(antiguos_y_hoy), MAXIMO_RESPALDOS)
        self.assertFalse((carpeta / "taller_1900-01-01.db").exists())  # Se borra lo más viejo.
        self.assertTrue((carpeta / "taller_copia-manual.db").exists())  # No toca otros archivos.
        self.assertEqual(ultimo_respaldo(), date.today().isoformat())

        copia = sqlite3.connect(hoy)
        try:
            encontrados = copia.execute(
                "SELECT COUNT(*) FROM vehiculos WHERE patente = 'RES555'"
            ).fetchone()[0]
        finally:
            copia.close()
        self.assertEqual(encontrados, 1)

    def test_11_panel_avisa_si_falla_el_respaldo(self):
        original = app.config["RESPALDOS"]
        # Una carpeta que cuelga de un archivo no se puede crear: fuerza el fallo.
        app.config["RESPALDOS"] = os.path.join(app.config["DATABASE"], "imposible")
        try:
            texto = self.cliente.get("/").get_data(as_text=True)
        finally:
            app.config["RESPALDOS"] = original
        self.assertIn("No se pudo crear la copia de seguridad de hoy", texto)
        self.assertNotIn("NotADirectoryError", texto)
        self.assertIn("No se pudo crear la copia de seguridad", self.leer_registro())

    def test_12_tablero_mueve_orden_y_actualiza_cifras(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Paz Rey", "TAB666"))
        self.crear_orden(self.vehiculo_id("TAB666"))
        orden_id = self.ultima_orden_id()
        numero = f"OT-{orden_id:05d}"
        self.assertIn(numero, self.pagina("/ordenes?estado=Pendiente&q=TAB666"))

        url = f"/api/ordenes/{orden_id}/estado"
        en_proceso = self.cliente.post(url, data={"estado": "En Proceso"}).get_json()
        self.assertIn('data-estado="En Proceso"', en_proceso["fila"])
        self.assertNotIn(numero, self.pagina("/ordenes?estado=Pendiente&q=TAB666"))
        self.assertIn(numero, self.pagina("/ordenes?estado=En%20Proceso&q=TAB666"))
        with app.app_context():
            antes = resumen_panel()

        completada = self.cliente.post(url, data={"estado": "Completado"}).get_json()
        self.assertIn('data-estado="Completado"', completada["fila"])
        self.assertIn(numero, self.pagina("/ordenes?estado=Completado&q=TAB666"))
        with app.app_context():
            ahora = resumen_panel()
        self.assertEqual(completada["conteos"], ahora["conteos"])
        self.assertEqual(ahora["meses"][-1]["ordenes"], antes["meses"][-1]["ordenes"] + 1)
        self.assertEqual(ahora["meses"][-1]["repuestos"], antes["meses"][-1]["repuestos"] + 300100)
        self.assertEqual(ahora["meses"][-1]["mano_obra"], antes["meses"][-1]["mano_obra"] + 300000)
        self.assertEqual(ahora["abiertas"], antes["abiertas"] - 1)
        self.assertEqual(ahora["completadas_hoy"], antes["completadas_hoy"] + 1)
        self.assertTrue(ahora["mas_facturado"])
        panel = self.pagina("/")
        self.assertIn("Cobrado este mes", panel)
        self.assertIn("Saldo por cobrar", panel)

        # La orden completada hoy suma a la ganancia de la semana, del mes y de su vehículo.
        with app.app_context():
            ganancias = ganancias_panel()
        for periodo in (ganancias["semana"], ganancias["mes"]):
            self.assertEqual(periodo["ganancia"], periodo["valor_completado"] - periodo["costo"])
            self.assertEqual(periodo["cobrado"], 0)  # Completar una orden no equivale a cobrarla.
            self.assertGreaterEqual(periodo["valor_completado"], 600100)
        self.assertIn("TAB666", [v["patente"] for v in ganancias["vehiculos"]])

        self.assertEqual(self.cliente.post(url, data={"estado": "Inventado"}).status_code, 400)
        self.assertEqual(self.cliente.post("/api/ordenes/999999/estado", data={"estado": "Pendiente"}).status_code, 404)

    def test_12b_respaldo_en_excel(self):
        import zipfile
        from xml.dom.minidom import parseString

        self.cliente.post("/clientes/nuevo", data=datos_cliente("Eva <Excel> & Cía", "XLS123"))
        self.crear_orden(self.vehiculo_id("XLS123"))

        respuesta = self.cliente.get("/respaldo/excel")
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("attachment", respuesta.headers["Content-Disposition"])
        respuesta.close()
        excel = Path(app.config["RESPALDOS_EXCEL"]) / f"taller_{date.today().isoformat()}.xlsx"
        self.assertTrue(excel.exists())
        with zipfile.ZipFile(excel) as archivo:
            nombres = archivo.namelist()
            for nombre in nombres:
                if nombre.endswith((".xml", ".rels")):
                    parseString(archivo.read(nombre))  # Todo el XML debe estar bien formado.
            hojas = "".join(archivo.read(n).decode("utf-8") for n in nombres if n.startswith("xl/worksheets/"))
            nombres_hojas = archivo.read("xl/workbook.xml").decode("utf-8")
        self.assertIn("XLS123", hojas)
        self.assertIn("Eva &lt;Excel&gt; &amp; Cía", hojas)
        self.assertIn("importe_centavos", hojas)  # El libro incluye el registro de Caja.
        self.assertIn("anulada_en", hojas)  # El libro conserva la hoja de anulaciones.
        self.assertIn("Movimientos de stock", nombres_hojas)
        self.assertIn("Detalle de recepciones", nombres_hojas)
        self.assertIn("Gastos", nombres_hojas)
        self.assertNotIn("pbkdf2", hojas)  # Nunca contraseñas (ni cifradas).
        self.assertNotIn("scrypt", hojas)

        # El empleado no puede descargarlo.
        self.entrar_como(EMPLEADO_ID)
        self.assertNotEqual(self.cliente.get("/respaldo/excel").status_code, 200)

    def test_13_orden_vieja_se_marca_atrasada(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Gil Sosa", "ATR777"))
        self.crear_orden(self.vehiculo_id("ATR777"), fecha_ingreso="2000-01-01")
        orden_id = self.ultima_orden_id()
        url = f"/api/ordenes/{orden_id}/estado"

        numero = f"OT-{orden_id:05d}"
        self.assertIn(numero, self.pagina("/ordenes?estado=atrasadas&q=ATR777"))
        self.assertIn(numero, self.pagina("/"))  # Aparece en "Requieren atención".

        abierta = self.cliente.post(url, data={"estado": "En Proceso"}).get_json()
        self.assertIn('data-atrasada="1"', abierta["fila"])
        completada = self.cliente.post(url, data={"estado": "Completado"}).get_json()
        self.assertIn('data-atrasada="0"', completada["fila"])
        self.assertNotIn(numero, self.pagina("/ordenes?estado=atrasadas&q=ATR777"))

    def test_14_vista_rapida_escapa_el_contenido(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("<b>Noa</b>", "RAP888"))
        self.crear_orden(self.vehiculo_id("RAP888"))
        orden_id = self.ultima_orden_id()
        texto = self.cliente.get(f"/ordenes/{orden_id}/rapida").get_data(as_text=True)
        self.assertIn("&lt;b&gt;Noa&lt;/b&gt;", texto)
        self.assertNotIn("<b>Noa</b>", texto)
        self.assertEqual(self.cliente.get("/ordenes/999999/rapida").status_code, 404)

    def test_17_listas_se_reparten_en_paginas(self):
        for numero in range(POR_PAGINA + 2):
            self.cliente.post(
                "/clientes/nuevo", data=datos_cliente(f"Paginado {numero:02d}", f"PAG{numero:03d}")
            )
        primera = self.pagina("/clientes?q=Paginado")
        self.assertIn("Paginado 00", primera)
        self.assertNotIn(f"Paginado {POR_PAGINA + 1:02d}", primera)
        self.assertIn("Siguiente", primera)
        self.assertIn("pagina=2", primera)

        segunda = self.pagina("/clientes?q=Paginado&pagina=2")
        self.assertIn(f"Paginado {POR_PAGINA + 1:02d}", segunda)
        self.assertNotIn("Paginado 00", segunda)
        self.assertIn("q=Paginado", segunda)  # La búsqueda se conserva al cambiar de página.

        # Una página inexistente no da error: muestra la última.
        self.assertIn(f"Paginado {POR_PAGINA + 1:02d}", self.pagina("/clientes?q=Paginado&pagina=999"))

    def test_18_secciones_de_ordenes_cargan(self):
        for estado in ("Pendiente", "En Proceso", "Completado", "abiertas", "atrasadas", "sin_precio", "inventado"):
            with self.subTest(estado=estado):
                respuesta = self.cliente.get("/ordenes", query_string={"estado": estado})
                self.assertEqual(respuesta.status_code, 200)

    def test_19_historial_muestra_solo_lo_de_ese_vehiculo(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Leo Ríos", "HIS111", "HIS222"))
        vehiculo_id = self.vehiculo_id("HIS111")

        # Una orden completada y una abierta en este vehículo, y una orden en el otro.
        self.crear_orden(vehiculo_id)
        self.cliente.post(
            f"/api/ordenes/{self.ultima_orden_id()}/estado", data={"estado": "Completado"}
        )
        self.crear_orden(
            vehiculo_id,
            item_tipo=["Repuesto"],
            item_descripcion=["Bomba de agua"],
            item_cantidad=["1"],
            item_precio=["400"],
        )
        self.crear_orden(
            self.vehiculo_id("HIS222"),
            item_tipo=["Servicio"],
            item_descripcion=["Trabajo del otro auto"],
            item_cantidad=["1"],
            item_precio=["999"],
        )

        texto = self.pagina(f"/vehiculos/{vehiculo_id}")
        self.assertIn("Pastillas de freno", texto)
        self.assertIn("Bomba de agua", texto)
        self.assertNotIn("Trabajo del otro auto", texto)
        self.assertIn("$6.001,00", texto)  # Total de lo completado.
        self.assertIn("$3.001,00", texto)  # Repuestos completados.
        self.assertIn("$400,00", texto)  # La orden abierta va aparte.

        lista = self.pagina("/vehiculos?q=HIS111")
        self.assertIn("HIS111", lista)
        self.assertNotIn("HIS222", lista)
        self.assertIn("$6.001,00", lista)
        self.assertEqual(self.cliente.get("/vehiculos/999999").status_code, 404)

    def test_20_costos_dan_ganancia_y_no_se_imprimen(self):
        datos = {"tipo": "Repuesto", "nombre": "Amortiguador", "precio": "50000", "costo": "30000"}
        lista = self.cliente.post("/catalogo", data=datos, follow_redirects=True).get_data(as_text=True)
        self.assertIn("$30.000,00", lista)

        self.cliente.post("/clientes/nuevo", data=datos_cliente("Mia Costa", "CST111"))
        vehiculo_id = self.vehiculo_id("CST111")
        # Repuesto: 2 x 1500,50 con costo 1000 c/u. Mano de obra sin costo.
        self.crear_orden(vehiculo_id, item_costo=["1000", ""])
        orden_id = self.ultima_orden_id()

        detalle = self.pagina(f"/ordenes/{orden_id}")
        self.assertIn("$2.000,00", detalle)  # Tu costo
        self.assertIn("$4.001,00", detalle)  # Ganancia: 6001 - 2000
        self.assertNotIn("sin costo cargado", detalle)
        self.assertIn('value="1000.00"', self.pagina(f"/ordenes/{orden_id}/editar"))

        for ruta in (f"/ordenes/{orden_id}/imprimir", f"/ordenes/{orden_id}/rapida"):
            with self.subTest(ruta=ruta):
                texto = self.pagina(ruta)
                self.assertNotIn("$2.000,00", texto)
                self.assertNotIn("costo", texto.lower())

        self.cliente.post(f"/api/ordenes/{orden_id}/estado", data={"estado": "Completado"})
        self.assertIn("$4.001,00", self.pagina(f"/vehiculos/{vehiculo_id}"))

        self.crear_orden(vehiculo_id)  # Sin costos: debe avisar.
        self.assertIn("sin costo cargado", self.pagina(f"/ordenes/{self.ultima_orden_id()}"))

        invalido = self.crear_orden(vehiculo_id, item_costo=["-5", ""]).get_data(as_text=True)
        self.assertIn("el costo no es válido", invalido)

    def test_21_base_anterior_recibe_las_columnas_nuevas(self):
        ruta = os.path.join(CARPETA_TEMPORAL, "version_anterior.db")
        conexion = sqlite3.connect(ruta)
        conexion.executescript(
            """
            CREATE TABLE repuestos_servicios (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT NOT NULL,
                nombre TEXT NOT NULL UNIQUE, precio_centavos INTEGER NOT NULL DEFAULT 0);
            INSERT INTO repuestos_servicios (tipo, nombre, precio_centavos) VALUES ('Repuesto', 'Viejo', 100);
            CREATE TABLE orden_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, orden_id INTEGER NOT NULL, tipo TEXT NOT NULL,
                descripcion TEXT NOT NULL, cantidad REAL NOT NULL,
                precio_unitario_centavos INTEGER NOT NULL, subtotal_centavos INTEGER NOT NULL);
            """
        )
        conexion.close()

        original = app.config["DATABASE"]
        app.config["DATABASE"] = ruta
        try:
            init_db()
            init_db()  # Una segunda vez no debe fallar.
        finally:
            app.config["DATABASE"] = original

        conexion = sqlite3.connect(ruta)
        try:
            columnas = {fila[1] for fila in conexion.execute("PRAGMA table_info(orden_items)")}
            fila = conexion.execute(
                "SELECT nombre, precio_centavos, costo_centavos FROM repuestos_servicios"
            ).fetchone()
        finally:
            conexion.close()
        self.assertTrue({"costo_unitario_centavos", "costo_total_centavos"} <= columnas)
        self.assertEqual(fila, ("Viejo", 100, None))  # Los datos anteriores se conservan.

    def test_22_repuesto_que_trae_el_cliente(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Iris Paz", "PRO111"))
        vehiculo_id = self.vehiculo_id("PRO111")
        # El cliente trae el repuesto (precio y costo vacíos) y solo se cobra la mano de obra.
        self.crear_orden(
            vehiculo_id,
            item_tipo=["Repuesto", "Servicio"],
            item_descripcion=["Pastillas que trajo el cliente", "Cambio de pastillas"],
            item_cantidad=["1", "1"],
            item_precio=["", "3000"],
            item_costo=["", ""],
            item_cliente=["1", "0"],  # La casilla "Lo trae el cliente" marcada en el repuesto.
        )
        orden_id = self.ultima_orden_id()
        detalle = self.pagina(f"/ordenes/{orden_id}")
        self.assertIn("Pastillas que trajo el cliente", detalle)
        self.assertNotIn("Revisa la cantidad", detalle)
        self.assertNotIn("sin costo cargado", detalle)  # No hay nada que cargar.
        self.assertIn("Ganancia estimada: <strong>$3.000,00</strong>", detalle)

        self.assertIn("(lo trajo el cliente)", detalle)
        self.assertIn("(provisto por el cliente)", self.pagina(f"/ordenes/{orden_id}/imprimir"))
        # Al editar, la casilla "Lo trae el cliente" aparece marcada.
        self.assertIn('class="linea-cliente h-4 w-4" checked', self.pagina(f"/ordenes/{orden_id}/editar"))

        self.cliente.post(f"/api/ordenes/{orden_id}/estado", data={"estado": "Completado"})
        self.assertNotIn("sin costo cargado", self.pagina(f"/vehiculos/{vehiculo_id}"))

    def test_23_copia_externa_y_actualizacion_del_dia(self):
        externa = Path(CARPETA_TEMPORAL) / "pendrive_simulado"
        app.config["RESPALDOS_EXTRA"] = [str(externa)]
        try:
            self.assertIn("Todavía no hay ninguna copia fuera de la compu", self.pagina("/"))

            self.cliente.post("/clientes/nuevo", data=datos_cliente("Ezequiel Mar", "EXT111"))
            texto = self.cliente.post("/respaldo", follow_redirects=True).get_data(as_text=True)
            self.assertIn("Copia guardada en la compu y en:", texto)

            copia = externa / f"taller_{date.today().isoformat()}.db"
            self.assertTrue(copia.exists())
            self.assertNotIn("Todavía no hay ninguna copia fuera de la compu", self.pagina("/"))

            # La copia del día se actualiza con lo que se carga después.
            self.cliente.post("/clientes/nuevo", data=datos_cliente("Ema Mar", "EXT222"))
            self.cliente.post("/respaldo")
            conexion = sqlite3.connect(copia)
            try:
                encontrados = conexion.execute(
                    "SELECT COUNT(*) FROM vehiculos WHERE patente IN ('EXT111', 'EXT222')"
                ).fetchone()[0]
            finally:
                conexion.close()
            self.assertEqual(encontrados, 2)
        finally:
            app.config["RESPALDOS_EXTRA"] = []

    def test_24_sin_pendrive_avisa_que_la_copia_quedo_solo_en_la_compu(self):
        texto = self.cliente.post("/respaldo", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Copia guardada solo en la compu", texto)

    def test_25_hay_que_iniciar_sesion(self):
        with self.cliente.session_transaction() as sesion:
            sesion.clear()
        respuesta = self.cliente.get("/clientes")
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/login", respuesta.headers["Location"])
        self.assertEqual(self.cliente.post("/api/ordenes/1/estado", data={"estado": "Pendiente"}).status_code, 401)

        incorrecto = self.cliente.post("/login", data={"usuario": "dueno", "clave": "mala"})
        self.assertIn("Usuario o contraseña incorrectos", incorrecto.get_data(as_text=True))

        dueno = self.cliente.post("/login", data={"usuario": "dueno", "clave": "clave-dueno-2026"})
        self.assertTrue(dueno.headers["Location"].endswith("/"))  # El dueño va al panel.

        self.cliente.post("/logout")
        empleado = self.cliente.post("/login", data={"usuario": "JUAN", "clave": "clave-juan-2026"})
        self.assertTrue(empleado.headers["Location"].endswith("/"))  # Cada rol entra a su panel.

    def test_26_empleado_no_ve_numeros_ni_borra(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Sara Lux", "EMP111"))
        vehiculo_id = self.vehiculo_id("EMP111")
        self.crear_orden(vehiculo_id)  # La crea el dueño, con precios.
        orden_id = self.ultima_orden_id()

        self.entrar_como(EMPLEADO_ID)
        panel = self.pagina("/")
        self.assertIn("Panel de operaciones", panel)
        self.assertIn('id="grafico-operativo"', panel)
        self.assertNotRegex(panel, r"\$\s?\d")
        self.assertNotIn("Reportes gráficos", panel)
        for ruta in ("/analitica", "/reportes", "/catalogo", "/usuarios", "/actividad", f"/ordenes/{orden_id}/imprimir"):
            with self.subTest(ruta=ruta):
                self.assertEqual(self.cliente.get(ruta).status_code, 403)
        for ruta in (f"/ordenes/{orden_id}/eliminar", f"/vehiculos/{vehiculo_id}/eliminar", "/respaldo"):
            with self.subTest(ruta=ruta):
                self.assertEqual(self.cliente.post(ruta).status_code, 403)

        for ruta in (
            f"/ordenes/{orden_id}",
            f"/ordenes/{orden_id}/rapida",
            f"/ordenes/{orden_id}/editar",
            f"/vehiculos/{vehiculo_id}",
            "/ordenes?q=EMP111",
            "/vehiculos?q=EMP111",
        ):
            with self.subTest(ruta=ruta):
                texto = self.pagina(ruta)
                self.assertIn("EMP111", texto)
                self.assertNotRegex(texto, r"\$\s?\d")  # Ningún importe a la vista.
                self.assertNotIn("1500.50", texto)  # Ni escondido en el formulario.

        # No puede completar órdenes.
        respuesta = self.cliente.post(f"/api/ordenes/{orden_id}/estado", data={"estado": "Completado"})
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("Solo el dueño", respuesta.get_json()["error"])
        self.assertEqual(
            self.cliente.post(f"/api/ordenes/{orden_id}/estado", data={"estado": "En Proceso"}).status_code, 200
        )

    def test_27_empleado_anota_y_el_dueno_pone_los_precios(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Tito Gil", "CAM111"))
        vehiculo_id = self.vehiculo_id("CAM111")

        # El empleado anota el trabajo, sin precios.
        self.entrar_como(EMPLEADO_ID)
        self.cliente.post(
            "/ordenes/nueva",
            data={
                "vehiculo_id": vehiculo_id,
                "fallas": "Pinchadura",
                "estado": "En Proceso",
                "fecha_ingreso": "2026-11-03",
                "item_id": ["", ""],
                "item_tipo": ["Repuesto", "Servicio"],
                "item_descripcion": ["Cámara rodado 14", "Cambio de cámara"],
                "item_cantidad": ["1", "1"],
                "item_cliente": ["0", "0"],
                "item_precio": ["99999", "99999"],  # Aunque lo intente, el servidor lo ignora.
            },
        )
        orden_id = self.ultima_orden_id()
        self.assertEqual(self.lineas(orden_id), [(0, 1), (0, 1)])  # (precio, falta_precio)

        # El dueño la ve en "Falta poner precio" y no puede completarla todavía.
        self.entrar_como(DUENO_ID)
        self.assertIn(f"OT-{orden_id:05d}", self.pagina("/ordenes?estado=sin_precio&q=CAM111"))
        bloqueada = self.cliente.post(f"/api/ordenes/{orden_id}/estado", data={"estado": "Completado"})
        self.assertEqual(bloqueada.status_code, 400)
        self.assertIn("falta poner precios", bloqueada.get_json()["error"])

        # El dueño pone los precios y la completa.
        self.cliente.post(
            f"/ordenes/{orden_id}/editar",
            data={
                "vehiculo_id": vehiculo_id,
                "fallas": "Pinchadura",
                "estado": "En Proceso",
                "fecha_ingreso": "2026-11-03",
                "item_tipo": ["Repuesto", "Servicio"],
                "item_descripcion": ["Cámara rodado 14", "Cambio de cámara"],
                "item_cantidad": ["1", "1"],
                "item_cliente": ["0", "0"],
                "item_precio": ["12000", "8000"],
                "item_costo": ["7000", ""],
            },
        )
        self.assertEqual(self.lineas(orden_id), [(1200000, 0), (800000, 0)])
        self.assertEqual(
            self.cliente.post(f"/api/ordenes/{orden_id}/estado", data={"estado": "Completado"}).status_code, 200
        )

        # Si el empleado edita una orden con precios (sin completar), los precios no se pierden.
        self.cliente.post(f"/api/ordenes/{orden_id}/estado", data={"estado": "En Proceso"})
        ids = self.ids_lineas(orden_id)
        self.entrar_como(EMPLEADO_ID)
        self.cliente.post(
            f"/ordenes/{orden_id}/editar",
            data={
                "vehiculo_id": vehiculo_id,
                "fallas": "Pinchadura (corregido por Juan)",
                "estado": "En Proceso",
                "fecha_ingreso": "2026-11-03",
                "item_id": [str(ids[0]), str(ids[1]), ""],
                "item_tipo": ["Repuesto", "Servicio", "Servicio"],
                "item_descripcion": ["Cámara rodado 14", "Cambio de cámara", "Balanceo"],
                "item_cantidad": ["1", "1", "1"],
                "item_cliente": ["0", "0", "0"],
            },
        )
        self.assertEqual(self.lineas(orden_id), [(1200000, 0), (800000, 0), (0, 1)])

    def test_28_actividad_registra_quien_hizo_que(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Ana Rojas", "ACT111"))
        self.entrar_como(EMPLEADO_ID)
        self.cliente.post(
            "/ordenes/nueva",
            data={
                "vehiculo_id": self.vehiculo_id("ACT111"),
                "fallas": "Revisión",
                "estado": "Pendiente",
                "fecha_ingreso": "2026-11-03",
                "item_tipo": ["Servicio"],
                "item_descripcion": ["Revisión general"],
                "item_cantidad": ["1"],
            },
        )
        orden_id = self.ultima_orden_id()

        self.entrar_como(DUENO_ID)
        actividad = self.pagina(f"/actividad?usuario={EMPLEADO_ID}")
        self.assertIn("Juan Empleado", actividad)
        self.assertIn("Creó la orden", actividad)
        self.assertIn("ACT111", actividad)
        self.assertIn("Juan Empleado</strong>: creó la orden", self.pagina(f"/ordenes/{orden_id}"))

    def test_29_primera_vez_pide_crear_al_dueno(self):
        original = app.config["DATABASE"]
        app.config["DATABASE"] = os.path.join(CARPETA_TEMPORAL, "nueva.db")
        try:
            init_db()
            cliente = app.test_client()
            respuesta = cliente.get("/clientes")
            self.assertIn("/configuracion-inicial", respuesta.headers["Location"])
            errores = cliente.post(
                "/configuracion-inicial",
                data={"nombre": "Roberto", "usuario": "roberto", "clave": "Roberto-clave-2026", "clave2": "otra-clave-distinta"},
            ).get_data(as_text=True)
            self.assertIn("no coinciden", errores)
            creada = cliente.post(
                "/configuracion-inicial",
                data={"nombre": "Roberto", "usuario": "roberto", "clave": "Roberto-clave-2026", "clave2": "Roberto-clave-2026"},
            )
            self.assertTrue(creada.headers["Location"].endswith("/"))
            self.assertEqual(cliente.get("/").status_code, 200)  # Quedó como dueño con sesión iniciada.
            self.assertIn("/login", cliente.get("/configuracion-inicial").headers["Location"])
        finally:
            app.config["DATABASE"] = original

    def test_30_usuarios_se_desactivan_y_no_pueden_entrar(self):
        self.cliente.post(
            "/usuarios",
            data={"nombre": "Pedro Tarde", "usuario": "pedro", "rol": "empleado", "clave": "clave-pedro-2026", "clave2": "clave-pedro-2026"},
        )
        conexion = conectar()
        try:
            pedro_id = conexion.execute("SELECT id FROM usuarios WHERE usuario = 'pedro'").fetchone()["id"]
        finally:
            conexion.close()
        self.cliente.post(f"/usuarios/{pedro_id}/activo")
        otro = app.test_client()
        texto = otro.post("/login", data={"usuario": "pedro", "clave": "clave-pedro-2026"}).get_data(as_text=True)
        self.assertIn("Usuario o contraseña incorrectos", texto)
        # El dueño no puede desactivarse a sí mismo.
        self.assertIn(
            "No puedes desactivar tu propio usuario",
            self.cliente.post(f"/usuarios/{DUENO_ID}/activo", follow_redirects=True).get_data(as_text=True),
        )

    def test_31_actividad_mide_produccion_por_persona_y_periodo(self):
        conexion = conectar()
        try:
            antes = conexion.execute(
                "SELECT COUNT(*) AS total FROM actividad WHERE accion IN ('Inició sesión', 'Cerró sesión')"
            ).fetchone()["total"]
        finally:
            conexion.close()
        otro = app.test_client()
        otro.post("/login", data={"usuario": "juan", "clave": "clave-juan-2026"})
        otro.post("/logout")

        self.cliente.post("/clientes/nuevo", data=datos_cliente("Leo Paz", "REN111"))
        self.entrar_como(EMPLEADO_ID)
        for _ in range(2):
            self.cliente.post(
                "/ordenes/nueva",
                data={
                    "vehiculo_id": self.vehiculo_id("REN111"),
                    "fallas": "Revisión",
                    "estado": "Pendiente",
                    "fecha_ingreso": "2026-11-03",
                    "item_tipo": ["Servicio"],
                    "item_descripcion": ["Revisión general"],
                    "item_cantidad": ["1"],
                },
            )

        self.entrar_como(DUENO_ID)
        self.cliente.post(f"/usuarios/{EMPLEADO_ID}/clave", data={"clave": "clave-juan-2026", "clave2": "clave-juan-2026"})

        conexion = conectar()
        try:
            despues = conexion.execute(
                "SELECT COUNT(*) AS total FROM actividad WHERE accion IN ('Inició sesión', 'Cerró sesión')"
            ).fetchone()["total"]
            creadas_juan = conexion.execute(
                "SELECT COUNT(*) AS total FROM actividad WHERE usuario_id = ? AND accion = 'Creó la orden'"
                " AND substr(momento, 1, 7) = ?",
                (EMPLEADO_ID, date.today().isoformat()[:7]),
            ).fetchone()["total"]
        finally:
            conexion.close()
        self.assertEqual(despues, antes)  # Entrar y salir no se registra.

        # Métrica de Juan en el mes en curso: coincide con lo que hay en la base.
        texto = self.pagina(f"/actividad?usuario={EMPLEADO_ID}")
        self.assertIn(f'data-metrica="creadas">{creadas_juan}<', texto)
        self.assertGreaterEqual(creadas_juan, 2)
        # Las acciones administrativas no aparecen salvo que se pidan.
        self.assertNotIn("Cambió una contraseña", self.pagina("/actividad"))
        self.assertIn("Cambió una contraseña", self.pagina("/actividad?todo=1"))
        # Fuera del período no hay nada.
        vacio = self.pagina(f"/actividad?usuario={EMPLEADO_ID}&desde=2000-01-01&hasta=2000-01-31")
        self.assertIn('data-metrica="creadas">0<', vacio)
        self.assertIn("No hay actividad en este período", vacio)
        # Sin persona elegida aparece la comparación de todo el equipo.
        self.assertIn("Rendimiento por persona", self.pagina("/actividad"))

    def test_32_reporte_trimestral_con_graficos_y_pdf(self):
        from app import datos_reporte, leer_trimestre, trimestre_vecino

        self.cliente.post("/clientes/nuevo", data=datos_cliente("Rita Paz", "REP111"))
        self.entrar_como(EMPLEADO_ID)
        self.cliente.post(
            "/ordenes/nueva",
            data={
                "vehiculo_id": self.vehiculo_id("REP111"),
                "fallas": "Service",
                "estado": "Pendiente",
                "fecha_ingreso": date.today().isoformat(),
                "item_tipo": ["Servicio"],
                "item_descripcion": ["Service completo"],
                "item_cantidad": ["1"],
            },
        )
        orden_id = self.ultima_orden_id()
        self.entrar_como(DUENO_ID)
        self.cliente.post(
            f"/ordenes/{orden_id}/editar",
            data={
                "vehiculo_id": self.vehiculo_id("REP111"),
                "fallas": "Service",
                "estado": "Pendiente",
                "fecha_ingreso": date.today().isoformat(),
                "item_tipo": ["Servicio"],
                "item_descripcion": ["Service completo"],
                "item_cantidad": ["1"],
                "item_precio": ["2500"],
            },
        )
        self.cliente.post(f"/ordenes/{orden_id}/estado", data={"estado": "Completado"})

        anio, trimestre = leer_trimestre(None)
        primero = (trimestre - 1) * 3 + 1
        conexion = conectar()
        try:
            completadas = conexion.execute(
                """SELECT COUNT(*) AS total FROM ordenes WHERE estado = 'Completado'
                   AND substr(fecha_completado, 1, 7) BETWEEN ? AND ?""",
                (f"{anio}-{primero:02d}", f"{anio}-{primero + 2:02d}"),
            ).fetchone()["total"]
        finally:
            conexion.close()

        texto = self.pagina("/reportes")
        self.assertIn(f"{trimestre}.º trimestre {anio}", texto)
        self.assertIn("Juan Empleado", texto)
        self.assertIn('id="datos-reporte"', texto)  # Datos para los gráficos interactivos.
        self.assertIn('id="grafico-medios"', texto)
        self.assertIn("Detalle de cobros y reversiones por medio de pago", texto)
        self.assertIn("graficos.js", texto)
        self.assertIn(f'data-total="completadas">{completadas}<', texto)
        self.assertGreaterEqual(completadas, 1)

        impresion = self.pagina(f"/reportes/imprimir?trimestre={anio}-{trimestre}")
        self.assertIn("Guardar como PDF", impresion)
        self.assertIn('id="grafico-medios"', impresion)
        self.assertIn("Juan Empleado", impresion)

        # Un trimestre sin datos carga en cero; uno futuro vuelve al actual.
        self.assertIn('data-total="ingresadas">0<', self.pagina("/reportes?trimestre=1999-1"))
        self.assertEqual(self.cliente.get("/reportes?trimestre=2999-1").status_code, 302)

        self.entrar_como(EMPLEADO_ID)
        for ruta in ("/reportes", "/reportes/imprimir"):
            with self.subTest(ruta=ruta):
                self.assertEqual(self.cliente.get(ruta).status_code, 403)

        # Cambio de trimestre y datos que reciben los gráficos.
        self.assertEqual(trimestre_vecino(2026, 1, -1), (2025, 4))
        self.assertEqual(trimestre_vecino(2026, 4, 1), (2027, 1))
        self.assertEqual(leer_trimestre("2026-9"), leer_trimestre(None))  # Inválido: el actual.
        with app.app_context():
            datos = datos_reporte(anio, trimestre)["graficos"]
        self.assertEqual(len(datos["meses"]), 3)
        self.assertEqual(sum(datos["taller"]["completadas"]), completadas)
        juan = next(p for p in datos["personas"] if p["nombre"] == "Juan Empleado")
        self.assertGreaterEqual(sum(juan["mensual"]["creadas"]), 1)

    def test_33_migraciones_versionadas_son_repetibles(self):
        init_db()
        init_db()
        conexion = conectar()
        try:
            versiones = conexion.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            tablas = {
                fila["name"]
                for fila in conexion.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            conexion.close()
        self.assertEqual([fila["version"] for fila in versiones], [1, 2, 3, 4, 5, 6, 7])
        self.assertTrue({"orden_pagos", "orden_anulaciones", "proveedores", "compras", "stock_movimientos", "gastos", "proveedor_pagos", "caja_cierres"} <= tablas)
        db = conectar()
        try:
            columnas_ordenes = {fila["name"] for fila in db.execute("PRAGMA table_info(ordenes)")}
            columnas_items = {fila["name"] for fila in db.execute("PRAGMA table_info(orden_items)")}
        finally:
            db.close()
        self.assertTrue({"fecha_prometida", "prioridad"} <= columnas_ordenes)
        self.assertIn("vigente", columnas_items)

    def test_34_anular_conserva_historial_y_saca_la_orden_de_operacion(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Marta Díaz", "ANU123"))
        self.crear_orden(self.vehiculo_id("ANU123"))
        orden_id = self.ultima_orden_id()

        respuesta = self.cliente.post(
            f"/ordenes/{orden_id}/anular",
            data={"motivo": "El cliente retiró el vehículo"},
            follow_redirects=True,
        )

        self.assertIn("anulada", respuesta.get_data(as_text=True).lower())
        self.assertNotIn(f"OT-{orden_id:05d}", self.pagina("/ordenes?estado=Pendiente"))
        self.assertIn(f"OT-{orden_id:05d}", self.pagina("/ordenes?estado=anuladas"))
        detalle = self.pagina(f"/ordenes/{orden_id}")
        self.assertIn("El cliente retiró el vehículo", detalle)
        self.assertIn("Pastillas de freno", detalle)

        cambio_estado = self.cliente.post(
            f"/ordenes/{orden_id}/estado",
            data={"estado": "Completado"},
            follow_redirects=True,
        )
        self.assertIn(
            "No se puede cambiar el estado de una orden anulada.",
            cambio_estado.get_data(as_text=True),
        )
        cambio_api = self.cliente.post(
            f"/api/ordenes/{orden_id}/estado", data={"estado": "Completado"}
        )
        self.assertEqual(cambio_api.status_code, 400)
        self.assertEqual(cambio_api.get_json()["error"], "No se puede cambiar el estado de una orden anulada.")

        conector = conectar()
        try:
            self.assertEqual(
                conector.execute(
                    "SELECT COUNT(*) AS n FROM orden_items WHERE orden_id = ?", (orden_id,)
                ).fetchone()["n"],
                2,
            )
        finally:
            conector.close()

        self.assertEqual(
            self.cliente.post(
                f"/ordenes/{orden_id}/pagos",
                data={"importe": "100", "medio": "Efectivo", "fecha": date.today().isoformat()},
            ).status_code,
            302,
        )
        conector = conectar()
        try:
            self.assertEqual(
                conector.execute("SELECT COUNT(*) AS n FROM orden_pagos WHERE orden_id = ?", (orden_id,)).fetchone()["n"],
                0,
            )
        finally:
            conector.close()

    def test_35_cobros_parciales_no_permiten_exceso_y_reversion_no_borra(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Pablo Ruiz", "PAG123"))
        self.crear_orden(self.vehiculo_id("PAG123"))
        orden_id = self.ultima_orden_id()
        fecha = date.today().isoformat()

        self.cliente.post(
            f"/ordenes/{orden_id}/pagos",
            data={"importe": "2000", "medio": "Efectivo", "fecha": fecha, "referencia": "Recibo 1"},
        )
        self.cliente.post(
            f"/ordenes/{orden_id}/pagos",
            data={"importe": "4001", "medio": "Transferencia", "fecha": fecha},
        )
        respuesta_exceso = self.cliente.post(
            f"/ordenes/{orden_id}/pagos",
            data={"importe": "0.01", "medio": "Efectivo", "fecha": fecha},
            follow_redirects=True,
        )
        self.assertIn("supera el saldo", respuesta_exceso.get_data(as_text=True).lower())

        conector = conectar()
        try:
            primer_pago = conector.execute(
                "SELECT id FROM orden_pagos WHERE orden_id = ? AND tipo = 'cobro' ORDER BY id LIMIT 1",
                (orden_id,),
            ).fetchone()["id"]
        finally:
            conector.close()
        self.cliente.post(
            f"/ordenes/{orden_id}/pagos/{primer_pago}/revertir",
            data={"motivo": "Se cargó por duplicado"},
            follow_redirects=True,
        )

        conector = conectar()
        try:
            eventos = conector.execute(
                "SELECT tipo, importe_centavos, revierte_id FROM orden_pagos WHERE orden_id = ? ORDER BY id",
                (orden_id,),
            ).fetchall()
            neto = conector.execute(
                """SELECT COALESCE(SUM(CASE WHEN tipo = 'cobro' THEN importe_centavos
                                               ELSE -importe_centavos END), 0) AS n
                   FROM orden_pagos WHERE orden_id = ?""",
                (orden_id,),
            ).fetchone()["n"]
        finally:
            conector.close()
        self.assertEqual(len(eventos), 3)
        self.assertEqual(eventos[0]["tipo"], "cobro")
        self.assertEqual(eventos[2]["tipo"], "reversion")
        self.assertEqual(eventos[2]["revierte_id"], primer_pago)
        self.assertEqual(neto, 400100)
        self.assertIn("$2.000,00", self.pagina(f"/ordenes/{orden_id}"))

    def test_36_empleado_no_puede_registrar_cobros_ni_anular(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Sofía León", "ROL123"))
        self.crear_orden(self.vehiculo_id("ROL123"))
        orden_id = self.ultima_orden_id()
        self.entrar_como(EMPLEADO_ID)

        for ruta, datos in (
            (f"/ordenes/{orden_id}/pagos", {"importe": "100", "medio": "Efectivo"}),
            (f"/ordenes/{orden_id}/anular", {"motivo": "Prueba"}),
        ):
            with self.subTest(ruta=ruta):
                self.assertEqual(self.cliente.post(ruta, data=datos).status_code, 403)

    def test_37_reportes_separan_trabajo_completado_de_dinero_cobrado(self):
        anio, trimestre = __import__("app").leer_trimestre(None)
        mes_actual = (date.today().month - 1) % 3
        with app.app_context():
            from app import datos_reporte

            reporte_antes = datos_reporte(anio, trimestre)
        cobrado_antes = reporte_antes["taller"][mes_actual]["cobrado"]
        medios_antes = reporte_antes["graficos"]["medios"]
        efectivo = medios_antes["etiquetas"].index("Efectivo")
        cobros_efectivo_antes = medios_antes["cobros"][efectivo]
        reversiones_efectivo_antes = medios_antes["reversiones"][efectivo]
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Elena Rey", "REP333"))
        self.crear_orden(self.vehiculo_id("REP333"))
        orden_id = self.ultima_orden_id()
        self.cliente.post(f"/ordenes/{orden_id}/estado", data={"estado": "Completado"})

        with app.app_context():
            sin_pago = datos_reporte(anio, trimestre)
        self.assertEqual(sin_pago["taller"][mes_actual]["cobrado"], cobrado_antes)
        completadas_antes = reporte_antes["taller"][mes_actual]["completadas"]
        self.assertEqual(sin_pago["taller"][mes_actual]["completadas"], completadas_antes + 1)

        self.cliente.post(
            f"/ordenes/{orden_id}/pagos",
            data={"importe": "3000", "medio": "Efectivo", "fecha": date.today().isoformat()},
        )
        with app.app_context():
            con_pago = datos_reporte(anio, trimestre)
        self.assertEqual(con_pago["taller"][mes_actual]["cobrado"], cobrado_antes + 300000)
        self.assertEqual(con_pago["taller"][mes_actual]["completadas"], completadas_antes + 1)
        medios = con_pago["graficos"]["medios"]
        self.assertEqual(medios["cobros"][efectivo], cobros_efectivo_antes + 300000)
        self.assertEqual(medios["reversiones"][efectivo], reversiones_efectivo_antes)

        conector = conectar()
        try:
            pago_id = conector.execute(
                "SELECT id FROM orden_pagos WHERE orden_id = ? AND tipo = 'cobro'", (orden_id,)
            ).fetchone()["id"]
        finally:
            conector.close()
        self.cliente.post(
            f"/ordenes/{orden_id}/pagos/{pago_id}/revertir", data={"motivo": "Prueba de reporte"}
        )
        with app.app_context():
            con_reversion = datos_reporte(anio, trimestre)
        medios_revertidos = con_reversion["graficos"]["medios"]
        self.assertEqual(medios_revertidos["cobros"][efectivo], cobros_efectivo_antes + 300000)
        self.assertEqual(
            medios_revertidos["reversiones"][efectivo], reversiones_efectivo_antes + 300000
        )

        gastos_antes = con_reversion["taller"][mes_actual]["gastos"]
        self.cliente.post("/gastos", data={
            "fecha": date.today().isoformat(), "categoria": "Servicios",
            "concepto": "Gasto para el reporte", "importe": "125",
            "medio": "Efectivo", "referencia": "Prueba de gráfico",
        })
        with app.app_context():
            con_gasto = datos_reporte(anio, trimestre)
        fila_gasto = con_gasto["taller"][mes_actual]
        self.assertEqual(fila_gasto["gastos"], gastos_antes + 12500)
        self.assertEqual(fila_gasto["resultado_neto"], fila_gasto["ganancia"] - fila_gasto["gastos"])
        self.assertEqual(con_gasto["graficos"]["taller"]["gastos"][mes_actual], gastos_antes + 12500)

        conector = conectar()
        try:
            gasto_id = conector.execute(
                "SELECT id FROM gastos WHERE concepto = ?", ("Gasto para el reporte",)
            ).fetchone()["id"]
        finally:
            conector.close()
        self.cliente.post(
            f"/gastos/{gasto_id}/revertir", data={"motivo": "Prueba de reversión del reporte"}
        )
        with app.app_context():
            gasto_revertido = datos_reporte(anio, trimestre)
        self.assertEqual(gasto_revertido["taller"][mes_actual]["gastos"], gastos_antes)
        self.assertEqual(
            gasto_revertido["taller"][mes_actual]["resultado_neto"],
            gasto_revertido["taller"][mes_actual]["ganancia"] - gastos_antes,
        )

    def test_38_caja_y_cuentas_por_cobrar_muestran_saldos_reales(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Raúl Paz", "CAJ123"))
        self.crear_orden(self.vehiculo_id("CAJ123"))
        orden_id = self.ultima_orden_id()
        self.cliente.post(f"/ordenes/{orden_id}/estado", data={"estado": "Completado"})
        self.cliente.post(
            f"/ordenes/{orden_id}/pagos",
            data={"importe": "1000", "medio": "Transferencia", "fecha": date.today().isoformat()},
        )

        caja = self.pagina("/caja")
        cuentas = self.pagina("/cuentas-por-cobrar")
        self.assertIn("Neto del período", caja)
        self.assertIn("Transferencia", caja)
        self.assertIn("Raúl Paz", caja)
        self.assertIn(f"OT-{orden_id:05d}", cuentas)
        self.assertIn("$5.001,00", cuentas)

    def test_39_migracion_no_inventa_cobros_historicos_y_respalda_antes(self):
        ruta = os.path.join(CARPETA_TEMPORAL, "erp_legacy.db")
        respaldo_dir = os.path.join(CARPETA_TEMPORAL, "respaldos_legacy")
        legacy = sqlite3.connect(ruta)
        legacy.executescript(ESQUEMA)
        legacy.execute(
            "INSERT INTO usuarios (usuario, nombre, clave_hash, rol) VALUES ('viejo', 'Dueño', 'hash', 'dueno')"
        )
        cliente_id = legacy.execute(
            "INSERT INTO clientes (nombre) VALUES ('Cliente histórico')"
        ).lastrowid
        vehiculo_id = legacy.execute(
            """INSERT INTO vehiculos (cliente_id, patente, marca, modelo)
               VALUES (?, 'OLD123', 'Ford', 'Ka')""",
            (cliente_id,),
        ).lastrowid
        legacy.execute(
            """INSERT INTO ordenes (vehiculo_id, fallas, estado, fecha_ingreso, fecha_completado)
               VALUES (?, 'Trabajo antiguo', 'Completado', '2026-10-01', '2026-10-02')""",
            (vehiculo_id,),
        )
        legacy.commit()
        legacy.close()

        original_db = app.config["DATABASE"]
        original_backups = app.config["RESPALDOS"]
        app.config.update(DATABASE=ruta, RESPALDOS=respaldo_dir)
        try:
            init_db()
            init_db()
        finally:
            app.config.update(DATABASE=original_db, RESPALDOS=original_backups)

        migrated = sqlite3.connect(ruta)
        try:
            flag = migrated.execute(
                "SELECT cobro_sin_conciliar FROM ordenes WHERE id = 1"
            ).fetchone()[0]
            cobros = migrated.execute("SELECT COUNT(*) FROM orden_pagos").fetchone()[0]
            integridad = migrated.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            migrated.close()
        self.assertEqual(flag, 1)
        self.assertEqual(cobros, 0)
        self.assertEqual(integridad, "ok")
        self.assertEqual(len(list(Path(respaldo_dir).glob("pre_migracion_*.db"))), 1)

    def test_40_compras_y_consumos_actualizan_stock_sin_duplicar(self):
        nombre = "Aceite 5W40 ERP"
        self.cliente.post(
            "/catalogo",
            data={
                "tipo": "Repuesto",
                "nombre": nombre,
                "precio": "18000",
                "costo": "9000",
                "sku": "ACE-5W40-ERP",
                "unidad": "litro",
                "controla_stock": "1",
                "stock_minimo": "2",
            },
        )
        self.cliente.post(
            "/proveedores",
            data={
                "nombre": "Lubricentro ERP",
                "documento": "30-12345678-9",
                "telefono": "1122334455",
                "email": "ventas@example.com",
                "direccion": "Ruta 5",
            },
        )
        conexion = conectar()
        try:
            articulo = conexion.execute(
                "SELECT id FROM repuestos_servicios WHERE nombre = ?", (nombre,)
            ).fetchone()
            proveedor = conexion.execute(
                "SELECT id FROM proveedores WHERE nombre = ?", ("Lubricentro ERP",)
            ).fetchone()
        finally:
            conexion.close()
        self.assertIsNotNone(articulo)
        self.assertIsNotNone(proveedor)
        articulo_id, proveedor_id = articulo["id"], proveedor["id"]

        self.cliente.post(
            "/compras/nueva",
            data={
                "proveedor_id": str(proveedor_id),
                "fecha": date.today().isoformat(),
                "documento": "FC-A-0001-00000001",
                "nota": "Compra de prueba",
                "articulo_id": [str(articulo_id)],
                "cantidad": ["5"],
                "costo_unitario": ["9500"],
            },
        )
        conexion = conectar()
        try:
            compra_id = conexion.execute("SELECT MAX(id) AS id FROM compras").fetchone()["id"]
            linea_compra_id = conexion.execute(
                "SELECT id FROM compra_items WHERE compra_id = ?", (compra_id,)
            ).fetchone()["id"]
        finally:
            conexion.close()

        self.assertIn("Borrador", self.pagina(f"/compras/{compra_id}"))
        self.assertEqual(self.stock_articulo(articulo_id), 0)
        datos_recepcion = {
            "solicitud_id": "recepcion-erp-40-a",
            "compra_item_id": [str(linea_compra_id)],
            "cantidad_recibir": ["2.5"],
        }
        self.assertEqual(
            self.cliente.post(f"/compras/{compra_id}/recibir", data=datos_recepcion).status_code,
            302,
        )
        self.cliente.post(f"/compras/{compra_id}/recibir", data=datos_recepcion)
        self.assertEqual(self.stock_articulo(articulo_id), 2500)
        self.assertIn("Parcial", self.pagina(f"/compras/{compra_id}"))

        self.cliente.post(
            f"/compras/{compra_id}/recibir",
            data={
                "solicitud_id": "recepcion-erp-40-b",
                "compra_item_id": [str(linea_compra_id)],
                "cantidad_recibir": ["2.5"],
            },
        )
        self.assertEqual(self.stock_articulo(articulo_id), 5000)
        self.assertIn("Recibida", self.pagina(f"/compras/{compra_id}"))

        cambio_identidad = self.cliente.post(
            f"/catalogo?editar={articulo_id}",
            data={
                "tipo": "Repuesto", "nombre": nombre, "precio": "18000", "costo": "9500",
                "sku": "ACE-5W40-ERP", "unidad": "bidon", "controla_stock": "1",
                "stock_minimo": "2",
            },
            follow_redirects=True,
        )
        self.assertIn("historial de stock", cambio_identidad.get_data(as_text=True).lower())
        self.assertEqual(self.stock_articulo(articulo_id), 5000)

        self.cliente.post("/clientes/nuevo", data=datos_cliente("Cliente stock", "STK123"))
        self.cliente.post(
            "/ordenes/nueva",
            data={
                "vehiculo_id": str(self.vehiculo_id("STK123")),
                "fallas": "Service con aceite",
                "estado": "En Proceso",
                "fecha_ingreso": date.today().isoformat(),
                "item_tipo": ["Repuesto"],
                "item_catalogo_id": [str(articulo_id)],
                "item_descripcion": [nombre],
                "item_cantidad": ["2"],
                "item_precio": ["18000"],
            },
        )
        orden_id = self.ultima_orden_id()
        conexion = conectar()
        try:
            linea_orden_id = conexion.execute(
                "SELECT id FROM orden_items WHERE orden_id = ?", (orden_id,)
            ).fetchone()["id"]
        finally:
            conexion.close()
        self.assertIn("Control de repuestos", self.pagina(f"/ordenes/{orden_id}"))
        ruta_consumo = f"/ordenes/{orden_id}/items/{linea_orden_id}/consumir"
        self.assertEqual(
            self.cliente.post(ruta_consumo, data={"motivo": "Instalado en el vehículo"}).status_code,
            302,
        )
        self.assertEqual(self.stock_articulo(articulo_id), 3000)
        conexion = conectar()
        try:
            costo_linea_consumida = conexion.execute(
                "SELECT costo_total_centavos FROM orden_items WHERE id = ?", (linea_orden_id,)
            ).fetchone()["costo_total_centavos"]
        finally:
            conexion.close()
        self.assertEqual(costo_linea_consumida, 1_900_000)
        self.cliente.post(
            "/ordenes/nueva",
            data={
                "vehiculo_id": str(self.vehiculo_id("STK123")),
                "fallas": "Revisión adicional",
                "estado": "En Proceso",
                "fecha_ingreso": date.today().isoformat(),
                "item_tipo": ["Repuesto"],
                "item_catalogo_id": [str(articulo_id)],
                "item_descripcion": [nombre],
                "item_cantidad": ["4"],
                "item_precio": ["18000"],
            },
        )
        orden_exceso = self.ultima_orden_id()
        conexion = conectar()
        try:
            linea_exceso = conexion.execute(
                "SELECT id FROM orden_items WHERE orden_id = ?", (orden_exceso,)
            ).fetchone()["id"]
        finally:
            conexion.close()
        exceso = self.cliente.post(
            f"/ordenes/{orden_exceso}/items/{linea_exceso}/consumir",
            data={"motivo": "Instalado en el vehículo"}, follow_redirects=True,
        )
        self.assertIn("No alcanza el stock", exceso.get_data(as_text=True))
        self.assertEqual(self.stock_articulo(articulo_id), 3000)
        self.cliente.post(ruta_consumo, data={"motivo": "Instalado en el vehículo"})
        self.assertEqual(self.stock_articulo(articulo_id), 3000)
        conexion = conectar()
        try:
            consumo_id = conexion.execute(
                "SELECT MAX(id) AS id FROM stock_movimientos WHERE orden_item_id = ? AND tipo = 'consumo'",
                (linea_orden_id,),
            ).fetchone()["id"]
        finally:
            conexion.close()
        self.cliente.post(
            f"/inventario/movimientos/{consumo_id}/revertir",
            data={"motivo": "Se retiró el repuesto antes de entregar"},
        )
        self.assertEqual(self.stock_articulo(articulo_id), 5000)
        conexion = conectar()
        try:
            costo_linea = conexion.execute(
                "SELECT costo_total_centavos FROM orden_items WHERE id = ?", (linea_orden_id,)
            ).fetchone()["costo_total_centavos"]
        finally:
            conexion.close()
        self.assertEqual(costo_linea, 1_900_000)
        respuesta_consumo_repetido = self.cliente.post(
            ruta_consumo, data={"motivo": "Instalado después de corregir"}, follow_redirects=True
        )
        self.assertIn("Consumo registrado", respuesta_consumo_repetido.get_data(as_text=True))
        self.assertEqual(self.stock_articulo(articulo_id), 3000, self.movimientos_articulo(articulo_id))
        anulacion = self.cliente.post(
            f"/ordenes/{orden_id}/anular", data={"motivo": "Trabajo cancelado por el cliente"},
            follow_redirects=True,
        )
        self.assertIn("Reversa los consumos", anulacion.get_data(as_text=True))
        conexion = conectar()
        try:
            consumo_id = conexion.execute(
                "SELECT MAX(id) AS id FROM stock_movimientos WHERE orden_item_id = ? AND tipo = 'consumo'",
                (linea_orden_id,),
            ).fetchone()["id"]
        finally:
            conexion.close()
        self.cliente.post(
            f"/inventario/movimientos/{consumo_id}/revertir",
            data={"motivo": "Repuesto devuelto al inventario"},
        )
        self.assertEqual(self.stock_articulo(articulo_id), 5000)
        self.assertEqual(
            self.cliente.post(
                f"/ordenes/{orden_id}/anular", data={"motivo": "Trabajo cancelado por el cliente"}
            ).status_code,
            302,
        )

    def test_41_gastos_pagos_proveedor_y_cierre_de_caja_concilian(self):
        nombre = "Filtro ERP financiero"
        self.cliente.post("/catalogo", data={
            "tipo": "Repuesto", "nombre": nombre, "precio": "200", "costo": "100",
            "sku": "FIL-ERP-41", "unidad": "unidad", "controla_stock": "1",
        })
        self.cliente.post("/proveedores", data={"nombre": "Proveedor Financiero ERP"})
        db = conectar()
        try:
            articulo_id = db.execute("SELECT id FROM repuestos_servicios WHERE nombre = ?", (nombre,)).fetchone()["id"]
            proveedor_id = db.execute("SELECT id FROM proveedores WHERE nombre = ?", ("Proveedor Financiero ERP",)).fetchone()["id"]
        finally:
            db.close()
        self.cliente.post("/compras/nueva", data={
            "proveedor_id": str(proveedor_id), "fecha": date.today().isoformat(),
            "documento": "FC-ERP-41", "articulo_id": [str(articulo_id)],
            "cantidad": ["1"], "costo_unitario": ["100"],
        })
        db = conectar()
        try:
            compra_id = db.execute("SELECT MAX(id) AS id FROM compras").fetchone()["id"]
            linea_id = db.execute("SELECT id FROM compra_items WHERE compra_id = ?", (compra_id,)).fetchone()["id"]
        finally:
            db.close()
        self.cliente.post(f"/compras/{compra_id}/recibir", data={
            "solicitud_id": "recepcion-finanzas-41", "compra_item_id": [str(linea_id)],
            "cantidad_recibir": ["1"],
        })
        self.cliente.post(f"/compras/{compra_id}/pagos", data={
            "importe": "25", "medio": "Transferencia", "fecha": date.today().isoformat(),
        })
        self.cliente.post("/gastos", data={
            "fecha": date.today().isoformat(), "categoria": "Servicios", "concepto": "Internet del taller",
            "importe": "5", "medio": "Efectivo", "referencia": "Factura 41",
        })
        self.assertIn("Internet del taller", self.pagina("/gastos"))
        self.assertIn("$75,00", self.pagina("/cuentas-por-pagar"))
        self.assertIn("Pago a proveedor", self.pagina("/caja"))
        self.assertIn("Gasto · Servicios", self.pagina("/caja"))
        db = conectar()
        try:
            pago_id = db.execute(
                "SELECT id FROM proveedor_pagos WHERE compra_id = ? AND tipo = 'pago'", (compra_id,)
            ).fetchone()["id"]
        finally:
            db.close()
        ruta_reversion = f"/compras/{compra_id}/pagos/{pago_id}/revertir"
        datos_reversion = {"motivo": "Pago registrado en factura equivocada"}
        self.cliente.post(ruta_reversion, data=datos_reversion)
        self.cliente.post(ruta_reversion, data=datos_reversion)
        self.assertIn("$100,00", self.pagina("/cuentas-por-pagar"))
        self.assertIn("Reversión de pago a proveedor", self.pagina("/caja"))
        db = conectar()
        try:
            reversiones_pago = db.execute(
                "SELECT COUNT(*) FROM proveedor_pagos WHERE revierte_id = ?", (pago_id,)
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(reversiones_pago, 1)

        fecha_hoy = date.today().isoformat()
        self.cliente.post("/caja/cerrar", data={
            "fecha": fecha_hoy, "efectivo_inicial": "100", "efectivo_contado": "94.50",
            "nota": "Cierre de prueba",
        })
        db = conectar()
        try:
            cierre = db.execute("SELECT * FROM caja_cierres WHERE fecha = ? AND estado = 'cerrado'", (fecha_hoy,)).fetchone()
            cantidad_gastos = db.execute("SELECT COUNT(*) AS total FROM gastos").fetchone()["total"]
        finally:
            db.close()
        self.assertEqual(cierre["efectivo_inicial_centavos"], 10000)
        self.assertEqual(cierre["efectivo_esperado_centavos"], 9500)
        self.assertEqual(cierre["diferencia_centavos"], -50)
        self.cliente.post("/gastos", data={
            "fecha": fecha_hoy, "categoria": "Otros", "concepto": "Debe rechazarse",
            "importe": "1", "medio": "Efectivo",
        })
        db = conectar()
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gastos").fetchone()[0], cantidad_gastos)
        finally:
            db.close()
        self.cliente.post(
            f"/caja/cierres/{cierre['id']}/reabrir", data={"motivo": "Corregir gasto de caja"}
        )
        self.cliente.post("/gastos", data={
            "fecha": fecha_hoy, "categoria": "Otros", "concepto": "Propina de reparto",
            "importe": "1", "medio": "Efectivo",
        })
        self.cliente.post("/caja/cerrar", data={
            "fecha": fecha_hoy, "efectivo_inicial": "100", "efectivo_contado": "94",
        })
        db = conectar()
        try:
            cierres = db.execute("SELECT * FROM caja_cierres WHERE fecha = ? ORDER BY id", (fecha_hoy,)).fetchall()
        finally:
            db.close()
        self.assertEqual([c["estado"] for c in cierres], ["reabierto", "cerrado"])
        self.assertEqual(cierres[-1]["efectivo_inicial_centavos"], 10000)
        self.assertEqual(cierres[-1]["efectivo_esperado_centavos"], 9400)

    def test_42_restaurar_valida_migra_y_respalda_la_base_anterior(self):
        ruta = Path(CARPETA_TEMPORAL) / "copia_legada.db"
        legacy = sqlite3.connect(ruta)
        legacy.executescript(ESQUEMA)
        legacy.execute(
            "INSERT INTO usuarios (id, usuario, nombre, clave_hash, rol) VALUES (1, 'dueno', 'Dueño', 'hash', 'dueno')"
        )
        cliente_id = legacy.execute(
            "INSERT INTO clientes (nombre) VALUES ('Cliente restaurado')"
        ).lastrowid
        vehiculo_id = legacy.execute(
            "INSERT INTO vehiculos (cliente_id, patente, marca, modelo) VALUES (?, 'RST123', 'Ford', 'Ka')",
            (cliente_id,),
        ).lastrowid
        legacy.execute(
            "INSERT INTO ordenes (vehiculo_id, fallas, estado, fecha_ingreso, fecha_completado) VALUES (?, 'Trabajo previo', 'Completado', ?, ?)",
            (vehiculo_id, date.today().isoformat(), date.today().isoformat()),
        )
        legacy.commit()
        legacy.close()

        respuesta = self.cliente.post(
            "/restaurar",
            data={"archivo": (io.BytesIO(ruta.read_bytes()), "copia_taller.db")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        texto = respuesta.get_data(as_text=True)
        self.assertIn("Base restaurada y verificada", texto)
        self.assertIn("Cliente restaurado", self.pagina("/clientes"))
        self.assertTrue(list(Path(app.config["RESPALDOS"]).glob("pre_restauracion_*.db")))
        conexion = conectar()
        try:
            versiones = [fila[0] for fila in conexion.execute("SELECT version FROM schema_migrations ORDER BY version")]
            conciliacion = conexion.execute("SELECT cobro_sin_conciliar FROM ordenes WHERE id = 1").fetchone()[0]
        finally:
            conexion.close()
        self.assertEqual(versiones, [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(conciliacion, 1)

        rechazada = self.cliente.post(
            "/restaurar",
            data={"archivo": (io.BytesIO(b"no es una base SQLite"), "corrupta.db")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("No se restauró la base", rechazada.get_data(as_text=True))
        self.assertIn("Cliente restaurado", self.pagina("/clientes"))

    def test_43_caja_acumula_dias_sin_cierre_y_exige_reapertura_en_orden(self):
        ruta = Path(CARPETA_TEMPORAL) / "caja_gaps.db"
        ruta.unlink(missing_ok=True)
        original_db = app.config["DATABASE"]
        original_respaldos = app.config["RESPALDOS"]
        fecha_a, fecha_b, fecha_c = (
            (date.today() - timedelta(days=2)).isoformat(),
            (date.today() - timedelta(days=1)).isoformat(),
            date.today().isoformat(),
        )
        carpeta_respaldos = Path(CARPETA_TEMPORAL) / "respaldos_caja_gaps"
        app.config.update(DATABASE=str(ruta), RESPALDOS=str(carpeta_respaldos))
        try:
            init_db()
            db = conectar()
            with db:
                usuario_id = crear_usuario(db, "dueño_caja", "Dueño Caja", "clave-caja", "dueno")
            db.close()
            self.entrar_como(usuario_id)

            self.cliente.post("/caja/cerrar", data={
                "fecha": fecha_a, "efectivo_inicial": "100", "efectivo_contado": "100",
            })
            for fecha, concepto, importe in (
                (fecha_b, "Gasto día intermedio", "5"),
                (fecha_c, "Gasto día de cierre", "2"),
            ):
                self.cliente.post("/gastos", data={
                    "fecha": fecha, "categoria": "Otros", "concepto": concepto,
                    "importe": importe, "medio": "Efectivo",
                })
            self.cliente.post("/caja/cerrar", data={
                "fecha": fecha_c, "efectivo_inicial": "", "efectivo_contado": "93",
            })
            db = conectar()
            try:
                cierres = db.execute(
                    "SELECT * FROM caja_cierres WHERE estado = 'cerrado' ORDER BY fecha"
                ).fetchall()
                cierre_a, cierre_c = cierres
                cierre_b = db.execute(
                    "SELECT id FROM caja_cierres WHERE fecha = ?", (fecha_b,)
                ).fetchone()
            finally:
                db.close()
            self.assertIsNone(cierre_b)
            self.assertEqual(cierre_c["efectivo_inicial_centavos"], 10000)
            self.assertEqual(cierre_c["efectivo_esperado_centavos"], 9300)

            reabrir_primero = self.cliente.post(
                f"/caja/cierres/{cierre_a['id']}/reabrir",
                data={"motivo": "Corregir período anterior"}, follow_redirects=True,
            )
            self.assertIn("Reabre primero los cierres posteriores", reabrir_primero.get_data(as_text=True))
            cierre_fuera_de_orden = self.cliente.post(
                "/caja/cerrar", data={
                    "fecha": fecha_b, "efectivo_inicial": "", "efectivo_contado": "95",
                }, follow_redirects=True,
            )
            self.assertIn("Reabre primero los cierres posteriores", cierre_fuera_de_orden.get_data(as_text=True))

            self.cliente.post(
                f"/caja/cierres/{cierre_c['id']}/reabrir",
                data={"motivo": "Corregir período"},
            )
            self.cliente.post(
                f"/caja/cierres/{cierre_a['id']}/reabrir",
                data={"motivo": "Corregir período anterior"},
            )
            self.cliente.post("/caja/cerrar", data={
                "fecha": fecha_a, "efectivo_inicial": "", "efectivo_contado": "100",
            })
            self.cliente.post("/caja/cerrar", data={
                "fecha": fecha_c, "efectivo_inicial": "", "efectivo_contado": "93",
            })
            db = conectar()
            try:
                cierre_final = db.execute(
                    "SELECT * FROM caja_cierres WHERE fecha = ? AND estado = 'cerrado' ORDER BY id DESC LIMIT 1",
                    (fecha_c,),
                ).fetchone()
            finally:
                db.close()
            self.assertEqual(cierre_final["efectivo_esperado_centavos"], 9300)
        finally:
            app.config.update(DATABASE=original_db, RESPALDOS=original_respaldos)

    def test_44_orden_con_fecha_prometida_prioridad_y_busqueda_operativa(self):
        cliente = datos_cliente("Agenda Operativa", "AGD444")
        cliente["telefono"] = "5551234567"
        self.cliente.post("/clientes/nuevo", data=cliente)
        vehiculo_id = self.vehiculo_id("AGD444")
        ingreso = date.today().isoformat()
        fecha_prometida = (date.today() + timedelta(days=3)).isoformat()

        invalida = self.cliente.post("/ordenes/nueva", data={
            "vehiculo_id": vehiculo_id, "fallas": "No enciende", "diagnostico": "Prueba de carga",
            "estado": "Pendiente", "fecha_ingreso": ingreso,
            "fecha_prometida": (date.today() - timedelta(days=1)).isoformat(),
            "prioridad": "Urgente",
        }, follow_redirects=True)
        self.assertIn("La fecha prometida no puede ser anterior al ingreso", invalida.get_data(as_text=True))

        creada = self.cliente.post("/ordenes/nueva", data={
            "vehiculo_id": vehiculo_id, "fallas": "No enciende", "diagnostico": "Prueba de carga alternador",
            "estado": "Pendiente", "fecha_ingreso": ingreso,
            "fecha_prometida": fecha_prometida, "prioridad": "Urgente",
        }, follow_redirects=True)
        self.assertIn(date.fromisoformat(fecha_prometida).strftime("%d/%m/%Y"), creada.get_data(as_text=True))
        orden_id = self.ultima_orden_id()
        db = conectar()
        try:
            orden = db.execute(
                "SELECT fecha_prometida, prioridad FROM ordenes WHERE id = ?", (orden_id,)
            ).fetchone()
        finally:
            db.close()
        self.assertEqual(orden["fecha_prometida"], fecha_prometida)
        self.assertEqual(orden["prioridad"], "Urgente")

        por_telefono = self.cliente.get(
            "/ordenes?q=5551234567&prioridad=Urgente&vencimiento=programada"
        ).get_data(as_text=True)
        self.assertIn(f"OT-{orden_id:05d}", por_telefono)
        por_diagnostico = self.cliente.get(
            "/ordenes?q=alternador&estado=abiertas"
        ).get_data(as_text=True)
        self.assertIn(f"OT-{orden_id:05d}", por_diagnostico)

    def test_45_empleado_no_puede_cambiar_fecha_ni_prioridad_de_orden(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Agenda Protegida", "AGD555"))
        vehiculo_id = self.vehiculo_id("AGD555")
        fecha_prometida = (date.today() + timedelta(days=5)).isoformat()
        self.cliente.post("/ordenes/nueva", data={
            "vehiculo_id": vehiculo_id, "fallas": "Ruido de motor", "diagnostico": "Revisar correa",
            "estado": "Pendiente", "fecha_ingreso": date.today().isoformat(),
            "fecha_prometida": fecha_prometida, "prioridad": "Alta",
        })
        orden_id = self.ultima_orden_id()
        self.entrar_como(EMPLEADO_ID)
        self.cliente.post(f"/ordenes/{orden_id}/editar", data={
            "vehiculo_id": vehiculo_id, "fallas": "Ruido actualizado", "diagnostico": "Revisar correa",
            "notas": "", "estado": "Pendiente", "kilometraje": "",
            "fecha_ingreso": date.today().isoformat(),
            "fecha_prometida": "1999-01-01", "prioridad": "Baja",
        })
        db = conectar()
        try:
            orden = db.execute(
                "SELECT fecha_prometida, prioridad FROM ordenes WHERE id = ?", (orden_id,)
            ).fetchone()
        finally:
            db.close()
        self.assertEqual(orden["fecha_prometida"], fecha_prometida)
        self.assertEqual(orden["prioridad"], "Alta")

    def test_46_ficha_cliente_resume_ordenes_activas_y_saldo(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Resumen Cliente", "CLI646"))
        vehiculo_id = self.vehiculo_id("CLI646")
        self.crear_orden(vehiculo_id)
        orden_activa = self.ultima_orden_id()
        self.crear_orden(vehiculo_id)
        orden_completada = self.ultima_orden_id()
        self.cliente.post(f"/ordenes/{orden_completada}/estado", data={"estado": "Completado"})
        self.cliente.post(f"/ordenes/{orden_completada}/pagos", data={"importe": "100", "medio": "Efectivo"})

        db = conectar()
        try:
            cliente_id = db.execute("SELECT id FROM clientes WHERE nombre = 'Resumen Cliente'").fetchone()["id"]
        finally:
            db.close()
        ficha = self.pagina(f"/clientes/{cliente_id}")
        self.assertIn("Órdenes activas", ficha)
        self.assertIn(f"OT-{orden_activa:05d}", ficha)
        self.assertIn("Saldo por cobrar", ficha)
        self.assertIn("$5.901,00", ficha)

    def test_47_catalogo_busca_sku_completo_y_codigo_de_barras(self):
        for nombre, sku in (("Filtro especial", "SCAN-0001"), ("Bujía común", "B-200")):
            self.cliente.post("/catalogo", data={
                "tipo": "Repuesto", "nombre": nombre, "precio": "500", "sku": sku,
                "unidad": "unidad", "controla_stock": "1",
            })

        resultado = self.pagina("/catalogo?q=scan-0001")
        self.assertIn("Filtro especial", resultado)
        self.assertNotIn("Bujía común", resultado)
        self.assertIn('value="scan-0001"', resultado)

    def test_48_inventario_muestra_valorizacion_y_sin_movimientos(self):
        self.cliente.post("/catalogo", data={
            "tipo": "Repuesto", "nombre": "Pastilla inventariada", "precio": "2000",
            "costo": "1250", "sku": "INV-480", "unidad": "juego",
            "controla_stock": "1", "stock_minimo": "1",
        })
        self.cliente.post("/catalogo", data={
            "tipo": "Repuesto", "nombre": "Filtro sin movimiento", "precio": "900",
            "costo": "400", "sku": "INV-481", "unidad": "unidad",
            "controla_stock": "1",
        })
        db = conectar()
        try:
            articulo_id = db.execute(
                "SELECT id FROM repuestos_servicios WHERE sku = 'INV-480'"
            ).fetchone()["id"]
            with db:
                db.execute(
                    """INSERT INTO stock_movimientos
                       (articulo_id, tipo, cantidad_milesimas, costo_unitario_centavos, motivo, usuario_id, creado_en)
                       VALUES (?, 'recepcion', 2000, 1250, 'Recepción de prueba', ?, ?)""",
                    (articulo_id, DUENO_ID, datetime.now().isoformat(timespec="seconds")),
                )
        finally:
            db.close()

        inventario = self.pagina("/inventario")
        self.assertIn("Valorización de existencias", inventario)
        self.assertIn("$25,00", inventario)
        self.assertIn("data-filtro-valor=\"sin_movimiento\"", inventario)
        self.assertRegex(inventario, r'data-filtro-item data-filtro-grupo="[^"]*sin_movimiento')
        self.assertIn("Filtro sin movimiento", inventario)

    def test_49_caja_muestra_cierres_y_exporta_movimientos_del_periodo(self):
        hoy = date.today().isoformat()
        self.cliente.post("/gastos", data={
            "fecha": hoy, "categoria": "Herramientas", "concepto": "Lámparas del taller",
            "importe": "5", "medio": "Efectivo",
        })
        self.cliente.post("/caja/cerrar", data={
            "fecha": hoy, "efectivo_inicial": "100", "efectivo_contado": "95",
            "nota": "Cierre de verificación",
        })

        libro = self.pagina(f"/caja?desde={hoy}&hasta={hoy}")
        self.assertIn("Historial de cierres", libro)
        self.assertIn("Cierre de verificación", libro)
        exportacion = self.cliente.get(f"/caja/exportar.csv?desde={hoy}&hasta={hoy}")
        self.assertEqual(exportacion.status_code, 200)
        self.assertIn("text/csv", exportacion.content_type)
        contenido = exportacion.data.decode("utf-8-sig")
        self.assertIn("Lámparas del taller", contenido)
        self.assertIn("-500", contenido)

    def test_50_catalogo_bloquea_cambios_de_identidad_si_esta_referenciado(self):
        referencias = (
            ("CAT-COMPRA-50", "Compra", "compra"),
            ("CAT-ORDEN-50", "Orden", "orden"),
        )
        articulo_ids = {}
        for sku, nombre, _referencia in referencias:
            self.cliente.post("/catalogo", data={
                "tipo": "Repuesto", "nombre": f"Filtro {nombre} 50", "precio": "100",
                "sku": sku, "unidad": "unidad", "controla_stock": "1",
            })
        db = conectar()
        try:
            articulo_ids = {
                fila["sku"]: fila["id"]
                for fila in db.execute(
                    "SELECT id, sku FROM repuestos_servicios WHERE sku IN (?, ?)",
                    ("CAT-COMPRA-50", "CAT-ORDEN-50"),
                )
            }
        finally:
            db.close()

        self.cliente.post("/proveedores", data={"nombre": "Proveedor Catálogo 50"})
        db = conectar()
        try:
            proveedor_id = db.execute(
                "SELECT id FROM proveedores WHERE nombre = 'Proveedor Catálogo 50'"
            ).fetchone()["id"]
        finally:
            db.close()
        self.cliente.post("/compras/nueva", data={
            "proveedor_id": str(proveedor_id), "fecha": date.today().isoformat(),
            "articulo_id": [str(articulo_ids["CAT-COMPRA-50"])],
            "cantidad": ["1"], "costo_unitario": ["50"],
        })

        self.cliente.post("/clientes/nuevo", data=datos_cliente("Cliente Catálogo 50", "CAT050"))
        self.cliente.post("/ordenes/nueva", data={
            "vehiculo_id": str(self.vehiculo_id("CAT050")), "fallas": "Revisión",
            "estado": "Pendiente", "fecha_ingreso": date.today().isoformat(),
            "item_tipo": ["Repuesto"], "item_catalogo_id": [str(articulo_ids["CAT-ORDEN-50"])],
            "item_descripcion": ["Filtro Orden 50"], "item_cantidad": ["1"],
            "item_precio": ["100"],
        })

        for sku, nombre, tipo_ref in referencias:
            articulo_id = articulo_ids[sku]
            respuesta = self.cliente.post(f"/catalogo?editar={articulo_id}", data={
                "tipo": "Repuesto", "nombre": f"Renombrado {nombre} 50", "precio": "100",
                "sku": f"{sku}-NUEVO", "unidad": "caja", "controla_stock": "1",
                "stock_minimo": "0",
            }, follow_redirects=True)
            self.assertIn("referencia", respuesta.get_data(as_text=True).lower(), tipo_ref)
            db = conectar()
            try:
                guardado = db.execute(
                    "SELECT nombre, sku, unidad FROM repuestos_servicios WHERE id = ?", (articulo_id,)
                ).fetchone()
            finally:
                db.close()
            self.assertEqual(guardado["nombre"], f"Filtro {'Compra' if tipo_ref == 'compra' else 'Orden'} 50")
            self.assertEqual(guardado["sku"], sku)
            self.assertEqual(guardado["unidad"], "unidad")

    def test_51_linea_de_orden_se_puede_corregir_despues_de_revertir_el_consumo(self):
        nombre = "Filtro corregible 51"
        self.cliente.post("/catalogo", data={
            "tipo": "Repuesto", "nombre": nombre, "precio": "200", "costo": "50",
            "sku": "FIX-051", "unidad": "unidad", "controla_stock": "1",
        })
        self.cliente.post("/proveedores", data={"nombre": "Proveedor FIX-051"})
        db = conectar()
        try:
            articulo_id = db.execute(
                "SELECT id FROM repuestos_servicios WHERE sku = 'FIX-051'"
            ).fetchone()["id"]
            proveedor_id = db.execute(
                "SELECT id FROM proveedores WHERE nombre = 'Proveedor FIX-051'"
            ).fetchone()["id"]
        finally:
            db.close()
        self.cliente.post("/compras/nueva", data={
            "proveedor_id": str(proveedor_id), "fecha": date.today().isoformat(),
            "articulo_id": [str(articulo_id)], "cantidad": ["5"], "costo_unitario": ["50"],
        })
        db = conectar()
        try:
            compra_id = db.execute("SELECT MAX(id) AS id FROM compras").fetchone()["id"]
            compra_item_id = db.execute(
                "SELECT id FROM compra_items WHERE compra_id = ?", (compra_id,)
            ).fetchone()["id"]
        finally:
            db.close()
        self.cliente.post(f"/compras/{compra_id}/recibir", data={
            "solicitud_id": "recepcion-fix-051", "compra_item_id": [str(compra_item_id)],
            "cantidad_recibir": ["5"],
        })
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Cliente FIX-051", "FIX051"))
        self.cliente.post("/ordenes/nueva", data={
            "vehiculo_id": str(self.vehiculo_id("FIX051")), "fallas": "Repuesto mal cargado",
            "estado": "En Proceso", "fecha_ingreso": date.today().isoformat(),
            "item_tipo": ["Repuesto"], "item_catalogo_id": [str(articulo_id)],
            "item_descripcion": [nombre], "item_cantidad": ["2"], "item_precio": ["200"],
            "item_costo": ["50"],
        })
        orden_id = self.ultima_orden_id()
        db = conectar()
        try:
            linea_id = db.execute(
                "SELECT id FROM orden_items WHERE orden_id = ? AND vigente = 1", (orden_id,)
            ).fetchone()["id"]
        finally:
            db.close()
        self.cliente.post(
            f"/ordenes/{orden_id}/items/{linea_id}/consumir",
            data={"motivo": "Instalación de prueba"},
        )
        db = conectar()
        try:
            consumo_id = db.execute(
                "SELECT id FROM stock_movimientos WHERE orden_item_id = ? AND tipo = 'consumo'",
                (linea_id,),
            ).fetchone()["id"]
        finally:
            db.close()
        self.cliente.post(
            f"/inventario/movimientos/{consumo_id}/revertir",
            data={"motivo": "Corrección antes de la entrega"},
        )

        respuesta = self.cliente.post(f"/ordenes/{orden_id}/editar", data={
            "vehiculo_id": str(self.vehiculo_id("FIX051")), "fallas": "Repuesto corregido",
            "estado": "En Proceso", "fecha_ingreso": date.today().isoformat(),
            "item_id": [str(linea_id)], "item_tipo": ["Repuesto"],
            "item_catalogo_id": [str(articulo_id)], "item_cliente": ["0"],
            "item_descripcion": [nombre], "item_cantidad": ["1"],
            "item_precio": ["200"], "item_costo": ["50"],
        })
        self.assertEqual(respuesta.status_code, 302)
        db = conectar()
        try:
            lineas = db.execute(
                "SELECT id, vigente, cantidad, subtotal_centavos FROM orden_items WHERE orden_id = ? ORDER BY id",
                (orden_id,),
            ).fetchall()
        finally:
            db.close()
        self.assertEqual(len(lineas), 2)
        self.assertEqual((lineas[0]["id"], lineas[0]["vigente"], lineas[0]["cantidad"]), (linea_id, 0, 2))
        self.assertEqual((lineas[1]["vigente"], lineas[1]["cantidad"], lineas[1]["subtotal_centavos"]), (1, 1, 20000))
        detalle = self.pagina(f"/ordenes/{orden_id}")
        self.assertIn("$200,00", detalle)
        self.assertIn("Líneas reemplazadas", detalle)
        self.assertIn("Línea reemplazada", self.pagina(f"/vehiculos/{self.vehiculo_id('FIX051')}"))

    def test_52_historial_vehiculo_identifica_y_excluye_orden_anulada(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Cliente Anulación 52", "ANU052"))
        vehiculo_id = self.vehiculo_id("ANU052")
        self.crear_orden(vehiculo_id)
        orden_id = self.ultima_orden_id()
        self.cliente.post(f"/ordenes/{orden_id}/estado", data={"estado": "Completado"})
        self.cliente.post(
            f"/ordenes/{orden_id}/anular", data={"motivo": "Trabajo cancelado por el cliente"}
        )

        historial = self.pagina(f"/vehiculos/{vehiculo_id}")
        self.assertIn("Orden anulada", historial)
        self.assertNotIn(">Completado</span>", historial)
        self.assertIn("$0,00", historial)
        listado = self.pagina("/vehiculos")
        self.assertIn("ANU052", listado)
        self.assertIn("Total completado", listado)

    def test_53_catalogo_permite_reactivar_articulos_archivados(self):
        respuesta = self.cliente.post("/catalogo", data={
            "tipo": "Repuesto", "nombre": "Articulo Archivo Frontend 53", "precio": "120",
            "costo": "80", "sku": "ARCH-FE-53", "unidad": "unidad", "stock_minimo": "0",
        })
        self.assertEqual(respuesta.status_code, 302)
        conexion = conectar()
        try:
            articulo_id = conexion.execute(
                "SELECT id FROM repuestos_servicios WHERE sku = 'ARCH-FE-53'"
            ).fetchone()["id"]
        finally:
            conexion.close()

        self.cliente.post(f"/catalogo/{articulo_id}/eliminar")
        archivados = self.pagina("/catalogo?estado=archivados")
        self.assertIn("Articulo Archivo Frontend 53", archivados)
        self.assertIn("Reactivar", archivados)
        self.assertEqual(
            self.cliente.post(f"/catalogo/{articulo_id}/reactivar").status_code, 302
        )
        self.assertIn("Articulo Archivo Frontend 53", self.pagina("/catalogo"))

    def test_54_ficha_proveedor_muestra_contacto_y_resumen_de_compras(self):
        self.cliente.post("/proveedores", data={
            "nombre": "Proveedor Ficha Frontend 54", "documento": "30-12345678-9",
            "telefono": "3415555454", "email": "compras@example.com", "direccion": "Taller 123",
        })
        conexion = conectar()
        try:
            proveedor_id = conexion.execute(
                "SELECT id FROM proveedores WHERE nombre = 'Proveedor Ficha Frontend 54'"
            ).fetchone()["id"]
        finally:
            conexion.close()

        ficha = self.pagina(f"/proveedores/{proveedor_id}")
        self.assertIn("Proveedor Ficha Frontend 54", ficha)
        self.assertIn("3415555454", ficha)
        self.assertIn("Compras asociadas", ficha)
        self.assertIn("Saldo pendiente", ficha)

    def test_55_compras_y_gastos_permiten_recorrer_todo_el_historial(self):
        prefijo = "HIST-FRONT-55"
        self.cliente.post("/proveedores", data={"nombre": f"Proveedor {prefijo}"})
        self.cliente.post("/catalogo", data={
            "tipo": "Repuesto", "nombre": f"Repuesto {prefijo}", "precio": "100",
            "costo": "75", "sku": prefijo, "unidad": "unidad", "controla_stock": "1",
        })
        conexion = conectar()
        try:
            proveedor_id = conexion.execute(
                "SELECT id FROM proveedores WHERE nombre = ?", (f"Proveedor {prefijo}",)
            ).fetchone()["id"]
            articulo_id = conexion.execute(
                "SELECT id FROM repuestos_servicios WHERE sku = ?", (prefijo,)
            ).fetchone()["id"]
        finally:
            conexion.close()

        compras_creadas = []
        for indice in range(POR_PAGINA + 1):
            respuesta = self.cliente.post("/compras/nueva", data={
                "proveedor_id": str(proveedor_id), "fecha": date.today().isoformat(),
                "documento": f"{prefijo}-{indice:02}", "articulo_id": [str(articulo_id)],
                "cantidad": ["1"], "costo_unitario": ["0.75"],
            })
            self.assertEqual(respuesta.status_code, 302)
            compras_creadas.append(respuesta.headers["Location"].rsplit("/", 1)[-1])

        fecha_gasto = date.today()
        conexion = conectar()
        try:
            while conexion.execute(
                "SELECT 1 FROM caja_cierres WHERE fecha = ? AND estado = 'cerrado'",
                (fecha_gasto.isoformat(),),
            ).fetchone():
                fecha_gasto -= timedelta(days=1)
        finally:
            conexion.close()
        for indice in range(POR_PAGINA + 1):
            respuesta = self.cliente.post("/gastos", data={
                "fecha": fecha_gasto.isoformat(), "categoria": "Otros",
                "concepto": f"{prefijo} gasto {indice:02}", "importe": "1.00",
                "medio": "Efectivo", "referencia": "",
            })
            self.assertEqual(respuesta.status_code, 302)

        compras_primera = self.pagina(f"/compras?q={prefijo}&pagina=1")
        compras_segunda = self.pagina(f"/compras?q={prefijo}&pagina=2")
        self.assertIn(f"Página 1 de 2 · {POR_PAGINA + 1} en total", compras_primera)
        self.assertIn(f"Página 2 de 2 · {POR_PAGINA + 1} en total", compras_segunda)
        self.assertIn(f"{prefijo}-00", compras_segunda)
        self.assertNotIn(f"{prefijo}-00</td>", compras_primera)

        gastos_primera = self.pagina(f"/gastos?q={prefijo}&pagina=1")
        gastos_segunda = self.pagina(f"/gastos?q={prefijo}&pagina=2")
        self.assertIn(f"Página 1 de 2 · {POR_PAGINA + 1} en total", gastos_primera)
        self.assertIn(f"Página 2 de 2 · {POR_PAGINA + 1} en total", gastos_segunda)
        self.assertIn(f"{prefijo} gasto 00", gastos_segunda)

    def test_56_compra_tiene_resumen_interactivo_y_quitar_linea(self):
        formulario = self.pagina("/compras/nueva")
        self.assertIn("data-compra-total", formulario)
        self.assertIn("data-quitar-linea", formulario)
        self.assertIn("aria-live=\"polite\"", formulario)

    def test_57_copias_y_restauracion_concentra_acciones_y_estado(self):
        pantalla = self.pagina("/restaurar")
        self.assertIn("Hacer copia ahora", pantalla)
        self.assertIn("Descargar Excel", pantalla)
        self.assertIn("Última copia", pantalla)
        self.assertIn("Último Excel", pantalla)

    def test_58_historial_de_stock_busca_y_pagina_todos_los_movimientos(self):
        prefijo = "STOCK-FRONT-58"
        self.cliente.post("/catalogo", data={
            "tipo": "Repuesto", "nombre": f"Artículo {prefijo}", "precio": "100",
            "costo": "75", "sku": prefijo, "unidad": "unidad", "controla_stock": "1",
        })
        conexion = conectar()
        try:
            articulo_id = conexion.execute(
                "SELECT id FROM repuestos_servicios WHERE sku = ?", (prefijo,)
            ).fetchone()["id"]
        finally:
            conexion.close()
        for cantidad in range(1, POR_PAGINA + 2):
            respuesta = self.cliente.post(
                f"/inventario/articulos/{articulo_id}/ajustar",
                data={"cantidad": str(cantidad), "motivo": f"{prefijo} ajuste {cantidad:02}"},
            )
            self.assertEqual(respuesta.status_code, 302)

        segunda = self.pagina(f"/inventario?q={prefijo}&pagina=2")
        primera = self.pagina(f"/inventario?q={prefijo}&pagina=1")
        self.assertIn(f"Página 2 de 2 · {POR_PAGINA + 1} en total", segunda)
        self.assertIn(f"{prefijo} ajuste 01", segunda)
        self.assertNotIn(f"{prefijo} ajuste 01", primera)

    def test_59_cuentas_por_cobrar_y_pagar_buscan_el_historial_completo(self):
        prefijo = "CUENTAS-FRONT-59"
        self.cliente.post("/clientes/nuevo", data=datos_cliente(f"Cliente {prefijo}", "CTA059"))
        vehiculo_id = self.vehiculo_id("CTA059")
        for _ in range(POR_PAGINA + 1):
            self.crear_orden(vehiculo_id)
            orden_id = self.ultima_orden_id()
            self.cliente.post(f"/ordenes/{orden_id}/estado", data={"estado": "Completado"})
        cobrar = self.pagina(f"/cuentas-por-cobrar?q={prefijo}&pagina=2")
        self.assertIn(f"Página 2 de 2 · {POR_PAGINA + 1} en total", cobrar)
        self.assertIn("Buscar en todas las cuentas", cobrar)

        self.cliente.post("/proveedores", data={"nombre": f"Proveedor {prefijo}"})
        self.cliente.post("/catalogo", data={
            "tipo": "Repuesto", "nombre": f"Repuesto {prefijo}", "precio": "100",
            "costo": "75", "sku": prefijo, "unidad": "unidad", "controla_stock": "1",
        })
        conexion = conectar()
        try:
            proveedor_id = conexion.execute(
                "SELECT id FROM proveedores WHERE nombre = ?", (f"Proveedor {prefijo}",)
            ).fetchone()["id"]
            articulo_id = conexion.execute(
                "SELECT id FROM repuestos_servicios WHERE sku = ?", (prefijo,)
            ).fetchone()["id"]
        finally:
            conexion.close()
        for indice in range(POR_PAGINA + 1):
            respuesta = self.cliente.post("/compras/nueva", data={
                "proveedor_id": str(proveedor_id), "fecha": date.today().isoformat(),
                "documento": f"{prefijo}-{indice:02}", "articulo_id": [str(articulo_id)],
                "cantidad": ["1"], "costo_unitario": ["0.75"],
            })
            compra_id = int(respuesta.headers["Location"].rsplit("/", 1)[-1])
            conexion = conectar()
            try:
                linea_id = conexion.execute(
                    "SELECT id FROM compra_items WHERE compra_id = ?", (compra_id,)
                ).fetchone()["id"]
            finally:
                conexion.close()
            recepcion = self.cliente.post(
                f"/compras/{compra_id}/recibir",
                data={
                    "solicitud_id": f"recepcion-{prefijo}-{indice}",
                    "compra_item_id": [str(linea_id)], "cantidad_recibir": ["1"],
                },
            )
            self.assertEqual(recepcion.status_code, 302)

        pagar = self.pagina(f"/cuentas-por-pagar?q={prefijo}&pagina=2")
        self.assertIn(f"Página 2 de 2 · {POR_PAGINA + 1} en total", pagar)
        self.assertIn("Buscar en todas las cuentas", pagar)

    def test_60_reversion_de_apertura_no_aparece_como_reversion_de_cobro(self):
        trimestre = ((date.today().month - 1) // 3) + 1
        meses = meses_del_trimestre(date.today().year, trimestre)
        with app.test_request_context():
            medios_antes = cobros_por_medio(meses)
        indice_efectivo = medios_antes["etiquetas"].index("Efectivo")
        reversiones_antes = medios_antes["reversiones"][indice_efectivo]

        self.cliente.post("/clientes/nuevo", data=datos_cliente("Cliente Reporte 60", "REP060"))
        self.crear_orden(self.vehiculo_id("REP060"))
        orden_id = self.ultima_orden_id()
        fecha = date.today().isoformat()
        conexion = conectar()
        try:
            with conexion:
                apertura_id = conexion.execute(
                    """INSERT INTO orden_pagos
                       (orden_id, tipo, importe_centavos, medio, fecha, usuario_id, creado_en)
                       VALUES (?, 'apertura', 2500, 'Efectivo', ?, ?, ?)""",
                    (orden_id, fecha, DUENO_ID, datetime.now().isoformat(timespec="seconds")),
                ).lastrowid
                conexion.execute(
                    """INSERT INTO orden_pagos
                       (orden_id, tipo, importe_centavos, medio, fecha, usuario_id,
                        revierte_id, motivo, creado_en)
                       VALUES (?, 'reversion', 2500, 'Efectivo', ?, ?, ?, 'Corrección de saldo', ?)""",
                    (orden_id, fecha, DUENO_ID, apertura_id, datetime.now().isoformat(timespec="seconds")),
                )
        finally:
            conexion.close()

        with app.test_request_context():
            medios_despues = cobros_por_medio(meses)
        self.assertEqual(medios_despues["reversiones"][indice_efectivo], reversiones_antes)

    def test_61_movimientos_de_caja_validan_fecha_dentro_del_bloqueo_de_escritura(self):
        fecha = date.today()
        conexion = conectar()
        try:
            while caja_fecha_cerrada(fecha.isoformat(), conexion):
                fecha -= timedelta(days=1)
        finally:
            conexion.close()

        self.cliente.post("/clientes/nuevo", data=datos_cliente("Cliente Caja 61", "CAJ061"))
        self.crear_orden(self.vehiculo_id("CAJ061"))
        orden_id = self.ultima_orden_id()
        self.cliente.post("/catalogo", data={
            "tipo": "Repuesto", "nombre": "Filtro Caja 61", "precio": "100",
            "costo": "50", "sku": "CAJ-61", "unidad": "unidad", "controla_stock": "1",
        })
        self.cliente.post("/proveedores", data={"nombre": "Proveedor Caja 61"})
        conexion = conectar()
        try:
            articulo_id = conexion.execute(
                "SELECT id FROM repuestos_servicios WHERE sku = 'CAJ-61'"
            ).fetchone()["id"]
            proveedor_id = conexion.execute(
                "SELECT id FROM proveedores WHERE nombre = 'Proveedor Caja 61'"
            ).fetchone()["id"]
        finally:
            conexion.close()
        self.cliente.post("/compras/nueva", data={
            "proveedor_id": str(proveedor_id), "fecha": fecha.isoformat(),
            "articulo_id": [str(articulo_id)], "cantidad": ["1"], "costo_unitario": ["50"],
        })
        conexion = conectar()
        try:
            compra_id = conexion.execute(
                "SELECT MAX(id) AS id FROM compras WHERE proveedor_id = ?", (proveedor_id,)
            ).fetchone()["id"]
            linea_id = conexion.execute(
                "SELECT id FROM compra_items WHERE compra_id = ?", (compra_id,)
            ).fetchone()["id"]
        finally:
            conexion.close()
        self.cliente.post(f"/compras/{compra_id}/recibir", data={
            "solicitud_id": "recepcion-caja-61", "compra_item_id": [str(linea_id)],
            "cantidad_recibir": ["1"],
        })

        def comprobar_con_bloqueo(fecha_texto, db=None):
            self.assertIsNotNone(db)
            self.assertTrue(db.in_transaction)
            return caja_fecha_cerrada(fecha_texto, db)

        with patch("app.caja_fecha_cerrada", side_effect=comprobar_con_bloqueo) as validar_caja:
            gasto = self.cliente.post("/gastos", data={
                "fecha": fecha.isoformat(), "categoria": "Otros", "concepto": "Prueba bloqueo 61",
                "importe": "1", "medio": "Efectivo",
            })
            cobro = self.cliente.post(f"/ordenes/{orden_id}/pagos", data={
                "importe": "1", "medio": "Efectivo", "fecha": fecha.isoformat(),
            })
            pago = self.cliente.post(f"/compras/{compra_id}/pagos", data={
                "importe": "1", "medio": "Efectivo", "fecha": fecha.isoformat(),
            })

        self.assertEqual((gasto.status_code, cobro.status_code, pago.status_code), (302, 302, 302))
        self.assertEqual(validar_caja.call_count, 3)

    def test_62_importe_pedido_y_deuda_redondean_cada_linea_a_centavos(self):
        self.cliente.post("/proveedores", data={"nombre": "Proveedor Redondeo 62"})
        self.cliente.post("/catalogo", data={
            "tipo": "Repuesto", "nombre": "Arandela Redondeo 62", "precio": "1",
            "costo": "0.01", "sku": "RED-62-A", "unidad": "unidad", "controla_stock": "1",
        })
        self.cliente.post("/catalogo", data={
            "tipo": "Repuesto", "nombre": "Tornillo Redondeo 62", "precio": "1",
            "costo": "0.01", "sku": "RED-62-B", "unidad": "unidad", "controla_stock": "1",
        })
        conexion = conectar()
        try:
            proveedor_id = conexion.execute(
                "SELECT id FROM proveedores WHERE nombre = 'Proveedor Redondeo 62'"
            ).fetchone()["id"]
            articulos = [fila["id"] for fila in conexion.execute(
                "SELECT id FROM repuestos_servicios WHERE sku IN ('RED-62-A', 'RED-62-B') ORDER BY sku"
            )]
        finally:
            conexion.close()
        respuesta = self.cliente.post("/compras/nueva", data={
            "proveedor_id": str(proveedor_id), "fecha": date.today().isoformat(),
            "documento": "REDONDEO-62", "articulo_id": [str(i) for i in articulos],
            "cantidad": ["0.5", "0.5"], "costo_unitario": ["0.01", "0.01"],
        })
        compra_id = int(respuesta.headers["Location"].rsplit("/", 1)[-1])
        detalle = self.pagina(f"/compras/{compra_id}")
        listado = self.pagina("/compras?q=REDONDEO-62")
        proveedor = self.pagina(f"/proveedores/{proveedor_id}")
        self.assertIn("$0,02", detalle)
        self.assertIn("$0,02", listado)
        self.assertIn("$0,02", proveedor)

        conexion = conectar()
        try:
            lineas = [fila["id"] for fila in conexion.execute(
                "SELECT id FROM compra_items WHERE compra_id = ? ORDER BY id", (compra_id,)
            )]
        finally:
            conexion.close()
        self.cliente.post(f"/compras/{compra_id}/recibir", data={
            "solicitud_id": "recepcion-redondeo-62", "compra_item_id": [str(i) for i in lineas],
            "cantidad_recibir": ["0.5", "0.5"],
        })
        cuenta = self.pagina(f"/compras/{compra_id}")
        self.assertIn("Importe recibido", cuenta)
        self.assertIn("$0,02", cuenta)

    def test_63_panel_del_dueno_muestra_grafico_y_acceso_a_reportes(self):
        panel = self.pagina("/")
        self.assertIn("Panel del taller", panel)
        self.assertIn('id="grafico"', panel)
        self.assertIn('id="grafico-vacio"', panel)
        self.assertIn("Reportes gráficos", panel)

    def test_64_panel_operativo_del_empleado_muestra_grafico_sin_importes(self):
        conexion = conectar()
        try:
            with conexion:
                empleado_id = crear_usuario(
                    conexion, "panel-empleado-64", "Empleado Panel", "clave-panel", "empleado"
                )
        finally:
            conexion.close()
        self.entrar_como(empleado_id)
        panel = self.pagina("/")
        self.assertIn("Panel de operaciones", panel)
        self.assertIn('id="grafico-operativo"', panel)
        self.assertIn("graficos.js", panel)
        self.assertNotRegex(panel, r"\$\s?\d")
        self.assertNotIn("Reportes gráficos", panel)

    def test_65_analitica_concilia_produccion_cobros_gastos_y_compara_periodos(self):
        from app import datos_analitica, rango_analitica

        fecha_movimiento = date.today()
        conexion = conectar()
        try:
            while caja_fecha_cerrada(fecha_movimiento.isoformat(), conexion):
                fecha_movimiento -= timedelta(days=1)
        finally:
            conexion.close()
        with app.app_context():
            rango = rango_analitica({"periodo": "30d"})
            antes = datos_analitica(rango)["actual"]

        self.cliente.post("/clientes/nuevo", data=datos_cliente("Cliente Analítica 65", "ANA065"))
        self.crear_orden(self.vehiculo_id("ANA065"))
        orden_id = self.ultima_orden_id()
        self.cliente.post(f"/ordenes/{orden_id}/estado", data={"estado": "Completado"})
        self.cliente.post(f"/ordenes/{orden_id}/pagos", data={
            "importe": "3000", "medio": "Efectivo", "fecha": fecha_movimiento.isoformat(),
        })
        self.cliente.post("/gastos", data={
            "fecha": fecha_movimiento.isoformat(), "categoria": "Otros", "concepto": "Analítica 65",
            "importe": "120.50", "medio": "Efectivo",
        })

        with app.app_context():
            rango = rango_analitica({"periodo": "30d"})
            despues = datos_analitica(rango)
        actual = despues["actual"]
        self.assertEqual(actual["trabajo"] - antes["trabajo"], 600100)
        self.assertEqual(actual["cobrado"] - antes["cobrado"], 300000)
        self.assertEqual(actual["gastos"] - antes["gastos"], 12050)
        self.assertEqual(actual["flujo_caja"] - antes["flujo_caja"], 287950)

        pagina = self.pagina("/analitica?periodo=30d")
        for texto in (
            "Analítica del negocio", "Trabajo terminado", "Cobros netos",
            "Costo de repuestos consumidos", "Resultado estimado", "Flujo neto de caja",
            "Ticket promedio", "Producción y movimiento de caja", "Gastos por rubro",
            'id="grafico-flujo"', 'id="grafico-ordenes"', 'id="grafico-margen"',
            'id="grafico-medios"', 'id="grafico-rubros"',
        ):
            self.assertIn(texto, pagina)

    def test_66_rango_analitico_acepta_hasta_un_ano_y_rechaza_fechas_invalidas(self):
        from app import rango_analitica

        hoy = date.today()
        personalizado = rango_analitica({
            "periodo": "personalizado",
            "desde": (hoy - timedelta(days=2)).isoformat(),
            "hasta": hoy.isoformat(),
        })
        self.assertEqual(personalizado["dias"], 3)
        self.assertEqual(personalizado["anterior_hasta"], hoy - timedelta(days=3))

        un_ano = rango_analitica({
            "periodo": "personalizado",
            "desde": (hoy - timedelta(days=365)).isoformat(),
            "hasta": hoy.isoformat(),
        })
        self.assertEqual(un_ano["dias"], 366)
        futuro = rango_analitica({
            "periodo": "personalizado",
            "desde": hoy.isoformat(),
            "hasta": (hoy + timedelta(days=1)).isoformat(),
        })
        self.assertEqual(futuro["periodo"], "30d")
        demasiado_largo = rango_analitica({
            "periodo": "personalizado",
            "desde": (hoy - timedelta(days=366)).isoformat(),
            "hasta": hoy.isoformat(),
        })
        self.assertEqual(demasiado_largo["periodo"], "30d")

    def test_67_login_bloquea_temporalmente_tras_intentos_repetidos(self):
        cliente = app.test_client()
        for _ in range(5):
            respuesta = cliente.post("/login", data={"usuario": "dueno", "clave": "incorrecta"})
        self.assertIn("Demasiados intentos", respuesta.get_data(as_text=True))

        bloqueado = cliente.post("/login", data={"usuario": "dueno", "clave": "clave-dueno-2026"})
        self.assertIn("Demasiados intentos", bloqueado.get_data(as_text=True))
        conexion = conectar()
        try:
            intentos = conexion.execute(
                "SELECT bloqueado_hasta FROM intentos_login WHERE bucket = ?", ("usuario:dueno",)
            ).fetchone()
        finally:
            conexion.close()
        self.assertIsNotNone(intentos["bloqueado_hasta"])

    def test_68_sesion_caduca_por_inactividad_y_login_registra_ultimo_acceso(self):
        cliente = app.test_client()
        cliente.post("/login", data={"usuario": "dueno", "clave": "clave-dueno-2026"})
        conexion = conectar()
        try:
            ultimo_acceso = conexion.execute(
                "SELECT ultimo_acceso FROM usuarios WHERE id = ?", (DUENO_ID,)
            ).fetchone()["ultimo_acceso"]
        finally:
            conexion.close()
        self.assertTrue(ultimo_acceso)

        with cliente.session_transaction() as sesion:
            sesion["actividad_sesion"] = (datetime.now() - timedelta(hours=9)).isoformat(timespec="seconds")
        respuesta = cliente.get("/clientes")
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/login", respuesta.headers["Location"])

    def test_69_reset_de_clave_revoca_sesion_y_obliga_al_usuario_a_cambiarla(self):
        empleado = app.test_client()
        empleado.post("/login", data={"usuario": "juan", "clave": "clave-juan-2026"})
        clave_nueva = "juan-clave-segura-2026"
        cambio = self.cliente.post(
            f"/usuarios/{EMPLEADO_ID}/clave",
            data={"clave": clave_nueva, "clave2": clave_nueva},
        )
        self.assertEqual(cambio.status_code, 302)

        sesion_anterior = empleado.get("/")
        self.assertEqual(sesion_anterior.status_code, 302)
        self.assertIn("/login", sesion_anterior.headers["Location"])

        empleado_nuevo = app.test_client()
        ingreso = empleado_nuevo.post("/login", data={"usuario": "juan", "clave": clave_nueva})
        self.assertIn("/mi-clave", ingreso.headers["Location"])
        inicio_bloqueado = empleado_nuevo.get("/")
        self.assertIn("/mi-clave", inicio_bloqueado.headers["Location"])
        actualizada = empleado_nuevo.post(
            "/mi-clave", data={"clave": "juan-otra-clave-segura", "clave2": "juan-otra-clave-segura"}
        )
        self.assertEqual(actualizada.status_code, 302)
        self.assertTrue(empleado_nuevo.get("/").status_code == 200)

    def test_70_alta_y_cambio_exigen_contrasena_de_doce_caracteres(self):
        self.assertEqual(LARGO_MINIMO_CLAVE, 12)
        pagina = self.cliente.post(
            "/usuarios",
            data={"nombre": "Clave Débil", "usuario": "clave.debil", "rol": "empleado", "clave": "corta123", "clave2": "corta123"},
            follow_redirects=True,
        )
        self.assertIn("al menos 12 caracteres", pagina.get_data(as_text=True))

    def test_71_migracion_conserva_cuentas_y_exige_renovar_clave_anterior(self):
        ruta_db = os.path.join(CARPETA_TEMPORAL, "migracion_v6.db")
        ruta_respaldos = os.path.join(CARPETA_TEMPORAL, "migracion_v6_respaldos")
        conexion = sqlite3.connect(ruta_db)
        conexion.executescript(
            """CREATE TABLE usuarios (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 usuario TEXT NOT NULL UNIQUE COLLATE NOCASE,
                 nombre TEXT NOT NULL,
                 clave_hash TEXT NOT NULL,
                 rol TEXT NOT NULL CHECK (rol IN ('dueno', 'empleado')),
                 activo INTEGER NOT NULL DEFAULT 1,
                 creado_en TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
               );"""
        )
        conexion.execute(
            "INSERT INTO usuarios (usuario, nombre, clave_hash, rol) VALUES (?, ?, ?, ?)",
            ("dueno-v6", "Dueño v6", generate_password_hash("clave-anterior-v6"), "dueno"),
        )
        conexion.commit()
        conexion.close()

        anterior_db, anterior_respaldos = app.config["DATABASE"], app.config["RESPALDOS"]
        app.config.update(DATABASE=ruta_db, RESPALDOS=ruta_respaldos)
        try:
            init_db()
            conexion = conectar()
            try:
                usuario = conexion.execute(
                    "SELECT * FROM usuarios WHERE usuario = 'dueno-v6'"
                ).fetchone()
                version = conexion.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                respaldo = list(Path(ruta_respaldos).glob("pre_migracion_*.db"))
            finally:
                conexion.close()
            self.assertEqual(version, 7)
            self.assertEqual(usuario["debe_cambiar_clave"], 1)
            self.assertTrue(usuario["clave_hash"])
            self.assertEqual(len(respaldo), 1)

            cliente = app.test_client()
            ingreso = cliente.post(
                "/login", data={"usuario": "dueno-v6", "clave": "clave-anterior-v6"}
            )
            self.assertIn("/mi-clave", ingreso.headers["Location"])
            cambio = cliente.post(
                "/mi-clave", data={"clave": "clave-nueva-migracion-v7", "clave2": "clave-nueva-migracion-v7"}
            )
            self.assertEqual(cambio.status_code, 302)
            self.assertEqual(cliente.get("/").status_code, 200)
        finally:
            app.config.update(DATABASE=anterior_db, RESPALDOS=anterior_respaldos)

    def test_72_matriz_impide_que_empleado_entre_a_modulos_y_acciones_del_dueno(self):
        self.entrar_como(EMPLEADO_ID)
        for ruta in (
            "/analitica", "/reportes", "/actividad", "/caja", "/caja/exportar.csv",
            "/cuentas-por-cobrar", "/cuentas-por-pagar", "/gastos", "/proveedores",
            "/proveedores/1", "/compras", "/inventario", "/catalogo", "/usuarios",
            "/restaurar", "/respaldo/excel",
        ):
            with self.subTest(ruta=ruta):
                self.assertEqual(self.cliente.get(ruta).status_code, 403)

        for ruta, datos in (
            ("/caja/cerrar", {"fecha": date.today().isoformat()}),
            ("/gastos", {"fecha": date.today().isoformat(), "importe": "1"}),
            ("/ordenes/999999/pagos", {"importe": "1", "medio": "Efectivo"}),
            ("/inventario/articulos/1/ajustar", {"motivo": "Prueba"}),
            ("/usuarios", {"nombre": "No autorizado"}),
            ("/respaldo", {}),
            ("/restaurar", {}),
        ):
            with self.subTest(ruta=ruta):
                self.assertEqual(self.cliente.post(ruta, data=datos).status_code, 403)

    def test_73_css_compilado_cubre_app_y_documentos_sin_runtime(self):
        for ruta in ("/", "/analitica", "/reportes/imprimir"):
            with self.subTest(ruta=ruta):
                html = self.pagina(ruta)
                self.assertIn("tailwind.css", html)
                self.assertNotIn("tailwindcss.js", html)
                self.assertNotIn("cdn.tailwindcss.com", html)

        hoja = self.cliente.get("/static/tailwind.css")
        self.assertEqual(hoja.status_code, 200)
        self.assertTrue(hoja.content_type.startswith("text/css"))
        estilos = hoja.get_data(as_text=True)
        hoja.close()
        for selector in (".btn-primario", ".md\\:flex", ".print\\:max-w-none", ".campo"):
            with self.subTest(selector=selector):
                self.assertIn(selector, estilos)

    def test_74_indicadores_y_graficos_de_analitica_enlazan_a_movimientos(self):
        pagina = self.pagina("/analitica?periodo=30d")
        for tipo in (
            "produccion", "cobros", "costos", "gastos", "resultado", "flujo",
            "movimientos", "ordenes", "margen", "trabajos",
        ):
            with self.subTest(tipo=tipo):
                self.assertIn(f"tipo={tipo}", pagina)
        self.assertIn("Ver movimientos del período", pagina)

    def test_75_detalle_analitico_abre_registros_fuente_y_protege_finanzas(self):
        patente = "DRILL075"
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Cliente Detalle 75", patente))
        self.crear_orden(self.vehiculo_id(patente))
        orden_id = self.ultima_orden_id()
        self.cliente.post(f"/ordenes/{orden_id}/estado", data={"estado": "Completado"})
        hoy = date.today().isoformat()
        detalle = self.cliente.get(
            f"/analitica/detalle?tipo=produccion&periodo=personalizado&desde={hoy}&hasta={hoy}"
        )
        html = detalle.get_data(as_text=True)
        self.assertEqual(detalle.status_code, 200)
        self.assertIn(f"OT-{orden_id:05d}", html)
        self.assertIn("Cliente Detalle 75", html)
        self.assertIn(f"/ordenes/{orden_id}", html)
        detalle.close()

        self.entrar_como(EMPLEADO_ID)
        empleado = self.cliente.get("/analitica/detalle?tipo=produccion")
        self.assertEqual(empleado.status_code, 403)
        empleado.close()

    def test_76_totales_de_detalle_reconcilian_con_indicadores_financieros(self):
        patente = "DRILL076"
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Cliente Detalle 76", patente))
        self.crear_orden(self.vehiculo_id(patente))
        orden_id = self.ultima_orden_id()
        self.cliente.post(f"/ordenes/{orden_id}/estado", data={"estado": "Completado"})

        fecha_movimiento = date.today()
        conexion = conectar()
        try:
            for _ in range(366):
                if not caja_fecha_cerrada(fecha_movimiento.isoformat(), conexion):
                    break
                fecha_movimiento -= timedelta(days=1)
        finally:
            conexion.close()
        self.cliente.post(f"/ordenes/{orden_id}/pagos", data={
            "importe": "250.25", "medio": "Efectivo", "fecha": fecha_movimiento.isoformat(),
        })
        self.cliente.post("/gastos", data={
            "fecha": fecha_movimiento.isoformat(), "categoria": "Otros", "concepto": "Detalle 76",
            "importe": "17.50", "medio": "Efectivo",
        })
        conexion = conectar()
        try:
            gasto_id = conexion.execute(
                "SELECT id FROM gastos WHERE concepto = ? ORDER BY id DESC LIMIT 1", ("Detalle 76",)
            ).fetchone()["id"]
        finally:
            conexion.close()
        libro_gastos = self.cliente.get(f"/gastos?gasto_id={gasto_id}")
        self.assertEqual(libro_gastos.status_code, 200)
        self.assertIn(f'id="gasto-{gasto_id}"', libro_gastos.get_data(as_text=True))
        libro_gastos.close()

        from app import datos_analitica, rango_analitica
        with app.app_context():
            esperado = datos_analitica(rango_analitica({"periodo": "12m"})) ["actual"]
        tipos = {
            "produccion": "trabajo",
            "cobros": "cobrado",
            "costos": "costo_consumido",
            "gastos": "gastos",
            "margen": "margen",
            "resultado": "resultado_estimado",
            "flujo": "flujo_caja",
        }
        for tipo, clave in tipos.items():
            with self.subTest(tipo=tipo):
                pagina = self.cliente.get(f"/analitica/detalle?tipo={tipo}&periodo=12m")
                html = pagina.get_data(as_text=True)
                self.assertEqual(pagina.status_code, 200)
                self.assertIn(f'id="total-detalle"', html)
                self.assertIn(f"{esperado[clave] / 100:,.2f}".replace(",", "§").replace(".", ",").replace("§", "."), html)
                if tipo == "resultado":
                    enlace_gasto = f"/gastos?gasto_id={gasto_id}#gasto-{gasto_id}"
                    encontrado = enlace_gasto in html
                    for pagina_num in range(2, 9):
                        if encontrado:
                            break
                        otra_pagina = self.cliente.get(
                            f"/analitica/detalle?tipo=resultado&periodo=12m&pagina={pagina_num}"
                        )
                        encontrado = enlace_gasto in otra_pagina.get_data(as_text=True)
                        otra_pagina.close()
                    self.assertTrue(encontrado, "El detalle debe enlazar al gasto fuente desde alguna página")
                pagina.close()
        for tipo in ("movimientos", "ordenes", "trabajos"):
            with self.subTest(tipo=tipo):
                pagina = self.cliente.get(f"/analitica/detalle?tipo={tipo}&periodo=12m")
                self.assertEqual(pagina.status_code, 200)
                pagina.close()

    def test_77_error_de_restauracion_no_se_expone_y_queda_registrado(self):
        marcador = "MARCADOR_INTERNO_RESTAURAR"
        with patch("app.verificar_archivo_base", side_effect=sqlite3.DatabaseError(marcador)):
            respuesta = self.cliente.post(
                "/restaurar",
                data={"archivo": (io.BytesIO(b"base"), "copia.db")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
        texto = respuesta.get_data(as_text=True)
        self.assertIn("No se restauró la base", texto)
        self.assertNotIn(marcador, texto)
        self.assertIn(marcador, self.leer_registro())

    def test_78_error_de_respaldo_no_se_expone_y_queda_registrado(self):
        marcador = "MARCADOR_INTERNO_RESPALDO"
        with patch("app.respaldar", side_effect=OSError(marcador)):
            texto = self.cliente.post("/respaldo", follow_redirects=True).get_data(as_text=True)
        self.assertIn("No se pudo crear la copia de seguridad", texto)
        self.assertNotIn(marcador, texto)
        self.assertIn(marcador, self.leer_registro())

    def test_79_error_de_excel_no_se_expone_y_queda_registrado(self):
        marcador = "MARCADOR_INTERNO_EXCEL"
        with patch("app.crear_respaldo_excel", side_effect=OSError(marcador)):
            texto = self.cliente.get("/respaldo/excel", follow_redirects=True).get_data(as_text=True)
        self.assertIn("No se pudo crear el Excel", texto)
        self.assertNotIn(marcador, texto)
        self.assertIn(marcador, self.leer_registro())

    def test_80_error_de_copia_externa_no_expone_ruta_ni_detalle(self):
        destino = Path(CARPETA_TEMPORAL) / "destino_privado"
        app.config["RESPALDOS_EXTRA"] = [str(destino)]
        marcador = "MARCADOR_INTERNO_DESTINO"
        try:
            with patch("app.shutil.copy2", side_effect=OSError(marcador)):
                texto = self.cliente.post("/respaldo", follow_redirects=True).get_data(as_text=True)
        finally:
            app.config["RESPALDOS_EXTRA"] = []
        self.assertIn("No se pudo copiar a una ubicación externa", texto)
        self.assertNotIn(str(destino), texto)
        self.assertNotIn(marcador, texto)
        self.assertIn(marcador, self.leer_registro())

    def test_81_error_al_copiar_excel_externo_queda_registrado_y_no_se_expone(self):
        destino = Path(CARPETA_TEMPORAL) / "destino_excel_privado"
        app.config["RESPALDOS_EXTRA"] = [str(destino)]
        marcador = "MARCADOR_INTERNO_EXCEL_EXTERNO"
        copiar_real = shutil.copy2

        def copiar_con_fallo_de_excel(origen, destino_final):
            if Path(destino_final).parent.name == "Excel":
                raise OSError(marcador)
            return copiar_real(origen, destino_final)

        try:
            with patch("app.shutil.copy2", side_effect=copiar_con_fallo_de_excel):
                texto = self.cliente.post("/respaldo", follow_redirects=True).get_data(as_text=True)
        finally:
            app.config["RESPALDOS_EXTRA"] = []
        self.assertIn("No se pudo copiar a una ubicación externa", texto)
        self.assertNotIn(marcador, texto)
        self.assertIn(marcador, self.leer_registro())

    def test_82_sesion_de_usuario_desactivado_se_revoca_en_rutas_privadas(self):
        conexion = conectar()
        try:
            with conexion:
                usuario_id = crear_usuario(
                    conexion, "desactivado", "Usuario Desactivado",
                    "clave-desactivado-2026", "empleado",
                )
                conexion.execute("UPDATE usuarios SET activo = 0 WHERE id = ?", (usuario_id,))
        finally:
            conexion.close()
        self.entrar_como(usuario_id)

        rutas = (
            "/", "/clientes", "/vehiculos", "/ordenes", "/ordenes/999999",
            "/catalogo", "/inventario", "/proveedores", "/compras", "/gastos",
            "/caja", "/actividad", "/usuarios", "/analitica", "/analitica/detalle",
            "/reportes", "/restaurar",
        )
        for ruta in rutas:
            with self.subTest(ruta=ruta):
                respuesta = self.cliente.get(ruta)
                self.assertEqual(respuesta.status_code, 302)
                self.assertIn("/login", respuesta.headers["Location"])
        self.assertEqual(
            self.cliente.post("/api/ordenes/999999/estado", data={"estado": "Pendiente"}).status_code,
            401,
        )
        self.assertEqual(self.cliente.get("/healthz").status_code, 200)

        with self.cliente.session_transaction() as sesion:
            self.assertNotIn("usuario_id", sesion)

    def test_83_reversion_de_recepcion_fraccionaria_conserva_pago_valido(self):
        sku = "REDONDEO-83"
        self.cliente.post("/catalogo", data={
            "tipo": "Repuesto", "nombre": "Arandela Redondeo 83", "precio": "10",
            "costo": "5.00", "sku": sku, "unidad": "unidad", "controla_stock": "1",
        })
        self.cliente.post("/proveedores", data={"nombre": "Proveedor Redondeo 83"})
        conexion = conectar()
        try:
            articulo_id = conexion.execute(
                "SELECT id FROM repuestos_servicios WHERE sku = ?", (sku,)
            ).fetchone()["id"]
            proveedor_id = conexion.execute(
                "SELECT id FROM proveedores WHERE nombre = ?", ("Proveedor Redondeo 83",)
            ).fetchone()["id"]
        finally:
            conexion.close()

        self.cliente.post("/compras/nueva", data={
            "proveedor_id": str(proveedor_id), "fecha": date.today().isoformat(),
            "documento": "RED-83", "articulo_id": [str(articulo_id)],
            "cantidad": ["0.002"], "costo_unitario": ["5.00"],
        })
        conexion = conectar()
        try:
            compra_id = conexion.execute(
                "SELECT MAX(id) AS id FROM compras WHERE proveedor_id = ?", (proveedor_id,)
            ).fetchone()["id"]
            compra_item_id = conexion.execute(
                "SELECT id FROM compra_items WHERE compra_id = ?", (compra_id,)
            ).fetchone()["id"]
        finally:
            conexion.close()

        for solicitud_id in ("redondeo-83-a", "redondeo-83-b"):
            respuesta = self.cliente.post(f"/compras/{compra_id}/recibir", data={
                "solicitud_id": solicitud_id,
                "compra_item_id": [str(compra_item_id)],
                "cantidad_recibir": ["0.001"],
            })
            self.assertEqual(respuesta.status_code, 302)

        fecha_pago = date.today()
        conexion = conectar()
        try:
            while caja_fecha_cerrada(fecha_pago.isoformat(), conexion):
                fecha_pago -= timedelta(days=1)
        finally:
            conexion.close()
        self.cliente.post(f"/compras/{compra_id}/pagos", data={
            "importe": "0.01", "medio": "Transferencia", "fecha": fecha_pago.isoformat(),
        })

        conexion = conectar()
        try:
            movimiento_id = conexion.execute(
                """SELECT id FROM stock_movimientos
                   WHERE recepcion_item_id IN (
                       SELECT id FROM compra_recepcion_items WHERE compra_item_id = ?
                   ) AND tipo = 'recepcion' ORDER BY id LIMIT 1""",
                (compra_item_id,),
            ).fetchone()["id"]
        finally:
            conexion.close()

        respuesta = self.cliente.post(
            f"/inventario/movimientos/{movimiento_id}/revertir",
            data={"motivo": "Corrección de recepción fraccionaria"},
            follow_redirects=True,
        )
        self.assertEqual(respuesta.status_code, 200)
        conexion = conectar()
        try:
            movimiento_restante = conexion.execute(
                """SELECT m.id FROM stock_movimientos m
                   WHERE m.recepcion_item_id IN (
                       SELECT id FROM compra_recepcion_items WHERE compra_item_id = ?
                   ) AND m.tipo = 'recepcion'
                     AND NOT EXISTS (
                         SELECT 1 FROM stock_movimientos rev WHERE rev.revierte_movimiento_id = m.id
                     )""",
                (compra_item_id,),
            ).fetchone()["id"]
            self.assertEqual(
                conexion.execute(
                    "SELECT COUNT(*) FROM stock_movimientos WHERE revierte_movimiento_id = ?",
                    (movimiento_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(cuenta_compra(compra_id, conexion), {
                "total": 1, "pagado": 1, "saldo": 0,
            })
            self.assertEqual(
                conexion.execute(
                    "SELECT COALESCE(SUM(cantidad_milesimas), 0) FROM stock_movimientos WHERE articulo_id = ?",
                    (articulo_id,),
                ).fetchone()[0],
                1,
            )
        finally:
            conexion.close()
        self.cliente.post(
            f"/inventario/movimientos/{movimiento_restante}/revertir",
            data={"motivo": "Recepción deja pago sin respaldo"},
        )
        conexion = conectar()
        try:
            self.assertIsNone(conexion.execute(
                "SELECT 1 FROM stock_movimientos WHERE revierte_movimiento_id = ?",
                (movimiento_restante,),
            ).fetchone())
            self.assertEqual(cuenta_compra(compra_id, conexion), {
                "total": 1, "pagado": 1, "saldo": 0,
            })
        finally:
            conexion.close()

    def leer_registro(self):
        ruta = Path(app.config["LOG_DIR"]) / "taller.log"
        for handler in app.logger.handlers:
            handler.flush()
        return ruta.read_text(encoding="utf-8") if ruta.exists() else ""

    # --- Ayudas ---
    def lineas(self, orden_id):
        conexion = conectar()
        try:
            return [
                (fila["precio_unitario_centavos"], fila["falta_precio"])
                for fila in conexion.execute(
                    "SELECT * FROM orden_items WHERE orden_id = ? ORDER BY id", (orden_id,)
                )
            ]
        finally:
            conexion.close()

    def ids_lineas(self, orden_id):
        conexion = conectar()
        try:
            return [
                fila["id"]
                for fila in conexion.execute("SELECT id FROM orden_items WHERE orden_id = ? ORDER BY id", (orden_id,))
            ]
        finally:
            conexion.close()

    def stock_articulo(self, articulo_id):
        conexion = conectar()
        try:
            fila = conexion.execute(
                "SELECT COALESCE(SUM(cantidad_milesimas), 0) AS stock FROM stock_movimientos WHERE articulo_id = ?",
                (articulo_id,),
            ).fetchone()
            return fila["stock"]
        finally:
            conexion.close()

    def movimientos_articulo(self, articulo_id):
        conexion = conectar()
        try:
            return [dict(fila) for fila in conexion.execute(
                "SELECT id, tipo, cantidad_milesimas, orden_item_id, revierte_movimiento_id FROM stock_movimientos WHERE articulo_id = ? ORDER BY id",
                (articulo_id,),
            )]
        finally:
            conexion.close()

    def pagina(self, ruta):
        return self.cliente.get(ruta).get_data(as_text=True)

    def vehiculo_id(self, patente):
        from app import conectar

        conexion = conectar()
        try:
            return conexion.execute(
                "SELECT id FROM vehiculos WHERE patente = ?", (patente,)
            ).fetchone()["id"]
        finally:
            conexion.close()

    def ultima_orden_id(self):
        from app import conectar

        conexion = conectar()
        try:
            return conexion.execute("SELECT MAX(id) AS id FROM ordenes").fetchone()["id"]
        finally:
            conexion.close()


if __name__ == "__main__":
    unittest.main()
