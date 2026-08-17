"""Tests for HTTPClient retry logic and error handling."""
import threading
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from inb4404.exceptions import HTTPError, ThreadNotFoundError
from inb4404.http_client import HTTPClient


class TestHTTPClientRetry(unittest.TestCase):
    """Test retry behavior and exception handling in HTTPClient."""

    def test_fetch_success_first_try(self):
        """A successful request returns bytes immediately without retrying."""
        client = HTTPClient()
        mock_response = MagicMock()
        mock_response.read.return_value = b'test video data'

        with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
            with patch.object(client, '_sleep') as mock_sleep:
                data = client.fetch('https://i.4cdn.org/wsg/123456.webm')
                self.assertEqual(data, b'test video data')
                self.assertEqual(mock_urlopen.call_count, 1)
                mock_sleep.assert_not_called()

    def test_fetch_retry_and_succeed(self):
        """Transient errors trigger retries with increasing wait before succeeding."""
        client = HTTPClient()
        mock_response = MagicMock()
        mock_response.read.return_value = b'recovered video'

        # Fail twice with read timeout, succeed on 3rd attempt
        side_effects = [
            TimeoutError('The read operation timed out'),
            urllib.error.URLError('The read operation timed out'),
            mock_response,
        ]

        with patch('urllib.request.urlopen', side_effect=side_effects) as mock_urlopen:
            with patch.object(client, '_sleep') as mock_sleep:
                data = client.fetch('https://i.4cdn.org/wsg/123456.webm', max_retries=10, initial_wait=1.0, wait_increment=1.0)
                self.assertEqual(data, b'recovered video')
                self.assertEqual(mock_urlopen.call_count, 3)
                # Sleep called before attempt 2 (1.0s) and attempt 3 (2.0s)
                self.assertEqual(mock_sleep.call_count, 2)
                mock_sleep.assert_any_call(1.0)
                mock_sleep.assert_any_call(2.0)

    def test_fetch_retries_10_times_with_increasing_wait_then_raises(self):
        """Failing all 10 retries attempts 11 times with increasing wait and raises HTTPError."""
        client = HTTPClient()
        timeout_err = TimeoutError('The read operation timed out')

        with patch('urllib.request.urlopen', side_effect=timeout_err) as mock_urlopen:
            with patch.object(client, '_sleep') as mock_sleep:
                with self.assertRaises(HTTPError) as ctx:
                    client.fetch('https://i.4cdn.org/wsg/123456.webm', max_retries=10, initial_wait=1.0, wait_increment=1.0)

                # 1 initial + 10 retries = 11 attempts
                self.assertEqual(mock_urlopen.call_count, 11)
                # 10 sleeps before each retry: 1s, 2s, 3s, ..., 10s
                self.assertEqual(mock_sleep.call_count, 10)
                expected_waits = [float(i) for i in range(1, 11)]
                actual_waits = [call.args[0] for call in mock_sleep.call_args_list]
                self.assertEqual(actual_waits, expected_waits)
                self.assertIn('The read operation timed out', str(ctx.exception))

    def test_fetch_404_raises_immediately(self):
        """HTTP 404 raises ThreadNotFoundError immediately without retries."""
        client = HTTPClient()
        http_404 = urllib.error.HTTPError('https://i.4cdn.org/wsg/123456.webm', 404, 'Not Found', {}, None)

        with patch('urllib.request.urlopen', side_effect=http_404) as mock_urlopen:
            with patch.object(client, '_sleep') as mock_sleep:
                with self.assertRaises(ThreadNotFoundError):
                    client.fetch('https://i.4cdn.org/wsg/123456.webm', max_retries=10)

                self.assertEqual(mock_urlopen.call_count, 1)
                mock_sleep.assert_not_called()

    def test_fetch_http_500_retries_and_raises_with_code(self):
        """HTTP 500 error retries and preserves status code on HTTPError."""
        client = HTTPClient()
        http_500 = urllib.error.HTTPError('https://i.4cdn.org/wsg/123456.webm', 500, 'Internal Server Error', {}, None)

        with patch('urllib.request.urlopen', side_effect=http_500) as mock_urlopen:
            with patch.object(client, '_sleep') as mock_sleep:
                with self.assertRaises(HTTPError) as ctx:
                    client.fetch('https://i.4cdn.org/wsg/123456.webm', max_retries=3, initial_wait=1.0, wait_increment=1.0)

                self.assertEqual(mock_urlopen.call_count, 4)
                self.assertEqual(mock_sleep.call_count, 3)
                self.assertEqual(ctx.exception.code, 500)

    def test_fetch_stop_event_aborts_retries(self):
        """Setting stop_event aborts retrying promptly."""
        stop_event = threading.Event()
        client = HTTPClient(stop_event=stop_event)
        timeout_err = TimeoutError('The read operation timed out')

        def set_stop_on_sleep(seconds):
            stop_event.set()

        with patch('urllib.request.urlopen', side_effect=timeout_err) as mock_urlopen:
            with patch.object(client, '_sleep', side_effect=set_stop_on_sleep) as mock_sleep:
                with self.assertRaises(HTTPError):
                    client.fetch('https://i.4cdn.org/wsg/123456.webm', max_retries=10)

                # Attempt 0 failed -> sleep called (which sets stop_event) -> break
                self.assertEqual(mock_urlopen.call_count, 1)
                self.assertEqual(mock_sleep.call_count, 1)

    def test_fetch_json_uses_retrying_fetch(self):
        """fetch_json successfully fetches and decodes JSON."""
        client = HTTPClient()
        with patch.object(client, 'fetch', return_value=b'{"posts": [{"no": 1}]}') as mock_fetch:
            data = client.fetch_json('https://a.4cdn.org/g/thread/1000.json')
            self.assertEqual(data, {'posts': [{'no': 1}]})
            mock_fetch.assert_called_once()


if __name__ == '__main__':
    unittest.main()
