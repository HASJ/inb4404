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
