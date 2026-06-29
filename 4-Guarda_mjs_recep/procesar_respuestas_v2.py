"""
procesar_respuestas_v2.py
--------------------------
Pipeline unificado: WhatsApp Web → captura texto/imagen/audio → STT → Azure.

Arquitectura:
    GestorCarpetas      – crea media/<numero>/audios/ e media/<numero>/imagenes/
                          SOLO cuando hay un archivo real que guardar.
    CapturadorAudio     – triple hook (decodeAudioData + createObjectURL + fetch)
                          instalado vía CDP antes de que cargue WhatsApp.
    ProcesadorMedios    – descarga blobs de imagen/video, llama al STT.
    ProcesadorWhatsApp  – orquesta todo: Azure → driver → contacto → Azure.

Flujo por contacto:
    1. Lee Azure Table (Respuesta_Recibida = 'NO') → lista de teléfonos.
    2. Abre el chat una sola vez en WhatsApp Web.
    3. Extrae texto del cliente (DOM).
    4. Descarga imágenes → OCR → texto.
    5. Captura audios (triple hook) → STT (Azure STT → Whisper) → texto.
    6. Consolida: Texto_Respuesta = texto + [imagen] + [audio].
    7. Actualiza Azure: Respuesta_Recibida = 'SI', Texto_Respuesta = <texto>.

Uso:
    python "4-Guarda_mjs_recep/procesar_respuestas_v2.py"
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path
from typing import Optional

from azure.data.tables import TableClient
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options
from selenium.webdriver.edge.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.microsoft import EdgeChromiumDriverManager

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import (
    AZURE_CONNECTION_STRING,
    MEDIA_DIR,
    PERFIL_WHATSAPP_DIR,
    TABLAS,
    get_logger,
)
from ia_local.audio_transcriber import transcribir_audio
from ia_local.image_ocr import extraer_texto_imagen

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTES DE NEGOCIO
# ─────────────────────────────────────────────────────────────────────────────
CODIGO_PAIS            = "504"
ESPERA_CHAT_SEG        = 20
ESPERA_MENSAJES_SEG    = 5
INTENTOS_AUDIO         = 16   # × 0.5 s = 8 s máx de espera por captura
ULTIMOS_MENSAJES       = 5

EXTENSIONES_MIME: dict[str, str] = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "audio/ogg": ".ogg",  "audio/ogg; codecs=opus": ".ogg",
    "audio/mp4": ".m4a",  "audio/mpeg": ".mp3",
    "video/mp4": ".mp4",  "video/webm": ".webm",
}

# ─────────────────────────────────────────────────────────────────────────────
# JAVASCRIPT – extracción de DOM
# ─────────────────────────────────────────────────────────────────────────────
_JS_EXTRAER_TODO = """
var resultados = [], todosMsg = [], mediaEls = [];
var SISTEMA = [
    'en línea','en linea','online','escribiendo','grabando',
    'cifrados de extremo a extremo','los mensajes y las llamadas',
    'haz clic para cambiar','desconectado','conectando',
    'mensajes temporales','reenviado','reenvió',
    'últ. vez','ult. vez','última vez','ultima vez','hoy a la','ayer a la'
];
function esSistema(t) {
    t = t.toLowerCase().trim();
    if (t.length < 6) return true;
    for (var i = 0; i < SISTEMA.length; i++)
        if (t.indexOf(SISTEMA[i]) !== -1) return true;
    return false;
}
var panel = document.querySelector('[data-testid="conversation-panel-messages"]') ||
            document.querySelector('[data-testid="msg-list"]') ||
            document.querySelector('div[tabindex="-1"]');
var pLeft  = panel ? panel.getBoundingClientRect().left  : 0;
var pW     = panel ? panel.getBoundingClientRect().width : window.innerWidth;
var centro = pLeft + pW / 2;

// Texto
document.querySelectorAll('[data-testid="selectable-text"]').forEach(function(s) {
    var txt = s.innerText.trim();
    if (!txt || txt.length < 2 || esSistema(txt)) return;
    var enHeader = false, el = s;
    for (var k = 0; k < 8; k++) {
        if (!el) break;
        var tid = el.getAttribute('data-testid') || '';
        if (tid === 'conversation-header' || tid === 'chatlist-header' || tid === 'status-v3')
            { enHeader = true; break; }
        el = el.parentElement;
    }
    if (enHeader) return;
    var r = s.getBoundingClientRect();
    var esCliente = (r.left + r.width / 2) < centro;
    todosMsg.push({ texto: txt, esCliente: esCliente });
    if (esCliente) resultados.push(txt);
});

// Imagen / video (audio se detecta después por botón DOM)
document.querySelectorAll('img[src^="blob:"], video').forEach(function(el) {
    var r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    if ((r.left + r.width / 2) < centro) mediaEls.push(el);
});

var ultimoMio = todosMsg.length > 0
    ? !todosMsg[todosMsg.length - 1].esCliente
    : null;

return { mensajes: resultados, ultimoEsMio: ultimoMio,
         mediaElementos: mediaEls, centroPanel: centro };
"""

_JS_BOTONES_AUDIO = """
var panel = document.querySelector('[data-testid="conversation-panel-messages"]') ||
            document.querySelector('[data-testid="msg-list"]') ||
            document.querySelector('div[tabindex="-1"]');
var pLeft  = panel ? panel.getBoundingClientRect().left  : 0;
var pW     = panel ? panel.getBoundingClientRect().width : window.innerWidth;
var centro = pLeft + pW / 2;
var res = [];
document.querySelectorAll(
    'button[aria-label*="voz" i], button[aria-label*="voice" i], ' +
    'button[aria-label*="audio" i], button[aria-label*="play" i]'
).forEach(function(b) {
    var r = b.getBoundingClientRect();
    var cx = r.left + r.width / 2;
    if (cx < centro) res.push([cx, r.top + r.height / 2]);
});
return res;
"""

_JS_CLIC_COORD = """
var el = document.elementFromPoint(arguments[0], arguments[1]);
if (el) { el.click(); return true; } return false;
"""

_JS_BLOB_A_BASE64 = """
var cb = arguments[arguments.length - 1];
fetch(arguments[0])
    .then(function(r){ return r.blob(); })
    .then(function(b){
        var fr = new FileReader();
        fr.onloadend = function(){ cb(fr.result); };
        fr.onerror   = function(){ cb(null); };
        fr.readAsDataURL(b);
    }).catch(function(){ cb(null); });
"""


# ─────────────────────────────────────────────────────────────────────────────
# CLASE 1 – GestorCarpetas
# ─────────────────────────────────────────────────────────────────────────────
class GestorCarpetas:
    """Crea media/<numero>/audios/ e media/<numero>/imagenes/ bajo demanda."""

    def __init__(self, base: Path = MEDIA_DIR):
        self.base = base

    def audios(self, numero: str) -> Path:
        ruta = self.base / numero / "audios"
        ruta.mkdir(parents=True, exist_ok=True)
        return ruta

    def imagenes(self, numero: str) -> Path:
        ruta = self.base / numero / "imagenes"
        ruta.mkdir(parents=True, exist_ok=True)
        return ruta


# ─────────────────────────────────────────────────────────────────────────────
# CLASE 2 – CapturadorAudio
# ─────────────────────────────────────────────────────────────────────────────
class CapturadorAudio:
    """
    Instala un hook triple en el navegador que captura el audio de WhatsApp
    por tres vectores simultáneos:

        1. AudioContext.decodeAudioData  – buffer crudo de Web Audio API.
        2. URL.createObjectURL           – Blob de tipo audio.
        3. fetch()                       – respuestas HTTP con Content-Type audio.

    El hook se instala vía CDP (addScriptToEvaluateOnNewDocument) para que
    esté activo ANTES de que cargue el JavaScript de WhatsApp, de modo que
    no se pierda ninguna decodificación que ocurra al abrir el chat.
    """

    _HOOK = """
(function() {
    if (window.__audioCapHook) return;
    window.__audioCapHook = true;
    window.__audioCaps    = [];

    function bufToB64(buf) {
        var b = new Uint8Array(buf), s = '';
        for (var i = 0; i < b.length; i += 0x8000)
            s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
        return btoa(s);
    }
    function blobToB64(blob) {
        return new Promise(function(resolve) {
            var r = new FileReader();
            r.onloadend = function() { resolve(r.result.split(',')[1]); };
            r.readAsDataURL(blob);
        });
    }

    // Vector 1: AudioContext.decodeAudioData
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) {
        var _dec = Ctx.prototype.decodeAudioData;
        Ctx.prototype.decodeAudioData = function(buf) {
            try { window.__audioCaps.push(bufToB64(buf.slice(0))); } catch(e) {}
            return _dec.apply(this, arguments);
        };
    }

    // Vector 2: URL.createObjectURL con Blob de audio
    var _cobj = URL.createObjectURL;
    URL.createObjectURL = function(obj) {
        var url = _cobj.call(URL, obj);
        if (obj instanceof Blob && obj.type && obj.type.indexOf('audio') !== -1)
            blobToB64(obj).then(function(b64) { window.__audioCaps.push(b64); });
        return url;
    };

    // Vector 3: fetch que devuelva audio
    var _fetch = window.fetch;
    window.fetch = function(url, opts) {
        return _fetch.apply(this, arguments).then(function(resp) {
            var ct = resp.headers.get('content-type') || '';
            if (ct.indexOf('audio') !== -1 || ct.indexOf('ogg') !== -1 || ct.indexOf('opus') !== -1)
                resp.clone().blob().then(function(bl) {
                    blobToB64(bl).then(function(b64) { window.__audioCaps.push(b64); });
                });
            return resp;
        });
    };
})();
"""
    _LEER    = "return window.__audioCaps || [];"
    _LIMPIAR = "window.__audioCaps = []; window.__audioCapHook = false;"

    def __init__(self, driver: webdriver.Edge):
        self.driver = driver

    def instalar_cdp(self) -> None:
        """Registra el hook para todas las páginas futuras (antes de navegar)."""
        self.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": self._HOOK}
        )

    def reforzar(self) -> None:
        """Inyecta el hook en la página actual (por si CDP no alcanzó)."""
        self.driver.execute_script(self._HOOK)

    def limpiar(self) -> None:
        self.driver.execute_script(self._LIMPIAR)

    def leer(self) -> list[str]:
        return self.driver.execute_script(self._LEER) or []

    def esperar_captura(self, timeout_seg: int = 8) -> Optional[str]:
        """Sondea el buffer hasta que aparezca al menos una captura."""
        fin = time.time() + timeout_seg
        while time.time() < fin:
            caps = self.leer()
            if caps:
                return caps[0]
            time.sleep(0.5)
        return None

    def capturar_con_clic(self, x: float, y: float, timeout_seg: int = 8) -> Optional[str]:
        """Limpia, inyecta hook, hace clic y espera la captura."""
        self.limpiar()
        self.reforzar()
        self.driver.execute_script(_JS_CLIC_COORD, x, y)
        logger.info("Clic en botón de audio (%.0f, %.0f)", x, y)
        captura = self.esperar_captura(timeout_seg)
        if not captura:
            # Segundo intento con más espera
            self.limpiar()
            self.reforzar()
            self.driver.execute_script(_JS_CLIC_COORD, x, y)
            logger.info("Segundo intento de captura de audio...")
            captura = self.esperar_captura(6)
        return captura


# ─────────────────────────────────────────────────────────────────────────────
# CLASE 3 – ProcesadorMedios
# ─────────────────────────────────────────────────────────────────────────────
class ProcesadorMedios:
    """Descarga blobs de imagen/video y transcribe audios capturados."""

    def __init__(self, driver: webdriver.Edge, gestor: GestorCarpetas, capturador: CapturadorAudio):
        self.driver     = driver
        self.gestor     = gestor
        self.capturador = capturador

    # ── helpers ───────────────────────────────────────────────────────────
    def _mime_de_data_url(self, data_url: str) -> str:
        try:
            return data_url.split(",", 1)[0].split(":", 1)[1].split(";", 1)[0]
        except Exception:
            return ""

    def _extension(self, mime: str, defecto: str) -> str:
        return EXTENSIONES_MIME.get(mime, defecto)

    def _detectar_ext_audio(self, b64: str) -> str:
        try:
            frag = b64[:64]
            frag = frag[: len(frag) - len(frag) % 4]
            hdr  = base64.b64decode(frag)
        except Exception:
            return ".ogg"
        if hdr.startswith(b"OggS"):           return ".ogg"
        if len(hdr) >= 8 and hdr[4:8] == b"ftyp": return ".m4a"
        if hdr.startswith(b"ID3"):             return ".mp3"
        if hdr.startswith(b"RIFF"):            return ".wav"
        return ".ogg"

    def _descargar_blob(self, blob_url: str, carpeta: Path, nombre: str, ext_def: str) -> Optional[Path]:
        self.driver.set_script_timeout(30)
        data_url = self.driver.execute_async_script(_JS_BLOB_A_BASE64, blob_url)
        if not data_url:
            return None
        try:
            _, b64 = data_url.split(",", 1)
            contenido = base64.b64decode(b64)
        except Exception as exc:
            logger.error("Error decodificando blob: %s", exc)
            return None
        ext  = self._extension(self._mime_de_data_url(data_url), ext_def)
        ruta = carpeta / f"{nombre}{ext}"
        ruta.write_bytes(contenido)
        logger.info("Guardado: %s", ruta)
        return ruta

    # ── imagen ────────────────────────────────────────────────────────────
    def procesar_imagen(self, elemento, numero: str, nombre: str) -> str:
        """Descarga imagen en media/<numero>/imagenes/ y aplica OCR."""
        blob_url = elemento.get_attribute("src") or ""
        if not blob_url.startswith("blob:"):
            return ""
        try:
            carpeta = self.gestor.imagenes(numero)
            ruta    = self._descargar_blob(blob_url, carpeta, nombre, ".jpg")
            if ruta is None:
                return ""
            return extraer_texto_imagen(ruta)
        except Exception as exc:
            logger.warning("Error procesando imagen: %s", exc)
            return ""

    # ── video ─────────────────────────────────────────────────────────────
    def procesar_video(self, elemento, numero: str, nombre: str) -> str:
        """Descarga video en media/<numero>/audios/ y transcribe su audio."""
        blob_url = elemento.get_attribute("src") or elemento.get_attribute("currentSrc") or ""
        if not blob_url.startswith("blob:"):
            try:
                elemento.click()
            except Exception:
                pass
            for _ in range(6):
                time.sleep(0.5)
                blob_url = self.driver.execute_script(
                    "return arguments[0].currentSrc || arguments[0].src;", elemento
                ) or ""
                if blob_url.startswith("blob:"):
                    break
        if not blob_url.startswith("blob:"):
            logger.warning("No se detectó blob de video.")
            return ""
        try:
            carpeta = self.gestor.audios(numero)
            ruta    = self._descargar_blob(blob_url, carpeta, nombre, ".mp4")
            if ruta is None:
                return ""
            return transcribir_audio(ruta)
        except Exception as exc:
            logger.warning("Error procesando video: %s", exc)
            return ""

    # ── audio (triple hook) ───────────────────────────────────────────────
    def procesar_audio(self, x: float, y: float, numero: str, nombre: str) -> str:
        """
        Captura el audio del cliente usando el triple hook y lo transcribe.

        Flujo:
            1. Limpia buffer → inyecta hook → hace clic.
            2. Espera hasta 8 s que el hook capture el audio.
            3. Si no hay captura → segundo intento con 6 s adicionales.
            4. Guarda en media/<numero>/audios/ y llama al STT.
        """
        try:
            b64 = self.capturador.capturar_con_clic(x, y)
            if not b64:
                logger.warning("Audio en (%.0f, %.0f): no capturado.", x, y)
                return ""
            carpeta = self.gestor.audios(numero)
            ext     = self._detectar_ext_audio(b64)
            ruta    = carpeta / f"{nombre}{ext}"
            ruta.write_bytes(base64.b64decode(b64))
            logger.info("Audio guardado: %s", ruta)
            return transcribir_audio(ruta)
        except Exception as exc:
            logger.warning("Error procesando audio: %s", exc)
            return ""


# ─────────────────────────────────────────────────────────────────────────────
# CLASE 4 – ProcesadorWhatsApp  (orquestador principal)
# ─────────────────────────────────────────────────────────────────────────────
class ProcesadorWhatsApp:
    """
    Orquesta el pipeline completo:
        Azure (leer) → WhatsApp Web → texto/imagen/audio → Azure (escribir).
    """

    def __init__(self):
        self.gestor      = GestorCarpetas()
        self.driver: Optional[webdriver.Edge] = None
        self.capturador: Optional[CapturadorAudio] = None
        self.procesador: Optional[ProcesadorMedios] = None
        self.table_client: Optional[TableClient] = None

    # ── inicialización ────────────────────────────────────────────────────
    def iniciar(self) -> None:
        """Conecta a Azure y abre el navegador con el hook de audio instalado."""
        self.table_client = TableClient.from_connection_string(
            AZURE_CONNECTION_STRING, TABLAS["directorio"]
        )

        opts = Options()
        opts.add_argument("--start-maximized")
        opts.add_argument(f"--user-data-dir={PERFIL_WHATSAPP_DIR}")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        svc = Service(EdgeChromiumDriverManager().install())

        self.driver = webdriver.Edge(service=svc, options=opts)
        self.driver.set_page_load_timeout(30)

        self.capturador = CapturadorAudio(self.driver)
        self.capturador.instalar_cdp()   # antes de navegar → cubre todas las páginas

        self.procesador = ProcesadorMedios(self.driver, self.gestor, self.capturador)

        self.driver.get("https://web.whatsapp.com")
        input("\nEscanea el QR si es necesario y presiona ENTER para continuar...\n")

    def cerrar(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass

    # ── loop principal ────────────────────────────────────────────────────
    def procesar_todos(self) -> None:
        """Recorre todas las entidades con Respuesta_Recibida = NO."""
        entidades = list(
            self.table_client.query_entities(query_filter="Respuesta_Recibida eq 'NO'")
        )
        logger.info("Contactos pendientes: %d", len(entidades))
        if not entidades:
            logger.info("No hay contactos pendientes.")
            return

        for entity in entidades:
            try:
                self._procesar_contacto(entity)
            except Exception as exc:
                phone = entity.get("phone", "?")
                logger.error("Error fatal en %s: %s", phone, exc)

    # ── procesar un contacto ──────────────────────────────────────────────
    def _procesar_contacto(self, entity: dict) -> None:
        phone = str(entity.get("phone", "")).replace("-", "").replace(" ", "")
        logger.info("─── Procesando: %s ───", phone)

        # Navegar al chat
        self._navegar_chat(phone)

        # Esperar elementos del chat
        resultado = self._esperar_y_extraer()
        if resultado is None:
            logger.warning("%s: chat no cargó.", phone)
            return

        mensajes      = resultado.get("mensajes", [])
        ultimo_mio    = resultado.get("ultimoEsMio")
        media_els     = resultado.get("mediaElementos", [])

        logger.info(
            "Msgs cliente: %d | Medios DOM: %d | Último mío: %s",
            len(mensajes), len(media_els), ultimo_mio,
        )

        if ultimo_mio is None:
            logger.info("Chat vacío, se omite.")
            return

        partes: list[str] = []

        # 1. Texto directo del cliente
        if mensajes:
            partes.append(" | ".join(mensajes[-ULTIMOS_MENSAJES:]))

        # 2. Imágenes y videos del cliente (detectados por DOM)
        for idx, el in enumerate(media_els):
            nombre = f"{int(time.time())}_{idx}"
            tag    = el.tag_name.lower()
            texto  = ""
            if tag == "img":
                texto = self.procesador.procesar_imagen(el, phone, nombre)
                if texto:
                    partes.append(f"[imagen]: {texto}")
            else:
                texto = self.procesador.procesar_video(el, phone, nombre)
                if texto:
                    partes.append(f"[video]: {texto}")
            time.sleep(0.3)

        # 3. Audios (botones DOM → triple hook)
        self._procesar_audios_chat(phone, partes)

        if not partes:
            logger.info("%s: sin respuesta procesable.", phone)
            return

        texto_final = " | ".join(partes)
        logger.info("Respuesta consolidada (%d chars): %s", len(texto_final), texto_final[:120])

        self._guardar_azure(entity, texto_final)

    # ── navegación ────────────────────────────────────────────────────────
    def _navegar_chat(self, phone: str) -> None:
        url = (
            f"https://web.whatsapp.com/send/?phone={CODIGO_PAIS}{phone}"
            "&text&app_absent=0"
        )
        try:
            self.driver.get(url)
        except Exception:
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                pass

    # ── extracción DOM ────────────────────────────────────────────────────
    def _esperar_y_extraer(self) -> Optional[dict]:
        try:
            WebDriverWait(self.driver, ESPERA_CHAT_SEG).until(
                EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true']"))
            )
        except Exception:
            return None

        try:
            WebDriverWait(self.driver, ESPERA_MENSAJES_SEG).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="selectable-text"]'))
            )
            time.sleep(3)
        except Exception:
            time.sleep(6)

        try:
            return self.driver.execute_script(_JS_EXTRAER_TODO)
        except Exception as exc:
            logger.error("Error extrayendo DOM: %s", exc)
            return None

    # ── audios ────────────────────────────────────────────────────────────
    def _procesar_audios_chat(self, phone: str, partes: list[str]) -> None:
        """Detecta botones de audio, captura y transcribe cada uno."""
        # Reforzar hook antes de buscar audios (por si la navegación lo perdió)
        self.capturador.reforzar()
        time.sleep(4)  # dar tiempo al preload de WhatsApp

        # Revisar si ya hay audio preloaded en el buffer
        caps_previas = self.capturador.leer()
        logger.info("Audios en buffer (preloaded): %d", len(caps_previas))

        # Buscar botones de audio del cliente en el DOM
        coords: list = []
        for _ in range(3):
            coords = self.driver.execute_script(_JS_BOTONES_AUDIO) or []
            if coords:
                break
            time.sleep(1)
        logger.info("Botones de audio (DOM): %d", len(coords))

        for i, (x, y) in enumerate(coords):
            nombre = f"{int(time.time())}_audio_{i}"

            # Si ya estaba capturado en preload, usar esa captura directamente
            if i < len(caps_previas):
                logger.info("Audio %d: usando captura preloaded.", i)
                try:
                    b64     = caps_previas[i]
                    ext     = self.procesador._detectar_ext_audio(b64)
                    carpeta = self.gestor.audios(phone)
                    ruta    = carpeta / f"{nombre}{ext}"
                    ruta.write_bytes(base64.b64decode(b64))
                    logger.info("Audio preloaded guardado: %s", ruta)
                    texto = transcribir_audio(ruta)
                except Exception as exc:
                    logger.warning("Error guardando audio preloaded: %s", exc)
                    texto = ""
            else:
                texto = self.procesador.procesar_audio(x, y, phone, nombre)

            if texto:
                partes.append(f"[audio]: {texto}")
            time.sleep(0.3)

    # ── persistencia Azure ────────────────────────────────────────────────
    def _guardar_azure(self, entity: dict, texto_final: str) -> None:
        """
        Actualiza la entidad en Azure Table Storage:
            Texto_Respuesta  = texto consolidado (texto + transcripciones)
            Respuesta_Recibida = 'SI'
            Fecha_Verificacion = timestamp actual
        """
        try:
            entity["Texto_Respuesta"]    = texto_final[:32000]  # límite Azure Tables
            entity["Respuesta_Recibida"] = "SI"
            entity["Fecha_Verificacion"] = time.strftime("%d/%m/%Y, %H:%M:%S")
            self.table_client.update_entity(mode="merge", entity=entity)
            logger.info("✅ Azure actualizado: Respuesta_Recibida=SI")
        except Exception as exc:
            logger.error("Error guardando en Azure: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# PUNTO DE ENTRADA
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    pipeline = ProcesadorWhatsApp()
    try:
        pipeline.iniciar()
        pipeline.procesar_todos()
    except KeyboardInterrupt:
        logger.info("Interrumpido por el usuario.")
    except Exception as exc:
        logger.exception("Error no controlado: %s", exc)
    finally:
        pipeline.cerrar()
        logger.info("Pipeline finalizado.")


if __name__ == "__main__":
    main()
