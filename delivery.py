"""Copias de entrega por evento. No mueve, borra ni comparte archivos."""
import io
import json
import re
import threading
import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo('America/Argentina/Mendoza')
FOLDER = 'application/vnd.google-apps.folder'
FIELDS = 'id,name,mimeType,createdTime,size,md5Checksum,appProperties,webViewLink'


def escaped(value):
    return str(value).replace('\\', '\\\\').replace("'", "\\'")


def parse_date(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('Las fechas deben incluir la zona horaria.')
    return dt


def validate_config(data):
    name = str(data.get('name', '')).strip()
    if not name or len(name) > 100 or re.search(r'[\x00-\x1f/\\]', name):
        raise ValueError('Escribí un nombre de evento válido (hasta 100 caracteres).')
    start, end = parse_date(data['start']), parse_date(data['end'])
    if end <= start or (end - start).total_seconds() > 172800:
        raise ValueError('El rango debe cubrir hasta dos días y terminar después del inicio.')
    identity = hashlib.sha256((name + start.isoformat() + end.isoformat()).encode()).hexdigest()[:20]
    return dict(name=name, start=start.isoformat(), end=end.isoformat(), enabled=bool(data.get('enabled')), identity=identity,
                source_id=str(data.get("source_id", "")), album_slug=str(data.get("album_slug", "")), reset_pending=bool(data.get("reset_pending",False)),
                closed=bool(data.get("closed",False)), close_pending=bool(data.get("close_pending",False)),
                folder_name=start.astimezone(TZ).strftime('%Y-%m-%d') + ' — ' + name)


class Delivery:
    def __init__(self, service, root, data_folder, state_name):
        self.service, self.root, self.data_folder, self.state_name = service, root, data_folder, state_name
        self.lock = threading.RLock()
        self.config = None
        self.last_error = None
        self.gallery = None

    def require_drive(self):
        if not self.service or not self.root:
            raise RuntimeError('No hay conexión persistente a Drive. Renovar autorización antes del evento.')

    def list_files(self, parent, extra=''):
        self.require_drive()
        query = f"'{escaped(parent)}' in parents and trashed = false" + (' and ' + extra if extra else '')
        out, token = [], None
        while True:
            result = self.service.files().list(q=query, fields='nextPageToken,files(' + FIELDS + ')', pageSize=100, pageToken=token).execute()
            out.extend(result.get('files', []))
            token = result.get('nextPageToken')
            if not token:
                return out

    def find_folder(self, parent, name, create=False):
        matches = self.list_files(parent, f"mimeType = '{FOLDER}' and name = '{escaped(name)}'")
        if len(matches) > 1:
            raise RuntimeError('Hay carpetas duplicadas: ' + name)
        if matches:
            return matches[0]
        if not create:
            return None
        return self.service.files().create(body=dict(name=name, mimeType=FOLDER, parents=[parent]), fields=FIELDS).execute()

    def write(self, parent, name, content, mime):
        from googleapiclient.http import MediaIoBaseUpload
        existing = self.list_files(parent, f"name = '{escaped(name)}'")
        if len(existing) > 1:
            raise RuntimeError('Hay archivos duplicados: ' + name)
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime, resumable=False)
        if existing:
            saved = self.service.files().update(fileId=existing[0]['id'], media_body=media, fields=FIELDS).execute()
        else:
            saved = self.service.files().create(body=dict(name=name, parents=[parent]), media_body=media, fields=FIELDS).execute()
        if saved.get('md5Checksum') != hashlib.md5(content).hexdigest():
            raise RuntimeError('No se pudo verificar el archivo guardado: ' + name)
        return saved

    def load_config(self):
        with self.lock:
            if self.config is not None:
                return self.config
            self.require_drive()
            files = self.list_files(self.data_folder(), f"name = '{escaped(self.state_name)}'")
            if len(files) > 1:
                raise RuntimeError('Configuración de entrega duplicada.')
            if not files:
                return None
            data = self.service.files().get_media(fileId=files[0]['id']).execute()
            self.config = validate_config(json.loads(data))
            return self.config

    def save_config(self, data):
        with self.lock:
            self.require_drive()
            config = validate_config(data)
            # Leer y verificar el estado anterior antes de reemplazarlo.
            previous = self.load_config()
            if previous and previous['enabled'] and previous['identity'] != config['identity']:
                now = datetime.now(timezone.utc)
                if parse_date(previous['start']) <= now < parse_date(previous['end']):
                    raise ValueError('El evento está en curso. Esperá al cierre para cambiarlo.')
            file = self.write(self.data_folder(), self.state_name, json.dumps(config, ensure_ascii=False).encode(), 'application/json')
            readback = self.service.files().get_media(fileId=file['id']).execute()
            if validate_config(json.loads(readback)) != config:
                raise RuntimeError('No se pudo verificar la configuración guardada.')
            self.config = config
            return config

    def accepts(self, timestamp, config):
        return config and config['enabled'] and parse_date(config['start']).timestamp() <= timestamp < parse_date(config['end']).timestamp()

    def folders(self, config, create=False):
        delivery = self.find_folder(self.root, 'Entregas', create)
        event = self.find_folder(delivery['id'], config['folder_name'], create) if delivery else None
        photos = self.find_folder(event['id'], 'Fotos originales', create) if event else None
        if create and event:
            # Cada nueva entrega nace con su carpeta de videos junto a Fotos originales.
            self.find_folder(event['id'], 'Videos', create=True)
        if create and event and photos and self.gallery:
            self.gallery.register(config, event, photos)
        return event, photos

    def copy_photo(self, source, config):
        self.require_drive()
        if source['mimeType'] == FOLDER or not source.get('md5Checksum'):
            raise RuntimeError('El original no permite verificar su integridad.')
        event, photos = self.folders(config, True)
        marker = f"appProperties has {{ key='sourceFileId' and value='{escaped(source['id'])}' }}"
        existing = self.list_files(photos['id'], marker)
        if len(existing) > 1:
            raise RuntimeError('La entrega contiene una foto duplicada.')
        if existing:
            copied = existing[0]
        else:
            stamp = parse_date(source['createdTime']).astimezone(TZ).strftime('%Y-%m-%d %H.%M.%S')
            suffix = source['name'].rsplit('.', 1)[-1].lower()
            # No incluir el UUID original: moderación busca ese UUID en nombres de todo Drive.
            token = hashlib.sha256(source['id'].encode()).hexdigest()[:10]
            name = f'Foto {stamp} - {token}.{suffix}'
            copied = self.service.files().copy(fileId=source['id'], body=dict(name=name, parents=[photos['id']], appProperties=dict(sourceFileId=source['id'], deliveryEvent=config['identity'])), fields=FIELDS).execute()
        if copied.get('md5Checksum') != source['md5Checksum'] or copied.get('size') != source.get('size'):
            raise RuntimeError('La copia no coincide con el original; entrega pendiente.')
        return event, copied

    def on_photo(self, source_id):
        with self.lock:
            config = self.load_config()
            if not config or not config['enabled']:
                return
            source = self.service.files().get(fileId=source_id, fields=FIELDS).execute()
            if self.accepts(parse_date(source['createdTime']).timestamp(), config):
                self.copy_photo(source, config)

    def on_messages(self, messages):
        with self.lock:
            config = self.load_config()
            if not config or not config['enabled']:
                return
            selected = [m for m in messages if self.accepts(float(m.get('ts', 0)), config)]
            event, _ = self.folders(config, False)
            if not selected and not event:
                return
            event, _ = self.folders(config, True)
            blocks = [config['name'] + '\nSaludos de la fiesta\n']
            for message in sorted(selected, key=lambda m: m['ts']):
                stamp = datetime.fromtimestamp(message['ts'], TZ).strftime('%d/%m/%Y %H:%M')
                blocks.append(f"{stamp} · {message.get('nombre') or 'Invitado'}\n{message.get('texto', '')}")
            self.write(event['id'], 'Saludos.txt', ('\n\n'.join(blocks) + '\n').encode(), 'text/plain')
            self.write(event['id'], 'Saludos.json', json.dumps(selected, ensure_ascii=False, indent=2).encode(), 'application/json')

    def reconcile(self, messages):
        with self.lock:
            config = self.load_config()
            if not config or not config['enabled']:
                raise ValueError('Primero activá la entrega de un evento.')
            originals = self.find_folder(self.root, 'Originales')
            if not originals:
                return dict(verified=0, folder=None)
            sources = self.list_files(originals['id'])
            selected = [s for s in sources if s['mimeType'] != FOLDER and self.accepts(parse_date(s['createdTime']).timestamp(), config)]
            for source in selected:
                self.copy_photo(source, config)
            self.on_messages(messages)
            event, _ = self.folders(config)
            self.last_error = None
            return dict(verified=len(selected), folder=event.get('webViewLink') if event else None)

    def status(self):
        with self.lock:
            config = self.load_config()
            if not config:
                return dict(config=None, verified=0, folder=None, error=self.last_error)
            event, photos = self.folders(config)
            count = len(self.list_files(photos['id'])) if photos else 0
            return dict(config=config, verified=count, folder=event.get('webViewLink') if event else None, error=self.last_error)
