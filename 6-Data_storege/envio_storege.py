"""
envio_storege.py - Archiva el directorio y resetea seguimientos vencidos.

Qué hace
--------
1. Copia cada entidad de ``TABLAS["directorio"]`` hacia ``TABLAS["historico"]``
   (``RowKey`` original + timestamp, para no chocar con copias anteriores).
2. Si una entidad tiene ``fecha_envio_msg`` con más de
   ``config.DIAS_RESET_SEGUIMIENTO`` días, resetea sus columnas de
   seguimiento en el directorio para permitir un nuevo ciclo de envío.

Cómo ejecutarlo
----------------
    python "6-Data_storege/envio_storege.py"

Nota
----
El histórico se mantiene únicamente en Azure Table Storage
(``TABLAS["historico"]``); por decisión explícita del usuario no se agregó
una base de datos local SQLite para este respaldo.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from azure.data.tables import TableClient, UpdateMode

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import AZURE_CONNECTION_STRING, DIAS_RESET_SEGUIMIENTO, TABLAS, get_logger  # noqa: E402

logger = get_logger(__name__)

# Columnas de seguimiento que se limpian cuando se reinicia un ciclo de
# contacto (después de DIAS_RESET_SEGUIMIENTO días sin avanzar).
COLUMNAS_A_RESETEAR: tuple[str, ...] = (
    "Respuesta_Recibida",
    "Texto_Respuesta",
    "Fecha_Verificacion",
    "fecha_envio_msg",
    "hora_envio_msg",
    "mensaje_enviado",
)


def copiar_a_historico(historico: TableClient, entity: dict, sello_tiempo: str) -> bool:
    """Copia una entidad del directorio a la tabla histórica con RowKey único.

    Parameters
    ----------
    historico:
        Cliente conectado a ``TABLAS["historico"]``.
    entity:
        Entidad original del directorio.
    sello_tiempo:
        Sufijo de tiempo (``YYYYMMDD_HHMMSS``) para evitar colisiones de RowKey.

    Returns
    -------
    bool
        ``True`` si la copia fue exitosa.
    """
    nuevo = dict(entity)
    nuevo["RowKey"] = f"{entity['RowKey']}_{sello_tiempo}"
    nuevo["Fecha_Registro_Historico"] = datetime.now().strftime("%d/%m/%Y, %H:%M:%S")
    historico.create_entity(entity=nuevo)
    return True


def debe_resetear(entity: dict) -> tuple[bool, int | None]:
    """Determina si una entidad debe resetearse según ``fecha_envio_msg``.

    Returns
    -------
    tuple[bool, int | None]
        ``(debe_resetear, dias_pasados)``. ``dias_pasados`` es ``None`` si
        no se pudo calcular (sin fecha o fecha inválida).
    """
    fecha_envio_str = str(entity.get("fecha_envio_msg", "")).strip()
    if not fecha_envio_str:
        return False, None

    fecha_envio = datetime.strptime(fecha_envio_str, "%Y-%m-%d")
    dias_pasados = (datetime.now() - fecha_envio).days
    return dias_pasados >= DIAS_RESET_SEGUIMIENTO, dias_pasados


def resetear_entidad(directorio: TableClient, entity: dict) -> None:
    """Limpia las columnas de seguimiento de una entidad en el directorio."""
    for columna in COLUMNAS_A_RESETEAR:
        entity[columna] = ""
    directorio.update_entity(mode=UpdateMode.MERGE, entity=entity)


def copiar_y_resetear() -> None:
    """Punto de entrada: archiva el directorio completo y resetea lo vencido."""
    directorio = TableClient.from_connection_string(AZURE_CONNECTION_STRING, TABLAS["directorio"])
    historico = TableClient.from_connection_string(AZURE_CONNECTION_STRING, TABLAS["historico"])

    entities = list(directorio.list_entities())
    if not entities:
        logger.info("No hay registros en %s.", TABLAS["directorio"])
        return

    sello_tiempo = datetime.now().strftime("%Y%m%d_%H%M%S")
    copiados = reseteados = sin_fecha = 0

    for entity in entities:
        phone = entity.get("phone", entity.get("RowKey", "?"))
        name = entity.get("name", "?")

        try:
            copiar_a_historico(historico, entity, sello_tiempo)
            copiados += 1
            logger.info("Copiado: %s | %s", phone, name)
        except Exception as exc:  # noqa: BLE001
            logger.error("Error copiando %s: %s", phone, exc)
            continue

        try:
            debe_reset, dias_pasados = debe_resetear(entity)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error calculando días para %s: %s", phone, exc)
            continue

        if dias_pasados is None:
            sin_fecha += 1
            logger.warning("Sin fecha_envio_msg: %s | %s", phone, name)
            continue

        if debe_reset:
            resetear_entidad(directorio, entity)
            reseteados += 1
            logger.info("Reseteado (%d días): %s | %s", dias_pasados, phone, name)
        else:
            faltan = DIAS_RESET_SEGUIMIENTO - dias_pasados
            logger.info("Sin resetear (%d días, faltan %d): %s | %s", dias_pasados, faltan, phone, name)

    logger.info("=" * 50)
    logger.info("Copiados a %s: %d", TABLAS["historico"], copiados)
    logger.info("Reseteados en directorio: %d", reseteados)
    logger.info("Sin fecha_envio_msg: %d", sin_fecha)
    logger.info("=" * 50)


if __name__ == "__main__":
    try:
        copiar_y_resetear()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ocurrió un error durante el archivado: %s", exc)
