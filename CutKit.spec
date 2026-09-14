# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

MAC = sys.platform == "darwin"

datas = [('assets', 'assets')]   # 素材随包分发：Fotor 转场 + 预合成动画 + fotor 水印
                                 # （少了它们，换机器就会「找不到素材」）
binaries = []
hiddenimports = ['render', 'screencut', 'history', 'dragdemo', 'ringarrow', 'align',
                 'rosie', 'rosiecut', 'nl', 'analyze', 'adcut', 'i18n']
for pkg in ('imageio_ffmpeg', 'webview', 'cv2'):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h
if MAC:
    # pyobjc frameworks the native WKWebView window needs
    for pkg in ('objc', 'Foundation', 'AppKit', 'WebKit', 'Cocoa',
                'Quartz', 'CoreFoundation', 'Security'):
        hiddenimports += collect_submodules(pkg)
else:
    # Windows 原生窗口: pywebview 走 WinForms + WebView2，需要 pythonnet/clr_loader
    for pkg in ('clr_loader', 'pythonnet'):
        try:
            d, b, h = collect_all(pkg)
            datas += d; binaries += b; hiddenimports += h
        except Exception:
            pass
    hiddenimports += ['webview.platforms.winforms',
                      'webview.platforms.edgechromium']


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if MAC:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='CutKit',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=True,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name='CutKit',
    )
    app = BUNDLE(
        coll,
        name='CutKit.app',
        icon='CutKit.icns',
        bundle_identifier='ai.fionn.cutkit',
        info_plist={
            'CFBundleDisplayName': 'CutKit',
            'NSHumanReadableCopyright': 'aifiltertrends',
            'NSHighResolutionCapable': True,
        },
    )
else:
    # Windows: 单文件便携 exe（双击即用，无需安装）
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='CutKit',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon='CutKit.ico' if os.path.exists('CutKit.ico') else None,
    )
