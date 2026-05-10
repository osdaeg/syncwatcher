#!/usr/bin/env python3
# =============================================================================
# SyncWatcher - Script post-sincronización de Syncthing
# =============================================================================

import os
import sys
import json
import time
import threading
import subprocess
import logging
import requests
from datetime import datetime
from pathlib import Path

# -----------------------------------------------------------------------------
# Cargar configuración desde syncwatcher.env
# -----------------------------------------------------------------------------

def load_env(env_file):
    """Lee el archivo .env y carga las variables en os.environ."""
    if not os.path.exists(env_file):
        print(f"[ERROR] No se encontró el archivo {env_file}", file=sys.stderr)
        sys.exit(1)

    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

SCRIPT_DIR = Path(__file__).parent
load_env(SCRIPT_DIR / 'syncwatcher.env')

def env(key, default=''):
    return os.environ.get(key, default)

# -----------------------------------------------------------------------------
# Configuración de logs
# -----------------------------------------------------------------------------

LOG_DIR = Path(env('LOG_DIR', '/var/log/syncwatcher'))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"syncwatcher_{datetime.now().strftime('%Y%m%d')}.log"

class MultiHandler(logging.Handler):
    """Escribe logs al archivo y a stderr simultáneamente."""
    def __init__(self, log_file):
        super().__init__()
        self.log_file = log_file

    def emit(self, record):
        msg = self.format(record)
        print(msg, file=sys.stderr)
        try:
            with open(self.log_file, 'a') as f:
                f.write(msg + '\n')
        except Exception:
            pass

logger = logging.getLogger('syncwatcher')
logger.setLevel(logging.DEBUG)
handler = MultiHandler(LOG_FILE)
handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)-5s] %(message)s',
                                        datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(handler)

# Alias para mantener consistencia con el script bash
def log_info(msg):  logger.info(msg)
def log_warn(msg):  logger.warning(msg)
def log_error(msg): logger.error(msg)
def log_ok(msg):    logger.info('OK    ' + msg)

# -----------------------------------------------------------------------------
# Configuración dinámica de tipos y carpetas
# -----------------------------------------------------------------------------

def get_types():
    """Devuelve lista de dicts con la config de cada tipo definido en el env."""
    types = []
    i = 1
    while True:
        name = env(f'TYPE{i}_NAME')
        if not name:
            break
        types.append({
            'name': name,
            'ext': env(f'TYPE{i}_EXT', '').split(),
            'destinations': env(f'TYPE{i}_DESTINATIONS', '').split(),
            'preserve_subfolders': env(f'TYPE{i}_PRESERVE_SUBFOLDERS', 'no').lower() == 'yes',
        })
        i += 1
    return types

def get_folders():
    """Devuelve lista de dicts con la config de cada carpeta definida en el env."""
    folders = []
    i = 1
    while True:
        folder_id = env(f'FOLDER{i}_ID')
        if not folder_id:
            break
        folders.append({
            'num': i,
            'name': env(f'FOLDER{i}_NAME', f'FOLDER{i}'),
            'id': folder_id,
            'scan': env(f'FOLDER{i}_SCAN', '').split(),
            'copy': env(f'FOLDER{i}_COPY', '').split(),
            'move': env(f'FOLDER{i}_MOVE', '').split(),
            'cards': env(f'FOLDER{i}_CARDS', '').split(),
        })
        i += 1
    return folders

def get_folder_by_id(folder_id, folders):
    for f in folders:
        if f['id'] == folder_id:
            return f
    return None

def get_type_for_ext(ext, types):
    for t in types:
        if ext.lower() in t['ext']:
            return t['name']
    return 'DESCONOCIDO'

def get_destinations_for_type(type_name, types):
    for t in types:
        if t['name'] == type_name:
            return t['destinations']
    return []

def get_preserve_subfolders_for_type(type_name, types):
    for t in types:
        if t['name'] == type_name:
            return t.get('preserve_subfolders', False)
    return False

# -----------------------------------------------------------------------------
# Notificaciones Gotify
# -----------------------------------------------------------------------------

def gotify_notify(title, message, priority=5):
    if env('SEND_NOTIFICATION', 'yes') != 'yes':
        return
    try:
        requests.post(
            f"http://{env('GOTIFY_HOST')}:{env('GOTIFY_PORT')}/message",
            headers={'X-Gotify-Key': env('GOTIFY_TOKEN')},
            data={'title': title, 'message': message, 'priority': priority},
            timeout=10
        )
        log_info(f"Gotify → [P{priority}] {title}: {message}")
    except Exception as e:
        log_error(f"Error enviando notificación Gotify: {e}")

# -----------------------------------------------------------------------------
# Syncthing API
# -----------------------------------------------------------------------------

SYNCTHING_BASE = f"http://{env('SYNCTHING_HOST')}:{env('SYNCTHING_PORT')}"
SYNCTHING_HEADERS = {'X-API-Key': env('SYNCTHING_API_KEY')}

def syncthing_pause_folder(folder_id):
    log_info(f"Pausando sincronización: {folder_id}")
    try:
        requests.post(f"{SYNCTHING_BASE}/rest/db/pause",
                      headers=SYNCTHING_HEADERS,
                      data={'folder': folder_id}, timeout=10)
    except Exception as e:
        log_error(f"Error pausando carpeta: {e}")

def syncthing_resume_folder(folder_id):
    log_info(f"Reanudando sincronización: {folder_id}")
    try:
        requests.post(f"{SYNCTHING_BASE}/rest/db/resume",
                      headers=SYNCTHING_HEADERS,
                      data={'folder': folder_id}, timeout=10)
    except Exception as e:
        log_error(f"Error reanudando carpeta: {e}")

def syncthing_get_folder_path(folder_id):
    try:
        r = requests.get(f"{SYNCTHING_BASE}/rest/config/folders/{folder_id}",
                         headers=SYNCTHING_HEADERS, timeout=10)
        return r.json().get('path', '')
    except Exception as e:
        log_error(f"Error obteniendo path de carpeta {folder_id}: {e}")
        return ''

# -----------------------------------------------------------------------------
# Escaneo antivirus con ClamAV
# -----------------------------------------------------------------------------

def scan_files(folder_cfg, folder_path, files, types):
    scan_types = folder_cfg['scan']
    clean_files = []
    infected_count = 0
    clean_count = 0
    skipped_count = 0

    log_info(f"--- Iniciando escaneo antivirus ({len(files)} archivos) ---")

    for rel_path in files:
        filepath = Path(folder_path) / rel_path
        ext = Path(rel_path).suffix.lstrip('.').lower()
        file_type = get_type_for_ext(ext, types)

        if file_type not in scan_types:
            log_info(f"Omitiendo (no escaneable): {rel_path}")
            clean_files.append(rel_path)
            skipped_count += 1
            continue

        if not filepath.is_file():
            log_warn(f"Archivo no encontrado en disco: {rel_path}")
            continue

        log_info(f"Escaneando [{file_type}]: {rel_path}")
        try:
            with open(filepath, 'rb') as f:
                r = requests.post(
                    f"http://{env('CLAMAV_HOST')}:{env('CLAMAV_PORT')}/api/v1/scan",
                    files={'FILES': (filepath.name, f)},
                    timeout=120
                )
            data = r.json()

            # Manejar error explícito de la API (ej: archivo demasiado grande)
            if not data.get('success', True):
                error_msg = data.get('data', {}).get('error', 'error desconocido')
                log_warn(f"ClamAV no pudo escanear {rel_path}: {error_msg} — se asume limpio")
                clean_files.append(rel_path)
                skipped_count += 1
                continue

            result = data['data']['result']
            if isinstance(result, list):
                result = result[0]

            if result['is_infected']:
                viruses = ', '.join(result.get('viruses', ['desconocido']))
                log_warn(f"INFECTADO: {rel_path} — {viruses}")
                filepath.unlink(missing_ok=True)
                log_warn(f"Eliminado del disco: {rel_path}")
                infected_count += 1
                gotify_notify("⚠️ Virus detectado",
                              f"Archivo infectado eliminado: {filepath.name}\nVirus: {viruses}",
                              priority=10)
            else:
                log_info(f"OK    Limpio: {rel_path}")
                clean_files.append(rel_path)
                clean_count += 1

        except requests.Timeout:
            log_error(f"Timeout escaneando {rel_path} — se asume limpio")
            clean_files.append(rel_path)
        except Exception as e:
            log_error(f"Error escaneando {rel_path}: {e} — se asume limpio")
            clean_files.append(rel_path)

    summary = f"Limpios: {clean_count} | Infectados eliminados: {infected_count} | Omitidos: {skipped_count}"
    log_info(f"Escaneo finalizado — {summary}")
    gotify_notify("🛡️ Escaneo completado", summary, priority=7 if infected_count > 0 else 3)

    return clean_files

# -----------------------------------------------------------------------------
# Transferencia según tipo y configuración de carpeta
# -----------------------------------------------------------------------------

def transfer_files(folder_cfg, folder_path, files, types):
    copy_types = folder_cfg['copy']
    move_types = folder_cfg['move']

    if not copy_types and not move_types:
        log_info("Sin transferencias configuradas para esta carpeta.")
        return

    log_info(f"--- Iniciando transferencias ({len(files)} archivos) ---")
    transferred = 0
    skipped = 0

    for rel_path in files:
        filepath = Path(folder_path) / rel_path
        ext = Path(rel_path).suffix.lstrip('.').lower()
        file_type = get_type_for_ext(ext, types)
        basename = filepath.name

        if not filepath.is_file():
            continue

        is_copy = file_type in copy_types
        is_move = file_type in move_types

        if not is_copy and not is_move:
            log_info(f"Sin destino para: {basename} [{file_type}] — omitido")
            skipped += 1
            continue

        destinations = get_destinations_for_type(file_type, types)
        if not destinations:
            log_warn(f"Tipo {file_type} no tiene destinos definidos — omitido")
            skipped += 1
            continue

        transfer_ok = True
        preserve = get_preserve_subfolders_for_type(file_type, types)
        subfolder = str(Path(rel_path).parent) if preserve and Path(rel_path).parent != Path('.') else None

        for dest in destinations:
            log_info(f"Transfiriendo [{file_type}] → {dest}{f'/{subfolder}' if subfolder else ''}: {basename}")
            try:
                with open(filepath, 'rb') as f:
                    data = {'destination': dest}
                    if subfolder:
                        data['subfolder'] = subfolder
                    r = requests.post(
                        f"http://{env('TRANSFERR_HOST')}:{env('TRANSFERR_PORT')}/transfer",
                        files={'file': (basename, f)},
                        data=data,
                        timeout=300
                    )
                data = r.json()
                if data.get('status') == 'ok' or data.get('success') is True:
                    log_info(f"OK    Transferido a {dest}: {basename}")
                else:
                    log_error(f"Error transfiriendo a {dest}: {basename}")
                    log_error(f"Respuesta Transferr: {data}")
                    transfer_ok = False
            except Exception as e:
                log_error(f"Error transfiriendo {basename} a {dest}: {e}")
                transfer_ok = False

        if is_move and transfer_ok:
            filepath.unlink(missing_ok=True)
            log_info(f"OK    Eliminado original (move): {basename}")

        transferred += 1

    log_info(f"Transferencias finalizadas — Transferidos: {transferred} | Sin destino: {skipped}")

# -----------------------------------------------------------------------------
# Generación de fichas con Butler-API
# -----------------------------------------------------------------------------

def generate_cards(folder_cfg, folder_path, files, types):
    card_types = folder_cfg['cards']

    if not card_types:
        log_info("Sin generación de fichas configurada para esta carpeta.")
        return

    log_info(f"--- Generando fichas con Butler-API ({len(files)} archivos) ---")
    generated = 0
    skipped = 0

    for rel_path in files:
        filepath = Path(folder_path) / rel_path
        ext = Path(rel_path).suffix.lstrip('.').lower()
        file_type = get_type_for_ext(ext, types)
        basename = filepath.name

        if not filepath.is_file():
            continue

        if file_type not in card_types:
            skipped += 1
            continue

        log_info(f"Generando ficha [{file_type}]: {basename}")
        try:
            r = requests.post(
                f"http://{env('BUTLER_HOST')}:{env('BUTLER_PORT')}/process",
                data={'filename': basename},
                timeout=120
            )
            data = r.json()
            if data.get('status') == 'ok':
                titulo = data.get('titulo', basename)
                log_info(f"OK    Ficha generada: {titulo}")
                generated += 1
            else:
                log_error(f"Error generando ficha para: {basename}")
                log_error(f"Respuesta Butler: {data}")
        except Exception as e:
            log_error(f"Error generando ficha para {basename}: {e}")

    log_info(f"Fichas finalizadas — Generadas: {generated} | Omitidas: {skipped}")

# -----------------------------------------------------------------------------
# Procesamiento de una carpeta
# -----------------------------------------------------------------------------

def process_folder(folder_cfg, folder_path, batch_file, types):
    folder_id = folder_cfg['id']
    folder_name = folder_cfg['name']

    log_info("=" * 60)
    log_info(f"Iniciando procesamiento — {folder_name} ({folder_id})")
    log_info("=" * 60)

    syncthing_pause_folder(folder_id)

    # Leer archivos del lote
    new_files = []
    batch = Path(batch_file)
    if batch.is_file():
        seen = set()
        for line in batch.read_text().splitlines():
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                new_files.append(line)
        batch.unlink(missing_ok=True)

    if not new_files:
        log_info("No hay archivos nuevos que procesar.")
        syncthing_resume_folder(folder_id)
        return

    log_info(f"Archivos a procesar: {len(new_files)}")
    types_loaded = get_types()
    for f in new_files:
        ext = Path(f).suffix.lstrip('.').lower()
        ftype = get_type_for_ext(ext, types_loaded)
        log_info(f"  → [{ftype}] {f}")

    gotify_notify("🔄 Sincronización finalizada",
                  f"Carpeta: {folder_name}\nArchivos nuevos: {len(new_files)}\nIniciando procesamiento...")

    # Escaneo
    if folder_cfg['scan']:
        clean_files = scan_files(folder_cfg, folder_path, new_files, types_loaded)
    else:
        log_info("Escaneo desactivado para esta carpeta.")
        clean_files = new_files

    log_info(f"Archivos limpios tras escaneo: {len(clean_files)}")

    if not clean_files:
        log_warn("No quedaron archivos limpios tras el escaneo.")
        syncthing_resume_folder(folder_id)
        return

    # Fichas (antes de transferir para que el archivo exista si es MOVE)
    generate_cards(folder_cfg, folder_path, clean_files, types_loaded)

    # Transferencia (puede eliminar el original si es MOVE)
    transfer_files(folder_cfg, folder_path, clean_files, types_loaded)

    syncthing_resume_folder(folder_id)

    gotify_notify("✅ Procesamiento completado",
                  f"Carpeta: {folder_name}\nArchivos procesados: {len(new_files)}")

    log_info("=" * 60)
    log_info(f"Procesamiento finalizado — {folder_name}")
    log_info("=" * 60)

# -----------------------------------------------------------------------------
# Loop principal
# -----------------------------------------------------------------------------

def main():
    idle_timer = int(env('IDLE_TIMER', '120'))

    # Cargar config inicial
    folders = get_folders()
    types = get_types()
    watched_ids = {f['id'] for f in folders}
    folder_path_cache = {}

    log_info("SyncWatcher iniciado.")
    log_info(f"Syncthing: {SYNCTHING_BASE}")
    log_info(f"Timer de inactividad: {idle_timer}s")
    for f in folders:
        log_info(f"Monitoreando: {f['name']} ({f['id']})")

    # Obtener último event ID para no reprocesar eventos viejos
    try:
        r = requests.get(f"{SYNCTHING_BASE}/rest/events?limit=1",
                         headers=SYNCTHING_HEADERS, timeout=10)
        events = r.json()
        last_event_id = events[-1]['id'] if events else 0
    except Exception:
        last_event_id = 0
    log_info(f"Último event ID al iniciar: {last_event_id}")

    # Estado de timers por carpeta: {folder_id: {'time': timestamp, 'timer': Timer}}
    pending = {}
    pending_lock = threading.Lock()

    def fire_processing(folder_id):
        """Se ejecuta cuando el timer de una carpeta expira."""
        with pending_lock:
            if folder_id not in pending:
                return
            state = pending.pop(folder_id)

        folder_cfg = get_folder_by_id(folder_id, get_folders())
        folder_path = folder_path_cache.get(folder_id, '')

        if not folder_cfg or not folder_path:
            log_error(f"No se pudo procesar carpeta: {folder_id}")
            return

        pending_file = f"/tmp/syncwatcher_pending_{folder_id}"
        batch_file = f"/tmp/syncwatcher_batch_{folder_id}_{int(time.time())}"

        try:
            os.rename(pending_file, batch_file)
        except FileNotFoundError:
            return

        log_info(f"Timer expirado — procesando: {folder_id}")
        process_folder(folder_cfg, folder_path, batch_file, get_types())

    # Long-polling loop
    while True:
        try:
            r = requests.get(
                f"{SYNCTHING_BASE}/rest/events",
                headers=SYNCTHING_HEADERS,
                params={'since': last_event_id, 'timeout': 60},
                timeout=70
            )
            events = r.json()
        except requests.Timeout:
            continue
        except Exception as e:
            log_error(f"Error en long-polling: {e}")
            time.sleep(5)
            continue

        if not events:
            continue

        # Actualizar last_event_id
        last_event_id = max(e.get('id', 0) for e in events)

        # Filtrar solo ItemFinished de carpetas monitoreadas
        for event in events:
            if event.get('type') != 'ItemFinished':
                continue

            data = event.get('data', {})
            folder_id = data.get('folder', '')
            item = data.get('item', '')
            action = data.get('action', '')
            ftype = data.get('type', 'file')
            error = data.get('error', '')

            if not folder_id or folder_id not in watched_ids:
                continue
            if not item or action not in ('update', 'metadata'):
                continue
            if error or ftype == 'dir':
                continue

            log_info(f"ItemFinished: {folder_id} → {item}")

            # Cachear path
            if folder_id not in folder_path_cache:
                folder_path_cache[folder_id] = syncthing_get_folder_path(folder_id)

            # Acumular archivo
            pending_file = f"/tmp/syncwatcher_pending_{folder_id}"
            with open(pending_file, 'a') as f:
                f.write(item + '\n')

            # Resetear timer
            with pending_lock:
                if folder_id in pending and pending[folder_id]['timer']:
                    pending[folder_id]['timer'].cancel()

                timer = threading.Timer(idle_timer, fire_processing, args=[folder_id])
                timer.daemon = True
                timer.start()
                pending[folder_id] = {'timer': timer}

if __name__ == '__main__':
    main()
