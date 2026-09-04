"""Tests for the consolidated single-process queue manager, rate pacer, and download worker."""
import os
import time
import queue
import shutil
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from inb4404.config import Config
from inb4404.exceptions import ThreadNotFoundError, MaintenanceError, HTTPError
from inb4404.queue_manager import (
    RatePacer,
    DownloadTask,
    DownloadWorker,
    QueueManager,
)


class TestRatePacer(unittest.TestCase):
    """Test the thread-safe API rate pacer."""

    def test_rate_pacer_enforces_interval(self):
        pacer = RatePacer(min_interval=0.1)
        stop_event = threading.Event()

        start = time.time()
        pacer.wait(stop_event)
        pacer.wait(stop_event)
        elapsed = time.time() - start

        self.assertGreaterEqual(elapsed, 0.08)

    def test_rate_pacer_aborts_on_stop_event(self):
        pacer = RatePacer(min_interval=5.0)
        pacer._last_request_time = time.time()  # just requested
        stop_event = threading.Event()
        stop_event.set()

        res = pacer.wait(stop_event)
        self.assertFalse(res)


class TestDownloadWorker(unittest.TestCase):
    """Test the media download worker thread."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.config = Config(workpath=self.tmp_dir, phash_enabled=False)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_worker_consumes_and_executes_task(self):
        task_queue = queue.Queue()
        stop_event = threading.Event()
        maint_event = threading.Event()
        maint_event.set()

        worker = DownloadWorker(task_queue, stop_event, maint_event)
        worker.start()

        mock_watcher = MagicMock()
        task = DownloadTask(
            watcher=mock_watcher,
            link="https://i.4cdn.org/g/12345.jpg",
            img="12345.jpg",
            img_path=os.path.join(self.tmp_dir, "12345.jpg"),
            api_md5_hex="abc123",
            api_md5_b64=None,
            original_name="test",
            tim=12345,
            ext=".jpg",
            total=1,
            count=1,
            enum_tuple=(),
        )

        task_queue.put(task)
        task_queue.join()  # wait until task_done called

        stop_event.set()
        worker.join(timeout=2.0)

        mock_watcher.execute_download_task.assert_called_once_with(task)

    def test_worker_pauses_during_maintenance(self):
        task_queue = queue.Queue()
        stop_event = threading.Event()
        maint_event = threading.Event()
        maint_event.clear()  # Maintenance active!

        worker = DownloadWorker(task_queue, stop_event, maint_event)
        worker.start()

        mock_watcher = MagicMock()
        task = DownloadTask(
            watcher=mock_watcher,
            link="https://i.4cdn.org/g/12345.jpg",
            img="12345.jpg",
            img_path=os.path.join(self.tmp_dir, "12345.jpg"),
            api_md5_hex="abc123",
            api_md5_b64=None,
            original_name="test",
            tim=12345,
            ext=".jpg",
            total=1,
            count=1,
            enum_tuple=(),
        )
        task_queue.put(task)

        # Worker should NOT have processed the task yet while maintenance is cleared
        time.sleep(0.2)
        mock_watcher.execute_download_task.assert_not_called()

        # Resume maintenance
        maint_event.set()
        task_queue.join()

        stop_event.set()
        worker.join(timeout=2.0)

        mock_watcher.execute_download_task.assert_called_once_with(task)


class TestQueueManagerSingleProcess(unittest.TestCase):
    """Test QueueManager scheduling and multi-thread handling in a single process."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.queue_file = os.path.join(self.tmp_dir, 'queue.txt')
        self.config = Config(
            workpath=self.tmp_dir,
            api_interval=0.5,
            phash_enabled=False,
        )
        self.qm = QueueManager(self.queue_file, self.config, self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_queue(self, content: str):
        with open(self.queue_file, 'w', encoding='utf-8') as f:
            f.write(content)

    def test_staggered_poll_schedule_on_start_watcher(self):
        link1 = "https://boards.4chan.org/gif/thread/1000"
        link2 = "https://boards.4chan.org/gif/thread/2000"
        link3 = "https://boards.4chan.org/gif/thread/3000"

        with patch('inb4404.queue_manager.ThreadWatcher') as mock_watcher_cls:
            mock_watcher = MagicMock()
            mock_watcher_cls.return_value = mock_watcher

            with patch('inb4404.queue_manager.time.time', return_value=100.0):
                self.qm.start_watcher(link1)
                self.qm.start_watcher(link2)
                self.qm.start_watcher(link3)

        # link1 scheduled at 100 + 1 * 0.5 = 100.5
        # link2 scheduled at 100 + 2 * 0.5 = 101.0
        # link3 scheduled at 100 + 3 * 0.5 = 101.5
        self.assertEqual(self.qm.poll_schedule[link1], 100.5)
        self.assertEqual(self.qm.poll_schedule[link2], 101.0)
        self.assertEqual(self.qm.poll_schedule[link3], 101.5)

    def test_poll_thread_enqueues_download_tasks(self):
        link = "https://boards.4chan.org/gif/thread/1000"
        mock_watcher = MagicMock()
        mock_watcher._fetch_thread_data.return_value = (
            [("https://i.4cdn.org/gif/1.jpg", "1.jpg", "hash1")],
            []
        )
        task_obj = MagicMock()
        mock_watcher.prepare_download_task.return_value = task_obj

        self.qm.watchers[link] = mock_watcher
        self.qm.poll_schedule[link] = 0.0

        self.qm.poll_thread(link, mock_watcher)

        self.assertFalse(self.qm.download_queue.empty())
        task = self.qm.download_queue.get_nowait()
        self.assertEqual(task, task_obj)
        self.assertGreater(self.qm.poll_schedule[link], time.time())

    def test_poll_thread_404_disables_link(self):
        link = "https://boards.4chan.org/gif/thread/1000"
        self._write_queue(f"{link}\n")

        mock_watcher = MagicMock()
        mock_watcher._fetch_thread_data.side_effect = ThreadNotFoundError("404")
        self.qm.watchers[link] = mock_watcher
        self.qm.poll_schedule[link] = 0.0

        self.qm.poll_thread(link, mock_watcher)

        self.assertNotIn(link, self.qm.watchers)
        self.assertNotIn(link, self.qm.poll_schedule)
        with open(self.queue_file, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn(f"-{link}", content)


if __name__ == '__main__':
    unittest.main()

