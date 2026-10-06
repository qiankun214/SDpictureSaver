# pictureSaver2 一键启动 Web 界面
# 用法： .\run_web.ps1 [端口]     默认 8765
param([int]$Port = 8765)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$env:PYTHONPATH = (Join-Path $root 'src')
$env:PYTHONIOENCODING = 'utf-8'

$python = (Get-Command python).Source
Write-Host "pictureSaver2 —— 启动 Web 界面 http://127.0.0.1:$Port/" -ForegroundColor Green
Write-Host "（按 Ctrl+C 停止）" -ForegroundColor DarkGray

& $python -m picsaver web --host 127.0.0.1 --port $Port
