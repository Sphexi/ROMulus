# Build the Windows portable ZIP distribution of ROMulus.
#
# Run from the repo root with the project's virtual env activated:
#
#   .\build-portable.ps1
#
# Produces:
#
#   dist/romulus.exe                    — onefile binary from PyInstaller
#   dist/romulus/                       — assembled portable folder
#     romulus.exe
#     dats/*.dat                        — bundled No-Intro DAT files
#     gamedb/*.json                     — bundled GameDB metadata files
#     libretro-metadat/<dim>/*.dat      — bundled libretro-database metadata
#     profiles/*.yaml                   — destination profiles
#     systems/*.yaml                    — system registry
#   dist/romulus-windows-x64.zip        — the shippable artifact
#
# The CI release workflow runs this exact script on a windows-latest
# runner; keep the two in sync.

$ErrorActionPreference = "Stop"

Write-Host "==> Cleaning previous build artifacts" -ForegroundColor Cyan
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist

# Locate the active Python. Prefer the venv if one is sitting in the repo,
# otherwise fall back to whatever `python` resolves to on PATH.
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $python = $venvPython
}
else {
    $python = (Get-Command python).Source
}
Write-Host "==> Using Python: $python" -ForegroundColor Cyan

Write-Host "==> Verifying PyInstaller is installed" -ForegroundColor Cyan
& $python -m pip show pyinstaller > $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller not installed in this env — installing now..." `
        -ForegroundColor Yellow
    & $python -m pip install "pyinstaller>=6.0"
    if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed" }
}

Write-Host "==> Running PyInstaller (--onefile)" -ForegroundColor Cyan
& $python -m PyInstaller romulus.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$exePath = Join-Path $PSScriptRoot "dist\romulus.exe"
if (-not (Test-Path $exePath)) {
    throw "Expected onefile binary at $exePath but it was not produced"
}

# Assemble the portable folder layout: exe + side-by-side data folders.
$bundleDir = Join-Path $PSScriptRoot "dist\romulus"
Write-Host "==> Assembling portable folder at $bundleDir" -ForegroundColor Cyan
New-Item -ItemType Directory -Path $bundleDir | Out-Null

Move-Item -Path $exePath -Destination (Join-Path $bundleDir "romulus.exe")

# Side-by-side user-editable folders. Each is copied from its canonical
# repo location into the bundle root.
$dataFolders = @(
    @{ Source = "data\dats";   Target = "dats"     ; Filter = "*.dat"  },
    @{ Source = "data\gamedb"; Target = "gamedb"   ; Filter = "*.json" },
    @{ Source = "profiles";    Target = "profiles" ; Filter = "*.yaml" },
    @{ Source = "systems";     Target = "systems"  ; Filter = "*.yaml" }
)
foreach ($folder in $dataFolders) {
    $sourcePath = Join-Path $PSScriptRoot $folder.Source
    $targetPath = Join-Path $bundleDir   $folder.Target
    if (-not (Test-Path $sourcePath)) {
        Write-Host "    skipping $($folder.Source) (does not exist)" -ForegroundColor Yellow
        continue
    }
    New-Item -ItemType Directory -Path $targetPath -Force | Out-Null
    Copy-Item -Path (Join-Path $sourcePath $folder.Filter) `
              -Destination $targetPath -ErrorAction SilentlyContinue
    $count = (Get-ChildItem -Path $targetPath -File).Count
    Write-Host "    $($folder.Target)/ — $count file(s)" -ForegroundColor Gray
}

# libretro-metadat is structured as <dimension>/<libretro_name>.dat, so a
# flat Copy-Item with a single filter doesn't work — preserve the
# directory tree instead.
$libretroSource = Join-Path $PSScriptRoot "data\libretro-metadat"
$libretroTarget = Join-Path $bundleDir   "libretro-metadat"
if (Test-Path $libretroSource) {
    Copy-Item -Path $libretroSource -Destination $libretroTarget -Recurse
    $count = (Get-ChildItem -Path $libretroTarget -File -Recurse).Count
    Write-Host "    libretro-metadat/ — $count file(s)" -ForegroundColor Gray
} else {
    Write-Host "    skipping data\libretro-metadat (does not exist)" -ForegroundColor Yellow
}

Write-Host "==> Creating dist/romulus-windows-x64.zip" -ForegroundColor Cyan
$zipPath = Join-Path $PSScriptRoot "dist\romulus-windows-x64.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath }

# Compress the bundle root, NOT its contents — that way unzipping produces
# a `romulus\` folder rather than dumping files into the current dir.
Compress-Archive -Path $bundleDir -DestinationPath $zipPath -CompressionLevel Optimal

$zipSize = (Get-Item $zipPath).Length / 1MB
Write-Host ("==> Build complete: {0} ({1:N1} MB)" -f $zipPath, $zipSize) `
    -ForegroundColor Green
