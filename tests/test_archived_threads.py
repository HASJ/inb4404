"""Tests for archived thread detection and getaddrinfo (11001) error handling."""
import os
import json
import socket
import shutil
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from inb4404.config import Config
from inb4404.http_client import HTTPClient, is_getaddrinfo_error
from inb4404.thread_watcher import ThreadWatcher
from inb4404.queue_manager import QueueManager, DownloadTask


class TestHTTPClientArchive(unittest.TestCase):
    """Test HTTPClient getaddrinfo error detection and archive API fetching."""

    def test_is_getaddrinfo_error(self):
        """is_getaddrinfo_error correctly detects 11001 and DNS failures."""
        err1 = urllib.error.URLError("[Errno 11001] getaddrinfo failed")
        self.assertTrue(is_getaddrinfo_error(err1))

        gai = socket.gaierror(11001, "getaddrinfo failed")
        err2 = urllib.error.URLError(gai)
        self.assertTrue(is_getaddrinfo_error(err2))

        err3 = urllib.error.URLError("Connection refused")
        self.assertFalse(is_getaddrinfo_error(err3))

        self.assertFalse(is_getaddrinfo_error(None))

    def test_fetch_archive_api_json_success(self):
        """fetch_archive_api decodes integer thread IDs from JSON."""
        client = HTTPClient()
        fake_json = json.dumps([31081732, 31108733, "31109999"]).encode('utf-8')
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_json

        with patch('urllib.request.urlopen', return_value=mock_resp):
            ids = client.fetch_archive_api('gif')
            self.assertEqual(ids, [31081732, 31108733, 31109999])

    def test_fetch_archive_api_uses_cache(self):
        """fetch_archive_api reuses cached result within TTL without re-fetching."""
        client = HTTPClient()
        fake_json = json.dumps([123, 456]).encode('utf-8')
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_json

        with patch('urllib.request.urlopen', return_value=mock_resp) as mock_urlopen:
            ids1 = client.fetch_archive_api('g', cache_ttl=60.0)
            self.assertEqual(ids1, [123, 456])
            self.assertEqual(mock_urlopen.call_count, 1)

            # Second call should use cache
            ids2 = client.fetch_archive_api('g', cache_ttl=60.0)
            self.assertEqual(ids2, [123, 456])
            self.assertEqual(mock_urlopen.call_count, 1)

    def test_fetch_archive_api_html_fallback(self):
        """fetch_archive_api falls back to scraping HTML archive when JSON fails."""
        client = HTTPClient()
        html_content = b"""
        <html>
        <body>
        <table id="arc-list">
        <tr><td><a href="/gif/thread/31108733#p31108733">31108733</a></td></tr>
        <tr><td><a href="/gif/thread/31109791#p31109791">31109791</a></td></tr>
        </table>
        </body>
        </html>
        """

        # JSON open fails, fetch succeeds
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("API down")):
            with patch.object(client, 'fetch', return_value=html_content):
                ids = client.fetch_archive_api('gif', cache_ttl=0.0)
                self.assertIsNotNone(ids)
                self.assertIn(31108733, ids)
                self.assertIn(31109791, ids)


class TestThreadWatcherArchive(unittest.TestCase):
    """Test ThreadWatcher archived detection and lifecycle."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.config = Config(
            workpath=self.tmp_dir,
            phash_enabled=False,
            refresh_time=0.1,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_fetch_thread_data_detects_api_archived(self):
        """API response with archived: 1 sets is_archived to True."""
        watcher = ThreadWatcher(
            "https://boards.4chan.org/gif/thread/31108733",
            self.config,
            self.tmp_dir,
        )
        fake_api_data = {
            'posts': [
                {'no': 31108733, 'archived': 1, 'closed': 1, 'tim': 100, 'ext': '.jpg', 'md5': 'dGVzdA=='}
            ]
        }
        watcher.http_client.fetch_thread_api = MagicMock(return_value=fake_api_data)

        self.assertFalse(watcher.is_archived)
        items, _ = watcher._fetch_thread_data()
        self.assertTrue(watcher.is_archived)
        self.assertEqual(len(items), 1)

    def test_fetch_thread_data_detects_html_archived(self):
        """Scraped HTML containing 'Thread archived.' sets is_archived to True."""
        watcher = ThreadWatcher(
            "https://boards.4chan.org/gif/thread/31108733",
            self.config,
            self.tmp_dir,
        )
        watcher.http_client.fetch_thread_api = MagicMock(return_value=None)
        html_content = (
            b'<html><div class="closed">Thread archived.<br>You cannot reply anymore.</div>'
            b'<a href="//i.4cdn.org/gif/123.jpg">123.jpg</a></html>'
        )
        watcher.http_client.fetch = MagicMock(return_value=html_content)

        self.assertFalse(watcher.is_archived)
        items, _ = watcher._fetch_thread_data()
        self.assertTrue(watcher.is_archived)
        self.assertEqual(len(items), 1)

    def test_check_if_archived_queries_archive_endpoint(self):
        """check_if_archived queries board archive and marks is_archived."""
        watcher = ThreadWatcher(
            "https://boards.4chan.org/gif/thread/31108733",
            self.config,
            self.tmp_dir,
        )
        watcher.http_client.fetch_archive_api = MagicMock(return_value=[31108733, 31109999])

        self.assertFalse(watcher.is_archived)
        self.assertTrue(watcher.check_if_archived())
        self.assertTrue(watcher.is_archived)

    def test_watch_exits_cleanly_when_archived_and_completed(self):
        """watch() finishes downloading files, detects archived, and exits cleanly."""
        watcher = ThreadWatcher(
            "https://boards.4chan.org/gif/thread/31108733",
            self.config,
            self.tmp_dir,
        )
        watcher._fetch_thread_data = MagicMock(return_value=([], []))
        watcher.is_archived = True

        # Should return immediately without hanging or sleeping
        watcher.watch()
        self.assertTrue(watcher.has_completed_cycle)

    def test_watch_exits_on_11001_error_if_archived_and_cycle_complete(self):
        """watch() exits cleanly on getaddrinfo error if archived and cycle complete."""
        watcher = ThreadWatcher(
            "https://boards.4chan.org/gif/thread/31108733",
            self.config,
            self.tmp_dir,
        )
        watcher.has_completed_cycle = True
        watcher.is_archived = True

        # Simulate getaddrinfo error on next poll
        watcher._fetch_thread_data = MagicMock(
            side_effect=urllib.error.URLError("[Errno 11001] getaddrinfo failed")
        )

        watcher.watch()
        # Successfully exited without looping


class TestQueueManagerArchive(unittest.TestCase):
    """Test QueueManager handling of archived threads in queue."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.queue_file = os.path.join(self.tmp_dir, 'queue.txt')
        self.config = Config(
            workpath=self.tmp_dir,
            phash_enabled=False,
            refresh_time=1.0,
        )
        self.qm = QueueManager(self.queue_file, self.config, self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_queue(self, content: str):
        with open(self.queue_file, 'w', encoding='utf-8') as f:
            f.write(content)

    def _read_queue(self) -> str:
        with open(self.queue_file, 'r', encoding='utf-8') as f:
            return f.read()

    def test_poll_thread_disables_archived_thread_when_all_downloaded(self):
        """poll_thread disables and stops archived thread when all files are downloaded/skipped."""
        link = "https://boards.4chan.org/gif/thread/31108733"
        self._write_queue(f"{link}\n")

        watcher = self.qm.start_watcher(link)
        self.assertIn(link, self.qm.watchers)

        # Mock thread data: 2 items, both already existing/skipped
        watcher._fetch_thread_data = MagicMock(return_value=([("link1",), ("link2",)], []))
        watcher.prepare_download_task = MagicMock(return_value=None)  # all skipped
        watcher.is_archived = True

        self.qm.poll_thread(link, watcher)

        # Watcher should be stopped
        self.assertNotIn(link, self.qm.watchers)
        # Queue file should have '-' prepended
        self.assertIn(f"-{link}", self._read_queue())

    def test_poll_thread_waits_for_pending_tasks_before_disabling(self):
        """poll_thread keeps archived thread active while tasks are still downloading."""
        link = "https://boards.4chan.org/gif/thread/31108733"
        self._write_queue(f"{link}\n")

        watcher = self.qm.start_watcher(link)
        watcher._fetch_thread_data = MagicMock(return_value=([("link1",)], []))
        dummy_task = MagicMock()
        dummy_task.watcher = watcher
        watcher.prepare_download_task = MagicMock(return_value=dummy_task)
        watcher.is_archived = True

        self.qm.poll_thread(link, watcher)

        # Still in watchers because 1 task is pending in queue
        self.assertIn(link, self.qm.watchers)
        self.assertEqual(watcher.pending_tasks, 1)
        self.assertFalse(self.qm.download_queue.empty())

        # Finish task
        task = self.qm.download_queue.get_nowait()
        watcher.decrement_pending_tasks()
        self.assertEqual(watcher.pending_tasks, 0)

        # Next poll: no new tasks, 0 pending
        watcher.prepare_download_task = MagicMock(return_value=None)
        self.qm.poll_thread(link, watcher)

        # Now stopped and disabled
        self.assertNotIn(link, self.qm.watchers)
        self.assertIn(f"-{link}", self._read_queue())

    def test_poll_thread_handles_11001_gracefully_when_archived_and_cycle_complete(self):
        """poll_thread disables archived thread if 11001 happens after completing cycle."""
        link = "https://boards.4chan.org/gif/thread/31108733"
        self._write_queue(f"{link}\n")

        watcher = self.qm.start_watcher(link)
        watcher.has_completed_cycle = True
        watcher.is_archived = True
        watcher.pending_tasks = 0

        # Simulate 11001 error on next poll
        watcher._fetch_thread_data = MagicMock(
            side_effect=urllib.error.URLError("[Errno 11001] getaddrinfo failed")
        )

        self.qm.poll_thread(link, watcher)

        # Must be disabled and removed from watchers instead of retrying endlessly
        self.assertNotIn(link, self.qm.watchers)
        self.assertIn(f"-{link}", self._read_queue())


if __name__ == '__main__':
    unittest.main()

