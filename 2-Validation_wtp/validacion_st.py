"""
validacion_st.py - Verifica qué números tienen WhatsApp activo.

Qué hace
--------
1. Lee todos los registros de ``TABLAS["limpia"]`` en Azure.
2. Abre WhatsApp Web con un perfil de Edge persistente (requiere escanear
   el QR una sola vez por sesión de perfil).
3. Para cada número, navega a ``web.whatsapp.com/send?phone=...`` y detecta
   si el número está o no registrado en WhatsApp.
4. Sube cada resultado a ``TABLAS["validada"]`` junto con la fecha/hora de
   validación.

Cómo ejecutarlo
----------------
    python "2-Validation_wtp/validacion_st.py"

Se pedirá interactivamente el código de país y escanear el QR de WhatsApp
Web la primera vez. Requiere Microsoft Edge instalado.

Nota de alcance
-----------------
Este script automatiza WhatsApp Web mediante Selenium, lo cual está fuera
de los Términos de Servicio oficiales de WhatsApp. Este refactor solo
reorganiza y documenta el código existente: no se modificó ni se reforzó
la lógica de automatización/envío.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from azure.data.tables import TableClient
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options
from selenium.webdriver.edge.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.microsoft import EdgeChromiumDriverManager

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import (  # noqa: E402
    AZURE_CONNECTION_STRING,
    PARTITION_KEY_HARWARD_STORE,
    PERFIL_WHATSAPP_DIR,
    TABLAS,
    get_logger,
)

logger = get_logger(__name__)

ESPERA_DETECCION_SEGUNDOS = 20
ESPERA_ENTRE_NUMEROS_SEGUNDOS = 2.0


def iniciar_driver() -> webdriver.Edge:
    """Inicializa un navegador Edge con perfil persistente para WhatsApp Web.

    Returns
    -------
    webdriver.Edge
        Instancia de navegador lista para usar.
    """
    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument(f"--user-data-dir={PERFIL_WHATSAPP_DIR}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    servicio = Service(EdgeChromiumDriverManager().install())
    return webdriver.Edge(service=servicio, options=options)


def validar_numero(driver: webdriver.Edge, numero: str, codigo_pais: str) -> str:
    """Determina si un número tiene WhatsApp activo navegando a su chat directo.

    Parameters
    ----------
    driver:
        Navegador ya autenticado en WhatsApp Web.
    numero:
        Número telefónico (con o sin formato) a validar.
    codigo_pais:
        Código de país a anteponer si el número no lo incluye (ej. "504").

    Returns
    -------
    str
        ``"SI"`` si el número tiene WhatsApp, ``"NO"`` en caso contrario o
        si ocurre cualquier error/timeout.
    """
    numero_limpio = re.sub(r"\D", "", str(numero))
    telefono = (
        numero_limpio
        if numero_limpio.startswith(codigo_pais)
        else f"{codigo_pais}{numero_limpio}"
    )

    driver.get(f"https://web.whatsapp.com/send/?phone={telefono}&app_absent=0")
    try:
        WebDriverWait(driver, ESPERA_DETECCION_SEGUNDOS).until(
            lambda d: "no está en whatsapp" in d.find_element(By.TAG_NAME, "body").text.lower()
            or d.find_elements(By.XPATH, "//div[@contenteditable='true']")
        )
        texto = driver.find_element(By.TAG_NAME, "body").text.lower()
        return "SI" if "no está en whatsapp" not in texto else "NO"
    except Exception:  # noqa: BLE001 - cualquier timeout/error se trata como "NO"
        return "NO"


def actualizar_azure(row: pd.Series, estado: str) -> None:
    """Sube el resultado de validación de un contacto a ``TABLAS["validada"]``.

    Parameters
    ----------
    row:
        Fila del DataFrame de origen (debe incluir columna ``phone``).
    estado:
        Resultado de ``validar_numero`` (``"SI"`` o ``"NO"``).
    """
    table_client = TableClient.from_connection_string(
        AZURE_CONNECTION_STRING, TABLAS["validada"]
    )
    try:
        table_client.create_table()
    except Exception:  # noqa: BLE001 - la tabla ya puede existir
        pass

    entity = {str(k): str(v) for k, v in row.to_dict().items()}
    entity["PartitionKey"] = PARTITION_KEY_HARWARD_STORE
    entity["RowKey"] = re.sub(r"\D+", "", str(row["phone"]))
    entity["Tiene_WhatsApp"] = estado
    entity["Fecha_Validacion"] = datetime.now().strftime("%Y-%m-%d")
    entity["Hora_Validacion"] = datetime.now().strftime("%H:%M:%S")

    table_client.upsert_entity(entity)


def main() -> None:
    """Punto de entrada: valida en lote todos los contactos de la tabla limpia."""
    table_client = TableClient.from_connection_string(
        AZURE_CONNECTION_STRING, TABLAS["limpia"]
    )
    df = pd.DataFrame(list(table_client.list_entities()))
    if df.empty:
        logger.warning("La tabla '%s' no tiene registros.", TABLAS["limpia"])
        return

    codigo_pais = input("Introduce tu código de país (ej. 504): ").strip()
    driver = iniciar_driver()
    driver.get("https://web.whatsapp.com")
    input("\nEscanea el QR y presiona ENTER para continuar...")

    try:
        for index, row in df.iterrows():
            numero = row["phone"]
            logger.info("Validando: %s...", numero)

            estado = validar_numero(driver, numero, codigo_pais)
            actualizar_azure(row, estado)

            logger.info("[%s] %s -> %s (subido a Azure)", index, numero, estado)
            time.sleep(ESPERA_ENTRE_NUMEROS_SEGUNDOS)
    finally:
        driver.quit()

    logger.info("Proceso terminado. Datos actualizados en Azure Storage.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ocurrió un error durante la validación: %s", exc)
