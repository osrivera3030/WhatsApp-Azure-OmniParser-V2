"""
actualizar_directorio_wtp.py
----------------------------
Toma los 4 registros enviados hoy (2026-06-24) del archivo prueba_2.csv
y los hace upsert en la tabla Directoriowtpp de Azure Table Storage.

Uso:
    python actualizar_directorio_wtp.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from azure.data.tables import TableClient, UpdateMode

sys.path.append(str(Path(__file__).resolve().parent))
from config import AZURE_CONNECTION_STRING, get_logger

logger = get_logger(__name__)

CSV_PATH   = Path(__file__).parent / "prueba_2.csv"
TABLA_DEST = "Directoriowtpp"

# Columnas que NO son datos de negocio (no se suben como propiedades)
EXCLUIR = {"PartitionKey", "RowKey"}


def _convertir_tipo(valor: str, tipo: str) -> object:
    """Convierte el valor al tipo indicado por la columna @type del CSV."""
    if not valor:
        return None
    tipo = (tipo or "").strip()
    if tipo == "Int32":
        try:
            return int(valor)
        except ValueError:
            return valor
    if tipo in ("Double", "Float"):
        try:
            return float(valor)
        except ValueError:
            return valor
    return valor


def cargar_sin_respuesta() -> list[dict]:
    """Lee el CSV y devuelve todos los registros con Respuesta_Recibida != 'SI'.

    Esta es la misma lógica que usa procesar_respuestas.py al consultar Azure:
    solo procesa los contactos a los que todavía no se ha capturado respuesta,
    para que el script de captura los pueda encontrar y leer su chat.
    """
    registros = []
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        cols_dato = [c for c in fieldnames if "@type" not in c and c not in EXCLUIR]

        for row in reader:
            # Mismo criterio que: query_filter="Respuesta_Recibida eq 'NO'"
            respuesta = row.get("Respuesta_Recibida", "").strip().upper()
            if respuesta == "SI":
                continue  # ya tiene respuesta capturada, no es necesario subirlo

            entidad: dict = {
                "PartitionKey": row["PartitionKey"],
                "RowKey":       row["RowKey"],
            }
            for col in cols_dato:
                val  = row.get(col, "").strip()
                tipo = row.get(col + "@type", "").strip()
                convertido = _convertir_tipo(val, tipo)
                if convertido is not None and convertido != "":
                    entidad[col] = convertido

            registros.append(entidad)

    return registros


def subir_a_directoriowtpp(registros: list[dict]) -> None:
    """Hace upsert de cada registro en la tabla Directoriowtpp."""
    with TableClient.from_connection_string(
        conn_str=AZURE_CONNECTION_STRING,
        table_name=TABLA_DEST,
    ) as cliente:
        for entidad in registros:
            pk  = entidad["PartitionKey"]
            rk  = entidad["RowKey"]
            try:
                cliente.upsert_entity(entity=entidad, mode=UpdateMode.REPLACE)
                logger.info("OK  ➜  %s / %s  (%s)", pk, rk, entidad.get("name", ""))
            except Exception as exc:
                logger.error("ERROR  %s / %s: %s", pk, rk, exc)


def main() -> None:
    if not CSV_PATH.exists():
        logger.error("No se encontró el archivo %s", CSV_PATH)
        sys.exit(1)

    registros = cargar_registros_hoy()
    if not registros:
        logger.warning("No se encontraron registros con fecha %s en el CSV.", FECHA_HOY)
        sys.exit(0)

    logger.info("Registros a subir: %d", len(registros))
    for r in registros:
        logger.info("  → %s | %s | %s", r["RowKey"], r.get("name",""), r.get("MUNICIPIO",""))

    subir_a_directoriowtpp(registros)
    logger.info("Listo. %d registros actualizados en '%s'.", len(registros), TABLA_DEST)


if __name__ == "__main__":
    main()
