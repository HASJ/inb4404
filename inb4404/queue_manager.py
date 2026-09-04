"""Consolidated queue manager for single-process, rate-paced multi-thread watching."""
import os
import sys
import time
import queue
import logging
import threading
import multiprocessing
from dataclasses import dataclass
from typing import Dict, Set, Optional, Any, List, Tuple

from .config import Config, DEFAULT_RATE_LIMIT_WAIT, EXITCODE_MAINTENANCE
from .thread_watcher import ThreadWatcher
from .thread_parser import ThreadURL
from .http_client import HTTPClient, is_maintenance_message
from .exceptions import ThreadNotFoundError, MaintenanceError, HTTPError

log = logging.getLogger('inb4404')


class RatePacer:
    """Thread-safe rate pacer enforcing a minimum interval between requests."""

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._last_request_time = 0.0
        self._lock = threading.Lock()

    def wait(self, stop_event: Optional[Any] = None) -> bool:
        """Wait until min_interval has elapsed since the previous request.

        Args:
            stop_event: Optional event to abort early on shutdown.

        Returns:
            False if stop_event was triggered, True otherwise.
        """
        with self._lock:
            now = time.time()
            elapsed = now - self._last_request_time
            remaining = self.min_interval - elapsed
            if remaining > 0:
                if stop_event:
                    if stop_event.wait(remaining):
                        return False
                else:
                    time.sleep(remaining)
            self._last_request_time = time.time()
            return True


@dataclass
class DownloadTask:
    """Represents a media file download job enqueued by the scheduler."""
    watcher: Any
    link: str
    img: Optional[str]
    img_path: str
    api_md5_hex: Optional[str]
    api_md5_b64: Optional[str]
    original_name: Optional[str]
    tim: Optional[Any]
    ext: Optional[str]
    total: int
    count: int
    enum_tuple: Tuple


class DownloadWorker(threading.Thread):
    """Worker thread that consumes and processes media download tasks."""

    def __init__(
        self,
        task_queue: queue.Queue,
        stop_event: Any,
        maintenance_active: threading.Event,
        name: str = "DownloadWorker"
    ):
        super().__init__(name=name, daemon=True)
        self.task_queue = task_queue
        self.stop_event = stop_event
        self.maintenance_active = maintenance_active

    def run(self) -> None:
        """Process download tasks continuously until shutdown."""
        while not (self.stop_event and self.stop_event.is_set()):
            # Wait if maintenance mode is active
            if not self.maintenance_active.is_set():
                time.sleep(0.5)
                continue

            try:
                task: DownloadTask = self.task_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                # If maintenance activated while waiting on queue, pause
                if not self.maintenance_active.is_set():
                    self.task_queue.put(task)
                    time.sleep(0.5)
                    continue

                if self.stop_event and self.stop_event.is_set():
                    break

                task.watcher.execute_download_task(task)
            except MaintenanceError as e:
                log.warning(f"Maintenance detected during download of {task.link}: {e}")
            except Exception as e:
                log.warning(f"Unexpected error downloading {task.link}: {e}")
            finally:
                if hasattr(task, 'watcher') and hasattr(task.watcher, 'decrement_pending_tasks'):
                    task.watcher.decrement_pending_tasks()
                self.task_queue.task_done()



class QueueManager:
    """Manages thread monitoring and downloading in a single consolidated process."""

    def __init__(self, filename: str, config: Config, workpath: str):
        self.filename = filename
        self.config = config
        self.workpath = workpath

        self.watchers: Dict[str, ThreadWatcher] = {}
        self.poll_schedule: Dict[str, float] = {}
        self.download_queue: queue.Queue = queue.Queue()
        self.workers: List[DownloadWorker] = []

        self.api_pacer = RatePacer(min_interval=config.api_interval)
        self.stop_event = multiprocessing.Event()
        self.maintenance_active = threading.Event()
        self.maintenance_active.set()  # set = running normally
        self.rate_limit_until = multiprocessing.Value('d', 0.0)

        self._force_reload = threading.Event()
        self.http_client = HTTPClient(stop_event=self.stop_event)
        self._stop_input_thread = False

        # Legacy compatibility attribute
        self.running_processes: Dict[str, Any] = {}

    def _wait_for_rate_limit(self) -> bool:
        """Wait until active shared rate-limit cooldown expires."""
        while not (self.stop_event and self.stop_event.is_set()):
            remaining = self.rate_limit_until.value - time.time()
            if remaining <= 0:
                return True
            log.warning(
                f"Rate limit cooldown active; waiting {remaining / 60.0:.1f} minutes."
            )
            if self.stop_event.wait(min(remaining, 5.0)):
                return False
        return False

    def _start_rate_limit_cooldown(self) -> None:
        """Start the rate-limit cooldown deadline."""
        now = time.time()
        deadline = now + DEFAULT_RATE_LIMIT_WAIT
        with self.rate_limit_until.get_lock():
            if self.rate_limit_until.value <= now:
                self.rate_limit_until.value = deadline

    def load_queue(self) -> Set[str]:
        """Load valid thread URLs from the queue file."""
        try:
            desired_links = set()
            with open(self.filename, 'r', encoding='utf-8') as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith('-') or stripped.startswith('#'):
                        continue
                    parts = stripped.split()
                    for part in parts:
                        if part.startswith('#') or part.startswith('-'):
                            continue
                        if part.startswith('http'):
                            desired_links.add(part)
            return desired_links
        except FileNotFoundError:
            log.error(f'File not found: {self.filename}')
            return set()

    def start_watcher(self, link: str) -> Optional[ThreadWatcher]:
        """Register a thread URL into the watcher pool."""
        if link in self.watchers:
            if self.config.verbose:
                log.info(f'Already watching {link}')
            return self.watchers[link]

        log.info(f'Adding thread to queue: {link}')
        try:
            watcher = ThreadWatcher(
                link,
                self.config,
                self.workpath,
                stop_event=self.stop_event,
                raise_on_maintenance=True,
                rate_limit_until=self.rate_limit_until,
            )
            watcher._load_existing_hashes()
            watcher._scan_directory()

            self.watchers[link] = watcher
            # Stagger initial poll so all threads do not hit API concurrently
            stagger_offset = len(self.watchers) * self.config.api_interval
            self.poll_schedule[link] = time.time() + stagger_offset
            return watcher
        except Exception as e:
            log.error(f"Error starting watcher for {link}: {e}")
            return None

    def stop_watcher(self, link: str) -> None:
        """Remove a thread from the watch pool."""
        if link in self.watchers:
            log.info(f'Stopping watcher for {link}')
            self.watchers.pop(link, None)
            self.poll_schedule.pop(link, None)

    def _disable_link(self, link: str, reason: str) -> None:
        """Disable all occurrences of a link in the queue file by prefixing with '-'."""
        try:
            target_board = None
            target_thread_id = None
            try:
                parsed = ThreadURL.parse(link)
                target_board = parsed.board
                target_thread_id = parsed.thread_id
            except Exception:
                pass

            with open(self.filename, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            modified = False
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    new_lines.append(line)
                    continue

                if line.lstrip().startswith('-') or line.lstrip().startswith('#'):
                    new_lines.append(line)
                    continue

                parts = line.split()
                line_has_match = False
                new_parts = []
                for part in parts:
                    is_match = False
                    if part.startswith('-') or part.startswith('#'):
                        new_parts.append(part)
                        continue

                    if part == link:
                        is_match = True
                    elif target_board and target_thread_id:
                        try:
                            p_url = ThreadURL.parse(part)
                            if p_url.board == target_board and p_url.thread_id == target_thread_id:
                                is_match = True
                        except Exception:
                            pass

                    if is_match:
                        new_parts.append('-' + part)
                        line_has_match = True
                        modified = True
                    else:
                        new_parts.append(part)

                if line_has_match:
                    if len(parts) == 1 and stripped == parts[0]:
                        indent = line[:len(line) - len(line.lstrip())]
                        trailing_nl = '\n' if line.endswith('\n') else ''
                        new_lines.append(f"{indent}-{parts[0]}{trailing_nl}")
                    else:
                        trailing_nl = '\n' if line.endswith('\n') else ''
                        new_lines.append(' '.join(new_parts) + trailing_nl)
                else:
                    new_lines.append(line)

            if modified:
                with open(self.filename, 'w', encoding='utf-8') as f:
                    f.writelines(new_lines)
                log.info(f'Disabled {link} in {self.filename} ({reason})')
            else:
                log.info(f"Link '{link}' is already disabled or not found in {self.filename}.")

        except IOError as e:
            log.error(f'Error writing to file {self.filename}: {e}')

    def _is_exitcode_404(self, exitcode: Optional[int]) -> bool:
        """Check if an exit code indicates a 404 (handling POSIX wrap)."""
        if exitcode is None:
            return False
        return exitcode in (404, 404 % 256)

    def _is_exitcode_maintenance(self, exitcode: Optional[int]) -> bool:
        """Check if an exit code indicates maintenance (handling POSIX wrap)."""
        if exitcode is None:
            return False
        return exitcode in (EXITCODE_MAINTENANCE, EXITCODE_MAINTENANCE % 256, 429, 429 % 256)

    def _handle_maintenance_mode(self, probe_link: str) -> None:
        """Handle maintenance mode by pausing workers and probing until recovery."""
        log.warning(
            "Server is performing maintenance ('Performing maintenance. We'll be back soon.'). "
            "Pausing scheduler and downloads..."
        )
        self.maintenance_active.clear()

        # Compatibility: clear running_processes if mocked in tests
        for link, process in list(self.running_processes.items()):
            try:
                process.terminate()
                process.join(timeout=2)
            except Exception:
                pass
        self.running_processes.clear()

        attempt = 0
        while not (self.stop_event and self.stop_event.is_set()):
            attempt += 1
            wait_seconds = min(
                self.config.maintenance_initial_wait + (attempt - 1) * self.config.maintenance_wait_increment,
                self.config.maintenance_max_wait
            )
            wait_minutes = wait_seconds / 60.0
            log.warning(
                f"Maintenance in progress. Checking {probe_link} in {wait_minutes:.1f} minutes "
                f"(attempt {attempt})..."
            )

            start_wait = time.time()
            while time.time() - start_wait < wait_seconds:
                if self.stop_event and self.stop_event.is_set():
                    return
                time.sleep(min(1.0, wait_seconds))

            if self.stop_event and self.stop_event.is_set():
                return

            log.info(f"Checking if maintenance is over (attempt {attempt})...")
            is_still_maintenance = False
            try:
                parsed = ThreadURL.parse(probe_link)
                api_res = self.http_client.fetch_thread_api(parsed.board, parsed.thread_id)
                if api_res is None:
                    self.http_client.fetch(parsed.canonical_url)
            except MaintenanceError:
                is_still_maintenance = True
            except HTTPError as e:
                if hasattr(e, 'code') and e.code == 429:
                    is_still_maintenance = True
                elif is_maintenance_message(str(e)):
                    is_still_maintenance = True
            except ThreadNotFoundError:
                # 404 indicates the server is back online!
                is_still_maintenance = False
            except Exception as e:
                if is_maintenance_message(str(e)):
                    is_still_maintenance = True

            if not is_still_maintenance:
                log.info("Server maintenance is over! Resuming scheduler and downloads.")
                # Reschedule watchers evenly
                now = time.time()
                for i, link in enumerate(self.watchers.keys()):
                    self.poll_schedule[link] = now + (i * self.config.api_interval)
                self.maintenance_active.set()
                break
            else:
                log.warning("Server is still in maintenance: Performing maintenance. We'll be back soon.")

    def _handle_dead_process(self, link: str, max_restarts: int) -> bool:
        """Compatibility helper for existing dead process handling tests."""
        proc = self.running_processes.get(link)
        exitcode = None
        if proc:
            try:
                exitcode = getattr(proc, 'exitcode', None)
            except Exception:
                pass

        if self._is_exitcode_maintenance(exitcode):
            log.warning(f"Watcher for {link} exited due to server maintenance.")
            self.running_processes.pop(link, None)
            self._handle_maintenance_mode(link)
            return False

        if self._is_exitcode_404(exitcode):
            self._disable_link(link, reason='404')
            self.running_processes.pop(link, None)
            return True

        # Quick probe
        is_404 = False
        is_maintenance = False
        try:
            parsed = ThreadURL.parse(link)
            api_res = self.http_client.fetch_thread_api(parsed.board, parsed.thread_id)
            if api_res is not None:
                is_404 = False
            else:
                try:
                    self.http_client.fetch(parsed.canonical_url)
                except ThreadNotFoundError:
                    is_404 = True
                except MaintenanceError:
                    is_maintenance = True
                except Exception:
                    pass
        except MaintenanceError:
            is_maintenance = True
        except ThreadNotFoundError:
            is_404 = True
        except Exception:
            try:
                self.http_client.fetch(link)
            except MaintenanceError:
                is_maintenance = True
            except ThreadNotFoundError:
                is_404 = True
            except Exception:
                pass

        if is_maintenance:
            log.warning(f"Server maintenance detected during probe for {link}.")
            self.running_processes.pop(link, None)
            self._handle_maintenance_mode(link)
            return False

        if is_404:
            self._disable_link(link, reason='404')
            self.running_processes.pop(link, None)
            return True

        from . import process_manager
        restarted = False
        for attempt in range(1, max_restarts + 1):
            try:
                old_proc = self.running_processes.pop(link, None)
                if old_proc is not None:
                    try:
                        old_proc.join(timeout=1)
                    except Exception:
                        pass

                new_proc = process_manager.Process(
                    target=_call_watcher,
                    args=(link, self.config, self.workpath, self.stop_event, self.rate_limit_until)
                )
                new_proc.start()
                time.sleep(1)
                if new_proc.is_alive():
                    self.running_processes[link] = new_proc
                    restarted = True
                    break
                else:
                    try:
                        new_proc.join(timeout=1)
                    except Exception:
                        pass
            except Exception as e:
                log.warning(f'Attempt {attempt} to restart watcher for {link} failed: {e}')
            time.sleep(5 * attempt)

        if not restarted:
            self.running_processes.pop(link, None)
            return False

        return False

    def _input_listener(self) -> None:
        """Background thread to listen for new URLs from stdin."""
        while not self._stop_input_thread:
            try:
                if sys.platform == 'win32':
                    import msvcrt
                    if not msvcrt.kbhit():
                        time.sleep(0.1)
                        continue

                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()

                tokens = line.split()
                new_urls = []
                for token in tokens:
                    if token.startswith('http'):
                        new_urls.append(token)
                    elif token:
                        log.warning(f'Ignored invalid input token: {token}')

                if new_urls:
                    log.info(f'New URL(s) detected from input: {", ".join(new_urls)}')
                    try:
                        with open(self.filename, 'a', encoding='utf-8') as f:
                            for url in new_urls:
                                f.write(f'\n{url}')
                        self._force_reload.set()
                    except IOError as e:
                        log.error(f'Failed to append URL(s) to {self.filename}: {e}')
            except ValueError:
                break
            except Exception as e:
                log.error(f'Error reading input: {e}')
                break

    def poll_thread(self, link: str, watcher: ThreadWatcher) -> None:
        """Perform a paced poll of a single thread, enqueuing new downloads."""
        if not self.api_pacer.wait(self.stop_event):
            return

        if not self._wait_for_rate_limit():
            return

        try:
            items, all_titles = watcher._fetch_thread_data()
            total = len(items)
            new_tasks = 0
            for enum_index, enum_tuple in enumerate(items):
                if self.stop_event.is_set():
                    break
                task = watcher.prepare_download_task(
                    enum_tuple,
                    enum_index,
                    all_titles,
                    total,
                    enum_index + 1
                )
                if task is not None:
                    if hasattr(watcher, 'increment_pending_tasks'):
                        watcher.increment_pending_tasks(1)
                    self.download_queue.put(task)
                    new_tasks += 1

            if hasattr(watcher, 'has_completed_cycle'):
                watcher.has_completed_cycle = True
            if hasattr(watcher, 'last_item_count'):
                watcher.last_item_count = total

            # Check if archived and all files downloaded/skipped
            is_archived = getattr(watcher, 'is_archived', False)
            if not is_archived and hasattr(watcher, 'check_if_archived'):
                is_archived = watcher.check_if_archived()

            pending = getattr(watcher, 'pending_tasks', 0)
            if is_archived:
                if new_tasks == 0 and pending == 0:
                    log.info(f"Thread {link} is archived and all files downloaded/skipped. Disabling.")
                    self._disable_link(link, reason='archived')
                    self.stop_watcher(link)
                    return
                else:
                    # Still downloading pending files; check back soon
                    self.poll_schedule[link] = time.time() + min(5.0, self.config.refresh_time)
                    return

            self.poll_schedule[link] = time.time() + self.config.refresh_time

        except ThreadNotFoundError:
            log.info(f"Thread {link} not found (404). Disabling.")
            self._disable_link(link, reason='404')
            self.stop_watcher(link)
        except MaintenanceError as e:
            log.warning(f"Server maintenance detected for {link}: {e}")
            self._handle_maintenance_mode(link)
        except HTTPError as ex:
            if hasattr(ex, 'code') and ex.code == 429:
                log.info(f"{link} 429'd during poll")
                self._start_rate_limit_cooldown()
                self.poll_schedule[link] = time.time() + 10 + self.config.throttle
            else:
                is_archived = getattr(watcher, 'is_archived', False)
                if not is_archived and hasattr(watcher, 'check_if_archived'):
                    is_archived = watcher.check_if_archived()
                completed = getattr(watcher, 'has_completed_cycle', False)
                pending = getattr(watcher, 'pending_tasks', 0)
                if is_archived and completed and pending == 0:
                    log.info(
                        f"Thread {link} is archived and all files downloaded/skipped "
                        f"(handled error: {ex}). Disabling."
                    )
                    self._disable_link(link, reason='archived')
                    self.stop_watcher(link)
                    return

                log.warning(f"Temporary error fetching {link}: {ex}")
                self.poll_schedule[link] = time.time() + 10
        except Exception as e:
            is_archived = getattr(watcher, 'is_archived', False)
            if not is_archived and hasattr(watcher, 'check_if_archived'):
                is_archived = watcher.check_if_archived()
            completed = getattr(watcher, 'has_completed_cycle', False)
            pending = getattr(watcher, 'pending_tasks', 0)
            if is_archived and completed and pending == 0:
                log.info(
                    f"Thread {link} is archived and all files downloaded/skipped "
                    f"(handled error: {e}). Disabling."
                )
                self._disable_link(link, reason='archived')
                self.stop_watcher(link)
                return

            log.warning(f"Unexpected error watching {link}: {e}")
            self.poll_schedule[link] = time.time() + 10


    def run(self) -> None:
        """Main scheduler loop - single process managing all threads and downloads."""
        input_thread = threading.Thread(target=self._input_listener, daemon=True)
        input_thread.start()

        num_workers = max(1, self.config.max_download_workers)
        for i in range(num_workers):
            worker = DownloadWorker(
                self.download_queue,
                self.stop_event,
                self.maintenance_active,
                name=f"DownloadWorker-{i + 1}"
            )
            worker.start()
            self.workers.append(worker)

        log.info(f"Started consolidated queue manager with {num_workers} download worker(s).")
        log.info("Listening for new URLs. Paste a link and press Enter to add it.")

        last_reload_time = time.time()
        reload_interval_seconds = 60 * self.config.reload_time

        try:
            while not self.stop_event.is_set():
                now = time.time()

                # Check if queue reload needed
                should_reload = self._force_reload.is_set()
                if self.config.reload and (now - last_reload_time >= reload_interval_seconds):
                    should_reload = True

                if should_reload or not self.watchers:
                    self._force_reload.clear()
                    last_reload_time = now
                    desired_links = self.load_queue()

                    current_links = set(self.watchers.keys())
                    new_links = desired_links - current_links
                    removed_links = current_links - desired_links

                    for link in new_links:
                        self.start_watcher(link)
                    for link in removed_links:
                        self.stop_watcher(link)

                    if self.config.verbose:
                        log.info(
                            f'Queue loaded {len(desired_links)} links; '
                            f'{len(self.watchers)} active watchers.'
                        )

                if not self.watchers:
                    if not self.config.reload and not self._force_reload.is_set():
                        log.info("All threads completed or 404'd. Exiting.")
                        break
                    time.sleep(1)
                    continue

                # Find earliest scheduled thread
                due_links = [
                    (l, sched) for l, sched in self.poll_schedule.items()
                    if sched <= time.time() and l in self.watchers
                ]

                if due_links:
                    # Poll the oldest due thread
                    due_links.sort(key=lambda item: item[1])
                    link_to_poll = due_links[0][0]
                    watcher = self.watchers.get(link_to_poll)
                    if watcher:
                        self.poll_thread(link_to_poll, watcher)
                else:
                    # Sleep briefly until next thread is due or reload triggered
                    earliest_sched = min(self.poll_schedule.values()) if self.poll_schedule else time.time() + 1
                    sleep_dur = max(0.1, min(1.0, earliest_sched - time.time()))
                    time.sleep(sleep_dur)

        except KeyboardInterrupt:
            self._stop_input_thread = True
            log.info("Ctrl+C detected. Shutting down queue manager...")
            self.stop_event.set()
            for worker in self.workers:
                worker.join(timeout=2.0)
            log.info("All download workers and watchers have been shut down.")


def _call_watcher(
    thread_url: str,
    config: Config,
    workpath: str,
    stop_event: Optional[Any] = None,
    rate_limit_until: Optional[Any] = None,
) -> None:
    """Compatibility callable for single-process wrapper or fallback."""
    try:
        watcher = ThreadWatcher(
            thread_url,
            config,
            workpath,
            stop_event=stop_event,
            raise_on_maintenance=True,
            rate_limit_until=rate_limit_until,
        )
        watcher.watch()
    except (ThreadNotFoundError, MaintenanceError):
        raise
    except KeyboardInterrupt:
        pass
