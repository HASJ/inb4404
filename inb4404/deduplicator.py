"""Deduplication logic for removing duplicate files."""
import os
import logging
from typing import Dict, List, Tuple

from .config import Config
from .database import HashDB
from .file_utils import FileManager

log = logging.getLogger('inb4404')


class Deduplicator:
    """Scans downloads directory and removes duplicate files."""

    def __init__(self, config: Config, workpath: str):
        """Initialize the Deduplicator.

        Args:
            config: Configuration settings.
            workpath: Base working directory path.
        """
        self.config = config
        self.workpath = workpath
        db_path = os.path.join(workpath, 'hashes.db')
        self.db = HashDB(db_path=db_path)
        self.file_manager = FileManager()
        self.downloads_root = os.path.join(workpath, 'downloads')

    def scan_directory(self) -> Dict[str, List[Tuple[str, int, int]]]:
        """Scan the downloads directory and collect files by MD5 hash.
        This method will first check the database for a file's metadata. If the
        file's modification time and size have not changed, it will use the
        hash from the database. Otherwise, it will re-hash the file.
        Returns:
            A dictionary mapping MD5 hashes to lists of (file_path, mtime, size) tuples.
        """
        md5_map: Dict[str, List[Tuple[str, int, int]]] = {}

        if not os.path.exists(self.downloads_root):
            log.warning(f'No downloads directory found at {self.downloads_root}')
            return md5_map

        if self.config.verbose:
            log.info(f'Scanning files in downloads directory: {self.downloads_root}')

        for root, dirs, files in os.walk(self.downloads_root):
            for fn in files:
                if fn == '.hashes.txt':
                    continue
                full_path = os.path.join(root, fn)
                if not os.path.isfile(full_path):
                    continue

                try:
                    stats = os.stat(full_path)
                    mtime = int(stats.st_mtime)
                    size = stats.st_size
                except OSError as e:
                    log.warning(f'Could not stat file: {full_path}: {e}')
                    continue

                metadata = self.db.get_file_metadata(full_path)
                if metadata:
                    db_md5, db_mtime, db_size = metadata
                    if db_mtime == mtime and db_size == size:
                        md5_map.setdefault(db_md5, []).append((full_path, mtime, size))
                        if self.config.verbose:
                            log.debug(f'Skipping hash for {full_path} (mtime and size match)')
                        continue
                    else:
                        # Metadata mismatch implies the file changed.
                        # The DB entry is stale and should be removed to prevent future confusion.
                        if self.config.verbose:
                            log.info(f'Metadata mismatch for {full_path}. invalidating DB entry.')
                        self.db.delete_file_metadata(full_path)

                h = self.file_manager.compute_hash(full_path)
                if not h:
                    log.warning(f'Could not read file for hashing: {full_path}')
                    continue

                md5_map.setdefault(h, []).append((full_path, mtime, size))
                if self.config.verbose:
                    log.info(f'Hashed: {full_path} -> {h}')
        
        if self.config.verbose:
            total_files = sum(len(v) for v in md5_map.values())
            log.info(f'Found {total_files} files, {len(md5_map)} unique hashes')

        return md5_map

    def remove_duplicates(self, md5_map: Dict[str, List[Tuple[str, int, int]]]) -> tuple:
        """Remove duplicate files, keeping the oldest for each hash.
        Args:
            md5_map: Dictionary mapping MD5 hashes to lists of (file_path, mtime, size) tuples.
        Returns:
            A tuple of (kept_count, deleted_count).
        """
        kept_count = 0
        deleted_count = 0

        for h, paths in md5_map.items():
            if len(paths) > 1:
                # Sort by mtime ascending (oldest first)
                paths_sorted = sorted(paths, key=lambda p: p[1])
                kept_path, mtime, size = paths_sorted[0]
                duplicates = paths_sorted[1:]

                log.info(f'Found {len(duplicates)} duplicate(s) for hash {h}. Keeping oldest file: {os.path.basename(kept_path)}')
                
                # Upsert the kept file's hash into the database only if needed
                if self.db.get_path(h) != kept_path:
                    rel = os.path.relpath(kept_path, self.downloads_root)
                    thread_name = os.path.dirname(rel).replace(os.sep, '/')
                    self.db.upsert(h, kept_path, thread_name, mtime, size)
                    
                    if self.config.verbose:
                        log.info(f'  Updated database with hash {h} for {kept_path}')

                for d_path, _, _ in duplicates:
                    try:
                        os.remove(d_path)
                        deleted_count += 1
                        if self.config.verbose:
                           log.info(f'  Deleted duplicate file: {d_path}')
                    except OSError as e:
                        # Log specific OS errors (like PermissionError/FileInUse)
                        log.error(f'Failed to delete duplicate {d_path}: {e}')
                    except Exception as e:
                        log.warning(f'Could not remove duplicate file {d_path}: {e}')
                
                kept_count += 1
            else:
                # No duplicates, just ensure hash is in DB
                kept_path, mtime, size = paths[0]

                # Check if hash mapping is already correct to avoid unnecessary writes
                if self.db.get_path(h) == kept_path:
                    kept_count += 1
                    continue

                rel = os.path.relpath(kept_path, self.downloads_root)
                thread_name = os.path.dirname(rel).replace(os.sep, '/')
                self.db.upsert(h, kept_path, thread_name, mtime, size)
                if self.config.verbose:
                    log.info(f'Added/updated hash {h} for {os.path.basename(kept_path)} in the database.')
                kept_count += 1
        
        return (kept_count, deleted_count)

    def remove_legacy_files(self) -> None:
        """Remove legacy .hashes.txt files."""
        for root, dirs, files in os.walk(self.downloads_root):
            for fn in files:
                if fn == '.hashes.txt':
                    fp = os.path.join(root, fn)
                    try:
                        os.remove(fp)
                        log.info(f'Removed legacy file: {fp}')
                    except Exception as e:
                        log.warning(f'Could not remove legacy .hashes.txt {fp}: {e}')

    def run(self) -> None:
        """Run the deduplication process."""
        # Scan directory
        md5_map = self.scan_directory()

        if not md5_map:
            return

        # Remove duplicates
        kept_count, deleted_count = self.remove_duplicates(md5_map)

        log.info(f'Dedupe complete. Kept {kept_count} groups, removed {deleted_count} files')

        # Remove legacy files
        self.remove_legacy_files()

