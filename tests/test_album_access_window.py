"""Policy: six calendar months of family access, without deleting Drive data."""
import hashlib
import hmac
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from album_app import create_app
from gallery import album_access_until


class AlbumAccessWindowTests(unittest.TestCase):
    def setUp(self):
        self.secret = 'six-month-policy-secret-at-least-32-characters'
        self.config = patch.dict('os.environ', {
            'GALLERY_LINK_SECRET': self.secret,
            'ABROLISTA_EVENT_SOURCE_TOKEN': 'private-key',
            'GDRIVE_FOLDER_ID': 'root',
            'GDRIVE_DATA_FOLDER_ID': 'data',
        })
        self.config.start()
        self.addCleanup(self.config.stop)
        self.service = MagicMock()
        self.app = create_app(self.service)
        self.client = self.app.test_client()
        self.gallery = self.app.extensions['album_gallery']
        self.manifest = dict(
            source_id='4', identity='abrolista:4', slug='vicky-herrera',
            name='15 Vicky Herrera', display_name='Vicky Herrera',
            date='2026-10-17', event_folder='event-4', photos_folder='photos-4',
        )
        self.gallery.manifests = lambda: [self.manifest]
        self.gallery.photos = lambda manifest: []
        self.gallery.videos = lambda manifest, fresh=False: []
        self.gallery.greetings = lambda manifest: []
        self.token = hmac.new(
            self.secret.encode(), b'abrolista:4', hashlib.sha256
        ).hexdigest()
        self.path = '/vicky-herrera?acceso=' + self.token

    def test_calendar_months_and_leap_year_clamping(self):
        self.assertEqual(album_access_until('2026-10-17'), date(2027, 4, 17))
        self.assertEqual(album_access_until('2026-08-31'), date(2027, 2, 28))
        self.assertEqual(album_access_until('2027-08-31'), date(2028, 2, 29))
        self.assertEqual(album_access_until('2026-12-31'), date(2027, 6, 30))

    def test_family_can_open_album_through_last_day(self):
        with patch('gallery.album_today', return_value=date(2027, 4, 17)):
            page = self.client.get(self.path)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Disponible hasta el 17 de abril de 2027', page.data)
        self.assertEqual(page.headers['Cache-Control'], 'private, no-store')

    def test_family_access_ends_after_six_months_for_all_routes(self):
        targets = [
            '/vicky-herrera',
            '/album/vicky-herrera',
            '/album/vicky-herrera/estado',
            '/album/vicky-herrera/descargar',
            '/album/vicky-herrera/saludos',
            '/album/vicky-herrera/foto/photo-1',
            '/album/vicky-herrera/miniatura/photo-1',
            '/album/vicky-herrera/video/video-1',
        ]
        with patch('gallery.album_today', return_value=date(2027, 4, 18)):
            for path in targets:
                with self.subTest(path=path):
                    response = self.client.get(path + '?acceso=' + self.token)
                    self.assertEqual(response.status_code, 410)
                    self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
                    self.assertIn('seis meses', response.get_data(as_text=True))
        self.service.files().create.assert_not_called()
        self.service.files().update.assert_not_called()
        self.service.files().delete.assert_not_called()

    def test_invalid_token_stays_private_even_after_expiry(self):
        with patch('gallery.album_today', return_value=date(2027, 4, 18)):
            self.assertEqual(self.client.get('/vicky-herrera').status_code, 404)
            self.assertEqual(self.client.get('/vicky-herrera?acceso=wrong').status_code, 404)

    def test_legacy_album_without_date_does_not_crash(self):
        del self.manifest['date']
        with patch('gallery.album_today', return_value=date(2029, 1, 1)):
            page = self.client.get(self.path)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'Disponible hasta', page.data)

    def test_invalid_saved_date_denies_access_without_guesswork(self):
        self.manifest['date'] = 'fecha corrupta'
        self.assertEqual(self.client.get(self.path).status_code, 503)
        self.service.files().delete.assert_not_called()


if __name__ == '__main__':
    unittest.main()
