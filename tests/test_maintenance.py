"""Tests for server maintenance detection, escalating backoff, and watcher coordination."""
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import MagicMock, patch, call

from inb4404.config import Config, EXITCODE_MAINTENANCE
from inb4404.exceptions import HTTPError, MaintenanceError, ThreadNotFoundError
from inb4404.http_client import HTTPClient, is_maintenance_message, MAINTENANCE_MESSAGE
from inb4404.thread_watcher import ThreadWatcher
from inb4404.process_manager import ProcessManager, _call_watcher


class TestMaintenanceDetector(unittest.TestCase):
    """Test the maintenance message detection helper."""

    def test_exact_message(self):
        self.assertTrue(is_maintenance_message("Performing maintenance. We'll be back soon."))
        self.assertTrue(is_maintenance_message("performing maintenance. we'll be back soon."))

    def test_html_wrapped_message(self):
        html_doc = (
            "<!DOCTYPE html><html><head><title>Maintenance</title></head>"
            "<body><p>Performing maintenance. We'll be back soon.</p></body></html>"
        )
        self.assertTrue(is_maintenance_message(html_doc))

    def test_html_entity_variants(self):
        self.assertTrue(is_maintenance_message("Performing maintenance. We&#039;ll be back soon."))
        self.assertTrue(is_maintenance_message("Performing maintenance. We&apos;ll be back soon."))

    def test_bytes_content(self):
        self.assertTrue(is_maintenance_message(b"Performing maintenance. We'll be back soon."))
        self.assertTrue(is_maintenance_message(b"<html>Performing maintenance. We'll be back soon.</html>"))

    def test_non_maintenance_content(self):
        self.assertFalse(is_maintenance_message("429 Too Many Requests"))
        self.assertFalse(is_maintenance_message("404 Not Found"))
        self.assertFalse(is_maintenance_message("<html><body>Normal thread content</body></html>"))
        self.assertFalse(is_maintenance_message(None))
        self.assertFalse(is_maintenance_message(b""))


class TestHTTPClientMaintenance(unittest.TestCase):
    """Test HTTPClient behavior when server is in maintenance."""

    def test_fetch_200_with_maintenance_body_raises_immediately(self):
        """If a 200 response contains the maintenance message, MaintenanceError is raised."""
        client = HTTPClient()
        mock_response = MagicMock()
        mock_response.read.return_value = b"Performing maintenance. We'll be back soon."
        mock_response.status = 200

        with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
            with patch.object(client, '_sleep') as mock_sleep:
                with self.assertRaises(MaintenanceError) as ctx:
                    client.fetch('https://boards.4chan.org/g/thread/1000')

                self.assertEqual(mock_urlopen.call_count, 1)
                mock_sleep.assert_not_called()
                self.assertIn("Performing maintenance", str(ctx.exception))

    def test_fetch_429_with_maintenance_body_raises_immediately(self):
        """HTTP 429 with maintenance message raises MaintenanceError immediately without 10 retries."""
        client = HTTPClient()
        http_429 = urllib.error.HTTPError(
            'https://boards.4chan.org/g/thread/1000',
            429,
            'Too Many Requests',
            {},
            None
        )
        http_429.read = MagicMock(return_value=b"Performing maintenance. We'll be back soon.")

        with patch('urllib.request.urlopen', side_effect=http_429) as mock_urlopen:
            with patch.object(client, '_sleep') as mock_sleep:
                with self.assertRaises(MaintenanceError) as ctx:
                    client.fetch('https://boards.4chan.org/g/thread/1000', max_retries=10)

                # Should fail on first try and NOT retry 10 times
                self.assertEqual(mock_urlopen.call_count, 1)
                mock_sleep.assert_not_called()
                self.assertEqual(ctx.exception.code, 429)

    def test_fetch_503_with_maintenance_body_raises_immediately(self):
        """HTTP 503 with maintenance message raises MaintenanceError immediately without retrying."""
        client = HTTPClient()
        http_503 = urllib.error.HTTPError(
            'https://boards.4chan.org/g/thread/1000',
            503,
            'Service Unavailable',
            {},
            None
        )
        http_503.read = MagicMock(return_value=b"Performing maintenance. We'll be back soon.")

        with patch('urllib.request.urlopen', side_effect=http_503) as mock_urlopen:
            with patch.object(client, '_sleep') as mock_sleep:
                with self.assertRaises(MaintenanceError) as ctx:
                    client.fetch('https://boards.4chan.org/g/thread/1000', max_retries=5)

                self.assertEqual(mock_urlopen.call_count, 1)
                mock_sleep.assert_not_called()
                self.assertEqual(ctx.exception.code, 503)

    def test_fetch_routine_429_without_maintenance_message_retries(self):
        """Standard 429 (rate limit) without maintenance message retries up to max_retries."""
        client = HTTPClient()
        http_429 = urllib.error.HTTPError(
            'https://boards.4chan.org/g/thread/1000',
            429,
            'Too Many Requests',
            {},
            None
        )
        http_429.read = MagicMock(return_value=b"Rate limit exceeded.")

        with patch('urllib.request.urlopen', side_effect=http_429) as mock_urlopen:
            with patch.object(client, '_sleep') as mock_sleep:
                with self.assertRaises(HTTPError) as ctx:
                    client.fetch('https://boards.4chan.org/g/thread/1000', max_retries=3)

                self.assertNotIsInstance(ctx.exception, MaintenanceError)
                self.assertEqual(mock_urlopen.call_count, 4)
                self.assertEqual(mock_sleep.call_count, 3)

    def test_fetch_thread_api_detects_maintenance(self):
        """fetch_thread_api raises MaintenanceError if maintenance is returned."""
        client = HTTPClient()
        http_429 = urllib.error.HTTPError(
            'https://a.4cdn.org/g/thread/1000.json',
            429,
            'Too Many Requests',
            {},
            None
        )
        http_429.read = MagicMock(return_value=b"Performing maintenance. We'll be back soon.")

        with patch('urllib.request.urlopen', side_effect=http_429):
            with self.assertRaises(MaintenanceError):
                client.fetch_thread_api('g', '1000')


class TestThreadWatcherMaintenance(unittest.TestCase):
    """Test ThreadWatcher maintenance handling and backoff."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.config = Config(
            workpath=self.tmp_dir,
            maintenance_initial_wait=1800.0,
            maintenance_wait_increment=1800.0,
            maintenance_max_wait=7200.0,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_single_watcher_escalating_backoff(self):
        """Single watcher applies 30m, 60m, 90m, 120m, 120m backoff on maintenance."""
        watcher = ThreadWatcher(
            'https://boards.4chan.org/g/thread/1000',
            self.config,
            self.tmp_dir,
            raise_on_maintenance=False,
        )

        sleep_calls = []
        stop_event = threading.Event()
        watcher.stop_event = stop_event

        attempt_count = 0

        def fake_fetch_thread_data():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count <= 5:
                raise MaintenanceError("Performing maintenance. We'll be back soon.")
            # On 6th attempt, succeed and set stop_event
            stop_event.set()
            return ([], [])

        def fake_sleep(seconds):
            sleep_calls.append(seconds)

        watcher._load_existing_hashes = MagicMock()
        watcher._scan_directory = MagicMock()
        watcher._fetch_thread_data = MagicMock(side_effect=fake_fetch_thread_data)
        watcher._sleep = MagicMock(side_effect=fake_sleep)

        watcher.watch()

        # Check sleep durations for the 5 maintenance attempts
        # Attempt 1: 1800s (30 min)
        # Attempt 2: 3600s (60 min)
        # Attempt 3: 5400s (90 min)
        # Attempt 4: 7200s (120 min)
        # Attempt 5: 7200s (120 min max capped)
        self.assertEqual(len(sleep_calls), 5)
        self.assertEqual(sleep_calls[0], 1800.0)
        self.assertEqual(sleep_calls[1], 3600.0)
        self.assertEqual(sleep_calls[2], 5400.0)
        self.assertEqual(sleep_calls[3], 7200.0)
        self.assertEqual(sleep_calls[4], 7200.0)

    def test_child_watcher_raises_system_exit_on_maintenance(self):
        """When raise_on_maintenance=True, watch() raises SystemExit(EXITCODE_MAINTENANCE)."""
        watcher = ThreadWatcher(
            'https://boards.4chan.org/g/thread/1000',
            self.config,
            self.tmp_dir,
            raise_on_maintenance=True,
        )

        watcher._load_existing_hashes = MagicMock()
        watcher._scan_directory = MagicMock()
        watcher._fetch_thread_data = MagicMock(
            side_effect=MaintenanceError("Performing maintenance. We'll be back soon.")
        )

        with self.assertRaises(SystemExit) as ctx:
            watcher.watch()

        self.assertEqual(ctx.exception.code, EXITCODE_MAINTENANCE)


class TestProcessManagerMaintenance(unittest.TestCase):
    """Test ProcessManager stopping all watchers and monitoring with single watcher during maintenance."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.queue_file = os.path.join(self.tmp_dir, 'queue.txt')
        self.config = Config(
            workpath=self.tmp_dir,
            maintenance_initial_wait=0.01,
            maintenance_wait_increment=0.01,
            maintenance_max_wait=0.05,
        )
        self.pm = ProcessManager(self.queue_file, self.config, self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_queue(self, content: str):
        with open(self.queue_file, 'w', encoding='utf-8') as f:
            f.write(content)

    def _read_queue(self) -> str:
        with open(self.queue_file, 'r', encoding='utf-8') as f:
            return f.read()

    def test_exitcode_maintenance_checks(self):
        """_is_exitcode_maintenance matches 503, 429, and POSIX wrapped codes."""
        self.assertTrue(self.pm._is_exitcode_maintenance(503))
        self.assertTrue(self.pm._is_exitcode_maintenance(503 % 256))
        self.assertTrue(self.pm._is_exitcode_maintenance(429))
        self.assertTrue(self.pm._is_exitcode_maintenance(429 % 256))
        self.assertFalse(self.pm._is_exitcode_maintenance(404))
        self.assertFalse(self.pm._is_exitcode_maintenance(0))
        self.assertFalse(self.pm._is_exitcode_maintenance(None))

    def test_maintenance_stops_all_other_watchers_and_preserves_queue(self):
        """When maintenance is detected, all running processes are stopped and queue URLs are preserved."""
        link1 = "https://boards.4chan.org/gif/thread/1000"
        link2 = "https://boards.4chan.org/gif/thread/2000"
        link3 = "https://boards.4chan.org/gif/thread/3000"
        self._write_queue(f"{link1}\n{link2}\n{link3}\n")

        proc1 = MagicMock()
        proc1.exitcode = EXITCODE_MAINTENANCE
        proc1.is_alive.return_value = False

        proc2 = MagicMock()
        proc2.is_alive.return_value = True

        proc3 = MagicMock()
        proc3.is_alive.return_value = True

        self.pm.running_processes = {
            link1: proc1,
            link2: proc2,
            link3: proc3,
        }

        # Mock probe in _handle_maintenance_mode so it succeeds on first check
        self.pm.http_client.fetch_thread_api = MagicMock(return_value={'posts': []})

        with patch('time.sleep'):
            res = self.pm._handle_dead_process(link1, max_restarts=3)

        # Should not disable link in file!
        self.assertFalse(res)
        queue_content = self._read_queue()
        self.assertNotIn("-https://boards.4chan.org/gif/thread/1000", queue_content)
        self.assertIn(link1, queue_content)
        self.assertIn(link2, queue_content)
        self.assertIn(link3, queue_content)

        # Other processes must have been terminated
        proc2.terminate.assert_called_once()
        proc3.terminate.assert_called_once()

        # running_processes should now be empty (ready to restart)
        self.assertEqual(len(self.pm.running_processes), 0)

    def test_maintenance_recovery_loop(self):
        """_handle_maintenance_mode probes until maintenance ends."""
        probe_link = "https://boards.4chan.org/gif/thread/1000"
        
        probe_attempts = 0
        def fake_fetch_thread_api(board, thread_id):
            nonlocal probe_attempts
            probe_attempts += 1
            if probe_attempts <= 2:
                raise MaintenanceError("Performing maintenance. We'll be back soon.")
            return {'posts': []}

        self.pm.http_client.fetch_thread_api = MagicMock(side_effect=fake_fetch_thread_api)

        with patch('time.sleep'):
            self.pm._handle_maintenance_mode(probe_link)

        # Failed twice with MaintenanceError, succeeded on 3rd attempt
        self.assertEqual(probe_attempts, 3)


if __name__ == '__main__':
    unittest.main()
