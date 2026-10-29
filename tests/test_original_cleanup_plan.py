"""Synthetic tests for the non-destructive preflight of event cleanup."""
import unittest
from unittest.mock import Mock
from delivery import Delivery, validate_config
from test_moderation import FakeDrive, UID, OTHER

class CleanupPlanTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.delivery = Delivery(self.service, 'root', lambda: 'data', 'state')
        self.config = validate_config(dict(name='Fiesta ficticia', start='2026-10-17T12:00:00-03:00', end='2026-10-18T00:00:00-03:00', enabled=True))
        self.original = dict(id='source', name='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg', mimeType='image/jpeg', createdTime='2026-10-17T16:00:00-03:00', md5Checksum='hash', size='5')
        self.copy = dict(id='copy', md5Checksum='hash', size='5', appProperties={'sourceFileId':'source', 'deliveryEvent':self.config['identity']})
        self.service.files().update().execute.return_value = {'id':'copy', 'appProperties':dict(self.copy['appProperties'], photoUid='a'*32)}
        self.service.reset_mock()
        self.delivery.find_folder = lambda root, name: {'id':'originals'} if name == 'Originales' else None
        self.delivery.folders = lambda config, create=False: ({'id':'event'}, {'id':'photos'})
        self.delivery.list_files = lambda parent: [self.original] if parent == 'originals' else [self.copy]

    def test_verified_copy_produces_plan_without_deleting(self):
        self.service.reset_mock()
        self.assertEqual(self.delivery.verified_event_originals(self.config), ['source'])
        self.service.files.assert_not_called()

    def test_missing_wrong_or_duplicate_copy_blocks_the_entire_plan(self):
        self.service.reset_mock()
        for copies in ([], [dict(self.copy, md5Checksum='bad')], [self.copy, self.copy],
                       [dict(self.copy, appProperties={'sourceFileId':'source', 'deliveryEvent':'other'})]):
            with self.subTest(copies=copies):
                self.delivery.list_files = lambda parent: [self.original] if parent == 'originals' else copies
                with self.assertRaises(RuntimeError):
                    self.delivery.verified_event_originals(self.config)
                self.service.files.assert_not_called()

    def test_photo_outside_event_is_never_planned(self):
        self.original['createdTime'] = '2026-10-18T01:00:00-03:00'
        self.assertEqual(self.delivery.verified_event_originals(self.config), [])

    def test_cleanup_keeps_copy_and_deletes_only_event_original(self):
        other = dict(self.original, id='future', createdTime='2026-10-18T01:00:00-03:00')
        self.delivery.list_files = lambda parent: [self.original, other] if parent == 'originals' else [self.copy]
        self.delivery.clean_event_originals(self.config)
        self.service.files().update.assert_called_once()
        self.service.files().delete.assert_called_once_with(fileId='source')

    def test_failed_marker_write_never_deletes_original(self):
        self.service.files().update().execute.side_effect = RuntimeError('simulated failure')
        with self.assertRaises(RuntimeError):
            self.delivery.clean_event_originals(self.config)
        self.service.files().delete.assert_not_called()

    def test_incomplete_delivery_stops_before_any_deletion(self):
        self.service.reset_mock()
        self.copy['size'] = '999'
        with self.assertRaises(RuntimeError):
            self.delivery.clean_event_originals(self.config)
        self.service.files.assert_not_called()

if __name__ == '__main__':
    unittest.main()

class CleanupDriveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.drive = FakeDrive()
        self.delivery = Delivery(self.drive, 'root', lambda:'data', 'state')
        self.config = validate_config(dict(name='Fiesta ficticia', start='2026-10-17T12:00:00-03:00', end='2026-10-18T00:00:00-03:00', enabled=True))
        self.delivery.folders = lambda config, create=False: ({'id':'event'}, {'id':'album'})
        for fid, uid in [('source-a',UID), ('source-b',OTHER)]:
            row = self.drive.add(fid, uid+'.jpg', 'originals', data=fid.encode())
            row['createdTime'] = '2026-10-17T16:00:00-03:00'
            self.drive.add('copy-'+fid, 'album-'+fid+'.jpg', 'album', props={'sourceFileId':fid,'deliveryEvent':self.config['identity']}, data=fid.encode())
        future = self.drive.add('future', 'c'*32+'.jpg', 'originals')
        future['createdTime'] = '2026-10-18T03:00:00-03:00'

    def test_moderation_after_source_removed_finds_album_copy(self):
        self.delivery.clean_event_originals(self.config)
        self.assertNotIn('source-a', self.drive.rows)
        self.assertIn('future', self.drive.rows)
        self.assertIn('copy-source-a', self.drive.rows)
        self.delivery.moderate_photo(UID)
        self.assertNotIn('copy-source-a', self.drive.rows)
        self.assertIn('copy-source-b', self.drive.rows)

    def test_partial_failure_is_retried_and_preserves_both_copies(self):
        self.drive.fail_delete.add('source-b')
        with self.assertRaises(RuntimeError):
            self.delivery.clean_event_originals(self.config)
        self.drive.fail_delete.clear()
        self.delivery.clean_event_originals(self.config)
        self.assertNotIn('source-a', self.drive.rows)
        self.assertNotIn('source-b', self.drive.rows)
        self.assertIn('copy-source-a', self.drive.rows)
        self.assertIn('copy-source-b', self.drive.rows)
        self.assertIn('future', self.drive.rows)
