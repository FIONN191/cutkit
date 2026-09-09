# -*- coding: utf-8 -*-
"""busy 状态机的回归闸门。

这里守的是一类特别难查的故障：后台线程静默死掉、busy 再没人清，
界面从此对所有模式都只会说「正在处理中」，只能重启 App。
2026-09-08 一次踩到两个：

  * worker_screen 早就收 7 个参数了，调用处还在传 5 个 —— 线程在进
    自己的 try 之前就 TypeError，busy 永久卡住；
  * /pick_video 不管谁调都顺手起分析并占住 busy，可只有「录屏步骤」
    那页在轮询，另外两页被自己触发的分析闷声挡住。

所以这里分两层：先用 AST 静态核对每个 Thread(target=worker_*) 的实参
个数（这条能在零成本下抓住上面第一类），再起真的 Handler 跑一遍
busy 的进出，只把文件对话框和真渲染打桩。

跑法：python3 tests/test_busy.py
"""
import ast
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS = []


def check(name, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (("   " + str(extra)[:200]) if not cond else ""))
    if not cond:
        FAILS.append(name)


# ── 第一层：静态核对线程实参个数 ────────────────────────────────────────
def test_thread_arity():
    print("\n[1] 每个 threading.Thread(target=worker_*) 的实参个数要对得上")
    tree = ast.parse(open(os.path.join(ROOT, "gui.py"), encoding="utf-8").read())
    sigs = {n.name: len(n.args.args) for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name.startswith("worker")}
    seen = 0
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "Thread"):
            continue
        target = args_n = None
        for kw in n.keywords:
            if kw.arg == "target":
                target = getattr(kw.value, "id", None)
            elif kw.arg == "args":
                args_n = len(kw.value.elts) if isinstance(kw.value, (ast.Tuple, ast.List)) else None
        if target not in sigs:
            continue
        seen += 1
        check(f"gui.py:{n.lineno} {target} 传 {args_n} 个，定义收 {sigs[target]} 个",
              args_n == sigs[target])
    check("确实扫到了线程启动点", seen >= 5, f"只扫到 {seen} 个")


# ── 第二层：真 Handler 跑 busy 的进出 ───────────────────────────────────
def load_gui():
    spec = importlib.util.spec_from_file_location("cutkit_gui", os.path.join(ROOT, "gui.py"))
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    return g


def test_busy_contract():
    g = load_gui()

    picked = os.path.join(tempfile.gettempdir(), "cutkit-test-source.mp4")
    open(picked, "wb").close()          # /rosie_run 会检查文件存在
    g.pick_video = lambda *a, **k: picked

    analyzed = threading.Event()
    release = threading.Event()

    def fake_analyze(video):
        analyzed.set()
        release.wait(10)
        with g.LOCK:
            g.STATE.update(busy=False, done=True, ok=True, video=video,
                           plan={"segments": [(0, 1)], "out_len": 1.0})
    g.worker_analyze = fake_analyze

    rosie_ran = threading.Event()

    def fake_rosie(src, params, mode, overlay, sizes):
        rosie_ran.set()
        with g.LOCK:
            g.STATE.update(busy=False, done=True, ok=True)
    g.worker_rosie = fake_rosie

    screen_kw = {}

    def fake_render_screen(video, out, plan_d, texts, tmp, **kw):
        screen_kw.update(kw)
    g.screencut.render_screen = fake_render_screen

    srv = ThreadingHTTPServer(("127.0.0.1", 0), g.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]

    def post(path, data=None):
        req = urllib.request.Request(base + path, data=json.dumps(data or {}).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=10).read())

    def busy():
        return post("/status")["busy"]

    def wait_idle(sec=8):
        end = time.time() + sec
        while time.time() < end and busy():
            time.sleep(.05)
        return not busy()

    print("\n[2] 自然流 / 广告结尾选文件：不该替别人占住 busy")
    r = post("/pick_video")
    check("返回路径", r.get("path") == picked, r)
    check("busy 保持 false", busy() is False)
    check("没有偷偷起分析", not analyzed.is_set())

    print("\n[3] 选完立刻开始剪辑：不该被自己触发的分析挡住")
    r = post("/rosie_run", {"src": picked, "mode": "natural", "overlay": False,
                            "params": {"paint_sec": 1.67, "type_speed": 9,
                                       "wait_sec": .8, "last_wait": .8}})
    check("没有回「正在处理中」", r.get("error") != "正在处理中", r)
    check("worker_rosie 真的开工了", rosie_ran.wait(3))
    check("跑完 busy 归位", wait_idle())

    print("\n[4] 录屏步骤那页显式要分析：分析照旧，busy 有进有出")
    r = post("/pick_video", {"analyze": True})
    check("返回路径", r.get("path") == picked, r)
    check("分析已启动", analyzed.wait(3))
    check("busy 变 true", busy() is True)
    check("分析中再选一次会被挡", post("/pick_video", {"analyze": True}).get("error") == "正在处理中")
    release.set()
    check("分析结束 busy 归位", wait_idle())

    print("\n[5] 录屏步骤的开始剪辑：线程别在进 try 之前就死掉")
    r = post("/run_screen", {"texts": ["a", "b", "c", "d"], "zoom_end": False})
    check("接受请求", r.get("ok") is True, r)
    check("busy 归位（没卡死）", wait_idle())
    check("真的跑到了 render_screen", bool(screen_kw), screen_kw)
    check("zoom 两个参数都传到位",
          screen_kw.get("zoom_end") is False and "zoom_photo" in screen_kw, screen_kw)
    st = post("/status")
    check("状态是成功而不是失败", st["ok"] is True and st["done"] is True, st)

    srv.shutdown()
    try:
        os.remove(picked)
    except OSError:
        pass


if __name__ == "__main__":
    test_thread_arity()
    test_busy_contract()
    print("\n" + ("全部通过 ✅" if not FAILS else "失败 %d 项 ❌ -> %s" % (len(FAILS), FAILS)))
    sys.exit(1 if FAILS else 0)
