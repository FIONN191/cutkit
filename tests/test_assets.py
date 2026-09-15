# -*- coding: utf-8 -*-
"""叠加素材的兜底链：内置那份必须永远找得到。

守的是换机器那类故障：素材盘没插、素材夹改了名、预设里存的还是旧机器的
绝对路径 —— 以前这些情况下 find_asset 返回 None，成片就静悄悄少了预合成
动画和 fotor 水印（或者干脆报「找不到素材」）。现在 assets/ 里内置了同样
两个素材，任何一环断了都还能落到内置那份上。

顺带核对内置素材本身还是渲染管线要的样子：预合成和水印都带真 alpha，两者
时长读得出来 —— 这几点错一个，叠加就会出错位或糊一片。

跑法：python3 tests/test_assets.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import rosiecut  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + name
          + (("   " + str(extra)[:200]) if not cond else ""))
    if not cond:
        FAILS.append(name)


def test_bundled_present():
    print("\n[1] 两个素材都随包在 assets/ 里")
    for name, fn in rosiecut.BUNDLED_NAMES.items():
        p = os.path.join(ROOT, "assets", fn)
        check(f"{name} -> assets/{fn}", os.path.isfile(p), p)
        check(f"{fn} 不是空文件", os.path.getsize(p) > 10000 if os.path.exists(p) else False)
        check(f"bundled_asset() 找得到 {fn}",
              rosiecut.bundled_asset(name) == os.path.abspath(p),
              rosiecut.bundled_asset(name))
    check("没登记内置的素材名返回 None", rosiecut.bundled_asset("没这个素材.mov") is None)


def test_fallback_chain():
    print("\n[2] 找不到外部素材时落到内置那份")
    for name in rosiecut.BUNDLED_NAMES:
        b = rosiecut.bundled_asset(name)
        check(f"无 override: {name}", rosiecut.find_asset(name) == b,
              rosiecut.find_asset(name))
        # 旧机器上存下来的路径：盘没插、文件夹改名 —— 以前这里直接 None
        stale = "/Volumes/没插的盘/素材/" + name
        check(f"路径失效的 override: {name}",
              rosiecut.find_asset(name, stale) == b,
              rosiecut.find_asset(name, stale))


def test_override_wins():
    print("\n[3] 自己选的素材仍然优先于内置")
    mine = os.path.join(ROOT, "assets", "fotor-loading.webm")   # 随便一个真实存在的文件
    for name in rosiecut.BUNDLED_NAMES:
        check(f"override 生效: {name}", rosiecut.find_asset(name, mine) == mine,
              rosiecut.find_asset(name, mine))


def test_asset_shape():
    print("\n[4] 内置素材还是渲染管线要的样子")
    pre = rosiecut.bundled_asset(rosiecut.PRECOMP_NAME)
    wm = rosiecut.bundled_asset(rosiecut.WATERMARK_NAME)
    check("预合成带 alpha（否则会糊一块方块上去）", rosiecut.has_alpha(pre))
    # 内置的是作者现在用的「合成 123.mov」：QTRLE 带 alpha。换回黑底版时这里也要跟着改
    check("水印带 alpha（1000x1000 QTRLE 那份）", rosiecut.has_alpha(wm))
    for label, p in (("预合成", pre), ("水印", wm)):
        try:
            d = rosiecut.asset_duration(p)
        except Exception as e:                      # noqa: BLE001
            d, e = 0.0, e
            check(f"{label}读得出时长", False, e)
            continue
        check(f"{label}读得出时长（{d:.2f}s）", d > 0.5, d)
    # content_bbox：画面里实际有东西，且不是整帧全满（否则「按画幅宽 %」会算错）
    for label, p, alpha in (("预合成", pre, True), ("水印", wm, True)):
        x0, y0, x1, y1 = rosiecut.content_bbox(p, alpha)
        check(f"{label}量得出画面内容", 0.02 < (x1 - x0) <= 1.0 and 0.02 < (y1 - y0) <= 1.0,
              (x0, y0, x1, y1))


def _isolated_rosie():
    """不碰这台机器上真实的配置：那里可能存着别人自己选的素材。"""
    import tempfile
    import rosie
    d = tempfile.mkdtemp()
    rosie.ASSETS_PATH = os.path.join(d, "rosie_assets.json")
    rosie.OVERLAY_PRESET_PATH = os.path.join(d, "rosie_overlay_presets.json")
    return rosie


def test_rosie_reports_builtin():
    print("\n[5] 界面这层知道自己用的是内置素材")
    rosie = _isolated_rosie()
    a = rosie.resolve_assets()
    check("两种素材都解析到了", all(v["path"] for v in a.values()), a)
    check("并且标成了内置", all(v["builtin"] for v in a.values()), a)

    mine = os.path.join(ROOT, "assets", "fotor-loading.webm")
    rosie.save_assets_file(precomp=mine)
    a = rosie.resolve_assets()
    check("选了自己的素材后不再算内置",
          a["precomp"]["path"] == mine and not a["precomp"]["builtin"], a["precomp"])
    a = rosie.clear_asset("precomp")["assets"]
    check("「用内置」能切回来", a["precomp"]["builtin"], a["precomp"])

    rosie.save_assets_file(precomp="/Volumes/没插的盘/素材/预合成 1.mov")
    a = rosie.resolve_assets()
    check("存着旧机器路径时照样落到内置", a["precomp"]["builtin"], a["precomp"])


def test_preset_does_not_pin_bundled_path():
    print("\n[6] 素材预设不把内置素材的绝对路径钉死")
    rosie = _isolated_rosie()
    a = rosie.resolve_assets()
    rosie.ov_save("测试", {"precomp": a["precomp"]["path"],
                           "watermark": a["watermark"]["path"],
                           "precomp_size": 7.4, "watermark_size": 44})
    e = rosie.ov_load()["presets"]["测试"]
    # 打包后内置素材住在临时解包目录（Windows 单文件每次启动都换），存了就会失效
    check("内置素材存成空路径", e["precomp"] is None and e["watermark"] is None, e)
    check("宽度照常存下", e["precomp_size"] == 7.4 and e["watermark_size"] == 44.0, e)
    a = rosie.ov_apply(e)["assets"]
    check("套用后仍解析到内置素材", all(v["builtin"] for v in a.values()), a)

    # 存的是内置那份，套用时就该把之前自选的素材让开，而不是留着不动
    rosie.save_assets_file(precomp=os.path.join(ROOT, "assets", "fotor-loading.webm"))
    a = rosie.ov_apply(e)["assets"]
    check("套用内置预设会顶掉之前自选的素材", a["precomp"]["builtin"], a["precomp"])

    mine = os.path.join(ROOT, "assets", "fotor-loading.webm")
    rosie.ov_save("自选", {"precomp": mine, "precomp_size": 20})
    check("自己选的素材照旧存绝对路径",
          rosie.ov_load()["presets"]["自选"]["precomp"] == mine)


if __name__ == "__main__":
    test_bundled_present()
    test_fallback_chain()
    test_override_wins()
    if os.path.exists(rosiecut.FFMPEG) or rosiecut.FFMPEG == "ffmpeg":
        test_asset_shape()
    else:
        print("\n[4] 没有 ffmpeg，跳过")
    test_rosie_reports_builtin()
    test_preset_does_not_pin_bundled_path()
    print("\n" + ("全部通过 ✅" if not FAILS
                  else "失败 %d 项 ❌ -> %s" % (len(FAILS), FAILS)))
    sys.exit(1 if FAILS else 0)
