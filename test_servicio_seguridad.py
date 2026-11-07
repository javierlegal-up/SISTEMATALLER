import sqlite3
import unittest
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash

import servicio_seguridad as seguridad


class ServicioSeguridadTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE intentos_login (
                bucket TEXT PRIMARY KEY,
                fallos INTEGER NOT NULL,
                ventana_desde TEXT NOT NULL,
                bloqueado_hasta TEXT
            )"""
        )

    def tearDown(self):
        self.db.close()

    def test_matriz_de_permisos_separa_dueno_y_empleado(self):
        dueno = {"rol": "dueno"}
        empleado = {"rol": "empleado"}

        self.assertTrue(seguridad.tiene_permiso("ver_finanzas", dueno))
        self.assertFalse(seguridad.tiene_permiso("ver_finanzas", empleado))
        self.assertTrue(seguridad.tiene_permiso("crear_orden", empleado))
        self.assertFalse(seguridad.tiene_permiso("accion_desconocida", dueno))
        self.assertFalse(seguridad.tiene_permiso("crear_orden", None))

    def test_validacion_de_usuario_conserva_reglas_y_mensajes(self):
        datos, clave, errores = seguridad.leer_datos_usuario({
            "nombre": " María ",
            "usuario": "maria.1",
            "rol": "empleado",
            "clave": "clave-segura-2026",
            "clave2": "clave-segura-2026",
        })

        self.assertEqual(datos, {"nombre": "María", "usuario": "maria.1", "rol": "empleado"})
        self.assertEqual(clave, "clave-segura-2026")
        self.assertEqual(errores, [])

        _datos, _clave, errores = seguridad.leer_datos_usuario({
            "nombre": "A",
            "usuario": "x",
            "rol": "root",
            "clave": "corta",
            "clave2": "otra",
        })
        self.assertEqual(len(errores), 3)

    def test_cambio_de_clave_no_exige_nombre_ni_usuario(self):
        datos, clave, errores = seguridad.leer_datos_usuario({
            "clave": "clave-nueva-2026",
            "clave2": "clave-nueva-2026",
        }, pedir_usuario=False)

        self.assertEqual(datos, {"nombre": "", "usuario": "", "rol": "empleado"})
        self.assertEqual(clave, "clave-nueva-2026")
        self.assertEqual(errores, [])

    def test_bloquea_por_usuario_y_reinicia_ventana_vencida(self):
        ahora = datetime(2026, 11, 7, 12, 0, 0)
        buckets = seguridad.buckets_login(" Ricardo ", "127.0.0.1")

        for intento in range(seguridad.MAXIMO_INTENTOS_LOGIN):
            bloqueado = seguridad.registrar_fallo_login(self.db, buckets, ahora)
            self.assertEqual(bloqueado, intento + 1 >= seguridad.MAXIMO_INTENTOS_LOGIN)
        self.assertTrue(seguridad.login_bloqueado(self.db, buckets, ahora))

        reinicio = ahora + seguridad.VENTANA_INTENTOS_LOGIN + timedelta(seconds=1)
        self.assertFalse(seguridad.login_bloqueado(self.db, buckets, reinicio))
        self.assertFalse(seguridad.registrar_fallo_login(self.db, buckets, reinicio))

    def test_crea_usuario_guardando_hash_y_no_clave_plana(self):
        self.db.execute(
            """CREATE TABLE usuarios (
                usuario TEXT, nombre TEXT, clave_hash TEXT, rol TEXT, clave_cambiada_en TEXT
            )"""
        )

        usuario_id = seguridad.crear_usuario(
            self.db, "dueno", "Dueño", "clave-segura-2026", "dueno"
        )
        fila = self.db.execute("SELECT * FROM usuarios WHERE rowid = ?", (usuario_id,)).fetchone()

        self.assertTrue(check_password_hash(fila["clave_hash"], "clave-segura-2026"))
        self.assertNotEqual(fila["clave_hash"], "clave-segura-2026")


if __name__ == "__main__":
    unittest.main()
