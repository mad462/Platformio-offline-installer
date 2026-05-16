# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path(r"D:\FuckArduino\PlatformIO 离线一键部署工具\offline-compiler-kit")
launcher = project_root / "source" / "launcher" / "setup_offline_pio_launcher.py"

datas = [
    (str(project_root / "dist" / "setup_offline_pio.ps1"), "."),
    (str(project_root / "dist" / "build-info.txt"), "."),
    (str(project_root / "dist" / "README.md"), "."),
    (str(project_root / "resources"), "resources"),
]

a = Analysis(
    [str(launcher)],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="setup_offline_pio_single",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
