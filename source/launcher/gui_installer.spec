# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path(SPECPATH).resolve().parents[1]
launcher = project_root / "source" / "launcher" / "gui_installer.py"

datas = [
    (str(project_root / "resources" / "runtime"), "r/rt"),
    (str(project_root / "resources" / "pio-data"), "r/p"),
    (str(project_root / "resources" / "dependencies"), "r/dependencies"),
    (str(project_root / "resources" / "vsix"), "r/vx"),
    (str(project_root / "resources" / "patch"), "r/pt"),
    (str(project_root / "resources" / "vscode"), "r/vc"),
    (str(project_root / "resources" / "tools"), "r/tl"),
    (str(project_root / "resources" / "sdk"), "r/sdk"),
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
    [],
    name="PlatformIO_Offline_Installer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
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
    name="PlatformIO_Offline_Installer",
)
