# solo_nube — Pipeline de contactos + IA local + Azure

Pipeline que limpia una base de contactos, valida y gestiona conversaciones
de WhatsApp Web, analiza localmente el contenido multimedia recibido (audio
e imágenes) con IA local, y persiste todo en Azure Table Storage.

## Estructura

```
solo_nube - Copy/
├── config.py                  # Configuración centralizada (Azure, rutas, logging)
├── requirements.txt
├── 1-Data_cleaning/           # Limpieza de Excel + carga a Azure (tablas cruda/limpia)
├── 2-Validation_wtp/          # Valida qué números tienen WhatsApp (Selenium)
├── 3-Envio_mjs/                # Envía mensajes de cotización iniciales (Selenium)
├── 4-Guarda_mjs_recep/        # procesar_respuestas.py: lee texto + audio/imágenes en una sola visita (Selenium)
├── 5-envio_mjs_repuesta/      # Envía agradecimientos y sube el reporte final
├── 6-Data_storege/            # Archiva el directorio a histórico y resetea seguimientos
└── ia_local/                  # Módulo de IA 100% local (OCR, audio, texto)
    ├── image_ocr.py           # Tesseract (OCR de imágenes)
    ├── audio_transcriber.py   # Whisper (transcripción de audio)
    ├── text_analyzer.py       # Transformers (sentimiento / categorización)
    └── pipeline.py            # Orquestador del análisis multimedia
```

## Setup

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate

pip install -r requirements.txt
```

Dependencias externas que no se instalan con `pip`:

* **Tesseract OCR** (binario del sistema, no solo el paquete `pytesseract`) — necesario
  para `ia_local.image_ocr`. Windows: https://github.com/UB-Mannheim/tesseract/wiki.
  Si después de instalarlo sigue dando el error "tesseract is not installed or it's not
  in your PATH", escribe la ruta exacta en `config.TESSERACT_CMD` (ver comentario ahí).
* **ffmpeg** (binario del sistema) — necesario para `ia_local.audio_transcriber` (Whisper).
  Windows: https://www.gyan.dev/ffmpeg/builds/ (agregar la carpeta `bin` al PATH).
* **Microsoft Edge** — necesario para los scripts de Selenium (2, 3, 4, 5).

## Orden de ejecución

1. `1-Data_cleaning/Data_cleaning.py` — limpia el Excel local (no requiere Azure).
2. `1-Data_cleaning/tablas_env.py` — sube datos crudos y limpios a Azure.
3. `2-Validation_wtp/validacion_st.py` — valida qué números tienen WhatsApp.
4. `3-Envio_mjs/envio_stg.py` — envía el primer mensaje a los contactos validados.
5. `4-Guarda_mjs_recep/procesar_respuestas.py` — abre cada chat pendiente **una sola vez**:
   extrae el texto del cliente, descarga sus imágenes/audios/videos a `media_whatsapp/`
   (quedan guardados ahí para que los puedas verificar manualmente), los transcribe/OCRea
   con `ia_local`, calcula sentimiento/categoría, y sube todo combinado a Azure en una sola
   actualización. Reemplaza a los antiguos `envo_stg_save.py` y `descargar_medios.py`
   (conservados como `.bak` en la misma carpeta), y ya no es necesario correr
   `ia_local/pipeline.py` por separado para respuestas nuevas (ese script sigue disponible
   para reprocesar respuestas viejas si hace falta).
6. `5-envio_mjs_repuesta/res_final.py` — envía agradecimientos a quien respondió.
7. `5-envio_mjs_repuesta/envio_storege.py` — sube el reporte final a Azure.
8. `6-Data_storege/envio_storege.py` — archiva el directorio y resetea seguimientos vencidos.

## Notas importantes

* **Tablas de Azure**: el flujo de nombres de tabla entre el paso 3 (valida
  hacia `DataValidadaWtp`) y el paso 4 (envía desde `Directoriowtpp`) no es
  automático — son tablas distintas en la cuenta actual. Revisa
  `config.py` (sección "Mapa del pipeline") antes de ejecutar en producción.
* **Histórico**: se mantiene únicamente en Azure (`Historicowtp`); no hay
  base de datos local SQLite para este respaldo (decisión del usuario).
* **Credenciales**: la cadena de conexión de Azure sigue hardcodeada en
  `config.py` por decisión explícita del usuario. Si este código se
  comparte o se sube a un repositorio, se recomienda **rotar la clave de
  la cuenta de Azure**, ya que quedaría expuesta en texto plano.
* **Automatización de WhatsApp Web**: los scripts de los pasos 2 a 5 (y
  `4-Guarda_mjs_recep`) automatizan WhatsApp Web vía Selenium, lo cual está
  fuera de los Términos de Servicio oficiales de WhatsApp. No se modificó
  ni se reforzó esa lógica en este refactor — solo se reorganizó y
  documentó el código existente.
