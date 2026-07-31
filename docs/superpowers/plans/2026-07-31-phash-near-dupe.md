# pHash Near-Duplicate Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect near-duplicate media (re-encodes, resizes) that MD5 cannot see, and resolve each pair by moving the weaker file to an `original/` folder instead of deleting it.

**Architecture:** A perceptual hash is computed by ffmpeg-decoded frames run through a truncated-basis DCT. Frame hashes are stored one row each in a new `phash_frames` table whose four 16-bit "band" columns are indexed, so a global near-duplicate lookup is four indexed SQL probes rather than an in-memory index. MD5 runs first and short-circuits the whole layer.

**Tech Stack:** Python 3.6+, standard library only. External `ffmpeg` binary (probed, optional). SQLite via `sqlite3`. Tests via stdlib `unittest`.

## Global Constraints

- Python 3.6+ compatible. No f-string `=` specifier, no walrus, no `int.bit_count` (3.10+), no `math.comb`. Use `bin(x).count('1')` for popcount.
- **Standard library only.** No new pip dependency. ffmpeg is an external binary, probed at runtime, and its absence disables the feature rather than raising.
- Google-style docstrings with `Args:`/`Returns:`/`Raises:` on every public method. Type hints throughout.
- All new exceptions derive from `Inb4404Error` in `exceptions.py`. DB and filesystem failures are logged as warnings and swallowed — a watcher must never die on transient I/O.
- Single logger: `logging.getLogger('inb4404')`. Detail output goes behind `if self.config.verbose:`.
- `Config` must stay a plain dataclass of picklable values — it is pickled into child processes under Windows `spawn`. Never attach an index, connection, or open handle to it.
- Do not modify `load_all_metadata()`, `get_file_metadata()`, or `Deduplicator.remove_duplicates`. Their `(md5, mtime, size)` tuples are unpacked positionally at `deduplicator.py:76`.
- Commits: one logical change each, no `Co-Authored-By` trailers.
- Run tests with `python -m unittest discover -s tests -v`.

---

### Task 1: Perceptual hashing core

**Files:**
- Create: `inb4404/perceptual.py`
- Create: `tests/__init__.py` (empty)
- Test: `tests/test_perceptual.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ffmpeg_available() -> bool`
  - `phash_frame(gray: bytes) -> int` — 1024 raw grayscale bytes to a 64-bit int
  - `chunks(h: int) -> Tuple[int, int, int, int]`
  - `hamming(a: int, b: int) -> int`
  - `match(a: List[int], b: List[int], distance: int) -> bool`
  - `supersedes(new: 'MediaMeta', old: 'MediaMeta') -> bool`
  - `extract(path: str) -> Optional['MediaMeta']`
  - `class MediaMeta` with fields `frames: List[int]`, `width: int`, `height: int`, `duration: float`
  - `to_hex(h: int) -> str` / `from_hex(s: str) -> int`

- [ ] **Step 1: Write the failing test**

Create `tests/__init__.py` as an empty file, then `tests/test_perceptual.py`:

```python
"""Tests for the perceptual hashing core."""
import unittest

from inb4404 import perceptual


def _flat(rows):
    """Flatten a 32x32 list of ints into 1024 raw bytes."""
    return bytes(bytearray([v for row in rows for v in row]))


def _solid(value):
    return _flat([[value] * 32 for _ in range(32)])


def _gradient():
    return _flat([[(x * 8) % 256 for x in range(32)] for _ in range(32)])


def _gradient_noisy():
    rows = []
    for y in range(32):
        rows.append([min(255, max(0, (x * 8) % 256 + (1 if (x + y) % 7 == 0 else 0)))
                     for x in range(32)])
    return _flat(rows)


class TestPhashFrame(unittest.TestCase):
    def test_returns_64_bit_value(self):
        h = perceptual.phash_frame(_gradient())
        self.assertIsInstance(h, int)
        self.assertGreaterEqual(h, 0)
        self.assertLess(h, 1 << 64)

    def test_deterministic(self):
        self.assertEqual(perceptual.phash_frame(_gradient()),
                         perceptual.phash_frame(_gradient()))

    def test_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            perceptual.phash_frame(b'\x00' * 100)

    def test_near_identical_images_are_close(self):
        a = perceptual.phash_frame(_gradient())
        b = perceptual.phash_frame(_gradient_noisy())
        self.assertLessEqual(perceptual.hamming(a, b), 3)

    def test_different_images_are_far(self):
        a = perceptual.phash_frame(_gradient())
        b = perceptual.phash_frame(_solid(0))
        self.assertGreater(perceptual.hamming(a, b), 3)


class TestChunks(unittest.TestCase):
    def test_splits_into_four_16_bit_values(self):
        h = 0x1122334455667788
        self.assertEqual(perceptual.chunks(h), (0x1122, 0x3344, 0x5566, 0x7788))

    def test_all_chunks_in_range(self):
        for c in perceptual.chunks((1 << 64) - 1):
            self.assertEqual(c, 0xFFFF)


class TestHamming(unittest.TestCase):
    def test_identical_is_zero(self):
        self.assertEqual(perceptual.hamming(0xDEADBEEF, 0xDEADBEEF), 0)

    def test_counts_differing_bits(self):
        self.assertEqual(perceptual.hamming(0b1011, 0b1000), 2)


class TestHex(unittest.TestCase):
    def test_roundtrip(self):
        h = 0x0123456789ABCDEF
        self.assertEqual(perceptual.to_hex(h), '0123456789abcdef')
        self.assertEqual(perceptual.from_hex(perceptual.to_hex(h)), h)

    def test_always_16_chars(self):
        self.assertEqual(len(perceptual.to_hex(1)), 16)


class TestMatch(unittest.TestCase):
    def test_identical_multiframe_matches(self):
        frames = [1 << i for i in range(5)]
        self.assertTrue(perceptual.match(frames, list(frames), 3))

    def test_unrelated_multiframe_does_not_match(self):
        a = [0x0000000000000000] * 1
        b = [0xFFFFFFFFFFFFFFFF] * 1
        self.assertFalse(perceptual.match(a, b, 3))

    def test_one_b_frame_cannot_satisfy_three_a_frames(self):
        """The degeneracy guard: distinct pairings are required."""
        shared = 0x00000000000000FF
        a = [shared, shared ^ 0b1, shared ^ 0b10, 0x1234567890ABCDEF, 0x0FEDCBA987654321]
        b = [shared]
        # min(5, 1) == 1 so one accepted pair suffices here, and the single
        # b-frame may only be consumed once.
        self.assertTrue(perceptual.match(a, b, 3))
        # But two five-frame files sharing only one near-identical frame must not match.
        a2 = [shared, 0x1111111111111111, 0x2222222222222222,
              0x4444444444444444, 0x8888888888888888]
        b2 = [shared ^ 0b1, 0x0F0F0F0F0F0F0F0F, 0x00FF00FF00FF00FF,
              0xF0F0F0F0F0F0F0F0, 0xFF00FF00FF00FF00]
        self.assertFalse(perceptual.match(a2, b2, 3))

    def test_single_frame_files_use_tighter_threshold(self):
        a = [0b111]
        b = [0b000]
        # hamming == 3; allowed at distance 3 for multiframe, but single-frame
        # files tighten to max(1, 3-1) == 2.
        self.assertFalse(perceptual.match(a, b, 3))

    def test_empty_never_matches(self):
        self.assertFalse(perceptual.match([], [1], 3))
        self.assertFalse(perceptual.match([1], [], 3))


class TestSupersedes(unittest.TestCase):
    def _meta(self, w, h, d):
        return perceptual.MediaMeta(frames=[1], width=w, height=h, duration=d)

    def test_higher_res_equal_duration_wins(self):
        self.assertTrue(perceptual.supersedes(self._meta(1920, 1080, 12.0),
                                              self._meta(1280, 720, 12.0)))

    def test_higher_res_longer_duration_wins(self):
        self.assertTrue(perceptual.supersedes(self._meta(1920, 1080, 20.0),
                                              self._meta(1280, 720, 12.0)))

    def test_higher_res_shorter_duration_loses(self):
        self.assertFalse(perceptual.supersedes(self._meta(1920, 1080, 5.0),
                                               self._meta(1280, 720, 12.0)))

    def test_lower_res_loses(self):
        self.assertFalse(perceptual.supersedes(self._meta(640, 480, 99.0),
                                               self._meta(1280, 720, 12.0)))

    def test_tie_loses(self):
        self.assertFalse(perceptual.supersedes(self._meta(1280, 720, 12.0),
                                               self._meta(1280, 720, 12.0)))

    def test_stills_compare_on_resolution_alone(self):
        self.assertTrue(perceptual.supersedes(self._meta(800, 600, 0.0),
                                              self._meta(400, 300, 0.0)))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest discover -s tests -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'inb4404.perceptual'`

- [ ] **Step 3: Write the implementation**

Create `inb4404/perceptual.py`:

```python
"""Perceptual hashing (pHash) of media via ffmpeg-decoded frames.

This module is the only place ffmpeg is probed or invoked. It has no
dependency on the database or the network, so the hashing logic can be
tested in isolation.
"""
import logging
import math
import re
import subprocess
from typing import List, Optional, Tuple

log = logging.getLogger('inb4404')

# Frame geometry. ffmpeg scales every frame to _SIZE x _SIZE 8-bit grayscale,
# so one frame is exactly _SIZE * _SIZE raw bytes.
_SIZE = 32
_LOW = 8               # top-left DCT block edge; _LOW ** 2 == 64 bits
_FRAME_BYTES = _SIZE * _SIZE

# Sampling: take a wide sample, deduplicate, then cap. Deduplicating a narrow
# sample would prematurely collapse videos that genuinely have motion.
_SAMPLE_FRAMES = 12
_KEEP_FRAMES = 5

_FFMPEG_TIMEOUT = 120

_RE_DIMS = re.compile(r'Stream #\d+:\d+.*?: Video:.*?, (\d+)x(\d+)')
_RE_DURATION = re.compile(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)')


def _build_basis() -> List[List[float]]:
    """Build the truncated DCT-II basis.

    Only the first `_LOW` rows of the full `_SIZE x _SIZE` basis are needed:
    for the separable 2D DCT, `full = B @ P @ B.T`, so `B[:8] @ P @ B[:8].T`
    is exactly rows and columns 0-7 of that transform rather than an
    approximation. That cuts the work from ~65k multiply-adds to ~10k, which
    is what makes a pure-Python DCT fast enough to avoid numpy.

    Returns:
        A `_LOW x _SIZE` matrix of basis coefficients.
    """
    basis = []
    for k in range(_LOW):
        scale = math.sqrt(1.0 / _SIZE) if k == 0 else math.sqrt(2.0 / _SIZE)
        basis.append([
            scale * math.cos(math.pi * (2 * n + 1) * k / (2.0 * _SIZE))
            for n in range(_SIZE)
        ])
    return basis


_BASIS = _build_basis()


def phash_frame(gray: bytes) -> int:
    """Compute the 64-bit perceptual hash of one grayscale frame.

    Args:
        gray: Exactly `_FRAME_BYTES` raw 8-bit grayscale pixels, row-major.

    Returns:
        The 64-bit hash as an int, bit 63 corresponding to DCT coefficient 0.

    Raises:
        ValueError: If `gray` is not exactly `_FRAME_BYTES` long.
    """
    if len(gray) != _FRAME_BYTES:
        raise ValueError(
            'expected %d bytes per frame, got %d' % (_FRAME_BYTES, len(gray))
        )

    pixels = [
        [float(b) for b in bytearray(gray[y * _SIZE:(y + 1) * _SIZE])]
        for y in range(_SIZE)
    ]

    # temp = B @ P  ->  _LOW x _SIZE
    temp = []
    for brow in _BASIS:
        temp.append([
            sum(brow[n] * pixels[n][j] for n in range(_SIZE))
            for j in range(_SIZE)
        ])

    # block = temp @ B.T  ->  _LOW x _LOW
    coeffs = []
    for trow in temp:
        for brow in _BASIS:
            coeffs.append(sum(trow[j] * brow[j] for j in range(_SIZE)))

    ordered = sorted(coeffs)
    median = (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2.0

    value = 0
    for c in coeffs:
        value = (value << 1) | (1 if c > median else 0)
    return value


def to_hex(h: int) -> str:
    """Render a 64-bit hash as 16 lowercase hex characters."""
    return '%016x' % h


def from_hex(s: str) -> int:
    """Parse a 16-character hex hash back into an int."""
    return int(s, 16)


def chunks(h: int) -> Tuple[int, int, int, int]:
    """Split a 64-bit hash into four 16-bit band values.

    Two hashes within Hamming distance 3 must agree exactly on at least one
    of these four chunks (pigeonhole), which is what makes the indexed SQL
    band probe a complete candidate generator.

    Args:
        h: The 64-bit hash.

    Returns:
        A 4-tuple of 16-bit ints, most significant first.
    """
    return ((h >> 48) & 0xFFFF, (h >> 32) & 0xFFFF,
            (h >> 16) & 0xFFFF, h & 0xFFFF)


def hamming(a: int, b: int) -> int:
    """Return the number of differing bits between two hashes."""
    return bin(a ^ b).count('1')


class MediaMeta(object):
    """Perceptual hashes and comparison metadata for one media file."""

    __slots__ = ('frames', 'width', 'height', 'duration')

    def __init__(self, frames: List[int], width: int, height: int,
                 duration: float):
        """Initialize the metadata.

        Args:
            frames: Distinct 64-bit frame hashes, in sampling order.
            width: Frame width in pixels.
            height: Frame height in pixels.
            duration: Duration in seconds; 0.0 for still images.
        """
        self.frames = frames
        self.width = width
        self.height = height
        self.duration = duration

    @property
    def pixels(self) -> int:
        """Total pixel count of one frame."""
        return self.width * self.height


def supersedes(new: MediaMeta, old: MediaMeta) -> bool:
    """Report whether `new` should replace `old` as the kept file.

    Higher resolution wins, but only when duration is not lost. The `>=`
    guard blocks a high-resolution clip that is a trimmed excerpt of a
    longer video, while still letting a 1080p re-encode beat its 720p twin
    at equal duration. Still images carry duration 0.0 on both sides, so the
    rule degenerates to resolution alone. Ties keep the held file.

    Args:
        new: Metadata for the incoming file.
        old: Metadata for the file already held.

    Returns:
        True when the incoming file wins.
    """
    return new.pixels > old.pixels and new.duration >= old.duration


def match(a: List[int], b: List[int], distance: int) -> bool:
    """Report whether two frame-hash lists describe near-duplicate media.

    Uses greedy maximum matching rather than a per-frame existence test. A
    naive "k frames of A each find some frame of B nearby" is degenerate,
    because all k can match the *same* frame of B; on an archive of
    near-static loops that makes every video match every other one. Here
    each frame may be consumed at most once.

    The threshold tightens when either side contributes a single frame: one
    frame is only 64 bits of evidence, and low-detail content produces
    low-entropy coefficients whose hashes cluster.

    Args:
        a: Distinct frame hashes of the first file.
        b: Distinct frame hashes of the second file.
        distance: Maximum Hamming distance for a frame pair.

    Returns:
        True when the two files are near-duplicates.
    """
    if not a or not b:
        return False

    smaller = min(len(a), len(b))
    needed = max(1, (smaller + 1) // 2)
    if len(a) >= 2 and len(b) >= 2:
        threshold = distance
    else:
        threshold = max(1, distance - 1)

    pairs = []
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            d = hamming(x, y)
            if d <= threshold:
                pairs.append((d, i, j))
    pairs.sort()

    used_a = set()
    used_b = set()
    accepted = 0
    for _, i, j in pairs:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        accepted += 1
        if accepted >= needed:
            return True
    return accepted >= needed


def ffmpeg_available() -> bool:
    """Report whether the ffmpeg binary can be invoked.

    Returns:
        True when `ffmpeg -version` succeeds.
    """
    try:
        proc = subprocess.run(
            ['ffmpeg', '-version'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15
        )
        return proc.returncode == 0
    except Exception:
        return False


def _parse_stderr(text: str) -> Tuple[int, int, float]:
    """Extract width, height and duration from ffmpeg's diagnostics.

    Args:
        text: The decoded stderr of an ffmpeg run.

    Returns:
        A tuple of (width, height, duration_seconds). Missing values are 0.
    """
    width = height = 0
    dims = _RE_DIMS.search(text)
    if dims:
        width = int(dims.group(1))
        height = int(dims.group(2))

    duration = 0.0
    dur = _RE_DURATION.search(text)
    if dur:
        duration = (int(dur.group(1)) * 3600 +
                    int(dur.group(2)) * 60 +
                    float(dur.group(3)))
    return width, height, duration


def _select(count: int, wanted: int) -> List[int]:
    """Pick `wanted` evenly-spaced indices out of `count` items."""
    if count <= wanted:
        return list(range(count))
    if wanted == 1:
        return [0]
    step = (count - 1) / float(wanted - 1)
    return [int(round(i * step)) for i in range(wanted)]


def extract(path: str) -> Optional[MediaMeta]:
    """Decode a media file and compute its perceptual frame hashes.

    Decodes keyframes only, scaled to grayscale `_SIZE x _SIZE`, so cost
    stays bounded on long videos. Still images yield exactly one frame from
    the same command. Identical frame hashes are deduplicated before the
    list is capped, so a near-static loop collapses to one entry rather than
    flooding the band index.

    Args:
        path: Absolute path to the media file.

    Returns:
        A `MediaMeta`, or None when ffmpeg fails or produces no frames.
    """
    cmd = [
        'ffmpeg', '-v', 'info', '-skip_frame', 'nokey', '-i', path,
        '-vf', 'scale=%d:%d' % (_SIZE, _SIZE), '-fps_mode', 'passthrough',
        '-pix_fmt', 'gray', '-f', 'rawvideo', '-'
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_FFMPEG_TIMEOUT
        )
    except Exception as e:
        log.warning('ffmpeg failed for %s: %s', path, e)
        return None

    raw = proc.stdout or b''
    total = len(raw) // _FRAME_BYTES
    if total == 0:
        log.warning('ffmpeg produced no frames for %s', path)
        return None

    width, height, duration = _parse_stderr(
        (proc.stderr or b'').decode('utf-8', 'replace')
    )

    frames = []
    seen = set()
    for idx in _select(total, _SAMPLE_FRAMES):
        chunk = raw[idx * _FRAME_BYTES:(idx + 1) * _FRAME_BYTES]
        try:
            h = phash_frame(chunk)
        except ValueError:
            continue
        if h in seen:
            continue
        seen.add(h)
        frames.append(h)
        if len(frames) >= _KEEP_FRAMES:
            break

    if not frames:
        return None
    return MediaMeta(frames=frames, width=width, height=height,
                     duration=duration)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all tests in `tests/test_perceptual.py`.

- [ ] **Step 5: Verify against a real file**

Run:

```bash
python -c "from inb4404 import perceptual; m = perceptual.extract('downloads/g/example.webm'); print(len(m.frames), m.width, m.height, m.duration)"
```

Expected: a small frame count (1-5), non-zero dimensions, non-zero duration.

- [ ] **Step 6: Commit**

```bash
git add inb4404/perceptual.py tests/__init__.py tests/test_perceptual.py
git commit -m "feat: add perceptual hashing core with truncated-basis DCT"
```

---

### Task 2: Database schema and access

**Files:**
- Modify: `inb4404/database.py` (extend `HashDB.init()`, add methods)
- Test: `tests/test_database_phash.py`

**Interfaces:**
- Consumes: `perceptual.chunks`, `perceptual.to_hex`, `perceptual.from_hex`, `perceptual.MediaMeta` from Task 1.
- Produces, all on `HashDB`:
  - `bulk_session()` — context manager yielding an open `sqlite3.Connection`
  - `record_phash(path: str, meta: MediaMeta, conn=None) -> None`
  - `get_phash(path: str, conn=None) -> Optional[MediaMeta]`
  - `find_phash_candidates(frame_chunks: List[Tuple[int, int, int, int]], exclude_path: str, conn=None) -> List[str]`
  - `move_phash_path(old_path: str, new_path: str, conn=None) -> None`
  - `delete_phash(path: str, conn=None) -> None`
  - `has_phash(path: str, conn=None) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_database_phash.py`:

```python
"""Tests for the phash_frames table and its accessors."""
import os
import shutil
import tempfile
import unittest

from inb4404 import perceptual
from inb4404.database import HashDB


def meta(frames, width=100, height=100, duration=1.0):
    return perceptual.MediaMeta(frames=frames, width=width, height=height,
                                duration=duration)


class TestPhashStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = HashDB(db_path=os.path.join(self.tmp, 'test.db'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_is_idempotent(self):
        self.db.init()
        self.db.init()
        self.assertFalse(self.db.has_phash('/nope'))

    def test_record_and_get_roundtrip(self):
        m = meta([1, 2, 3], width=640, height=480, duration=12.5)
        self.db.record_phash('/a.webm', m)
        got = self.db.get_phash('/a.webm')
        self.assertEqual(got.frames, [1, 2, 3])
        self.assertEqual(got.width, 640)
        self.assertEqual(got.height, 480)
        self.assertAlmostEqual(got.duration, 12.5)

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.db.get_phash('/missing.webm'))

    def test_record_replaces_previous_rows(self):
        self.db.record_phash('/a.webm', meta([1, 2, 3]))
        self.db.record_phash('/a.webm', meta([9]))
        self.assertEqual(self.db.get_phash('/a.webm').frames, [9])

    def test_has_phash(self):
        self.assertFalse(self.db.has_phash('/a.webm'))
        self.db.record_phash('/a.webm', meta([1]))
        self.assertTrue(self.db.has_phash('/a.webm'))

    def test_frame_order_is_preserved(self):
        self.db.record_phash('/a.webm', meta([30, 10, 20]))
        self.assertEqual(self.db.get_phash('/a.webm').frames, [30, 10, 20])


class TestBandProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = HashDB(db_path=os.path.join(self.tmp, 'test.db'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_exact_band_match(self):
        target = 0x1111222233334444
        self.db.record_phash('/held.webm', meta([target]))
        found = self.db.find_phash_candidates(
            [perceptual.chunks(target)], exclude_path='/new.webm')
        self.assertEqual(found, ['/held.webm'])

    def test_finds_hash_differing_by_one_bit(self):
        target = 0x1111222233334444
        self.db.record_phash('/held.webm', meta([target]))
        # Flip one bit in the last chunk; the first three chunks still match.
        found = self.db.find_phash_candidates(
            [perceptual.chunks(target ^ 0b1)], exclude_path='/new.webm')
        self.assertEqual(found, ['/held.webm'])

    def test_excludes_self(self):
        target = 0x1111222233334444
        self.db.record_phash('/new.webm', meta([target]))
        found = self.db.find_phash_candidates(
            [perceptual.chunks(target)], exclude_path='/new.webm')
        self.assertEqual(found, [])

    def test_unrelated_hash_not_returned(self):
        self.db.record_phash('/held.webm', meta([0x1111222233334444]))
        found = self.db.find_phash_candidates(
            [perceptual.chunks(0xAAAABBBBCCCCDDDD)], exclude_path='/new.webm')
        self.assertEqual(found, [])

    def test_deduplicates_paths_across_frames(self):
        a, b = 0x1111222233334444, 0xAAAABBBBCCCCDDDD
        self.db.record_phash('/held.webm', meta([a, b]))
        found = self.db.find_phash_candidates(
            [perceptual.chunks(a), perceptual.chunks(b)],
            exclude_path='/new.webm')
        self.assertEqual(found, ['/held.webm'])


class TestPathMaintenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = HashDB(db_path=os.path.join(self.tmp, 'test.db'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_move_updates_path(self):
        self.db.record_phash('/a.webm', meta([1, 2]))
        self.db.move_phash_path('/a.webm', '/original/a_1.webm')
        self.assertIsNone(self.db.get_phash('/a.webm'))
        self.assertEqual(self.db.get_phash('/original/a_1.webm').frames, [1, 2])

    def test_delete_removes_rows(self):
        self.db.record_phash('/a.webm', meta([1]))
        self.db.delete_phash('/a.webm')
        self.assertIsNone(self.db.get_phash('/a.webm'))

    def test_bulk_session_shares_one_connection(self):
        with self.db.bulk_session() as conn:
            self.db.record_phash('/a.webm', meta([1]), conn=conn)
            self.assertTrue(self.db.has_phash('/a.webm', conn=conn))
        self.assertTrue(self.db.has_phash('/a.webm'))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_database_phash -v`
Expected: FAIL with `AttributeError: 'HashDB' object has no attribute 'record_phash'`

- [ ] **Step 3: Extend the schema**

In `inb4404/database.py`, add the import at the top of the file:

```python
from . import perceptual
```

Then inside `HashDB.init()`, immediately before `conn.commit()`, add:

```python
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
```

- [ ] **Step 4: Add the accessor methods**

Append these methods to the `HashDB` class in `inb4404/database.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest tests.test_database_phash -v`
Expected: PASS.

- [ ] **Step 6: Confirm the existing MD5 path still works**

Run: `python -m unittest discover -s tests -v`
Expected: PASS. Then confirm the migration is non-destructive on a populated DB:

```bash
python -c "from inb4404.database import HashDB; d = HashDB(db_path='hashes.db'); print('md5 rows:', d.count_hashes())"
```

Expected: the same row count as before this task.

- [ ] **Step 7: Commit**

```bash
git add inb4404/database.py tests/test_database_phash.py
git commit -m "feat: add phash_frames table with indexed band columns"
```

---

### Task 3: Configuration and CLI flags

**Files:**
- Modify: `inb4404/config.py`
- Modify: `inb4404/__main__.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `Config.phash_enabled: bool` (default `True`), `Config.phash_distance: int` (default `3`).

- [ ] **Step 1: Add the config fields**

In `inb4404/config.py`, add two fields to the `Config` dataclass alongside the existing ones. Both are plain picklable scalars — `Config` is pickled into child processes under Windows `spawn`, so never add an index, connection, or handle here.

```python
    phash_enabled: bool = True
    phash_distance: int = 3
```

- [ ] **Step 2: Add the CLI flags**

In `inb4404/__main__.py`, next to the existing `--no-subject` argument, add:

```python
    parser.add_argument(
        '--no-phash', dest='phash', action='store_false', default=True,
        help='disable perceptual-hash near-duplicate detection'
    )
    parser.add_argument(
        '--phash-distance', type=int, default=3,
        help='maximum Hamming distance for a near-duplicate frame pair (max 3)'
    )
```

- [ ] **Step 3: Wire them into `create_config_from_args`**

In the same file, inside `create_config_from_args`, pass the parsed values through and clamp the distance. The band probe is only complete up to distance 3 by pigeonhole; a larger value would silently miss pairs, so refuse it loudly rather than returning wrong answers.

```python
    phash_distance = args.phash_distance
    if phash_distance > 3:
        log.warning(
            'phash distance %d exceeds the indexed maximum of 3; clamping to 3',
            phash_distance
        )
        phash_distance = 3
    if phash_distance < 1:
        phash_distance = 1
```

Then include `phash_enabled=args.phash, phash_distance=phash_distance` in the `Config(...)` construction.

- [ ] **Step 4: Probe ffmpeg and disable when missing**

In `inb4404/__main__.py`, mirroring how `--title` is silently disabled when its imports are absent, add after the config is built:

```python
    if config.phash_enabled:
        from .perceptual import ffmpeg_available
        if not ffmpeg_available():
            log.warning(
                'ffmpeg not found on PATH; perceptual-hash '
                'near-duplicate detection is disabled'
            )
            config.phash_enabled = False
```

- [ ] **Step 5: Verify the flags parse**

Run:

```bash
python inb4404.py --help
```

Expected: `--no-phash` and `--phash-distance` both listed.

- [ ] **Step 6: Commit**

```bash
git add inb4404/config.py inb4404/__main__.py
git commit -m "feat: add --no-phash and --phash-distance flags"
```

---

### Task 4: Near-duplicate resolution

**Files:**
- Create: `inb4404/near_dupe.py`
- Test: `tests/test_near_dupe.py`

**Interfaces:**
- Consumes: `perceptual.extract`, `perceptual.match`, `perceptual.supersedes`, `perceptual.chunks`, `MediaMeta` (Task 1); `HashDB.find_phash_candidates`, `get_phash`, `record_phash`, `move_phash_path` (Task 2); `Config.phash_distance` (Task 3).
- Produces:
  - `ORIGINAL_DIR = 'original'`
  - `relocate(path: str) -> Optional[str]` — move a file into its sibling `original/` with the smallest free `_N` suffix, returning the new path
  - `class NearDupeResolver` with `check(path, meta, allow_foreign_moves, conn=None) -> Optional[str]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_near_dupe.py`:

```python
"""Tests for near-duplicate resolution and relocation."""
import os
import shutil
import tempfile
import unittest

from inb4404 import perceptual
from inb4404.database import HashDB
from inb4404.near_dupe import ORIGINAL_DIR, NearDupeResolver, relocate


def meta(frames, width=100, height=100, duration=1.0):
    return perceptual.MediaMeta(frames=frames, width=width, height=height,
                                duration=duration)


class TestRelocate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, name):
        p = os.path.join(self.tmp, name)
        with open(p, 'wb') as fh:
            fh.write(b'x')
        return p

    def test_moves_into_original_with_suffix(self):
        src = self._touch('clip.webm')
        dest = relocate(src)
        self.assertEqual(
            dest, os.path.join(self.tmp, ORIGINAL_DIR, 'clip_1.webm'))
        self.assertTrue(os.path.isfile(dest))
        self.assertFalse(os.path.exists(src))

    def test_increments_suffix_on_collision(self):
        relocate(self._touch('clip.webm'))
        dest = relocate(self._touch('clip.webm'))
        self.assertEqual(
            dest, os.path.join(self.tmp, ORIGINAL_DIR, 'clip_2.webm'))

    def test_missing_file_returns_none(self):
        self.assertIsNone(relocate(os.path.join(self.tmp, 'gone.webm')))


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = HashDB(db_path=os.path.join(self.tmp, 'test.db'))
        self.resolver = NearDupeResolver(self.db, distance=3, verbose=False)
        self.thread = os.path.join(self.tmp, 'downloads', 'g', '1234')
        os.makedirs(self.thread)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, directory, name):
        os.makedirs(directory, exist_ok=True)
        p = os.path.join(directory, name)
        with open(p, 'wb') as fh:
            fh.write(b'x')
        return p

    def test_no_candidates_returns_none(self):
        p = self._make(self.thread, 'new.webm')
        self.assertIsNone(self.resolver.check(p, meta([0x1234])))

    def test_incoming_loser_is_relocated(self):
        held = self._make(self.thread, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=1920, height=1080))
        new = self._make(self.thread, 'new.webm')
        moved = self.resolver.check(new, meta([0x1234], width=640, height=480))
        self.assertEqual(
            moved, os.path.join(self.thread, ORIGINAL_DIR, 'new_1.webm'))
        self.assertTrue(os.path.isfile(held))
        self.assertFalse(os.path.exists(new))

    def test_incoming_winner_relocates_held_file(self):
        held = self._make(self.thread, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=640, height=480))
        new = self._make(self.thread, 'new.webm')
        moved = self.resolver.check(new, meta([0x1234], width=1920, height=1080))
        self.assertIsNone(moved)
        self.assertTrue(os.path.isfile(new))
        self.assertFalse(os.path.exists(held))
        self.assertTrue(os.path.isfile(
            os.path.join(self.thread, ORIGINAL_DIR, 'held_1.webm')))

    def test_cross_thread_winner_moves_nothing_without_permission(self):
        other = os.path.join(self.tmp, 'downloads', 'g', '9999')
        held = self._make(other, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=640, height=480))
        new = self._make(self.thread, 'new.webm')
        moved = self.resolver.check(new, meta([0x1234], width=1920, height=1080),
                                    allow_foreign_moves=False)
        self.assertIsNone(moved)
        self.assertTrue(os.path.isfile(held))
        self.assertTrue(os.path.isfile(new))

    def test_cross_thread_loser_moves_itself(self):
        other = os.path.join(self.tmp, 'downloads', 'g', '9999')
        held = self._make(other, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=1920, height=1080))
        new = self._make(self.thread, 'new.webm')
        moved = self.resolver.check(new, meta([0x1234], width=640, height=480),
                                    allow_foreign_moves=False)
        self.assertEqual(
            moved, os.path.join(self.thread, ORIGINAL_DIR, 'new_1.webm'))
        self.assertTrue(os.path.isfile(held))

    def test_cross_thread_winner_moves_held_when_permitted(self):
        other = os.path.join(self.tmp, 'downloads', 'g', '9999')
        held = self._make(other, 'held.webm')
        self.db.record_phash(held, meta([0x1234], width=640, height=480))
        new = self._make(self.thread, 'new.webm')
        self.resolver.check(new, meta([0x1234], width=1920, height=1080),
                            allow_foreign_moves=True)
        self.assertFalse(os.path.exists(held))
        self.assertTrue(os.path.isfile(
            os.path.join(other, ORIGINAL_DIR, 'held_1.webm')))

    def test_stale_candidate_row_is_dropped(self):
        ghost = os.path.join(self.thread, 'ghost.webm')
        self.db.record_phash(ghost, meta([0x1234]))
        new = self._make(self.thread, 'new.webm')
        self.assertIsNone(self.resolver.check(new, meta([0x1234])))
        self.assertIsNone(self.db.get_phash(ghost))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_near_dupe -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'inb4404.near_dupe'`

- [ ] **Step 3: Write the implementation**

Create `inb4404/near_dupe.py`:

```python
"""Near-duplicate detection and non-destructive resolution.

Shared by the live watcher and the `--dedupe-downloads` pass so the win
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
                    return None
                moved = relocate(other_path)
                if moved:
                    self.db.move_phash_path(other_path, moved, conn=conn)
                    log.info('Near-dupe: %s supersedes %s -> %s',
                             os.path.basename(path),
                             os.path.basename(other_path), moved)
                return None

            moved = relocate(path)
            if moved:
                self.db.move_phash_path(path, moved, conn=conn)
                log.info('Near-dupe: %s superseded by %s -> %s',
                         os.path.basename(path),
                         os.path.basename(other_path), moved)
            return moved

        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_near_dupe -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add inb4404/near_dupe.py tests/test_near_dupe.py
git commit -m "feat: add near-duplicate resolution with original/ relocation"
```

---

### Task 5: Live watcher integration

**Files:**
- Modify: `inb4404/thread_watcher.py` (`__init__`, `_process_file_entry`, `_scan_directory`)

**Interfaces:**
- Consumes: `perceptual.extract` (Task 1); `HashDB.record_phash`, `has_phash` (Task 2); `Config.phash_enabled`, `phash_distance` (Task 3); `NearDupeResolver` (Task 4).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Construct the resolver**

In `ThreadWatcher.__init__`, after `self.db` is created, add:

```python
        self.resolver = None
        if self.config.phash_enabled:
            from .near_dupe import NearDupeResolver
            self.resolver = NearDupeResolver(
                self.db, self.config.phash_distance, self.config.verbose
            )
```

Built here rather than on `Config` because `Config` is pickled into child processes and must stay a plain dataclass of picklable values.

- [ ] **Step 2: Hash after a successful save**

In `_process_file_entry`, immediately after the existing `self._save_file(img_path, data, data_hash, total, count)` call and its `count += 1`, add:

```python
            self._phash_new_file(img_path)
```

MD5 already returned early for any duplicate, so reaching this line means the file survived both MD5 checks — that is the short-circuit the design depends on, and no ffmpeg process is spawned for a file MD5 rejected.

- [ ] **Step 3: Add the helper**

Add this method to `ThreadWatcher`:

```python
    def _phash_new_file(self, img_path: str) -> None:
        """Compute and record the perceptual hash of a newly saved file.

        Runs only for files that survived both MD5 checks. Any failure is
        logged and swallowed -- a watcher must never die because ffmpeg
        misbehaved on one file.

        Args:
            img_path: Absolute path to the file just written.
        """
        if not self.resolver:
            return
        from . import perceptual
        try:
            meta = perceptual.extract(img_path)
            if meta is None:
                return
            self.db.record_phash(img_path, meta)
            # Foreign moves stay off here: a held file in another thread
            # directory may belong to a sibling watcher process.
            self.resolver.check(img_path, meta, allow_foreign_moves=False)
        except Exception as e:
            log.warning('Perceptual hashing failed for %s: %s', img_path, e)
```

- [ ] **Step 4: Skip `original/` during the startup scan**

In `_scan_directory`, skip any directory named `original` so resolved duplicates are not re-examined on every restart. Inside the `os.walk` loop add, before files are processed:

```python
            dirs[:] = [d for d in dirs if d != 'original']
```

If `_scan_directory` does not use `os.walk`, filter the listing equivalently so entries under `original/` are ignored.

- [ ] **Step 5: Verify MD5 short-circuits phash**

Run a thread you have already downloaded in full, with `--verbose`:

```bash
python inb4404.py https://boards.4chan.org/g/thread/<id> --verbose
```

Expected: every file reports as an MD5 duplicate and **no** ffmpeg process appears in Task Manager. Stop with Ctrl+C.

- [ ] **Step 6: Verify a real near-duplicate resolves**

Take a webm already in a thread directory, produce a smaller re-encode, and confirm the loser lands in `original/`:

```bash
ffmpeg -i downloads/g/<thread>/<file>.webm -vf scale=iw/2:ih/2 -c:v libvpx-vp9 -b:v 200k /tmp/small.webm
```

Copy `small.webm` into the thread directory, run `--dedupe-downloads`, and expect `original/small_1.webm` to appear while the original file stays put.

- [ ] **Step 7: Commit**

```bash
git add inb4404/thread_watcher.py
git commit -m "feat: hash and resolve near-duplicates on download"
```

---

### Task 6: --dedupe-downloads integration

**Files:**
- Modify: `inb4404/deduplicator.py` (`__init__`, `scan_directory`, `run`)

**Interfaces:**
- Consumes: `perceptual.extract` (Task 1); `HashDB.bulk_session`, `has_phash`, `record_phash` (Task 2); `Config.phash_enabled`, `phash_distance` (Task 3); `NearDupeResolver` (Task 4).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Skip `original/` in the walk**

In `Deduplicator.scan_directory`, inside the `os.walk` loop and before files are processed, add:

```python
            dirs[:] = [d for d in dirs if d != 'original']
```

Resolved duplicates live there; walking them would re-detect every pair on every run.

- [ ] **Step 2: Add the perceptual pass**

Add this method to `Deduplicator`:

```python
    def run_phash_pass(self) -> tuple:
        """Compute missing perceptual hashes and resolve near-duplicates.

        Runs inside one `bulk_session`, which is safe only because
        `--dedupe-downloads` returns before any watcher process starts.
        Cross-thread relocation is enabled for the same reason: this is the
        one context with no concurrent writer.

        Returns:
            A tuple of (hashed_count, resolved_count).
        """
        from . import perceptual
        from .near_dupe import NearDupeResolver

        if not self.config.phash_enabled:
            return (0, 0)

        resolver = NearDupeResolver(
            self.db, self.config.phash_distance, self.config.verbose
        )
        hashed = 0
        resolved = 0

        with self.db.bulk_session() as conn:
            for root, dirs, files in os.walk(self.downloads_root):
                dirs[:] = [d for d in dirs if d != 'original']
                for fn in files:
                    if fn == '.hashes.txt':
                        continue
                    full_path = os.path.join(root, fn)
                    if not os.path.isfile(full_path):
                        continue

                    meta = self.db.get_phash(full_path, conn=conn)
                    if meta is None:
                        meta = perceptual.extract(full_path)
                        if meta is None:
                            continue
                        self.db.record_phash(full_path, meta, conn=conn)
                        hashed += 1
                        if self.config.verbose:
                            log.info('Perceptual hash: %s (%d frames)',
                                     full_path, len(meta.frames))

                    moved = resolver.check(full_path, meta,
                                           allow_foreign_moves=True, conn=conn)
                    if moved:
                        resolved += 1

        return (hashed, resolved)
```

- [ ] **Step 3: Call it from `run`**

In `Deduplicator.run`, after the existing `log.info(f'Dedupe complete. ...')` line and before `self.remove_legacy_files()`, add:

```python
        if self.config.phash_enabled:
            hashed, resolved = self.run_phash_pass()
            log.info(
                'Perceptual pass complete. Hashed %d new files, '
                'relocated %d near-duplicates', hashed, resolved
            )
```

Placed after the MD5 pass on purpose: exact duplicates are removed first, so the perceptual pass never wastes an ffmpeg spawn on a file MD5 was about to delete.

- [ ] **Step 4: Verify the cache path**

Run the pass twice:

```bash
python inb4404.py --dedupe-downloads --verbose
```

Expected on the first run: a non-zero "Hashed N new files" count. On the second run: `Hashed 0 new files, relocated 0 near-duplicates`, proving both the stored-hash reuse and the `original/` exclusion work.

- [ ] **Step 5: Verify graceful degradation**

Temporarily rename ffmpeg off PATH and re-run:

```bash
python inb4404.py --dedupe-downloads
```

Expected: one warning that ffmpeg was not found, the MD5 dedupe completes normally, and the perceptual pass is skipped. Restore ffmpeg afterwards.

- [ ] **Step 6: Commit**

```bash
git add inb4404/deduplicator.py
git commit -m "feat: compute and resolve perceptual hashes in --dedupe-downloads"
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `inb4404/__init__.py` (version bump)

**Interfaces:**
- Consumes: the finished behaviour of Tasks 1-6.
- Produces: nothing.

- [ ] **Step 1: Document the feature in README.md**

Add a section describing: what a perceptual hash catches that MD5 does not; that MD5 runs first and short-circuits it; that ffmpeg is optional and its absence disables the feature; that nothing is deleted and losers go to `<thread>/original/`; the win rule (higher resolution, duration not shorter); and the `--no-phash` / `--phash-distance` flags.

State plainly that MD5 saves bandwidth while perceptual hashing only saves disk, since every file reaching it has already been downloaded in full.

- [ ] **Step 2: Update CLAUDE.md**

Amend two claims that this work makes stale:

- The "no test suite" line — there is now `tests/`, run with `python -m unittest discover -s tests -v`.
- The deduplication section — add perceptual hashing as a fifth, post-download layer, note the `phash_frames` table and its indexed band columns, and record that the band probe is only complete to distance 3 by pigeonhole.

- [ ] **Step 3: Bump the version**

In `inb4404/__init__.py`, increment `__version__` for the release.

- [ ] **Step 4: Run the full suite once more**

Run: `python -m unittest discover -s tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md inb4404/__init__.py
git commit -m "docs: document perceptual near-duplicate detection"
```

---

## Worker Assignment

Disjoint file scopes, so no two workers touch the same file:

| Worker | Tasks | Files |
|---|---|---|
| A (agy) | 1 | `inb4404/perceptual.py`, `tests/test_perceptual.py`, `tests/__init__.py` |
| B (agy) | 2 | `inb4404/database.py`, `tests/test_database_phash.py` |
| C (agy) | 3 | `inb4404/config.py`, `inb4404/__main__.py` |
| Supervisor | 4, 5, 6, 7 | `inb4404/near_dupe.py`, `inb4404/thread_watcher.py`, `inb4404/deduplicator.py`, docs |

Task 1 carries the highest defect risk: the truncated-basis DCT and the greedy matcher both produce plausible-looking wrong code, and only the tests catch it. Verify Worker A's output by running `tests/test_perceptual.py` personally, not by trusting the exit code.

Tasks 5 and 6 depend on 1-4 landing first. Task 3 is independent and can run in parallel with 1 and 2.
