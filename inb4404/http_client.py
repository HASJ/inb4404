"""HTTP client for fetching thread data and files."""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from .exceptions import HTTPError, ThreadNotFoundError

log = logging.getLogger('inb4404')


class HTTPClient:
    """Handles HTTP requests for thread data and file downloads."""

    USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15'

    def __init__(self, stop_event: Optional[Any] = None):
        """Initialize the HTTP client.

        Args:
            stop_event: Optional threading/multiprocessing Event to signal shutdown.
        """
        self.stop_event = stop_event

    def _sleep(self, seconds: float) -> None:
        """Sleep for specified seconds or until stop_event is set.

        Args:
            seconds: Time to sleep in seconds.
        """
        if self.stop_event:
            self.stop_event.wait(seconds)
        else:
            time.sleep(seconds)

    def _build_headers(self, url: str) -> Dict[str, str]:
        """Build HTTP headers for a request.

        Args:
            url: The URL to fetch.

        Returns:
            A dictionary of HTTP headers.
        """
        parsed = urllib.parse.urlparse(url)
        path_parts = parsed.path.strip('/').split('/')
        referer = f'{parsed.scheme}://{parsed.netloc}/{path_parts[0]}' if path_parts else url

        return {
            'User-Agent': self.USER_AGENT,
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-User': '?1',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': referer,
            'Connection': 'keep-alive',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Priority': 'u=0, i',
            'TE': 'trailers',
        }

    def fetch(
        self,
        url: str,
        max_retries: int = 10,
        initial_wait: float = 1.0,
        wait_increment: float = 1.0,
        timeout: float = 15.0,
    ) -> bytes:
        """Perform an HTTP GET and return the raw bytes of the response.

        A Request object is used with common headers (User-Agent, Referer,
        Accept-Language etc.) to mimic a modern browser and avoid basic
        anti-bot measures. The referer is derived from the URL's board root so
        that some hosts accept the request.

        If a transient error occurs (e.g. read timeout, connection reset,
        server error), the request is retried up to `max_retries` times with
        increasing wait before each retry.

        Args:
            url: The URL to fetch.
            max_retries: Number of retries on failure (default: 10).
            initial_wait: Wait time in seconds before the first retry (default: 1.0).
            wait_increment: Amount in seconds by which wait time increases with each retry (default: 1.0).
            timeout: Timeout in seconds for the request (default: 15.0).

        Returns:
            The raw content of the response.

        Raises:
            HTTPError: If the request fails after all retries.
            ThreadNotFoundError: If the response is 404 (not retried).
        """
        # Normalize protocol-relative URLs
        if url.startswith('//'):
            url = 'https:' + url

        headers = self._build_headers(url)
        req = urllib.request.Request(url, headers=headers)

        last_error = None
        last_code = None

        for attempt in range(max_retries + 1):
            if attempt > 0:
                if self.stop_event and self.stop_event.is_set():
                    break
                wait_time = initial_wait + (attempt - 1) * wait_increment
                log.warning(
                    f"Retry {attempt}/{max_retries} for {url} in {wait_time:.1f}s after error: {last_error}"
                )
                self._sleep(wait_time)
                if self.stop_event and self.stop_event.is_set():
                    break

            try:
                response = urllib.request.urlopen(req, timeout=timeout)
                return response.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise ThreadNotFoundError(f'Thread not found: {url}') from e
                last_error = e
                last_code = e.code
            except urllib.error.URLError as e:
                last_error = e
                last_code = None
            except Exception as e:
                last_error = e
                last_code = None

        if last_code is not None:
            raise HTTPError(f'HTTP error {last_code} for {url}', code=last_code) from last_error
        elif last_error is not None:
            if isinstance(last_error, urllib.error.URLError):
                raise HTTPError(f'URL error for {url}: {last_error}') from last_error
            else:
                raise HTTPError(f'Unexpected error fetching {url}: {last_error}') from last_error
        else:
            raise HTTPError(f'Failed to fetch {url}')

    def fetch_json(
        self,
        url: str,
        max_retries: int = 10,
        initial_wait: float = 1.0,
        wait_increment: float = 1.0,
        timeout: float = 15.0,
    ) -> Dict[str, Any]:
        """Fetch a URL and parse the response as JSON.

        Args:
            url: The URL to fetch.
            max_retries: Number of retries on failure (default: 10).
            initial_wait: Wait time in seconds before the first retry (default: 1.0).
            wait_increment: Amount in seconds by which wait time increases with each retry (default: 1.0).
            timeout: Timeout in seconds for the request (default: 15.0).

        Returns:
            The parsed JSON data.

        Raises:
            HTTPError: If the request fails or JSON parsing fails.
        """
        try:
            data = self.fetch(
                url,
                max_retries=max_retries,
                initial_wait=initial_wait,
                wait_increment=wait_increment,
                timeout=timeout,
            )
            return json.loads(data.decode('utf-8'))
        except json.JSONDecodeError as e:
            raise HTTPError(f'Failed to parse JSON from {url}: {e}') from e

    def fetch_thread_api(
        self,
        board: str,
        thread_id: str,
        max_retries: int = 0,
        initial_wait: float = 1.0,
        wait_increment: float = 1.0,
        timeout: float = 15.0,
    ) -> Optional[Dict[str, Any]]:
        """Fetch thread data from the 4chan JSON API.

        Args:
            board: The board identifier (e.g., 'g', 'wg').
            thread_id: The numeric thread ID.
            max_retries: Number of retries on failure (default: 0).
            initial_wait: Wait time in seconds before the first retry (default: 1.0).
            wait_increment: Amount in seconds by which wait time increases with each retry (default: 1.0).
            timeout: Timeout in seconds for the request (default: 15.0).

        Returns:
            The parsed thread JSON data, or None if the API is unavailable.
        """
        api_url = f'https://a.4cdn.org/{board}/thread/{thread_id}.json'
        req = urllib.request.Request(api_url, headers={'User-Agent': self.USER_AGENT})

        last_error = None
        for attempt in range(max_retries + 1):
            if attempt > 0:
                if self.stop_event and self.stop_event.is_set():
                    break
                wait_time = initial_wait + (attempt - 1) * wait_increment
                log.warning(
                    f"Retry {attempt}/{max_retries} for {api_url} in {wait_time:.1f}s after error: {last_error}"
                )
                self._sleep(wait_time)
                if self.stop_event and self.stop_event.is_set():
                    break

            try:
                response = urllib.request.urlopen(req, timeout=timeout)
                data = response.read().decode('utf-8')
                return json.loads(data)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise ThreadNotFoundError(f'Thread not found: {board}/{thread_id}') from e
                log.debug(f"HTTP error {e.code} fetching thread API for {board}/{thread_id}: {e}")
                last_error = e
            except Exception as e:
                log.debug(f"Failed to fetch thread API for {board}/{thread_id}: {e}")
                last_error = e

        return None
