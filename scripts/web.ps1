[CmdletBinding()]
param([ValidateSet('install', 'dev', 'build', 'check', 'preview')][string]$Task = 'dev')

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path $PSScriptRoot -Parent
$localNodePath = Join-Path $repositoryRoot '.local/node-path.txt'
if (-not (Get-Command node -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath $localNodePath)) {
    $nodeDirectory = (Get-Content -Raw -LiteralPath $localNodePath).Trim()
    $env:PATH = $nodeDirectory + [IO.Path]::PathSeparator + $env:PATH
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Install Node.js 24 or configure the local runtime first.' }
if ((& node --version) -notmatch '^v24\.') { throw 'Use Node.js 24 for this project.' }
$npmExecutable = if ($env:OS -eq 'Windows_NT') { 'npm.cmd' } else { 'npm' }
$env:npm_config_cache = Join-Path $repositoryRoot '.local/npm-cache'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $repositoryRoot '.local/playwright'

function Invoke-Npm([string[]]$Arguments) {
    & $npmExecutable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "npm failed ($LASTEXITCODE)." }
}

Push-Location (Join-Path $repositoryRoot 'web')
try {
    switch ($Task) {
        'install' { Invoke-Npm -Arguments @('ci'); Invoke-Npm -Arguments @('exec', '--', 'playwright', 'install', 'chromium') }
        'check' { foreach ($step in @('lint', 'build', 'test', 'test:e2e')) { Invoke-Npm -Arguments @('run', $step) } }
        default { Invoke-Npm -Arguments @('run', $Task) }
    }
} finally { Pop-Location }
