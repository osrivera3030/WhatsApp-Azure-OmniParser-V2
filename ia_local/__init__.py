"""
ia_local - Módulo de Inteligencia Artificial 100% local del pipeline.

Sub-módulos
-----------
image_ocr
    Extracción de texto de imágenes con Tesseract (OCR offline).
audio_transcriber
    Transcripción de notas de voz con Whisper (modelo local, sin nube).
text_analyzer
    Análisis de sentimiento/categorización de texto con un modelo de
    Hugging Face Transformers ejecutado localmente.
pipeline
    Orquestador: recorre los archivos multimedia descargados de WhatsApp,
    decide qué analizador usar según el tipo de archivo, y guarda el
    resultado en Azure Table Storage.

Todo el procesamiento ocurre en la máquina local. Los modelos de Whisper y
Transformers se descargan una sola vez desde Hugging Face / OpenAI la
primera vez que se usan y luego quedan cacheados para correr sin conexión.

Uso típico
----------
    from ia_local.pipeline import procesar_multimedia
    procesar_multimedia()
"""

from . import audio_transcriber, image_ocr, pipeline, text_analyzer

__all__ = ["audio_transcriber", "image_ocr", "pipeline", "text_analyzer"]
