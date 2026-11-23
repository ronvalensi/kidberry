@echo off
title Kidberry installer
rem Double-click to install Kidberry. Uses install.ps1 next to this file if there is one (a local copy),
rem otherwise downloads the latest installer from GitHub.
if exist "%~dp0install.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; $f=Join-Path $env:TEMP 'kidberry-install.ps1'; Invoke-WebRequest 'https://raw.githubusercontent.com/ronvalensi/kidberry/main/install/install.ps1' -OutFile $f -UseBasicParsing; & $f %*"
)
echo.
pause
