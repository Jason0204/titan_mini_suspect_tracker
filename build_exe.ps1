param(
    [string]$Venv = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Changed 2026-10-18: accept .venv / ui_venv / venv instead of hardcoding ui_venv,
# so the README flow (python -m venv .venv) works without renaming anything.
# Pass -Venv <name> to point at a different folder.
$python = $null
$names = if ($Venv) { @($Venv) } else { @(".venv", "ui_venv", "venv") }
foreach ($name in $names) {
    $candidate = Join-Path $PSScriptRoot "$name\Scripts\python.exe"
    if (Test-Path $candidate) { $python = $candidate; break }
}
if (-not $python) {
    throw "Python environment not found. Create one first: python -m venv .venv, then .venv\Scripts\python.exe -m pip install -r requirements.txt"
}
Write-Host "Using Python: $python"

# Changed 2026-09-20: Propagate native command failures instead of reporting a false success.
& $python -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install build requirements (exit code $LASTEXITCODE)."
}
# Changed 2026-09-20: Build one self-contained EXE so Python DLLs cannot be separated.
# Changed 2026-10-18: --icon embeds the icon into the EXE resource (Explorer/taskbar);
# the assets folder is collected so the runtime window icon resolves from _MEIPASS.
# Changed 2026-10-18f: every device page must be collected -- the app uploads the page text
# from _MEIPASS at connect time, so a missing script means "connection failed" on the EXE
# while the same source run works (the offline guard had never been bundled at all).
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onefile `
    --name TitanSuspectTracker `
    --icon "src\suspect_tracker\assets\icon.ico" `
    --paths src `
    --add-data "src\device\openmv_ae3_raw_jpeg.py;device" `
    --add-data "src\device\titan_lcd_monitor.py;device" `
    --add-data "src\device\titan_offline_guard.py;device" `
    --add-data "src\suspect_tracker\assets;assets" `
    src\suspect_tracker\__main__.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed (exit code $LASTEXITCODE). Close any running TitanSuspectTracker.exe and retry."
}

Write-Host "Executable created: dist\TitanSuspectTracker.exe"
