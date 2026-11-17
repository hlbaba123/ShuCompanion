param(
    [string]$Version = '0.3.22-thinking'
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$outputRoot = Join-Path $project 'artifacts/20261112-thinking-three-rounds'
$executable = Join-Path $project "release/ShuCompanion-$Version-win-x64/ShuCompanion.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'Final EXE is missing' }
# All cases are the unchanged natural-language cases from the preceding run.
# There are no developer-selected copilot codes or seeded model answers here.
foreach ($roundName in @('round-01', 'round-02', 'round-03')) {
    $questions = Join-Path $outputRoot "$roundName-questions.json"
    if (-not (Test-Path -LiteralPath $questions -PathType Leaf)) { throw "Missing $questions" }
    if (Test-Path -LiteralPath (Join-Path $outputRoot $roundName)) { throw "Do not overwrite $roundName evidence" }
}
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
$results = @()
foreach ($roundName in @('round-01', 'round-02', 'round-03')) {
    $questions = Join-Path $outputRoot "$roundName-questions.json"
    $destination = Join-Path $outputRoot $roundName
    $process = Start-Process -FilePath $executable -ArgumentList @(
        '--conversation-test', $questions, '--conversation-output', $destination,
        '--allow-maa-import-test', '--allow-maa-queue-conversation-test'
    ) -WorkingDirectory $project -WindowStyle Hidden -PassThru
    $started = (Get-Date).ToString('o')
    @{ round = $roundName; pid = $process.Id; started = $started } |
        ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $outputRoot 'launches.jsonl') -Encoding utf8
    $process.WaitForExit()
    $results += @{ round = $roundName; pid = $process.Id; started = $started;
        ended = (Get-Date).ToString('o'); exit_code = $process.ExitCode }
    ConvertTo-Json -InputObject $results -Depth 5 |
        Set-Content -LiteralPath (Join-Path $outputRoot 'exit-codes.json') -Encoding utf8
    Write-Output "$roundName exited with $($process.ExitCode)"
}
