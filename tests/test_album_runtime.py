import hashlib
import hmac
import io
import json
import unittest
from unittest.mock import MagicMock, patch
from album_app import create_app, ThreadLocalDrive
from concurrent.futures import ThreadPoolExecutor
import threading


class ThreadLocalDriveTests(unittest.TestCase):
    def test_request_threads_have_separate_reusable_http_transports(self):
        barrier=threading.Barrier(2)
        with patch.dict('os.environ',{'GOOGLE_CLIENT_ID':'test-id','GOOGLE_CLIENT_SECRET':'test-secret','GOOGLE_REFRESH_TOKEN':'test-refresh'}):
            connection=ThreadLocalDrive()
        services=[MagicMock(),MagicMock()]
        def use(_):
            first=connection.files();barrier.wait(timeout=5)
            self.assertIs(connection.files(),first)
            return first
        with patch('album_app.build',side_effect=services) as build:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results=list(executor.map(use,range(2)))
        self.assertEqual(build.call_count,2)
        self.assertIsNot(results[0],results[1])


class PermanentAlbumTests(unittest.TestCase):
    def setUp(self):
        self.secret = 'test-gallery-key-at-least-32-characters'
        self.environment = patch.dict('os.environ', {
            'GALLERY_LINK_SECRET': self.secret,
            'ABROLISTA_EVENT_SOURCE_TOKEN': 'test-private-source-key',
            'GDRIVE_FOLDER_ID': 'root', 'GDRIVE_DATA_FOLDER_ID': 'data'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.service = MagicMock()
        self.service.files().list().execute.return_value = {'files': []}
        self.app = create_app(self.service)
        self.client = self.app.test_client()
        self.event = dict(source_id='4', identity='abrolista:4', slug='vicky-herrera',
                          name='15 Vicky herrera', display_name='Vicky herrera',
                          date='2026-10-17', event_folder=None, photos_folder=None)
        self.token = hmac.new(self.secret.encode(), b'abrolista:4', hashlib.sha256).hexdigest()

    def test_empty_album_uses_local_abrolista_and_keeps_existing_link(self):
        payload = io.BytesIO(json.dumps({'ok': True, 'event': self.event}).encode())
        with patch('gallery.urllib.request.urlopen', return_value=payload) as fetch:
            response = self.client.get('/vicky-herrera?acceso=' + self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetch.call_args.args[0].full_url,
                         'http://127.0.0.1:8000/api/eventphotos/album')
        self.assertIn(b'Los recuerdos est', response.data)
        self.assertNotIn(b'render.com', response.data)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')

    def test_access_and_screen_routes_stay_protected(self):
        self.assertEqual(self.client.get('/vicky-herrera').status_code, 404)
        self.assertEqual(self.client.post('/api/upload', data={'mensaje': 'test'}).status_code, 404)
        self.service.files().create.assert_not_called()
        self.service.files().update.assert_not_called()
        self.service.files().delete.assert_not_called()

    def test_health_does_not_require_drive_or_render(self):
        self.service.files.side_effect = RuntimeError('Drive unavailable')
        self.assertEqual(self.client.get('/healthz').json, {'ok': True})


if __name__ == '__main__':
    unittest.main()
