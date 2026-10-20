import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from PIL import Image
from album_media import AlbumMedia, AlbumUnavailable, LimitedWriter, UnsupportedPreview, MAX_SOURCE_BYTES
from delivery import Delivery
from gallery import Gallery, register_gallery
from test_moderation import FakeDrive, Request, UID


class CountingDrive(FakeDrive):
    def __init__(self):
        super().__init__()
        self.calls = Counter()

    def list(self, *args, **kwargs):
        self.calls['list'] += 1
        if "'album' in parents" in kwargs.get('q', '') and 'deliveryEvent' in kwargs.get('q', ''):
            self.calls['album_index'] += 1
        return super().list(*args, **kwargs)

    def get_media(self, fileId):
        self.calls['media:' + fileId] += 1
        return super().get_media(fileId)

    def create(self, *args, **kwargs):
        self.calls['create'] += 1
        return super().create(*args, **kwargs)


class InMemoryDownload:
    """Drive streaming emulator; real Pillow decoding and Google upload objects are used."""
    def __init__(self, target, request, chunksize):
        self.target, self.request, self.chunksize = target, request, chunksize

    def next_chunk(self, num_retries=0):
        data = self.request.execute()
        for offset in range(0, len(data), self.chunksize):
            self.target.write(data[offset:offset + self.chunksize])
        return None, True


class AlbumThumbnailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Synthetic detailed pixels; no personal images are loaded.
        image = Image.effect_noise((1800, 1200), 70).convert('RGB')
        out = io.BytesIO()
        image.save(out, 'JPEG', quality=95)
        cls.original = out.getvalue()
        image.close()

    def setUp(self):
        self.offline = patch('socket.socket.connect', side_effect=AssertionError('No real network in album tests'))
        self.offline.start()
        self.addCleanup(self.offline.stop)
        streamer = patch('googleapiclient.http.MediaIoBaseDownload', InMemoryDownload)
        streamer.start()
        self.addCleanup(streamer.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        temporary = patch('album_media.tempfile.gettempdir', return_value=self.temp.name)
        temporary.start()
        self.addCleanup(temporary.stop)
        self.drive = CountingDrive()
        self.delivery = Delivery(self.drive, 'fake-root', lambda: 'data', 'unused.json')
        self.photo = self.drive.add('copy-original', 'Fiesta 01.jpg', 'album',
                                    props=dict(sourceFileId='source-original', deliveryEvent='fake-event'),
                                    data=self.original)
        self.drive.add('source-original', UID + '.jpg', 'originals', data=self.original)
        self.gallery = Gallery(self.delivery, secret='fake-gallery-secret-with-32-characters')
        self.manifest = dict(identity='fake-event', slug='fake', name='Fiesta ficticia',
                             photos_folder='album', event_folder='event', date='2026-10-20', display_name='Fiesta')
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'templates'))
        register_gallery(self.app, self.gallery)
        authorization = patch.object(self.gallery, 'authorized', return_value=self.manifest)
        authorization.start()
        self.addCleanup(authorization.stop)
        self.client = self.app.test_client()
        self.url = '/album/fake/miniatura/copy-original'

    def thumbs(self):
        return [r for r in self.drive.rows.values() if r['appProperties'].get('kind') == 'albumThumbnail']

    def test_generation_is_small_persistent_linked_and_original_unchanged(self):
        before = dict(self.drive.data)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.data), len(self.original) / 8)
        with Image.open(io.BytesIO(response.data)) as thumb:
            self.assertEqual(thumb.format, 'JPEG')
            self.assertEqual(max(thumb.size), 512)
        self.assertEqual(len(self.thumbs()), 1)
        self.assertEqual(self.thumbs()[0]['appProperties']['sourceFileId'], 'source-original')
        self.assertEqual(self.thumbs()[0]['appProperties']['albumPhotoId'], 'copy-original')
        for fid, content in before.items():
            self.assertEqual(content, self.drive.data[fid])
        self.assertEqual(self.drive.calls['media:copy-original'], 1)

    def test_reuses_thumbnail_after_worker_restart_without_reading_original(self):
        first = self.gallery.media.thumbnail(self.manifest, self.photo)
        cold = dict(self.drive.calls)
        fresh = AlbumMedia(self.delivery)
        with patch.object(fresh, 'render', side_effect=AssertionError('Must not process original')):
            second = fresh.thumbnail(self.manifest, self.photo)
        self.assertEqual(first, second)
        self.assertEqual(self.drive.calls['media:copy-original'], cold['media:copy-original'])
        self.assertEqual(self.drive.calls['create'], cold['create'])

    def test_hot_cache_and_http_304_skip_binary_downloads_and_processing(self):
        response = self.client.get(self.url)
        binary = sum(value for key, value in self.drive.calls.items() if key.startswith('media:'))
        lists = self.drive.calls['list']
        with patch.object(self.gallery.media, 'render', side_effect=AssertionError('No repeat processing')):
            for _ in range(5):
                warm = self.client.get(self.url, headers={'If-None-Match': response.headers['ETag']})
                self.assertEqual(warm.status_code, 304)
                self.assertEqual(warm.data, b'')
        self.assertEqual(sum(value for key, value in self.drive.calls.items() if key.startswith('media:')), binary)
        self.assertEqual(self.drive.calls['list'] - lists, 5) # One live veto-metadata request per 304.
        self.assertEqual(response.headers['Cache-Control'], 'private, no-cache, must-revalidate')

    def test_individual_download_is_byte_exact_and_creates_no_thumbnail(self):
        response = self.client.get('/album/fake/foto/copy-original?descargar=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, self.original)
        self.assertIn('attachment;', response.headers['Content-Disposition'])
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.assertFalse(self.thumbs())

    def test_zip_contains_only_byte_exact_originals_and_no_thumbnails(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        response = self.client.get('/album/fake/descargar')
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(), ['fake/Foto-0001.jpg'])
            self.assertEqual(archive.read(archive.namelist()[0]), self.original)

    def test_existing_moderation_deletes_persistent_thumbnail_and_invalidates_caches(self):
        response = self.client.get(self.url)
        thumb_id = self.thumbs()[0]['id']
        # Use root matching the original fake folders for the unchanged moderation method.
        self.delivery.root = 'root'
        self.delivery.moderate_photo(UID)
        self.assertIn(thumb_id, self.drive.deleted)
        self.assertFalse(self.thumbs())
        for path in [self.url, '/album/fake/foto/copy-original?descargar=1']:
            blocked = self.client.get(path, headers={'If-None-Match': response.headers['ETag']})
            self.assertEqual(blocked.status_code, 404)
        self.assertFalse(self.gallery.media.memory)
        self.assertEqual(self.client.get('/album/fake/estado').json, {'photos': []})
        fresh = Gallery(self.delivery)
        self.assertEqual(fresh.photos(self.manifest), [])

    def test_failed_thumbnail_deletion_still_blocks_cached_bytes_and_original_download(self):
        response = self.client.get(self.url)
        self.drive.fail_delete.add(self.thumbs()[0]['id'])
        self.delivery.root = 'root'
        with self.assertRaises(RuntimeError):
            self.delivery.moderate_photo(UID)
        self.assertEqual(self.client.get(self.url, headers={'If-None-Match': response.headers['ETag']}).status_code, 404)
        self.assertEqual(self.client.get('/album/fake/foto/copy-original?descargar=1').status_code, 404)

    def test_moderation_during_generation_never_persists_a_thumbnail(self):
        render = self.gallery.media.render
        def veto_during_render(photo):
            data = render(photo)
            self.delivery.save_veto(UID, {'source-original'})
            return data
        with patch.object(self.gallery.media, 'render', side_effect=veto_during_render):
            self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertFalse(self.thumbs())

    def test_concurrent_workers_generate_once(self):
        other = AlbumMedia(self.delivery)
        with patch.object(self.gallery.media, 'render', wraps=self.gallery.media.render) as first, \
                patch.object(other, 'render', wraps=other.render) as second:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda m: m.thumbnail(self.manifest, self.photo), [self.gallery.media, other]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(first.call_count + second.call_count, 1)
        self.assertEqual(self.drive.calls['media:copy-original'], 1)
        self.assertEqual(len(self.thumbs()), 1)

    def test_changed_original_checksum_uses_new_thumbnail_revision(self):
        first = self.gallery.media.thumbnail(self.manifest, self.photo)
        image = Image.new('RGB', (800, 900), 'green')
        output = io.BytesIO(); image.save(output, 'JPEG'); image.close()
        self.photo = self.drive.add('copy-original', 'Fiesta 01.jpg', 'album',
                                    props=dict(sourceFileId='source-original', deliveryEvent='fake-event'), data=output.getvalue())
        second = self.gallery.media.thumbnail(self.manifest, self.photo)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.thumbs()), 2)

    def test_veto_body_cache_refreshes_when_checksum_changes(self):
        self.delivery.save_veto(UID, set())
        self.assertEqual(len(self.gallery.photos(self.manifest)), 1)
        self.delivery.save_veto(UID, {'source-original'})
        self.assertEqual(self.gallery.photos(self.manifest), [])

    def test_live_moderation_failure_never_returns_304_or_cached_thumbnail(self):
        response = self.client.get(self.url)
        self.drive.fail_read = True
        blocked = self.client.get(self.url, headers={'If-None-Match': response.headers['ETag']})
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(blocked.headers['Cache-Control'], 'private, no-store')

    def test_write_failure_does_not_claim_thumbnail_success(self):
        self.drive.fail_write = True
        response = self.client.get(self.url)
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(self.gallery.media.memory)
        self.assertEqual(self.drive.data['source-original'], self.original)

    def test_bad_or_oversized_images_keep_original_download_working(self):
        self.photo['size'] = str(MAX_SOURCE_BYTES + 1)
        self.assertEqual(self.client.get(self.url).status_code, 415)
        self.assertEqual(self.drive.calls['media:copy-original'], 0)
        self.assertEqual(self.client.get('/album/fake/foto/copy-original?descargar=1').data, self.original)
        with self.assertRaises(UnsupportedPreview):
            LimitedWriter(io.BytesIO(), 2).write(b'too large')

    def test_corrupt_thumbnail_is_not_reused(self):
        self.client.get(self.url)
        self.drive.data[self.thumbs()[0]['id']] = b'corrupt'
        self.gallery.media.memory.clear(); self.gallery.media.memory_size = 0
        self.assertEqual(self.client.get(self.url).status_code, 503)

    def test_album_with_1000_photos_reuses_index_and_downloads_no_original_for_html(self):
        for index in range(999):
            self.drive.add('copy-' + str(index), f'Fiesta {index+2:04d}.jpg', 'album',
                           props=dict(sourceFileId='fake-' + str(index), deliveryEvent='fake-event'))
        started = time.perf_counter()
        response = self.client.get('/fake')
        duration = time.perf_counter() - started
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.count(b'data-src='), 1000)
        self.assertEqual(sum(value for key, value in self.drive.calls.items() if key.startswith('media:')), 0)
        count = self.drive.calls['album_index']
        self.assertGreater(count, 1) # Fake deliberately forces two-row pages.
        for _ in range(5):
            self.assertEqual(len(self.gallery.photos(self.manifest)), 1000)
        self.assertEqual(self.drive.calls['album_index'], count)
        self.assertLess(duration, 5) # Regression guard only; not a claim about real Drive latency.

    def test_page_has_deferred_images_priorities_and_original_links(self):
        response = self.client.get('/fake')
        self.assertIn(b'data-src="/album/fake/miniatura/', response.data)
        self.assertIn(b'fetchpriority="high"', response.data)
        self.assertIn(b'decoding="async"', response.data)
        self.assertIn(b'album_loader.js', response.data)
        for asset in (b'gallery.js', b'gallery.css', b'album_loader.js'):
            self.assertIn(asset + b'?v=album-512-v1', response.data)
        self.assertIn(b'descargar=1', response.data)

    def test_authorization_still_runs_before_cache_or_binary_read(self):
        with patch.object(self.gallery, 'authorized', side_effect=lambda slug: __import__('flask').abort(404)):
            self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.drive.calls['media:copy-original'], 0)

    def test_exif_orientation_is_applied_and_original_metadata_is_not_copied(self):
        original = Image.new('RGB', (800, 600), 'red')
        exif = Image.Exif(); exif[274] = 6; exif[315] = 'Fictional author'
        output = io.BytesIO(); original.save(output, 'JPEG', exif=exif); original.close()
        self.photo = self.drive.add('copy-original', 'Fiesta 01.jpg', 'album',
                                    props=dict(sourceFileId='source-original', deliveryEvent='fake-event'), data=output.getvalue())
        response = self.client.get(self.url)
        with Image.open(io.BytesIO(response.data)) as thumbnail:
            self.assertEqual(thumbnail.size, (384, 512))
            self.assertFalse(thumbnail.getexif())
        self.assertEqual(self.drive.data['copy-original'], output.getvalue())

    def test_unsupported_format_preserves_individual_and_zip_download(self):
        raw = b'fictional-unsupported-camera-raw'
        self.photo = self.drive.add('copy-original', 'Fiesta 01.dng', 'album',
                                    props=dict(sourceFileId='source-original', deliveryEvent='fake-event'), data=raw)
        self.assertEqual(self.client.get(self.url).status_code, 415)
        self.assertEqual(self.client.get('/album/fake/foto/copy-original?descargar=1').data, raw)
        with zipfile.ZipFile(io.BytesIO(self.client.get('/album/fake/descargar').data)) as archive:
            self.assertEqual(archive.read('fake/Foto-0001.dng'), raw)

    def test_optional_preparation_is_sequential_and_reuses_persistent_thumbnails(self):
        from prepare_album_thumbnails import prepare
        with patch.object(self.gallery, 'manifests', return_value=[self.manifest]):
            first = prepare(self.gallery, 'fake')
            second = prepare(self.gallery, 'fake')
        self.assertEqual(first, dict(ready=1, vetoed=0, unsupported=0, failed=0, ok=True))
        self.assertEqual(first, second)
        self.assertEqual(self.drive.calls['media:copy-original'], 1)

    def test_preparation_reports_failures_and_excludes_vetoed_photos(self):
        from prepare_album_thumbnails import prepare
        with patch.object(self.gallery, 'manifests', return_value=[self.manifest]):
            self.drive.fail_write = True
            self.assertFalse(prepare(self.gallery, 'fake')['ok'])
            self.drive.fail_write = False
            self.delivery.save_veto(UID, {'source-original'})
            self.assertEqual(prepare(self.gallery, 'fake')['ready'], 0)

    def test_thumbnails_do_not_confuse_existing_delivery_reconciliation_or_counts(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertNotEqual(self.thumbs()[0]['parents'], ['album'])
        self.delivery.config = dict(identity='fake-event', enabled=True)
        with patch.object(self.delivery, 'folders', return_value=({'id': 'event'}, {'id': 'album'})):
            result = self.delivery.copy_photo(self.drive.rows['source-original'], self.delivery.config)
            self.assertEqual(result[1]['id'], 'copy-original')
            self.assertEqual(self.delivery.status()['verified'], 1)


if __name__ == '__main__':
    unittest.main()
