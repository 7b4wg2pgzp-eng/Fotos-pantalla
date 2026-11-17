"""Los videos de invitados entran al álbum; los borrados, no."""
import io
import zipfile
import unittest
from unittest.mock import patch
from flask import Flask

from delivery import Delivery, validate_config
from gallery import Gallery, register_gallery
from test_moderation import FakeDrive, UID, OTHER

VIDEO = b'0123456789'
GUEST = 'e' * 32


class GuestAlbumVideoTests(unittest.TestCase):
    def setUp(self):
        self.drive = FakeDrive()
        self.delivery = Delivery(self.drive, 'root', lambda: 'data', 'state.json')
        self.config = validate_config(dict(
            name='Fiesta ficticia',
            start='2026-10-17T12:00:00-03:00',
            end='2026-10-18T12:00:00-03:00',
            enabled=True,
        ))
        self.delivery.config = self.config
        self.gallery = Gallery(self.delivery, secret='k' * 32)
        self.delivery.gallery = self.gallery
        self.drive.add('screen', 'Videos pantalla', 'root', 'application/vnd.google-apps.folder')
        self.guest = self.drive.add('guest-file', GUEST + '.mp4', 'screen', 'video/mp4', data=VIDEO)
        self.guest['createdTime'] = '2026-10-17T18:00:00-03:00'
        self.removed = self.drive.add('removed-file', UID + '.mov', 'screen', 'video/quicktime', data=b'removed!!')
        self.removed['createdTime'] = '2026-10-17T18:05:00-03:00'

    def manifest(self):
        event, photos = self.delivery.folders(self.config)
        return dict(
            identity=self.config['identity'], slug='fiesta', name=self.config['name'],
            date='2026-10-17', event_folder=event['id'], photos_folder=photos['id'],
        )

    def test_guest_video_is_in_the_album_stream_and_zip(self):
        copied = self.delivery.copy_guest_video(self.guest, self.config)
        self.assertIsNotNone(copied)
        self.assertNotEqual(copied[1]['id'], 'guest-file')
        self.assertEqual(copied[1]['mimeType'], 'video/mp4')
        self.assertIn('guest-file', self.drive.rows)
        again = self.delivery.copy_guest_video(self.guest, self.config)
        self.assertEqual(again[1]['id'], copied[1]['id'])

        manifest = self.manifest()
        videos = self.gallery.videos(manifest, fresh=True)
        self.assertEqual([item['id'] for item in videos], [copied[1]['id']])

        class MediaRequest:
            def __init__(self):
                self.headers = {}

            def execute(self, num_retries=0):
                start, end = map(int, self.headers['Range'].removeprefix('bytes=').split('-'))
                return VIDEO[start:end + 1]

        self.drive.get_media = lambda fileId: MediaRequest()
        app = Flask('guest-album')
        register_gallery(app, self.gallery)
        with patch.object(self.gallery, 'authorized', return_value=manifest):
            client = app.test_client()
            played = client.get('/album/fiesta/video/' + copied[1]['id'], headers={'Range': 'bytes=2-5'})
            self.assertEqual(played.status_code, 206)
            self.assertEqual(played.data, b'2345')
            self.assertEqual(played.headers['Content-Range'], 'bytes 2-5/10')
            downloaded = client.get('/album/fiesta/video/' + copied[1]['id'] + '?descargar=1',
                                    headers={'Range': 'bytes=0-9'})
            self.assertIn('attachment', downloaded.headers['Content-Disposition'])
            self.assertEqual(downloaded.data, VIDEO)

        self.gallery.download_chunks = lambda file_id: iter([self.drive.data[file_id]])
        chunks = b''.join(self.gallery.zip_chunks(manifest, [], [('Videos', self.gallery.videos(manifest, fresh=True))]))
        with zipfile.ZipFile(io.BytesIO(chunks)) as archive:
            names = [name for name in archive.namelist() if '/Videos/' in name]
            self.assertEqual(len(names), 1)
            self.assertTrue(names[0].endswith('.mp4'))
            self.assertEqual(archive.read(names[0]), VIDEO)

    def test_deleted_guest_video_stays_out_of_the_album(self):
        self.delivery.save_veto(UID, {'removed-file'})
        self.assertIsNone(self.delivery.copy_guest_video(self.removed, self.config))
        copied = self.delivery.copy_guest_video(self.guest, self.config)
        manifest = self.manifest()
        self.delivery.save_veto(GUEST, {'guest-file'})
        self.drive.fail_delete.add(copied[1]['id'])
        with self.assertRaises(RuntimeError):
            self.delivery.moderate_photo(GUEST)
        self.assertIn(copied[1]['id'], self.drive.rows)
        self.assertEqual(self.gallery.videos(manifest, fresh=True), [])
        self.gallery.download_chunks = lambda file_id: iter([b'should-not'])
        chunks = b''.join(self.gallery.zip_chunks(
            manifest, [], [('Videos', self.gallery.videos(manifest, fresh=True))]))
        with zipfile.ZipFile(io.BytesIO(chunks)) as archive:
            self.assertEqual(archive.namelist(), [])

        self.drive.fail_delete.clear()
        self.delivery.moderate_photo(GUEST)
        self.assertNotIn(copied[1]['id'], self.drive.rows)
        self.assertNotIn('guest-file', self.drive.rows)
        self.assertIn('removed-file', self.drive.rows)

    def test_photo_cleanup_keeps_guest_videos_before_they_are_copied(self):
        original = self.drive.add('source-a', OTHER + '.jpg', 'originals', data=b'photo')
        original['createdTime'] = '2026-10-17T16:00:00-03:00'
        self.delivery.copy_photo(self.drive.rows['source-a'], self.config)
        self.assertFalse(any(
            row.get('appProperties', {}).get('kind') == 'guestScreenVideo'
            for row in self.drive.rows.values()
        ))
        self.delivery.clean_event_originals(self.config)
        self.assertNotIn('source-a', self.drive.rows)
        self.assertIn('guest-file', self.drive.rows)
        self.assertIn('removed-file', self.drive.rows)
        self.delivery.on_guest_video('guest-file')
        self.assertTrue(any(
            row.get('appProperties', {}).get('sourceFileId') == 'guest-file'
            for row in self.drive.rows.values()
        ))


if __name__ == '__main__':
    unittest.main()
