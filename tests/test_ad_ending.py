"""Check frozen-frame alpha compositing, full video duration, audio and GUI routing."""
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adending
import gui
import render


def ffmpeg(*args):
    return subprocess.run([render.ffmpeg_exe(), "-v", "error", *map(str, args)],
                          check=True, capture_output=True).stdout


def frames(path, width=270, height=480):
    return np.frombuffer(ffmpeg("-i", path, "-map", "0:v", "-f", "rawvideo",
                                "-pix_fmt", "rgb24", "-"), dtype=np.uint8).reshape(-1, height, width, 3)


class AdEndingTests(unittest.TestCase):
    def setUp(self):
        render.clear_cancel()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_full_ending_freezes_last_frame_and_preserves_alpha_and_audio(self):
        body, output = self.root / "body.mp4", self.root / "ending.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=270x480:rate=30:duration=1",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", body)
        info = adending.media_info(adending.bundled_ending())
        adending.append_ending(str(body), adending.bundled_ending(), str(output),
                               body_duration=1, width=270, height=480)
        original, actual = frames(body), frames(output)
        self.assertEqual(len(actual), 30 + round(info["duration"] * 30))
        # A fully transparent first ending frame must display the last body frame.
        self.assertLess(np.abs(actual[30].astype(float) - original[-1]).mean(), 4)
        overlay = np.frombuffer(ffmpeg("-c:v", "libvpx-vp9", "-i", adending.bundled_ending(),
            "-vf", "setpts=PTS-STARTPTS,scale=270:480,format=rgba,fps=30",
            "-f", "rawvideo", "-pix_fmt", "rgba", "-"), dtype=np.uint8).reshape(-1, 480, 270, 4)
        transparent = np.all(overlay[14:17, :, :, 3] == 0, axis=0)
        self.assertGreater(transparent.sum(), 1000)
        self.assertLess(np.abs(actual[45].astype(float) - original[-1])[transparent].mean(), 5)
        self.assertGreater(np.abs(actual[75].astype(float) - original[-1]).mean(), 30)
        audio = np.frombuffer(ffmpeg("-i", output, "-map", "0:a", "-ac", "1", "-ar", "48000",
                                     "-f", "f32le", "-"), dtype=np.float32)
        self.assertLess(np.abs(audio[:40000]).max(), .002)
        self.assertGreater(np.sqrt(np.mean(audio[60000:] ** 2)), .01)

    def test_short_bgm_does_not_truncate_comparison(self):
        a, b = self.root / "a.png", self.root / "b.png"
        Image.new("RGB", (108, 192), "red").save(a)
        Image.new("RGB", (108, 192), "blue").save(b)
        bgm, output = self.root / "short.wav", self.root / "body.mp4"
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=0.1", bgm)
        demo = render.Renderer([(str(a), str(b))], str(output), width=108, height=192,
                               scene_sec=.8, transition="none", reveal="clean",
                               label_before="", label_after="", align_mode="off", audio_path=str(bgm))
        demo.render()
        self.assertEqual(len(frames(output, 108, 192)), demo.total)

    def test_gui_toggle_routes_ending_and_preserves_other_settings(self):
        photo = self.root / "photo.png"
        Image.new("RGB", (32, 32), "red").save(photo)
        settings = self.root / "settings.json"
        settings.write_text(json.dumps({"demo_font": "sukhumvit"}))
        state = dict(gui.STATE, busy=False)
        received, completed = [], threading.Event()
        def worker(pairs, opts):
            received.append(opts)
            state["busy"] = False
            completed.set()
        server = ThreadingHTTPServer(("127.0.0.1", 0), gui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def post(path, payload):
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}",
                      data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            return json.load(opener.open(request))
        try:
            with patch.object(gui, "SETTINGS_PATH", str(settings)), patch.object(gui, "STATE", state), patch.object(gui, "worker", worker):
                for enabled in ("1", ""):
                    completed.clear()
                    self.assertTrue(post("/run", {"pairs": [[str(photo), str(photo)]],
                        "folder": str(self.root), "pair_ending": enabled})["ok"])
                    self.assertTrue(completed.wait(2))
                    self.assertEqual(received[-1]["ending_path"], adending.bundled_ending() if enabled else None)
                    saved = json.loads(settings.read_text())
                    self.assertEqual(saved["demo_font"], "sukhumvit")
                    self.assertEqual(saved["pair_ending"], enabled)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
