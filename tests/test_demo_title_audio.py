"""Blank title preview and real transition audio timing in MP4/alpha MOV."""
import io
import sys
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dragdemo as d
import gui


def run():
    server = gui.ThreadingHTTPServer(('127.0.0.1', 0), gui.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = 'http://127.0.0.1:%s' % server.server_port
    try:
        blank = client.open(base + '/caption_preview?text=').read()
        omitted = client.open(base + '/caption_preview?y=0.12').read()
        title = client.open(base + '/caption_preview?text=Test').read()
        assert blank == omitted and blank != title
        top = np.array(Image.open(io.BytesIO(blank)))[:70]
        assert top.max() == 0, 'blank preview unexpectedly has a title'
    finally:
        server.shutdown()
        server.server_close()
    print('blank/text preview PASS', flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        Image.new('RGB', (300, 500), 'coral').save(p / 'a.png')
        Image.new('RGB', (500, 300), 'skyblue').save(p / 'b.png')
        for mode, transparent, transition, duration in [
            ('single', False, True, 1.9),
            ('single', True, True, 3),
            ('single', False, True, .6),
            ('single', False, False, .6),
        ]:
            out = p / (mode + ('.mov' if transparent else '.mp4'))
            obj = d.DragDemo(str(p/'a.png'), str(out), transparent=transparent,
                             use_transition=transition, duration=duration,
                             sound=False, tmp_dir=tmp)
            assert obj.caption == ''
            obj.render()
            assert obj.pill is None
            assert abs(d.probe_duration(str(out)) - duration) < .1
            raw = subprocess.check_output([d.ffmpeg_exe(), '-v', 'error', '-i', str(out),
                '-vn', '-ar', '48000', '-ac', '1', '-f', 'f32le', '-'])
            a = np.frombuffer(raw, dtype='<f4')
            if transition:
                start = obj.t_trans / obj.speed
                assert np.max(np.abs(a[:int((start-.05)*48000)])) < .001
                assert np.sqrt(np.mean(a[int((start+.05)*48000):]**2)) > .003
            else:
                assert np.max(np.abs(a)) < .001
            print(mode, 'MOV' if transparent else 'MP4', 'audio timing PASS', flush=True)

if __name__ == '__main__':
    run()
