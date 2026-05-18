# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# futu / moomoo SDK 10.5.6508 以降は VERSION.txt や .proto 等の data ファイルを
# import 時に参照するため、明示的にバンドルする必要がある。
futu_datas = collect_data_files('futu', include_py_files=False)
moomoo_datas = collect_data_files('moomoo', include_py_files=False)
futu_submodules = collect_submodules('futu')
moomoo_submodules = collect_submodules('moomoo')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[*futu_datas, *moomoo_datas],
    hiddenimports=[
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'fastapi',
        'starlette',
        'slowapi',
        'tenacity',
        'dotenv',
        *futu_submodules,
        *moomoo_submodules,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'tkinter',
        'streamlit',
    ],
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
    name='alpha-strike',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
