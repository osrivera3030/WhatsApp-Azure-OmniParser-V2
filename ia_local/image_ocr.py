"""
image_ocr.py - Extracción de texto de imágenes con Tesseract (OCR local).

Todo el procesamiento ocurre en la máquina local; no se sube ninguna
imagen a un servicio externo. Requiere tener instalado el binario de
Tesseract OCR en el sistema (no solo el paquete de Python ``pytesseract``):
https://github.com/UB-Mannheim/tesseract/wiki (Windows).

Si ves el error "tesseract is not installed or it's not in your PATH"
después de instalarlo, escribe la ruta exacta del ejecutable en
``config.TESSERACT_CMD`` (ver comentario en ``config.py``) en vez de
depender del PATH de Windows.

Uso
---
    from ia_local.image_ocr import extraer_texto_imagen
    texto = extraer_texto_imagen(Path("foto_recibo.jpg"))
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytesseract
from PIL import Image

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import OCR_IDIOMAS, OCR_MIN_CONFIDENCE, TESSERACT_CMD, get_logger  # noqa: E402

logger = get_logger(__name__)

ANCHO_MINIMO_PX = 800

# Si el usuario configuró una ruta explícita (porque el binario no está en
# el PATH del sistema), se la indicamos a pytesseract aquí, una sola vez.
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


def mejorar_imagen(imagen: Image.Image) -> Image.Image:
    """Pre-procesa una imagen para mejorar la precisión del OCR.

    Escala imágenes pequeñas (equivalente a más DPI) y convierte a escala
    de grises, lo que típicamente mejora el reconocimiento de Tesseract.

    Parameters
    ----------
    imagen:
        Imagen original cargada con Pillow.

    Returns
    -------
    Image.Image
        Imagen procesada en escala de grises.
    """
    ancho, alto = imagen.size
    if ancho < ANCHO_MINIMO_PX:
        factor = ANCHO_MINIMO_PX / ancho
        imagen = imagen.resize((int(ancho * factor), int(alto * factor)), Image.LANCZOS)
    return imagen.convert("L")


def extraer_texto_imagen(ruta_imagen: Path) -> str:
    """Aplica OCR a una imagen y devuelve el texto reconocido con confianza suficiente.

    Parameters
    ----------
    ruta_imagen:
        Ruta al archivo de imagen (jpg, png, webp, bmp, tiff, etc.).

    Returns
    -------
    str
        Texto extraído (palabras con confianza >= ``config.OCR_MIN_CONFIDENCE``),
        o cadena vacía si no se detectó texto o falló el procesamiento.
    """
    try:
        imagen = Image.open(ruta_imagen)
        imagen = mejorar_imagen(imagen)

        datos = pytesseract.image_to_data(
            imagen, lang=OCR_IDIOMAS, output_type=pytesseract.Output.DICT
        )

        palabras_fiables = [
            datos["text"][i]
            for i in range(len(datos["text"]))
            if int(datos["conf"][i]) >= OCR_MIN_CONFIDENCE and datos["text"][i].strip()
        ]
        return " ".join(palabras_fiables).strip()

    except Exception as exc:  # noqa: BLE001
        logger.error("Error en OCR para %s: %s", ruta_imagen.name, exc)
        return ""
