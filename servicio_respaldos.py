"""Operaciones de respaldo y validación de bases del ERP del taller."""

from __future__ import annotations

import os
import shutil
import sqlite3
import string
from datetime import date, datetime
from pathlib import Path


class ServicioRespaldos:
    """Gestiona copias locales/externas con configuración de la aplicación."""

    def __init__(
        self,
        configuracion,
        logger,
        exportador_excel,
        maximo_respaldos,
        patron_respaldo,
        patron_excel,
        carpeta_pendrive,
    ):
        self.configuracion = configuracion
        self.logger = logger
        self.exportador_excel = exportador_excel
        self.maximo_respaldos = maximo_respaldos
        self.patron_respaldo = patron_respaldo
        self.patron_excel = patron_excel
        self.carpeta_pendrive = carpeta_pendrive

    def crear_respaldo(self, actualizar=False):
        """Crea y valida la copia diaria antes de reemplazar la anterior."""
        carpeta = Path(self.configuracion["RESPALDOS"])
        carpeta.mkdir(parents=True, exist_ok=True)
        destino = carpeta / f"taller_{date.today().isoformat()}.db"
        if destino.exists() and not actualizar:
            return None

        temporal = destino.with_suffix(".tmp")
        origen = sqlite3.connect(self.configuracion["DATABASE"])
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

        for antiguo in sorted(carpeta.glob(self.patron_respaldo))[:-self.maximo_respaldos]:
            antiguo.unlink()
        return destino

    def crear_respaldo_excel(self, origen=None):
        """Genera el Excel correspondiente a la base indicada o a la base actual."""
        carpeta = Path(self.configuracion["RESPALDOS_EXCEL"])
        carpeta.mkdir(parents=True, exist_ok=True)
        destino = carpeta / f"taller_{date.today().isoformat()}.xlsx"
        self.exportador_excel.exportar(origen or self.configuracion["DATABASE"], destino)
        for antiguo in sorted(carpeta.glob(self.patron_excel))[:-self.maximo_respaldos]:
            try:
                antiguo.unlink()
            except OSError:
                pass  # Si está abierto en Excel, se borra en otra ocasión.
        return destino

    def carpetas_en_pendrives(self):
        """Busca la carpeta configurada en unidades Windows conectadas."""
        if os.name != "nt" or not self.configuracion["BUSCAR_PENDRIVE"]:
            return []
        import ctypes

        ctypes.windll.kernel32.SetErrorMode(0x0001)
        encontradas = []
        for letra in string.ascii_uppercase[3:]:
            carpeta = Path(f"{letra}:\\") / self.carpeta_pendrive
            try:
                if carpeta.is_dir():
                    encontradas.append(carpeta)
            except OSError:
                continue
        return encontradas

    def archivo_estado_externo(self):
        return Path(self.configuracion["RESPALDOS"]) / ".ultima_copia_externa"

    def copiar_a_destinos_externos(self, archivo, excel=None):
        """Copia un respaldo al pendrive y destinos configurados."""
        copiados, errores = [], []
        destinos = [Path(carpeta) for carpeta in self.configuracion["RESPALDOS_EXTRA"]]
        destinos.extend(self.carpetas_en_pendrives())
        for carpeta in destinos:
            try:
                carpeta.mkdir(parents=True, exist_ok=True)
                temporal = carpeta / (archivo.name + ".tmp")
                shutil.copy2(archivo, temporal)
                os.replace(temporal, carpeta / archivo.name)
                for antiguo in sorted(carpeta.glob(self.patron_respaldo))[:-self.maximo_respaldos]:
                    antiguo.unlink()
                if excel:
                    carpeta_excel = carpeta / "Excel"
                    carpeta_excel.mkdir(exist_ok=True)
                    try:
                        shutil.copy2(excel, carpeta_excel / excel.name)
                        for antiguo in sorted(carpeta_excel.glob(self.patron_excel))[:-self.maximo_respaldos]:
                            antiguo.unlink()
                    except OSError:
                        self.logger.exception(
                            "No se pudo copiar el Excel del respaldo a %s", carpeta_excel
                        )
                        errores.append("destino externo")
                copiados.append(carpeta)
            except OSError:
                self.logger.exception("No se pudo copiar el respaldo a %s", carpeta)
                errores.append("destino externo")
        if copiados:
            self.archivo_estado_externo().write_text(
                datetime.now().isoformat(timespec="minutes"), encoding="utf-8"
            )
        return copiados, errores

    def ultima_copia_externa(self):
        try:
            return datetime.fromisoformat(
                self.archivo_estado_externo().read_text(encoding="utf-8").strip()
            )
        except (OSError, ValueError):
            return None

    def ultimo_respaldo(self):
        archivos = sorted(Path(self.configuracion["RESPALDOS"]).glob(self.patron_respaldo))
        return archivos[-1].stem.removeprefix("taller_") if archivos else None

    def ultimo_excel(self):
        archivos = sorted(
            Path(self.configuracion["RESPALDOS_EXCEL"]).glob(self.patron_excel)
        )
        return archivos[-1].stem.removeprefix("taller_") if archivos else None


def verificar_archivo_base(ruta, tablas_base, ultima_version_esquema, exigir_version_actual=False):
    """Valida integridad, claves foráneas y versión del archivo a restaurar."""
    conexion = sqlite3.connect(ruta)
    try:
        integridad = conexion.execute("PRAGMA integrity_check").fetchone()[0]
        if integridad != "ok":
            raise sqlite3.DatabaseError(f"integridad: {integridad}")
        if conexion.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.DatabaseError("el archivo tiene relaciones rotas")
        tablas = {
            fila[0]
            for fila in conexion.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not tablas_base <= tablas:
            raise sqlite3.DatabaseError("el archivo no contiene las tablas de una base del taller")
        version = (
            conexion.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
            if "schema_migrations" in tablas
            else 0
        )
        if version > ultima_version_esquema:
            raise sqlite3.DatabaseError("el archivo fue creado por una versión más nueva del sistema")
        if exigir_version_actual and version != ultima_version_esquema:
            raise sqlite3.DatabaseError("no se aplicaron todas las migraciones al archivo restaurado")
        return {"tablas": tablas, "version": version}
    finally:
        conexion.close()
