"""Pruebas de fotos manuales, reels, historias y acceso privado de los álbumes."""
import hashlib
import hmac
import io
import zipfile
import unittest
from unittest.mock import MagicMock, patch

from album_app import create_app


class ManualAlbumMediaTests(unittest.TestCase):
    def setUp(self):
        self.secret = 'test-gallery-key-at-least-32-characters'
        self.environment = patch.dict('os.environ', {
            'GALLERY_LINK_SECRET': self.secret,
            'ABROLISTA_EVENT_SOURCE_TOKEN': 'private-test-key',
            'GDRIVE_FOLDER_ID': 'root', 'GDRIVE_DATA_FOLDER_ID': 'data'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

        self.service = MagicMock()
        self.service.files().list().execute.return_value = {'files': []}
        self.app = create_app(self.service)
        self.client = self.app.test_client()
        self.gallery = self.app.extensions['album_gallery']
        self.manifest = dict(source_id='4', identity='abrolista:4',
                             slug='vicky-herrera', name='15 Vicky Herrera',
                             date='2026-10-17', event_folder='event-4',
                             photos_folder='photos-4')
        self.gallery.manifests = lambda: [self.manifest]
        self.token = hmac.new(self.secret.encode(), b'abrolista:4',
                              hashlib.sha256).hexdigest()
        self.query = '?acceso=' + self.token
        self.media = {
            'photos-4': [
                dict(id='manual-photo', name='foto-manual.jpg', mimeType='image/jpeg'),
                dict(id='delivery-photo', name='foto-automatica.png',
                     mimeType='image/png', appProperties={'deliveryEvent': 'abrolista:4'}),
                dict(id='other-photo', name='ajena.jpg', mimeType='image/jpeg',
                     appProperties={'deliveryEvent': 'abrolista:999'}),
                dict(id='not-photo', name='archivo.html', mimeType='text/html')],
            'videos-4': [dict(id='reel-1', name='reel.mp4',
                              mimeType='video/mp4', size='10'),
                         dict(id='story-1', name='historia.mov',
                              mimeType='video/quicktime', size='10'),
                         dict(id='resumen-1', name='Resumen fiesta.mp4',
                              mimeType='video/mp4', size='10')]}
        self.gallery.delivery.list_files = lambda folder, extra='': (
            [] if extra else self.media.get(folder, []))
        self.gallery.delivery.find_folder = lambda parent, name, create=False: (
            {'id': {'Videos': 'videos-4'}[name]}
            if parent == 'event-4' else None)
        self.gallery.greetings = lambda manifest: []

    def test_manual_photos_and_videos_show_on_the_existing_private_album(self):
        response = self.client.get('/vicky-herrera' + self.query)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Videos de la fiesta', html)
        self.assertIn('Resumen fiesta', html)
        self.assertIn('/video/reel-1?', html)
        self.assertIn('/video/story-1?', html)
        self.assertIn('/video/resumen-1?', html)
        self.assertIn('/foto/manual-photo?', html)
        self.assertIn('/foto/delivery-photo?', html)
        self.assertNotIn('other-photo', html)
        self.assertNotIn('archivo.html', html)
        self.assertIn('Descargar original', html)

    def test_manual_duplicate_checksum_is_not_repeated(self):
        self.media['videos-4'].append(dict(id='dupe', name='same.mp4',
                                           mimeType='video/mp4', size='10',
                                           md5Checksum='identical'))
        self.media['videos-4'][0]['md5Checksum']='identical'
        self.assertEqual(len(self.gallery.videos(self.manifest)),3)

    def test_fast_page_reuses_video_metadata_but_playback_rechecks_membership(self):
        original=self.gallery.delivery.list_files
        with patch.object(self.gallery.delivery,'list_files',wraps=original) as listing:
            self.gallery.videos(self.manifest)
            calls=listing.call_count
            self.gallery.videos(self.manifest)
            self.assertEqual(listing.call_count,calls)
            self.gallery.videos(self.manifest,fresh=True)
            self.assertGreater(listing.call_count,calls)
        self.assertEqual(self.client.get('/vicky-herrera'+self.query).status_code,200)
        self.assertEqual(self.client.get('/vicky-herrera?acceso=invalid').status_code,404)
        html=self.client.get('/vicky-herrera'+self.query).data
        self.assertIn(b'href="#videos"',html)
        self.assertIn(b'href="#fotos"',html)
        self.assertIn(b'preload="none"',html)
        self.assertNotIn(b'preload="metadata"',html)

    def test_video_access_cannot_bypass_the_album_token(self):
        self.assertEqual(self.client.get('/album/vicky-herrera/video/reel-1').status_code, 404)
        self.assertEqual(self.client.get('/album/vicky-herrera/video/other-photo' + self.query).status_code, 404)

    def test_partial_video_streaming_can_seek_without_full_download(self):
        source = b'0123456789'
        requests = []
        class MediaRequest:
            def __init__(self):
                self.headers = {}
            def execute(self, num_retries=0):
                r = self.headers['Range']
                requests.append(r)
                start, end = map(int, r.removeprefix('bytes=').split('-'))
                return source[start:end + 1]
        self.service.files().get_media.side_effect = lambda fileId: MediaRequest()
        response = self.client.get('/album/vicky-herrera/video/reel-1' + self.query,
                                   headers={'Range': 'bytes=2-5'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b'2345')
        self.assertEqual(response.headers['Content-Range'], 'bytes 2-5/10')
        self.assertEqual(requests, ['bytes=2-5'])
        invalid = self.client.get('/album/vicky-herrera/video/reel-1' + self.query,
                                  headers={'Range': 'bytes=10-'})
        self.assertEqual(invalid.status_code, 416)

    def test_zip_contains_original_photos_reels_and_stories(self):
        self.gallery.download_chunks = lambda file_id: iter([('DATA-' + file_id).encode()])
        chunks = b''.join(self.gallery.zip_chunks(
            self.manifest, self.gallery.photos(self.manifest),
            [('Videos', self.gallery.videos(self.manifest))]))
        with zipfile.ZipFile(io.BytesIO(chunks)) as album:
            names = album.namelist()
            self.assertTrue(any(name.endswith('/Videos/Videos-0001.mp4') for name in names))
            self.assertTrue(any(name.startswith('vicky-herrera/Videos/') and name.endswith('.mov') for name in names))
            self.assertEqual(len([name for name in names if '/Foto-' in name]), 2)
            self.assertEqual(len([name for name in names if '/Videos/' in name]), 3)


if __name__ == '__main__':
    unittest.main()
