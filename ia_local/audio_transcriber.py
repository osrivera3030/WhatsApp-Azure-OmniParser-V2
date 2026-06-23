"""
audio_transcriber.py - Transcripción local de notas de voz con Whisper.

Migración desde Vosk
---------------------
La versión anterior de este módulo usaba Vosk (modelo pequeño en español).
Se migró a `openai-whisper <https://github.com/openai/whisper>`_ corriendo
100% local en CPU/GPU, según las instrucciones del proyecto. Ventajas:
mejor precisión y manejo nativo de múltiples formatos de audio (ogg, mp4,
aac, opus, etc.) sin tener que convertir manualmente a WAV (Whisper usa
ffmpeg internamente).

Requisitos
----------
* ``pip install openai-whisper`` (ver requirements.txt).
* Tener ``ffmpeg`` instalado en el sistema y disponible en el PATH.
* La primera vez que se usa un tamaño de modelo, Whisper lo descarga desde
  internet y lo cachea localmente (``~/.cache/whisper``); las ejecuciones
  posteriores son 100% offline.

Uso
---
    from ia_local.audio_transcriber import transcribir_audio
    texto = transcribir_audio(Path("nota_voz.ogg"))
"""

from __future__ import annotations

import sys
from pathlib import Path

import whisper

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import WHISPER_LANGUAGE, WHISPER_MODEL_SIZE, get_logger  # noqa: E402

logger = get_logger(__name__)

# Caché del modelo en memoria del proceso: cargarlo es costoso, así que se
# hace una sola vez y se reutiliza en cada llamada a transcribir_audio().
_modelo_cache: whisper.Whisper | None = None


def cargar_modelo(tamano: str = WHISPER_MODEL_SIZE) -> whisper.Whisper:
    """Carga (o reutiliza) el modelo de Whisper en memoria.

    Parameters
    ----------
    tamano:
        Tamaño del modelo de Whisper (``tiny``, ``base``, ``small``,
        ``medium``, ``large``). Por defecto usa ``config.WHISPER_MODEL_SIZE``.

    Returns
    -------
    whisper.Whisper
        Instancia del modelo cargado.
    """
    global _modelo_cache
    if _modelo_cache is None:
        logger.info("Cargando modelo Whisper '%s' (puede tardar la primera vez)...", tamano)
        _modelo_cache = whisper.load_model(tamano)
    return _modelo_cache


def transcribir_audio(ruta_audio: Path, modelo: whisper.Whisper | None = None) -> str:
    """Transcribe un archivo de audio a texto usando Whisper local.

    Acepta directamente formatos comunes de WhatsApp (.ogg, .opus, .mp4,
    .aac, .m4a, .wav, .mp3); Whisper se encarga de la decodificación vía
    ffmpeg internamente, sin pasos de conversión manual.

    Parameters
    ----------
    ruta_audio:
        Ruta al archivo de audio a transcribir.
    modelo:
        Instancia ya cargada de Whisper. Si se omite, se carga/reutiliza el
        modelo cacheado por ``cargar_modelo()``.

    Returns
    -------
    str
        Texto transcrito, o cadena vacía si falla la transcripción.
    """
    modelo = modelo or cargar_modelo()

    try:
        resultado = modelo.transcribe(str(ruta_audio), language=WHISPER_LANGUAGE)
        return str(resultado.get("text", "")).strip()
    except Exception as exc:  # noqa: BLE001
        logger.error("Error transcribiendo audio %s: %s", ruta_audio.name, exc)
        return ""
