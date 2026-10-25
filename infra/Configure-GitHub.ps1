[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'bootstrap.json'),
    [string]$StatePath = (Join-Path (Split-Path $PSScriptRoot -Parent) '.local/bootstrap-state.json'),
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$config = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigPath | ConvertFrom-Json
$state = Get-Content -Raw -Encoding UTF8 -LiteralPath $StatePath | ConvertFrom-Json
if ($config.repository -cne $state.repository -or $config.cloud_id -cne $state.cloud_id) { throw 'Bootstrap state does not match configuration.' }
if ($config.repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw 'Invalid repository.' }

$token = $env:GH_TOKEN
$previousPrompt = $env:GIT_TERMINAL_PROMPT
try {
    if (-not $token) {
        $env:GIT_TERMINAL_PROMPT = '0'
        $credentialLines = @('protocol=https', 'host=github.com', '') | git credential fill
        if ($LASTEXITCODE -ne 0) { throw 'Existing GitHub authorization is unavailable.' }
        foreach ($line in $credentialLines) {
            $pair = $line.Split('=', 2)
            if ($pair.Count -eq 2 -and $pair[0] -ceq 'password') { $token = $pair[1] }
        }
    }
    if (-not $token) { throw 'GitHub authorization is required.' }
    $headers = @{Authorization="Bearer $token"; Accept='application/vnd.github+json'; 'X-GitHub-Api-Version'='2022-11-28'}

    function Invoke-GitHub {
        param([string]$Method, [string]$Path, $Body = $null)
        $parameters = @{Method=$Method; Uri="https://api.github.com$Path"; Headers=$headers}
        if ($null -ne $Body) {
            $parameters.ContentType = 'application/json'
            $parameters.Body = $Body | ConvertTo-Json -Depth 12 -Compress
        }
        try { return (Invoke-RestMethod @parameters) }
        catch {
            $code = 'unknown'
            if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
            throw "GitHub $Method $Path failed (HTTP $code). Credentials and response bodies are not logged."
        }
    }

    $repoPath = "/repos/$($config.repository)"
    $repository = Invoke-GitHub GET $repoPath
    if (-not $repository.permissions.admin -or $repository.default_branch -cne 'main') { throw 'Repository admin access and default branch main are required.' }
    $oidc = Invoke-GitHub GET "$repoPath/actions/oidc/customization/sub"
    $actualPrefix = "repo:$($config.repository)"
    if ($oidc.PSObject.Properties['sub_claim_prefix']) { $actualPrefix = $oidc.sub_claim_prefix }
    if (-not $oidc.use_default -or $actualPrefix -cne $config.repository_subject_prefix) { throw 'Actual GitHub OIDC subject differs from bootstrap configuration.' }
    $existingEnvironments = (Invoke-GitHub GET "$repoPath/environments?per_page=100").environments
    $plans = @()

    # Preflight both environments before modifying either one.
    foreach ($name in @('staging', 'production')) {
        $environment = @($existingEnvironments | Where-Object { $_.name -ceq $name })
        $existingPolicies = @()
        $existingVariables = @()
        if ($environment.Count -gt 1) { throw 'Ambiguous environment.' }
        if ($environment.Count -eq 1) {
            $environment = @(Invoke-GitHub GET "$repoPath/environments/$name")
            $policy = $environment[0].deployment_branch_policy
            if (-not $policy -or -not $policy.custom_branch_policies -or $policy.protected_branches) { throw "Unexpected protection policy on $name. Refusing to replace it." }
            $existingPolicies = @((Invoke-GitHub GET "$repoPath/environments/$name/deployment-branch-policies?per_page=100").branch_policies)
            foreach ($item in $existingPolicies) {
                if ($item.name -cne 'main' -or $item.type -cne 'branch') { throw "Unexpected branch/tag policy on $name." }
            }
            $reviewRules = @($environment[0].protection_rules | Where-Object { $_.type -eq 'required_reviewers' })
            if ($name -ceq 'production') {
                if ($environment[0].can_admins_bypass -ne $false) { throw 'Production allows administrator bypass; review the drift before applying.' }
                if ($reviewRules.Count -ne 1 -or $reviewRules[0].prevent_self_review -or @($reviewRules[0].reviewers).Count -ne 1 -or $reviewRules[0].reviewers[0].reviewer.id -ne $repository.owner.id) { throw 'Unexpected production review policy; no weakening or replacement is allowed.' }
            }
            $existingVariables = @((Invoke-GitHub GET "/repositories/$($repository.id)/environments/$name/variables?per_page=100").variables)
        }
        $values = [ordered]@{
            YC_CLOUD_ID=$state.cloud_id
            YC_FOLDER_ID=$state.folders.$name
            YC_OTHER_FOLDER_ID=$state.folders.($(if ($name -ceq 'staging') { 'production' } else { 'staging' }))
            YC_SERVICE_ACCOUNT_ID=$state.accounts.("${name}_ci")
            YC_FEDERATION_ID=$state.federation_id
            YC_OIDC_AUDIENCE=$state.audience
            GYM_ENVIRONMENT=$name
            GYM_MONTHLY_BUDGET_RUB=[string]$config.monthly_budget_rub
        }
        foreach ($entry in $values.GetEnumerator()) {
            $existing = @($existingVariables | Where-Object { $_.name -ceq $entry.Key })
            if ($existing.Count -eq 1 -and $existing[0].value -cne $entry.Value) { throw "Variable $($entry.Key) on $name differs; review the drift before applying." }
        }
        $plans += [PSCustomObject]@{name=$name; exists=($environment.Count -eq 1); policies=$existingPolicies; variables=$existingVariables; values=$values}
    }

    if (-not $Apply) {
        [PSCustomObject]@{mode='plan'; repository=$config.repository; oidc_subject_prefix=$actualPrefix; environments=@($plans | ForEach-Object { [PSCustomObject]@{name=$_.name; exists=$_.exists; branch='main'; production_reviewer=($_.name -ceq 'production'); variable_names=@($_.values.Keys)} })} | ConvertTo-Json -Depth 8
        return
    }
    foreach ($plan in $plans) {
        $name = $plan.name
        if (-not $plan.exists) {
            $body = @{deployment_branch_policy=@{protected_branches=$false; custom_branch_policies=$true}; can_admins_bypass=$false}
            if ($name -ceq 'production') { $body.reviewers = @(@{type='User'; id=$repository.owner.id}); $body.prevent_self_review=$false }
            $null = Invoke-GitHub PUT "$repoPath/environments/$name" $body
        }
        if (-not $plan.policies.Count) {
            $null = Invoke-GitHub POST "$repoPath/environments/$name/deployment-branch-policies" @{name='main'; type='branch'}
        }
        foreach ($entry in $plan.values.GetEnumerator()) {
            if (-not @($plan.variables | Where-Object { $_.name -ceq $entry.Key }).Count) {
                $null = Invoke-GitHub POST "/repositories/$($repository.id)/environments/$name/variables" @{name=$entry.Key; value=$entry.Value}
            }
        }
        Write-Output "Configured GitHub environment: $name (main only; no secrets stored)"
    }
} finally {
    $env:GIT_TERMINAL_PROMPT = $previousPrompt
    Remove-Variable token,headers,credentialLines -ErrorAction SilentlyContinue
}
