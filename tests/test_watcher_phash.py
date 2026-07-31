"""Tests for the live-download perceptual hashing hook.

`ThreadWatcher._phash_new_file` is the path every live download takes, but
constructing a real ThreadWatcher needs a network round-trip. These tests
call the method unbound against a minimal stub so the extract -> record ->
resolve sequence is exercised without a live thread.
"""
import glob
import os
import shutil
import tempfile
import unittest

from inb4404 import perceptual
from inb4404.database import HashDB
from inb4404.near_dupe import ORIGINAL_DIR, NearDupeResolver
from inb4404.thread_watcher import ThreadWatcher


def _sample_media():
    """Return a real media file from downloads/, or None when unavailable."""
    for pattern in ('downloads/**/*.webm', 'downloads/**/*.mp4',
                    'downloads/**/*.gif'):
        hits = [h for h in glob.glob(pattern, recursive=True)
                if ORIGINAL_DIR not in h.replace('\\', '/').split('/')]
        if hits:
            return sorted(hits, key=os.path.getsize)[0]
    return None


class _Stub(object):
    """Minimal stand-in carrying only what _phash_new_file touches."""

    def __init__(self, db, resolver, verbose=False):
        self.db = db
        self.resolver = resolver

        class _Cfg(object):
            pass
        self.config = _Cfg()
        self.config.verbose = verbose


class TestPhashNewFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = HashDB(db_path=os.path.join(self.tmp, 'test.db'))
        self.resolver = NearDupeResolver(self.db, distance=3, verbose=False)
        self.stub = _Stub(self.db, self.resolver)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_resolver_is_a_noop(self):
        stub = _Stub(self.db, None)
        ThreadWatcher._phash_new_file(stub, '/does/not/exist.webm')

    def test_unreadable_file_does_not_raise(self):
        """A watcher must never die because ffmpeg failed on one file."""
        bogus = os.path.join(self.tmp, 'not-media.webm')
        with open(bogus, 'wb') as fh:
            fh.write(b'this is not a video')
        ThreadWatcher._phash_new_file(self.stub, bogus)
        self.assertIsNone(self.db.get_phash(bogus))

    def test_records_phash_for_real_media(self):
        src = _sample_media()
        if not src:
            self.skipTest('no media in downloads/ to hash')
        dest = os.path.join(self.tmp, os.path.basename(src))
        shutil.copyfile(src, dest)

        ThreadWatcher._phash_new_file(self.stub, dest)

        stored = self.db.get_phash(dest)
        self.assertIsNotNone(stored)
        self.assertGreaterEqual(len(stored.frames), 1)
        self.assertGreater(stored.width, 0)

    def test_relocates_a_lower_resolution_repost(self):
        """The end-to-end live path: held file wins, incoming is set aside."""
        src = _sample_media()
        if not src:
            self.skipTest('no media in downloads/ to hash')
        held = os.path.join(self.tmp, 'held' + os.path.splitext(src)[1])
        shutil.copyfile(src, held)
        meta = perceptual.extract(held)
        if meta is None:
            self.skipTest('ffmpeg unavailable')
        self.db.record_phash(held, meta)

        # Same content arriving again under a different name.
        incoming = os.path.join(self.tmp, 'incoming' + os.path.splitext(src)[1])
        shutil.copyfile(src, incoming)
        ThreadWatcher._phash_new_file(self.stub, incoming)

        # Identical resolution means the incoming file cannot supersede, so it
        # is the one moved aside.
        self.assertFalse(os.path.exists(incoming))
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, ORIGINAL_DIR,
                         'incoming_1' + os.path.splitext(src)[1])))
        self.assertTrue(os.path.isfile(held))


if __name__ == '__main__':
    unittest.main()
