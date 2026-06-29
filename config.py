"""
config.py - Configuración centralizada del pipeline "solo_nube".

Este módulo es la única fuente de verdad para:
    * Cadena de conexión y nombres de tablas de Azure Table Storage.
    * Rutas de datos, medios y modelos usados por el pipeline.
    * Configuración del logger común a todos los scripts.

Cómo usarlo
-----------
Cualquier script del proyecto debe importar lo que necesite desde aquí en
lugar de declarar sus propias constantes:

    from config import AZURE_CONNECTION_STRING, TABLAS, DATA_DIR, get_logger

    logger = get_logger(__name__)

Notas sobre seguridad
----------------------
La cadena de conexión sigue hardcodeada en este archivo por decisión
explícita del usuario (no se migró a variables de entorno / .env). Si en
algún momento se comparte este código o se sube a un repositorio, se
recomienda rotar la clave de la cuenta de Azure, ya que quedaría expuesta
en texto plano.

Mapa del pipeline y flujo de tablas de Azure
---------------------------------------------
    1. 1-Data_cleaning   -> escribe en TABLAS["cruda"] y TABLAS["limpia"]
    2. 2-Validation_wtp  -> lee TABLAS["limpia"], escribe TABLAS["validada"]
    3. 3-Envio_mjs       -> lee/escribe TABLAS["directorio"]
    4. 4-Guarda_mjs_recep-> lee/escribe TABLAS["directorio"]
    5. 5-envio_mjs_repuesta -> escribe TABLAS["monitoreo_precios"]
    6. 6-Data_storege    -> copia TABLAS["directorio"] -> TABLAS["historico"]

ATENCIÓN: el script de validación (paso 2) escribe en "DataValidadaWtp",
mientras que el script de envío (paso 3) lee de "Directoriowtpp". Son tablas
distintas en la cuenta de Azure actual. Si tu flujo de trabajo depende de
que los datos validados lleguen al directorio de envío, asegúrate de
sincronizar/renombrar esa tabla manualmente entre ambos pasos: este
refactor no modifica esa lógica, solo documenta el comportamiento existente.
"""

from __future__ import annotations

import logging
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────
# RUTAS BASE
# ──────────────────────────────────────────────────────────────────────────
# PROJECT_ROOT = carpeta "desarrollo" (un nivel arriba de "solo_nube - Copy").
# Calculado a partir de la ubicación de este archivo para que el pipeline
# funcione sin importar desde qué directorio se invoque cada script.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
SOLO_NUBE_DIR: Path = Path(__file__).resolve().parent

DATA_DIR: Path = PROJECT_ROOT / "Data"
MEDIA_DIR: Path = SOLO_NUBE_DIR / "media_whatsapp"
MODELS_DIR: Path = SOLO_NUBE_DIR / "models"
PERFIL_WHATSAPP_DIR: Path = SOLO_NUBE_DIR / "perfil_whatsapp"

# Subcarpetas de datos usadas por las distintas fases del pipeline.
DATA_LIMPIA_DIR: Path = DATA_DIR / "Data_limpia"
DATA_MJS_RECIBIDOS_DIR: Path = DATA_DIR / "Data_mjs_reci"
DATA_RESPUESTA_FINAL_DIR: Path = DATA_DIR / "data_repuesta_final"

# ──────────────────────────────────────────────────────────────────────────
# AZURE TABLE STORAGE
# ──────────────────────────────────────────────────────────────────────────
AZURE_CONNECTION_STRING: str = (
    "DefaultEndpointsProtocol=https;"
    "AccountName=azurefuncprojectsjfers;"
    "AccountKey=unJ5ktFFjq3XxZ9174Uy0tCF5zEsMOlmWEzDMtc9v6WZDsDxuwwzsABD"
    "+0D1HThrwcCKufna1WvdFnmHX3qjDQ==;"
    "EndpointSuffix=core.windows.net"
)

# Azure Blob Storage — contenedor donde se guardan imágenes y audios de WhatsApp.
# Usa la misma cuenta (AZURE_CONNECTION_STRING). El contenedor debe existir
# previamente en el portal de Azure (o se crea automáticamente con exist_ok).
AZURE_BLOB_CONTAINER: str = "media-whatsapp"

# Nombres de tabla agrupados en un diccionario para evitar duplicar strings
# sueltos en cada script (una sola fuente de verdad por tabla).
TABLAS: dict[str, str] = {
    "cruda": "ContenedorDatawtp",          # Datos crudos recién importados.
    "limpia": "Datacleaningwtp",           # Datos limpios (teléfono validado).
    "validada": "DataValidadaWtp",         # Resultado de validación de WhatsApp.
    "directorio": "prueba",        # Directorio usado para envío/respuesta.
    "historico": "Historicowtp",           # Histórico (copia + reseteo de 15 días).
    "monitoreo_precios": "MonitoreoPrecios1",  # Reporte final de agradecimientos.
}

# PartitionKey usado de forma consistente en las tablas relacionadas con la
# tienda Harward / Argos.
PARTITION_KEY_HARWARD_STORE: str = "HarwardStore"
PARTITION_KEY_REPORTE_ARGOS: str = "Reporte_Argos"

# Días que deben pasar desde el envío de un mensaje antes de resetear el
# estado de seguimiento de un contacto (usado en 6-Data_storege).
DIAS_RESET_SEGUIMIENTO: int = 15

# ──────────────────────────────────────────────────────────────────────────
# IA LOCAL
# ──────────────────────────────────────────────────────────────────────────
# Confianza mínima (0-100) para aceptar una palabra detectada por OCR.
OCR_MIN_CONFIDENCE: int = 40
OCR_IDIOMAS: str = "spa+eng"

# Ruta al ejecutable de Tesseract OCR. pytesseract es solo el conector de
# Python: el motor de OCR hay que instalarlo aparte en el sistema
# (https://github.com/UB-Mannheim/tesseract/wiki en Windows).
#
# Si después de instalarlo sigues viendo el error
# "tesseract is not installed or it's not in your PATH", lo más rápido es
# escribir aquí la ruta exacta donde quedó instalado, en vez de depender de
# que el PATH de Windows esté bien configurado. Ejemplo típico en Windows:
#
#     TESSERACT_CMD: str | None = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
#
# Déjalo en None si Tesseract ya funciona desde cualquier terminal (es decir,
# si el comando `tesseract --version` te responde sin error).
TESSERACT_CMD: str | None = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ── Azure Cognitive Services Speech-to-Text ───────────────────────────────
# Método PRINCIPAL de transcripción (más rápido que Whisper, corre en cloud).
# Adaptado del ejemplo de Juan Fernando Ramírez / SCITIS GROUP.
# Flujo: POST issueToken → POST audio/ogg → JSON { DisplayText }
#
# AZURE_SPEECH_KEY:      clave del servicio (Ocp-Apim-Subscription-Key).
# AZURE_SPEECH_REGION:   región del recurso en Azure (ej. "eastus").
# AZURE_SPEECH_LANGUAGE: idioma BCP-47 (es-HN Honduras, es-CO Colombia...).
#
# Si AZURE_SPEECH_KEY está vacío, audio_transcriber.py usa Whisper local.
AZURE_SPEECH_KEY: str      = "e6232e11c3f04bc292f59cb28949f2ad"
AZURE_SPEECH_REGION: str   = "eastus"
AZURE_SPEECH_LANGUAGE: str = "es-HN"

# Tamaño de modelo de Whisper a usar para transcripción de audio offline.
# Opciones típicas: "tiny", "base", "small", "medium", "large".
# "base" es un buen punto de partida en CPU; "small" mejora precisión a
# costa de más tiempo de cómputo.
WHISPER_MODEL_SIZE: str = "base"
WHISPER_LANGUAGE: str = "es"

# Modelo de Hugging Face Transformers para análisis de sentimiento en
# español, usado de forma local (se descarga una vez y luego corre offline).
TEXT_SENTIMENT_MODEL: str = "pysentimiento/robertuito-sentiment-analysis"

EXTENSIONES_AUDIO: set[str] = {".ogg", ".mp3", ".mp4", ".aac", ".m4a", ".wav", ".opus"}
EXTENSIONES_IMAGEN: set[str] = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}

# ──────────────────────────────────────────────────────────────────────────
# OMNIPARSER V2 (detección visual del botón de "reproducir audio")
# ──────────────────────────────────────────────────────────────────────────
# Motivo: el selector por ``aria-label`` (ver procesar_respuestas.py) no
# siempre logra descargar el audio -- a veces el clic no cae sobre el botón
# real o WhatsApp reordena el DOM. OmniParser V2 (microsoft/OmniParser)
# resuelve esto detectando visualmente el botón a partir de una captura de
# pantalla, igual que lo haría una persona mirando el chat, en vez de
# depender de atributos del DOM que pueden romperse.
#
# OmniParser V2 NO es un paquete de pip: hay que clonar su repositorio y
# descargar los pesos por separado (ver
# ``ia_local/omniparser_detector.py`` y ``requirements-omniparser.txt`` para
# las instrucciones completas). Esta sección solo deja la configuración de
# rutas/umbrales en un solo lugar.

# Carpeta donde clonaste https://github.com/microsoft/OmniParser (debe
# contener `util/utils.py` y, dentro de `weights/`, `icon_detect/model.pt` y
# `icon_caption_florence/`). Ajusta esta ruta si vuelves a clonar el repo en
# otro lugar -- por ahora apunta a donde el usuario lo clonó realmente.
OMNIPARSER_DIR: Path = Path(r"C:\Users\Oscar\Desktop\Nueva_prueba\OmniParser")
OMNIPARSER_WEIGHTS_DIR: Path = OMNIPARSER_DIR / "weights"
OMNIPARSER_ICON_DETECT_MODEL: Path = OMNIPARSER_WEIGHTS_DIR / "icon_detect" / "model.pt"
OMNIPARSER_ICON_CAPTION_DIR: Path = OMNIPARSER_WEIGHTS_DIR / "icon_caption_florence"

# Dispositivo para OmniParser: se detecta automáticamente.
# CUDA (NVIDIA) > CPU. Si tienes GPU NVIDIA con drivers instalados
# y torch fue instalado con soporte CUDA, se usará automáticamente.
def _detectar_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"

OMNIPARSER_DEVICE: str = _detectar_device()

# Confianza mínima (0-1) para que YOLO considere un ícono como detección
# válida. 0.05 es más permisivo que el valor por defecto del repo (0.01 es
# el de la demo, pero genera demasiado ruido); ajusta si detecta de más o
# de menos.
OMNIPARSER_BOX_THRESHOLD: float = 0.05

# Palabras (en inglés, porque Florence-2 genera sus descripciones en
# inglés sin importar el idioma de la cuenta de WhatsApp) que, si aparecen
# en la descripción generada para un ícono, lo consideran candidato a
# "botón de reproducir audio". Lista deliberadamente amplia: es preferible
# un falso positivo ocasional (se descarta solo si el clic no produce audio)
# a no detectar el botón real.
OMNIPARSER_PALABRAS_CLAVE_AUDIO: tuple[str, ...] = (
    "play", "audio", "voice", "sound", "speaker", "microphone", "mic",
    "headphone", "music", "wave", "volume",
    "triangle", "arrow", "playback", "record", "listen",
)


# ──────────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────────
_LOG_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
_LOG_DATEFMT = "%H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    """Devuelve un logger configurado de forma consistente para todo el pipeline.

    Parameters
    ----------
    name:
        Nombre del logger, normalmente ``__name__`` del módulo que lo solicita.

    Returns
    -------
    logging.Logger
        Logger con un único ``StreamHandler`` y formato uniforme. Si el
        logger ya tenía handlers configurados (por ejemplo en tests), no se
        agregan handlers duplicados.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def asegurar_directorios(*rutas: Path) -> None:
    """Crea cada ruta de ``rutas`` (y sus padres) si todavía no existe.

    Utilidad común para que cada script no repita su propio ``mkdir``.
    """
    for ruta in rutas:
        ruta.mkdir(parents=True, exist_ok=True)
