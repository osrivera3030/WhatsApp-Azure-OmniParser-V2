"""
envio_storege.py - Sube el reporte final de agradecimientos a Azure.

Qué hace
--------
1. Lee ``Data/data_repuesta_final/Final_Agradecimientos_Clientes.xlsx``.
2. Filtra solo filas con teléfono presente.
3. Sube cada fila a ``TABLAS["monitoreo_precios"]`` usando el teléfono como
   ``RowKey`` (upsert dinámico: cualquier columna nueva del Excel se sube
   automáticamente).

Cómo ejecutarlo
----------------
    python "5-envio_mjs_repuesta/envio_storege.py"

Debe ejecutarse después de ``res_final.py``, que genera el Excel de entrada.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from azure.data.tables import TableServiceClient

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import (  # noqa: E402
    AZURE_CONNECTION_STRING,
    DATA_RESPUESTA_FINAL_DIR,
    PARTITION_KEY_REPORTE_ARGOS,
    TABLAS,
    get_logger,
)

logger = get_logger(__name__)

ARCHIVO_INPUT: Path = DATA_RESPUESTA_FINAL_DIR / "Final_Agradecimientos_Clientes.xlsx"


def normalizar_nombre_columna(nombre: str) -> str:
    """Convierte un nombre de columna a un nombre válido para Azure Tables.

    Azure Table Storage no acepta espacios ni guiones en los nombres de
    propiedad, por lo que se reemplazan por guion bajo
    (ej. ``"Precio Cemento"`` -> ``"Precio_Cemento"``).
    """
    return nombre.replace(" ", "_").replace("-", "_")


def construir_entidad(fila: dict) -> dict:
    """Convierte una fila del Excel en una entidad lista para subir a Azure.

    Se omiten valores nulos o la cadena literal ``"nan"`` para no ensuciar
    la tabla con propiedades vacías.

    Parameters
    ----------
    fila:
        Diccionario con los datos de una fila (``DataFrame.to_dict()``).

    Returns
    -------
    dict
        Entidad con ``PartitionKey``, ``RowKey`` (teléfono) y el resto de
        columnas saneadas.
    """
    telefono = str(fila.get("phone", "SIN_TEL"))
    entidad = {"PartitionKey": PARTITION_KEY_REPORTE_ARGOS, "RowKey": telefono}

    for columna, valor in fila.items():
        if pd.notna(valor) and str(valor).lower() != "nan":
            entidad[normalizar_nombre_columna(str(columna))] = str(valor)

    return entidad


def subir_excel_a_azure() -> None:
    """Punto de entrada: sincroniza el Excel final con la tabla de monitoreo."""
    if not ARCHIVO_INPUT.exists():
        logger.error("No se encontró el archivo en %s", ARCHIVO_INPUT)
        return

    df = pd.read_excel(ARCHIVO_INPUT)
    df_a_subir = df[df["phone"].notna()].copy()

    if df_a_subir.empty:
        logger.info("No hay registros para subir.")
        return

    service_client = TableServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    table_client = service_client.create_table_if_not_exists(TABLAS["monitoreo_precios"])

    logger.info("Sincronizando %d registros...", len(df_a_subir))
    for _, fila in df_a_subir.iterrows():
        entidad = construir_entidad(fila.to_dict())
        table_client.upsert_entity(entity=entidad)

    logger.info("Sincronización dinámica completada.")


if __name__ == "__main__":
    try:
        subir_excel_a_azure()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error crítico durante la sincronización: %s", exc)
