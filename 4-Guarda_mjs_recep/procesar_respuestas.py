"""
procesar_respuestas.py - Lee texto y transcribe/OCRea audio/imágenes/video de WhatsApp en un solo paso.

Reemplaza a
------------
* ``envo_stg_save.py`` (extracción de texto) -- conservado como
  ``envo_stg_save.py.bak``.
* ``descargar_medios.py`` (descarga de audio/imagen) -- conservado como
  ``descargar_medios.py.bak``.

Nota: no se calcula sentimiento/categoría aquí (se quitó esa parte). Si en
algún momento la necesitas, ``ia_local/pipeline.py`` (función
``analizar_respuestas_directorio``) la sigue teniendo disponible como paso
aparte.

Por qué se unificaron
-----------------------
Antes había que abrir el mismo chat dos veces: una para leer texto y otra
para descargar medios. Este script hace todo en **una sola visita** por
contacto: lee el texto del chat, descarga imagen/audio/video, y los
transcribe/OCRea con ``ia_local``.

Los medios descargados (imagen/audio/video) se guardan de forma
**permanente** dentro de ``config.MEDIA_DIR`` (carpeta ``media_whatsapp/``),
en una subcarpeta por contacto nombrada con su teléfono, por ejemplo:

    media_whatsapp/
    ├── 99887766/
    │   ├── 1718900000_0.jpg
    │   └── 1718900012_1.ogg
    └── 88776655/
        └── 1718900050_0.mp4

Así puedes abrir la carpeta del teléfono que te interese y verificar
manualmente lo que se descargó de ese chat. No se borran automáticamente.

Cambio importante: se procesan medios aunque ya hayas respondido
--------------------------------------------------------------------
Antes, si el último mensaje del chat era tuyo (es decir, ya le habías
respondido al cliente), el script se detenía sin revisar nada más -- lo cual
dejaba sin extraer cualquier imagen/audio que el cliente hubiera enviado
*antes* de tu respuesta. Ahora solo se omite un chat si está completamente
vacío; si ya respondiste pero el cliente había enviado medios, igual se
extraen.

Qué hace
--------
1. Lee ``TABLAS["directorio"]`` y filtra contactos con
   ``Respuesta_Recibida == 'NO'``.
2. Abre el chat una sola vez y, con un único script en el DOM, extrae a la
   vez:
       * los mensajes de texto del cliente.
       * los elementos de imagen / nota de voz / video enviados por el cliente.
3. Por cada imagen: la descarga (``blob:`` -> base64 -> archivo en
   ``media_whatsapp/``) y le aplica OCR (``ia_local.image_ocr``).
4. Por cada nota de voz: ubica visualmente el botón de "reproducir" con
   OmniParser V2 (``ia_local.omniparser_detector``), hace clic ahí, captura
   el blob, lo descarga a ``media_whatsapp/`` y lo transcribe con Whisper
   (``ia_local.audio_transcriber``).
5. Por cada video: lo descarga igual que el audio y transcribe su pista de
   audio con Whisper (que internamente usa ffmpeg para extraer el audio de
   cualquier contenedor de video, sin necesidad de un módulo aparte).
6. Combina el texto del chat + el texto extraído de cada medio en un solo
   campo ``Texto_Respuesta`` y lo sube a Azure (merge).

Cómo ejecutarlo
----------------
    python "4-Guarda_mjs_recep/procesar_respuestas.py"

Requiere una sesión de WhatsApp Web ya autenticada (escanear QR). La primera
vez que se detecte una nota de voz o video, se descargará el modelo de
Whisper (ver ``config.WHISPER_MODEL_SIZE``); requiere ``ffmpeg`` instalado en
el sistema.

Limitaciones conocidas
------------------------
Igual que ``descargar_medios.py`` (ver su docstring original): depende de
atributos del DOM de WhatsApp Web que pueden cambiar con cualquier
actualización, y solo procesa lo que ya está visible/cargado en el chat al
abrirlo (no hace scroll hacia mensajes antiguos). Esto sigue aplicando para
imagen y video, que se detectan por DOM.

El botón de reproducir audio, en cambio, ya **no** se detecta por
``aria-label`` -- se detectaba así antes, pero el clic no siempre lograba
descargar el audio. Ahora se ubica visualmente con **OmniParser V2**
(``ia_local.omniparser_detector.detectar_botones_audio_cliente``): se toma
una captura de pantalla del chat, OmniParser identifica el ícono de
"reproducir" y se hace clic ahí por coordenadas
(``document.elementFromPoint``). Esto requiere haber clonado el repo de
OmniParser y descargado sus pesos por separado (ver el docstring de
``ia_local/omniparser_detector.py`` para el setup completo) y es más lento
que un selector de DOM (varios segundos por chat en CPU, ya que corre un
modelo de visión), pero no depende de atributos que WhatsApp pueda cambiar
entre builds.

Esta cuenta reproduce las notas de voz con la Web Audio API en vez de un
``<audio>`` normal (confirmado: 0 elementos ``<audio>`` en el DOM incluso
después de hacer clic en reproducir). Por eso el script instala un "hook"
de ``decodeAudioData`` vía CDP (``_SCRIPT_INSTALAR_CAPTURA_AUDIO``) para
poder capturar el archivo igual. Si en el futuro WhatsApp cambia su forma
de reproducir audio (por ejemplo, deja de usar Web Audio API), este hook
dejaría de capturar nada y habría que investigar de nuevo cómo se reproduce.

Nota de alcance
-----------------
Este script automatiza la lectura/descarga de contenido de WhatsApp Web vía
Selenium, lo cual está fuera de los Términos de Servicio oficiales de
WhatsApp. Se documenta el comportamiento existente sin reforzarlo: no agrega
envío masivo ni nuevas técnicas de evasión, solo lee/procesa lo que el
cliente ya envió.
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

from azure.data.tables import TableClient
from azure.storage.blob import BlobServiceClient
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import (  # noqa: E402
    AZURE_BLOB_CONTAINER,
    AZURE_CONNECTION_STRING,
    MEDIA_DIR,
    TABLAS,
    asegurar_directorios,
    get_logger,
)
from ia_local.audio_transcriber import transcribir_audio  # noqa: E402
from ia_local.image_ocr import extraer_texto_imagen  # noqa: E402
from ia_local.omniparser_detector import detectar_botones_audio_cliente  # noqa: E402

logger = get_logger(__name__)

ESPERA_CHAT_SEGUNDOS = 20
ESPERA_MENSAJES_SEGUNDOS = 5
CODIGO_PAIS_POR_DEFECTO = "504"
CANTIDAD_ULTIMOS_MENSAJES = 5

# Sondeo para detectar el audio/video en reproducción después de hacer clic
# en "play": en vez de esperar un tiempo fijo (que puede ser muy corto si
# WhatsApp tarda en preparar el blob), se revisa varias veces seguidas.
INTENTOS_DETECCION_REPRODUCCION = 6
INTERVALO_DETECCION_REPRODUCCION_SEGUNDOS = 0.5

EXTENSIONES_POR_MIME: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "audio/ogg": ".ogg",
    "audio/ogg; codecs=opus": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/webm": ".webm",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}

# Script combinado: extrae en una sola pasada los mensajes de texto del
# cliente y los elementos multimedia (imagen/audio) que también le
# pertenecen, usando la posición horizontal dentro del panel de chat para
# distinguir cliente vs. mensajes propios.
_SCRIPT_EXTRAER_TODO = """
var resultados       = [];
var todosLosMensajes = [];
var mediaElementos   = [];

var TEXTOS_SISTEMA = [
    'en línea', 'en linea', 'online',
    'escribiendo', 'grabando',
    'cifrados de extremo a extremo',
    'los mensajes y las llamadas',
    'haz clic para cambiar',
    'desconectado', 'conectando',
    'usas una duración',
    'mensajes temporales',
    'acceder a un historial',
    'obten más información',
    'reenviado', 'reenvió',
    'últ. vez', 'ult. vez',
    'última vez', 'ultima vez',
    'últ. vez hoy a la',
    'hoy a la', 'ayer a la'
];

function esSistema(texto) {
    var t = texto.toLowerCase().trim();
    for (var i = 0; i < TEXTOS_SISTEMA.length; i++) {
        if (t.indexOf(TEXTOS_SISTEMA[i]) !== -1) return true;
    }
    if (t.length < 6) return true;
    return false;
}

var panelChat = document.querySelector('[data-testid="conversation-panel-messages"]') ||
               document.querySelector('[data-testid="msg-list"]')                     ||
               document.querySelector('div[tabindex="-1"]');

var panelLeft  = panelChat ? panelChat.getBoundingClientRect().left  : 0;
var panelWidth = panelChat ? panelChat.getBoundingClientRect().width : window.innerWidth;
var centroPan  = panelLeft + (panelWidth / 2);

// ── Texto ──
var spans = document.querySelectorAll('[data-testid="selectable-text"]');

spans.forEach(function(span) {
    var texto = span.innerText.trim();
    if (!texto || texto.length < 2) return;
    if (esSistema(texto)) return;

    var enHeader = false;
    var el = span;
    for (var k = 0; k < 8; k++) {
        if (!el) break;
        var testid = el.getAttribute('data-testid') || '';
        if (testid === 'conversation-header' ||
            testid === 'chatlist-header'      ||
            testid === 'status-v3') {
            enHeader = true;
            break;
        }
        el = el.parentElement;
    }
    if (enHeader) return;

    var rect       = span.getBoundingClientRect();
    var centroSpan = rect.left + (rect.width / 2);
    var esCliente  = centroSpan < centroPan;

    todosLosMensajes.push({ texto: texto, esCliente: esCliente });
    if (esCliente) resultados.push(texto);
});

// ── Medios (imagen / video) ──
// El botón de audio YA NO se busca aquí: se detecta por visión con
// OmniParser V2 (ver detectar_botones_audio_cliente en Python), porque el
// selector por aria-label no siempre lograba que el clic descargara el
// audio. Imagen y video sí siguen funcionando con DOM normal, así que se
// dejan igual que antes.
var elementosMedia = document.querySelectorAll('img[src^="blob:"], video');

elementosMedia.forEach(function(el) {
    var rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return;  // oculto
    var centroEl = rect.left + (rect.width / 2);
    if (centroEl < centroPan) mediaElementos.push(el);
});

var ultimoEsMio = null;
if (todosLosMensajes.length > 0) {
    ultimoEsMio = !todosLosMensajes[todosLosMensajes.length - 1].esCliente;
}

// Se devuelve también centroPan (en píxeles CSS del viewport) para que el
// paso de detección de audio por OmniParser V2 use el mismo criterio
// cliente-vs-cuenta-propia que el resto del script.
return {
    mensajes: resultados,
    ultimoEsMio: ultimoEsMio,
    mediaElementos: mediaElementos,
    centroPanel: centroPan
};
"""

# Hace clic en lo que sea que esté en una coordenada del viewport (en
# píxeles CSS, no de la captura de pantalla -- ver conversión por
# devicePixelRatio en procesar_audio_cliente_en_coordenadas). Se usa en vez
# de un selector porque el botón de audio ahora se ubica visualmente con
# OmniParser V2, no por un atributo del DOM.
_SCRIPT_CLIC_EN_COORDENADAS = """
var x = arguments[0];
var y = arguments[1];
var el = document.elementFromPoint(x, y);
if (!el) return false;
el.click();
return true;
"""

_SCRIPT_DEVICE_PIXEL_RATIO = "return window.devicePixelRatio || 1;"

# Encuentra todos los botones de audio del cliente por aria-label y devuelve
# sus coordenadas CSS (centro de cada botón), filtradas al lado izquierdo del
# panel (mensajes del cliente). Rápido: solo DOM, sin IA ni captura.
_SCRIPT_COORDS_BOTONES_AUDIO = """
var panelChat = document.querySelector('[data-testid="conversation-panel-messages"]') ||
               document.querySelector('[data-testid="msg-list"]') ||
               document.querySelector('div[tabindex="-1"]');
var panelLeft  = panelChat ? panelChat.getBoundingClientRect().left  : 0;
var panelWidth = panelChat ? panelChat.getBoundingClientRect().width : window.innerWidth;
var centroPan  = panelLeft + (panelWidth / 2);
var botones = [];
var els = document.querySelectorAll(
    'button[aria-label*="voz" i], button[aria-label*="voice" i], ' +
    'button[aria-label*="audio" i], button[aria-label*="play" i]'
);
els.forEach(function(btn) {
    var r  = btn.getBoundingClientRect();
    var cx = r.left + r.width  / 2;
    var cy = r.top  + r.height / 2;
    if (cx < centroPan) botones.push([cx, cy]);
});
return botones;
"""

# Busca, entre todos los <audio> de la página, uno cuyo src/currentSrc ya
# sea un blob: (sin importar si está pausado o no). Antes solo se aceptaba
# un <audio> "sonando" (!paused), pero el log mostró que después de hacer
# clic nunca se detecta ninguno así -- probablemente porque WhatsApp Web
# asigna el blob al <audio> sin que su estado "paused" cambie de forma
# detectable (o tarda en cambiar). Si tras todos los intentos sigue sin
# encontrarse nada, se devuelve un diagnóstico completo (cuántos <audio> hay
# en la página y su estado) para poder ver en el log qué está pasando
# realmente, en vez de seguir adivinando a ciegas.
_SCRIPT_AUDIO_REPRODUCIENDO = """
var audios = document.querySelectorAll('audio');
var diagnostico = [];
for (var i = 0; i < audios.length; i++) {
    var a = audios[i];
    var src = a.currentSrc || a.src || '';
    diagnostico.push({ src: src.slice(0, 40), paused: a.paused, readyState: a.readyState });
    if (src.indexOf('blob:') === 0) {
        if (!a.paused) { a.pause(); }
        return { encontrado: src, diagnostico: diagnostico };
    }
}
return { encontrado: null, diagnostico: diagnostico };
"""

# Confirmado con el diagnóstico anterior: esta cuenta no tiene NINGÚN
# elemento <audio> en la página (0 encontrados, incluso después de hacer
# clic en reproducir). Eso significa que WhatsApp Web reproduce las notas de
# voz con la Web Audio API (AudioContext + decodeAudioData) en vez de un
# <audio> normal -- el archivo nunca pasa por el DOM.
#
# Para poder capturarlo igual, este script intercepta decodeAudioData a
# nivel de navegador (instalado vía CDP, "Page.addScriptToEvaluateOnNewDocument",
# así que corre antes de que cargue el código de WhatsApp) y guarda una copia
# en base64 del archivo original cada vez que se llama. Es el mismo archivo
# que WhatsApp recibió del servidor (ya descifrado), solo que capturado en el
# punto exacto donde el navegador lo decodifica para poder reproducirlo.
_SCRIPT_INSTALAR_CAPTURA_AUDIO = """
(function() {
    if (window.__whatsappAudioHookInstalado) return;
    window.__whatsappAudioHookInstalado = true;
    window.__whatsappAudioCaptures = [];

    function bufferABase64(buffer) {
        var bytes = new Uint8Array(buffer);
        var binary = '';
        var tamanoChunk = 0x8000;
        for (var i = 0; i < bytes.length; i += tamanoChunk) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + tamanoChunk));
        }
        return btoa(binary);
    }

    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    var decodeOriginal = Ctx.prototype.decodeAudioData;
    Ctx.prototype.decodeAudioData = function(arrayBuffer) {
        try {
            var copia = arrayBuffer.slice(0);
            window.__whatsappAudioCaptures.push(bufferABase64(copia));
        } catch (e) { /* si falla la captura, igual deja reproducir normal */ }
        return decodeOriginal.apply(this, arguments);
    };
})();
"""

# Cuenta cuántas capturas hay hasta el momento (se usa para saber a partir de
# qué índice buscar la captura de un audio específico, sin perder las que ya
# existían de audios anteriores en el mismo chat).
_SCRIPT_CONTAR_CAPTURAS_AUDIO = "return (window.__whatsappAudioCaptures || []).length;"

# Lee la captura en una posición concreta del arreglo (puede que todavía no
# exista si WhatsApp aún no terminó de decodificar ese audio).
_SCRIPT_LEER_CAPTURA_AUDIO_EN_INDICE = """
var indice = arguments[0];
var caps = window.__whatsappAudioCaptures || [];
if (caps.length > indice) return caps[indice];
return null;
"""

# Descarga una blob: URL dentro del contexto de la página y la devuelve como
# data URL base64 (no se puede usar requests/urllib desde Python porque los
# blob: URL solo existen dentro del proceso del navegador).
_SCRIPT_BLOB_A_BASE64 = """
var callback = arguments[arguments.length - 1];
fetch(arguments[0])
    .then(function(resp) { return resp.blob(); })
    .then(function(blob) {
        var reader = new FileReader();
        reader.onloadend = function() { callback(reader.result); };
        reader.onerror = function() { callback(null); };
        reader.readAsDataURL(blob);
    })
    .catch(function() { callback(null); });
"""


def esperar_audio_reproduciendo(driver: webdriver.Edge) -> str | None:
    """Sondea repetidamente hasta encontrar un ``<audio>`` en reproducción.

    Después de hacer clic en "reproducir", WhatsApp Web tarda un tiempo
    variable en descifrar la nota de voz y poblar el ``<audio>`` interno con
    su blob. En vez de esperar un tiempo fijo (que puede quedarse corto),
    se revisa varias veces seguidas con una pequeña pausa entre intentos.

    Returns
    -------
    str | None
        La URL ``blob:`` del audio detectado, o ``None`` si después de todos
        los intentos no se encontró ninguno (en ese caso se deja en el log un
        diagnóstico de los ``<audio>`` que sí había en la página, para poder
        ver la causa real en vez de un simple "no se detectó nada").
    """
    diagnostico_final: list = []
    for _ in range(INTENTOS_DETECCION_REPRODUCCION):
        resultado = driver.execute_script(_SCRIPT_AUDIO_REPRODUCIENDO)
        diagnostico_final = resultado.get("diagnostico", [])
        blob_url = resultado.get("encontrado")
        if blob_url:
            return blob_url
        time.sleep(INTERVALO_DETECCION_REPRODUCCION_SEGUNDOS)

    if diagnostico_final:
        logger.warning("Elementos <audio> encontrados en la página (ninguno con blob): %s", diagnostico_final)
    else:
        logger.warning("La página no tiene ningún elemento <audio> (0 encontrados).")
    return None


def esperar_captura_audio_en_indice(driver: webdriver.Edge, indice: int) -> str | None:
    """Sondea el buffer de ``decodeAudioData`` hasta que aparezca la captura ``indice``.

    Cada nota de voz del chat tiene su propio índice esperado (asignado en
    ``procesar_entidad`` según el orden en que aparecen en el DOM). Se sondea
    en vez de esperar un tiempo fijo porque WhatsApp puede tardar en decodificar
    el audio después del clic.

    Returns
    -------
    str | None
        El audio capturado en base64, o ``None`` si no apareció tras todos
        los intentos.
    """
    for _ in range(INTENTOS_DETECCION_REPRODUCCION):
        captura = driver.execute_script(_SCRIPT_LEER_CAPTURA_AUDIO_EN_INDICE, indice)
        if captura:
            return captura
        time.sleep(INTERVALO_DETECCION_REPRODUCCION_SEGUNDOS)
    return None


def detectar_extension_audio(base64_datos: str) -> str:
    """Adivina la extensión de un audio capturado a partir de la firma de sus primeros bytes.

    ``decodeAudioData`` no nos da el tipo MIME directamente (a diferencia de
    un blob normal), así que hay que inferirlo del contenido.
    """
    try:
        # Recorta a un múltiplo de 4 caracteres para que el base64 parcial
        # siga siendo válido (solo nos interesan los primeros bytes/firma).
        fragmento = base64_datos[:64]
        fragmento = fragmento[: len(fragmento) - len(fragmento) % 4]
        cabecera = base64.b64decode(fragmento)
    except Exception:  # noqa: BLE001
        return ".ogg"

    if cabecera.startswith(b"OggS"):
        return ".ogg"
    if len(cabecera) >= 8 and cabecera[4:8] == b"ftyp":
        return ".m4a"
    if cabecera.startswith(b"ID3") or cabecera[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if cabecera.startswith(b"RIFF"):
        return ".wav"
    return ".ogg"  # formato más común en notas de voz de WhatsApp (Opus en Ogg)


def extension_para_mime(mime: str) -> str:
    """Devuelve la extensión de archivo asociada a un tipo MIME conocido."""
    return EXTENSIONES_POR_MIME.get(mime, "")


def mime_de_data_url(data_url: str) -> str:
    """Extrae el tipo MIME de un data URL (``data:image/jpeg;base64,...`` -> ``image/jpeg``)."""
    try:
        cabecera = data_url.split(",", 1)[0]
        return cabecera.split(":", 1)[1].split(";", 1)[0]
    except Exception:  # noqa: BLE001
        return ""


def descargar_blob_a_archivo(
    driver: webdriver.Edge, blob_url: str, carpeta: Path, nombre_base: str, sufijo_por_defecto: str
) -> Path | None:
    """Descarga una ``blob:`` URL y la guarda de forma permanente dentro de ``carpeta``.

    A diferencia de un archivo temporal, este archivo **no se borra**: queda
    guardado para que puedas verificarlo manualmente (abrir la imagen,
    escuchar el audio, etc.) después de correr el script.

    Parameters
    ----------
    driver:
        Navegador con la página de WhatsApp Web abierta (el blob solo existe
        en ese contexto).
    blob_url:
        URL ``blob:https://web.whatsapp.com/...``.
    carpeta:
        Carpeta donde se guarda el archivo (normalmente
        ``MEDIA_DIR / <teléfono>``, ya creada de antemano).
    nombre_base:
        Nombre de archivo sin extensión (ej. ``"1718900000_0"``).
    sufijo_por_defecto:
        Extensión a usar si no se puede determinar el MIME real (ej. ``".jpg"``).

    Returns
    -------
    Path | None
        Ruta del archivo guardado, o ``None`` si falló la descarga.
    """
    driver.set_script_timeout(30)
    data_url = driver.execute_async_script(_SCRIPT_BLOB_A_BASE64, blob_url)
    if not data_url:
        return None

    try:
        _, datos_base64 = data_url.split(",", 1)
        contenido = base64.b64decode(datos_base64)
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo decodificar el blob: %s", exc)
        return None

    extension = extension_para_mime(mime_de_data_url(data_url)) or sufijo_por_defecto
    ruta_destino = carpeta / f"{nombre_base}{extension}"
    ruta_destino.write_bytes(contenido)
    logger.info("Medio guardado en: %s", ruta_destino)
    return ruta_destino


def subir_a_blob(ruta: Path, nombre_blob: str) -> None:
    """Sube un archivo local a Azure Blob Storage.

    Usa la misma cadena de conexión que Table Storage (misma cuenta Azure).
    El blob queda en: media-whatsapp/<nombre_blob>
    Igual que en el código de Juan Fernando pero con archivos locales en vez
    de bytes descargados desde Telegram.

    Parameters
    ----------
    ruta:
        Ruta local del archivo a subir (.jpg, .ogg, .mp4, etc.)
    nombre_blob:
        Nombre del blob dentro del contenedor (ej. "504XXXXXX/audio_0.ogg").
    """
    try:
        blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        contenedor = blob_service.get_container_client(AZURE_BLOB_CONTAINER)
        try:
            contenedor.create_container()
        except Exception:  # noqa: BLE001 — ya existe
            pass
        blob_client = contenedor.get_blob_client(nombre_blob)
        with open(ruta, "rb") as f:
            blob_client.upload_blob(f, overwrite=True)
        logger.info("Blob subido: %s/%s", AZURE_BLOB_CONTAINER, nombre_blob)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo subir %s a Blob Storage: %s", ruta.name, exc)


def procesar_imagen_cliente(driver: webdriver.Edge, elemento, carpeta: Path, nombre_base: str) -> str:
    """Descarga una imagen del cliente (queda en ``carpeta``) y le aplica OCR."""
    blob_url = elemento.get_attribute("src")
    if not blob_url or not blob_url.startswith("blob:"):
        return ""

    ruta = descargar_blob_a_archivo(driver, blob_url, carpeta, nombre_base, ".jpg")
    if ruta is None:
        return ""
    subir_a_blob(ruta, f"{carpeta.name}/{ruta.name}")
    return extraer_texto_imagen(ruta)


def procesar_audio_cliente_en_coordenadas(
    driver: webdriver.Edge, x: float, y: float, carpeta: Path, nombre_base: str, indice_captura: int
) -> str:
    """Hace clic en un botón de "reproducir audio" ubicado por OmniParser V2,
    captura el archivo original y lo transcribe.

    A diferencia de la versión anterior (que recibía un ``WebElement``
    ubicado por ``aria-label``), esta recibe coordenadas en píxeles **CSS**
    del viewport (ya convertidas desde la captura de pantalla -- ver
    ``procesar_entidad``) y hace clic ahí mismo vía
    ``document.elementFromPoint`` (``_SCRIPT_CLIC_EN_COORDENADAS``). El resto
    de la captura del audio no cambió:

    1. **Vía ``<audio>`` normal**: si aparece un ``<audio>`` con ``src``
       tipo ``blob:``, se descarga igual que una imagen.
    2. **Vía captura de ``decodeAudioData``**: confirmado en esta cuenta que
       no existe ningún ``<audio>`` en el DOM (WhatsApp usa la Web Audio API
       para reproducir notas de voz). En ese caso se usa el "hook" instalado
       al inicio de la sesión (``_SCRIPT_INSTALAR_CAPTURA_AUDIO``) que
       intercepta esa llamada y guarda el archivo original.

    Parameters
    ----------
    x, y:
        Coordenadas (en píxeles CSS del viewport) del botón a clickear.
    indice_captura:
        Posición esperada de este audio dentro del buffer de capturas de la
        Web Audio API (asignada en ``procesar_entidad`` según el orden de
        aparición en el chat).
    """
    try:
        # Limpiar capturas previas del hook para que solo quede el audio nuevo
        driver.execute_script("window.__whatsappAudioCaptures = [];")
        hizo_clic = driver.execute_script(_SCRIPT_CLIC_EN_COORDENADAS, x, y)
        if not hizo_clic:
            logger.warning(
                "No había ningún elemento en (%.0f, %.0f) para hacer clic (botón de audio).", x, y
            )
            return ""
        logger.info("Clic en botón de audio (%.0f, %.0f)", x, y)
        time.sleep(2)  # dar tiempo a WhatsApp para decodificar el audio
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo hacer clic en reproducir audio en (%.0f, %.0f): %s", x, y, exc)
        return ""

    blob_url = esperar_audio_reproduciendo(driver)
    if blob_url:
        ruta = descargar_blob_a_archivo(driver, blob_url, carpeta, nombre_base, ".ogg")
        if ruta is None:
            logger.warning("Se detectó el audio (<audio>) pero falló la descarga del blob.")
            return ""
        # transcribir_audio() carga (y cachea) el modelo de Whisper la
        # primera vez que se necesita; si nunca hay audio, nunca se carga.
        return transcribir_audio(ruta)

    base64_audio = esperar_captura_audio_en_indice(driver, indice_captura)
    if not base64_audio:
        logger.warning(
            "No se detectó audio por ningún método (ni <audio>, ni captura de "
            "decodeAudioData) tras %d intentos.",
            INTENTOS_DETECCION_REPRODUCCION,
        )
        return ""

    extension = detectar_extension_audio(base64_audio)
    ruta = carpeta / f"{nombre_base}{extension}"
    try:
        ruta.write_bytes(base64.b64decode(base64_audio))
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo decodificar el audio capturado: %s", exc)
        return ""
    logger.info("Medio guardado en: %s", ruta)
    subir_a_blob(ruta, f"{carpeta.name}/{ruta.name}")
    return transcribir_audio(ruta)


def procesar_video_cliente(driver: webdriver.Edge, elemento, carpeta: Path, nombre_base: str) -> str:
    """Descarga un video del cliente (queda en ``carpeta``) y transcribe su pista de audio.

    No hace falta un módulo de "video" aparte: Whisper usa ffmpeg
    internamente, así que puede extraer y transcribir el audio de cualquier
    contenedor de video (mp4, webm, mov) igual que lo haría con un archivo
    de audio puro.
    """
    blob_url = elemento.get_attribute("src") or elemento.get_attribute("currentSrc")

    # Algunos videos no tienen el blob listo hasta que se hace clic/reproduce.
    if not blob_url or not blob_url.startswith("blob:"):
        try:
            elemento.click()
        except Exception:  # noqa: BLE001
            pass

        for _ in range(INTENTOS_DETECCION_REPRODUCCION):
            blob_url = driver.execute_script(
                "return arguments[0].currentSrc || arguments[0].src;", elemento
            )
            if blob_url and blob_url.startswith("blob:"):
                break
            time.sleep(INTERVALO_DETECCION_REPRODUCCION_SEGUNDOS)

    if not blob_url or not blob_url.startswith("blob:"):
        logger.warning(
            "No se detectó video en reproducción tras %d intentos.",
            INTENTOS_DETECCION_REPRODUCCION,
        )
        return ""

    ruta = descargar_blob_a_archivo(driver, blob_url, carpeta, nombre_base, ".mp4")
    if ruta is None:
        logger.warning("Se detectó el video pero falló la descarga del blob.")
        return ""
    return transcribir_audio(ruta)


def procesar_entidad(driver: webdriver.Edge, table_client: TableClient, entity: dict) -> None:
    """Revisa el chat de una entidad, consolida texto+medios y actualiza Azure.

    Parameters
    ----------
    driver:
        Navegador con sesión activa de WhatsApp Web.
    table_client:
        Cliente conectado a ``TABLAS["directorio"]``.
    entity:
        Entidad de Azure a revisar (debe incluir ``phone``).
    """
    phone = entity.get("phone", "").replace("-", "").replace(" ", "")
    logger.info("Revisando: %s", phone)

    try:
        try:
            driver.get(
                f"https://web.whatsapp.com/send/?phone={CODIGO_PAIS_POR_DEFECTO}{phone}"
                "&text&app_absent=0"
            )
        except Exception:  # noqa: BLE001 — page_load_timeout: la página tardó >30s
            # La página sigue cargándose en background; detenemos la carga
            # y continuamos — WhatsApp Web ya habrá cargado suficiente DOM.
            try:
                driver.execute_script("window.stop();")
            except Exception:  # noqa: BLE001
                pass

        WebDriverWait(driver, ESPERA_CHAT_SEGUNDOS).until(
            EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true']"))
        )
        try:
            WebDriverWait(driver, ESPERA_MENSAJES_SEGUNDOS).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, '[data-testid="selectable-text"]')
                )
            )
            time.sleep(3)
        except Exception:  # noqa: BLE001 - no hay mensajes visibles aún
            time.sleep(8)

        resultado = driver.execute_script(_SCRIPT_EXTRAER_TODO)
        mensajes = resultado.get("mensajes", [])
        ultimo_mio = resultado.get("ultimoEsMio")
        media_elementos = resultado.get("mediaElementos", [])

        logger.info(
            "Mensajes del cliente: %d | Imagen/video detectados (DOM): %d | Último msg es mío: %s",
            len(mensajes), len(media_elementos), ultimo_mio,
        )

        # Solo se omite si el chat está completamente vacío (no hay nada que
        # extraer). Si ya respondiste (ultimo_mio is True), igual se
        # procesan los medios que el cliente haya enviado antes de tu
        # respuesta -- antes este caso se saltaba por completo y esos
        # audios/imágenes/videos nunca se llegaban a extraer.
        if ultimo_mio is None:
            logger.info("Chat vacío, no se actualiza.")
            return

        partes_texto: list[str] = []
        if mensajes:
            partes_texto.append(" | ".join(mensajes[-CANTIDAD_ULTIMOS_MENSAJES:]))

        medios_procesados = 0

        # Cada contacto tiene su propia subcarpeta dentro de media_whatsapp/,
        # nombrada con su teléfono (ej. media_whatsapp/99887766/), para que
        # sea fácil ubicar manualmente lo que se descargó de cada chat.
        carpeta_contacto = MEDIA_DIR / (phone or "sin_telefono")
        asegurar_directorios(carpeta_contacto)

        # Índice esperado de la próxima captura de audio en el buffer de
        # decodeAudioData (ver _SCRIPT_INSTALAR_CAPTURA_AUDIO). No se
        # reinicia entre audios del mismo chat para no perder ninguna
        # captura ya existente; se calcula una sola vez al empezar y se
        # incrementa por cada audio que se vaya procesando.
        indice_captura_audio = driver.execute_script(_SCRIPT_CONTAR_CAPTURAS_AUDIO) or 0

        # ── Imagen / video: siguen detectándose por DOM, sin cambios ──
        for indice, elemento in enumerate(media_elementos):
            tag = elemento.tag_name.lower()
            nombre_base = f"{int(time.time())}_{indice}"

            if tag == "img":
                etiqueta = "imagen"
                texto_medio = procesar_imagen_cliente(driver, elemento, carpeta_contacto, nombre_base)
            else:
                etiqueta = "video"
                texto_medio = procesar_video_cliente(driver, elemento, carpeta_contacto, nombre_base)

            if texto_medio:
                partes_texto.append(f"[{etiqueta}]: {texto_medio}")
                medios_procesados += 1
            time.sleep(0.5)

        # ── Audio: DOM rápido primero, OmniParser V2 como fallback ──
        # aria-label confirmado: "Reproducir mensaje de voz".
        # Se intenta hasta 3 veces con pausa: WhatsApp puede tardar en
        # renderizar los botones de audio tras abrir el chat.
        device_pixel_ratio = driver.execute_script(_SCRIPT_DEVICE_PIXEL_RATIO) or 1.0
        coords_raw: list = []
        for _intento in range(3):
            coords_raw = driver.execute_script(_SCRIPT_COORDS_BOTONES_AUDIO) or []
            if coords_raw:
                break
            time.sleep(1)
        candidatos_audio: list[tuple[float, float]] = [
            (float(c[0]), float(c[1])) for c in coords_raw
        ]
        logger.info("Botones de audio (DOM): %d", len(candidatos_audio))

        if not candidatos_audio and mensajes:
            # DOM no encontró botones de audio — usar OmniParser V2
            try:
                captura_png = driver.get_screenshot_as_png()
                centro_panel_css = resultado.get("centroPanel", 0)
                candidatos_audio = detectar_botones_audio_cliente(
                    captura_png, centro_panel_css * device_pixel_ratio
                )
                # OmniParser devuelve coords en píxeles de captura → convertir a CSS
                candidatos_audio = [
                    (x / device_pixel_ratio, y / device_pixel_ratio)
                    for x, y in candidatos_audio
                ]
            except Exception as exc:  # noqa: BLE001
                logger.warning("OmniParser V2 falló: %s", exc)

        for indice, (x_css, y_css) in enumerate(candidatos_audio):
            nombre_base = f"{int(time.time())}_audio_{indice}"
            texto_medio = procesar_audio_cliente_en_coordenadas(
                driver, x_css, y_css, carpeta_contacto, nombre_base, indice_captura_audio
            )
            indice_captura_audio += 1

            if texto_medio:
                partes_texto.append(f"[audio]: {texto_medio}")
                medios_procesados += 1
            time.sleep(0.5)

        if not partes_texto:
            logger.info("Cliente no ha respondido (sin texto ni medios procesables).")
            return

        texto_final = " | ".join(partes_texto)
        logger.info("Respuesta consolidada: %s", texto_final[:200])

        entity["Texto_Respuesta"] = texto_final[:32000]  # límite de Azure Table Storage
        entity["Respuesta_Recibida"] = "SI"
        entity["Fecha_Verificacion"] = time.strftime("%d/%m/%Y, %H:%M:%S")
        entity["Medios_Procesados"] = medios_procesados
        table_client.update_entity(mode="merge", entity=entity)
        logger.info("Guardado en Azure Table Storage.")

    except Exception as exc:  # noqa: BLE001
        logger.error("Error en %s: %s", phone, exc)


def procesar_respuestas() -> None:
    """Punto de entrada: recorre los contactos pendientes de respuesta."""
    asegurar_directorios(MEDIA_DIR)

    table_client = TableClient.from_connection_string(
        AZURE_CONNECTION_STRING, TABLAS["directorio"]
    )
    entidades = list(
        table_client.query_entities(query_filter="Respuesta_Recibida eq 'NO'")
    )
    if not entidades:
        logger.info("No hay entidades pendientes.")
        return

    driver = webdriver.Edge()
    # Límite de carga de página: si WhatsApp no termina de cargar en 30 s
    # se lanza TimeoutException en vez de colgar 120 s por defecto.
    driver.set_page_load_timeout(30)
    # Se instala ANTES de navegar para que el "hook" de captura de audio
    # (_SCRIPT_INSTALAR_CAPTURA_AUDIO) ya esté activo desde la primera carga
    # de la página -- así no se pierde ningún decodeAudioData que WhatsApp
    # haga apenas abre el chat.
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument", {"source": _SCRIPT_INSTALAR_CAPTURA_AUDIO}
    )
    driver.get("https://web.whatsapp.com")
    input("Escanea el QR y presiona ENTER...")

    try:
        for entity in entidades:
            procesar_entidad(driver, table_client, entity)
    finally:
        driver.quit()

    logger.info("Proceso terminado.")


if __name__ == "__main__":
    try:
        procesar_respuestas()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ocurrió un error durante el procesamiento de respuestas: %s", exc)
