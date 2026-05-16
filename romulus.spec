# PyInstaller spec for Romulus — portable Windows ZIP distribution.
#
# Produces a ``--onedir`` build under ``dist/romulus/`` containing:
#
#   romulus.exe
#   _internal/                 (Python runtime, PySide6, Qt plugins)
#   profiles/*.yaml            (seeded into <install_dir>/profiles on first launch)
#   systems/*.yaml             (seeded into <install_dir>/systems on first launch)
#   dats/*.dat                 (seeded into <install_dir>/dats on first launch)
#   themes/*.qss               (Qt stylesheet themes)
#
# Build locally with::
#
#   .venv/Scripts/python.exe -m PyInstaller romulus.spec
#
# Or end-to-end via::
#
#   .\build-portable.ps1
#
# Note: ``--onedir`` was chosen over ``--onefile`` deliberately. The single-exe
# variant unpacks the entire payload to %TEMP% on every launch (slow first
# start, redundant disk writes) and prevents the user from dropping custom
# profiles/systems/dats next to romulus.exe — which is the whole point of the
# portable layout. The onedir bundle is also dramatically easier to debug:
# you can just open _internal/ and look.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

PROJECT_ROOT = Path.cwd()

# Bundled data: top-level profiles/ and systems/ go beside the exe; DATs go
# under ``dats/`` (the runtime first-launch step copies them to the editable
# location only if it's empty). Qt themes ship as part of the Python package.
datas = []
profiles_dir = PROJECT_ROOT / "profiles"
if profiles_dir.is_dir():
    datas.append((str(profiles_dir / "*.yaml"), "profiles"))
systems_dir = PROJECT_ROOT / "systems"
if systems_dir.is_dir():
    datas.append((str(systems_dir / "*.yaml"), "systems"))
dats_dir = PROJECT_ROOT / "data" / "dats"
if dats_dir.is_dir():
    datas.append((str(dats_dir / "*.dat"), "dats"))
themes_dir = PROJECT_ROOT / "src" / "romulus" / "ui" / "themes"
if themes_dir.is_dir():
    # ``*`` so any .qss / .css / palette file accompanies the package.
    datas.append((str(themes_dir / "*.qss"), "romulus/ui/themes"))

# Hidden imports: PySide6 model/view and SQL drivers get pulled in
# indirectly and PyInstaller's static analyzer doesn't always catch them.
hiddenimports = []
hiddenimports.extend(collect_submodules("romulus"))
hiddenimports.extend([
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    # defusedxml is used by romulus.core.dat_parser for XML billion-laughs
    # protection — explicitly listed because PyInstaller misses the import
    # that lives behind a try/except.
    "defusedxml",
    "defusedxml.ElementTree",
])

a = Analysis(
    ["src/romulus/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Test-only deps; never needed at runtime, drop them to shrink the
        # bundle and avoid leaking test fixtures into a release.
        "pytest",
        "_pytest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="romulus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX trips Windows Defender heuristics; skip it.
    console=False,  # GUI app — no console window on launch.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="romulus",
)
