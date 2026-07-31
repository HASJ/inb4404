"""SQLite database operations for hash storage."""
import sqlite3
import time
import logging
from typing import Dict, Iterable, Optional, Sequence, Set, Tuple
import os
from contextlib import contextmanager

from .config import DB_PATH_DEFAULT, DB_TIMEOUT
from .exceptions import DatabaseError
from . import perceptual

log = logging.getLogger('inb4404')


class HashDB:
    """Manages SQLite database operations for MD5 hash storage."""

    def __init__(self, db_path: Optional[str] = None, timeout: int = DB_TIMEOUT):
        """Initialize the HashDB instance.

        Args:
            db_path: Path to the SQLite database file. If None, uses default.
            timeout: Database connection timeout in seconds.
        """
        if db_path is None:
            # Calculate default path based on workpath
            from .config import DEFAULT_WORKPATH, DB_PATH_DEFAULT
            self.db_path = os.path.join(DEFAULT_WORKPATH, DB_PATH_DEFAULT)
        else:
            self.db_path = db_path
        self.timeout = timeout
        self.init()

    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=self.timeout)
            yield conn
        except Exception as e:
            log.warning(f'Database operation failed: {e}')
            raise DatabaseError(f'Database operation failed: {e}') from e
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def init(self) -> None:
        """Ensure the SQLite database and the required table exist.

        This function is idempotent and safe to call multiple times. It sets
        `journal_mode=WAL` to improve concurrency between processes.
        """
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute('PRAGMA journal_mode=WAL;')
                cur.execute(
                    'CREATE TABLE IF NOT EXISTS hashes '
                    '(md5 TEXT PRIMARY KEY, path TEXT, thread TEXT, ts INTEGER)'
                )

                # Check for mtime and size columns
                cur.execute('PRAGMA table_info(hashes)')
                columns = [row[1] for row in cur.fetchall()]
                if 'mtime' not in columns:
                    cur.execute('ALTER TABLE hashes ADD COLUMN mtime INTEGER')
                if 'size' not in columns:
                    cur.execute('ALTER TABLE hashes ADD COLUMN size INTEGER')
                
                # Add an index on the path column for faster lookups
                cur.execute('CREATE INDEX IF NOT EXISTS idx_hashes_path ON hashes(path)')

                # Perceptual-hash frames. Kept in a separate table so the
                # existing (md5, mtime, size) queries stay untouched -- they
                # are unpacked positionally by callers.
                cur.execute(
                    'CREATE TABLE IF NOT EXISTS phash_frames ('
                    'path TEXT NOT NULL, frame INTEGER NOT NULL, h TEXT NOT NULL, '
                    'b0 INTEGER, b1 INTEGER, b2 INTEGER, b3 INTEGER, '
                    'width INTEGER, height INTEGER, duration REAL, '
                    'PRIMARY KEY (path, frame))'
                )
                for band in ('b0', 'b1', 'b2', 'b3'):
                    cur.execute(
                        'CREATE INDEX IF NOT EXISTS idx_phash_%s '
                        'ON phash_frames(%s)' % (band, band)
                    )

                conn.commit()
        except Exception as e:
            log.warning(f'Could not initialize hashes DB: {e}')

    def get_path(self, md5: str) -> Optional[str]:
        """Return stored path for `md5` or None when not present.

        Args:
            md5: The MD5 hash to look up.

        Returns:
            The file path associated with the MD5 hash, or None if not found.
        """
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute('SELECT path FROM hashes WHERE md5=?', (md5,))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception:
            return None

    def get_file_metadata(self, path: str) -> Optional[Tuple[str, int, int]]:
        """Return stored md5, mtime and size for `path` or None when not present.
        Args:
            path: The file path to look up.
        Returns:
            A tuple of (md5, mtime, size), or None if not found.
        """
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute('SELECT md5, mtime, size FROM hashes WHERE path=?', (path,))
                row = cur.fetchone()
                return row if row else None
        except Exception:
            return None

    def has_hash(self, md5: str) -> bool:
        """Check if the given MD5 hash exists in the database.

        Args:
            md5: The MD5 hash to check.

        Returns:
            True if the hash exists, False otherwise.
        """
        return self.get_path(md5) is not None

    def insert(self, md5: str, path: str, thread_name: str, mtime: int, size: int) -> None:
        """Insert md5->path mapping. Uses INSERT OR IGNORE to avoid races.

        Args:
            md5: The MD5 hash of the file.
            path: The file path.
            thread_name: The name/ID of the thread.
            mtime: The modification time of the file.
            size: The size of the file.
        """
        try:
            with self._get_connection() as conn:
                conn.execute(
                    'INSERT OR IGNORE INTO hashes (md5, path, thread, ts, mtime, size) VALUES (?,?,?,?,?,?)',
                    (md5, path, thread_name, int(time.time()), mtime, size)
                )
                conn.commit()
        except Exception as e:
            log.warning(f'Could not write to hashes DB: {e}')

    def upsert(self, md5: str, path: str, thread_name: str, mtime: int, size: int) -> None:
        """Insert or replace the md5->path mapping.

        Used after dedupe to ensure the DB points to the kept file path.

        Args:
            md5: The MD5 hash of the file.
            path: The file path.
            thread_name: The name/ID of the thread.
            mtime: The modification time of the file.
            size: The size of the file.
        """
        try:
            with self._get_connection() as conn:
                conn.execute(
                    'INSERT OR REPLACE INTO hashes (md5, path, thread, ts, mtime, size) VALUES (?,?,?,?,?,?)',
                    (md5, path, thread_name, int(time.time()), mtime, size)
                )
                conn.commit()
        except Exception as e:
            log.warning(f'Could not upsert into hashes DB: {e}')

    def delete_file_metadata(self, path: str) -> None:
        """Delete metadata for a specific file path.

        Args:
            path: The file path to remove from the database.
        """
        try:
            with self._get_connection() as conn:
                conn.execute('DELETE FROM hashes WHERE path=?', (path,))
                conn.commit()
        except Exception as e:
            log.warning(f'Could not delete from hashes DB: {e}')

    def load_all_metadata(self) -> Dict[str, Tuple[str, int, int]]:
        """Load metadata for every stored path in a single query.

        Bulk equivalent of calling `get_file_metadata` once per path. Intended
        for whole-tree passes (e.g. `--dedupe-downloads`) where opening one
        connection per file dominates the runtime.

        Returns:
            A dict mapping path -> (md5, mtime, size). Empty on failure.
        """
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute('SELECT path, md5, mtime, size FROM hashes')
                return {
                    row[0]: (row[1], row[2], row[3])
                    for row in cur.fetchall() if row and row[0]
                }
        except Exception as e:
            log.warning(f'Could not bulk-load metadata from hashes DB: {e}')
            return {}

    def load_all_paths(self) -> Dict[str, str]:
        """Load the md5 -> path mapping for every row in a single query.

        Bulk equivalent of calling `get_path` once per hash.

        Returns:
            A dict mapping md5 -> path. Empty on failure.
        """
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute('SELECT md5, path FROM hashes')
                return {row[0]: row[1] for row in cur.fetchall() if row and row[0]}
        except Exception as e:
            log.warning(f'Could not bulk-load paths from hashes DB: {e}')
            return {}

    def upsert_many(self, rows: Sequence[Tuple[str, str, str, int, int]]) -> None:
        """Insert or replace many md5->path mappings in one transaction.

        Args:
            rows: Sequence of (md5, path, thread_name, mtime, size) tuples.
        """
        if not rows:
            return
        ts = int(time.time())
        try:
            with self._get_connection() as conn:
                conn.executemany(
                    'INSERT OR REPLACE INTO hashes (md5, path, thread, ts, mtime, size) VALUES (?,?,?,?,?,?)',
                    [(md5, path, thread, ts, mtime, size)
                     for md5, path, thread, mtime, size in rows]
                )
                conn.commit()
        except Exception as e:
            log.warning(f'Could not bulk-upsert into hashes DB: {e}')

    def delete_paths(self, paths: Iterable[str]) -> None:
        """Delete metadata for many file paths in one transaction.

        Args:
            paths: The file paths to remove from the database.
        """
        rows = [(p,) for p in paths]
        if not rows:
            return
        try:
            with self._get_connection() as conn:
                conn.executemany('DELETE FROM hashes WHERE path=?', rows)
                conn.commit()
        except Exception as e:
            log.warning(f'Could not bulk-delete from hashes DB: {e}')

    def get_thread_hashes(self, thread_id: str) -> Set[str]:
        """Get all MD5 hashes for a specific thread.

        Args:
            thread_id: The numeric thread ID.

        Returns:
            A set of MD5 hashes for the thread.
        """
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute('SELECT md5 FROM hashes WHERE thread=?', (thread_id,))
                rows = cur.fetchall()
                return {r[0] for r in rows if r and r[0]}
        except Exception:
            return set()

    def count_hashes(self) -> int:
        """Get the total count of hashes in the database.

        Returns:
            The number of hashes in the database.
        """
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute('SELECT COUNT(*) FROM hashes')
                row = cur.fetchone()
                return row[0] if row else 0
        except Exception:
            return 0

    @contextmanager
    def bulk_session(self):
        """Yield one connection reused across many operations.

        `_get_connection` opens and closes per call, which is correct for
        watcher processes but costs ~0.5 ms per operation on a whole-tree
        pass. Only use this from `Deduplicator`, which runs before any
        watcher starts.

        Yields:
            An open sqlite3.Connection, committed and closed on exit.
        """
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            yield conn
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @contextmanager
    def _session(self, conn):
        """Yield `conn` when given, otherwise a short-lived connection."""
        if conn is not None:
            yield conn
        else:
            owned = sqlite3.connect(self.db_path, timeout=self.timeout)
            try:
                yield owned
                owned.commit()
            finally:
                try:
                    owned.close()
                except Exception:
                    pass

    def record_phash(self, path, meta, conn=None) -> None:
        """Store the perceptual frame hashes for one file.

        Replaces any rows previously stored for `path`.

        Args:
            path: Absolute path to the media file.
            meta: A `perceptual.MediaMeta` holding frames and dimensions.
            conn: Optional open connection from `bulk_session`.
        """
        try:
            with self._session(conn) as c:
                c.execute('DELETE FROM phash_frames WHERE path=?', (path,))
                rows = []
                for i, h in enumerate(meta.frames):
                    b0, b1, b2, b3 = perceptual.chunks(h)
                    rows.append((path, i, perceptual.to_hex(h), b0, b1, b2, b3,
                                 meta.width, meta.height, meta.duration))
                c.executemany(
                    'INSERT INTO phash_frames '
                    '(path, frame, h, b0, b1, b2, b3, width, height, duration) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?)', rows
                )
        except Exception as e:
            log.warning('Could not record phash for %s: %s', path, e)

    def get_phash(self, path, conn=None):
        """Load the stored perceptual hashes for one file.

        Args:
            path: Absolute path to the media file.
            conn: Optional open connection from `bulk_session`.

        Returns:
            A `perceptual.MediaMeta`, or None when nothing is stored.
        """
        try:
            with self._session(conn) as c:
                cur = c.cursor()
                cur.execute(
                    'SELECT h, width, height, duration FROM phash_frames '
                    'WHERE path=? ORDER BY frame', (path,)
                )
                rows = cur.fetchall()
                if not rows:
                    return None
                frames = [perceptual.from_hex(r[0]) for r in rows]
                return perceptual.MediaMeta(
                    frames=frames, width=rows[0][1] or 0,
                    height=rows[0][2] or 0, duration=rows[0][3] or 0.0
                )
        except Exception as e:
            log.warning('Could not read phash for %s: %s', path, e)
            return None

    def has_phash(self, path, conn=None) -> bool:
        """Report whether any frame rows exist for `path`."""
        try:
            with self._session(conn) as c:
                cur = c.cursor()
                cur.execute(
                    'SELECT 1 FROM phash_frames WHERE path=? LIMIT 1', (path,)
                )
                return cur.fetchone() is not None
        except Exception:
            return False

    def find_phash_candidates(self, frame_chunks, exclude_path, conn=None):
        """Return paths sharing a band value with any of the given frames.

        Two hashes within Hamming distance 3 must agree exactly on at least
        one 16-bit chunk, so this is a complete candidate set for that
        distance -- the caller still verifies with a real comparison.

        Args:
            frame_chunks: One 4-tuple of band values per frame.
            exclude_path: Path to omit, normally the file being checked.
            conn: Optional open connection from `bulk_session`.

        Returns:
            A list of distinct candidate paths.
        """
        found = []
        seen = set()
        try:
            with self._session(conn) as c:
                cur = c.cursor()
                for b0, b1, b2, b3 in frame_chunks:
                    cur.execute(
                        'SELECT DISTINCT path FROM phash_frames '
                        'WHERE b0=? OR b1=? OR b2=? OR b3=?',
                        (b0, b1, b2, b3)
                    )
                    for row in cur.fetchall():
                        p = row[0]
                        if p == exclude_path or p in seen:
                            continue
                        seen.add(p)
                        found.append(p)
        except Exception as e:
            log.warning('Could not probe phash candidates: %s', e)
        return found

    def move_phash_path(self, old_path, new_path, conn=None) -> None:
        """Repoint stored frame rows after a file is relocated.

        Args:
            old_path: The path the rows currently carry.
            new_path: The path the file now lives at.
            conn: Optional open connection from `bulk_session`.
        """
        try:
            with self._session(conn) as c:
                c.execute('DELETE FROM phash_frames WHERE path=?', (new_path,))
                c.execute('UPDATE phash_frames SET path=? WHERE path=?',
                          (new_path, old_path))
        except Exception as e:
            log.warning('Could not move phash rows %s -> %s: %s',
                        old_path, new_path, e)

    def delete_phash(self, path, conn=None) -> None:
        """Remove all frame rows for `path`."""
        try:
            with self._session(conn) as c:
                c.execute('DELETE FROM phash_frames WHERE path=?', (path,))
        except Exception as e:
            log.warning('Could not delete phash rows for %s: %s', path, e)

