"""Process manager for multi-thread file watching."""
import os
import time
import logging
import sys
import threading
import multiprocessing
from multiprocessing import Process
from typing import Dict, Set, Optional, Any

from .config import Config, EXITCODE_MAINTENANCE
from .thread_watcher import ThreadWatcher
from .thread_parser import ThreadURL
from .http_client import HTTPClient, is_maintenance_message
from .exceptions import ThreadNotFoundError, MaintenanceError, HTTPError

log = logging.getLogger('inb4404')


def _call_watcher(thread_url: str, config: Config, workpath: str, stop_event: Optional[Any] = None) -> None:
    """Helper wrapper used when spawning a multiprocessing.Process.

    The Process target should be a picklable callable; this thin wrapper lets
    the child process run ThreadWatcher.watch() and simply ignores
    KeyboardInterrupt so cleanup can proceed gracefully in the parent.
    Ensure logging is configured in spawned child processes so that
    per-file download messages are emitted to the console when the
    script is run in "file of links" mode (multiprocessing spawn
    on Windows doesn't inherit the parent's basicConfig). Use the
    same date format selection as the main process.

    Args:
        thread_url: The URL of the thread to watch.
        config: Configuration settings.
        workpath: Base working directory path.
        stop_event: Optional multiprocessing Event to signal shutdown.
    """
    try:
        # Configure logging for child process
        if config.date:
            logging.basicConfig(
                level=logging.INFO,
                format='[%(asctime)s] %(message)s',
                datefmt='%Y-%m-%d %I:%M:%S %p'
            )
        else:
            logging.basicConfig(
                level=logging.INFO,
                format='[%(asctime)s] %(message)s',
                datefmt='%I:%M:%S %p'
            )
    except Exception:
        # If logging config fails for any reason, continue anyway
        pass

    try:
        watcher = ThreadWatcher(
            thread_url, config, workpath, stop_event=stop_event, raise_on_maintenance=True
        )
        watcher.watch()
    except ValueError as e:
        log.error(f"Error starting watcher for {thread_url}: {e}")
        raise SystemExit(1)
    except MaintenanceError as e:
        log.warning(f"Server maintenance detected for {thread_url}: {e}")
        raise SystemExit(EXITCODE_MAINTENANCE)
    except KeyboardInterrupt:
        pass
    except SystemExit:
        # Re-raise SystemExit to preserve exit code (e.g., 404, 503)
        raise


class ProcessManager:
    """Manages multiple watcher processes for threads listed in a file."""

    def __init__(self, filename: str, config: Config, workpath: str):
        """Initialize the ProcessManager.

        Args:
            filename: Path to the file containing thread URLs.
            config: Configuration settings.
            workpath: Base working directory path.
        """
        self.filename = filename
        self.config = config
        self.workpath = workpath
        self.running_processes: Dict[str, Process] = {}
        self._force_reload = threading.Event()
        self.stop_event = multiprocessing.Event()
        self.http_client = HTTPClient(stop_event=self.stop_event)
        self._stop_input_thread = False

    def load_queue(self) -> Set[str]:
        """Load thread URLs from the queue file.

        Returns:
            A set of valid thread URLs (lines/tokens starting with 'http' and not disabled).
        """
        try:
            desired_links = set()
            with open(self.filename, 'r', encoding='utf-8') as f:
                for line in f:
                    stripped = line.strip()
                    # Skip empty lines and whole-line comments / disabled lines
                    if not stripped or stripped.startswith('-') or stripped.startswith('#'):
                        continue

                    parts = stripped.split()
                    for part in parts:
                        # Skip tokens commented with # or disabled with - or -http
                        if part.startswith('#') or part.startswith('-'):
                            continue
                        if part.startswith('http'):
                            desired_links.add(part)
            return desired_links
        except FileNotFoundError:
            log.error(f'File not found: {self.filename}')
            return set()

    def start_watcher(self, link: str) -> None:
        """Start a watcher process for a thread URL.

        Args:
            link: The thread URL to watch.
        """
        if link in self.running_processes:
            if self.config.verbose:
                log.info(f'Already watching {link}')
            return

        log.info(f'Starting new watcher for {link}')
        process = Process(
            target=_call_watcher,
            args=(link, self.config, self.workpath, self.stop_event)
        )
        process.start()
        self.running_processes[link] = process

    def stop_watcher(self, link: str) -> None:
        """Stop a watcher process for a thread URL.

        Args:
            link: The thread URL to stop watching.
        """
        if link not in self.running_processes:
            return

        log.info(f'Link {link} removed from file. Stopping watcher.')
        process = self.running_processes[link]
        process.terminate()
        process.join(timeout=5)  # Give it a moment to die
        del self.running_processes[link]

    def check_dead_processes(self) -> Set[str]:
        """Check for dead processes and handle them appropriately."""
        dead_links = []
        for link, process in list(self.running_processes.items()):
            if not process.is_alive():
                dead_links.append(link)

        if not dead_links:
            return set()

        disabled_links: Set[str] = set()
        max_restarts = 3
        for link in dead_links:
            if self._handle_dead_process(link, max_restarts):
                disabled_links.add(link)
        
        return disabled_links

    def _is_exitcode_404(self, exitcode: Optional[int]) -> bool:
        """Check if an exit code indicates a 404 (handling POSIX 8-bit wrap)."""
        if exitcode is None:
            return False
        # Windows: 404; POSIX (Linux/macOS): 404 % 256 = 148
        return exitcode in (404, 404 % 256)

    def _is_exitcode_maintenance(self, exitcode: Optional[int]) -> bool:
        """Check if an exit code indicates server maintenance (handling POSIX 8-bit wrap)."""
        if exitcode is None:
            return False
        # Windows: 503 / 429; POSIX: 503 % 256 = 247, 429 % 256 = 173
        return exitcode in (EXITCODE_MAINTENANCE, EXITCODE_MAINTENANCE % 256, 429, 429 % 256)

    def _handle_maintenance_mode(self, probe_link: str) -> None:
        """Handle server maintenance by stopping all watchers and monitoring with a single probe.

        Args:
            probe_link: The URL to probe for checking if maintenance has ended.
        """
        log.warning(
            "Server is performing maintenance ('Performing maintenance. We'll be back soon.'). "
            "Stopping all other watchers..."
        )

        # Stop all running watcher processes
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
                f"Maintenance in progress. Single watcher checking {probe_link} in {wait_minutes:.1f} minutes "
                f"(attempt {attempt})..."
            )

            # Wait with periodic check of stop_event
            start_wait = time.time()
            while time.time() - start_wait < wait_seconds:
                if self.stop_event and self.stop_event.is_set():
                    return
                time.sleep(1)

            if self.stop_event and self.stop_event.is_set():
                return

            log.info(f"Checking if maintenance is over (attempt {attempt})...")
            is_still_maintenance = False
            try:
                parsed = ThreadURL.parse(probe_link)
                # Try fetching thread API or canonical URL
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
                # 404 means the server is back online and responding normally!
                is_still_maintenance = False
            except Exception as e:
                if is_maintenance_message(str(e)):
                    is_still_maintenance = True

            if not is_still_maintenance:
                log.info("Server maintenance is over! Resuming all watchers.")
                break
            else:
                log.warning("Server is still in maintenance: Performing maintenance. We'll be back soon.")

    def _handle_dead_process(self, link: str, max_restarts: int) -> bool:
        """Handle a dead process - check exit code, probe, and restart if needed.

        Args:
            link: The thread URL of the dead process.
            max_restarts: Maximum number of restart attempts.
        
        Returns:
            True if the link was disabled, False otherwise.
        """
        proc = self.running_processes.get(link)
        exitcode = None
        if proc:
            try:
                exitcode = getattr(proc, 'exitcode', None)
            except Exception:
                pass

        # If exit code indicates maintenance, enter maintenance mode
        if self._is_exitcode_maintenance(exitcode):
            log.warning(f"Watcher for {link} exited due to server maintenance.")
            self.running_processes.pop(link, None)
            self._handle_maintenance_mode(link)
            return False

        # If exit code is 404, immediately disable
        if self._is_exitcode_404(exitcode):
            self._disable_link(link, reason='404')
            self.running_processes.pop(link, None)
            return True

        # Quick probe: try to load the thread page/API to detect 404s or maintenance
        is_404 = False
        is_maintenance = False
        try:
            parsed = ThreadURL.parse(link)
            # Try 4chan JSON API first
            api_res = self.http_client.fetch_thread_api(parsed.board, parsed.thread_id)
            if api_res is not None:
                is_404 = False
            else:
                # If API returned None or failed, verify with canonical HTML without slug
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
            # Fallback probe with raw link
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

        # Not a 404. Try restarting the watcher
        restarted = False
        for attempt in range(1, max_restarts + 1):
            try:
                old_proc = self.running_processes.pop(link, None)
                if old_proc is not None:
                    try:
                        old_proc.join(timeout=1)
                    except Exception:
                        pass

                new_proc = Process(
                    target=_call_watcher,
                    args=(link, self.config, self.workpath, self.stop_event)
                )
                new_proc.start()
                time.sleep(1)  # Give it a moment to start
                if new_proc.is_alive():
                    self.running_processes[link] = new_proc
                    restarted = True
                    if self.config.verbose:
                        log.info(f'Restarted watcher for {link} (attempt {attempt})')
                    break
                else:
                    # Process died immediately; try again after backoff
                    try:
                        new_proc.join(timeout=1)
                    except Exception:
                        pass
            except Exception as e:
                log.warning(f'Attempt {attempt} to restart watcher for {link} failed: {e}')
            time.sleep(5 * attempt)

        if not restarted:
            log.warning(f'Watcher for {link} stopped and could not be restarted. Leaving in queue.')
            self.running_processes.pop(link, None)
            return False
        
        return False

    def _disable_link(self, link: str, reason: str) -> None:
        """Disable a link in the queue file by prefixing with '-'.

        Disables ALL occurrences of the thread in the queue file.

        Args:
            link: The thread URL to disable.
            reason: Reason for disabling (for logging).
        """
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

                # If line is already disabled or whole-line comment, keep it as is
                if line.lstrip().startswith('-') or line.lstrip().startswith('#'):
                    new_lines.append(line)
                    continue

                # Check if this line contains our target link
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
                    # If it was a simple single-URL line, preserve indentation
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

    def _input_listener(self) -> None:
        """Background thread to listen for new URLs from stdin."""
        while not self._stop_input_thread:
            try:
                if sys.platform == 'win32':
                    import msvcrt
                    # On Windows, readline() is blocking and can prevent Ctrl+C
                    # from working if no input is present. We check kbhit() first.
                    if not msvcrt.kbhit():
                        time.sleep(0.1)
                        continue

                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                
                # Split line into tokens to handle multiple URLs pasted at once
                tokens = line.split()
                new_urls = []
                for token in tokens:
                    if token.startswith('http'):
                        new_urls.append(token)
                    elif token:
                        log.warning(f'Ignored invalid input token: {token}')

                if new_urls:
                    log.info(f'New URL(s) detected from input: {", ".join(new_urls)}')
                    # Append to file
                    try:
                        with open(self.filename, 'a', encoding='utf-8') as f:
                            for url in new_urls:
                                f.write(f'\n{url}')
                        # Trigger reload
                        self._force_reload.set()
                    except IOError as e:
                        log.error(f'Failed to append URL(s) to {self.filename}: {e}')
            except ValueError:
                # Can happen if stdin is closed
                break
            except Exception as e:
                log.error(f'Error reading input: {e}')
                break

    def run(self) -> None:
        """Main run loop - manages watcher processes."""
        # Start input listener thread
        input_thread = threading.Thread(target=self._input_listener, daemon=True)
        input_thread.start()
        
        # Log instruction for user
        log.info("Listening for new URLs. Paste a link and press Enter to add it.")

        try:
            while True:
                # Clear the force reload event at start of loop
                self._force_reload.clear()

                # Load queue
                desired_links = self.load_queue()

                if self.config.verbose:
                    log.info(
                        f'Loaded {len(desired_links)} links from {self.filename}; '
                        f'{len(self.running_processes)} watchers currently running.'
                    )
                    # Show which links are new vs already running
                    current = set(self.running_processes.keys())
                    new_links = desired_links - current
                    removed = current - desired_links
                    if new_links:
                        log.info('New links to start: ' + ', '.join(sorted(new_links)))
                    if removed:
                        log.info('Links present but not in file: ' + ', '.join(sorted(removed)))

                if not desired_links and not self.running_processes:
                    log.warning(f'{self.filename} is empty or all links are disabled.')

                # Check for dead processes
                disabled_in_run = self.check_dead_processes()
                desired_links.difference_update(disabled_in_run)

                # Start new processes for new links
                for link in desired_links:
                    if link not in self.running_processes:
                        self.start_watcher(link)
                        
                # Stop processes for links that have been removed from the file
                removed_links = [
                    link for link in self.running_processes
                    if link not in desired_links
                ]
                for link in removed_links:
                    self.stop_watcher(link)

                if not self.config.reload:
                    # Run until all processes have completed
                    while True:
                        if self._force_reload.is_set():
                            break
                        
                        # Continuously process dead/404'd processes
                        self.check_dead_processes()

                        alive_count = sum(1 for p in self.running_processes.values() if p.is_alive())
                        if alive_count == 0:
                            # Final pass to handle any remaining processes
                            self.check_dead_processes()
                            break
                        
                        time.sleep(1)
                    
                    if not self._force_reload.is_set():
                        break
                        
                else:
                    # If reloading is enabled:
                    if self.config.verbose:
                        log.info(
                            f'Reloading {self.filename} in {self.config.reload_time} minutes. '
                            f'Watching {len(self.running_processes)} threads.'
                        )
                    
                    # Wait for reload time while periodically checking dead processes
                    start_wait = time.time()
                    wait_seconds = 60 * self.config.reload_time
                    while time.time() - start_wait < wait_seconds:
                        if self._force_reload.is_set():
                            break
                        self.check_dead_processes()
                        time.sleep(1)

        except KeyboardInterrupt:
            self._stop_input_thread = True
            log.info('Ctrl+C detected. Shutting down all watcher processes...')
            self.stop_event.set()
            
            # Check dead processes before stopping
            self.check_dead_processes()

            # Wait for processes to exit gracefully
            for link, process in list(self.running_processes.items()):
                process.join(timeout=0.5)
            
            still_running = [(l, p) for l, p in self.running_processes.items() if p.is_alive()]
            if still_running:
                log.info(f'Waiting for {len(still_running)} processes to finish...')
                for link, process in still_running:
                    process.join(timeout=5)
            
            # Finally, terminate any stubborn processes
            for link, process in self.running_processes.items():
                if process.is_alive():
                    log.warning(f'Process for {link} did not exit gracefully. Terminating.')
                    process.terminate()
            
            log.info('All watchers have been shut down.')

