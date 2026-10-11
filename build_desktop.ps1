# pictureSaver2 - build the desktop executable with PyInstaller
#
# usage:
#   .\build_desktop.ps1              # onefile (default, dist\pictureSaver2.exe)
#   .\build_desktop.ps1 -OneDir      # onedir  (dist\pictureSaver2\pictureSaver2.exe)
#   .\build_desktop.ps1 -SkipInstall # do not try to install PyInstaller
#
# Requires: Python 3.10+, PySide6 (with QtWebEngine). PyInstaller is a BUILD-only
# dependency and is installed from the Tsinghua mirror when missing.
#
# NOTE: native commands (python/pip/PyInstaller) write warnings to stderr, so this
# script keeps ErrorActionPreference = 'Continue' and checks $LASTEXITCODE instead.

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
    # 只用 stdout 回传结果，避免 ImportError 的 traceback 干扰 PowerShell
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
if (-not (Test-Path 'src\picsaver\desktop\icon.ico')) {
    & $python scripts\make_icon.py
    if ($LASTEXITCODE -ne 0) { throw "icon generation failed (exit $LASTEXITCODE)" }
}

# 4) build
if ($OneDir) {
    Write-Host "Building ONEDIR ..."
    New-Item -ItemType Directory -Force -Path build | Out-Null
    & $python -m PyInstaller --noconfirm --clean --onedir --windowed --name pictureSaver2 `
        --specpath build --workpath "build\onedir" --distpath dist `
        --icon "src\picsaver\desktop\icon.ico" `
        --add-data "src\picsaver\web;picsaver\web" `
        --paths src `
        --hidden-import uvicorn.logging `
        --hidden-import uvicorn.loops.auto `
        --hidden-import uvicorn.protocols.http.auto `
        --hidden-import uvicorn.protocols.websockets.auto `
        --hidden-import uvicorn.lifespan.on `
        --hidden-import sse_starlette.sse `
        --exclude-module pytest --exclude-module tkinter --exclude-module pydoc `
        "src\picsaver\desktop\__main__.py"
} else {
    Write-Host "Building ONEFILE ..."
    & $python -m PyInstaller --noconfirm --clean "packaging\picsaver-desktop.spec"
}
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

# 5) report
$exe = if ($OneDir) { "dist\pictureSaver2\pictureSaver2.exe" } else { "dist\pictureSaver2.exe" }
if (-not (Test-Path $exe)) { throw "expected output not found: $exe" }
$sizeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "BUILD OK -> $exe  ($sizeMB MB)"
Write-Host "Smoke test    : & '$exe' --smoke"
Write-Host "Window (debug): & '$exe' --cdp-port 9222"
exit 0
