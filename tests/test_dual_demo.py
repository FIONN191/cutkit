"""Dual upload timing, layout, fallback and real MP4/MOV export regression."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
import dragdemo as d

def run():
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)
        Image.new('RGB',(300,500),'red').save(p/'a.png')
        Image.new('RGB',(500,300),'blue').save(p/'b.png')
        try:
            d.DragDemo(str(p/'a.png'),str(p/'bad.mp4'),upload_mode='sequence')
            raise AssertionError('missing second image accepted')
        except ValueError:
            pass
        for mode in ('single','sequence','together','replace'):
            for transparent in (False,True):
                out=p/(mode+('.mov' if transparent else '.mp4'))
                obj=d.DragDemo(str(p/'a.png'),str(out), photo2=str(p/'b.png'),
                    upload_mode=mode, use_transition=False, duration=.6,
                    transparent=transparent, sound=True, caption='', tmp_dir=tmp)
                obj.render()
                assert out.stat().st_size>1000
                assert abs(d.probe_duration(str(out))-.6)<.1
                if mode!='single':
                    frame=obj._frame_dual(obj.t_trans)
                    if mode=='replace':
                        assert frame.getpixel((d.W//2,int(d.H*.53)))[2]>200
                    else:
                        assert frame.getpixel((int(d.W*.26),int(d.H*.53)))[0]>200
                        assert frame.getpixel((int(d.W*.74),int(d.H*.53)))[2]>200
                    if transparent:
                        assert frame.getpixel((0,0))[3]==0
                print(mode, 'MOV' if transparent else 'MP4', 'PASS',flush=True)
if __name__=='__main__':run()
