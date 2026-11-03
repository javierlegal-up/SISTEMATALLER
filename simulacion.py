"""Modo simulación: el sistema completo con datos inventados y comentarios en cada pantalla.

Cómo funciona:
- Se abre con simulacion.bat, en http://127.0.0.1:5001.
- Usa su propia base de datos, en la carpeta simulacion_datos. Nunca toca taller.db.
- Cada vez que se inicia, borra esa carpeta y vuelve a cargar los datos de ejemplo,
  así que puedes probar, cambiar y borrar lo que quieras.
- Las fechas se calculan a partir de hoy, para que las listas y el gráfico siempre
  muestren órdenes recientes, atrasadas y de los últimos seis meses.
"""
import os
import shutil
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

CARPETA_DATOS = Path(__file__).resolve().parent / "simulacion_datos"
PUERTO = 5001

# (nombre, documento, teléfono, email, dirección, [(patente, marca, modelo, año, color)])
CLIENTES = [
    ("María González", "20.111.222", "555-0101", "maria@ejemplo.test", "Calle Los Álamos 123",
     [("AB123CD", "Toyota", "Corolla", 2019, "Gris"), ("AE456FG", "Renault", "Kangoo", 2016, "Blanco")]),
    ("Carlos Rodríguez", "23.333.444", "555-0102", "carlos@ejemplo.test", "Av. Central 456",
     [("AC789HJ", "Ford", "Ranger", 2021, "Negro")]),
    ("Lucía Fernández", "27.555.666", "555-0103", "", "Pasaje Norte 78",
     [("AD234KL", "Volkswagen", "Gol", 2015, "Rojo")]),
    ("Transportes del Sur", "30.777.888", "555-0104", "flota@ejemplo.test", "Ruta 9, km 12",
     [("AF567MN", "Fiat", "Ducato", 2020, "Blanco"), ("AF568MP", "Fiat", "Cronos", 2022, "Azul")]),
    ("Jorge Martínez", "", "555-0105", "", "",
     [("KJL123", "Chevrolet", "Onix", 2018, "Plata")]),
    ("Ana Torres", "31.999.000", "555-0106", "ana@ejemplo.test", "Calle del Sol 910",
     [("AG890QR", "Peugeot", "208", 2023, "Azul")]),
]

# (tipo, nombre, precio en pesos, costo para el taller en pesos o None si no se cargó)
CATALOGO = [
    ("Repuesto", "Aceite 5W30 (litro)", 9500, 6000),
    ("Repuesto", "Filtro de aceite", 12000, 7000),
    ("Repuesto", "Filtro de aire", 15000, 9000),
    ("Repuesto", "Pastillas de freno (juego)", 48000, 30000),
    ("Repuesto", "Batería 12V", 165000, 110000),
    ("Repuesto", "Bujía", 8500, 5000),
    ("Repuesto", "Líquido refrigerante (litro)", 6000, None),
    ("Servicio", "Cambio de aceite y filtro", 25000, None),
    ("Servicio", "Cambio de pastillas", 35000, None),
    ("Servicio", "Diagnóstico por escáner", 20000, None),
    ("Servicio", "Alineación y balanceo", 40000, None),
    ("Servicio", "Afinación de motor", 60000, None),
]

SERVICE = [("Aceite 5W30 (litro)", 4), ("Filtro de aceite", 1), ("Cambio de aceite y filtro", 1)]
FRENOS = [("Pastillas de freno (juego)", 1), ("Cambio de pastillas", 1)]
BATERIA = [("Batería 12V", 1), ("Diagnóstico por escáner", 1)]
AFINACION = [("Bujía", 4), ("Filtro de aire", 1), ("Afinación de motor", 1)]

# Órdenes abiertas: (patente, estado, días desde el ingreso, km, fallas, diagnóstico, líneas)
ABIERTAS = [
    ("AD234KL", "Pendiente", 0, 142300, "Hace ruido al frenar.", "", []),
    ("KJL123", "Pendiente", 2, 88150, "Se enciende la luz de motor.", "", [("Diagnóstico por escáner", 1)]),
    ("AE456FG", "Pendiente", 12, 201400, "Pierde líquido refrigerante.\nEl cliente espera presupuesto.", "", []),
    ("AC789HJ", "En Proceso", 1, 45200, "Servicio de los 45.000 km.", "Se realiza el cambio de aceite y filtro.", SERVICE),
    ("AF567MN", "En Proceso", 3, 120900, "Vibra el volante en ruta.", "Falta alinear y balancear.", [("Alineación y balanceo", 1)]),
    ("AB123CD", "En Proceso", 9, 67800, "Frena mal y hace ruido.", "Pastillas gastadas. Se espera el repuesto.", FRENOS),
    # Anotada por un empleado: sus líneas (con tipo y sin precio) esperan que el dueño las cotice.
    ("AE456FG", "En Proceso", 0, 201550, "Pinchadura en la rueda trasera.", "Se cambia la cámara.",
     [("Cámara rodado 14", 1, "Repuesto"), ("Cambio de cámara", 1, "Servicio")]),
]

# Usuarios de prueba: (usuario, nombre, contraseña, rol). Se muestran en la pantalla de ingreso.
USUARIOS = [
    ("roberto", "Roberto (dueño)", "1234", "dueno"),
    ("juan", "Juan (turno mañana)", "1234", "empleado"),
    ("pedro", "Pedro (turno tarde)", "1234", "empleado"),
]

# Órdenes completadas: (patente, meses atrás, día de finalización, días en el taller, km, fallas, diagnóstico, líneas)
COMPLETADAS = [
    ("AG890QR", 0, 1, 1, 15200, "Servicio programado.", "Cambio de aceite y filtro.", SERVICE),
    ("AF568MP", 1, 6, 2, 30100, "No arranca por las mañanas.", "Batería agotada; se reemplaza.", BATERIA),
    ("AB123CD", 1, 22, 1, 65000, "Servicio programado.", "Cambio de aceite y filtro.", SERVICE),
    ("AC789HJ", 2, 9, 3, 40000, "Tironea al acelerar.", "Bujías y filtro de aire gastados.", AFINACION),
    ("AD234KL", 2, 25, 1, 139000, "Ruido en las ruedas delanteras.", "Pastillas en el límite; se cambian.", FRENOS),
    ("AF567MN", 3, 12, 2, 110500, "Servicio de flota.", "Cambio de aceite y filtro.", SERVICE),
    ("KJL123", 3, 27, 1, 82000, "Pérdida de potencia.", "Afinación completa.", AFINACION),
    ("AE456FG", 4, 8, 4, 195000, "No arranca.", "Batería dañada; se reemplaza.", BATERIA),
    ("AF568MP", 4, 19, 1, 25000, "Servicio programado.", "Cambio de aceite y filtro.", SERVICE),
    ("AB123CD", 5, 14, 2, 60000, "Frenos largos.", "Se cambian las pastillas.", FRENOS),
]

# Comentario que aparece en cada pantalla, según la ruta de app.py que la muestra.
GUIA = {
    "login": {
        "titulo": "Ingresa con un usuario de prueba (contraseña 1234 para todos)",
        "pasos": [
            "roberto: el dueño. Ve el panel, los precios, las ganancias y quién hizo qué.",
            "juan (turno mañana) o pedro (turno tarde): empleados. Registran autos y trabajos, sin ver números.",
            "Prueba los dos: entra como juan, anota un trabajo, cierra sesión (abajo a la izquierda) y entra como roberto.",
        ],
    },
    "usuarios": {
        "titulo": "Usuarios: uno por persona",
        "pasos": [
            "Crea un usuario para cada empleado. Así cada cambio queda registrado con su nombre.",
            "Si alguien olvida su contraseña, ponle una nueva aquí.",
            "Si alguien deja de trabajar, desactívalo: ya no puede entrar, pero su historial se conserva.",
        ],
    },
    "actividad": {
        "titulo": "Actividad: el trabajo de cada persona en un período",
        "pasos": [
            "Arriba están los totales del período: órdenes creadas, órdenes gestionadas, cambios de estado y clientes registrados.",
            "Rendimiento por persona compara a todo el equipo. Haz clic en Juan para ver solo su trabajo.",
            "Cambia el período con Desde y Hasta, o con Este mes y Mes anterior. Prueba con Mes anterior.",
            "Solo cuenta el trabajo del taller. Inicio y cierre de sesión no se registran.",
        ],
    },
    "inicio": {
        "titulo": "Este es el panel: el resumen del taller",
        "pasos": [
            "El menú de la izquierda lleva a cada sección. Los números indican cuántas órdenes hay en cada estado.",
            "Falta poner precio: órdenes que anotó un empleado y esperan que les pongas los precios. Hay una de ejemplo: un cambio de cámara.",
            "Haz clic en Órdenes abiertas o Atrasadas para ver esa lista.",
            "Pasa el cursor por el gráfico para ver el desglose de cada mes.",
            "En Requieren atención aparecen las órdenes que llevan más días esperando. Haz clic en una para abrirla.",
        ],
    },
    "clientes": {
        "titulo": "Lista de clientes",
        "pasos": [
            "Busca por nombre, teléfono, documento o patente. Prueba con KJL123.",
            "Haz clic en un nombre para ver sus vehículos y todas sus órdenes.",
        ],
    },
    "cliente_nuevo": {
        "titulo": "Registro de un cliente con sus vehículos",
        "pasos": [
            "Completa el nombre y al menos un vehículo. Con + Agregar vehículo puedes sumar varios a la vez.",
            "La patente se guarda sin espacios ni guiones y en mayúsculas: ab-123 cd queda como AB123CD.",
            "Prueba a escribir la patente AB123CD: el sistema la rechaza porque ya existe.",
        ],
    },
    "cliente_detalle": {
        "titulo": "Ficha del cliente",
        "pasos": [
            "Aquí ves sus datos, sus vehículos y el historial de órdenes.",
            "Nueva orden crea una orden con ese cliente y vehículo ya seleccionados.",
            "Prueba a eliminar un vehículo con órdenes: el sistema no te deja, para no perder el historial.",
        ],
    },
    "cliente_editar": {
        "titulo": "Editar los datos del cliente",
        "pasos": ["Los cambios se reflejan también en sus órdenes y comprobantes, incluso en los ya emitidos."],
    },
    "vehiculo_editar": {
        "titulo": "Editar un vehículo",
        "pasos": ["Sirve para corregir datos. La patente sigue sin poder repetirse."],
    },
    "ordenes": {
        "titulo": "Lista de órdenes de trabajo",
        "pasos": [
            "Usa Pendientes, En proceso y Completadas en el menú para ver cada sección por separado.",
            "Los botones de la columna Mover a cambian el estado al instante: la orden pasa a su nueva sección y se actualizan los números del menú.",
            "Haz clic en una fila para ver su resumen sin salir de la lista.",
            "Busca por cliente, patente, marca o modelo. Prueba con AB123.",
            "En las secciones abiertas, las que más tiempo llevan esperando salen primero. Si hay muchas, se reparten en páginas.",
        ],
    },
    "orden_nueva": {
        "titulo": "Crear una orden de trabajo",
        "pasos": [
            "Elige un cliente: la lista de vehículos muestra solo los suyos.",
            "Anota las fallas reportadas y el estado.",
            "Repuestos y mano de obra van en tablas separadas, cada una con su botón para agregar.",
            "Pulsa + Agregar repuesto y escribe Filtro: al elegirlo de la lista se completan solos lo que cobras y lo que te costó.",
            "Si el cliente trae el repuesto, marca Lo trae el cliente: no se le cobra, y en el comprobante figura como provisto por el cliente.",
            "Te costó es lo que pagaste al proveedor: es opcional y nunca aparece en el comprobante. Abajo ves el total a cobrar y la ganancia estimada.",
        ],
    },
    "orden_editar": {
        "titulo": "Editar una orden",
        "pasos": ["Puedes agregar repuestos o mano de obra a medida que avanza la reparación. Los precios se guardan en la orden, así que cambiar el catálogo no la altera."],
    },
    "orden_detalle": {
        "titulo": "Detalle de la orden",
        "pasos": [
            "Pulsa Imprimir / PDF para abrir el comprobante del cliente. Después, en la ventana de impresión, elige Guardar como PDF.",
            "Cambia el estado aquí mismo. Al pasarla a Completado se registra la fecha de finalización.",
        ],
    },
    "vehiculos": {
        "titulo": "Lista de vehículos",
        "pasos": [
            "Muestra cada auto con su cliente, sus órdenes, su última visita y el total de trabajos completados.",
            "Haz clic en una patente para ver todo lo que se le hizo. Prueba con AB123CD, que tiene varias visitas.",
        ],
    },
    "vehiculo_detalle": {
        "titulo": "Historial del vehículo: lo que se le hizo a este auto",
        "pasos": [
            "Arriba ves cuánto suman sus trabajos completados, separados en repuestos y mano de obra.",
            "Ganancia estimada es lo cobrado menos lo que te costaron los repuestos. Si falta algún costo, te avisa.",
            "La tabla lista cada repuesto y servicio cargado en sus órdenes, de lo más reciente a lo más antiguo.",
            "Se llena solo: cada línea que agregas a una orden de este auto aparece aquí.",
            "Lo que está en órdenes sin terminar aparece en la tabla, pero no se suma al total hasta completarlas.",
        ],
    },
    "catalogo": {
        "titulo": "Lista de precios: es general, no de un auto",
        "pasos": [
            "Guarda lo que usas con frecuencia para no escribirlo en cada orden. Nada de esta lista se le suma a un auto hasta que lo agregas a su orden.",
            "Tu costo es lo que pagas al proveedor. Es opcional, se copia a la orden y no aparece en el comprobante del cliente.",
            "Cambia un precio y crea una orden nueva: usa el precio nuevo, y las órdenes anteriores conservan el suyo.",
        ],
    },
}


def fecha_en_mes(hoy, meses_atras, dia):
    """Fecha en el mes indicado. Nunca devuelve una fecha futura."""
    anio, mes = divmod(hoy.year * 12 + hoy.month - 1 - meses_atras, 12)
    return min(date(anio, mes + 1, min(dia, 28)), hoy)


def cargar_datos(conexion, hoy=None):
    """Carga los datos de ejemplo en una base con el esquema ya creado."""
    hoy = hoy or date.today()
    with conexion:
        ids_usuario = {
            usuario: conexion.execute(
                "INSERT INTO usuarios (usuario, nombre, clave_hash, rol) VALUES (?, ?, ?, ?)",
                (usuario, nombre, generate_password_hash(clave), rol),
            ).lastrowid
            for usuario, nombre, clave, rol in USUARIOS
        }

        def anotar(usuario, momento, accion, detalle, orden_id=None):
            conexion.execute(
                "INSERT INTO actividad (usuario_id, momento, accion, detalle, orden_id) VALUES (?, ?, ?, ?, ?)",
                (ids_usuario[usuario], momento.isoformat(timespec="seconds"), accion, detalle, orden_id),
            )

        ids_vehiculo = {}
        for nombre, documento, telefono, email, direccion, vehiculos in CLIENTES:
            cliente_id = conexion.execute(
                "INSERT INTO clientes (nombre, documento, telefono, email, direccion) VALUES (?, ?, ?, ?, ?)",
                (nombre, documento, telefono, email, direccion),
            ).lastrowid
            for patente, marca, modelo, anio, color in vehiculos:
                ids_vehiculo[patente] = conexion.execute(
                    """INSERT INTO vehiculos (cliente_id, patente, marca, modelo, anio, color)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (cliente_id, patente, marca, modelo, anio, color),
                ).lastrowid

        precios = {}
        for tipo, nombre, pesos, costo_pesos in CATALOGO:
            costo = costo_pesos * 100 if costo_pesos is not None else None
            precios[nombre] = (tipo, pesos * 100, costo)
            conexion.execute(
                """INSERT INTO repuestos_servicios (tipo, nombre, precio_centavos, costo_centavos)
                   VALUES (?, ?, ?, ?)""",
                (tipo, nombre, pesos * 100, costo),
            )

        ordenes = []
        for patente, estado, dias, km, fallas, diagnostico, lineas in ABIERTAS:
            ingreso = hoy - timedelta(days=dias)
            ordenes.append((ingreso, patente, estado, None, km, fallas, diagnostico, lineas))
        for patente, meses_atras, dia, dias_taller, km, fallas, diagnostico, lineas in COMPLETADAS:
            fin = fecha_en_mes(hoy, meses_atras, dia)
            ingreso = fin - timedelta(days=dias_taller)
            ordenes.append((ingreso, patente, "Completado", fin, km, fallas, diagnostico, lineas))

        # Por fecha de ingreso, para que los números de orden sigan un orden lógico.
        for numero, (ingreso, patente, estado, fin, km, fallas, diagnostico, lineas) in enumerate(
            sorted(ordenes, key=lambda orden: orden[0])
        ):
            orden_id = conexion.execute(
                """INSERT INTO ordenes (vehiculo_id, fallas, diagnostico, estado, kilometraje,
                                        fecha_ingreso, fecha_completado)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ids_vehiculo[patente],
                    fallas,
                    diagnostico,
                    estado,
                    km,
                    ingreso.isoformat(),
                    fin.isoformat() if fin else None,
                ),
            ).lastrowid
            for linea in lineas:
                if len(linea) == 3:
                    # (descripción, cantidad, tipo): anotada por un empleado, todavía sin precio.
                    nombre, cantidad, tipo = linea
                    precio, costo, falta_precio = 0, None, 1
                else:
                    nombre, cantidad = linea
                    tipo, precio, costo = precios[nombre]
                    falta_precio = 0
                conexion.execute(
                    """INSERT INTO orden_items (orden_id, tipo, descripcion, cantidad,
                                                precio_unitario_centavos, subtotal_centavos,
                                                costo_unitario_centavos, costo_total_centavos,
                                                falta_precio)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        orden_id,
                        tipo,
                        nombre,
                        cantidad,
                        precio,
                        cantidad * precio,
                        costo,
                        cantidad * costo if costo is not None else None,
                        falta_precio,
                    ),
                )

            # Actividad de ejemplo: los empleados crean las órdenes (uno de mañana, otro de tarde)
            # y el dueño las completa.
            detalle = f"OT-{orden_id:05d} · {patente}"
            if numero % 2 == 0:
                anotar("juan", datetime.combine(ingreso, time(9, 30)), "Creó la orden", detalle, orden_id)
            else:
                anotar("pedro", datetime.combine(ingreso, time(15, 30)), "Creó la orden", detalle, orden_id)
            if fin:
                anotar(
                    "roberto",
                    datetime.combine(fin, time(18, 0)),
                    "Cambió el estado",
                    f"{detalle} · En Proceso → Completado",
                    orden_id,
                )


def main():
    shutil.rmtree(CARPETA_DATOS, ignore_errors=True)
    base_datos = CARPETA_DATOS / "taller_simulacion.db"
    if base_datos.exists():
        # En Windows no se puede borrar un archivo que está en uso.
        print("La simulación ya está abierta en otra ventana. Ciérrala y vuelve a intentarlo.")
        sys.exit(1)
    CARPETA_DATOS.mkdir(exist_ok=True)

    # Tiene que definirse antes de importar app, que abre la base de datos al cargarse.
    os.environ["TALLER_DB"] = str(base_datos)
    os.environ["TALLER_RESPALDOS"] = str(CARPETA_DATOS / "respaldos")
    from app import app, conectar

    conexion = conectar()
    try:
        cargar_datos(conexion)
    finally:
        conexion.close()

    # La simulación nunca copia al pendrive ni a la nube: pisaría las copias reales.
    app.config.update(SIMULACION=True, GUIA=GUIA, BUSCAR_PENDRIVE=False, RESPALDOS_EXTRA=[])
    app.run(host="127.0.0.1", port=PUERTO)


if __name__ == "__main__":
    main()
