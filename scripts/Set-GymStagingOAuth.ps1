param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{32}$')][string]$ClientId
)
$ErrorActionPreference = 'Stop'
$gymRoot = Split-Path -Parent $PSScriptRoot
$gymYc = Join-Path $gymRoot '.local\bin\yc.exe'
# Native HTTPS использует проверку цепочки Windows. Ни payload, ни token
# не попадают в argv, временный файл или Write-Output.
$gymToken = (& $gymYc iam create-token | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $gymToken) { throw 'Не удалось получить краткоживущий IAM-токен' }
try {
    $gymExisting = Invoke-RestMethod -Uri 'https://lockbox.api.cloud.yandex.net/lockbox/v1/secrets?folderId=b1ghdeobqrevsim6jg9o&pageSize=1000' `
        -Headers @{ Authorization = "Bearer $gymToken" } -TimeoutSec 30
} catch { throw 'Список секретов staging не подтверждён' }
if ($gymExisting.nextPageToken -or @($gymExisting.secrets | Where-Object { $_.name -eq 'gym-staging-yandex-id' }).Count -gt 0) {
    throw 'Секрет уже существует или список неполон: сначала проверь метаданные, не создавай дубликат'
}
$gymSecure = Read-Host 'Client secret приложения Gym staging (скрытый ввод)' -AsSecureString
$gymPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($gymSecure)
try {
    $gymPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($gymPointer)
    if (-not $gymPlain -or $gymPlain.Length -gt 1024) { throw 'Некорректный Client secret' }
    $gymBody = @{
        folderId = 'b1ghdeobqrevsim6jg9o'
        name = 'gym-staging-yandex-id'
        description = 'OAuth пользователей Gym staging; P06'
        labels = @{ 'gym-project' = 'gym'; 'gym-environment' = 'staging'; 'gym-client-id' = $ClientId }
        versionPayloadEntries = @(@{ key = 'client_secret'; textValue = $gymPlain })
    } | ConvertTo-Json -Depth 5 -Compress
    try {
        $gymOperation = Invoke-RestMethod -Method Post -Uri 'https://lockbox.api.cloud.yandex.net/lockbox/v1/secrets' `
            -Headers @{ Authorization = "Bearer $gymToken" } -ContentType 'application/json; charset=utf-8' `
            -Body ([Text.Encoding]::UTF8.GetBytes($gymBody)) -TimeoutSec 30
    } catch { throw 'Создание секрета не подтверждено. Проверь список секретов перед повтором; не публикуй диагностику HTTP с payload.' }
    $gymDeadline = [DateTime]::UtcNow.AddSeconds(60)
    while (-not $gymOperation.done -and [DateTime]::UtcNow -lt $gymDeadline) {
        Start-Sleep -Seconds 2
        try {
            $gymOperation = Invoke-RestMethod -Uri ('https://operation.api.cloud.yandex.net/operations/' + $gymOperation.id) `
                -Headers @{ Authorization = "Bearer $gymToken" } -TimeoutSec 15
        } catch {
            [pscustomobject]@{ operation_id = $gymOperation.id } | ConvertTo-Json
            throw 'Статус операции не подтверждён. Проверь её через YC; не создавай секрет повторно.'
        }
    }
    if (-not $gymOperation.done -or $gymOperation.error -or -not $gymOperation.response.id) {
        [pscustomobject]@{ operation_id = $gymOperation.id } | ConvertTo-Json
        throw 'Операция не завершена: проверь её статус через YC, не создавай второй секрет вслепую'
    }
    [pscustomobject]@{ client_id = $ClientId; secret_id = $gymOperation.response.id; secret_version_id = $gymOperation.response.currentVersion.id } | ConvertTo-Json
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($gymPointer)
    $gymPlain = $null; $gymBody = $null; $gymToken = $null
    $gymSecure.Dispose()
}
