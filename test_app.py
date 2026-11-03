"""Prueba del recorrido completo. Ejecutar con:  python -m unittest -v

Usa una base de datos temporal: no toca taller.db.
"""
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

CARPETA_TEMPORAL = tempfile.mkdtemp()
os.environ["TALLER_DB"] = os.path.join(CARPETA_TEMPORAL, "prueba.db")
os.environ["TALLER_RESPALDOS"] = os.path.join(CARPETA_TEMPORAL, "respaldos")

from app import (  # noqa: E402
    ESQUEMA,
    MAXIMO_RESPALDOS,
    POR_PAGINA,
    app,
    conectar,
    crear_respaldo,
    crear_usuario,
    init_db,
    resumen_panel,
    ultimo_respaldo,
)
from simulacion import GUIA, cargar_datos  # noqa: E402

# Las pruebas nunca deben copiar al pendrive real de la compu donde se corren.
app.config.update(BUSCAR_PENDRIVE=False, RESPALDOS_EXTRA=[])

# Un dueño y un empleado para las pruebas.
_conexion = conectar()
with _conexion:
    DUENO_ID = crear_usuario(_conexion, "dueno", "Dueño de Prueba", "clave-dueno", "dueno")
    EMPLEADO_ID = crear_usuario(_conexion, "juan", "Juan Empleado", "clave-juan", "empleado")
_conexion.close()


def tearDownModule():
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
        self.cliente = app.test_client()
        self.entrar_como(DUENO_ID)  # Salvo que la prueba diga otra cosa, entra el dueño.

    def entrar_como(self, usuario_id):
        with self.cliente.session_transaction() as sesion:
            sesion["usuario_id"] = usuario_id

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
        for ruta in ("/", "/clientes", "/clientes/nuevo", "/ordenes", "/ordenes/nueva", "/catalogo"):
            with self.subTest(ruta=ruta):
                self.assertEqual(self.cliente.get(ruta).status_code, 200)

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
            with redirect_stdout(io.StringIO()):
                texto = self.cliente.get("/").get_data(as_text=True)
        finally:
            app.config["RESPALDOS"] = original
        self.assertIn("No se pudo crear la copia de seguridad", texto)

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

        self.assertEqual(self.cliente.post(url, data={"estado": "Inventado"}).status_code, 400)
        self.assertEqual(self.cliente.post("/api/ordenes/999999/estado", data={"estado": "Pendiente"}).status_code, 404)

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

    def test_15_datos_de_simulacion_cubren_todo_el_panel(self):
        # El día 31 y el día 1 son los casos difíciles: ningún mes debe quedar sin datos.
        for hoy in (date(2026, 8, 31), date(2026, 3, 1)):
            with self.subTest(hoy=hoy):
                conexion = sqlite3.connect(":memory:")
                conexion.executescript(ESQUEMA)
                cargar_datos(conexion, hoy)

                estados = {fila[0] for fila in conexion.execute("SELECT DISTINCT estado FROM ordenes")}
                self.assertEqual(estados, {"Pendiente", "En Proceso", "Completado"})
                meses = {
                    fila[0]
                    for fila in conexion.execute(
                        "SELECT DISTINCT substr(fecha_completado, 1, 7) FROM ordenes WHERE estado = 'Completado'"
                    )
                }
                self.assertEqual(len(meses), 6)
                futuras = conexion.execute(
                    "SELECT COUNT(*) FROM ordenes WHERE fecha_completado > ? OR fecha_ingreso > ?",
                    (hoy.isoformat(), hoy.isoformat()),
                ).fetchone()[0]
                self.assertEqual(futuras, 0)
                conexion.close()

    def test_16_comentarios_de_simulacion_aparecen_en_pantalla(self):
        for ruta in GUIA:
            with self.subTest(ruta=ruta):
                self.assertIn(ruta, app.view_functions)

        self.assertNotIn("Comentario:", self.cliente.get("/").get_data(as_text=True))
        app.config.update(SIMULACION=True, GUIA=GUIA)
        try:
            texto = self.cliente.get("/").get_data(as_text=True)
        finally:
            app.config.update(SIMULACION=False, GUIA={})
        self.assertIn("Modo simulación", texto)
        self.assertIn(GUIA["inicio"]["titulo"], texto)

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

        dueno = self.cliente.post("/login", data={"usuario": "dueno", "clave": "clave-dueno"})
        self.assertTrue(dueno.headers["Location"].endswith("/"))  # El dueño va al panel.

        self.cliente.post("/logout")
        empleado = self.cliente.post("/login", data={"usuario": "JUAN", "clave": "clave-juan"})
        self.assertIn("/ordenes", empleado.headers["Location"])  # El empleado, a las órdenes.

    def test_26_empleado_no_ve_numeros_ni_borra(self):
        self.cliente.post("/clientes/nuevo", data=datos_cliente("Sara Lux", "EMP111"))
        vehiculo_id = self.vehiculo_id("EMP111")
        self.crear_orden(vehiculo_id)  # La crea el dueño, con precios.
        orden_id = self.ultima_orden_id()

        self.entrar_como(EMPLEADO_ID)
        self.assertEqual(self.cliente.get("/").status_code, 302)  # Sin panel.
        for ruta in ("/catalogo", "/usuarios", "/actividad", f"/ordenes/{orden_id}/imprimir"):
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
                data={"nombre": "Roberto", "usuario": "roberto", "clave": "1234", "clave2": "4321"},
            ).get_data(as_text=True)
            self.assertIn("no coinciden", errores)
            creada = cliente.post(
                "/configuracion-inicial",
                data={"nombre": "Roberto", "usuario": "roberto", "clave": "1234", "clave2": "1234"},
            )
            self.assertTrue(creada.headers["Location"].endswith("/"))
            self.assertEqual(cliente.get("/").status_code, 200)  # Quedó como dueño con sesión iniciada.
            self.assertIn("/login", cliente.get("/configuracion-inicial").headers["Location"])
        finally:
            app.config["DATABASE"] = original

    def test_30_usuarios_se_desactivan_y_no_pueden_entrar(self):
        self.cliente.post(
            "/usuarios",
            data={"nombre": "Pedro Tarde", "usuario": "pedro", "rol": "empleado", "clave": "abcd", "clave2": "abcd"},
        )
        conexion = conectar()
        try:
            pedro_id = conexion.execute("SELECT id FROM usuarios WHERE usuario = 'pedro'").fetchone()["id"]
        finally:
            conexion.close()
        self.cliente.post(f"/usuarios/{pedro_id}/activo")
        otro = app.test_client()
        texto = otro.post("/login", data={"usuario": "pedro", "clave": "abcd"}).get_data(as_text=True)
        self.assertIn("Usuario o contraseña incorrectos", texto)
        # El dueño no puede desactivarse a sí mismo.
        self.assertIn(
            "No puedes desactivar tu propio usuario",
            self.cliente.post(f"/usuarios/{DUENO_ID}/activo", follow_redirects=True).get_data(as_text=True),
        )

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
