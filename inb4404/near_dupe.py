"""Near-duplicate detection and non-destructive resolution.

Shared by the live watcher and the ``--dedupe-downloads`` pass so the win
rule and the relocation behaviour exist in exactly one place.
"""
import logging
import os
import shutil
from typing import Optional

from . import perceptual

log = logging.getLogger('inb4404')

# Folder that superseded media is moved into, created beside the file it
# relates to so a resolved pair stays adjacent when browsing.
ORIGINAL_DIR = 'original'


def is_set_aside(path: str) -> bool:
    """Report whether a path already lives inside an `original/` folder.

    Files there were resolved on an earlier run. Both the directory walks and
    the candidate scan skip them, so a repeated pass is idempotent.

    Args:
        path: Path to test.

    Returns:
        True when any component of the path is the `original/` folder.
    """
    parts = os.path.normpath(path).split(os.sep)
    return ORIGINAL_DIR in parts


def relocate(path: str) -> Optional[str]:
    """Move a file into a sibling `original/` folder, suffixing its stem.

    Picks the smallest free suffix -- `_1`, `_2`, ... -- so repeated
    resolutions never collide and nothing is overwritten.

    Args:
        path: Absolute path to the file to move.

    Returns:
        The new absolute path, or None when the move failed.
    """
    if not os.path.isfile(path):
        return None

    directory = os.path.dirname(path)
    stem, ext = os.path.splitext(os.path.basename(path))
    target_dir = os.path.join(directory, ORIGINAL_DIR)

    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as e:
        log.warning('Could not create %s: %s', target_dir, e)
        return None

    index = 1
    while True:
        candidate = os.path.join(target_dir, '%s_%d%s' % (stem, index, ext))
        if not os.path.exists(candidate):
            break
        index += 1

    try:
        shutil.move(path, candidate)
    except Exception as e:
        log.warning('Could not move %s to %s: %s', path, candidate, e)
        return None
    return candidate


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

    def check(self, path, meta, allow_foreign_moves: bool = False,
              conn=None) -> Optional[str]:
        """Compare one file against everything already hashed and resolve.

        The loser of a pair is moved into `original/`; nothing is deleted.
        When the incoming file loses it is relocated and its new path is
        returned, so the caller knows the file is no longer where it was
        written.

        A held file in a *different* thread directory may be owned by
        another watcher process, and moving it mid-download is a race.
        `allow_foreign_moves` is therefore False during live watching and
        True only in `--dedupe-downloads`, which runs single-process.

        Args:
            path: Absolute path to the file being checked.
            meta: Its `perceptual.MediaMeta`.
            allow_foreign_moves: Whether files outside `path`'s directory
                may be relocated.
            conn: Optional open connection from `HashDB.bulk_session`.

        Returns:
            The incoming file's new path when it was relocated, else None.
        """
        frame_chunks = [perceptual.chunks(h) for h in meta.frames]
        candidates = self.db.find_phash_candidates(
            frame_chunks, exclude_path=path, conn=conn)

        for other_path in candidates:
            if is_set_aside(other_path):
                # Already resolved on an earlier run. Comparing against it
                # again would relocate it a second time, nesting original/
                # inside original/ and making the pass non-idempotent.
                continue
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
                moved = relocate(other_path)
                if moved:
                    self.db.move_phash_path(other_path, moved, conn=conn)
                    log.info('Near-dupe: %s supersedes %s -> %s',
                             os.path.basename(path),
                             os.path.basename(other_path), moved)
                # Keep scanning: this file may beat several held copies, and
                # resolving only the first would leave the rest for a later
                # run, making the pass non-idempotent.
                continue

            moved = relocate(path)
            if moved:
                self.db.move_phash_path(path, moved, conn=conn)
                log.info('Near-dupe: %s superseded by %s -> %s',
                         os.path.basename(path),
                         os.path.basename(other_path), moved)
            return moved

        return None
