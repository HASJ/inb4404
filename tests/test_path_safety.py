"""Regression tests for filename sanitization and thread-URL validation.

These cover the hole fixed in "sanitize every download filename and validate
thread URLs": `_determine_file_path` sanitized only its `--title` branch, so
a name coming from the uploader (via `--origin-name`) or from the server
reached disk unsanitized and could escape the thread directory.

The path assertions here are the ones that matter -- they fail if anyone
reintroduces an early return that skips `sanitize_filename`.
"""
import os
import unittest

from inb4404.file_utils import FileManager
from inb4404.thread_parser import ThreadParser
from inb4404.thread_watcher import ThreadWatcher

TRAVERSAL = [
    '../../../../Windows/System32/evil.exe',
    '../../etc/passwd',
    'a/b/c.webm',
    '..\\..\\evil.exe',
    '/absolute/evil.webm',
]


class _Cfg(object):
    """Only the flags _determine_file_path reads."""

    def __init__(self, title=False, origin_name=False):
        self.title = title
        self.origin_name = origin_name
        self.verbose = False


class _Stub(object):
    """Minimal stand-in so _determine_file_path can be called unbound."""

    def __init__(self, directory, **flags):
        self.directory = directory
        self.config = _Cfg(**flags)
        self.file_manager = FileManager()


class TestSanitizeFilename(unittest.TestCase):
    def setUp(self):
        self.fm = FileManager()

    def test_strips_path_separators(self):
        for payload in TRAVERSAL:
            cleaned = self.fm.sanitize_filename(payload)
            self.assertNotIn('/', cleaned, payload)
            self.assertNotIn('\\', cleaned, payload)

    def test_result_stays_inside_the_directory(self):
        base = os.path.join('downloads', 'g', '1234')
        for payload in TRAVERSAL:
            joined = os.path.normpath(
                os.path.join(base, self.fm.sanitize_filename(payload)))
            self.assertTrue(
                joined.startswith(os.path.normpath(base) + os.sep),
                '%r escaped to %r' % (payload, joined))


class TestDeterminedPathIsContained(unittest.TestCase):
    """Every naming branch must funnel through sanitization."""

    def setUp(self):
        self.base = os.path.join('downloads', 'g', '1234')

    def _assert_contained(self, path):
        self.assertIsNotNone(path)
        norm = os.path.normpath(path)
        self.assertTrue(
            norm.startswith(os.path.normpath(self.base) + os.sep),
            'escaped to %r' % norm)

    def test_default_branch_is_sanitized(self):
        """The branch that was unsanitized before the fix."""
        for payload in TRAVERSAL:
            stub = _Stub(self.base)
            entry = ('https://i.4cdn.org/g/1.webm', payload)
            self._assert_contained(
                ThreadWatcher._determine_file_path(stub, entry, 0, []))

    def test_origin_name_branch_is_sanitized(self):
        """--origin-name takes the name straight from the uploader."""
        for payload in TRAVERSAL:
            stub = _Stub(self.base, origin_name=True)
            entry = ('https://i.4cdn.org/g/1.webm', '1.webm', None, None,
                     payload, 1234, '.webm')
            self._assert_contained(
                ThreadWatcher._determine_file_path(stub, entry, 0, []))

    def test_title_branch_is_sanitized(self):
        for payload in TRAVERSAL:
            stub = _Stub(self.base, title=True)
            entry = ('https://i.4cdn.org/g/1.webm', '1.webm')
            self._assert_contained(
                ThreadWatcher._determine_file_path(stub, entry, 0, [payload]))

    def test_normal_names_survive_intact(self):
        stub = _Stub(self.base)
        entry = ('https://i.4cdn.org/g/1.webm', '1591293452555.webm')
        path = ThreadWatcher._determine_file_path(stub, entry, 0, [])
        self.assertEqual(os.path.basename(path), '1591293452555.webm')


class TestThreadURLValidation(unittest.TestCase):
    def setUp(self):
        self.parser = ThreadParser()

    def test_accepts_valid_urls(self):
        r = self.parser.parse_url('https://boards.4chan.org/g/thread/12345')
        self.assertEqual((r.board, r.thread_id), ('g', '12345'))
        r = self.parser.parse_url(
            'https://boards.4channel.org/g/thread/12345/some-slug')
        self.assertEqual(r.slug, 'some-slug')

    def test_accepts_apex_and_subdomains(self):
        for url in ('https://4chan.org/g/thread/1',
                    'https://boards.4chan.org/g/thread/1',
                    'https://BOARDS.4CHAN.ORG/g/thread/1',
                    'https://boards.4chan.org./g/thread/1'):
            self.assertEqual(self.parser.parse_url(url).board, 'g', url)

    def test_rejects_foreign_host(self):
        for url in ('https://evil.example.com/g/thread/1',
                    'https://not-4chan.example/g/thread/1'):
            with self.assertRaises(ValueError):
                self.parser.parse_url(url)

    def test_rejects_lookalike_domains(self):
        """A bare endswith() check would let all of these through."""
        for url in ('https://not4chan.org/g/thread/1',
                    'https://evil4chan.org/g/thread/1',
                    'https://my4channel.org/g/thread/1',
                    'https://4chan.org.evil.com/g/thread/1'):
            with self.assertRaises(ValueError):
                self.parser.parse_url(url)

    def test_rejects_traversal_in_components(self):
        for url in ('https://boards.4chan.org/g/thread/../../etc',
                    'https://boards.4chan.org/../g/thread/1'):
            with self.assertRaises(ValueError):
                self.parser.parse_url(url)

    def test_rejects_malformed_paths(self):
        for url in ('https://boards.4chan.org/g/12345',
                    'https://boards.4chan.org/g',
                    'not a url',
                    ''):
            with self.assertRaises(ValueError):
                self.parser.parse_url(url)


if __name__ == '__main__':
    unittest.main()
