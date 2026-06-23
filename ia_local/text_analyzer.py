"""
text_analyzer.py - Análisis local de texto (sentimiento y categorización).

Qué hace
--------
Aplica un modelo de Hugging Face Transformers en modo local para:

1. **Análisis de sentimiento**: clasifica una respuesta de cliente como
   positiva, negativa o neutra (útil para priorizar seguimiento manual).
2. **Categorización por palabras clave**: clasifica el texto en una
   categoría de negocio (precio, disponibilidad, ubicación, queja, etc.)
   usando un diccionario de palabras clave configurable. Es una heurística
   simple y rápida; si se necesita mayor precisión, ``clasificar_zero_shot``
   usa un modelo de *zero-shot classification* con las categorías que se le
   pasen.

Todo corre localmente: el modelo se descarga una sola vez desde Hugging
Face Hub y luego se cachea para uso offline.

Uso
---
    from ia_local.text_analyzer import analizar_sentimiento, categorizar_por_palabras_clave

    resultado = analizar_sentimiento("Gracias, ya no necesito nada más")
    categoria = categorizar_por_palabras_clave("¿Cuánto cuesta la bolsa?")
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TypedDict

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import TEXT_SENTIMENT_MODEL, get_logger  # noqa: E402

logger = get_logger(__name__)

# Caché del pipeline de sentimiento en memoria del proceso (cargar el
# modelo es costoso, se hace una sola vez).
_pipeline_sentimiento = None

# Categorías de negocio detectadas por palabras clave simples. Se puede
# ajustar libremente sin tocar el resto del código.
CATEGORIAS_PALABRAS_CLAVE: dict[str, tuple[str, ...]] = {
    "precio": ("precio", "cuanto", "cuesta", "vale", "cotiz"),
    "disponibilidad": ("marca", "disponible", "stock", "hay", "tiene"),
    "ubicacion": ("donde", "ubicacion", "direccion", "queda"),
    "queja": ("mal", "molesto", "reclamo", "demora", "nunca llego"),
    "agradecimiento": ("gracias", "agradezco", "amable"),
}


class ResultadoSentimiento(TypedDict):
    """Resultado tipado de ``analizar_sentimiento``."""

    etiqueta: str
    confianza: float


def _cargar_pipeline_sentimiento():
    """Carga (o reutiliza) el pipeline de sentimiento de Transformers.

    ``truncation=True`` es importante: el modelo de sentimiento (RoBERTuito)
    solo soporta secuencias cortas (~128 tokens). Sin truncado automático a
    nivel de tokens, un texto con muchas palabras puede generar más tokens
    que el límite del modelo y lanzar un error de índice fuera de rango.
    """
    global _pipeline_sentimiento
    if _pipeline_sentimiento is None:
        from transformers import pipeline  # import perezoso: evita cargar torch si no se usa

        logger.info("Cargando modelo de sentimiento '%s'...", TEXT_SENTIMENT_MODEL)
        _pipeline_sentimiento = pipeline(
            "sentiment-analysis", model=TEXT_SENTIMENT_MODEL, truncation=True
        )
    return _pipeline_sentimiento


def analizar_sentimiento(texto: str) -> ResultadoSentimiento:
    """Clasifica el sentimiento de un texto en español.

    Parameters
    ----------
    texto:
        Texto a analizar (por ejemplo, la respuesta de un cliente).

    Returns
    -------
    ResultadoSentimiento
        ``{"etiqueta": "POS" | "NEG" | "NEU" | "DESCONOCIDO", "confianza": float}``.
        Devuelve ``"DESCONOCIDO"`` con confianza 0.0 si el texto está vacío
        o si falla la inferencia.
    """
    if not texto or not texto.strip():
        return {"etiqueta": "DESCONOCIDO", "confianza": 0.0}

    try:
        clasificador = _cargar_pipeline_sentimiento()
        # El recorte por caracteres es solo un límite de seguridad básico
        # (evita mandar textos enormes); el truncado real a nivel de
        # tokens lo hace el pipeline gracias a truncation=True.
        resultado = clasificador(texto[:2000])[0]
        return {"etiqueta": resultado["label"], "confianza": float(resultado["score"])}
    except Exception as exc:  # noqa: BLE001
        logger.error("Error analizando sentimiento: %s", exc)
        return {"etiqueta": "DESCONOCIDO", "confianza": 0.0}


def categorizar_por_palabras_clave(
    texto: str, categorias: dict[str, tuple[str, ...]] = CATEGORIAS_PALABRAS_CLAVE
) -> str:
    """Asigna una categoría de negocio a un texto usando coincidencia de palabras clave.

    Es una heurística simple (sin modelo) pensada para ser rápida y
    determinista. Para mayor precisión semántica, ver
    ``clasificar_zero_shot``.

    Parameters
    ----------
    texto:
        Texto a categorizar.
    categorias:
        Diccionario ``{categoria: (palabras_clave, ...)}``. Por defecto usa
        ``CATEGORIAS_PALABRAS_CLAVE``.

    Returns
    -------
    str
        Nombre de la primera categoría cuya palabra clave aparece en el
        texto (case-insensitive), o ``"sin_categoria"`` si ninguna coincide.
    """
    texto_normalizado = texto.lower()
    for categoria, palabras_clave in categorias.items():
        if any(palabra in texto_normalizado for palabra in palabras_clave):
            return categoria
    return "sin_categoria"


def clasificar_zero_shot(texto: str, categorias: list[str]) -> str:
    """Clasifica un texto entre categorías arbitrarias usando zero-shot classification.

    Más flexible que ``categorizar_por_palabras_clave`` pero más lento (usa
    un modelo de Transformers tipo NLI). Útil cuando las categorías cambian
    frecuentemente o el texto es más complejo que un par de palabras clave.

    Parameters
    ----------
    texto:
        Texto a clasificar.
    categorias:
        Lista de posibles categorías (en lenguaje natural, ej.
        ``["precio", "queja", "agradecimiento"]``).

    Returns
    -------
    str
        Categoría con mayor probabilidad, o ``"sin_categoria"`` si falla.
    """
    if not texto or not categorias:
        return "sin_categoria"

    try:
        from transformers import pipeline  # import perezoso

        clasificador = pipeline("zero-shot-classification")
        resultado = clasificador(texto[:512], candidate_labels=categorias)
        return str(resultado["labels"][0])
    except Exception as exc:  # noqa: BLE001
        logger.error("Error en clasificación zero-shot: %s", exc)
        return "sin_categoria"
