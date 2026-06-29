"""
audio_transcriber.py - Transcripción de notas de voz: Azure STT (principal) + Whisper (fallback).

Estrategia de transcripción
-----------------------------
1. **Azure Cognitive Services Speech-to-Text** (método principal):
   - Envía los bytes del .ogg directamente a la API REST de Azure STT.
   - No requiere modelo local ni GPU; responde en ~1-2 segundos.
   - Idioma: es-HN (español Honduras). Configurable en config.py.
   - Flujo:
       a. POST /sts/v1.0/issueToken  → access_token (válido 10 min)
       b. POST /speech/recognition/… con los bytes del audio
       c. JSON { "DisplayText": "texto transcrito" }

2. **Whisper local** (fallback):
   - Se usa solo si Azure STT falla (sin internet, cuota agotada, etc.).
   - Corre 100% offline en CPU/GPU, más lento en CPU pero confiable.

Origen del código de Azure STT
--------------------------------
Adaptado del ejemplo de Juan Fernando Ramírez / SCITIS GROUP (bot de
Telegram + Azure Cognitive Services). La diferencia es que aquí el audio
ya está disponible como archivo en disco (capturado vía hook decodeAudioData
en WhatsApp Web), por lo que no hay paso de descarga desde Telegram ni
subida previa a Blob Storage.

Requisitos
----------
* ``pip install openai-whisper requests`` (ver requirements.txt).
* ``ffmpeg`` en PATH (solo para el fallback de Whisper).
* ``AZURE_SPEECH_KEY`` y ``AZURE_SPEECH_REGION`` en config.py.
"""

from __future__ import annotations

import http.client
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import (  # noqa: E402
    AZURE_SPEECH_KEY,
    AZURE_SPEECH_REGION,
    AZURE_SPEECH_LANGUAGE,
    WHISPER_LANGUAGE,
    WHISPER_MODEL_SIZE,
    get_logger,
)

logger = get_logger(__name__)

# ── Caché de Whisper ──────────────────────────────────────────────────────
_modelo_whisper_cache = None


def _cargar_whisper(tamano: str = WHISPER_MODEL_SIZE):
    global _modelo_whisper_cache
    if _modelo_whisper_cache is None:
        import whisper
        logger.info("Cargando modelo Whisper '%s' (fallback)...", tamano)
        _modelo_whisper_cache = whisper.load_model(tamano)
    return _modelo_whisper_cache


# ── Azure STT ─────────────────────────────────────────────────────────────

def _obtener_token_azure() -> str:
    """Obtiene un access token de Azure Cognitive Services (válido 10 min)."""
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
    }
    conn = http.client.HTTPSConnection(f"{AZURE_SPEECH_REGION}.api.cognitive.microsoft.com")
    conn.request("POST", "/sts/v1.0/issueToken", None, headers)
    response = conn.getresponse()
    token = response.read().decode("utf-8")
    conn.close()
    return token


def transcribir_audio_azure(ruta_audio: Path) -> str:
    """Transcribe un archivo .ogg/.opus usando Azure Cognitive Services STT.

    Parameters
    ----------
    ruta_audio:
        Ruta al archivo de audio (.ogg con codecs=opus, formato de WhatsApp).

    Returns
    -------
    str
        Texto transcrito, o cadena vacía si falla.
    """
    try:
        audio_bytes = ruta_audio.read_bytes()
        token = _obtener_token_azure()

        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json;text/xml",
            "Content-Type": "audio/ogg; codecs=opus",
        }
        endpoint = f"{AZURE_SPEECH_REGION}.stt.speech.microsoft.com"
        path = (
            f"/speech/recognition/conversation/cognitiveservices/v1"
            f"?language={AZURE_SPEECH_LANGUAGE}&format=simple"
        )
        conn = http.client.HTTPSConnection(endpoint)
        conn.request("POST", path, audio_bytes, headers)
        response = conn.getresponse()
        res = json.loads(response.read().decode("utf-8"))
        conn.close()

        texto = res.get("DisplayText", "").strip()
        if texto:
            logger.info("Azure STT OK: %s", texto[:80])
        else:
            logger.warning("Azure STT devolvió respuesta vacía. Status: %s", res.get("RecognitionStatus"))
        return texto

    except Exception as exc:
        logger.warning("Azure STT falló (%s), usando Whisper como fallback.", exc)
        return ""


# ── Whisper (fallback) ────────────────────────────────────────────────────

def _transcribir_whisper(ruta_audio: Path) -> str:
    """Transcribe con Whisper local. Se llama solo si Azure STT falla."""
    try:
        modelo = _cargar_whisper()
        resultado = modelo.transcribe(str(ruta_audio), language=WHISPER_LANGUAGE)
        texto = str(resultado.get("text", "")).strip()
        if texto:
            logger.info("Whisper OK: %s", texto[:80])
        return texto
    except Exception as exc:
        logger.error("Whisper también falló: %s", exc)
        return ""


# Alias público para compatibilidad con pipeline.py que importa cargar_modelo
def cargar_modelo(tamano: str = WHISPER_MODEL_SIZE):
    """Alias público de _cargar_whisper() para compatibilidad con pipeline.py."""
    return _cargar_whisper(tamano)


# ── Función principal ─────────────────────────────────────────────────────

def transcribir_audio(ruta_audio: Path, **_kwargs) -> str:
    """Transcribe un archivo de audio: Azure STT primero, Whisper si falla.

    Parameters
    ----------
    ruta_audio:
        Ruta al archivo de audio (.ogg, .opus, .mp4, .aac, .m4a, .wav, .mp3).

    Returns
    -------
    str
        Texto transcrito, o cadena vacía si ambos métodos fallan.
    """
    if not ruta_audio.exists():
        logger.error("Archivo de audio no encontrado: %s", ruta_audio)
        return ""

    # Intento 1: Azure STT (rápido, cloud)
    if AZURE_SPEECH_KEY:
        texto = transcribir_audio_azure(ruta_audio)
        if texto:
            return texto

    # Intento 2: Whisper local (offline, más lento)
    logger.info("Usando Whisper local para %s", ruta_audio.name)
    return _transcribir_whisper(ruta_audio)
