$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonCandidates = @(
  "D:\claude\learn\.venv\Scripts\python.exe",
  "C:\Users\29500\AppData\Local\Programs\Python\Python312\python.exe",
  "D:\develop\python\python.exe"
)

$python = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) {
  throw "No usable Python executable was found. Install Python 3.10+ or edit build_exe.ps1."
}

Set-Location $root

& $python -m PyInstaller --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
  & $python -m pip install pyinstaller
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to install PyInstaller."
  }
}

& $python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --name "BYOPracticeAssistant" `
  --add-data "static;static" `
  "server.py"

if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller build failed."
}

Write-Host ""
Write-Host "Built: $root\dist\BYOPracticeAssistant.exe"
