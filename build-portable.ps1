# Build the Windows portable ZIP distribution of ROMulus.
#
# Run from the repo root with the project's virtual env activated:
#
#   .\build-portable.ps1
#
# Produces:
#
#   dist/romulus/                       — the unpacked onedir bundle
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

Write-Host "==> Running PyInstaller (--onedir)" -ForegroundColor Cyan
& $python -m PyInstaller romulus.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$bundleDir = Join-Path $PSScriptRoot "dist\romulus"
if (-not (Test-Path $bundleDir)) {
    throw "Expected bundle at $bundleDir but it was not produced"
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
