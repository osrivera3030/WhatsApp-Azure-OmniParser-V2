"""
omniparser_detector.py - Detección visual del botón "reproducir audio" con
Microsoft OmniParser V2.

Por qué existe este módulo
---------------------------
``procesar_respuestas.py`` ubicaba el botón de reproducir una nota de voz
buscando en el DOM un ``button[aria-label*="voz" i]``. Ese selector es
correcto cuando el botón existe con ese atributo, pero no siempre basta para
descargar el audio (a veces el clic no llega a registrarse, o WhatsApp
reordena/oculta el elemento real detrás de otro). OmniParser V2
(https://github.com/microsoft/OmniParser) resuelve esto de otra forma: en vez
de leer el DOM, toma una captura de pantalla del chat y la "mira" con un
modelo de visión, igual que lo haría una persona, devolviendo la posición en
píxeles de cada ícono/botón visible junto con una descripción de qué es.

Este módulo NO reemplaza la captura del audio en sí (el "hook" de
``decodeAudioData`` / detección de ``<audio>`` en ``procesar_respuestas.py``
sigue igual) -- solo reemplaza el paso de "encontrar y hacer clic en el botón
de play". Una vez que se hace clic (por coordenadas, vía
``document.elementFromPoint`` en el script que llama a este módulo), el resto
del flujo de descarga/transcripción no cambia.

Requisitos (no son pip-instalables como paquete normal)
---------------------------------------------------------
OmniParser V2 se usa clonando su repositorio y descargando sus pesos, no
instalándolo con pip. Pasos (una sola vez):

1. Clonar el repo en la ruta que apunte ``config.OMNIPARSER_DIR`` (por
   defecto, una carpeta ``OmniParser`` junto a ``solo_nube - Copy``)::

       git clone https://github.com/microsoft/OmniParser "<OMNIPARSER_DIR>"

2. Instalar las dependencias pesadas (ver ``requirements-omniparser.txt`` en
   la raíz de este proyecto -- son pesadas y solo las necesitas si usas esta
   función, por eso están separadas del ``requirements.txt`` principal)::

       pip install -r requirements-omniparser.txt

3. Descargar los pesos del modelo (icon detect + icon caption) dentro de
   ``<OMNIPARSER_DIR>/weights/`` (requiere ``huggingface-cli``, incluido en
   el paquete ``huggingface_hub``)::

       cd <OMNIPARSER_DIR>
       for f in icon_detect/model.pt icon_detect/model.yaml icon_detect/train_args.yaml icon_caption/config.json icon_caption/generation_config.json icon_caption/model.safetensors; do
           huggingface-cli download microsoft/OmniParser-v2.0 "$f" --local-dir weights
       done
       mv weights/icon_caption weights/icon_caption_florence

Sin GPU funciona en CPU, pero cada captura de pantalla tarda varios segundos
(en vez de fracciones de segundo en GPU) -- normal para este caso de uso,
porque solo se llama una vez por chat con notas de voz pendientes, no en
tiempo real.

Uso
---
    from ia_local.omniparser_detector import detectar_botones_audio_cliente

    candidatos = detectar_botones_audio_cliente(captura_png_bytes, centro_panel_x=480)
    # candidatos: lista de (x, y) en píxeles de la imagen, ordenados de
    # arriba hacia abajo (mismo orden en que aparecen los mensajes en el chat)
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

from PIL import Image

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import (  # noqa: E402
    OMNIPARSER_BOX_THRESHOLD,
    OMNIPARSER_DEVICE,
    OMNIPARSER_DIR,
    OMNIPARSER_ICON_CAPTION_DIR,
    OMNIPARSER_ICON_DETECT_MODEL,
    OMNIPARSER_PALABRAS_CLAVE_AUDIO,
    get_logger,
)

logger = get_logger(__name__)

# Caché en memoria del proceso: cargar los modelos (YOLO + Florence-2) es
# costoso (varios segundos, o descarga de pesos de Florence-2 base la
# primera vez), así que se hace una sola vez por ejecución del script y se
# reutiliza para cada chat, igual que el modelo de Whisper en
# ``ia_local/audio_transcriber.py``.
_yolo_model_cache: Any = None
_caption_model_processor_cache: dict | None = None
_util_module_cache: Any = None


def _importar_util_omniparser() -> Any:
    """Agrega el repo de OmniParser al ``sys.path`` e importa ``util.utils``.

    Se hace en una función (en vez de un ``import`` normal arriba del
    archivo) porque OmniParser no es un paquete instalado -- es un repo
    clonado en ``config.OMNIPARSER_DIR`` -- y porque sus dependencias
    (torch, ultralytics, easyocr, paddleocr, transformers) son pesadas: si
    nunca se usa esta función, nunca se cargan.

    Raises
    ------
    FileNotFoundError
        Si ``config.OMNIPARSER_DIR`` no existe (no se clonó el repo) o no
        contiene ``util/utils.py``.
    """
    global _util_module_cache
    if _util_module_cache is not None:
        return _util_module_cache

    if not (OMNIPARSER_DIR / "util" / "utils.py").exists():
        raise FileNotFoundError(
            f"No se encontró el repo de OmniParser en {OMNIPARSER_DIR}. "
            "Clónalo con: git clone https://github.com/microsoft/OmniParser "
            f'"{OMNIPARSER_DIR}" (ver docstring de este módulo para el resto '
            "del setup: dependencias y pesos)."
        )

    sys.path.append(str(OMNIPARSER_DIR))
    from util import utils as omniparser_utils  # type: ignore  # noqa: E402

    # Parche: OmniParser llama AutoModelForCausalLM.from_pretrained sin
    # attn_implementation="eager", lo que hace que Florence-2 busque flash_attn
    # (no instalable en CPU/Windows). Reemplazamos get_caption_model_processor
    # con una versión que fuerza eager attention.
    import torch
    from transformers import AutoModelForCausalLM, AutoProcessor

    def _get_caption_model_processor_cpu(model_name, model_name_or_path, device="cpu"):
        # transformers hace dos chequeos sobre flash_attn:
        # 1. check_imports() busca el paquete en sys.modules -> necesita que exista.
        # 2. is_flash_attn_2_available() llama importlib.util.find_spec() ->
        #    necesita __spec__ != None, y luego comprueba __version__ para decidir
        #    si flash_attn2 está disponible. Sin __version__ devuelve False, con lo
        #    que Florence-2 usa atención estándar (compatible con CPU).
        import importlib.machinery
        import types as _types
        for _mod in [
            "flash_attn",
            "flash_attn.bert_padding",
            "flash_attn.flash_attn_interface",
            "flash_attn.flash_attn_varlen_func",
        ]:
            if _mod not in sys.modules:
                _stub = _types.ModuleType(_mod)
                _stub.__spec__ = importlib.machinery.ModuleSpec(_mod, loader=None)
                # Sin __version__: is_flash_attn_2_available() devuelve False
                sys.modules[_mod] = _stub

        processor = AutoProcessor.from_pretrained(
            "microsoft/Florence-2-base", trust_remote_code=True
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_name_or_path,
            torch_dtype=torch.float32,
            trust_remote_code=True,
            attn_implementation="eager",  # no usa flash_attn aunque esté "instalado"
        )
        model.to(device)
        return {"model": model, "processor": processor}

    omniparser_utils.get_caption_model_processor = _get_caption_model_processor_cpu

    _util_module_cache = omniparser_utils
    return _util_module_cache


def _cargar_modelos() -> tuple[Any, dict]:
    """Carga (o reutiliza) el modelo de detección de íconos (YOLO) y el de
    descripción de íconos (Florence-2), ambos requeridos por OmniParser V2.

    Returns
    -------
    tuple[Any, dict]
        ``(yolo_model, caption_model_processor)`` listos para pasar a
        ``util.utils.get_som_labeled_img``.
    """
    global _yolo_model_cache, _caption_model_processor_cache

    if _yolo_model_cache is not None and _caption_model_processor_cache is not None:
        return _yolo_model_cache, _caption_model_processor_cache

    omniparser_utils = _importar_util_omniparser()

    if not OMNIPARSER_ICON_DETECT_MODEL.exists():
        raise FileNotFoundError(
            f"No se encontraron los pesos de detección de íconos en "
            f"{OMNIPARSER_ICON_DETECT_MODEL}. Revisa el paso de descarga de "
            "pesos en el docstring de ia_local/omniparser_detector.py."
        )

    logger.info("Cargando modelo de detección de íconos (YOLO) de OmniParser V2...")
    _yolo_model_cache = omniparser_utils.get_yolo_model(str(OMNIPARSER_ICON_DETECT_MODEL))

    logger.info(
        "Cargando modelo de descripción de íconos (Florence-2) de OmniParser V2 "
        "(puede tardar la primera vez)..."
    )
    _caption_model_processor_cache = omniparser_utils.get_caption_model_processor(
        model_name="florence2",
        model_name_or_path=str(OMNIPARSER_ICON_CAPTION_DIR),
        device=OMNIPARSER_DEVICE,
    )

    return _yolo_model_cache, _caption_model_processor_cache


def detectar_elementos_pantalla(imagen: Image.Image) -> list[dict]:
    """Corre OmniParser V2 sobre una captura de pantalla y devuelve sus elementos.

    Parameters
    ----------
    imagen:
        Captura de pantalla (normalmente del viewport del navegador) ya
        cargada con Pillow.

    Returns
    -------
    list[dict]
        Uno por elemento detectado, con las llaves ``bbox`` (``[x1, y1, x2,
        y2]`` en **píxeles** de ``imagen``, ya convertido desde las
        proporciones 0-1 que devuelve OmniParser), ``content`` (descripción
        en inglés generada por Florence-2, o texto si vino de OCR) e
        ``interactivity`` (``True`` si OmniParser lo considera clickeable).
        Lista vacía si no se detectó nada o si algo falló (se registra en el
        log, no se interrumpe el pipeline por esto).
    """
    try:
        omniparser_utils = _importar_util_omniparser()
        yolo_model, caption_model_processor = _cargar_modelos()

        ancho, alto = imagen.size

        (texto_ocr, cajas_ocr), _ = omniparser_utils.check_ocr_box(
            imagen,
            display_img=False,
            output_bb_format="xyxy",
            easyocr_args={"paragraph": False, "text_threshold": 0.9},
            use_paddleocr=False,
        )

        _, _, elementos = omniparser_utils.get_som_labeled_img(
            imagen,
            model=yolo_model,
            BOX_TRESHOLD=OMNIPARSER_BOX_THRESHOLD,
            output_coord_in_ratio=False,
            ocr_bbox=cajas_ocr,
            draw_bbox_config=None,
            caption_model_processor=caption_model_processor,
            ocr_text=texto_ocr,
            use_local_semantics=True,
            iou_threshold=0.7,
        )

        resultado: list[dict] = []
        for elemento in elementos:
            x1, y1, x2, y2 = elemento["bbox"]
            resultado.append(
                {
                    "bbox": [x1 * ancho, y1 * alto, x2 * ancho, y2 * alto],
                    "content": (elemento.get("content") or "").strip(),
                    "interactivity": bool(elemento.get("interactivity")),
                }
            )
        return resultado

    except FileNotFoundError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Error corriendo OmniParser V2 sobre la captura de pantalla: %s", exc)
        return []


def _parece_boton_audio(contenido: str) -> bool:
    """``True`` si la descripción del ícono sugiere un botón de reproducir audio."""
    texto = contenido.lower()
    return any(palabra in texto for palabra in OMNIPARSER_PALABRAS_CLAVE_AUDIO)


def detectar_botones_audio_cliente(
    captura_png: bytes, centro_panel_x: float
) -> list[tuple[float, float]]:
    """Detecta botones de "reproducir audio" del lado del cliente en una captura.

    Combina la detección visual de OmniParser V2 con el mismo criterio que
    ya usaba ``procesar_respuestas.py`` para distinguir cliente vs. cuenta
    propia (posición horizontal respecto al centro del panel de chat):
    solo se devuelven íconos a la izquierda de ``centro_panel_x``.

    Parameters
    ----------
    captura_png:
        Bytes de una captura de pantalla en PNG (por ejemplo, el resultado
        de ``driver.get_screenshot_as_png()`` de Selenium).
    centro_panel_x:
        Coordenada X (en píxeles de la misma captura) del centro del panel
        de mensajes, usada para descartar audios enviados por la cuenta
        propia (quedan a la derecha).

    Returns
    -------
    list[tuple[float, float]]
        Coordenadas ``(x, y)`` del centro de cada botón candidato, en
        píxeles de la captura recibida, ordenadas de arriba hacia abajo
        (mismo orden en que aparecen los mensajes en el chat). Lista vacía
        si no se detectó ningún candidato o si OmniParser no está
        configurado (en ese caso se registra el motivo en el log).
    """
    # Reducir a la mitad: Florence-2 tarda ~4x menos y la precisión basta
    # para localizar botones de play. Las coordenadas se escalan de vuelta
    # al tamaño original antes de retornarlas.
    escala = 0.5
    try:
        imagen = Image.open(io.BytesIO(captura_png)).convert("RGB")
        ancho_orig, alto_orig = imagen.size
        imagen = imagen.resize(
            (int(ancho_orig * escala), int(alto_orig * escala)),
            Image.LANCZOS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo abrir la captura de pantalla: %s", exc)
        return []

    try:
        elementos = detectar_elementos_pantalla(imagen)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return []

    # centro_panel_x está en píxeles de la captura original; para comparar
    # con las coordenadas de la imagen reducida hay que escalarlo también.
    centro_panel_x_scaled = centro_panel_x * escala

    candidatos: list[tuple[float, float]] = []
    for elemento in elementos:
        if not elemento["interactivity"]:
            continue
        if not _parece_boton_audio(elemento["content"]):
            continue

        x1, y1, x2, y2 = elemento["bbox"]
        centro_x = (x1 + x2) / 2
        centro_y = (y1 + y2) / 2
        if centro_x >= centro_panel_x_scaled:
            continue  # del lado de la cuenta propia, no del cliente

        candidatos.append((centro_x, centro_y))

    candidatos.sort(key=lambda punto: punto[1])  # arriba -> abajo, como el chat

    # Escalar coordenadas de vuelta al tamaño original de la captura
    candidatos = [(x / escala, y / escala) for x, y in candidatos]

    logger.info("OmniParser V2 detectó %d candidato(s) a botón de audio del cliente.", len(candidatos))
    return candidatos
