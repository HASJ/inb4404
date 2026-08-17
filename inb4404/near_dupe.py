"""Near-duplicate detection and resolution.

Shared by the live watcher and the ``--dedupe-downloads`` pass so the win
rule and the deletion behaviour exist in exactly one place.
"""
import logging
import os

from . import perceptual

log = logging.getLogger('inb4404')

ORIGINAL_DIR = 'original'


class NearDupeResolver(object):
    """Finds near-duplicates of a file and resolves the pair."""

    def __init__(self, db, distance: int, verbose: bool = False):
        """Initialize the resolver.

        Args:
            db: A `HashDB` instance.
            distance: Maximum Hamming distance for a near-duplicate frame pair.
            verbose: Whether to emit per-file detail.
        """
        self.db = db
        self.distance = distance
        self.verbose = verbose
        # Total files deleted. Counted here rather than inferred from
        # check()'s return value, which only reports whether the incoming
        # file was deleted -- a winning file can delete several held copies
        # in one call and those would go uncounted.
        self.deleted = 0

    def _discard(self, path: str, conn=None) -> bool:
        """Delete one file and drop its rows from both tables.

        Args:
            path: Absolute path to the file to delete.
            conn: Optional open connection from `HashDB.bulk_session`.

        Returns:
            True when the file was removed.
        """
        try:
            os.remove(path)
        except OSError as e:
            log.warning('Could not delete %s: %s', path, e)
            return False
        self.db.delete_phash(path, conn=conn)
        self.db.delete_hash(path, conn=conn)
        self.deleted += 1
        return True

    def check(self, path, meta, allow_foreign_moves: bool = False,
              conn=None) -> bool:
        """Compare one file against everything already hashed and resolve.

        The loser of a pair is deleted outright. There is no way to verify a
        held file is still the winner later -- it may be deleted, moved, or
        renamed by the user -- so keeping a "loser" copy around as a hedge
        just accumulates disk with no way to know if it is still needed.

        A held file in a *different* thread directory may be owned by
        another watcher process, and deleting it mid-download is a race.
        `allow_foreign_moves` is therefore False during live watching and
        True only in `--dedupe-downloads`, which runs single-process.

        Args:
            path: Absolute path to the file being checked.
            meta: Its `perceptual.MediaMeta`.
            allow_foreign_moves: Whether files outside `path`'s directory
                may be deleted.
            conn: Optional open connection from `HashDB.bulk_session`.

        Returns:
            True when the incoming file was deleted.
        """
        frame_chunks = [perceptual.chunks(h) for h in meta.frames]
        candidates = self.db.find_phash_candidates(
            frame_chunks, exclude_path=path, conn=conn)

        for other_path in candidates:
            other = self.db.get_phash(other_path, conn=conn)
            if other is None:
                continue
            if not perceptual.match(meta.frames, other.frames, self.distance):
                continue
            if not os.path.isfile(other_path):
                # Row survived its file. Drop it rather than resolving
                # against media that is no longer there.
                self.db.delete_phash(other_path, conn=conn)
                continue

            same_dir = (os.path.dirname(os.path.abspath(other_path)) ==
                        os.path.dirname(os.path.abspath(path)))

            if perceptual.supersedes(meta, other):
                if not (same_dir or allow_foreign_moves):
                    log.info(
                        'Near-dupe: %s supersedes %s (left in place; another '
                        'process may own it, --dedupe-downloads will resolve)',
                        os.path.basename(path), other_path
                    )
                    continue
                if self._discard(other_path, conn=conn):
                    log.info('Near-dupe: %s supersedes %s -> deleted',
                             os.path.basename(path),
                             os.path.basename(other_path))
                # Keep scanning: this file may beat several held copies, and
                # resolving only the first would leave the rest for a later
                # run, making the pass non-idempotent.
                continue

            deleted = self._discard(path, conn=conn)
            if deleted:
                log.info('Near-dupe: %s superseded by %s -> deleted',
                         os.path.basename(path),
                         os.path.basename(other_path))
            return deleted

        return False
