"""Policy: six calendar months of family access, without deleting Drive data."""
import hashlib
import hmac
import io
import json
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from album_app import create_app
from gallery import album_access_until, album_today


class AlbumAccessWindowTests(unittest.TestCase):
    def setUp(self):
        offline = patch('socket.socket.connect', side_effect=AssertionError('No real network in policy tests'))
        offline.start()
        self.addCleanup(offline.stop)
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
            source_id='4', identity='abrolista:4', slug='fiesta-sintetica',
            name='Fiesta sintética', display_name='Fiesta sintética',
            date='2026-10-17', event_folder='event-4', photos_folder='photos-4',
        )
        self.gallery.manifests = lambda: [self.manifest]
        self.gallery.photos = lambda manifest: []
        self.gallery.videos = lambda manifest, fresh=False: []
        self.gallery.greetings = lambda manifest: []
        self.token = hmac.new(
            self.secret.encode(), b'abrolista:4', hashlib.sha256
        ).hexdigest()
        self.path = '/fiesta-sintetica?acceso=' + self.token
        self.targets = [
            '/fiesta-sintetica', '/album/fiesta-sintetica',
            '/album/fiesta-sintetica/estado', '/album/fiesta-sintetica/descargar',
            '/album/fiesta-sintetica/saludos', '/album/fiesta-sintetica/foto/photo-1',
            '/album/fiesta-sintetica/miniatura/photo-1', '/album/fiesta-sintetica/video/video-1',
        ]

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
        with patch('gallery.album_today', return_value=date(2027, 4, 18)):
            for path in self.targets:
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
            for path in self.targets:
                for query in ('', '?acceso=wrong'):
                    with self.subTest(path=path, query=query):
                        self.assertEqual(self.client.get(path + query).status_code, 404)

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

    def test_head_range_and_conditional_requests_cannot_bypass_expiry(self):
        with patch('gallery.album_today', return_value=date(2027, 4, 18)), \
             patch.object(self.gallery, 'photos') as photos, \
             patch.object(self.gallery, 'videos') as videos, \
             patch.object(self.gallery, 'greetings') as greetings:
            for path in self.targets:
                for method in ('GET', 'HEAD'):
                    for query in ('', '&descargar=1'):
                        with self.subTest(path=path, method=method, query=query):
                            r = self.client.open(path + '?acceso=' + self.token + query,
                                method=method, headers={'Range':'bytes=0-9','If-None-Match':'"cached"'})
                            self.assertEqual(r.status_code, 410)
                            self.assertEqual(r.headers['Cache-Control'], 'private, no-store')
                            self.assertEqual(r.headers['Referrer-Policy'], 'no-referrer')
            photos.assert_not_called()
            videos.assert_not_called()
            greetings.assert_not_called()
        self.service.files().get_media.assert_not_called()
        self.service.files().delete.assert_not_called()

    def test_historical_identity_link_keeps_calendar_expiry(self):
        del self.manifest['source_id']
        self.manifest['identity'] = 'historical-fixture'
        self.manifest['date'] = '2020-08-31'
        token = self.gallery.token(self.manifest)
        with patch('gallery.album_today', return_value=date(2021, 2, 28)):
            self.assertEqual(self.client.get('/fiesta-sintetica?acceso='+token).status_code, 200)
        with patch('gallery.album_today', return_value=date(2021, 3, 1)):
            self.assertEqual(self.client.get('/fiesta-sintetica?acceso='+token).status_code, 410)

    def test_empty_album_from_private_event_source_also_expires(self):
        self.gallery.manifests = lambda: []
        payload = json.dumps({'ok':True, 'event':self.manifest}).encode()
        with patch('gallery.urllib.request.urlopen', side_effect=lambda *a, **k: io.BytesIO(payload)), \
             patch('gallery.album_today', return_value=date(2027, 4, 18)):
            self.assertEqual(self.client.get(self.path).status_code, 410)
        self.service.files().create.assert_not_called()
        self.service.files().update.assert_not_called()
        self.service.files().delete.assert_not_called()

    def test_clock_uses_mendoza_day_at_utc_midnight_boundary(self):
        with patch('gallery.datetime') as clock:
            clock.now.side_effect = lambda tz: datetime(2027,4,18,2,59,tzinfo=timezone.utc).astimezone(tz)
            self.assertEqual(album_today(), date(2027,4,17))
            clock.now.side_effect = lambda tz: datetime(2027,4,18,3,0,tzinfo=timezone.utc).astimezone(tz)
            self.assertEqual(album_today(), date(2027,4,18))


if __name__ == '__main__':
    unittest.main()
