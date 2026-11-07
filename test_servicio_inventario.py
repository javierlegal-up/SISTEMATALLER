import sqlite3
import unittest

import servicio_inventario as inventario


class ServicioInventarioTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE repuestos_servicios (
                id INTEGER PRIMARY KEY,
                nombre TEXT NOT NULL DEFAULT 'Filtro',
                sku TEXT NOT NULL DEFAULT 'FIL-1',
                unidad TEXT NOT NULL DEFAULT 'unidad',
                costo_centavos INTEGER
            );
            CREATE TABLE stock_movimientos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                articulo_id INTEGER NOT NULL,
                tipo TEXT NOT NULL,
                cantidad_milesimas INTEGER NOT NULL,
                costo_unitario_centavos INTEGER,
                recepcion_item_id INTEGER,
                orden_item_id INTEGER,
                revierte_movimiento_id INTEGER,
                motivo TEXT NOT NULL DEFAULT '',
                usuario_id INTEGER,
                creado_en TEXT NOT NULL
            );
            CREATE TABLE compras (id INTEGER PRIMARY KEY, estado TEXT NOT NULL);
            CREATE TABLE compra_items (
                id INTEGER PRIMARY KEY,
                compra_id INTEGER NOT NULL,
                articulo_id INTEGER NOT NULL,
                cantidad_milesimas INTEGER NOT NULL,
                costo_unitario_centavos INTEGER NOT NULL
            );
            CREATE TABLE compra_recepcion_items (
                id INTEGER PRIMARY KEY,
                compra_item_id INTEGER NOT NULL,
                cantidad_milesimas INTEGER NOT NULL
            );
            CREATE TABLE proveedor_pagos (
                id INTEGER PRIMARY KEY,
                compra_id INTEGER NOT NULL,
                tipo TEXT NOT NULL,
                importe_centavos INTEGER NOT NULL
            );
            """
        )

    def tearDown(self):
        self.db.close()

    def test_redondea_cada_renglon_de_compra_a_centavos(self):
        self.assertEqual(inventario.importe_linea_compra_centavos(1500, 250), 375)
        self.assertEqual(inventario.importe_linea_compra_centavos(1, 500), 1)

    def test_stock_y_costo_promedio_ponderado_con_redondeo_comercial(self):
        self.db.execute("INSERT INTO repuestos_servicios (id, costo_centavos) VALUES (1, 999)")
        self.db.executemany(
            """INSERT INTO stock_movimientos
               (articulo_id, tipo, cantidad_milesimas, costo_unitario_centavos, creado_en)
               VALUES (1, 'recepcion', ?, ?, '2026-11-07T10:00:00')""",
            ((1000, 100), (2000, 200)),
        )

        self.assertEqual(inventario.stock_articulo(self.db, 1), 3000)
        self.assertEqual(inventario.costo_promedio_articulo(self.db, 1), 167)

    def test_costo_promedio_sin_stock_usa_costo_del_catalogo(self):
        self.db.execute("INSERT INTO repuestos_servicios (id, costo_centavos) VALUES (1, 4321)")

        self.assertEqual(inventario.costo_promedio_articulo(self.db, 1), 4321)
        self.assertEqual(inventario.costo_promedio_articulo(self.db, 2), 0)

    def test_movimiento_de_stock_guarda_usuario_motivo_y_referencia(self):
        movimiento_id = inventario.insertar_movimiento_stock(
            self.db, 1, "ajuste", 2500, 125, "Conteo físico", usuario_id=7,
            creado_en="2026-11-07T12:00:00",
        )
        fila = self.db.execute("SELECT * FROM stock_movimientos WHERE id = ?", (movimiento_id,)).fetchone()

        self.assertEqual(fila["usuario_id"], 7)
        self.assertEqual(fila["motivo"], "Conteo físico")
        self.assertEqual(fila["cantidad_milesimas"], 2500)
        with self.assertRaisesRegex(ValueError, "debe cambiar"):
            inventario.insertar_movimiento_stock(self.db, 1, "ajuste", 0)

    def test_recepcion_neta_calcula_estado_y_pendiente_de_compra(self):
        self.db.execute("INSERT INTO repuestos_servicios (id) VALUES (1)")
        self.db.execute("INSERT INTO compras VALUES (1, 'Borrador')")
        self.db.execute("INSERT INTO compra_items VALUES (11, 1, 1, 10000, 250)")
        self.db.execute("INSERT INTO compra_recepcion_items VALUES (21, 11, 4000)")
        movimiento = inventario.insertar_movimiento_stock(
            self.db, 1, "recepcion", 4000, 250, "Recepción parcial", recepcion_item_id=21,
            creado_en="2026-11-07T12:00:00",
        )

        self.assertEqual(inventario.cantidades_recibidas(self.db, 11), 4000)
        self.assertEqual(inventario.actualizar_estado_compra(self.db, 1), "Parcial")
        linea = inventario.articulos_de_compra(self.db, 1)[0]
        self.assertEqual(linea["recibida_milesimas"], 4000)
        self.assertEqual(linea["pendiente_milesimas"], 6000)

        self.db.execute("INSERT INTO stock_movimientos VALUES (?, 1, 'reversion', -1000, 250, NULL, NULL, ?, 'Corrección', 7, '2026-11-07T13:00:00')", (movimiento + 1, movimiento))
        self.assertEqual(inventario.cantidades_recibidas(self.db, 11), 3000)
        self.assertEqual(inventario.articulos_de_compra(self.db, 1)[0]["pendiente_milesimas"], 7000)

    def test_importes_pedidos_y_saldo_de_proveedor_conciliados(self):
        self.db.execute("INSERT INTO compras VALUES (1, 'Parcial')")
        self.db.executemany(
            "INSERT INTO compra_items VALUES (?, 1, 1, ?, ?)",
            ((11, 1500, 250), (12, 1000, 500)),
        )
        self.db.execute("INSERT INTO compra_recepcion_items VALUES (21, 11, 1500)")
        self.db.execute("INSERT INTO stock_movimientos (articulo_id, tipo, cantidad_milesimas, costo_unitario_centavos, recepcion_item_id, motivo, creado_en) VALUES (1, 'recepcion', 1500, 250, 21, '', '2026-11-07T12:00:00')")
        self.db.execute("INSERT INTO proveedor_pagos VALUES (1, 1, 'pago', 100)")

        self.assertEqual(inventario.importes_pedidos_compra(self.db, [1, 1, 999]), {1: 875, 999: 0})
        self.assertEqual(inventario.cuenta_compra(self.db, 1), {"total": 375, "pagado": 100, "saldo": 275})


if __name__ == "__main__":
    unittest.main()
