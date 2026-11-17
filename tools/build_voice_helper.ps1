$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$frameworkPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$compilerPath = Join-Path $frameworkPath 'csc.exe'
$speechAssembly = Get-ChildItem -LiteralPath (Join-Path $env:WINDIR 'Microsoft.NET\assembly\GAC_MSIL\System.Speech') -Recurse -Filter 'System.Speech.dll' | Select-Object -First 1 -ExpandProperty FullName
if (-not (Test-Path -LiteralPath $compilerPath) -or -not $speechAssembly) {
    throw 'Windows .NET Framework 4 and System.Speech are required to build the local helper.'
}
$sourcePath = Join-Path $PSScriptRoot 'voice-helper.cs'
$outputPath = Join-Path $projectRoot 'assets\voice-helper.exe'
& $compilerPath '/nologo' '/target:exe' '/platform:x64' '/optimize+' "/reference:$speechAssembly" "/out:$outputPath" $sourcePath
if ($LASTEXITCODE -ne 0) { throw 'Voice helper compilation failed.' }
& $outputPath '--probe'
if ($LASTEXITCODE -ne 0) { throw 'Voice helper capability probe failed.' }
