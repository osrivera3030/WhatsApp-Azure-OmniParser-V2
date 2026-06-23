"""
res_final.py - Envía mensajes de agradecimiento a clientes que respondieron.

Qué hace
--------
1. Lee ``Data/Data_mjs_reci/Data_mjs_revdos.xlsx`` (respuestas ya revisadas
   manualmente o por ``4-Guarda_mjs_recep/envo_stg_save.py``).
2. Filtra clientes con ``Respuesta_Recibida == 'SI'`` que aún no recibieron
   agradecimiento.
3. Envía, vía WhatsApp Web (Selenium), un mensaje de agradecimiento elegido
   al azar de ``MENSAJES_AGRADECIMIENTO``.
4. Guarda el resultado final en
   ``Data/data_repuesta_final/Final_Agradecimientos_Clientes.xlsx`` (insumo
   de ``envio_storege.py``).

Cómo ejecutarlo
----------------
    python "5-envio_mjs_repuesta/res_final.py"

Requiere una sesión de WhatsApp Web ya autenticada (escanear QR).

Nota de alcance
-----------------
Este script automatiza el envío de mensajes vía WhatsApp Web con Selenium,
lo cual está fuera de los Términos de Servicio oficiales de WhatsApp. Este
refactor solo reorganiza y documenta el código existente: no se modificó
ni se reforzó la lógica de automatización/envío.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.microsoft import EdgeChromiumDriverManager

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import (  # noqa: E402
    DATA_MJS_RECIBIDOS_DIR,
    DATA_RESPUESTA_FINAL_DIR,
    asegurar_directorios,
    get_logger,
)

logger = get_logger(__name__)

ARCHIVO_RESPUESTAS: Path = DATA_MJS_RECIBIDOS_DIR / "Data_mjs_revdos.xlsx"
ARCHIVO_FINAL: Path = DATA_RESPUESTA_FINAL_DIR / "Final_Agradecimientos_Clientes.xlsx"
ESPERA_CAJA_TEXTO_SEGUNDOS = 40
PAUSA_ENTRE_ENVIOS = (25, 45)  # rango en segundos (min, max)

MENSAJES_AGRADECIMIENTO: list[str] = [
    "Muchas gracias amigo por la información, se lo agradezco bastante. Que "
    "tenga un excelente día.",
    "Gracias por responder tan rápido, me ha servido mucho la información. "
    "Bendiciones.",
    "Perfecto amigo, muchas gracias por el apoyo y por tomarse el tiempo de "
    "responder.",
    "Le agradezco mucho la ayuda, me queda clara la información. Que esté muy "
    "bien.",
    "Excelente, gracias por el dato amigo. Se lo agradezco bastante.",
    "Muchas gracias por la información brindada, me fue de mucha utilidad. "
    "Saludos.",
    "Gracias amigo, muy amable por responder. Que tenga un buen día y éxitos "
    "en sus ventas.",
    "Listo, información recibida. Muchas gracias por su atención y apoyo.",
    "Le agradezco mucho la respuesta. Cualquier cosa estaremos en contacto. "
    "Saludos.",
    "Perfecto amigo, muchas gracias por la información. Que le vaya muy bien.",
    "Gracias por el tiempo y por compartir la información. Se lo agradezco "
    "bastante.",
    "Muy amable de su parte responder. Muchas gracias y que tenga un excelente "
    "día.",
    "Gracias amigo, ya tengo la información que necesitaba. Bendiciones y "
    "éxitos.",
    "Excelente atención, muchas gracias por la respuesta y por la ayuda "
    "brindada.",
    "Muchas gracias por responder, le agradezco mucho la colaboración. "
    "Saludos cordiales.",
    "Gracias por el dato amigo, me ayuda bastante para la cotización. Que "
    "esté bien.",
    "Perfecto, quedó todo claro. Muchas gracias por su tiempo y atención.",
    "Le agradezco mucho la información compartida. Que tenga una excelente "
    "jornada.",
    "Gracias amigo por la ayuda, muy amable de su parte responder tan rápido.",
    "Muchas gracias por la información y por su atención. Que tenga un buen "
    "día.",
]


def obtener_pendientes(df: pd.DataFrame) -> pd.DataFrame:
    """Filtra clientes que respondieron y aún no recibieron agradecimiento.

    Parameters
    ----------
    df:
        DataFrame con columnas ``Respuesta_Recibida`` y, opcionalmente,
        ``agradecimiento_enviado`` (se crea vacía si no existe).

    Returns
    -------
    pd.DataFrame
        Subconjunto de filas pendientes de agradecimiento.
    """
    if "agradecimiento_enviado" not in df.columns:
        df["agradecimiento_enviado"] = ""

    return df[(df["Respuesta_Recibida"] == "SI") & (df["agradecimiento_enviado"] != "SI")]


def enviar_agradecimiento(driver: webdriver.Edge, telefono: str, mensaje: str) -> bool:
    """Abre el chat de un contacto y envía el mensaje de agradecimiento.

    Returns
    -------
    bool
        ``True`` si se envió correctamente, ``False`` en caso de error.
    """
    driver.get(f"https://web.whatsapp.com/send/?phone={telefono}")
    try:
        wait = WebDriverWait(driver, ESPERA_CAJA_TEXTO_SEGUNDOS)
        caja = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[@contenteditable='true']")))

        for char in mensaje:
            caja.send_keys(char)
            time.sleep(random.uniform(0.03, 0.07))

        time.sleep(1.2)
        caja.send_keys(Keys.ENTER)
        return True
    except Exception:  # noqa: BLE001
        return False


def enviar_agradecimientos() -> None:
    """Punto de entrada: envía agradecimientos y guarda el Excel final."""
    asegurar_directorios(DATA_RESPUESTA_FINAL_DIR)

    if not ARCHIVO_RESPUESTAS.exists():
        logger.error("No se encontró el archivo de respuestas en %s", ARCHIVO_RESPUESTAS)
        return

    df = pd.read_excel(ARCHIVO_RESPUESTAS)
    pendientes = obtener_pendientes(df)

    if pendientes.empty:
        logger.info("No hay clientes pendientes de agradecimiento.")
        return

    logger.info("Se enviarán %d mensajes de agradecimiento.", len(pendientes))
    codigo_pais = input("Introduce el código de país (ej. 504): ").strip()

    driver = webdriver.Edge(
        service=webdriver.edge.service.Service(EdgeChromiumDriverManager().install())
    )
    driver.get("https://web.whatsapp.com")
    input("Escanea el QR y presiona ENTER para continuar...")

    try:
        for index, row in pendientes.iterrows():
            num_limpio = str(row["phone"]).replace(" ", "").replace("-", "")
            telefono = (
                num_limpio if num_limpio.startswith(codigo_pais) else f"{codigo_pais}{num_limpio}"
            )
            mensaje = random.choice(MENSAJES_AGRADECIMIENTO)

            if enviar_agradecimiento(driver, telefono, mensaje):
                df.at[index, "agradecimiento_enviado"] = "SI"
                logger.info("Agradecimiento enviado a %s", row.get("name", "Cliente"))
                time.sleep(random.randint(*PAUSA_ENTRE_ENVIOS))
            else:
                logger.warning("Error con %s: no se pudo abrir el chat.", row.get("name", "Cliente"))
    finally:
        driver.quit()

    df.to_excel(ARCHIVO_FINAL, index=False)
    logger.info("Proceso finalizado. Archivo de agradecimientos guardado en: %s", ARCHIVO_FINAL)


if __name__ == "__main__":
    try:
        enviar_agradecimientos()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ocurrió un error durante el envío de agradecimientos: %s", exc)
