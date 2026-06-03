$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonCandidates = @(
  "D:\claude\learn\.venv\Scripts\python.exe",
  "C:\Users\29500\AppData\Local\Programs\Python\Python312\python.exe",
  "D:\develop\python\python.exe"
)

$python = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) {
  throw "No usable Python executable was found. Install Python 3.10+ or edit run.ps1."
}

Set-Location $root
& $python "$root\server.py" --host 127.0.0.1 --port 8765
