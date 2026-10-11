# pictureSaver2 - build the desktop executable with PyInstaller
#
# usage:
#   .\build_desktop.ps1              # onefile (default, dist\pictureSaver2.exe)
#   .\build_desktop.ps1 -OneDir      # onedir  (dist\pictureSaver2\pictureSaver2.exe)
#   .\build_desktop.ps1 -SkipInstall # do not try to install PyInstaller
#
# If the onefile exe reports "could not create temporary directory", use -OneDir:
# onedir does not extract anything to %TEMP%.
#
# Requires: Python 3.10+, PySide6 (with QtWebEngine). PyInstaller is a BUILD-only
# dependency and is installed from the Tsinghua mirror when missing.
#
# NOTE 1: keep this file ASCII-only. Windows PowerShell 5.1 decodes a BOM-less
#         .ps1 as ANSI, so non-ASCII comments corrupt the script (happened once:
#         a Chinese comment swallowed the newline and broke the whole build).
# NOTE 2: no backtick line continuations. Arguments are passed as an array so
#         there is nothing for ANSI decoding or trailing spaces to break.
# NOTE 3: native commands (python/pip/PyInstaller) write warnings to stderr, so
#         ErrorActionPreference stays 'Continue' and $LASTEXITCODE is checked.

param(
    [switch]$OneDir,
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = (Get-Command python).Source
Write-Host "python : $python"

function Test-PythonModule {
    param([string]$Name)
    # Only stdout carries the result, so an ImportError traceback cannot upset PowerShell.
    $code = "import importlib.util as u, sys; sys.stdout.write('1' if u.find_spec('$Name') else '0')"
    $result = & $python -c $code
    return ("$result".Trim() -eq '1')
}

# 1) PySide6 + QtWebEngine must be importable
if (-not (Test-PythonModule 'PySide6.QtWebEngineWidgets')) {
    throw "PySide6 with QtWebEngine is required. Install first: python -m pip install PySide6 -i https://pypi.tuna.tsinghua.edu.cn/simple"
}
Write-Host "PySide6: OK"

# 2) PyInstaller (build-only dependency)
if (-not (Test-PythonModule 'PyInstaller')) {
    if ($SkipInstall) { throw "PyInstaller is not installed (and -SkipInstall was given)" }
    Write-Host "Installing PyInstaller from the Tsinghua mirror ..."
    & $python -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw "failed to install PyInstaller (exit $LASTEXITCODE)" }
}
Write-Host "PyInstaller: OK"

# 3) icon
$icon = Join-Path $root 'src\picsaver\desktop\icon.ico'
if (-not (Test-Path $icon)) {
    & $python (Join-Path $root 'scripts\make_icon.py')
    if ($LASTEXITCODE -ne 0) { throw "icon generation failed (exit $LASTEXITCODE)" }
}

# 4) build
if ($OneDir) {
    Write-Host "Building ONEDIR ..."
    $pyArgs = @(
        '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed',
        '--name', 'pictureSaver2',
        '--icon', $icon,
        '--add-data', ((Join-Path $root 'src\picsaver\web') + ';picsaver/web'),
        '--paths', (Join-Path $root 'src'),
        '--hidden-import', 'uvicorn.logging',
        '--hidden-import', 'uvicorn.loops.auto',
        '--hidden-import', 'uvicorn.protocols.http.auto',
        '--hidden-import', 'uvicorn.protocols.websockets.auto',
        '--hidden-import', 'uvicorn.lifespan.on',
        '--hidden-import', 'sse_starlette.sse',
        (Join-Path $root 'src\picsaver\desktop\__main__.py')
    )
    & $python @pyArgs
} else {
    Write-Host "Building ONEFILE ..."
    & $python -m PyInstaller --noconfirm --clean (Join-Path $root 'packaging\picsaver-desktop.spec')
}
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

# 5) report
$exe = if ($OneDir) { Join-Path $root 'dist\pictureSaver2\pictureSaver2.exe' } else { Join-Path $root 'dist\pictureSaver2.exe' }
if (-not (Test-Path $exe)) { throw "expected output not found: $exe" }
$sizeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "BUILD OK -> $exe  ($sizeMB MB)"
Write-Host "Smoke test    : & '$exe' --smoke"
Write-Host "Window (debug): & '$exe' --cdp-port 9222"
exit 0
