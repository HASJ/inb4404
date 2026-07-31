"""Tests for the perceptual hashing core."""
import unittest

from inb4404 import perceptual


def _flat(rows):
    """Flatten a 32x32 list of ints into 1024 raw bytes."""
    return bytes(bytearray([v for row in rows for v in row]))


def _solid(value):
    return _flat([[value] * 32 for _ in range(32)])


def _gradient():
    rows = []
    for y in range(32):
        rows.append([(x * 4 + y * 4) for x in range(32)])
    return _flat(rows)


def _gradient_noisy():
    rows = []
    for y in range(32):
        rows.append([min(255, max(0, (x * 4 + y * 4) + (1 if x % 8 == 0 else 0)))
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
