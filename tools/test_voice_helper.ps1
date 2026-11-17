# No microphone: synthesize a Chinese WAV into the QA directory and feed it
# directly to the installed recognizer through the production helper.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$qaDirectory = Join-Path $projectRoot 'artifacts\voice-qa'
New-Item -ItemType Directory -Force -Path $qaDirectory | Out-Null
$helperPath = Join-Path $projectRoot 'assets\voice-helper.exe'
$probeText = & $helperPath '--probe'
if ($LASTEXITCODE -ne 0) { throw 'Capability probe failed.' }
$probe = $probeText | ConvertFrom-Json
if (-not $probe.available) { throw 'No installed Chinese recognizer.' }
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fixturePath = Join-Path $qaDirectory 'synthesized-chinese.wav'
$fixtureText = '你好，请打开基建页面。'
try {
    $synth.SelectVoice('Microsoft Huihui Desktop')
    $synth.Rate = -2
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo -ArgumentList 16000,([System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen),([System.Speech.AudioFormat.AudioChannel]::Mono)
    $synth.SetOutputToWaveFile($fixturePath, $format)
    $synth.Speak($fixtureText)
    $synth.SetOutputToNull()
} finally { $synth.Dispose() }
$rawEvents = @(& $helperPath '--wave' $fixturePath)
$helperExitCode = $LASTEXITCODE
$events = @($rawEvents | ForEach-Object { $_ | ConvertFrom-Json })
$transcription = $events | Where-Object { $_.type -eq 'result' } | Select-Object -Last 1
$report = [ordered]@{
    mode = 'synthesized_wave_only'; microphone_opened = $false; network_requests = 0
    capability = $probe; fixture_text = $fixtureText; helper_exit_code = $helperExitCode
    transcript = $transcription.text; event_types = @($events.type | Select-Object -Unique)
    passed = ($helperExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($transcription.text))
    limitation = 'Only a synthesized WAV was tested; live microphone audio and recognition quality require a separate user-authorized test.'
}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $qaDirectory 'report.json') -Encoding utf8
$report | ConvertTo-Json -Depth 5
if (-not $report.passed) { throw 'Synthesized WAV recognition did not produce a transcript.' }
