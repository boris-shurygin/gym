[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'bootstrap.json'),
    [string]$ResourcesPath = (Join-Path $PSScriptRoot 'resources.json'),
    [string]$StatePath = (Join-Path (Split-Path $PSScriptRoot -Parent) '.local/bootstrap-state.json'),
    [string]$YcPath = (Join-Path (Split-Path $PSScriptRoot -Parent) '.local/bin/yc.exe'),
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'Yc-Common.ps1')

$config = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigPath | ConvertFrom-Json
$resources = Get-Content -Raw -Encoding UTF8 -LiteralPath $ResourcesPath | ConvertFrom-Json
$state = Get-Content -Raw -Encoding UTF8 -LiteralPath $StatePath | ConvertFrom-Json
if ($resources.schema_version -ne 1 -or $state.cloud_id -cne $config.cloud_id) { throw 'Configuration/state mismatch.' }
if ($resources.ydb_provisioned_ru -ne 0 -or $resources.ydb_throttling_ru_per_second -le 0 -or $resources.bucket_max_bytes -le 0) { throw 'Reserved or unbounded resources are outside bootstrap scope.' }
if ($resources.ydb_storage_limit -cnotmatch '^([1-9][0-9]*)(MB|GB)$') { throw 'Use an explicit bounded YDB storage limit in MB or GB.' }
$storageMultiplier = if ($Matches[2] -ceq 'GB') { 1GB } else { 1MB }
$storageLimitBytes = [long]$Matches[1] * $storageMultiplier
$cloud = Invoke-Yc -Arguments @('resource-manager', 'cloud', 'get', '--id', $config.cloud_id)
if ($cloud.id -cne $config.cloud_id -or $cloud.organization_id -cne $config.organization_id) { throw 'Cloud/organization mismatch.' }
foreach ($target in $config.folders) {
    $folder = Invoke-Yc -Arguments @('resource-manager', 'folder', 'get', '--id', $state.folders.($target.key))
    Assert-Owned $folder $target.name
    if ($folder.cloud_id -cne $config.cloud_id -or $folder.name -cne $target.name) { throw 'Folder state mismatch.' }
}

$plan = @()
foreach ($environment in @('delivery', 'staging', 'production')) {
    $folderId = $state.folders.$environment
    $buckets = @(Invoke-Yc -Arguments @('storage', 'bucket', 'list', '--folder-id', $folderId))
    $bucket = Find-One $buckets $resources.buckets.$environment
    if ($bucket) {
        $bucket = Invoke-Yc -Arguments @('storage', 'bucket', 'get', '--name', $bucket.name, '--full')
        if ($bucket.folder_id -cne $folderId -or [long]$bucket.max_size -ne [long]$resources.bucket_max_bytes) { throw 'Bucket folder/size differs from the reviewed configuration.' }
        $anonymous = Get-Property $bucket 'anonymous_access_flags'
        if ((Get-Property $anonymous 'read' $false) -or (Get-Property $anonymous 'list' $false) -or (Get-Property $anonymous 'config_read' $false)) { throw 'Existing bucket is public; refusing implicit changes.' }
    }
    $plan += [PSCustomObject]@{key="bucket.$environment"; existing=$bucket; arguments=@('storage', 'bucket', 'create', '--name', $resources.buckets.$environment, '--folder-id', $folderId, '--default-storage-class', 'standard', '--max-size', [string]$resources.bucket_max_bytes, '--public-read=false', '--public-list=false', '--public-config-read=false', '--tags', 'gym-project=gym')}
    if ($environment -ceq 'delivery') {
        $registry = Find-One @(Invoke-Yc -Arguments @('container', 'registry', 'list', '--folder-id', $folderId)) $resources.registry_name
        Assert-Owned $registry $resources.registry_name
        $plan += [PSCustomObject]@{key='registry.delivery'; existing=$registry; arguments=@('container', 'registry', 'create', '--name', $resources.registry_name, '--folder-id', $folderId, '--labels', 'gym-project=gym')}
        continue
    }
    $database = Find-One @(Invoke-Yc -Arguments @('ydb', 'database', 'list', '--folder-id', $folderId)) $resources.database_name
    Assert-Owned $database $resources.database_name
    if ($database) {
        $serverless = Get-Property $database 'serverless_database'
        if ([long](Get-Property $serverless 'storage_size_limit' 0) -ne $storageLimitBytes) { throw 'Existing database storage limit differs from bootstrap configuration.' }
        if (-not $serverless -or -not (Get-Property $database 'deletion_protection' $false) -or (Get-Property $serverless 'provisioned_rcu_limit' 0) -ne 0 -or -not (Get-Property $serverless 'enable_throttling_rcu_limit' $false) -or (Get-Property $serverless 'throttling_rcu_limit' 0) -ne $resources.ydb_throttling_ru_per_second) { throw 'Existing database limits/type differ from bootstrap configuration.' }
    }
    $plan += [PSCustomObject]@{key="database.$environment"; existing=$database; arguments=@('ydb', 'database', 'create', '--name', $resources.database_name, '--folder-id', $folderId, '--serverless', '--deletion-protection', '--sls-enable-throttling-rcu', '--sls-throttling-rcu', [string]$resources.ydb_throttling_ru_per_second, '--sls-provisioned-rcu', '0', '--sls-storage-size', $resources.ydb_storage_limit, '--labels', 'gym-project=gym')}
    foreach ($definition in @(
        @{key='function'; command=@('serverless', 'function'); name=$resources.function_name},
        @{key='container'; command=@('serverless', 'container'); name=$resources.migration_container_name},
        @{key='gateway'; command=@('serverless', 'api-gateway'); name=$resources.gateway_name}
    )) {
        $item = Find-One @(Invoke-Yc -Arguments ($definition.command + @('list', '--folder-id', $folderId))) $definition.name
        Assert-Owned $item $definition.name
        $arguments = $definition.command + @('create', '--name', $definition.name, '--folder-id', $folderId, '--labels', 'gym-project=gym')
        if ($definition.key -ceq 'gateway') { $arguments += @('--spec', (Join-Path $PSScriptRoot 'gateway-bootstrap.json'), '--no-logging') }
        $plan += [PSCustomObject]@{key="$($definition.key).$environment"; existing=$item; arguments=$arguments}
    }
}
$actions = @($plan | Where-Object { -not $_.existing } | ForEach-Object { "create $($_.key)" })
if (-not $Apply) { [PSCustomObject]@{mode='plan'; actions=$actions} | ConvertTo-Json -Depth 6; return }

$observed = [ordered]@{cloud_id=$config.cloud_id; scope='empty-infrastructure'; resources=[ordered]@{}}
foreach ($entry in $plan) {
    $item = $entry.existing
    if (-not $item) { $item = Invoke-Yc -Arguments $entry.arguments }
    $observed.resources[$entry.key] = $item
}
$outputPath = Join-Path (Split-Path $PSScriptRoot -Parent) '.local/resources-state.json'
[IO.File]::WriteAllText($outputPath, ($observed | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding($false)))
[PSCustomObject]@{mode='applied'; created=$actions; resources=@($observed.resources.Keys); state_file='.local/resources-state.json'} | ConvertTo-Json -Depth 6
