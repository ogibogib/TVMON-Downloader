# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_delvewheel_libs_directory, copy_metadata

source_dir = Path(SPECPATH).resolve().parent / 'source'
curl_datas, curl_binaries = collect_delvewheel_libs_directory('curl_cffi')

a = Analysis(
    [str(source_dir / 'tvmon_downloader.py')],
    pathex=[str(source_dir)],
    binaries=curl_binaries,
    datas=curl_datas + copy_metadata('curl_cffi') + copy_metadata('cffi') + copy_metadata('certifi')
          + [(str(source_dir / 'assets'), 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['win32evtlog', 'win32evtlogutil'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TVMON-Download-Manager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    name='TVMON-Download-Manager',
)
