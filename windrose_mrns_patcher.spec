# -*- mode: python ; coding: utf-8 -*-

import importlib.util
from pathlib import Path

_spec_dir = Path(SPECPATH)
_version_path = _spec_dir / "_version.py"
_version_spec = importlib.util.spec_from_file_location("_version", _version_path)
_version_mod = importlib.util.module_from_spec(_version_spec)
_version_spec.loader.exec_module(_version_mod)
__version__ = _version_mod.__version__

a = Analysis(
    ['windrose_mrns_patcher.py'],
    pathex=[],
    binaries=[],
    datas=[],
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
    name=f'windrose_mrns_+g_patcher_v{__version__}',
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
