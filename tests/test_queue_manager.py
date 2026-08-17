"""Tests for ProcessManager queue loading, link disabling, and 404 handling."""
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from inb4404.config import Config
from inb4404.process_manager import ProcessManager
from inb4404.thread_parser import ThreadParser, ThreadURL
from inb4404.exceptions import ThreadNotFoundError, HTTPError


class TestQueueManager(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.queue_file = os.path.join(self.tmp_dir, 'queue.txt')
        self.config = Config(workpath=self.tmp_dir)
        self.pm = ProcessManager(self.queue_file, self.config, self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_queue(self, content: str):
        with open(self.queue_file, 'w', encoding='utf-8') as f:
            f.write(content)

    def _read_queue(self) -> str:
        with open(self.queue_file, 'r', encoding='utf-8') as f:
            return f.read()

    def test_load_queue_simple_and_disabled(self):
        self._write_queue(
            "https://boards.4chan.org/gif/thread/1000\n"
            "-https://boards.4chan.org/gif/thread/2000\n"
            "- https://boards.4chan.org/gif/thread/3000\n"
            "# https://boards.4chan.org/gif/thread/4000\n"
            "   https://boards.4chan.org/gif/thread/5000   \n"
        )
        links = self.pm.load_queue()
        self.assertIn("https://boards.4chan.org/gif/thread/1000", links)
        self.assertIn("https://boards.4chan.org/gif/thread/5000", links)
        self.assertNotIn("https://boards.4chan.org/gif/thread/2000", links)
        self.assertNotIn("https://boards.4chan.org/gif/thread/3000", links)
        self.assertNotIn("https://boards.4chan.org/gif/thread/4000", links)
        self.assertEqual(len(links), 2)

    def test_load_queue_multi_token_and_inline_comments(self):
        self._write_queue(
            "https://boards.4chan.org/gif/thread/1001 https://boards.4chan.org/gif/thread/1002\n"
            "https://boards.4chan.org/gif/thread/1003 # some comment\n"
            "https://boards.4chan.org/gif/thread/1005 -https://boards.4chan.org/gif/thread/1004\n"
        )
        links = self.pm.load_queue()
        self.assertIn("https://boards.4chan.org/gif/thread/1001", links)
        self.assertIn("https://boards.4chan.org/gif/thread/1002", links)
        self.assertIn("https://boards.4chan.org/gif/thread/1003", links)
        self.assertIn("https://boards.4chan.org/gif/thread/1005", links)
        self.assertNotIn("https://boards.4chan.org/gif/thread/1004", links)
        self.assertNotIn("#", links)
        self.assertEqual(len(links), 4)

    def test_disable_link_all_duplicate_occurrences(self):
        self._write_queue(
            "https://boards.4chan.org/gif/thread/1000\n"
            "https://boards.4chan.org/gif/thread/2000\n"
            "https://boards.4chan.org/gif/thread/1000\n"
            "https://boards.4chan.org/gif/thread/3000\n"
            "https://boards.4chan.org/gif/thread/1000\n"
        )
        self.pm._disable_link("https://boards.4chan.org/gif/thread/1000", reason="404")
        content = self._read_queue()
        self.assertEqual(
            content,
            "-https://boards.4chan.org/gif/thread/1000\n"
            "https://boards.4chan.org/gif/thread/2000\n"
            "-https://boards.4chan.org/gif/thread/1000\n"
            "https://boards.4chan.org/gif/thread/3000\n"
            "-https://boards.4chan.org/gif/thread/1000\n"
        )
        # Next load_queue should not contain thread 1000
        links = self.pm.load_queue()
        self.assertNotIn("https://boards.4chan.org/gif/thread/1000", links)
        self.assertEqual(len(links), 2)

    def test_disable_link_slug_and_canonical_matching(self):
        # File has link with a slug, but disable is called with canonical link (or vice-versa)
        self._write_queue(
            "https://boards.4chan.org/gif/thread/1000/my-awesome-thread\n"
            "https://boards.4chan.org/gif/thread/2000\n"
        )
        self.pm._disable_link("https://boards.4chan.org/gif/thread/1000", reason="404")
        content = self._read_queue()
        self.assertIn("-https://boards.4chan.org/gif/thread/1000/my-awesome-thread", content)
        self.assertIn("https://boards.4chan.org/gif/thread/2000", content)

    def test_exitcode_404_checks(self):
        self.assertTrue(self.pm._is_exitcode_404(404))
        self.assertTrue(self.pm._is_exitcode_404(148))  # 404 % 256
        self.assertFalse(self.pm._is_exitcode_404(0))
        self.assertFalse(self.pm._is_exitcode_404(1))
        self.assertFalse(self.pm._is_exitcode_404(None))

    def test_handle_dead_process_exitcode_404(self):
        link = "https://boards.4chan.org/gif/thread/1000"
        self._write_queue(f"{link}\n")
        
        proc = MagicMock()
        proc.exitcode = 404
        proc.is_alive.return_value = False
        self.pm.running_processes[link] = proc

        res = self.pm._handle_dead_process(link, max_restarts=3)
        self.assertTrue(res)
        self.assertNotIn(link, self.pm.running_processes)
        self.assertIn(f"-{link}", self._read_queue())

    def test_handle_dead_process_exitcode_posix_wrap_404(self):
        link = "https://boards.4chan.org/gif/thread/1000"
        self._write_queue(f"{link}\n")
        
        proc = MagicMock()
        proc.exitcode = 148  # 404 % 256 on POSIX
        proc.is_alive.return_value = False
        self.pm.running_processes[link] = proc

        res = self.pm._handle_dead_process(link, max_restarts=3)
        self.assertTrue(res)
        self.assertNotIn(link, self.pm.running_processes)
        self.assertIn(f"-{link}", self._read_queue())

    def test_handle_dead_process_transient_error_leaves_in_queue(self):
        link = "https://boards.4chan.org/gif/thread/1000"
        self._write_queue(f"{link}\n")
        
        proc = MagicMock()
        proc.exitcode = 1
        proc.is_alive.return_value = False
        self.pm.running_processes[link] = proc

        # Mock http_client to simulate alive thread API response
        self.pm.http_client.fetch_thread_api = MagicMock(return_value={'posts': []})

        # Mock Process so restart fails
        with patch('inb4404.process_manager.Process') as mock_proc_cls:
            mock_new_proc = MagicMock()
            mock_new_proc.is_alive.return_value = False
            mock_proc_cls.return_value = mock_new_proc

            res = self.pm._handle_dead_process(link, max_restarts=1)
            # Should NOT disable the link because it's not a 404
            self.assertFalse(res)
            self.assertNotIn(link, self.pm.running_processes)
            # Queue file must NOT have '-' prepended!
            self.assertEqual(self._read_queue().strip(), link)


class TestThreadURLRobustness(unittest.TestCase):
    def setUp(self):
        self.parser = ThreadParser()

    def test_slug_with_dots_and_symbols(self):
        url = "https://boards.4chan.org/g/thread/12345/v1.0.0_release"
        parsed = self.parser.parse_url(url)
        self.assertEqual(parsed.board, "g")
        self.assertEqual(parsed.thread_id, "12345")
        self.assertEqual(parsed.slug, "v1.0.0_release")
        self.assertEqual(parsed.canonical_url, "https://boards.4chan.org/g/thread/12345")

    def test_slug_with_percent_encoding(self):
        url = "https://boards.4chan.org/jp/thread/99999/%E3%83%86%E3%82%B9%E3%83%88"
        parsed = self.parser.parse_url(url)
        self.assertEqual(parsed.board, "jp")
        self.assertEqual(parsed.thread_id, "99999")
        self.assertEqual(parsed.canonical_url, "https://boards.4chan.org/jp/thread/99999")

    def test_url_with_fragment_or_query(self):
        url = "https://boards.4chan.org/wsg/thread/55555#p55555"
        parsed = self.parser.parse_url(url)
        self.assertEqual(parsed.board, "wsg")
        self.assertEqual(parsed.thread_id, "55555")
        self.assertEqual(parsed.canonical_url, "https://boards.4chan.org/wsg/thread/55555")

    def test_trailing_slash(self):
        url = "https://boards.4chan.org/wsg/thread/55555/"
        parsed = self.parser.parse_url(url)
        self.assertEqual(parsed.board, "wsg")
        self.assertEqual(parsed.thread_id, "55555")
        self.assertEqual(parsed.canonical_url, "https://boards.4chan.org/wsg/thread/55555")


if __name__ == '__main__':
    unittest.main()
