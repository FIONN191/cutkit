"""Regression: preview must use the export layout, including portrait geometry."""
import io
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer

from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dragdemo
import gui


class CaptionPreviewTests(unittest.TestCase):
    def test_portrait_keeps_default_title_above_frame(self):
        box = dragdemo.target_box_for_aspect(3 / 4)
        title = dragdemo.build_pill("Upload Your Photo", "plain").getbbox()
        self.assertLess(title[3], box[1])
        self.assertEqual(box[3] - box[1], dragdemo.FIT_MAX_H)

    def test_http_preview_matches_export_frame(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), gui.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                first, second = Path(tmp) / "portrait.png", Path(tmp) / "wide.png"
                Image.new("RGB", (300, 400), "red").save(first)
                Image.new("RGB", (600, 300), "blue").save(second)
                cases = [
                    ("single", "drag", False, "plain", .185, first),
                    ("single", "drag", False, "pill_pink", .12, second),
                    ("single", "slide", False, "plain", .22, first),
                    ("sequence", "drag", False, "plain", .185, first),
                    ("together", "drag", False, "plain", .185, first),
                    ("replace", "drag", True, "plain", .185, first),
                ]
                for mode, motion, transparent, style, y, photo in cases:
                    with self.subTest(mode=mode, motion=motion, transparent=transparent):
                        demo = dragdemo.DragDemo(str(photo), str(Path(tmp) / "out.mp4"),
                            photo2=str(second), upload_mode=mode, motion=motion,
                            transparent=transparent, caption="Upload Your Photo",
                            caption_style=style, caption_y=y, use_transition=False)
                        demo.prepare_scene()
                        expected = demo.frame_at(0)
                        expected.thumbnail((216, 384), Image.Resampling.LANCZOS)
                        query = urllib.parse.urlencode(dict(photo=str(photo), photo2=str(second),
                            upload_mode=mode, motion=motion, transparent=int(transparent),
                            text="Upload Your Photo", style=style, y=y,
                            font=dragdemo.DEFAULT_CAPTION_FONT))
                        with opener.open(f"http://127.0.0.1:{server.server_port}/caption_preview?{query}") as response:
                            actual = Image.open(io.BytesIO(response.read())).convert("RGBA")
                        diff = ImageChops.difference(actual, expected.convert("RGBA"))
                        self.assertTrue(all(high == 0 for low, high in diff.getextrema()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
