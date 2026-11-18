# -*- mode: python ; coding: utf-8 -*-
import os

# PyInstaller injects SPECPATH (directory of this .spec) when running a spec;
# fall back to cwd so the file also works when executed manually.
try:
    _SPEC_DIR = SPECPATH
except NameError:
    _SPEC_DIR = os.getcwd()
ROOT = os.path.normpath(os.path.join(_SPEC_DIR, '..'))

a = Analysis(
    [os.path.join(ROOT, 'radial_click.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(ROOT, 'app.ico'), '.')],
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
    name='RadialMenu',
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
    icon=[os.path.join(ROOT, 'app.ico')],
)
