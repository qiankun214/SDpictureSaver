# pictureSaver2 - create a shortcut that launches the DESKTOP app (no packaging needed)
#
# usage:
#   .\scripts\make_shortcut.ps1                 # desktop shortcut
#   .\scripts\make_shortcut.ps1 -StartMenu      # start-menu shortcut instead
#   .\scripts\make_shortcut.ps1 -Name MyApp     # custom shortcut name
#   .\scripts\make_shortcut.ps1 -Folder D:\x    # custom destination folder
#
# The shortcut points at run_desktop.cmd (which sets PYTHONPATH and starts
# `python -m picsaver desktop`), uses src\picsaver\desktop\icon.ico, and runs
# minimized so the console window stays out of the way.

param(
    [switch]$StartMenu,
    [string]$Name = 'pictureSaver2',
    [string]$Folder = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$target = Join-Path $root 'run_desktop.cmd'
$icon = Join-Path $root 'src\picsaver\desktop\icon.ico'

if (-not (Test-Path $target)) { throw "not found: $target" }
if (-not (Test-Path $icon)) { Write-Host "icon missing, generating ..."; & python (Join-Path $root 'scripts\make_icon.py') }

$folder = if ($Folder) { $Folder }
          elseif ($StartMenu) { [Environment]::GetFolderPath('Programs') }
          else { [Environment]::GetFolderPath('Desktop') }
if (-not (Test-Path $folder)) { New-Item -ItemType Directory -Force -Path $folder | Out-Null }
$link = Join-Path $folder "$Name.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'pictureSaver2 - SD card photo copier (desktop app)'
$shortcut.WindowStyle = 7   # minimized console; the Qt window shows normally
$shortcut.Save()

Write-Host "OK -> $link"
Write-Host "Double-click it to open the desktop window (no packaging required)."
