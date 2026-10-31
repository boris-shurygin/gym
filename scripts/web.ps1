[CmdletBinding()]
param([ValidateSet('install', 'dev', 'build', 'check', 'check-full', 'preview')][string]$Task = 'dev')

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
$backendPython = Join-Path $repositoryRoot '.local/backend-venv/Scripts/python.exe'
if (-not $env:GYM_TEST_PYTHON -and (Test-Path -LiteralPath $backendPython)) { $env:GYM_TEST_PYTHON = $backendPython }
if (-not $env:PLAYWRIGHT_CHANNEL -and (Test-Path -LiteralPath 'C:/Program Files/Google/Chrome/Application/chrome.exe')) { $env:PLAYWRIGHT_CHANNEL = 'chrome' }

function Invoke-Npm([string[]]$Arguments) {
    & $npmExecutable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "npm failed ($LASTEXITCODE)." }
}

Push-Location (Join-Path $repositoryRoot 'web')
try {
    switch ($Task) {
        'install' {
            Invoke-Npm -Arguments @('ci')
            $testBrowsers = if ($env:PLAYWRIGHT_CHANNEL -eq 'chrome') { @('webkit') } else { @('chromium', 'webkit') }
            Invoke-Npm -Arguments (@('exec', '--', 'playwright', 'install') + $testBrowsers)
        }
        { $_ -in @('check', 'check-full') } {
            $browserTask = 'test:e2e'
            if ($Task -eq 'check' -and (Test-Path -LiteralPath $backendPython)) {
                $checkScope = & $backendPython (Join-Path $repositoryRoot 'scripts/check_scope.py') detect --base origin/main --head HEAD --worktree
                if ($LASTEXITCODE -ne 0) { throw 'Could not select checks.' }
                if (-not ($checkScope | ConvertFrom-Json).browser) { $browserTask = 'test:smoke' }
            }
            foreach ($step in @('lint', 'build', 'test', $browserTask)) { Invoke-Npm -Arguments @('run', $step) }
        }
        default { Invoke-Npm -Arguments @('run', $Task) }
    }
} finally { Pop-Location }
