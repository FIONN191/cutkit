"""Pixel-level checks for the fixed-image, line-free reveal."""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from render import Renderer


class CleanRevealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Every position has distinct pixels so sliding/stretching the source
        # cannot accidentally pass a test using two solid-colour images.
        y, x = np.mgrid[:320, :180]
        self.before = np.stack((x, y % 256, (x + y) % 256), axis=-1).astype('uint8')
        self.after = 255 - self.before
        paths = [str(Path(self.tmp.name) / name) for name in ('b.png', 'a.png')]
        for pixels, path in zip((self.before, self.after), paths):
            Image.fromarray(pixels).save(path)
        self.pairs = [tuple(paths)]

    def renderer(self, direction='ltr', **kwargs):
        return Renderer(self.pairs, str(Path(self.tmp.name) / 'out.mp4'),
                        width=180, height=320, align_mode='off',
                        label_before='', label_after='', transition='none',
                        direction=direction, **kwargs)

    def test_linear_reveal_preserves_pixels_and_has_no_handle(self):
        for direction in ('ltr', 'rtl'):
            r = self.renderer(direction, reveal='clean')
            for tf, columns in ((0, 0), (.18, 45), (.36, 90), (.54, 135),
                                (.72, 180), (.9, 180), (1, 180)):
                with self.subTest(direction=direction, tf=tf):
                    expected = self.before.copy()
                    if columns:
                        start, end = ((0, columns) if direction == 'ltr'
                                      else (180 - columns, 180))
                        expected[:, start:end] = self.after[:, start:end]
                    np.testing.assert_array_equal(
                        np.asarray(r._scene_frame(0, tf, tf)), expected)

    def test_frame_timing_and_multiple_pairs(self):
        self.pairs *= 2
        for sec in (1, 3.4, 11.2):
            r = self.renderer(reveal='clean', scene_sec=sec)
            self.assertEqual(r.total, round(sec * 30) * 2)
            for offset in (0, r.scene_frames):
                np.testing.assert_array_equal(r.frame_at(offset), self.before)
                for fraction in (.8, 1):
                    n = offset + round((r.scene_frames - 1) * fraction)
                    np.testing.assert_array_equal(r.frame_at(n), self.after)

    def test_legacy_once_still_draws_slider(self):
        r = self.renderer(reveal='once')
        mid = np.asarray(r._scene_frame(0, .5, .5))
        self.assertTrue(np.all(mid[20, 90] > 230))
        np.testing.assert_array_equal(np.asarray(r.frame_at(0))[1:-1, 1:-1],
                                      self.before[1:-1, 1:-1])

    def test_legacy_slider_argument_accepts_new_mode(self):
        r = self.renderer(slider_mode='clean')
        self.assertEqual(r.reveal, 'clean')
        np.testing.assert_array_equal(r.frame_at(r.total - 1), self.after)


if __name__ == '__main__':
    unittest.main()
