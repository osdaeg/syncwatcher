# SyncWatcher

Script Python dockerizado que monitorea carpetas de [Syncthing](https://syncthing.net/) y ejecuta automáticamente un pipeline sobre los archivos nuevos: escaneo antivirus, generación de fichas con IA y transferencia a destinos.

---

## ¿Qué hace?

Cuando Syncthing termina de sincronizar archivos en una carpeta monitoreada, SyncWatcher:

1. **Detecta** los archivos nuevos via la Event API de Syncthing (long-polling)
2. **Pausa** la sincronización de la carpeta mientras procesa
3. **Notifica** vía Gotify que llegaron archivos nuevos
4. **Clasifica** cada archivo por tipo según su extensión (libros, música, videos, etc.)
5. **Escanea** con ClamAV los tipos configurados — los infectados se eliminan automáticamente
6. **Genera fichas** HTML con reseña vía IA usando Butler para los tipos configurados
7. **Transfiere** los archivos a sus destinos (copy o move) según el tipo y la carpeta, respetando subcarpetas si se configura
8. **Reanuda** la sincronización y envía notificación de cierre

Cada paso y cada tipo de archivo se configura de forma independiente por carpeta, todo desde el archivo `.env` sin tocar el script.

---

## Dependencias

| Servicio | Descripción |
|---|---|
| [Syncthing](https://syncthing.net/) | Sincronización de archivos entre dispositivos |
| [Transferr](https://codeberg.org/osdaeg/transferr) | Microservicio de transferencia de archivos a destinos montados |
| [Butler](https://codeberg.org/osdaeg/butler) | Generador de fichas HTML con reseña vía Gemini |
| [clamav-rest-api](https://github.com/benzino77/clamav-rest-api) | API REST para escaneo antivirus con ClamAV |
| [Gotify](https://gotify.net/) | Servidor de notificaciones push |

---

## Requisitos

- Docker y Docker Compose
- Red Docker externa (por defecto `GeneralNetwork`)
- Los servicios de dependencias corriendo en la misma red Docker

---

## Instalación

### 1. Clonar el repositorio

```bash
git clone https://codeberg.org/osdaeg/syncwatcher
cd syncwatcher
```

### 2. Configurar el entorno

```bash
cp syncwatcher.env.example syncwatcher.env
```

Editar `syncwatcher.env` con los valores correspondientes (ver sección [Configuración](#configuración)).

### 3. Ajustar el volumen de Syncthing en docker-compose.yml

Cambiar la ruta del host por la ruta real donde tenés montadas las carpetas de Syncthing:

```yaml
volumes:
  - /ruta/real/syncthing:/var/syncthing:ro
```

### 4. Construir y levantar

```bash
docker compose up -d --build
```

Los cambios en `syncwatcher.env` aplican con restart sin rebuild:

```bash
docker compose restart syncwatcher
```

---

## Configuración

Toda la configuración vive en `syncwatcher.env`. El archivo tiene tres secciones: servicios, tipos de archivo y carpetas.

### Servicios

```env
# --- Syncthing ---
SYNCTHING_HOST=syncthing          # nombre del contenedor o IP
SYNCTHING_PORT=8384
SYNCTHING_API_KEY=tu_api_key

# --- Gotify ---
GOTIFY_HOST=gotify                # admite IP si Gotify no está en Docker
GOTIFY_PORT=8088
GOTIFY_TOKEN=tu_token
SEND_NOTIFICATION=yes             # yes/no

# --- ClamAV REST ---
CLAMAV_HOST=nombre-contenedor
CLAMAV_PORT=8080                  # puerto interno del contenedor

# --- Transferr ---
TRANSFERR_HOST=transferr
TRANSFERR_PORT=8000               # puerto interno del contenedor

# --- Butler ---
BUTLER_HOST=butler-api
BUTLER_PORT=8000                  # puerto interno del contenedor

# --- General ---
LOG_DIR=/var/log/syncwatcher
IDLE_TIMER=120                    # segundos sin nuevos archivos antes de procesar el lote
```

> **Nota sobre puertos:** dentro de la red Docker los servicios se comunican por nombre de contenedor y puerto **interno**, no por el puerto mapeado al host.

---

### Tipos de archivo

Cada tipo agrupa extensiones y define sus destinos de transferencia. Los destinos son nombres de montajes configurados en Transferr.

```env
TYPE1_NAME="LIBROS"
TYPE1_EXT="epub mobi azw3 azw djvu fb2 lit lrf"
TYPE1_DESTINATIONS="calibre booklore"

TYPE2_NAME="MUSICA"
TYPE2_EXT="mp3 flac ogg m4a wav aac opus wma ape alac"
TYPE2_DESTINATIONS="slskd"

TYPE3_NAME="DOCUMENTOS"
TYPE3_EXT="doc docx odt rtf txt md csv xls xlsx ods ppt pptx odp"
TYPE3_DESTINATIONS="docs"

TYPE4_NAME="BINARIOS"
TYPE4_EXT="exe dll bat cmd sh bin apk dmg pkg msi deb rpm"
TYPE4_DESTINATIONS="downloads"

TYPE5_NAME="IMAGENES"
TYPE5_EXT="jpg jpeg png gif bmp webp tiff svg heic heif raw cr2 nef"
TYPE5_DESTINATIONS="pics"
TYPE5_PRESERVE_SUBFOLDERS="yes"

TYPE6_NAME="PDF"
TYPE6_EXT="pdf"
TYPE6_DESTINATIONS="pdf"

TYPE7_NAME="VIDEOS"
TYPE7_EXT="mp4 mkv avi mov wmv flv webm m4v mpeg ts"
TYPE7_DESTINATIONS="videos"

TYPE8_NAME="COMICS"
TYPE8_EXT="cbz cbr cb7 cbt"
TYPE8_DESTINATIONS="comics"

TYPE9_NAME="TORRENTS"
TYPE9_EXT="torrent"
TYPE9_DESTINATIONS="torrents"

TYPE10_NAME="COMPRIMIDOS"
TYPE10_EXT="zip rar 7z tar gz bz2 xz zst tgz"
TYPE10_DESTINATIONS="downloads"
```

**`TYPEn_PRESERVE_SUBFOLDERS`** (opcional, por defecto `no`): si es `yes`, al transferir se respeta la subcarpeta relativa del archivo dentro de la carpeta monitoreada. Por ejemplo `fotos/viaje/img.jpg` se transfiere a `pics/fotos/viaje/img.jpg` en lugar de `pics/img.jpg`.

Se pueden agregar tantos tipos como se necesite incrementando el número.

> **Atención:** los números deben ser únicos y consecutivos. Si dos tipos tienen el mismo número, el segundo pisa al primero.

---

### Carpetas monitoreadas

```env
FOLDER1_NAME="Computadora"                          # nombre descriptivo
FOLDER1_ID="abc123-def456"                          # ID de la carpeta en Syncthing
FOLDER1_SCAN="BINARIOS COMPRIMIDOS DOCUMENTOS"      # tipos a escanear con ClamAV
FOLDER1_COPY=""                                     # tipos a transferir conservando original
FOLDER1_MOVE="MUSICA LIBROS COMICS PDF VIDEOS ..."  # tipos a transferir eliminando original
FOLDER1_CARDS="LIBROS MUSICA COMICS"                # tipos para los que generar fichas
```

- `SCAN` vacío → no se escanea ningún archivo de esa carpeta.
- `COPY` y `MOVE` vacíos → no se transfiere nada.
- `CARDS` vacío → no se generan fichas.
- Un tipo en `MOVE` elimina el archivo original **solo si todas las transferencias fueron exitosas**.
- Las fichas se generan **antes** de transferir, por lo que el archivo siempre existe al momento de generar la ficha incluso si es `MOVE`.

Para agregar más carpetas, incrementar el número (`FOLDER2_NAME`, `FOLDER2_ID`, etc.).

#### Ejemplo: carpeta de ingest para Paperless

```env
FOLDER2_NAME="Paperless"
FOLDER2_ID="xyz789-abc123"
FOLDER2_SCAN=""
FOLDER2_COPY=""
FOLDER2_MOVE="PDF DOCUMENTOS"
FOLDER2_CARDS=""
```

---

## Cómo obtener el ID de una carpeta en Syncthing

1. Abrir la interfaz web de Syncthing (`http://host:8384`)
2. Click en la carpeta → **Editar**
3. Copiar el campo **ID de carpeta** (formato `abc123-def456`)

---

## Notas técnicas

### Detección de eventos

Syncthing expone una [Event API](https://docs.syncthing.net/dev/events.html) vía long-polling. SyncWatcher escucha el evento `ItemFinished` — que se dispara cuando un archivo termina de sincronizarse completamente — y acumula los archivos en un lote. El procesamiento se dispara cuando pasan `IDLE_TIMER` segundos sin nuevos archivos, agrupando correctamente envíos en ráfaga.

> El evento `FolderSyncFinished` documentado en la API oficial no se genera cuando el cliente es **Syncthing Fork para Android**. Por eso SyncWatcher usa `ItemFinished` + timer como estrategia de detección.

### Timers independientes por carpeta

Cada carpeta monitoreada tiene su propio timer (`threading.Timer`) y archivo de lote, por lo que múltiples carpetas pueden recibir archivos simultáneamente sin interferencia.

### Consumo de CPU

SyncWatcher está implementado en Python con un único proceso persistente que hace long-polling bloqueante a la API de Syncthing. En idle el consumo es de ~1% CPU.

### ClamAV y límite de tamaño

El contenedor `clamav-rest-api` tiene un límite de tamaño configurable via `APP_MAX_FILE_SIZE`. Los archivos que superen ese límite son reportados como no escaneables y tratados como limpios. Para archivos grandes también puede ser necesario ajustar `MaxScanSize` y `MaxFileSize` en `clamd.conf`.

---

## Logs

Los logs se guardan diariamente en `LOG_DIR` con el formato `syncwatcher_YYYYMMDD.log`, persistidos vía volumen Docker.

```bash
# Ver logs en tiempo real
docker logs syncwatcher -f

# Ver log del día de hoy
docker exec syncwatcher cat /var/log/syncwatcher/syncwatcher_$(date +%Y%m%d).log
```

---

## Licencia

AGPL
