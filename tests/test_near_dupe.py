"""Tests for near-duplicate detection and deletion."""
import os
import shutil
import tempfile
import unittest

from inb4404 import perceptual
from inb4404.database import HashDB
from inb4404.near_dupe import NearDupeResolver


def meta(frames, width=100, height=100, duration=1.0):
    return perceptual.MediaMeta(frames=frames, width=width, height=height,
                                duration=duration)


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

    def test_no_candidates_returns_false(self):
        p = self._make(self.thread, 'new.webm')
        self.assertFalse(self.resolver.check(p, meta([0x1234])))

    def test_incoming_loser_is_deleted(self):
        held = self._make(self.thread, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=1920, height=1080))
        new = self._make(self.thread, 'new.webm')
        deleted = self.resolver.check(new, meta([0x1234], width=640, height=480))
        self.assertTrue(deleted)
        self.assertTrue(os.path.isfile(held))
        self.assertFalse(os.path.exists(new))

    def test_incoming_winner_deletes_held_file(self):
        held = self._make(self.thread, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=640, height=480))
        new = self._make(self.thread, 'new.webm')
        deleted = self.resolver.check(new, meta([0x1234], width=1920, height=1080))
        self.assertFalse(deleted)
        self.assertTrue(os.path.isfile(new))
        self.assertFalse(os.path.exists(held))

    def test_cross_thread_winner_deletes_nothing_without_permission(self):
        other = os.path.join(self.tmp, 'downloads', 'g', '9999')
        held = self._make(other, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=640, height=480))
        new = self._make(self.thread, 'new.webm')
        deleted = self.resolver.check(new, meta([0x1234], width=1920, height=1080),
                                      allow_foreign_moves=False)
        self.assertFalse(deleted)
        self.assertTrue(os.path.isfile(held))
        self.assertTrue(os.path.isfile(new))

    def test_cross_thread_loser_deletes_itself(self):
        other = os.path.join(self.tmp, 'downloads', 'g', '9999')
        held = self._make(other, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=1920, height=1080))
        new = self._make(self.thread, 'new.webm')
        deleted = self.resolver.check(new, meta([0x1234], width=640, height=480),
                                      allow_foreign_moves=False)
        self.assertTrue(deleted)
        self.assertTrue(os.path.isfile(held))

    def test_cross_thread_winner_deletes_held_when_permitted(self):
        other = os.path.join(self.tmp, 'downloads', 'g', '9999')
        held = self._make(other, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=640, height=480))
        new = self._make(self.thread, 'new.webm')
        self.resolver.check(new, meta([0x1234], width=1920, height=1080),
                            allow_foreign_moves=True)
        self.assertFalse(os.path.exists(held))
        self.assertTrue(os.path.isfile(new))

    def test_resolves_every_weaker_copy_in_one_pass(self):
        """A group of near-dupes must fully resolve in a single run."""
        for name in ('a.webm', 'b.webm', 'c.webm'):
            held = self._make(self.thread, name)
            self.db.record_phash(held, meta([0x1234], width=640, height=480))
        new = self._make(self.thread, 'best.webm')
        self.resolver.check(new, meta([0x1234], width=1920, height=1080))
        remaining = sorted(os.listdir(self.thread))
        self.assertEqual(remaining, ['best.webm'])

    def test_second_pass_is_idempotent(self):
        """Re-checking after the loser is gone must not raise or double-count."""
        held = self._make(self.thread, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=640, height=480))
        new = self._make(self.thread, 'new.webm')
        winner_meta = meta([0x1234], width=1920, height=1080)
        self.resolver.check(new, winner_meta)
        self.db.record_phash(new, winner_meta)

        deleted_before = self.resolver.deleted
        self.resolver.check(new, winner_meta)
        self.assertEqual(self.resolver.deleted, deleted_before)

    def test_counter_includes_every_deleted_held_copy(self):
        """A winning file deletes several held copies; all must be counted."""
        for name in ('a.webm', 'b.webm', 'c.webm'):
            held = self._make(self.thread, name)
            self.db.record_phash(held, meta([0x1234], width=640, height=480))
        new = self._make(self.thread, 'best.webm')
        deleted = self.resolver.check(new, meta([0x1234], width=1920, height=1080))
        # check() reports only the incoming file being deleted, which did not happen.
        self.assertFalse(deleted)
        self.assertEqual(self.resolver.deleted, 3)

    def test_md5_row_is_dropped_for_the_deleted_file(self):
        held = self._make(self.thread, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=640, height=480))
        self.db.insert('deadbeef', held, '1234', 111, 222)
        new = self._make(self.thread, 'new.webm')
        self.resolver.check(new, meta([0x1234], width=1920, height=1080))
        self.assertIsNone(self.db.get_path('deadbeef'))

    def test_stale_candidate_row_is_dropped(self):
        ghost = os.path.join(self.thread, 'ghost.webm')
        self.db.record_phash(ghost, meta([0x1234]))
        new = self._make(self.thread, 'new.webm')
        self.assertFalse(self.resolver.check(new, meta([0x1234])))
        self.assertIsNone(self.db.get_phash(ghost))


if __name__ == '__main__':
    unittest.main()
