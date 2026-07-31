"""Tests for near-duplicate resolution and relocation."""
import os
import shutil
import tempfile
import unittest

from inb4404 import perceptual
from inb4404.database import HashDB
from inb4404.near_dupe import ORIGINAL_DIR, NearDupeResolver, relocate


def meta(frames, width=100, height=100, duration=1.0):
    return perceptual.MediaMeta(frames=frames, width=width, height=height,
                                duration=duration)


class TestRelocate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, name):
        p = os.path.join(self.tmp, name)
        with open(p, 'wb') as fh:
            fh.write(b'x')
        return p

    def test_moves_into_original_with_suffix(self):
        src = self._touch('clip.webm')
        dest = relocate(src)
        self.assertEqual(
            dest, os.path.join(self.tmp, ORIGINAL_DIR, 'clip_1.webm'))
        self.assertTrue(os.path.isfile(dest))
        self.assertFalse(os.path.exists(src))

    def test_increments_suffix_on_collision(self):
        relocate(self._touch('clip.webm'))
        dest = relocate(self._touch('clip.webm'))
        self.assertEqual(
            dest, os.path.join(self.tmp, ORIGINAL_DIR, 'clip_2.webm'))

    def test_missing_file_returns_none(self):
        self.assertIsNone(relocate(os.path.join(self.tmp, 'gone.webm')))


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = HashDB(db_path=os.path.join(self.tmp, 'test.db'))
        self.resolver = NearDupeResolver(self.db, distance=3, verbose=False)
        self.thread = os.path.join(self.tmp, 'downloads', 'g', '1234')
        os.makedirs(self.thread)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, directory, name):
        if not os.path.isdir(directory):
            os.makedirs(directory)
        p = os.path.join(directory, name)
        with open(p, 'wb') as fh:
            fh.write(b'x')
        return p

    def test_no_candidates_returns_none(self):
        p = self._make(self.thread, 'new.webm')
        self.assertIsNone(self.resolver.check(p, meta([0x1234])))

    def test_incoming_loser_is_relocated(self):
        held = self._make(self.thread, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=1920, height=1080))
        new = self._make(self.thread, 'new.webm')
        moved = self.resolver.check(new, meta([0x1234], width=640, height=480))
        self.assertEqual(
            moved, os.path.join(self.thread, ORIGINAL_DIR, 'new_1.webm'))
        self.assertTrue(os.path.isfile(held))
        self.assertFalse(os.path.exists(new))

    def test_incoming_winner_relocates_held_file(self):
        held = self._make(self.thread, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=640, height=480))
        new = self._make(self.thread, 'new.webm')
        moved = self.resolver.check(new, meta([0x1234], width=1920, height=1080))
        self.assertIsNone(moved)
        self.assertTrue(os.path.isfile(new))
        self.assertFalse(os.path.exists(held))
        self.assertTrue(os.path.isfile(
            os.path.join(self.thread, ORIGINAL_DIR, 'held_1.webm')))

    def test_cross_thread_winner_moves_nothing_without_permission(self):
        other = os.path.join(self.tmp, 'downloads', 'g', '9999')
        held = self._make(other, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=640, height=480))
        new = self._make(self.thread, 'new.webm')
        moved = self.resolver.check(new, meta([0x1234], width=1920, height=1080),
                                    allow_foreign_moves=False)
        self.assertIsNone(moved)
        self.assertTrue(os.path.isfile(held))
        self.assertTrue(os.path.isfile(new))

    def test_cross_thread_loser_moves_itself(self):
        other = os.path.join(self.tmp, 'downloads', 'g', '9999')
        held = self._make(other, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=1920, height=1080))
        new = self._make(self.thread, 'new.webm')
        moved = self.resolver.check(new, meta([0x1234], width=640, height=480),
                                    allow_foreign_moves=False)
        self.assertEqual(
            moved, os.path.join(self.thread, ORIGINAL_DIR, 'new_1.webm'))
        self.assertTrue(os.path.isfile(held))

    def test_cross_thread_winner_moves_held_when_permitted(self):
        other = os.path.join(self.tmp, 'downloads', 'g', '9999')
        held = self._make(other, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=640, height=480))
        new = self._make(self.thread, 'new.webm')
        self.resolver.check(new, meta([0x1234], width=1920, height=1080),
                            allow_foreign_moves=True)
        self.assertFalse(os.path.exists(held))
        self.assertTrue(os.path.isfile(
            os.path.join(other, ORIGINAL_DIR, 'held_1.webm')))

    def test_stale_candidate_row_is_dropped(self):
        ghost = os.path.join(self.thread, 'ghost.webm')
        self.db.record_phash(ghost, meta([0x1234]))
        new = self._make(self.thread, 'new.webm')
        self.assertIsNone(self.resolver.check(new, meta([0x1234])))
        self.assertIsNone(self.db.get_phash(ghost))


if __name__ == '__main__':
    unittest.main()
