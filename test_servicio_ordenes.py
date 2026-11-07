import unittest
from datetime import date

import servicio_ordenes as ordenes


class ServicioOrdenesTests(unittest.TestCase):
    def test_precio_de_empleado_respeta_linea_existente_y_repuesto_del_cliente(self):
        guardada = {
            "del_cliente": 0,
            "precio_unitario_centavos": 12500,
            "costo_unitario_centavos": 7000,
            "falta_precio": 0,
        }

        self.assertEqual(ordenes.precio_para_empleado(None, False), (0, None, True))
        self.assertEqual(ordenes.precio_para_empleado(guardada, False), (12500, 7000, False))
        self.assertEqual(ordenes.precio_para_empleado(guardada, True), (0, None, False))

    def test_fecha_completado_se_conserva_al_editar_y_se_limpia_al_reabrir(self):
        hoy = date(2026, 11, 7)

        self.assertEqual(ordenes.fecha_completado_para("Completado", hoy=hoy), "2026-11-07")
        self.assertEqual(
            ordenes.fecha_completado_para("Completado", "2026-11-01", hoy=hoy),
            "2026-11-01",
        )
        self.assertIsNone(ordenes.fecha_completado_para("En Proceso", "2026-11-01", hoy=hoy))

    def test_transicion_de_estado_respeta_anulacion_rol_y_precios(self):
        base = {"id": 12, "estado": "En Proceso", "lineas_sin_precio": 0}

        self.assertEqual(
            ordenes.problema_cambio_estado(
                {**base, "anulada": True}, "Completado", ("Pendiente", "En Proceso", "Completado"),
                True, lambda _id: "OT-00012",
            ),
            "No se puede cambiar el estado de una orden anulada.",
        )
        self.assertEqual(
            ordenes.problema_cambio_estado(
                base, "Invalido", ("Pendiente", "En Proceso", "Completado"), True,
                lambda _id: "OT-00012",
            ),
            "Estado no válido.",
        )
        self.assertEqual(
            ordenes.problema_cambio_estado(
                base, "Completado", ("Pendiente", "En Proceso", "Completado"), False,
                lambda _id: "OT-00012",
            ),
            "Solo el dueño puede completar una orden o reabrir una completada.",
        )
        self.assertEqual(
            ordenes.problema_cambio_estado(
                {**base, "lineas_sin_precio": 2}, "Completado",
                ("Pendiente", "En Proceso", "Completado"), True,
                lambda _id: "OT-00012",
            ),
            "Antes de completar OT-00012 falta poner precios: edita la orden.",
        )
        self.assertIsNone(ordenes.problema_cambio_estado(
            base, "Completado", ("Pendiente", "En Proceso", "Completado"), True,
            lambda _id: "OT-00012",
        ))

    def test_resumen_de_costos_ignora_costo_desconocido_en_total_y_cuenta_repuestos(self):
        items = [
            {"costo_total_centavos": 7000, "tipo": "Repuesto", "subtotal_centavos": 10000},
            {"costo_total_centavos": None, "tipo": "Repuesto", "subtotal_centavos": 8000},
            {"costo_total_centavos": None, "tipo": "Repuesto", "subtotal_centavos": 0},
            {"costo_total_centavos": None, "tipo": "Servicio", "subtotal_centavos": 5000},
        ]

        self.assertEqual(ordenes.resumir_costos(items), {"costo": 7000, "sin_costo": 1})


if __name__ == "__main__":
    unittest.main()
