"""Persistent album thumbnails. Never changes an original or the live-screen pipeline."""
import fcntl
import hashlib
import io
import json
import re
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from delivery import FOLDER, escaped, photo_uid

VERSION = 'jpeg-512-v1'
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_THUMB_BYTES = 256 * 1024
MEMORY_BYTES = 8 * 1024 * 1024


class AlbumUnavailable(RuntimeError):
    pass


class UnsupportedPreview(ValueError):
    pass


class LimitedWriter:
    def __init__(self, target, limit):
        self.target, self.limit = target, limit

    def write(self, data):
        if self.target.tell() + len(data) > self.limit:
            raise UnsupportedPreview('La foto supera el límite de la vista previa.')
        return self.target.write(data)


class AlbumMedia:
    def __init__(self, delivery):
        self.delivery = delivery
        self.lock = threading.RLock()
        self.generation = threading.BoundedSemaphore(1)
        self.indexes = OrderedDict()
        self.veto_bodies = OrderedDict()
        self.memory = OrderedDict()
        self.memory_size = 0
        self.folders = OrderedDict()

    def vetoes(self):
        """Always list live veto metadata; reuse only checksum-identified JSON bodies."""
        try:
            rows = self.delivery.list_files(self.delivery.data_folder(), "name contains 'moderation-'")
            vetoes = {}
            with self.lock:
                for row in rows:
                    match = re.fullmatch(r'moderation-([0-9a-f]{32})\.json', row['name'])
                    if not match:
                        continue
                    key = (row['id'], row.get('md5Checksum'))
                    data = self.veto_bodies.get(key) if key[1] else None
                    if data is None:
                        data = json.loads(self.delivery.service.files().get_media(fileId=row['id']).execute())
                    if (data.get('uid') != match.group(1) or not isinstance(data.get('source_ids'), list)
                            or any(not isinstance(s, str) or not s for s in data['source_ids'])):
                        raise ValueError('Veto inválido')
                    if key[1]:
                        self.veto_bodies[key] = data
                        self.veto_bodies.move_to_end(key)
                    vetoes.setdefault(data['uid'], set()).update(data['source_ids'])
                while len(self.veto_bodies) > 4096:
                    self.veto_bodies.popitem(last=False)
            return vetoes
        except Exception as error:
            raise AlbumUnavailable('No se pudo verificar la moderación del álbum.') from error

    def photos(self, manifest):
        if not manifest.get('photos_folder'):
            return []
        key = (manifest['photos_folder'], manifest['identity'])
        with self.lock:
            cached = self.indexes.get(key)
            if cached is None or cached[0] <= time.monotonic():
                marker = "appProperties has { key='deliveryEvent' and value='" + escaped(manifest['identity']) + "' }"
                rows = self.delivery.list_files(manifest['photos_folder'], marker)
                rows = sorted([p for p in rows if p['mimeType'] != FOLDER
                               and p.get('appProperties', {}).get('deliveryEvent') == manifest['identity']
                               and p.get('appProperties', {}).get('kind') != 'albumThumbnail'], key=lambda p: p['name'])
                self.indexes[key] = (time.monotonic() + 15, rows)
                self.indexes.move_to_end(key)
                while len(self.indexes) > 8 or sum(len(v[1]) for v in self.indexes.values()) > 20000:
                    self.indexes.popitem(last=False)
            else:
                rows = cached[1]
        # Metadata may be briefly cached; moderation never has a positive TTL.
        vetoes = self.vetoes()
        source_ids = set().union(*vetoes.values()) if vetoes else set()
        allowed = []
        for photo in rows:
            if (photo_uid(photo['name']) in vetoes
                    or (photo.get('appProperties', {}).get('sourceFileId') or photo['id']) in source_ids):
                if photo.get('md5Checksum') and photo.get('appProperties', {}).get('sourceFileId'):
                    self.forget(self.key(photo))
            else:
                allowed.append(photo)
        return allowed

    def allowed(self, photo):
        return not self.delivery.is_vetoed(photo, self.vetoes())

    def key(self, photo):
        checksum = photo.get('md5Checksum')
        if not checksum or not photo.get('appProperties', {}).get('sourceFileId'):
            raise UnsupportedPreview('La foto no tiene una identidad verificable para miniatura.')
        return hashlib.sha256((VERSION + ':' + photo['id'] + ':' + checksum).encode()).hexdigest()

    @contextmanager
    def process_lock(self, key):
        # Shared by gunicorn workers on this host. Fixed stripes bound lock-file count.
        scope = hashlib.sha256(self.delivery.root.encode()).hexdigest()[:16]
        folder = Path(tempfile.gettempdir()) / ('eventphotos-album-' + scope)
        folder.mkdir(mode=0o700, exist_ok=True)
        with (folder / (str(int(key[:4], 16) % 64) + '.lock')).open('a+b') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def remember(self, key, data):
        with self.lock:
            previous = self.memory.pop(key, None)
            if previous is not None:
                self.memory_size -= len(previous)
            self.memory[key] = data
            self.memory_size += len(data)
            while self.memory_size > MEMORY_BYTES:
                _, removed = self.memory.popitem(last=False)
                self.memory_size -= len(removed)

    def forget(self, key):
        with self.lock:
            data = self.memory.pop(key, None)
            if data is not None:
                self.memory_size -= len(data)

    def thumbnail_folder(self, manifest):
        event_folder = manifest.get('event_folder')
        if not event_folder:
            raise UnsupportedPreview('El álbum todavía no tiene una carpeta persistente.')
        with self.lock:
            if event_folder in self.folders:
                self.folders.move_to_end(event_folder)
                return self.folders[event_folder]
        # Resolve the folder before acquiring a photo lock: never nest striped locks.
        folder_key = hashlib.sha256(('folder:' + event_folder).encode()).hexdigest()
        with self.process_lock(folder_key):
            folder = self.delivery.find_folder(event_folder, 'Miniaturas', create=True)
        with self.lock:
            self.folders[event_folder] = folder['id']
            while len(self.folders) > 64:
                self.folders.popitem(last=False)
        return folder['id']

    def render(self, photo):
        from googleapiclient.http import MediaIoBaseDownload
        if int(photo.get('size', MAX_SOURCE_BYTES + 1)) > MAX_SOURCE_BYTES:
            raise UnsupportedPreview('El original es demasiado grande para la vista previa.')
        # Bounded disk spool: avoid holding the original and decoded pixels in RAM together.
        with tempfile.TemporaryFile() as original:
            downloader = MediaIoBaseDownload(LimitedWriter(original, MAX_SOURCE_BYTES),
                                              self.delivery.service.files().get_media(fileId=photo['id']),
                                              chunksize=512 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=2)
            original.seek(0)
            digest = hashlib.md5()
            for block in iter(lambda: original.read(512 * 1024), b''):
                digest.update(block)
            if digest.hexdigest() != photo['md5Checksum']:
                raise AlbumUnavailable('El contenido del original cambió durante la lectura.')
            original.seek(0)
            try:
                with Image.open(original) as image:
                    image.draft('RGB', (512, 512))
                    if image.width * image.height > 12_000_000:
                        raise UnsupportedPreview('La imagen requiere demasiada memoria para una vista previa.')
                    # Resize before RGB conversion, with EXIF orientation retained until transpose.
                    image.thumbnail((512, 512), Image.Resampling.LANCZOS)
                    scaled = ImageOps.exif_transpose(image).convert('RGB')
                    try:
                        buffer = io.BytesIO()
                        scaled.save(buffer, 'JPEG', quality=78, optimize=True, progressive=True)
                        data = buffer.getvalue()
                    finally:
                        scaled.close()
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
                raise UnsupportedPreview('Este formato no permite una vista previa.') from error
        if len(data) > MAX_THUMB_BYTES:
            raise UnsupportedPreview('La miniatura supera el límite de tamaño.')
        return data

    def thumbnail(self, manifest, photo):
        from googleapiclient.http import MediaIoBaseUpload
        key = self.key(photo)
        if not self.allowed(photo):
            self.forget(key)
            return None
        with self.lock:
            data = self.memory.get(key)
            if data is not None:
                self.memory.move_to_end(key)
                return data
        folder = self.thumbnail_folder(manifest)
        with self.generation, self.process_lock(key):
            # A different worker may have generated it while this worker waited.
            if not self.allowed(photo):
                self.forget(key)
                return None
            name = 'thumbnail-' + key + '.jpg'
            existing = self.delivery.list_files(folder, "name = '" + name + "'")
            if len(existing) > 1:
                raise AlbumUnavailable('Hay miniaturas duplicadas; no se confirmó la vista previa.')
            source_id = photo['appProperties']['sourceFileId']
            if existing:
                row = existing[0]
                props = row.get('appProperties', {})
                if (props.get('sourceFileId') != source_id or props.get('albumPhotoId') != photo['id']
                        or props.get('kind') != 'albumThumbnail' or int(row.get('size', MAX_THUMB_BYTES + 1)) > MAX_THUMB_BYTES):
                    raise AlbumUnavailable('La miniatura no coincide con su original.')
                data = self.delivery.service.files().get_media(fileId=row['id']).execute()
                if len(data) > MAX_THUMB_BYTES or hashlib.md5(data).hexdigest() != row.get('md5Checksum'):
                    raise AlbumUnavailable('No se pudo verificar la miniatura persistente.')
            else:
                data = self.render(photo)
                # Do not publish a thumbnail if moderation arrived during conversion.
                if not self.allowed(photo):
                    return None
                row = self.delivery.service.files().create(
                    body=dict(name=name, parents=[folder], mimeType='image/jpeg',
                              appProperties=dict(kind='albumThumbnail', sourceFileId=source_id,
                                                 albumPhotoId=photo['id'], thumbnailVersion=VERSION)),
                    media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype='image/jpeg', resumable=False),
                    fields='id,md5Checksum').execute()
                if row.get('md5Checksum') != hashlib.md5(data).hexdigest():
                    raise AlbumUnavailable('No se pudo verificar la miniatura guardada.')
            if not self.allowed(photo):
                # Same link as the original: existing moderation also deletes these files.
                self.delivery.service.files().delete(fileId=row['id']).execute()
                self.forget(key)
                return None
            self.remember(key, data)
            return data
