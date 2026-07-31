"""Tests for the phash_frames table and its accessors."""
import os
import shutil
import tempfile
import unittest

from inb4404 import perceptual
from inb4404.database import HashDB


def meta(frames, width=100, height=100, duration=1.0):
    return perceptual.MediaMeta(frames=frames, width=width, height=height,
                                duration=duration)


class TestPhashStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = HashDB(db_path=os.path.join(self.tmp, 'test.db'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_is_idempotent(self):
        self.db.init()
        self.db.init()
        self.assertFalse(self.db.has_phash('/nope'))

    def test_record_and_get_roundtrip(self):
        m = meta([1, 2, 3], width=640, height=480, duration=12.5)
        self.db.record_phash('/a.webm', m)
        got = self.db.get_phash('/a.webm')
        self.assertEqual(got.frames, [1, 2, 3])
        self.assertEqual(got.width, 640)
        self.assertEqual(got.height, 480)
        self.assertAlmostEqual(got.duration, 12.5)

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.db.get_phash('/missing.webm'))

    def test_record_replaces_previous_rows(self):
        self.db.record_phash('/a.webm', meta([1, 2, 3]))
        self.db.record_phash('/a.webm', meta([9]))
        self.assertEqual(self.db.get_phash('/a.webm').frames, [9])

    def test_has_phash(self):
        self.assertFalse(self.db.has_phash('/a.webm'))
        self.db.record_phash('/a.webm', meta([1]))
        self.assertTrue(self.db.has_phash('/a.webm'))

    def test_frame_order_is_preserved(self):
        self.db.record_phash('/a.webm', meta([30, 10, 20]))
        self.assertEqual(self.db.get_phash('/a.webm').frames, [30, 10, 20])


class TestBandProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = HashDB(db_path=os.path.join(self.tmp, 'test.db'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_exact_band_match(self):
        target = 0x1111222233334444
        self.db.record_phash('/held.webm', meta([target]))
        found = self.db.find_phash_candidates(
            [perceptual.chunks(target)], exclude_path='/new.webm')
        self.assertEqual(found, ['/held.webm'])

    def test_finds_hash_differing_by_one_bit(self):
        target = 0x1111222233334444
        self.db.record_phash('/held.webm', meta([target]))
        # Flip one bit in the last chunk; the first three chunks still match.
        found = self.db.find_phash_candidates(
            [perceptual.chunks(target ^ 0b1)], exclude_path='/new.webm')
        self.assertEqual(found, ['/held.webm'])

    def test_excludes_self(self):
        target = 0x1111222233334444
        self.db.record_phash('/new.webm', meta([target]))
        found = self.db.find_phash_candidates(
            [perceptual.chunks(target)], exclude_path='/new.webm')
        self.assertEqual(found, [])

    def test_unrelated_hash_not_returned(self):
        self.db.record_phash('/held.webm', meta([0x1111222233334444]))
        found = self.db.find_phash_candidates(
            [perceptual.chunks(0xAAAABBBBCCCCDDDD)], exclude_path='/new.webm')
        self.assertEqual(found, [])

    def test_deduplicates_paths_across_frames(self):
        a, b = 0x1111222233334444, 0xAAAABBBBCCCCDDDD
        self.db.record_phash('/held.webm', meta([a, b]))
        found = self.db.find_phash_candidates(
            [perceptual.chunks(a), perceptual.chunks(b)],
            exclude_path='/new.webm')
        self.assertEqual(found, ['/held.webm'])


class TestPathMaintenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = HashDB(db_path=os.path.join(self.tmp, 'test.db'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_move_updates_path(self):
        self.db.record_phash('/a.webm', meta([1, 2]))
        self.db.move_phash_path('/a.webm', '/original/a_1.webm')
        self.assertIsNone(self.db.get_phash('/a.webm'))
        self.assertEqual(self.db.get_phash('/original/a_1.webm').frames, [1, 2])

    def test_delete_removes_rows(self):
        self.db.record_phash('/a.webm', meta([1]))
        self.db.delete_phash('/a.webm')
        self.assertIsNone(self.db.get_phash('/a.webm'))

    def test_bulk_session_shares_one_connection(self):
        with self.db.bulk_session() as conn:
            self.db.record_phash('/a.webm', meta([1]), conn=conn)
            self.assertTrue(self.db.has_phash('/a.webm', conn=conn))
        self.assertTrue(self.db.has_phash('/a.webm'))


if __name__ == '__main__':
    unittest.main()
