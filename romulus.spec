# PyInstaller spec for ROMulus — portable Windows ZIP distribution.
#
# Produces a single-binary ``--onefile`` build: ``dist/romulus.exe`` contains
# the entire Python runtime, PySide6, every Qt plugin, every bundled package
# resource (themes, icons), and every transitive DLL. There is NO ``_internal/``
# folder, no loose ``*.pyd``, no ``base_library.zip`` next to the exe.
#
# At runtime the bootloader self-extracts the payload into a per-process temp
# dir (``sys._MEIPASS``); ``romulus.app`` resolves resources relative to that.
#
# User-editable data folders (``dats/``, ``profiles/``, ``systems/``) are NOT
# embedded in the exe — ``build-portable.ps1`` copies them next to the exe in
# the final ZIP so users can see and edit them on first extract without having
# to launch the app to trigger seeding. End-user layout::
#
#   romulus.exe        (single self-contained binary)
#   dats/*.dat         (bundled No-Intro DAT files — user-editable)
#   profiles/*.yaml    (destination profiles — user-editable)
#   systems/*.yaml     (system registry YAMLs — user-editable)
#
# Build locally with::
#
#   .venv/Scripts/python.exe -m PyInstaller romulus.spec
#
# Or end-to-end (recommended) via::
#
#   .\build-portable.ps1
#
# Design notes:
#
# * ``--onefile`` was chosen so the distribution is exactly one binary plus the
#   user-editable data folders — no ``_internal/`` clutter, no loose runtime
#   DLLs. Cost: ~1.5s extra startup on first launch each session (bootloader
#   unpacks ~80 MB to %TEMP%). Acceptable for an infrequently-updated portable.
# * UPX is disabled — it trips Windows Defender heuristics and the savings are
#   marginal compared to the ZIP compression applied to the final artifact.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

PROJECT_ROOT = Path.cwd()

# Embedded resources (live inside the exe and are extracted to ``sys._MEIPASS``
# at launch). Only files the Python code reads via ``Path(__file__).parent``
# go here — package resources for the Qt UI. User-editable data folders
# (profiles, systems, dats) are deliberately NOT embedded; the build script
# ships them as real folders alongside the exe in the final ZIP.
datas = []
themes_dir = PROJECT_ROOT / "src" / "romulus" / "ui" / "themes"
if themes_dir.is_dir():
    datas.append((str(themes_dir / "*.qss"), "romulus/ui/themes"))
icons_dir = PROJECT_ROOT / "src" / "romulus" / "ui" / "icons"
if icons_dir.is_dir():
    # CD-ROM disc icon used by ``app.run`` -> ``QApplication.setWindowIcon``
    # AND the EXE icon below. Ship both PNG + ICO; Qt resolves whichever is
    # appropriate at runtime.
    datas.append((str(icons_dir / "*.png"), "romulus/ui/icons"))
    datas.append((str(icons_dir / "*.ico"), "romulus/ui/icons"))

# Path to the ICO used as the exe's Windows shell icon. Resolves to None
# during dev if the icon hasn't been generated yet — PyInstaller treats
# ``icon=None`` as "use the default" rather than erroring out.
_ico_candidate = PROJECT_ROOT / "src" / "romulus" / "ui" / "icons" / "cdrom.ico"
exe_icon: str | None = str(_ico_candidate) if _ico_candidate.is_file() else None

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

# ``--onefile`` layout: a.binaries + a.datas folded directly into the EXE.
# No COLLECT() call — the EXE IS the distribution.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="romulus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX trips Windows Defender heuristics; skip it.
    runtime_tmpdir=None,
    console=False,  # GUI app — no console window on launch.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=exe_icon,
)
