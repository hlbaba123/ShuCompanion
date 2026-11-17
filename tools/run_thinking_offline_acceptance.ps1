param(
    [string]$Version = '0.3.22-thinking'
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$evidenceRoot = Join-Path $project 'artifacts/20261112-thinking-three-rounds/offline'
$executable = Join-Path $project "release/ShuCompanion-$Version-win-x64/ShuCompanion.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'Final EXE is missing' }
$plans = @(
    @{ name = 'round-01-lifecycle'; arguments = @('--task-dialogue-test', (Join-Path $evidenceRoot 'round-01-lifecycle'), '--task-dialogue-mode', 'seeded-lifecycle') },
    @{ name = 'round-02-lifecycle'; arguments = @('--task-dialogue-test', (Join-Path $evidenceRoot 'round-02-lifecycle'), '--task-dialogue-mode', 'seeded-lifecycle') },
    @{ name = 'round-03-lifecycle'; arguments = @('--task-dialogue-test', (Join-Path $evidenceRoot 'round-03-lifecycle'), '--task-dialogue-mode', 'seeded-lifecycle') },
    @{ name = 'exe-selftest'; arguments = @('--self-test', '--data-dir', (Join-Path $evidenceRoot 'exe-selftest/data'), '--snapshot', (Join-Path $evidenceRoot 'exe-selftest/activity-preview.png')) }
)
foreach ($plan in $plans) {
    if (Test-Path -LiteralPath (Join-Path $evidenceRoot $plan.name)) {
        throw "Do not overwrite $($plan.name) evidence"
    }
}
New-Item -ItemType Directory -Path $evidenceRoot -Force | Out-Null
$originalQtPlatform = $env:QT_QPA_PLATFORM
$env:QT_QPA_PLATFORM = 'offscreen'
$results = @()
try {
    foreach ($plan in $plans) {
        $started = (Get-Date).ToString('o')
        $process = Start-Process -FilePath $executable -ArgumentList $plan.arguments `
            -WorkingDirectory $project -WindowStyle Hidden -PassThru
        $process.WaitForExit()
        $results += @{ test = $plan.name; pid = $process.Id; started = $started;
            ended = (Get-Date).ToString('o'); exit_code = $process.ExitCode }
        ConvertTo-Json -InputObject $results -Depth 5 |
            Set-Content -LiteralPath (Join-Path $evidenceRoot 'exe-exit-codes.json') -Encoding utf8
        Write-Output "$($plan.name) exited with $($process.ExitCode)"
    }
}
finally {
    if ($null -eq $originalQtPlatform) {
        Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }
    else {
        $env:QT_QPA_PLATFORM = $originalQtPlatform
    }
}
if ($results.Where({$_.exit_code -ne 0}).Count) { exit 1 }
