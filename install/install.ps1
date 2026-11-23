# Kidberry installer for Windows. Installs everything into one folder in the user's profile; no admin needed.
#
#   Install-Kidberry.cmd           downloads this script and runs it (what friends double-click)
#   install.ps1 -Source <folder>   install from a local copy of the code instead of GitHub (for testing)
#   install.ps1 -Cpu               install the CPU version even when an NVIDIA GPU is present
#
# What it puts in %LOCALAPPDATA%\Kidberry:
#   app\         the Kidberry code (replaced on update)
#   venv\        Python packages          python\  a private Python 3.11 (from uv)
#   tools\       uv and ffmpeg            data\, recordings\  the family's data: never touched by updates
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Kidberry",
    [string]$Source = "",
    [string]$Ref = "main",
    [switch]$Cpu,
    [switch]$NoShortcuts,
    [switch]$NoLaunch
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"          # Invoke-WebRequest is many times faster without its progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Repo = "ronvalensi/kidberry"

function Step($n, $text) { Write-Host ""; Write-Host "[$n/6] $text" -ForegroundColor Magenta }
function Info($text) { Write-Host "      $text" }
function Fail($text) {
    Write-Host ""; Write-Host "Kidberry could not be installed:" -ForegroundColor Red; Write-Host "  $text" -ForegroundColor Red
    Write-Host ""; Write-Host "Nothing of yours was changed. You can run the installer again." ; exit 1
}
function Download($url, $to) {
    for ($i = 1; $i -le 3; $i++) {
        try { Invoke-WebRequest -Uri $url -OutFile $to -UseBasicParsing; return }
        catch { if ($i -eq 3) { Fail "Could not download $url ($($_.Exception.Message)). Check the internet connection." }
                Start-Sleep -Seconds (3 * $i) }
    }
}

Write-Host ""
Write-Host "  Kidberry installer" -ForegroundColor Magenta
Write-Host "  Installs into $InstallDir (only for this Windows user)."

# ---------------------------------------------------------------- 1. this PC
Step 1 "Checking this PC"
if (-not [Environment]::Is64BitOperatingSystem) { Fail "Kidberry needs 64-bit Windows." }
if ([Environment]::OSVersion.Version.Major -lt 10) { Fail "Kidberry needs Windows 10 or 11." }
$gpu = $null
if (-not $Cpu) {
    try {
        $smi = & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null
        if ($LASTEXITCODE -eq 0 -and $smi) {
            $parts = ($smi | Select-Object -First 1).Split(",")
            if ([int]$parts[1].Trim() -ge 3000) { $gpu = $parts[0].Trim() }
            else { Info "$($parts[0].Trim()) has less than 3 GB of memory: using the CPU version." }
        }
    } catch { }
}
$device = if ($gpu) { "gpu" } else { "cpu" }
if ($gpu) { Info "NVIDIA GPU: $gpu - installing the fast GPU version." }
else { Info "No suitable NVIDIA GPU - installing the CPU version (slower: let a day run in the background)." }
$needGB = if ($gpu) { 10 } else { 6 }
$drive = Get-PSDrive -Name ($InstallDir.Substring(0, 1))
$freeGB = [math]::Round($drive.Free / 1GB, 1)
Info "Free space on $($drive.Name): $freeGB GB (needs about $needGB GB with the speech models)."
if ($freeGB -lt $needGB) { Fail "Not enough free disk space: $freeGB GB free, about $needGB GB needed." }

$tools = Join-Path $InstallDir "tools"
$tmp = Join-Path $InstallDir "tmp"
New-Item -ItemType Directory -Force -Path $InstallDir, $tools, $tmp | Out-Null

# Stop a running Kidberry from this folder, so its files can be replaced.
Get-Process python, pythonw -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) } |
    Stop-Process -Force -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- 2. tools: uv and ffmpeg
Step 2 "Getting tools (uv for Python, ffmpeg for audio)"
$uv = Join-Path $tools "uv\uv.exe"
if (-not (Test-Path $uv)) {
    Download "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" "$tmp\uv.zip"
    Expand-Archive "$tmp\uv.zip" -DestinationPath "$tools\uv" -Force
}
Info "uv: $(& $uv --version)"
$ffmpeg = Join-Path $tools "ffmpeg\ffmpeg.exe"
if (-not (Test-Path $ffmpeg)) {
    Info "Downloading ffmpeg (about 100 MB)..."
    Download "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip" "$tmp\ffmpeg.zip"
    Expand-Archive "$tmp\ffmpeg.zip" -DestinationPath "$tmp\ffmpeg" -Force
    $bin = Get-ChildItem "$tmp\ffmpeg" -Recurse -Filter ffmpeg.exe | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path "$tools\ffmpeg" | Out-Null
    Copy-Item (Join-Path $bin.DirectoryName "ffmpeg.exe"), (Join-Path $bin.DirectoryName "ffprobe.exe") "$tools\ffmpeg" -Force
}
Info "ffmpeg: ready"

# ---------------------------------------------------------------- 3. the code
Step 3 "Getting Kidberry"
$app = Join-Path $InstallDir "app"
$new = Join-Path $tmp "app"
if (Test-Path $new) { Remove-Item -Recurse -Force $new }
if ($Source) {
    Info "From $Source"
    New-Item -ItemType Directory -Force -Path $new | Out-Null
    robocopy $Source $new /E /NFL /NDL /NJH /NJS /NP /XD .venv .git data recordings __pycache__ .claude | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "Could not copy the code from $Source." }
} else {
    Info "From github.com/$Repo ($Ref)"
    Download "https://github.com/$Repo/archive/refs/heads/$Ref.zip" "$tmp\code.zip"
    Expand-Archive "$tmp\code.zip" -DestinationPath "$tmp\code" -Force
    $top = Get-ChildItem "$tmp\code" -Directory | Select-Object -First 1
    Move-Item $top.FullName $new
}
if (-not (Test-Path "$new\kidberry\server.py")) { Fail "The downloaded code looks incomplete." }
if (Test-Path $app) { Remove-Item -Recurse -Force $app }
Move-Item $new $app
$version = (Select-String -Path "$app\kidberry\__init__.py" -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
Info "Kidberry $version"

# ---------------------------------------------------------------- 4. Python and packages
Step 4 "Installing Python and the speech-recognition packages (a few GB, this takes a while)"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $InstallDir "python"
$env:UV_CACHE_DIR = Join-Path $InstallDir "cache"
$env:UV_LINK_MODE = "copy"
$venv = Join-Path $InstallDir "venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    & $uv python install 3.11 --quiet
    if ($LASTEXITCODE -ne 0) { Fail "Could not install Python." }
    & $uv venv $venv --python 3.11 --python-preference only-managed --quiet
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the Python environment." }
}
$torchIndex = if ($gpu) { "https://download.pytorch.org/whl/cu126" } else { "https://download.pytorch.org/whl/cpu" }
Info "PyTorch ($device)..."
& $uv pip install --python $py torch --index-url $torchIndex
if ($LASTEXITCODE -ne 0) { Fail "Could not install PyTorch." }
# The NVIDIA libraries are only needed with a GPU.
$reqs = Get-Content "$app\requirements.txt" | Where-Object { $gpu -or ($_ -notmatch "^\s*nvidia-") }
$reqs | Set-Content "$tmp\requirements.txt" -Encoding ascii
Info "Kidberry's packages..."
& $uv pip install --python $py -r "$tmp\requirements.txt"
if ($LASTEXITCODE -ne 0) { Fail "Could not install Kidberry's packages." }
& $py -c "import faster_whisper, torch, transformers, fastapi; print('      packages: ok, torch', torch.__version__)"
if ($LASTEXITCODE -ne 0) { Fail "The packages were installed but do not load." }

# ---------------------------------------------------------------- 5. settings for this install
Step 5 "Finishing"
@{ version = $version; ref = $Ref; device = $device; gpu = $gpu; installed = (Get-Date -Format s) } |
    ConvertTo-Json | Set-Content (Join-Path $InstallDir "install.json") -Encoding utf8
New-Item -ItemType Directory -Force -Path "$InstallDir\data", "$InstallDir\recordings" | Out-Null
if (-not $gpu) {   # remember the choice, so a later GPU check cannot flip it
    $settingsFile = "$InstallDir\data\settings.json"
    if (-not (Test-Path $settingsFile)) { '{"hardware": {"device": "cpu"}}' | Set-Content $settingsFile -Encoding utf8 }
}
Copy-Item "$app\install\uninstall.ps1" "$InstallDir\uninstall.ps1" -Force
@"
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
pause
"@ | Set-Content "$InstallDir\Uninstall Kidberry.cmd" -Encoding ascii
try { & $uv cache clean --quiet } catch { }   # frees the downloaded packages (a few GB); best effort
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
Info "Settings and data will be kept in $InstallDir\data"

# ---------------------------------------------------------------- 6. shortcuts
Step 6 "Creating shortcuts"
if (-not $NoShortcuts) {
    $shell = New-Object -ComObject WScript.Shell
    $targets = @([Environment]::GetFolderPath("Desktop"), (Join-Path ([Environment]::GetFolderPath("Programs")) ""))
    foreach ($dir in $targets) {
        $lnk = $shell.CreateShortcut((Join-Path $dir "Kidberry.lnk"))
        $lnk.TargetPath = Join-Path $venv "Scripts\pythonw.exe"
        $lnk.Arguments = "-m kidberry.launch"
        $lnk.WorkingDirectory = $app
        $lnk.IconLocation = "$app\kidberry\static\kidberry.ico"
        $lnk.Description = "Kidberry - listen to the kindergarten day"
        $lnk.Save()
    }
    Info "Desktop and Start menu: Kidberry"
}

Write-Host ""
Write-Host "  Kidberry $version is installed." -ForegroundColor Green
Write-Host "  Open it from the Kidberry icon on your desktop."
Write-Host "  The first time you process a day, the speech models download (1.5 to 3 GB) once."
if (-not $NoLaunch) {
    Write-Host "  Opening Kidberry now..."
    Start-Process -FilePath (Join-Path $venv "Scripts\pythonw.exe") -ArgumentList "-m", "kidberry.launch" -WorkingDirectory $app
}
