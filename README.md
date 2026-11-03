# Sistema para taller mecánico

Aplicación web local para gestionar clientes, vehículos, órdenes de trabajo y comprobantes imprimibles. Usa Python (Flask), SQLite y Tailwind CSS.

## Inicio rápido

Haz doble clic en `iniciar.bat`. La primera vez instala lo necesario (requiere Python e Internet); después solo inicia el sistema y abre el navegador. Para apagarlo, cierra la ventana negra.

## Usuarios

La primera vez que abres el sistema te pide crear la cuenta del **dueño**. Con ella, en **Usuarios**, creas un usuario para cada empleado.

| | Dueño | Empleado |
|---|---|---|
| Registrar clientes, vehículos y órdenes | ✅ | ✅ |
| Anotar repuestos y trabajos hechos | ✅ | ✅ (sin precios) |
| Pasar órdenes de Pendiente a En proceso | ✅ | ✅ |
| Poner precios y completar órdenes | ✅ | ❌ |
| Ver el panel, importes, costos y ganancias | ✅ | ❌ |
| Imprimir comprobantes, borrar, lista de precios | ✅ | ❌ |
| Usuarios, Actividad y copias de seguridad | ✅ | ❌ |

- **Líneas sin precio:** lo que anota un empleado queda marcado como **Falta poner precio**. El dueño lo ve en esa sección del menú y no puede completar la orden hasta ponerle precio a todo.
- **Registro de actividad:** cada acción queda anotada con quién la hizo y a qué hora. El turno (mañana o tarde) se calcula por la hora, y el cambio de turno se configura en `HORA_CAMBIO_TURNO`, en `app.py`.
- **Usuarios que se van:** no se borran, se desactivan. Así se conserva su historial.
- **Contraseñas olvidadas:** el dueño le pone una nueva a cualquier usuario en **Usuarios**. Anota la contraseña del dueño en un lugar seguro.

## Instalación manual en Windows

Necesitas Python 3.10 o superior. Compruébalo en PowerShell, dentro de esta carpeta:

```powershell
python --version
```

Si no lo tienes, instálalo desde [python.org](https://www.python.org/downloads/) y marca **Add python.exe to PATH**.

Crea el entorno virtual:

```powershell
python -m venv venv
```

Actívalo (hay que hacerlo cada vez que abras una terminal nueva):

```powershell
.\venv\Scripts\Activate.ps1
```

Si PowerShell bloquea el script, ejecuta esto una vez y vuelve a activarlo:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Instala las dependencias:

```powershell
pip install -r requirements.txt
```

## Uso

Con el entorno virtual activo:

```powershell
python app.py
```

Abre <http://127.0.0.1:5000> en el navegador. Para detener el sistema, pulsa `Ctrl+C` en la terminal.

La base de datos `taller.db` se crea sola la primera vez.

## Flujo de trabajo

1. **Datos del taller:** edita `TALLER` al inicio de `app.py`. Es lo que aparece en el comprobante. Reinicia la aplicación para ver los cambios.
2. **Lista de precios (opcional):** carga lo que usas con frecuencia. Es una lista general, no de un auto; al crear una orden, se autocompleta el tipo y el precio.
3. **Clientes:** registra el cliente con uno o varios vehículos.
4. **Órdenes:** selecciona cliente y vehículo, anota las fallas, el estado, los repuestos y la mano de obra. Los totales se calculan solos.
5. **Historial:** en *Vehículos*, abre una patente para ver todo lo que se le hizo a ese auto y cuánto suma.
   Si cargas *Tu costo* en los repuestos (es opcional), también ves la ganancia estimada. Ese costo es interno y nunca aparece en el comprobante.
6. **Comprobante:** en la orden, pulsa **Imprimir / PDF** y luego **Imprimir / Guardar PDF**. Para obtener un PDF, elige *Guardar como PDF* (o *Microsoft Print to PDF*) como impresora.

## Comprobar que todo funciona

```powershell
python -m unittest -v
```

Las pruebas usan una base de datos temporal y no tocan tus datos.

## Copias de seguridad

Todos los datos están en `taller.db`. El sistema hace copias solo:

- **En la compu:** una copia por día en la carpeta `respaldos`, y se conservan las últimas 30. La copia de hoy se actualiza al abrir el sistema y, mientras se trabaja, como mucho cada 15 minutos.
- **En el pendrive:** crea una carpeta llamada `RespaldoTaller` en la raíz del pendrive, es decir, al abrirlo, no dentro de otra carpeta. Mientras el pendrive esté conectado, el sistema le copia los respaldos solo. La letra de la unidad da igual.
- **En la nube (opcional):** agrega una carpeta de Google Drive u OneDrive en `CARPETAS_EXTRA`, al inicio de `app.py`.

El panel muestra la fecha de la última copia en la compu y fuera de ella. Tiene un botón **Hacer copia ahora**, y avisa si pasan más de 3 días sin una copia fuera de la compu.

**Hábito recomendado:** al terminar el día, conecta el pendrive, pulsa **Hacer copia ahora**, cierra el sistema y guarda el pendrive lejos de la compu.

### Restaurar una copia

1. Cierra el sistema.
2. Renombra `taller.db` a `taller_danado.db`. No lo borres todavía.
3. Copia el archivo de la fecha que quieras, por ejemplo `respaldos\taller_2026-11-02.db`, a la carpeta del sistema.
4. Renombra esa copia a `taller.db` y vuelve a abrir el sistema.

Lo registrado después de la fecha de la copia no estará en la base restaurada.

## Limitaciones

- **Solo en esta computadora:** tiene usuarios y contraseñas, pero está pensado para una sola compu. Solo acepta conexiones desde ella misma; no lo publiques en Internet ni lo abras a la red.
- **Requiere Internet para los estilos:** Tailwind se carga desde su CDN. Sin conexión, el sistema funciona y guarda datos, pero se ve sin diseño, incluso el comprobante.
- **Sin control de stock:** el catálogo guarda nombres y precios, no existencias.
- **Comprobante no fiscal:** es un recibo interno. No reemplaza una factura legal.

## Estructura

```text
app.py             Rutas, validaciones y esquema de la base de datos
requirements.txt   Dependencias de Python
test_app.py        Pruebas automáticas
templates/         Pantallas HTML
iniciar.bat        Instala lo necesario e inicia el sistema con doble clic
taller.db          Base de datos (se crea al iniciar)
respaldos/         Copias de seguridad diarias (se crea al iniciar)
.secret_key        Clave de sesión (se crea al iniciar; no la compartas)
```
