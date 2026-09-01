"""Tests for server maintenance detection, escalating backoff, and watcher coordination."""
import multiprocessing
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


class TestThreadWatcherRateLimit(unittest.TestCase):
    """Test shared cooldown behavior after media-download rate limiting."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.config = Config(workpath=self.tmp_dir, phash_enabled=False)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _watcher(self, stop_event=None, rate_limit_until=None, subject=None):
        config = Config(
            workpath=self.tmp_dir,
            phash_enabled=False,
            subject=self.config.subject if subject is None else subject,
        )
        return ThreadWatcher(
            'https://boards.4chan.org/g/thread/1000',
            config,
            self.tmp_dir,
            stop_event=stop_event,
            rate_limit_until=rate_limit_until,
        )

    def test_media_429_stops_retries_and_sets_shared_ten_minute_deadline(self):
        """The first media 429 starts a shared ten-minute cooldown immediately."""
        rate_limit_until = multiprocessing.Value('d', 0.0)
        watcher = self._watcher(rate_limit_until=rate_limit_until, subject=False)
        watcher._determine_file_path = MagicMock(
            return_value=os.path.join(self.tmp_dir, 'file.jpg')
        )
        watcher.http_client._sleep = MagicMock()
        watcher._sleep = MagicMock()

        http_429 = urllib.error.HTTPError(
            'https://i.4cdn.org/g/1.jpg',
            429,
            'Too Many Requests',
            {},
            None,
        )
        http_429.read = MagicMock(return_value=b'Rate limit exceeded.')
        self.addCleanup(http_429.close)

        with patch('urllib.request.urlopen', side_effect=http_429) as mock_urlopen:
            with patch('inb4404.thread_watcher.time.time', return_value=1000.0):
                watcher._process_file_entry(
                    ('https://i.4cdn.org/g/1.jpg', 'file.jpg', None),
                    0,
                    [],
                    1,
                    1,
                )

        self.assertEqual(mock_urlopen.call_count, 1)
        watcher.http_client._sleep.assert_not_called()
        self.assertEqual(rate_limit_until.value, 1600.0)

    def test_in_flight_429_does_not_extend_active_deadline(self):
        """Later 429s while cooldown is active do not extend deadline; after expiry a new 429 resets it."""
        rate_limit_until = multiprocessing.Value('d', 1600.0)
        watcher = self._watcher(rate_limit_until=rate_limit_until, subject=False)
        watcher._determine_file_path = MagicMock(
            return_value=os.path.join(self.tmp_dir, 'file.jpg')
        )
        watcher.http_client._sleep = MagicMock()
        watcher._sleep = MagicMock()
        # Simulate in-flight request that already passed the gate
        watcher._wait_for_rate_limit = MagicMock(return_value=True)

        http_429 = urllib.error.HTTPError(
            'https://i.4cdn.org/g/1.jpg',
            429,
            'Too Many Requests',
            {},
            None,
        )
        http_429.read = MagicMock(return_value=b'Rate limit exceeded.')
        self.addCleanup(http_429.close)

        # In-flight 429 at now=1030 while deadline is 1600: must NOT extend to 1630
        with patch('urllib.request.urlopen', side_effect=http_429):
            with patch('inb4404.thread_watcher.time.time', return_value=1030.0):
                watcher._process_file_entry(
                    ('https://i.4cdn.org/g/1.jpg', 'file.jpg', None),
                    0,
                    [],
                    1,
                    1,
                )

        self.assertEqual(rate_limit_until.value, 1600.0)

        # After deadline expiry at now=1605: new 429 sets 1605 + 600 = 2205
        with patch('urllib.request.urlopen', side_effect=http_429):
            with patch('inb4404.thread_watcher.time.time', return_value=1605.0):
                watcher._process_file_entry(
                    ('https://i.4cdn.org/g/1.jpg', 'file.jpg', None),
                    0,
                    [],
                    1,
                    1,
                )

        self.assertEqual(rate_limit_until.value, 2205.0)

    def test_local_rate_limit_cooldown_semantics(self):
        """Local cooldown (rate_limit_until=None) starts at now+600 and is not extended while active."""
        watcher = self._watcher(rate_limit_until=None, subject=False)
        watcher._determine_file_path = MagicMock(
            return_value=os.path.join(self.tmp_dir, 'file.jpg')
        )
        watcher.http_client._sleep = MagicMock()
        watcher._sleep = MagicMock()
        watcher._wait_for_rate_limit = MagicMock(return_value=True)

        http_429 = urllib.error.HTTPError(
            'https://i.4cdn.org/g/1.jpg',
            429,
            'Too Many Requests',
            {},
            None,
        )
        http_429.read = MagicMock(return_value=b'Rate limit exceeded.')
        self.addCleanup(http_429.close)

        # First 429 at now=1000 sets local deadline to 1600
        with patch('urllib.request.urlopen', side_effect=http_429):
            with patch('inb4404.thread_watcher.time.time', return_value=1000.0):
                watcher._process_file_entry(
                    ('https://i.4cdn.org/g/1.jpg', 'file.jpg', None),
                    0,
                    [],
                    1,
                    1,
                )
        self.assertEqual(watcher._get_rate_limit_until(), 1600.0)

        # Subsequent 429 at now=1030 does not extend
        with patch('urllib.request.urlopen', side_effect=http_429):
            with patch('inb4404.thread_watcher.time.time', return_value=1030.0):
                watcher._process_file_entry(
                    ('https://i.4cdn.org/g/1.jpg', 'file.jpg', None),
                    0,
                    [],
                    1,
                    1,
                )
        self.assertEqual(watcher._get_rate_limit_until(), 1600.0)

    def test_startup_subject_lookup_gated_by_active_cooldown(self):
        """Startup subject lookup waits for an active cooldown before making request."""
        rate_limit_until = multiprocessing.Value('d', 1600.0)
        now = [1000.0]
        sleep_calls = []

        config = Config(workpath=self.tmp_dir, phash_enabled=False, subject=True)

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            now[0] += seconds

        with patch('inb4404.thread_watcher.time.time', side_effect=lambda: now[0]):
            with patch('inb4404.thread_parser.ThreadParser.get_subject', return_value='Test Subject') as mock_get_subj:
                with patch.object(ThreadWatcher, '_sleep', side_effect=fake_sleep):
                    watcher = ThreadWatcher(
                        'https://boards.4chan.org/g/thread/1000',
                        config,
                        self.tmp_dir,
                        rate_limit_until=rate_limit_until,
                    )

        self.assertEqual(sleep_calls, [600.0])
        mock_get_subj.assert_called_once_with('g', '1000')
        self.assertEqual(watcher.thread_dir_name, '1000 (Test Subject)')

    def test_startup_subject_lookup_aborts_on_stop_event(self):
        """Startup subject lookup does not perform network request if stop_event is set during wait."""
        rate_limit_until = multiprocessing.Value('d', 1600.0)
        stop_event = threading.Event()
        stop_event.set()

        config = Config(workpath=self.tmp_dir, phash_enabled=False, subject=True)

        with patch('inb4404.thread_watcher.time.time', return_value=1000.0):
            with patch('inb4404.thread_parser.ThreadParser.get_subject') as mock_get_subj:
                with patch.object(ThreadWatcher, '_sleep') as mock_sleep:
                    watcher = ThreadWatcher(
                        'https://boards.4chan.org/g/thread/1000',
                        config,
                        self.tmp_dir,
                        stop_event=stop_event,
                        rate_limit_until=rate_limit_until,
                    )

        mock_get_subj.assert_not_called()
        mock_sleep.assert_not_called()
        self.assertEqual(watcher.thread_dir_name, '1000')

    def test_watcher_waits_for_shared_rate_limit_before_refreshing(self):
        """A watcher waits for the shared cooldown before its next request."""
        stop_event = threading.Event()
        rate_limit_until = multiprocessing.Value('d', 1600.0)
        watcher = self._watcher(
            stop_event=stop_event,
            rate_limit_until=rate_limit_until,
            subject=False,
        )
        watcher._load_existing_hashes = MagicMock()
        watcher._scan_directory = MagicMock()
        watcher._fetch_thread_data = MagicMock(return_value=([], []))

        now = [1000.0]
        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            now[0] += seconds
            stop_event.set()

        watcher._sleep = MagicMock(side_effect=fake_sleep)

        with patch('inb4404.thread_watcher.time.time', side_effect=lambda: now[0]):
            watcher.watch()

        self.assertEqual(sleep_calls, [600.0])
        watcher._fetch_thread_data.assert_not_called()

    def test_media_download_waits_for_shared_rate_limit(self):
        """_process_file_entry waits for active cooldown before downloading media."""
        rate_limit_until = multiprocessing.Value('d', 1600.0)
        watcher = self._watcher(rate_limit_until=rate_limit_until, subject=False)
        watcher._determine_file_path = MagicMock(
            return_value=os.path.join(self.tmp_dir, 'file.jpg')
        )
        now = [1000.0]
        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            now[0] += seconds

        watcher._sleep = MagicMock(side_effect=fake_sleep)
        watcher.http_client.fetch = MagicMock(return_value=b'filedata')
        watcher._save_file = MagicMock()
        watcher._phash_new_file = MagicMock()

        with patch('inb4404.thread_watcher.time.time', side_effect=lambda: now[0]):
            watcher._process_file_entry(
                ('https://i.4cdn.org/g/1.jpg', 'file.jpg', None),
                0,
                [],
                1,
                1,
            )

        self.assertEqual(sleep_calls, [600.0, watcher.throttle])
        watcher.http_client.fetch.assert_called_once_with('https://i.4cdn.org/g/1.jpg', retry_on_429=False)

    def test_media_download_aborts_on_stop_event_during_cooldown(self):
        """_process_file_entry does not download if stop_event is set during cooldown wait."""
        stop_event = threading.Event()
        stop_event.set()
        rate_limit_until = multiprocessing.Value('d', 1600.0)
        watcher = self._watcher(stop_event=stop_event, rate_limit_until=rate_limit_until, subject=False)
        watcher._determine_file_path = MagicMock(
            return_value=os.path.join(self.tmp_dir, 'file.jpg')
        )
        watcher._sleep = MagicMock()
        watcher.http_client.fetch = MagicMock()

        with patch('inb4404.thread_watcher.time.time', return_value=1000.0):
            watcher._process_file_entry(
                ('https://i.4cdn.org/g/1.jpg', 'file.jpg', None),
                0,
                [],
                1,
                1,
            )

        watcher.http_client.fetch.assert_not_called()

    def test_wait_for_rate_limit_no_cooldown_returns_immediately(self):
        """_wait_for_rate_limit returns True immediately without sleeping when no cooldown is active."""
        rate_limit_until = multiprocessing.Value('d', 0.0)
        watcher = self._watcher(rate_limit_until=rate_limit_until, subject=False)
        watcher._sleep = MagicMock()

        with patch('inb4404.thread_watcher.time.time', return_value=1000.0):
            res = watcher._wait_for_rate_limit()

        self.assertTrue(res)
        watcher._sleep.assert_not_called()


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
