"""
tablas_env.py - Ingesta del Excel consolidado hacia Azure Table Storage.

Qué hace
--------
1. Busca en ``Data/`` el primer archivo que empiece con
   ``Conso_Harward_store`` y termine en ``.xlsx``.
2. Sube el contenido crudo a la tabla ``TABLAS["cruda"]`` (Azure).
3. Limpia los teléfonos y elimina columnas innecesarias (misma lógica que
   ``Data_cleaning.py``) y sube el resultado a ``TABLAS["limpia"]``.

Cómo ejecutarlo
----------------
    python "1-Data_cleaning/tablas_env.py"

Requiere conectividad a Azure y que la cadena de conexión en ``config.py``
sea válida.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
from azure.data.tables import TableClient

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import AZURE_CONNECTION_STRING, DATA_DIR, TABLAS, get_logger  # noqa: E402

logger = get_logger(__name__)

COLUMNAS_A_ELIMINAR: list[str] = ["address", "website", "review_count"]
PARTITION_KEY: str = "HarwardStore"


def limpiar_telefono_honduras(telefono: object) -> str | None:
    """Normaliza un número telefónico hondureño al formato ``XXXX-XXXX``.

    Ver docstring equivalente en ``1-Data_cleaning/Data_cleaning.py`` para el
    detalle de las reglas de validación.
    """
    if pd.isna(telefono) or telefono == "":
        return None

    texto = str(telefono).strip()
    texto = re.split(r"\s+ext\.?", texto, flags=re.IGNORECASE)[0]
    digitos = re.sub(r"\D", "", texto)

    numero_final: str | None = None
    if len(digitos) == 11 and digitos.startswith("504"):
        numero_final = digitos[3:]
    elif len(digitos) == 8:
        numero_final = digitos
    elif len(digitos) == 9 and digitos.startswith("504"):
        # Corrección de bug de la versión original (ver Data_cleaning.py).
        numero_final = digitos[1:]

    return f"{numero_final[:4]}-{numero_final[4:]}" if numero_final else None


def buscar_archivo_consolidado(carpeta_data: Path) -> Path | None:
    """Busca el primer archivo ``Conso_Harward_store*.xlsx`` dentro de ``carpeta_data``.

    Returns
    -------
    Path | None
        Ruta del primer archivo encontrado, o ``None`` si no existe ninguno.
    """
    if not carpeta_data.exists():
        return None

    candidatos = sorted(
        f for f in carpeta_data.iterdir()
        if f.name.startswith("Conso_Harward_store") and f.suffix == ".xlsx"
    )
    return candidatos[0] if candidatos else None


def actualizar_tabla(df: pd.DataFrame, nombre_tabla: str) -> None:
    """Sincroniza un DataFrame completo contra una tabla de Azure (upsert por fila).

    Cada fila se sube como una entidad con ``PartitionKey="HarwardStore"`` y
    ``RowKey`` igual al teléfono sin caracteres especiales (para evitar
    duplicados al re-ejecutar el script).

    Parameters
    ----------
    df:
        DataFrame a sincronizar.
    nombre_tabla:
        Nombre de la tabla de Azure (ver ``config.TABLAS``).
    """
    table_client = TableClient.from_connection_string(
        conn_str=AZURE_CONNECTION_STRING, table_name=nombre_tabla
    )

    try:
        table_client.create_table(mode="skip")
    except Exception as exc:  # noqa: BLE001
        logger.info("Tabla '%s' ya existe o no se pudo crear: %s", nombre_tabla, exc)

    for entity in df.to_dict(orient="records"):
        entidad_limpia = {str(k): str(v) for k, v in entity.items()}
        entidad_limpia["PartitionKey"] = PARTITION_KEY

        raw_phone = str(entity.get("phone", "sin_telefono"))
        entidad_limpia["RowKey"] = re.sub(r"\D+", "", raw_phone)

        table_client.upsert_entity(entidad_limpia)

    logger.info("Tabla '%s' actualizada con éxito.", nombre_tabla)


def main() -> None:
    """Punto de entrada: localiza el Excel, sube datos crudos y datos limpios."""
    archivo_path = buscar_archivo_consolidado(DATA_DIR)
    if archivo_path is None:
        logger.error(
            "No se encontró ningún archivo 'Conso_Harward_store*.xlsx' en %s", DATA_DIR
        )
        return

    logger.info("Procesando archivo: %s", archivo_path)
    df = pd.read_excel(archivo_path)

    actualizar_tabla(df, TABLAS["cruda"])

    df_limpio = df.copy()
    df_limpio["phone"] = df_limpio["phone"].apply(limpiar_telefono_honduras)
    df_limpio = df_limpio[df_limpio["phone"].notna()]
    df_limpio = df_limpio.drop(columns=COLUMNAS_A_ELIMINAR, errors="ignore")

    actualizar_tabla(df_limpio, TABLAS["limpia"])
    logger.info("Proceso finalizado. Tus tablas en Azure están al día.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ocurrió un error durante la sincronización: %s", exc)
