"""Photo-card geometry, real alpha MOV and shared HTTP preview/export options."""
import base64
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gui
import ringarrow
import render

class PhotoCardTests(unittest.TestCase):
    def test_alpha_card_and_roundtrip_mov(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo, out = Path(tmp)/'photo.png', Path(tmp)/'card.mov'
            Image.new('RGB', (200, 400), 'red').save(photo)
            badge = ringarrow.build_photo_card(photo, 540, 960)
            self.assertEqual(badge.getpixel((0, 0))[3], 0)
            self.assertEqual(badge.getpixel((90, 715)), (255, 0, 0, 255))
            # Rounded corners, white border, white plus badge and dark plus centre.
            self.assertLess(badge.getpixel((24, 627))[3], 5)
            self.assertTrue(all(v > 245 for v in badge.getpixel((90, 628))[:3]))
            self.assertTrue(all(v > 245 for v in badge.getpixel((145, 778))[:3]))
            self.assertTrue(all(v < 90 for v in badge.getpixel((135, 785))[:3]))
            render.clear_cancel()
            ringarrow.render_mov(badge, str(out), dur=.6)
            raw = subprocess.check_output([render.ffmpeg_exe(), '-v', 'error', '-i', str(out),
                '-f', 'rawvideo', '-pix_fmt', 'rgba', '-'])
            frames = np.frombuffer(raw, np.uint8).reshape(-1, 960, 540, 4)
            self.assertEqual(len(frames), 18)
            self.assertEqual(frames[0, 0, 0, 3], 0)
            self.assertGreater(frames[0, 715, 90, 3], 250)
            self.assertLess(np.abs(frames[0].astype(float)-np.array(badge)).mean(), 1)

    def test_http_style_crop_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp)/'wide.png'
            im = Image.new('RGB', (800, 200), 'blue')
            im.paste('red', (0, 0, 400, 200)); im.save(photo)
            state = dict(gui.STATE, busy=False)
            received = []
            done = threading.Event()
            def worker(opts):
                received.append(opts); state['busy'] = False; done.set()
            server = gui.ThreadingHTTPServer(('127.0.0.1',0), gui.Handler)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def post(path, data):
                return json.load(op.open(urllib.request.Request(
                    f'http://127.0.0.1:{server.server_port}'+path,
                    data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})))
            data = dict(photo=str(photo),style='photo_card',W=540,H=960,crop_x=0,crop_y=0)
            try:
                with patch.object(gui,'STATE',state), patch.object(gui,'worker_ring',worker):
                    preview=post('/ring_preview',data)
                    pixels=Image.open(io.BytesIO(base64.b64decode(preview['png'].split(',')[1])))
                    self.assertEqual(pixels.getpixel((60,477)), (255,0,0))
                    self.assertTrue(post('/run_ring',data)['ok']); self.assertTrue(done.wait(2))
                    self.assertEqual(received[0]['crop_x'],0)
                    self.assertEqual(received[0]['crop_y'],0)
                    self.assertEqual(received[0]['cx'],.166)
                    self.assertTrue(received[0]['out'].endswith('-照片卡片加号.mov'))
                    for key,value in [('W',0),('dur',-1),('style','bad'),('cx',float('nan'))]:
                        self.assertIn('error',post('/run_ring',dict(data,**{key:value})))
                    self.assertFalse(state['busy'])
            finally:
                server.shutdown(); server.server_close()

if __name__ == '__main__': unittest.main()
