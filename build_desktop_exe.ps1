$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonCandidates = @(
  "D:\claude\learn\.venv\Scripts\python.exe",
  "C:\Users\29500\AppData\Local\Programs\Python\Python312\python.exe",
  "D:\develop\python\python.exe"
)

$python = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) {
  throw "No usable Python executable was found. Install Python 3.10+ or edit build_desktop_exe.ps1."
}

function Ensure-Module($moduleName, $packageName) {
  & $python -c "import $moduleName" 2>$null
  if ($LASTEXITCODE -ne 0) {
    & $python -m pip install $packageName
    if ($LASTEXITCODE -ne 0) {
      throw "Failed to install $packageName."
    }
  }
}

Set-Location $root
Ensure-Module "PyInstaller" "pyinstaller"
Ensure-Module "PIL" "pillow"

& $python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --noconsole `
  --name "PracticeDesktopAssistant_Local" `
  --add-data "static;static" `
  --hidden-import "PIL._tkinter_finder" `
  "desktop_app.py"

if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller desktop build failed."
}

Write-Host ""
Write-Host "Built: $root\dist\PracticeDesktopAssistant_Local.exe"
