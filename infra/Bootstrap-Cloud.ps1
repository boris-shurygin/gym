[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'bootstrap.json'),
    [string]$YcPath = (Join-Path (Split-Path $PSScriptRoot -Parent) '.local/bin/yc.exe'),
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'Yc-Common.ps1')

$config = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigPath | ConvertFrom-Json
if ($config.schema_version -ne 1) { throw 'Unsupported bootstrap schema version.' }
if (-not (Test-Path -LiteralPath $YcPath -PathType Leaf)) { throw 'YC CLI was not found.' }
if ($config.cloud_id -notmatch '^b1[a-z0-9]+$') { throw 'Invalid cloud ID.' }
if ($config.repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw 'Invalid repository.' }
$repositoryParts = $config.repository.Split('/')
$prefixPattern = '^repo:' + [regex]::Escape($repositoryParts[0]) + '(@[0-9]+)?/' + [regex]::Escape($repositoryParts[1]) + '(@[0-9]+)?$'
if ($config.repository_subject_prefix -cnotmatch $prefixPattern) { throw 'Unexpected repository subject prefix.' }
if ($config.federation.issuer -cne 'https://token.actions.githubusercontent.com') { throw 'Unexpected OIDC issuer.' }
if ($config.federation.audience -cne "https://github.com/$($config.repository)") { throw 'Unexpected OIDC audience.' }
if ($config.federation.jwks_url -cne 'https://token.actions.githubusercontent.com/.well-known/jwks') { throw 'Unexpected JWKS URL.' }
foreach ($account in $config.accounts) {
    $subject = Get-Property $account 'subject'
    if ($subject -and -not $subject.StartsWith("$($config.repository_subject_prefix):", [StringComparison]::Ordinal)) {
        throw 'An OIDC subject refers to another repository.'
    }
}

# Read all existing resources before any mutation.
$cloud = Invoke-Yc -Arguments @('resource-manager', 'cloud', 'get', '--id', $config.cloud_id)
if ($cloud.id -cne $config.cloud_id -or $cloud.organization_id -cne $config.organization_id) {
    throw 'Cloud/organization mismatch. No resources were changed.'
}
$existingFolders = @(Invoke-Yc -Arguments @('resource-manager', 'folder', 'list', '--cloud-id', $config.cloud_id))
$folders = @{}
$accounts = @{}
$actions = New-Object 'System.Collections.Generic.List[string]'
foreach ($target in $config.folders) {
    $folder = Find-One $existingFolders $target.name
    Assert-Owned $folder $target.name
    if ($folder -and ($folder.cloud_id -cne $config.cloud_id -or $folder.status -cne 'ACTIVE')) {
        throw "Unexpected folder state: $($target.name)"
    }
    $folders[$target.key] = $folder
    if (-not $folder) { $actions.Add("create folder $($target.name)") }
    $existingAccounts = @()
    if ($folder) { $existingAccounts = @(Invoke-Yc -Arguments @('iam', 'service-account', 'list', '--folder-id', $folder.id)) }
    foreach ($targetAccount in @($config.accounts | Where-Object { $_.folder -ceq $target.key })) {
        $account = Find-One $existingAccounts $targetAccount.name
        Assert-Owned $account $targetAccount.name
        if ($account -and $account.folder_id -cne $folder.id) { throw 'Service account folder mismatch.' }
        $accounts[$targetAccount.key] = $account
        if (-not $account) { $actions.Add("create account $($targetAccount.name)") }
    }
}

$federation = $null
if ($folders['delivery']) {
    $existingFederations = @(Invoke-Yc -Arguments @('iam', 'workload-identity', 'oidc', 'federation', 'list', '--folder-id', $folders['delivery'].id))
    $federation = Find-One $existingFederations $config.federation.name
    Assert-Owned $federation $config.federation.name
    if ($federation) {
        if ($federation.issuer -cne $config.federation.issuer -or
            $federation.jwks_url -cne $config.federation.jwks_url -or
            @(Compare-Object @($federation.audiences) @($config.federation.audience)).Count -ne 0 -or
            (Get-Property $federation 'disabled' $false)) {
            throw 'Existing federation differs from the approved trust configuration.'
        }
    }
}
if (-not $federation) { $actions.Add("create federation $($config.federation.name)") }

$credentials = @{}
$bindings = @{}
foreach ($target in @($config.accounts | Where-Object { Get-Property $_ 'subject' })) {
    $account = $accounts[$target.key]
    $credential = $null
    $binding = $null
    if ($account) {
        $existingCredentials = @(Invoke-Yc -Arguments @('iam', 'workload-identity', 'federated-credential', 'list', '--service-account-id', $account.id))
        foreach ($item in $existingCredentials) {
            if (-not $federation -or $item.federation_id -cne $federation.id -or $item.external_subject_id -cne $target.subject) {
                throw "Unexpected federated credential on $($target.name)."
            }
            $credential = $item
        }
        $existingBindings = @(Invoke-Yc -Arguments @('resource-manager', 'folder', 'list-access-bindings', '--id', $folders[$target.folder].id))
        $binding = @($existingBindings | Where-Object { $_.role_id -ceq 'auditor' -and $_.subject.id -ceq $account.id -and $_.subject.type -ceq 'serviceAccount' })
    }
    $credentials[$target.key] = $credential
    $bindings[$target.key] = $binding
    if (-not $credential) { $actions.Add("bind OIDC subject for $($target.name)") }
    if (-not $binding) { $actions.Add("grant metadata-only auditor to $($target.name) on $($target.folder)") }
}

if (-not $Apply) {
    [PSCustomObject]@{ mode = 'plan'; cloud_id = $cloud.id; actions = @($actions) } | ConvertTo-Json -Depth 6
    return
}

# Apply only the listed IAM/folder foundation. No databases, buckets, keys or deployments.
foreach ($target in $config.folders) {
    if (-not $folders[$target.key]) {
        $folders[$target.key] = Invoke-Yc -Arguments @('resource-manager', 'folder', 'create', '--name', $target.name, '--cloud-id', $cloud.id, '--labels', 'gym-project=gym,gym-component=bootstrap')
    }
}
foreach ($target in $config.accounts) {
    if (-not $accounts[$target.key]) {
        $accounts[$target.key] = Invoke-Yc -Arguments @('iam', 'service-account', 'create', '--name', $target.name, '--folder-id', $folders[$target.folder].id, '--labels', "gym-project=gym,gym-environment=$($target.folder)")
    }
}
if (-not $federation) {
    $federation = Invoke-Yc -Arguments @('iam', 'workload-identity', 'oidc', 'federation', 'create', '--name', $config.federation.name, '--folder-id', $folders['delivery'].id, '--issuer', $config.federation.issuer, '--jwks-url', $config.federation.jwks_url, '--audiences', $config.federation.audience, '--labels', 'gym-project=gym,gym-component=bootstrap')
}
foreach ($target in @($config.accounts | Where-Object { Get-Property $_ 'subject' })) {
    $account = $accounts[$target.key]
    if (-not $credentials[$target.key]) {
        $null = Invoke-Yc -Arguments @('iam', 'workload-identity', 'federated-credential', 'create', '--service-account-id', $account.id, '--federation-id', $federation.id, '--external-subject-id', $target.subject)
    }
    if (-not $bindings[$target.key]) {
        $null = Invoke-Yc -Arguments @('resource-manager', 'folder', 'add-access-binding', '--id', $folders[$target.folder].id, '--role', 'auditor', '--service-account-id', $account.id)
    }
}

$state = [ordered]@{ schema_version = 1; cloud_id = $cloud.id; organization_id = $cloud.organization_id; repository = $config.repository; repository_subject_prefix = $config.repository_subject_prefix; federation_id = $federation.id; audience = $config.federation.audience; folders = [ordered]@{}; accounts = [ordered]@{}; scope = 'foundation-only'; planned_actions = @($actions) }
foreach ($target in $config.folders) { $state.folders[$target.key] = $folders[$target.key].id }
foreach ($target in $config.accounts) { $state.accounts[$target.key] = $accounts[$target.key].id }
$stateDirectory = Join-Path (Split-Path $PSScriptRoot -Parent) '.local'
New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
$stateJson = $state | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText((Join-Path $stateDirectory 'bootstrap-state.json'), $stateJson, (New-Object Text.UTF8Encoding($false)))
$stateJson
