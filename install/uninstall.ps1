# Removes Kidberry: the app, its Python and tools, and the shortcuts. Asks before deleting the family's data.
$ErrorActionPreference = "Continue"
$InstallDir = $PSScriptRoot
if (-not (Test-Path (Join-Path $InstallDir "install.json"))) {
    Write-Host "This does not look like a Kidberry folder: $InstallDir" -ForegroundColor Red; exit 1
}
Write-Host ""
Write-Host "  Uninstall Kidberry from $InstallDir" -ForegroundColor Magenta
$keep = Read-Host "  Keep your recordings, transcripts and settings (data and recordings folders)? [Y/n]"
$deleteData = $keep -match "^\s*(n|no)\s*$"

Get-Process python, pythonw -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) } |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    Remove-Item (Join-Path $dir "Kidberry.lnk") -Force -ErrorAction SilentlyContinue
}
foreach ($name in "app", "venv", "python", "tools", "cache", "tmp") {
    Remove-Item (Join-Path $InstallDir $name) -Recurse -Force -ErrorAction SilentlyContinue
}
if ($deleteData) {
    Remove-Item (Join-Path $InstallDir "data"), (Join-Path $InstallDir "recordings") -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  Kidberry and all its data were removed." -ForegroundColor Green
    Set-Location $env:TEMP
    Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
} else {
    Remove-Item (Join-Path $InstallDir "install.json"), (Join-Path $InstallDir "Uninstall Kidberry.cmd") -Force -ErrorAction SilentlyContinue
    Write-Host "  Kidberry was removed. Your data is still in:" -ForegroundColor Green
    Write-Host "    $InstallDir\data and $InstallDir\recordings"
    Write-Host "  Installing Kidberry again into the same folder picks it up."
    Remove-Item (Join-Path $InstallDir "uninstall.ps1") -Force -ErrorAction SilentlyContinue
}
