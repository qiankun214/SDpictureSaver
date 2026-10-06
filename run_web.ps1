# pictureSaver2 - start the web UI (kept ASCII-only so Windows PowerShell 5.1 parses it)
# usage: .\run_web.ps1 [port]     default 8765
# if the execution policy blocks this file, run instead:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\run_web.ps1
# or simply use run_web.cmd
param([int]$Port = 8765)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$env:PYTHONPATH = (Join-Path $root 'src')
$env:PYTHONIOENCODING = 'utf-8'

$python = (Get-Command python).Source
Write-Host "pictureSaver2 - Web UI: http://127.0.0.1:$Port/" -ForegroundColor Green
Write-Host "(press Ctrl+C to stop)" -ForegroundColor DarkGray

& $python -m picsaver web --host 127.0.0.1 --port $Port
