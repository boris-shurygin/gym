$ErrorActionPreference = 'Stop'
$parts = @($args)
$statePath = $env:GYM_FAKE_YC_STATE
$state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
$command = $parts -join ' '
$state.calls = @($state.calls) + $command

function Value-After([string]$Flag) {
    $position = [Array]::IndexOf($parts, $Flag)
    if ($position -lt 0) { throw "Missing flag $Flag" }
    return $parts[$position + 1]
}
function Labels {
    $result = @{}
    foreach ($pair in (Value-After '--labels').Split(',')) {
        $key, $value = $pair.Split('=', 2)
        $result[$key] = $value
    }
    return $result
}

function Resources([string]$Kind) {
    $property = $state.resources.PSObject.Properties[$Kind]
    if ($property) { return @($property.Value) }
    return @()
}

function Save-Resource([string]$Kind, $Item) {
    $items = @(Resources $Kind) + $Item
    $state.resources | Add-Member -MemberType NoteProperty -Name $Kind -Value $items -Force
    return $Item
}

$global:LASTEXITCODE = 0
if ($state.progress) { Write-Error 'Provider progress on stderr' -ErrorAction Continue }
if ($state.fail_prefix -and $command.StartsWith($state.fail_prefix)) {
    $global:LASTEXITCODE = 1
    'SECRET-SENTINEL must not appear in bootstrap output'
    return
}

$result = $null
switch -Regex ($command) {
    '^resource-manager cloud get ' { $result = $state.cloud; break }
    '^resource-manager folder list --' { $result = @($state.folders); break }
    '^resource-manager folder get ' {
        $id = Value-After '--id'
        $result = @($state.folders | Where-Object { $_.id -eq $id })[0]; break
    }
    '^(storage bucket|container registry|ydb database|serverless function|serverless container|serverless api-gateway) list ' {
        $kind = switch ($parts[0] + ' ' + $parts[1]) { 'storage bucket' {'bucket'} 'container registry' {'registry'} 'ydb database' {'database'} 'serverless api-gateway' {'gateway'} default {$parts[1]} }
        $folder = Value-After '--folder-id'
        $result = @(Resources $kind | Where-Object { $_.folder_id -eq $folder }); break
    }
    '^storage bucket get ' {
        $name = Value-After '--name'
        $result = @(Resources 'bucket' | Where-Object { $_.name -eq $name })[0]; break
    }
    '^(storage bucket|container registry|ydb database|serverless function|serverless container|serverless api-gateway) create ' {
        $kind = switch ($parts[0] + ' ' + $parts[1]) { 'storage bucket' {'bucket'} 'container registry' {'registry'} 'ydb database' {'database'} 'serverless api-gateway' {'gateway'} default {$parts[1]} }
        $folder = Value-After '--folder-id'
        $name = Value-After '--name'
        $item = [ordered]@{id="$kind-$folder-$name"; folder_id=$folder; name=$name}
        if ($kind -ceq 'bucket') {
            $item.max_size = [long](Value-After '--max-size')
            $item.anonymous_access_flags = @{read=$false; list=$false; config_read=$false}
        } else { $item.labels = Labels }
        if ($kind -ceq 'database') {
            $item.deletion_protection = $true
            $item.serverless_database = @{enable_throttling_rcu_limit=$true; throttling_rcu_limit=[int](Value-After '--sls-throttling-rcu'); provisioned_rcu_limit=[int](Value-After '--sls-provisioned-rcu'); storage_size_limit=1073741824}
        }
        $result = Save-Resource $kind ([PSCustomObject]$item); break
    }
    '^iam service-account list ' {
        $folder = Value-After '--folder-id'
        $result = @($state.accounts | Where-Object { $_.folder_id -eq $folder }); break
    }
    '^iam workload-identity oidc federation list ' { $result = @($state.federations); break }
    '^iam workload-identity federated-credential list ' {
        $account = Value-After '--service-account-id'
        $result = @($state.credentials | Where-Object { $_.service_account_id -eq $account }); break
    }
    '^resource-manager folder list-access-bindings ' {
        $folder = Value-After '--id'
        $result = @($state.bindings | Where-Object { $_.folder_id -eq $folder }); break
    }
    '^resource-manager folder create ' {
        $result = [PSCustomObject]@{id=('folder-' + (Value-After '--name')); cloud_id=(Value-After '--cloud-id'); name=(Value-After '--name'); status='ACTIVE'; labels=(Labels)}
        $state.folders = @($state.folders) + $result; break
    }
    '^iam service-account create ' {
        $result = [PSCustomObject]@{id=('sa-' + (Value-After '--name')); folder_id=(Value-After '--folder-id'); name=(Value-After '--name'); labels=(Labels)}
        $state.accounts = @($state.accounts) + $result; break
    }
    '^iam workload-identity oidc federation create ' {
        $result = [PSCustomObject]@{id='federation-gym'; name=(Value-After '--name'); issuer=(Value-After '--issuer'); jwks_url=(Value-After '--jwks-url'); audiences=@((Value-After '--audiences')); labels=(Labels)}
        $state.federations = @($state.federations) + $result; break
    }
    '^iam workload-identity federated-credential create ' {
        $result = [PSCustomObject]@{id=('credential-' + (Value-After '--service-account-id')); service_account_id=(Value-After '--service-account-id'); federation_id=(Value-After '--federation-id'); external_subject_id=(Value-After '--external-subject-id')}
        $state.credentials = @($state.credentials) + $result; break
    }
    '^resource-manager folder add-access-binding ' {
        $result = [PSCustomObject]@{folder_id=(Value-After '--id'); role_id=(Value-After '--role'); subject=@{id=(Value-After '--service-account-id'); type='serviceAccount'}}
        $state.bindings = @($state.bindings) + $result; break
    }
    default { throw "Unexpected fake YC command: $command" }
}

[IO.File]::WriteAllText($statePath, ($state | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding($false)))
ConvertTo-Json -InputObject $result -Depth 20 -Compress
