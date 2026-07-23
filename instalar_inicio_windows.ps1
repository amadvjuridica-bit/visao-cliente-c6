$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BatPath = Join-Path $AppDir "abrir_app_local.bat"
$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir "App C6 Empresas.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $BatPath
$shortcut.WorkingDirectory = $AppDir
$shortcut.WindowStyle = 7
$shortcut.Description = "Inicia o App C6 Empresas no Windows"
$shortcut.Save()

Write-Host "Atalho instalado na inicializacao do Windows:"
Write-Host $ShortcutPath
