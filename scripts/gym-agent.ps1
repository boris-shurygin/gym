$ErrorActionPreference = 'Stop'
$gymRoot = Split-Path $PSScriptRoot -Parent
$gymPython = Join-Path $gymRoot '.local/backend-venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $gymPython)) { $gymPython = 'python' }
$env:PYTHONUTF8 = '1'
Push-Location $gymRoot
try {
    & $gymPython -m gym_agent @args
    $gymAgentExitCode = $LASTEXITCODE
} finally { Pop-Location }
if ($gymAgentExitCode -ne 0) { throw "Gym agent failed ($gymAgentExitCode)." }
