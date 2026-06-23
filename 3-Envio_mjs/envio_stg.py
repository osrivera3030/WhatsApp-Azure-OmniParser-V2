"""
envio_stg.py - Envío de mensajes iniciales a contactos validados.

Qué hace
--------
1. Lee ``TABLAS["directorio"]`` de Azure y filtra contactos con
   ``Tiene_WhatsApp == "SI"`` que aún no tienen ``fecha_envio_msg``.
2. Pide al usuario cuántos mensajes enviar y el código de país.
3. Envía, vía WhatsApp Web (Selenium), un mensaje elegido al azar de
   ``MENSAJES_COTIZACION`` a cada contacto pendiente.
4. Marca cada contacto como enviado en Azure (mensaje, fecha y hora).

Cómo ejecutarlo
----------------
    python "3-Envio_mjs/envio_stg.py"

Requiere una sesión de WhatsApp Web ya autenticada en el perfil de Edge
configurado (ver ``2-Validation_wtp/validacion_st.py``).

Nota de alcance
-----------------
Este script automatiza el envío masivo de mensajes vía WhatsApp Web con
Selenium, lo cual está fuera de los Términos de Servicio oficiales de
WhatsApp. Este refactor solo reorganiza y documenta el código existente:
no se modificó ni se reforzó la lógica de automatización/envío.
"""

from __future__ import annotations

import random
import sys
import time
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from azure.data.tables import TableClient

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import AZURE_CONNECTION_STRING, PERFIL_WHATSAPP_DIR, TABLAS, get_logger  # noqa: E402

logger = get_logger(__name__)

ESPERA_CARGA_SESION_SEGUNDOS = 20
ESPERA_CAJA_TEXTO_SEGUNDOS = 90
PAUSA_ENTRE_ENVIOS = (30, 60)  # rango en segundos (min, max)

# Variantes del mismo mensaje de cotización (se elige una al azar por envío).
MENSAJES_COTIZACION: list[str] = [
    "Buenos días. Quería consultar qué marcas de cemento tiene disponibles y a "
    "cuánto está la bolsa. Estaría ocupando unas 20 bolsas y yo mismo pasaría a "
    "recogerlas. Muchas gracias.",
    "Buen día. ¿Me podría indicar qué marcas de cemento maneja y cuál es el "
    "precio por bolsa? Necesito 20 bolsas y puedo pasar por ellas directamente. "
    "Gracias.",
    "Hola, buenos días. Estoy buscando cemento y quisiera saber qué marcas tiene "
    "y cuánto cuesta cada bolsa. Yo pasaría a recoger unas 20 bolsas. Muchas "
    "gracias.",
    "Buenas. ¿Me podría compartir el precio de la bolsa de cemento y las marcas "
    "que tiene disponibles? Ocupo 20 bolsas y yo me encargaría de retirarlas. "
    "Gracias.",
    "Buen día, una consulta. ¿Qué marcas de cemento tiene en existencia y a cómo "
    "vende la bolsa? Necesito alrededor de 20 bolsas y puedo pasar a traerlas "
    "personalmente.",
    "Hola, ¿cómo está? Quería cotizar cemento. ¿Me puede indicar qué marcas "
    "maneja y el precio por bolsa? Estaría necesitando 20 bolsas y yo pasaría "
    "por ellas.",
    "Buenos días. Estoy interesado en comprar unas 20 bolsas de cemento. ¿Qué "
    "marcas tiene disponibles y cuál es el precio por bolsa? Yo mismo las "
    "recogería. Gracias.",
    "Buenas tardes. ¿Me podría informar qué marcas de cemento tiene y cuánto "
    "cuesta la bolsa? Ocupo 20 bolsas y pasaría a retirarlas personalmente. "
    "Muchas gracias.",
    "Buen día. Quisiera saber el precio actual de la bolsa de cemento y las "
    "marcas que tiene disponibles. Necesito 20 bolsas y yo puedo pasar a "
    "recogerlas.",
    "Hola. Estoy consultando precios de cemento. ¿Qué marcas tiene disponibles y "
    "a cuánto vende la bolsa? Ocupo unas 20 bolsas y yo pasaría por ellas. "
    "Gracias.",
    "Buenos días. ¿Me podría brindar información sobre las marcas de cemento que "
    "maneja y el valor de cada bolsa? Necesito 20 bolsas y puedo retirarlas "
    "personalmente.",
    "Buenas. Estoy buscando comprar cemento y quisiera conocer las marcas "
    "disponibles y el precio por bolsa. Ocupo 20 bolsas y yo mismo las "
    "recogería. Gracias.",
    "Buen día. ¿Qué marcas de cemento tiene actualmente y cuál es el precio de "
    "la bolsa? Necesito unas 20 bolsas y puedo pasar a traerlas cuando estén "
    "disponibles.",
    "Hola, buenos días. Quisiera cotizar 20 bolsas de cemento. ¿Me podría "
    "indicar qué marcas tiene y cuánto cuesta cada bolsa? Yo pasaría a "
    "recogerlas. Muchas gracias.",
    "Saludos. ¿Me podría compartir los precios del cemento y las marcas que "
    "maneja? Estoy interesado en comprar 20 bolsas y yo mismo pasaría por "
    "ellas. Gracias.",
]


def obtener_pendientes(table_client: TableClient) -> list[dict]:
    """Filtra las entidades del directorio que ya tienen WhatsApp y no han recibido mensaje.

    Parameters
    ----------
    table_client:
        Cliente conectado a ``TABLAS["directorio"]``.

    Returns
    -------
    list[dict]
        Entidades con ``Tiene_WhatsApp == "SI"`` y sin ``fecha_envio_msg``.
    """
    entidades = list(table_client.list_entities())
    return [
        e
        for e in entidades
        if str(e.get("Tiene_WhatsApp", "")).upper() == "SI" and not e.get("fecha_envio_msg")
    ]


def iniciar_driver() -> webdriver.Edge:
    """Inicializa Edge con el perfil persistente de WhatsApp Web."""
    options = webdriver.EdgeOptions()
    options.add_argument(f"user-data-dir={PERFIL_WHATSAPP_DIR}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    servicio = webdriver.edge.service.Service(EdgeChromiumDriverManager().install())
    return webdriver.Edge(service=servicio, options=options)


def enviar_mensaje(driver: webdriver.Edge, telefono: str, mensaje: str) -> bool:
    """Abre el chat de un contacto y envía un mensaje carácter por carácter.

    Parameters
    ----------
    driver:
        Navegador con sesión activa de WhatsApp Web.
    telefono:
        Número completo (código de país + número) sin formato.
    mensaje:
        Texto a enviar.

    Returns
    -------
    bool
        ``True`` si el mensaje se envió correctamente, ``False`` si no se
        pudo abrir el chat o escribir el mensaje a tiempo.
    """
    driver.get(f"https://web.whatsapp.com/send/?phone={telefono}")
    try:
        wait = WebDriverWait(driver, ESPERA_CAJA_TEXTO_SEGUNDOS)
        caja = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")
            )
        )
        time.sleep(1.5)
        for char in mensaje:
            caja.send_keys(char)
            time.sleep(random.uniform(0.01, 0.03))

        caja.send_keys(Keys.ENTER)
        return True
    except Exception:  # noqa: BLE001
        return False


def enviar_mensajes() -> None:
    """Punto de entrada: envía mensajes de cotización a los contactos pendientes."""
    table_client = TableClient.from_connection_string(
        AZURE_CONNECTION_STRING, TABLAS["directorio"]
    )
    pendientes = obtener_pendientes(table_client)

    logger.info("Contactos listos para recibir mensaje: %d", len(pendientes))
    if not pendientes:
        return

    limite = int(input(f"¿Cuántos mensajes enviar? (Máx {len(pendientes)}): "))
    pendientes = pendientes[:limite]
    codigo_pais = input("Introduce código de país (ej. 504): ").strip()

    driver = iniciar_driver()
    driver.get("https://web.whatsapp.com")
    logger.info("Esperando carga de sesión (%d segundos)...", ESPERA_CARGA_SESION_SEGUNDOS)
    time.sleep(ESPERA_CARGA_SESION_SEGUNDOS)

    try:
        for entidad in pendientes:
            telefono = entidad["RowKey"]
            mensaje = random.choice(MENSAJES_COTIZACION)

            if enviar_mensaje(driver, f"{codigo_pais}{telefono}", mensaje):
                ahora = datetime.now()
                entidad["mensaje_enviado"] = mensaje
                entidad["fecha_envio_msg"] = ahora.strftime("%Y-%m-%d")
                entidad["hora_envio_msg"] = ahora.strftime("%H:%M:%S")
                entidad["Respuesta_Recibida"] = "NO"

                table_client.upsert_entity(entidad)
                logger.info("Enviado a %s. Marcado en Azure.", telefono)
                time.sleep(random.randint(*PAUSA_ENTRE_ENVIOS))
            else:
                logger.warning("Error con %s: no se pudo abrir el chat.", telefono)
    finally:
        driver.quit()

    logger.info("Tanda finalizada.")


if __name__ == "__main__":
    try:
        enviar_mensajes()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ocurrió un error durante el envío: %s", exc)
