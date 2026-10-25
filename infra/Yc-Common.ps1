function Get-Property {
    param($Object, [string]$Name, $Default = $null)
    if ($null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]) { return $Object.$Name }
    return $Default
}

function Invoke-Yc {
    param([string[]]$Arguments)
    if ($Arguments[0] -ceq 'serverless' -and '--endpoint' -cnotin $Arguments) {
        $Arguments += @('--endpoint', 'api.cloud.yandex.net:443')
    }
    $previousPreference = $ErrorActionPreference
    $stderrPath = [IO.Path]::GetTempFileName()
    try {
        $ErrorActionPreference = 'Continue'
        $raw = @(& $YcPath @Arguments --format json 2>$stderrPath)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
        Remove-Item -LiteralPath $stderrPath -ErrorAction SilentlyContinue
    }
    $operation = $Arguments[0..([Math]::Min(3, $Arguments.Count - 1))] -join ' '
    if ($exitCode -ne 0) { throw "YC command failed: $operation. Check authentication and permissions locally." }
    $text = ($raw | ForEach-Object { $_.ToString() }) -join "`n"
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    try { return ($text | ConvertFrom-Json) }
    catch { throw "YC returned invalid JSON for $operation. No response or credentials have been logged." }
}

function Find-One {
    param([object[]]$Items, [string]$Name)
    $matches = @($Items | Where-Object { $_.name -ceq $Name })
    if ($matches.Count -gt 1) { throw "Ambiguous resource name: $Name" }
    if ($matches.Count -eq 1) { return $matches[0] }
    return $null
}

function Assert-Owned {
    param($Resource, [string]$Name)
    if ($null -eq $Resource) { return }
    $labels = Get-Property $Resource 'labels'
    if ((Get-Property $labels 'gym-project') -cne 'gym') {
        throw "Existing resource $Name is not marked gym-project=gym. Refusing implicit adoption."
    }
}
