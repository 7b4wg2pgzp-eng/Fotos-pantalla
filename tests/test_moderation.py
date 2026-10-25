"""All Drive operations are in-memory; imports disable credentials and startup threads."""
import hashlib
import importlib
import io
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from delivery import Delivery, FOLDER, photo_uid, validate_config
from gallery import Gallery, register_gallery
from flask import Flask

UID = 'a' * 32
OTHER = 'b' * 32


class Request:
    def __init__(self, action):
        self.action = action

    def execute(self):
        return self.action()


class Missing(Exception):
    resp = SimpleNamespace(status=404)


class FakeDrive:
    def __init__(self):
        self.rows = {}
        self.data = {}
        self.deleted = []
        self.fail_delete = set()
        self.fail_write = False
        self.fail_read = False
        self.corrupt_readback = False
        self.add('data', 'Datos', 'root', FOLDER)
        self.add('originals', 'Originales', 'root', FOLDER)
        self.add('previews', 'Previews', 'root', FOLDER)

    def add(self, fid, name, parent, mime='image/jpeg', props=None, data=b'fake-photo'):
        self.rows[fid] = dict(id=fid, name=name, parents=[parent], mimeType=mime,
                              appProperties=props or {}, createdTime=datetime.now(timezone.utc).isoformat(),
                              size=str(len(data)), md5Checksum=hashlib.md5(data).hexdigest())
        self.data[fid] = data
        return self.rows[fid]

    def files(self):
        return self

    def list(self, q, fields=None, pageSize=100, pageToken=None):
        def execute():
            if self.fail_read:
                raise RuntimeError('simulated list failure')
            rows = list(self.rows.values())
            parents = re.findall(r"'([^']+)' in parents", q)
            equal = re.findall(r"(?<![A-Za-z])name = '([^']*)'", q)
            contains = re.findall(r"name contains '([^']*)'", q)
            markers = re.findall(r"key='([^']+)' and value='([^']+)'", q)
            for parent in parents:
                rows = [r for r in rows if parent in r['parents']]
            for name in equal:
                rows = [r for r in rows if r['name'] == name]
            for term in contains:
                rows = [r for r in rows if term in r['name']]
            if "mimeType = '" + FOLDER + "'" in q:
                rows = [r for r in rows if r['mimeType'] == FOLDER]
            for key, value in markers:
                rows = [r for r in rows if r['appProperties'].get(key) == value]
            # Force pagination even for small fixtures.
            start = int(pageToken or 0)
            end = start + min(pageSize, 2)
            result = dict(files=[dict(r) for r in rows[start:end]])
            if end < len(rows):
                result['nextPageToken'] = str(end)
            return result
        return Request(execute)

    def get_media(self, fileId):
        def execute():
            if self.fail_read:
                raise RuntimeError('simulated read failure')
            if self.corrupt_readback and self.rows[fileId]['name'].startswith('moderation-'):
                return b'{}'
            return self.data[fileId]
        return Request(execute)

    def get(self, fileId, fields=None):
        def execute():
            if fileId not in self.rows:
                raise Missing()
            return dict(self.rows[fileId])
        return Request(execute)

    def create(self, body, media_body=None, fields=None):
        def execute():
            if self.fail_write:
                raise RuntimeError('simulated persistence failure')
            data = media_body.getbytes(0, media_body.size()) if media_body else b''
            return self.add('new-' + str(len(self.rows)), body['name'], body['parents'][0],
                            body.get('mimeType', 'application/json'), body.get('appProperties'), data)
        return Request(execute)

    def update(self, fileId, media_body=None, body=None, fields=None):
        def execute():
            if self.fail_write:
                raise RuntimeError('simulated persistence failure')
            if media_body:
                data = media_body.getbytes(0, media_body.size())
                self.data[fileId] = data
                self.rows[fileId].update(md5Checksum=hashlib.md5(data).hexdigest(), size=str(len(data)))
            if body:
                self.rows[fileId].update(body)
            return dict(self.rows[fileId])
        return Request(execute)

    def delete(self, fileId):
        def execute():
            if fileId in self.fail_delete:
                raise RuntimeError('simulated deletion failure')
            if fileId not in self.rows:
                raise Missing()
            self.deleted.append(fileId)
            del self.rows[fileId]
            del self.data[fileId]
            return {}
        return Request(execute)

    def copy(self, fileId, body, fields=None):
        return Request(lambda: self.add('copy-' + str(len(self.rows)), body['name'],
                                        body['parents'][0], props=body['appProperties'], data=self.data[fileId]))


class ModerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.import_dir = tempfile.TemporaryDirectory()
        # No real credential or background restoration can be used during import.
        with patch.dict('os.environ', {'EVENTPHOTOS_DIR': cls.import_dir.name,
                                      'EVENTPHOTOS_CACHE': cls.import_dir.name}, clear=True), \
                patch('threading.Thread.start'):
            cls.screen = importlib.import_module('app')

    @classmethod
    def tearDownClass(cls):
        cls.import_dir.cleanup()

    def setUp(self):
        network = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden in tests'))
        network.start()
        self.addCleanup(network.stop)
        self.drive = FakeDrive()
        self.delivery = Delivery(self.drive, 'root', lambda: 'data', 'state.json')
        now = datetime.now(timezone.utc)
        self.config = validate_config(dict(name='Fiesta ficticia', start=(now-timedelta(hours=1)).isoformat(),
                                           end=(now+timedelta(hours=1)).isoformat(), enabled=True))
        self.delivery.config = self.config
        self.drive.add('source', UID + '.heic', 'originals')
        self.drive.add('preview', 'preview-' + UID + '.jpg', 'previews')
        for i in range(5):
            self.drive.add('family-' + str(i), 'Foto renombrada ' + str(i) + '.jpg', 'album',
                           props=dict(sourceFileId='source', deliveryEvent=self.config['identity']))
        self.drive.add('untouched', OTHER + '.jpg', 'originals')
        self.drive.add('similar', UID + '-backup.jpg', 'elsewhere')
        self.drive.add('other-copy', 'Otra foto.jpg', 'album',
                       props=dict(sourceFileId='untouched', deliveryEvent=self.config['identity']))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / 'cache'
        self.originals = Path(self.temp.name) / 'local'
        self.cache.mkdir()
        self.originals.mkdir()
        (self.cache / (UID + '.jpg')).write_bytes(b'fake-preview')
        (self.cache / (OTHER + '.jpg')).write_bytes(b'other-preview')
        for name, value in dict(_drive_service=self.drive, _delivery=self.delivery, CACHE_DIR=self.cache,
                                PHOTOS_DIR=self.originals, ADMIN_KEY='test-key',
                                MESSAGES_FILE=self.cache / 'mensajes.json').items():
            patcher = patch.object(self.screen, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        ensure = patch.object(self.screen._event_source, 'ensure', return_value=self.config)
        ensure.start()
        self.addCleanup(ensure.stop)
        self.client = self.screen.app.test_client()

    def delete(self, name=UID + '.jpg', key='test-key'):
        return self.client.post('/api/admin/borrar-foto', data=dict(nombre=name, key=key))

    def test_delete_all_pages_exact_original_and_all_linked_copies(self):
        result = self.delete()
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json['ok'])
        self.assertEqual(set(self.drive.deleted), {'source', 'preview'} | {'family-' + str(i) for i in range(5)})
        self.assertTrue({'untouched', 'similar', 'other-copy'} <= self.drive.rows.keys())
        self.assertEqual(self.delivery.vetoes(), {UID: {'source'}})
        self.assertEqual(self.client.get('/api/photos').json['photos'], ['/photos/' + OTHER + '.jpg'])
        self.assertEqual(self.client.get('/photos/' + UID + '.jpg').status_code, 404)

    def test_partial_failure_hides_album_and_screen_then_retry_after_restart(self):
        self.drive.fail_delete.add('family-3')
        self.assertEqual(self.delete().status_code, 503)
        self.assertNotIn('source', self.drive.rows)
        self.assertIn('family-3', self.drive.rows)
        fresh = Delivery(self.drive, 'root', lambda: 'data', 'state.json')
        gallery = Gallery(fresh)
        manifest = dict(photos_folder='album', identity=self.config['identity'])
        self.assertEqual([p['id'] for p in gallery.photos(manifest)], ['other-copy'])
        self.assertEqual(self.client.get('/photos/' + UID + '.jpg').status_code, 404)
        self.drive.fail_delete.clear()
        fresh.moderate_photo(UID)
        fresh.moderate_photo(UID)
        self.assertNotIn('family-3', self.drive.rows)

    def test_failed_veto_write_never_deletes_and_never_claims_success(self):
        self.drive.fail_write = True
        result = self.delete()
        self.assertEqual(result.status_code, 503)
        self.assertFalse(result.json['ok'])
        self.assertEqual(self.drive.deleted, [])
        self.assertNotIn('/photos/' + UID + '.jpg', self.client.get('/api/photos').json['photos'])

    def test_readback_failure_never_deletes(self):
        self.drive.corrupt_readback = True
        self.assertEqual(self.delete().status_code, 503)
        self.assertEqual(self.drive.deleted, [])

    def test_remote_read_failure_is_closed_for_screen_and_gallery(self):
        self.drive.fail_read = True
        self.assertEqual(self.client.get('/api/photos').status_code, 503)
        self.assertEqual(self.client.get('/photos/' + OTHER + '.jpg').status_code, 503)
        with self.assertRaises(RuntimeError):
            Gallery(self.delivery).photos(dict(photos_folder='album', identity=self.config['identity']))

    def test_invalid_input_and_auth_never_mutate_drive(self):
        for name in ['', '../' + UID + '.jpg', UID[:8] + '.jpg', 'preview-' + UID + '.jpg', UID + '.json']:
            self.assertEqual(self.delete(name).status_code, 400)
        self.assertEqual(self.delete(key='wrong').status_code, 403)
        self.assertEqual(self.drive.deleted, [])
        self.assertFalse(self.delivery.vetoes())

    def test_restore_and_reconcile_skip_persisted_veto(self):
        self.delivery.save_veto(UID, {'source'})
        # Simulate a surviving/restored original and preview in Drive after a failed delete.
        (self.cache / (UID + '.jpg')).unlink()
        self.screen._restore_from_drive()
        self.assertFalse((self.cache / (UID + '.jpg')).exists())
        self.assertIsNone(self.delivery.copy_photo(self.drive.rows['source'], self.config))
        self.delivery.on_photo('source')
        with patch.object(self.delivery, 'copy_photo', wraps=self.delivery.copy_photo) as copy, \
                patch.object(self.delivery, 'on_messages'), \
                patch.object(self.delivery, 'folders', return_value=({'id': 'event'}, {'id': 'album'})):
            result = self.delivery.reconcile([])
        self.assertEqual(result['verified'], 1)
        self.assertEqual([call.args[0]['id'] for call in copy.call_args_list], ['untouched'])

    def test_unmoderated_delivery_is_idempotent_and_screen_still_serves(self):
        with patch.object(self.delivery, 'folders', return_value=({'id': 'event'}, {'id': 'album'})):
            first = self.delivery.copy_photo(self.drive.rows['untouched'], self.config)
            second = self.delivery.copy_photo(self.drive.rows['untouched'], self.config)
        self.assertEqual(first[1]['id'], second[1]['id'])
        response = self.client.get('/photos/' + OTHER + '.jpg')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'other-preview')
        response.close()

    def test_direct_album_download_and_zip_exclude_vetoed_copies(self):
        self.delivery.save_veto(UID, {'source'})
        gallery = Gallery(self.delivery)
        manifest = dict(photos_folder='album', identity=self.config['identity'], slug='fake', name='Fake')
        app = Flask('fake-gallery')
        register_gallery(app, gallery)
        with patch.object(gallery, 'authorized', return_value=manifest), \
                patch.object(gallery, 'download_chunks', return_value=iter([b'allowed-photo'])) as download, \
                patch.object(gallery, 'greetings', return_value=[]):
            client = app.test_client()
            self.assertEqual(client.get('/album/fake/foto/family-0?descargar=1').status_code, 404)
            response = client.get('/album/fake/descargar')
            import zipfile
            with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                self.assertEqual(archive.namelist(), ['fake/Foto-0001.jpg'])
                self.assertEqual(archive.read(archive.namelist()[0]), b'allowed-photo')
            download.assert_called_once_with('other-copy')

    def test_uid_parser_only_accepts_exact_photo_names(self):
        self.assertEqual(photo_uid('preview-' + UID + '.jpg'), UID)
        self.assertIsNone(photo_uid(UID + '-other.jpg'))

    def test_original_failure_keeps_veto_across_recreated_delivery(self):
        self.drive.fail_delete.add('source')
        self.assertFalse(self.delete().json['ok'])
        self.assertIn('source', self.drive.rows)
        fresh = Delivery(self.drive, 'root', lambda: 'data', 'state.json')
        self.assertIsNone(fresh.copy_photo(self.drive.rows['source'], self.config))
        self.drive.fail_delete.clear()
        fresh.moderate_photo(UID)
        self.assertNotIn('source', self.drive.rows)

    def test_no_drive_reports_failure_with_local_veto(self):
        offline = Delivery(None, '', lambda: None, 'state.json')
        with patch.object(self.screen, '_drive_service', None), patch.object(self.screen, '_delivery', offline):
            self.assertEqual(self.delete().status_code, 503)
            self.assertEqual(self.client.get('/photos/' + UID + '.jpg').status_code, 404)
        self.assertEqual(self.drive.deleted, [])

    def test_preview_failure_does_not_restore_after_losing_local_cache(self):
        self.drive.fail_delete.add('preview')
        self.assertEqual(self.delete().status_code, 503)
        for marker in self.cache.glob('moderation-*.json'):
            marker.unlink()
        self.screen._restore_from_drive()
        self.assertFalse((self.cache / (UID + '.jpg')).exists())
        # Even a stale local preview is blocked by the persistent Drive veto.
        (self.cache / (UID + '.jpg')).write_bytes(b'stale')
        self.assertEqual(self.client.get('/photos/' + UID + '.jpg').status_code, 404)

    def test_linked_copy_listing_failure_deletes_nothing(self):
        original_query = self.delivery.query_files
        def query(q):
            if 'sourceFileId' in q:
                raise RuntimeError('simulated copy lookup failure')
            return original_query(q)
        with patch.object(self.delivery, 'query_files', side_effect=query):
            self.assertEqual(self.delete().status_code, 503)
        self.assertEqual(self.drive.deleted, [])
        self.assertEqual(self.delivery.vetoes(), {UID: {'source'}})

    def test_missing_source_is_an_idempotent_delete(self):
        self.assertTrue(self.delete().json['ok'])
        self.assertTrue(self.delete().json['ok'])
        self.assertEqual(self.delivery.vetoes(), {UID: {'source'}})

    def test_veto_during_copy_discards_copy_and_does_not_count_success(self):
        source = self.drive.add('race-source', 'c' * 32 + '.jpg', 'originals')
        with patch.object(self.delivery, 'folders', return_value=({'id': 'event'}, {'id': 'album'})), \
                patch.object(self.delivery, 'is_vetoed', side_effect=[False, True]):
            self.assertIsNone(self.delivery.copy_photo(source, self.config))
        self.assertFalse(any(p['appProperties'].get('sourceFileId') == 'race-source'
                             for p in self.drive.rows.values()))


if __name__ == '__main__':
    unittest.main()
