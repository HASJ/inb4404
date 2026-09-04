"""Process manager compatibility wrapper for the consolidated QueueManager."""
from multiprocessing import Process
from .queue_manager import (
    QueueManager,
    _call_watcher,
    RatePacer,
    DownloadTask,
    DownloadWorker,
)

ProcessManager = QueueManager

__all__ = [
    'ProcessManager',
    'QueueManager',
    '_call_watcher',
    'Process',
    'RatePacer',
    'DownloadTask',
    'DownloadWorker',
]
