# Perceptual-hash near-duplicate detection

Date: 2026-07-31
Status: design, pending implementation

## Summary

Add a perceptual-hash (pHash) layer that detects near-duplicate media — re-encodes, resizes,
requantisations — which MD5 cannot see. It runs **after** MD5 has had its say, never replaces
it, and never deletes anything: the weaker of a near-duplicate pair is renamed and moved to an
`original/` folder beside its thread.

The feature is **forward-looking**. It computes hashes for files as they arrive and for files
present when `--dedupe-downloads` runs. It does not attempt to reconstruct hashes for database
rows whose media is gone.

## Why pHash cannot replace MD5

The 4chan JSON API supplies each post's MD5 as base64. `_process_file_entry`
(`thread_watcher.py:350-359`) checks it against the DB **before** calling
`http_client.fetch()`, so an exact repost costs zero bytes. A perceptual hash requires decoded
pixels, so it can never occupy that pre-download slot.

The practical consequence, worth stating plainly: **MD5 saves bandwidth, pHash can only save
disk.** Every file that gets past MD5 is downloaded in full and costs an ffmpeg invocation
regardless of what pHash then decides.

MD5 is also strictly broader — it covers any byte stream, and its hashes stay valid after the
file is gone, continuing to block re-downloads forever. pHash has neither property.

**MD5 short-circuits pHash.** If either MD5 check skips a file, pHash never runs for it. There
is no ffmpeg spawn, no DB write, nothing.

## Scope

**In scope**
- pHash computed for each newly downloaded file that survives both MD5 checks.
- pHash computed during `--dedupe-downloads` for any on-disk file whose row lacks one.
- Global near-duplicate lookup across every thread, via an indexed SQL band probe.
- Resolution of a detected pair by moving the loser to `<thread dir>/original/`.

**Out of scope**
- Deleting media. Nothing in this feature calls `os.remove` on a media file.
- Any change to MD5 behaviour, to the four existing MD5 checkpoints, or to
  `Deduplicator.remove_duplicates`.
- Reconstructing pHashes for rows whose file no longer exists.
- Pruning dead rows.

## Dependency: ffmpeg only

ffmpeg is the sole frame extractor. One command handles jpg, png, gif, webm and mp4, so there
is a single probe, a single code path, one failure mode, and no new Python package — the
project's Python side stays standard-library only.

Degradation: if `ffmpeg -version` fails, log one warning at startup and disable the feature
entirely. Never raise. This mirrors how `__main__.py` probes beautifulsoup4/django and
silently disables `--title`.

## Algorithm

Classic DCT pHash, 64 bits per frame.

1. ffmpeg yields each frame as 32x32 8-bit grayscale (1024 raw bytes).
2. Apply the separable 2D DCT-II using a **truncated basis**. For basis matrix `B` (32x32),
   the full transform is `B @ P @ B.T`; therefore `B[:8] @ P @ B[:8].T` is *exactly* rows and
   columns 0-7 of that transform, not an approximation. Cost is ~10k multiply-adds instead of
   ~65k, which makes pure Python fast enough that numpy is unnecessary. `B` is computed once
   at module import.
3. Take all 64 coefficients of the 8x8 block **including DC**. Median over all 64.
   Bit *i* = 1 when coefficient *i* > median. Emit MSB-first as 16 hex characters.

Including DC matches the reference `imagehash` implementation. DC is a large outlier so its
bit is effectively constant; this costs one bit of entropy and buys an exact 64-bit word,
which the band arithmetic depends on. A 63-bit hash would silently break the 4x16 split.

## Frame extraction

One subprocess per file:

```
ffmpeg -v info -skip_frame nokey -i <path> -vf scale=32:32 -fps_mode passthrough \
       -pix_fmt gray -f rawvideo -
```

- `-skip_frame nokey` decodes keyframes only. Measured over 12 random archive videos: mean
  **0.61 s/file**.
- stdout is N x 1024 bytes; verified to remain an exact multiple of 1024 under `-v info`,
  because ffmpeg writes all diagnostics to stderr.
- **Order of operations matters.** Subsample evenly to at most **12** frames, hash those,
  **deduplicate identical frame hashes**, then keep at most **5**. Deduplicating a 5-frame
  sample instead would prematurely collapse videos that do have motion, discarding the
  corroborating evidence the matcher depends on. Most 4chan webms are short near-static loops
  whose frames genuinely collapse to one hash; without the dedup step a single static video
  floods every band bucket.
- Still images produce exactly one frame from the same command.
- Width, height and duration are parsed from the same run's stderr — the
  `Stream #0:0: Video: ... WxH` and `Duration: HH:MM:SS.ss` lines. Zero extra subprocess
  spawns, no ffprobe dependency. Verified parsing against a real archive file (`1024x576`).
  Still images have no duration; store `0.0`.

Failures (non-zero exit, timeout, unparseable output) log a warning for that file, yield no
pHash, and never propagate — matching the existing policy of swallowing filesystem and DB
errors so a watcher never dies on transient I/O.

## Storage

A new table, so the existing `hashes` queries are untouched. `load_all_metadata()` and
`get_file_metadata()` return `(md5, mtime, size)` and are unpacked positionally
(`deduplicator.py:76`); widening them breaks those call sites.

```sql
CREATE TABLE IF NOT EXISTS phash_frames (
    path     TEXT NOT NULL,
    frame    INTEGER NOT NULL,   -- ordinal within the file
    h        TEXT NOT NULL,      -- 16 hex chars
    b0 INTEGER, b1 INTEGER, b2 INTEGER, b3 INTEGER,
    width    INTEGER,
    height   INTEGER,
    duration REAL,
    PRIMARY KEY (path, frame)
);
CREATE INDEX IF NOT EXISTS idx_phash_b0 ON phash_frames(b0);
CREATE INDEX IF NOT EXISTS idx_phash_b1 ON phash_frames(b1);
CREATE INDEX IF NOT EXISTS idx_phash_b2 ON phash_frames(b2);
CREATE INDEX IF NOT EXISTS idx_phash_b3 ON phash_frames(b3);
```

Created in the existing idempotent `HashDB.init()` migration block. `h` is TEXT rather than
INTEGER because a 64-bit unsigned hash overflows SQLite's signed INTEGER into negatives; the
`b*` chunks are 16-bit and safe as INTEGER.

Dimensions and duration are denormalised onto every frame row. It wastes a few bytes and
avoids a second table and a join for what is always read together.

New `HashDB` methods:

- `record_phash(path, frames, width, height, duration)` — replaces all rows for `path`.
- `find_phash_candidates(chunks, conn=None)` — the band probe, below.
- `get_phash(path, conn=None)` — frames plus metadata for one path.
- `move_phash_path(old_path, new_path, conn=None)` — after a file is relocated.
- `delete_phash(path, conn=None)`
- `bulk_session()` — context manager yielding one open connection.

`bulk_session()` exists because `_get_connection` opens and closes per call. That is correct
for watchers, but a whole-tree `--dedupe-downloads` pass would pay ~0.5 ms per probe per file.
The bulk session is only ever used from `Deduplicator`, which runs before any watcher starts
(`__main__.py:187-191`).

## Matching

**Candidate generation — SQL band probe.** Split each 64-bit frame hash into 4 x 16-bit
chunks stored as `b0..b3`. By pigeonhole, two hashes at Hamming distance <= 3 must agree
exactly on at least one chunk, so:

```sql
SELECT DISTINCT path FROM phash_frames
 WHERE b0 = ? OR b1 = ? OR b2 = ? OR b3 = ?
```

is a complete candidate set for that frame — four indexed probes returning a handful of rows.
Keeping the index in SQLite rather than memory is what makes a *global* lookup viable: no
per-process memory cost, no staleness between sibling watchers, no index object on `Config`
(which is pickled into children under Windows `spawn` and must stay a plain dataclass of
picklable values), and WAL already handles concurrent access.

`--phash-distance` defaults to **3** and is capped at 3. A larger value logs a warning and
falls back to a full scan.

**Acceptance rule — greedy matching, not per-frame existence.** A naive "≥3 frames of A each
find some frame of B within distance 3" is degenerate: all three can match the *same* frame of
B, so on an archive of near-static loops every video matches every other. Instead:

1. Enumerate candidate frame pairs between the two files, sort by Hamming distance ascending.
2. Walk the list accepting a pair only when neither frame is already consumed.
3. Files match iff accepted pairs within the effective threshold reach
   `max(1, ceil(min(len(A), len(B)) / 2))`.

Two 5-frame videos need 3 distinct pairings. A still image against a video needs 1, correctly
catching a webm that is a single still.

**Effective threshold scales with available evidence.** A file collapsing to one distinct
frame carries only 64 bits, and low-detail content produces low-entropy DCT coefficients whose
hashes cluster — a fixed tolerance would flag unrelated near-static webms against each other,
reintroducing exactly the failure the greedy matcher prevents. Therefore:

- both files contribute >= 2 distinct frames -> threshold = `phash_distance` (default 3)
- either file contributes exactly 1 frame -> threshold = `max(1, phash_distance - 1)`

Less corroboration, tighter tolerance. Completeness is unaffected: a tighter acceptance rule
only discards candidates the band probe returned, it never misses pairs.

## Resolution

Nothing is deleted. The **loser** of a pair is renamed and moved aside.

**Win predicate.** The incoming file supersedes the held one iff:

```
new_width * new_height > old_width * old_height  AND  new_duration >= old_duration
```

Higher resolution wins, but only when duration is not lost — the `>=` blocks a high-res clip
that is a trimmed excerpt of a longer video, while still letting a 1080p re-encode of the same
12-second webm beat its 720p twin. Still images have duration `0.0` on both sides, so the rule
degenerates to resolution alone. Any tie leaves the held file as winner.

**Relocation.** The loser moves to `<its thread dir>/original/`, keeping its stem and gaining
the smallest suffix `_1`, `_2`, ... that is free in that folder — e.g.
`downloads/g/1234/orig.webm` -> `downloads/g/1234/original/orig_1.webm`. Both the `hashes` row
and the `phash_frames` rows follow via `move_phash_path`.

**Cross-process safety.** A global match can name a file owned by a different watcher process,
and moving another process's file mid-download is a race.

- **Same thread directory** — the running watcher owns both files. Full resolution applies:
  winner keeps its normal path, loser is relocated.
- **Different thread directory** — the watcher only ever relocates *its own* new file. If the
  new file loses, it moves to its own `original/`. If the new file wins, log the supersession
  and leave both files in place; `--dedupe-downloads` resolves it later.

`--dedupe-downloads` runs single-process before any watcher starts, so it is the one context
where cross-thread relocation is safe, and it performs it.

## Where it runs

**Live watching** — after `_save_file` succeeds, so the file is on disk and ffmpeg reads a real
path. Piping bytes via `pipe:0` was rejected: mp4 with a trailing `moov` atom is not reliably
demuxable from a non-seekable stream. Sequence: extract -> band probe -> greedy match ->
resolve -> `record_phash`.

**`--dedupe-downloads`** — after the existing MD5 pass completes, inside one `bulk_session()`.
For each on-disk file with no `phash_frames` rows, extract and record; then run the same
probe/match/resolve logic, with cross-thread relocation enabled. In `scan_directory`, the
`(mtime, size)` cache-hit branch gains pHash computation when rows are absent, **without
invalidating the MD5 row** — preserving that cache is load-bearing, and invalidating it turns
full rescans from seconds into minutes.

Files under any `original/` folder are skipped by both the walk and the probe. They are
resolved duplicates; re-examining them would re-detect the pair on every run.

## Configuration

`Config` gains two picklable scalars: `phash_enabled: bool = True`, `phash_distance: int = 3`.
No index object is ever attached to `Config`.

CLI: `--no-phash` (mirroring the existing `--no-subject` shape) and `--phash-distance N`.

## New module

`inb4404/perceptual.py` — the only place ffmpeg is invoked or probed.

```
ffmpeg_available()        -> bool
extract(path)             -> FrameSet | None      # frames, width, height, duration
chunks(frame_hash)        -> tuple[int, int, int, int]
hamming(a, b)             -> int
match(frames_a, frames_b, distance) -> bool
supersedes(new_meta, old_meta)      -> bool
```

Isolating it keeps the `thread_watcher.py` and `deduplicator.py` edits thin and makes the
hashing logic testable without a DB or the network.

## Cost

One ffmpeg spawn per newly downloaded file, ~0.6 s measured, on a path that was previously
I/O-only. Candidate lookup is four indexed SQLite probes per frame — negligible beside the
decode. `--dedupe-downloads` pays the same ~0.6 s once per file lacking a pHash, then never
again.

## Verification

The repo has no test suite, linter, or packaging metadata; verification is manual.

1. `python -c "from inb4404 import perceptual; print(perceptual.ffmpeg_available())"` -> True.
2. Hash one file twice; the two frame lists must be identical.
3. Re-encode a webm at lower bitrate, hash both, confirm `match()` is True while MD5 differs —
   precisely the case MD5 misses.
4. Hash two visually unrelated videos, confirm `match()` is False.
5. Confirm two *unrelated* near-static webms are **not** matched. This is the main
   false-positive risk in the design.
6. Downscale a webm to half resolution, feed it as the new file, confirm it loses and lands in
   `original/` with a `_1` suffix while the held file stays put.
7. Upscale-re-encode the same clip at higher resolution and equal duration, confirm it wins and
   the previously held file is the one relocated.
8. Trim a high-resolution clip shorter than the held file, confirm it does **not** win.
9. Confirm a file blocked by either MD5 check spawns no ffmpeg process at all.
10. Run `--dedupe-downloads` twice; the second run must extract zero pHashes and report no new
    pairs, proving both the cache path and the `original/` exclusion work.
11. Rename ffmpeg off PATH, confirm both live watching and `--dedupe-downloads` still run,
    logging one warning and skipping pHash.
12. Run with `--no-phash` and confirm no ffmpeg process is spawned.
