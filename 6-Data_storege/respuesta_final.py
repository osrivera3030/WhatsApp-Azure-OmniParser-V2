"""
respuesta_final.py
------------------
Pipeline: copia Directorio → Histórico, verifica condiciones en Histórico,
resetea en Directorio solo los números que califican.

Orden de ejecución estricto:
    PASO 1 – COPIA TOTAL
        Copia TODOS los registros de Directoriowtpp a Historicowtp.
        RowKey histórico = OriginalRowKey_Timestamp (evita duplicados).
        Este paso siempre se ejecuta, sin importar fechas ni condiciones.

    PASO 2 – VERIFICACIÓN EN HISTÓRICO
        Lee Historicowtp y evalúa qué números ya cumplieron los 15 días.
        La verificación ocurre sobre el histórico, NO sobre el directorio.
        Soporta múltiples formatos de fecha.

    PASO 3 – RESETEO EN DIRECTORIO
        Solo para los números que califican (paso 2).
        Vacía columnas de control y pone Respuesta_Recibida = 'NO'
        para habilitar el próximo ciclo de envío.

Uso:
    python "6-Data_storege/respuesta_final.py"
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from azure.data.tables import TableClient, UpdateMode

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import (
    AZURE_CONNECTION_STRING,
    DIAS_RESET_SEGUIMIENTO,
    TABLAS,
    get_logger,
)

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

# Formatos de fecha aceptados en cualquier campo de la tabla.
FORMATOS_FECHA: list[str] = [
    "%Y-%m-%d",
    "%d/%m/%Y, %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
]

# Campos donde buscar la fecha de última interacción (en orden de prioridad).
CAMPOS_FECHA: tuple[str, ...] = (
    "Fecha_Verificacion",
    "fecha_envio_msg",
    "Fecha_Envio",
    "Fecha_Reset",
)

# Columnas de control que se vacían al resetear un contacto.
COLUMNAS_RESETEAR: tuple[str, ...] = (
    "Texto_Respuesta",
    "Fecha_Verificacion",
    "fecha_envio_msg",
    "hora_envio_msg",
    "mensaje_enviado",
    "Mensaje_Enviado",
    "Estado_Flujo",
    "Medios_Procesados",
    "Fecha_Reset",
)

# Valores fijos que se asignan tras el reseteo.
VALORES_POST_RESET: dict[str, str] = {
    "Respuesta_Recibida": "NO",   # habilita al contacto para el próximo ciclo
}


# ─────────────────────────────────────────────────────────────────────────────
# DATACLASS – resultado por número
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ResultadoContacto:
    phone: str
    dias_pasados: Optional[int] = None
    copiado: bool = False
    reseteado: bool = False
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# UTILS – fechas
# ─────────────────────────────────────────────────────────────────────────────
def parsear_fecha(valor: str) -> Optional[datetime]:
    """Intenta parsear una cadena con todos los formatos conocidos."""
    for fmt in FORMATOS_FECHA:
        try:
            return datetime.strptime(valor.strip(), fmt)
        except ValueError:
            continue
    return None


def fecha_mas_reciente(entity: dict) -> Optional[datetime]:
    """Devuelve la fecha más reciente entre todos los campos de fecha del registro."""
    candidatas: list[datetime] = []
    for campo in CAMPOS_FECHA:
        valor = str(entity.get(campo, "")).strip()
        if valor:
            dt = parsear_fecha(valor)
            if dt:
                candidatas.append(dt)
    return max(candidatas) if candidatas else None


# ─────────────────────────────────────────────────────────────────────────────
# PASO 1 – CopiadorDirectorio
# ─────────────────────────────────────────────────────────────────────────────
class CopiadorDirectorio:
    """
    Copia TODOS los registros de Directoriowtpp a Historicowtp.
    Siempre se ejecuta, sin filtro de fechas.
    RowKey histórico = OriginalRowKey_Timestamp para evitar duplicados.
    """

    def __init__(self, directorio: TableClient, historico: TableClient):
        self.directorio = directorio
        self.historico  = historico

    def _row_key_historico(self, row_key_original: str) -> str:
        return f"{row_key_original}_{int(datetime.now().timestamp())}"

    def copiar_todo(self) -> int:
        """Copia todas las entidades. Retorna la cantidad copiada con éxito."""
        sello       = datetime.now().strftime("%d/%m/%Y, %H:%M:%S")
        copiados    = 0
        errores     = 0

        logger.info("PASO 1 — Copiando '%s' → '%s'...", TABLAS["directorio"], TABLAS["historico"])

        for entity in self.directorio.list_entities():
            row_key_orig = entity.get("RowKey", "")
            try:
                copia = dict(entity)
                copia["RowKey"]                   = self._row_key_historico(row_key_orig)
                copia["RowKey_Original"]           = row_key_orig
                copia["Fecha_Registro_Historico"]  = sello
                self.historico.create_entity(entity=copia)
                copiados += 1
                logger.debug("Copiado: %s → %s", row_key_orig, copia["RowKey"])
            except Exception as exc:
                errores += 1
                logger.error("Error copiando %s: %s", row_key_orig, exc)

        logger.info("PASO 1 completado — Copiados: %d | Errores: %d", copiados, errores)
        return copiados


# ─────────────────────────────────────────────────────────────────────────────
# PASO 2 – VerificadorHistorico
# ─────────────────────────────────────────────────────────────────────────────
class VerificadorHistorico:
    """
    Lee Historicowtp, evalúa fechas y devuelve los RowKey_Original
    que ya cumplieron el umbral de días.
    La verificación ocurre sobre el histórico, no sobre el directorio.
    """

    def __init__(self, historico: TableClient, dias_umbral: int = DIAS_RESET_SEGUIMIENTO):
        self.historico   = historico
        self.dias_umbral = dias_umbral

    def obtener_calificados(self) -> dict[str, int]:
        """
        Retorna {RowKey_Original: dias_pasados} para los números
        cuya entrada más reciente en el histórico supera el umbral.
        Si un número tiene varias entradas históricas, se usa la MÁS RECIENTE
        (la que tiene mayor fecha de última interacción).
        """
        hoy = datetime.now()

        # Agrupa por RowKey_Original y queda con la fecha más reciente
        por_numero: dict[str, datetime] = {}

        logger.info("PASO 2 — Verificando condiciones en '%s'...", TABLAS["historico"])

        for entity in self.historico.list_entities(
            select="PartitionKey,RowKey,RowKey_Original,Fecha_Verificacion,fecha_envio_msg,Fecha_Envio,Fecha_Reset"
        ):
            row_key_orig = str(entity.get("RowKey_Original", "")).strip()
            if not row_key_orig:
                continue

            fecha = fecha_mas_reciente(entity)
            if fecha is None:
                continue

            # Guardar la fecha más reciente por número
            if row_key_orig not in por_numero or fecha > por_numero[row_key_orig]:
                por_numero[row_key_orig] = fecha

        # Filtrar los que superaron el umbral
        calificados: dict[str, int] = {}
        sin_condicion = 0

        for numero, fecha_reciente in por_numero.items():
            dias = (hoy - fecha_reciente).days
            if dias >= self.dias_umbral:
                calificados[numero] = dias
                logger.info("Califica (%d días): %s", dias, numero)
            else:
                sin_condicion += 1
                logger.debug("No califica (%d días, faltan %d): %s",
                             dias, self.dias_umbral - dias, numero)

        logger.info(
            "PASO 2 completado — Califican: %d | No califican: %d",
            len(calificados), sin_condicion,
        )
        return calificados


# ─────────────────────────────────────────────────────────────────────────────
# PASO 3 – ResetadorDirectorio
# ─────────────────────────────────────────────────────────────────────────────
class ResetadorDirectorio:
    """
    Resetea en Directoriowtpp SOLO los números que calificaron en el paso 2.
    El reseteo vacía columnas de control y pone Respuesta_Recibida = 'NO'.
    """

    def __init__(self, directorio: TableClient):
        self.directorio = directorio

    def resetear(self, row_key: str, partition_key: str, dias_pasados: int) -> bool:
        """Lee la entidad del directorio y ejecuta el reseteo. Retorna True si OK."""
        try:
            entity = self.directorio.get_entity(partition_key, row_key)
        except Exception as exc:
            logger.error("Error leyendo %s del directorio: %s", row_key, exc)
            return False

        try:
            for columna in COLUMNAS_RESETEAR:
                entity[columna] = ""

            ahora = datetime.now().strftime("%d/%m/%Y, %H:%M:%S")
            for col, val in VALORES_POST_RESET.items():
                entity[col] = val
            entity["Fecha_Reset"] = ahora

            self.directorio.update_entity(mode=UpdateMode.MERGE, entity=entity)
            logger.info(
                "🔄 Reseteado (%d días): %s | Respuesta_Recibida=NO | %s",
                dias_pasados, row_key, ahora,
            )
            return True
        except Exception as exc:
            logger.error("Error reseteando %s: %s", row_key, exc)
            return False

    def resetear_lote(self, calificados: dict[str, int], directorio_index: dict[str, str]) -> int:
        """
        Itera sobre los números calificados y ejecuta el reseteo en cada uno.
        directorio_index = {RowKey: PartitionKey} construido en el pipeline.
        Retorna la cantidad de reseteos exitosos.
        """
        reseteados = 0
        for row_key, dias in calificados.items():
            partition_key = directorio_index.get(row_key, "")
            if not partition_key:
                logger.warning("No se encontró PartitionKey para %s, omitiendo.", row_key)
                continue
            if self.resetear(row_key, partition_key, dias):
                reseteados += 1
        return reseteados


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE – orquestador
# ─────────────────────────────────────────────────────────────────────────────
class PipelineArchivado:
    """
    Ejecuta los 3 pasos en orden estricto y garantiza que:
      - El reseteo nunca ocurre si el número no está en el histórico.
      - Un error en un número no detiene el resto del lote.
      - El resumen final muestra exactamente qué pasó con cada registro.
    """

    def __init__(self):
        self.directorio = TableClient.from_connection_string(
            AZURE_CONNECTION_STRING, TABLAS["directorio"]
        )
        self.historico = TableClient.from_connection_string(
            AZURE_CONNECTION_STRING, TABLAS["historico"]
        )

    def ejecutar(self) -> None:
        inicio = datetime.now()
        self._encabezado(inicio)

        # PASO 1: Copia total Directorio → Histórico
        copiador = CopiadorDirectorio(self.directorio, self.historico)
        copiados = copiador.copiar_todo()

        if copiados == 0:
            logger.warning("No se copió ningún registro. Verificar tabla '%s'.", TABLAS["directorio"])
            return

        # Construir índice {RowKey: PartitionKey} del directorio para el paso 3
        directorio_index: dict[str, str] = {}
        for entity in self.directorio.list_entities(select="PartitionKey,RowKey"):
            directorio_index[entity["RowKey"]] = entity["PartitionKey"]

        # PASO 2: Verificación de condiciones en Histórico
        verificador  = VerificadorHistorico(self.historico)
        calificados  = verificador.obtener_calificados()

        if not calificados:
            logger.info("Ningún número cumple los %d días. Sin reseteos.", DIAS_RESET_SEGUIMIENTO)
            self._resumen(copiados, 0, 0, inicio)
            return

        # PASO 3: Reseteo en Directorio
        logger.info("PASO 3 — Reseteando %d número(s) en '%s'...", len(calificados), TABLAS["directorio"])
        reseteador = ResetadorDirectorio(self.directorio)
        reseteados = reseteador.resetear_lote(calificados, directorio_index)

        self._resumen(copiados, len(calificados), reseteados, inicio)

    def _encabezado(self, inicio: datetime) -> None:
        logger.info("=" * 60)
        logger.info("PIPELINE ARCHIVADO — %s", inicio.strftime("%d/%m/%Y %H:%M:%S"))
        logger.info("  Directorio : %s", TABLAS["directorio"])
        logger.info("  Histórico  : %s", TABLAS["historico"])
        logger.info("  Umbral     : %d días", DIAS_RESET_SEGUIMIENTO)
        logger.info("=" * 60)

    def _resumen(self, copiados: int, califican: int, reseteados: int, inicio: datetime) -> None:
        duracion = (datetime.now() - inicio).total_seconds()
        logger.info("=" * 60)
        logger.info("RESUMEN")
        logger.info("  Copiados a histórico : %d", copiados)
        logger.info("  Califican 15 días    : %d", califican)
        logger.info("  Reseteados OK        : %d", reseteados)
        logger.info("  Duración             : %.1f s", duracion)
        logger.info("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# PUNTO DE ENTRADA
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    pipeline = PipelineArchivado()
    try:
        pipeline.ejecutar()
    except Exception as exc:
        logger.exception("Error no controlado: %s", exc)


if __name__ == "__main__":
    main()
