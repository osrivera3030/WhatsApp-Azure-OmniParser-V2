"""
pipeline.py - Orquestador del módulo de IA local.

Pipeline general
----------------
    Data_cleaning -> validacion_st -> envio_stg -> [ia_local.pipeline] -> envo_stg_save

Qué hace ``procesar_multimedia()``
------------------------------------
1. Recorre ``config.MEDIA_DIR`` (archivos multimedia descargados de chats
   de WhatsApp: notas de voz e imágenes).
2. Clasifica cada archivo por extensión (audio / imagen / desconocido).
3. Transcribe audio con Whisper (``audio_transcriber``) u extrae texto de
   imágenes con Tesseract (``image_ocr``).
4. Sobre el texto resultante, corre análisis de sentimiento y
   categorización (``text_analyzer``).
5. Sube el resultado combinado a Azure (``TABLAS["validada"]``).
6. Mueve el archivo procesado a ``MEDIA_DIR/procesados/``.

Qué hace ``analizar_respuestas_directorio()``
-----------------------------------------------
Complementa el flujo anterior: recorre las respuestas de texto ya
guardadas en ``TABLAS["directorio"]`` (columna ``Texto_Respuesta``, llenada
por ``4-Guarda_mjs_recep/envo_stg_save.py``) y les agrega sentimiento +
categoría, sin necesidad de que el cliente haya enviado un audio o imagen.

Cómo ejecutarlo
----------------
    python -m ia_local.pipeline
"""

from __future__ import annotations

import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from azure.data.tables import TableClient

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import (  # noqa: E402
    AZURE_CONNECTION_STRING,
    EXTENSIONES_AUDIO,
    EXTENSIONES_IMAGEN,
    MEDIA_DIR,
    PARTITION_KEY_HARWARD_STORE,
    TABLAS,
    asegurar_directorios,
    get_logger,
)
from ia_local.audio_transcriber import cargar_modelo, transcribir_audio  # noqa: E402
from ia_local.image_ocr import extraer_texto_imagen  # noqa: E402
from ia_local.text_analyzer import analizar_sentimiento, categorizar_por_palabras_clave  # noqa: E402

logger = get_logger(__name__)

PAUSA_ENTRE_ARCHIVOS_SEGUNDOS = 0.3


def tipo_archivo(ruta: Path) -> str:
    """Clasifica un archivo como ``"audio"``, ``"imagen"`` o ``"desconocido"`` por extensión."""
    ext = ruta.suffix.lower()
    if ext in EXTENSIONES_AUDIO:
        return "audio"
    if ext in EXTENSIONES_IMAGEN:
        return "imagen"
    return "desconocido"


def inferir_telefono(ruta: Path) -> str:
    """Extrae un número de teléfono del nombre de archivo (convención ``504XXXXXXXX_*``).

    Si el nombre no contiene una secuencia de 7 a 15 dígitos, se usa el
    nombre del archivo (sin extensión) como respaldo, para no perder el
    registro aunque no se identifique el teléfono.
    """
    match = re.search(r"(\d{7,15})", ruta.stem)
    return match.group(1) if match else ruta.stem


def guardar_resultado_multimedia(
    table_client: TableClient,
    phone: str,
    texto_extraido: str,
    tipo: str,
    nombre_archivo: str,
) -> None:
    """Sube a Azure el texto extraído de un archivo multimedia, con sentimiento y categoría.

    Parameters
    ----------
    table_client:
        Cliente conectado a ``TABLAS["validada"]``.
    phone:
        Teléfono inferido del nombre de archivo (se usa como ``RowKey``).
    texto_extraido:
        Texto obtenido por OCR o transcripción de audio.
    tipo:
        ``"audio"`` o ``"imagen"``.
    nombre_archivo:
        Nombre original del archivo (para trazabilidad).
    """
    sentimiento = analizar_sentimiento(texto_extraido) if texto_extraido else None
    categoria = categorizar_por_palabras_clave(texto_extraido) if texto_extraido else "sin_categoria"

    entidad = {
        "PartitionKey": PARTITION_KEY_HARWARD_STORE,
        "RowKey": re.sub(r"\D", "", phone) or phone,
        "phone": phone,
        "Texto_Multimedia": texto_extraido[:32000],  # límite de Azure Table Storage
        "Tipo_Multimedia": tipo,
        "Archivo_Analizado": nombre_archivo,
        "Fecha_Analisis": datetime.now().strftime("%Y-%m-%d"),
        "Hora_Analisis": datetime.now().strftime("%H:%M:%S"),
        "Tiene_Respuesta_Multimedia": "SI" if texto_extraido else "NO",
        "Categoria_Multimedia": categoria,
    }
    if sentimiento:
        entidad["Sentimiento_Etiqueta"] = sentimiento["etiqueta"]
        entidad["Sentimiento_Confianza"] = round(sentimiento["confianza"], 4)

    table_client.upsert_entity(entidad)


def procesar_multimedia() -> None:
    """Punto de entrada principal: analiza todo lo que haya en ``MEDIA_DIR``."""
    if not MEDIA_DIR.exists():
        logger.error("Carpeta de medios no existe: %s", MEDIA_DIR)
        logger.error("Crea la carpeta y coloca los archivos descargados de WhatsApp allí.")
        return

    archivos = [
        f for f in MEDIA_DIR.iterdir() if f.is_file() and tipo_archivo(f) != "desconocido"
    ]
    if not archivos:
        logger.info("No hay archivos multimedia para procesar.")
        return

    logger.info("Archivos encontrados: %d", len(archivos))

    # Cargar el modelo de Whisper una sola vez para todo el lote.
    modelo_whisper = cargar_modelo()

    table_client = TableClient.from_connection_string(AZURE_CONNECTION_STRING, TABLAS["validada"])
    try:
        table_client.create_table()
    except Exception:  # noqa: BLE001 - la tabla ya puede existir
        pass

    carpeta_ok = MEDIA_DIR / "procesados"
    asegurar_directorios(carpeta_ok)

    for ruta in archivos:
        tipo = tipo_archivo(ruta)
        logger.info("Analizando [%s]: %s", tipo.upper(), ruta.name)

        phone = inferir_telefono(ruta)
        texto = ""

        if tipo == "audio":
            texto = transcribir_audio(ruta, modelo_whisper)
            if texto:
                logger.info("Transcripción: %s", texto[:120])
            else:
                logger.warning("No se obtuvo transcripción para %s.", ruta.name)

        elif tipo == "imagen":
            texto = extraer_texto_imagen(ruta)
            if texto:
                logger.info("Texto OCR: %s", texto[:120])
            else:
                logger.warning("No se extrajo texto de la imagen %s.", ruta.name)

        try:
            guardar_resultado_multimedia(table_client, phone, texto, tipo, ruta.name)
            logger.info("Guardado en Azure -> RowKey: %s", re.sub(r"\D", "", phone))
        except Exception as exc:  # noqa: BLE001
            logger.error("Error al guardar en Azure: %s", exc)

        shutil.move(str(ruta), str(carpeta_ok / ruta.name))
        time.sleep(PAUSA_ENTRE_ARCHIVOS_SEGUNDOS)

    logger.info("Proceso terminado. Archivos movidos a: %s", carpeta_ok)


def analizar_respuestas_directorio() -> None:
    """Agrega sentimiento y categoría a las respuestas de texto ya guardadas.

    Recorre ``TABLAS["directorio"]`` y, para cada entidad con
    ``Texto_Respuesta`` no vacío y sin análisis previo
    (``Sentimiento_Etiqueta`` ausente), calcula sentimiento y categoría y
    actualiza la entidad (merge) en Azure.
    """
    table_client = TableClient.from_connection_string(AZURE_CONNECTION_STRING, TABLAS["directorio"])
    entidades = list(table_client.list_entities())

    pendientes = [
        e
        for e in entidades
        if e.get("Texto_Respuesta") and not e.get("Sentimiento_Etiqueta")
    ]
    logger.info("Respuestas pendientes de análisis de texto: %d", len(pendientes))

    for entidad in pendientes:
        texto = str(entidad["Texto_Respuesta"])
        sentimiento = analizar_sentimiento(texto)
        categoria = categorizar_por_palabras_clave(texto)

        entidad["Sentimiento_Etiqueta"] = sentimiento["etiqueta"]
        entidad["Sentimiento_Confianza"] = round(sentimiento["confianza"], 4)
        entidad["Categoria_Respuesta"] = categoria

        table_client.update_entity(mode="merge", entity=entidad)
        logger.info(
            "Analizado %s -> sentimiento=%s, categoria=%s",
            entidad.get("phone", entidad.get("RowKey")),
            sentimiento["etiqueta"],
            categoria,
        )

    logger.info("Análisis de respuestas de texto finalizado.")


if __name__ == "__main__":
    try:
        procesar_multimedia()
        analizar_respuestas_directorio()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ocurrió un error en el pipeline de IA local: %s", exc)
