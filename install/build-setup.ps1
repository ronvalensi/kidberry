# Builds dist\Kidberry-Setup.exe from the last commit (git archive: nothing uncommitted or git-ignored, like data\,
# can get into it). Needs Inno Setup 6:  winget install JRSoftware.InnoSetup
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$stage = Join-Path $root "dist\stage"
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 is not installed (winget install JRSoftware.InnoSetup)." }
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
git -C $root archive --format=zip -o "$stage\kidberry-src.zip" HEAD
if ($LASTEXITCODE -ne 0) { throw "git archive failed" }
Expand-Archive "$stage\kidberry-src.zip" -DestinationPath "$stage\src"
Copy-Item "$stage\src\install\install.ps1" "$stage\install.ps1"
$version = (Select-String -Path "$stage\src\kidberry\__init__.py" -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
& $python "$PSScriptRoot\make_wizard_images.py" $stage
& $iscc /Q "/DVersion=$version" "/DStage=$stage" "$PSScriptRoot\kidberry.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
$exe = Join-Path $root "dist\Kidberry-Setup.exe"
Write-Host "Built $exe ($([math]::Round((Get-Item $exe).Length / 1MB, 1)) MB), Kidberry $version, from commit $(git -C $root rev-parse --short HEAD)"
