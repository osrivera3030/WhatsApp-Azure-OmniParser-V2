"""
Data_cleaning.py - Limpieza local de la base de contactos (Excel).

Qué hace
--------
1. Carga ``Data/Conso_Harward_store.xlsx`` (ruta relativa a la carpeta
   "desarrollo", ver ``config.DATA_DIR``).
2. Normaliza la columna ``phone`` a números hondureños de 8 dígitos con
   formato ``XXXX-XXXX``, descartando los que no se pueden validar.
3. Elimina columnas que no aportan al pipeline (``address``, ``website``,
   ``review_count``).
4. Guarda el resultado limpio en ``Data/Data_limpia/Harward_Store_Limpio.xlsx``.

Cómo ejecutarlo
----------------
    python "1-Data_cleaning/Data_cleaning.py"

Requiere que el archivo de entrada exista en ``Data/Conso_Harward_store.xlsx``
dentro de la carpeta "desarrollo".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import DATA_DIR, DATA_LIMPIA_DIR, asegurar_directorios, get_logger  # noqa: E402

logger = get_logger(__name__)

ARCHIVO_ENTRADA: Path = DATA_DIR / "Conso_Harward_store.xlsx"
ARCHIVO_SALIDA: Path = DATA_LIMPIA_DIR / "Harward_Store_Limpio.xlsx"
COLUMNAS_A_ELIMINAR: list[str] = ["address", "website", "review_count"]


def limpiar_telefono_honduras(telefono: object) -> str | None:
    """Normaliza un número telefónico hondureño al formato ``XXXX-XXXX``.

    Reglas de validación:
        * Se ignoran extensiones del tipo "ext. 4300".
        * 11 dígitos que empiezan con "504"  -> se usan los últimos 8.
        * 9 dígitos que empiezan con "504"   -> se usa el dígito 2 al 9.
        * 8 dígitos                          -> se usan tal cual.
        * Cualquier otro caso                -> no es válido (``None``).

    Parameters
    ----------
    telefono:
        Valor crudo de la celda de teléfono (puede venir como str, float o NaN).

    Returns
    -------
    str | None
        Teléfono formateado como ``XXXX-XXXX``, o ``None`` si no es válido.
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
        # NOTA: en la versión original había un bug aquí
        # (`len(digitos == 9 and ...)`) que lanzaba TypeError para
        # cualquier teléfono de 9 dígitos. Se corrigió la condición.
        numero_final = digitos[1:]

    if numero_final:
        return f"{numero_final[:4]}-{numero_final[4:]}"
    return None


def limpiar_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Aplica la limpieza de teléfono y elimina columnas innecesarias.

    Parameters
    ----------
    df:
        DataFrame crudo, debe contener al menos la columna ``phone``.

    Returns
    -------
    pd.DataFrame
        Copia del DataFrame solo con filas de teléfono válido y sin las
        columnas listadas en ``COLUMNAS_A_ELIMINAR``.

    Raises
    ------
    KeyError
        Si el DataFrame no tiene columna ``phone``.
    """
    if "phone" not in df.columns:
        raise KeyError("El archivo de entrada no tiene columna 'phone'.")

    df = df.copy()
    df["phone"] = df["phone"].apply(limpiar_telefono_honduras)
    df_limpio = df[df["phone"].notna()].copy()
    return df_limpio.drop(columns=COLUMNAS_A_ELIMINAR, errors="ignore")


def main() -> None:
    """Punto de entrada: lee, limpia y guarda el archivo de contactos."""
    if not ARCHIVO_ENTRADA.exists():
        logger.error("No se encuentra el archivo de entrada en %s", ARCHIVO_ENTRADA)
        return

    df = pd.read_excel(ARCHIVO_ENTRADA)
    df_limpio = limpiar_dataframe(df)

    asegurar_directorios(DATA_LIMPIA_DIR)
    df_limpio.to_excel(ARCHIVO_SALIDA, index=False)

    logger.info("Proceso completado con éxito.")
    logger.info("Se guardaron %d registros válidos con formato XXXX-XXXX.", len(df_limpio))
    logger.info("Archivo guardado en: %s", ARCHIVO_SALIDA)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - se reporta y se termina con claridad
        logger.exception("Ocurrió un error durante la limpieza: %s", exc)
