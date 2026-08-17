"""Custom exception classes for inb4404."""
from typing import Optional


class Inb4404Error(Exception):
    """Base exception for all inb4404 errors."""
    pass


class ThreadNotFoundError(Inb4404Error):
    """Raised when a thread cannot be found (404)."""
    pass


class DownloadError(Inb4404Error):
    """Raised when a file download fails."""
    pass


class DatabaseError(Inb4404Error):
    """Raised when a database operation fails."""
    pass


class HTTPError(Inb4404Error):
    """Raised when an HTTP request fails."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


class MaintenanceError(HTTPError):
    """Raised when the server is in maintenance mode."""

    def __init__(self, message: str = "Performing maintenance. We'll be back soon.", code: Optional[int] = None):
        super().__init__(message, code=code)

