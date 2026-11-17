param(
    [Parameter(Mandatory = $true)][string]$ImagePath,
    [string]$NavigationImagePath = ''
)

# Existing image files are the sole input. No window/capture/input APIs.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$script:failure = 'ocr_failed'

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
    $null = [Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
    $script:asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    function Await-Operation($operation, [Type]$resultType) {
        $task = $script:asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
        return $task.GetAwaiter().GetResult()
    }
    $script:engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
        [Windows.Globalization.Language]::new('zh-Hans-CN'))
    if ($null -eq $script:engine) {
        $script:failure = 'ocr_language_unavailable'
        throw 'language'
    }

    function Read-OcrImage([string]$Path) {
        $stream = $null
        $writer = $null
        $bitmap = $null
        $converted = $null
        try {
            if (-not [System.IO.File]::Exists($Path)) {
                $script:failure = 'ocr_image_missing'
                throw 'input'
            }
            if (([System.IO.FileInfo]$Path).Length -gt 33554432) {
                $script:failure = 'ocr_image_too_large'
                throw 'input'
            }
            $script:failure = 'ocr_image_unreadable'
            $bytes = [System.IO.File]::ReadAllBytes($Path)
            if ($bytes.Length -gt 33554432) {
                $script:failure = 'ocr_image_too_large'
                throw 'input'
            }
            $script:failure = 'ocr_failed'
            $timer = [System.Diagnostics.Stopwatch]::StartNew()
            $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
            $writer = New-Object Windows.Storage.Streams.DataWriter($stream)
            $writer.WriteBytes($bytes)
            $null = Await-Operation ($writer.StoreAsync()) ([uint32])
            $null = $writer.DetachStream()
            $writer.Dispose()
            $writer = $null
            $stream.Seek(0)
            $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            if ($decoder.PixelWidth -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension -or
                $decoder.PixelHeight -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension -or
                $decoder.PixelWidth -gt 10000 -or $decoder.PixelHeight -gt 10000) {
                $script:failure = 'ocr_image_too_large'
                throw 'dimensions'
            }
            $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $converted = [Windows.Graphics.Imaging.SoftwareBitmap]::Convert($bitmap,
                [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
                [Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore)
            $recognized = Await-Operation ($script:engine.RecognizeAsync($converted)) ([Windows.Media.Ocr.OcrResult])
            $timer.Stop()
            $words = New-Object 'System.Collections.Generic.List[object]'
            foreach ($line in $recognized.Lines) {
                foreach ($word in $line.Words) {
                    if ($words.Count -ge 2048 -or $word.Text.Length -gt 512) {
                        $script:failure = 'ocr_output_too_large'
                        throw 'words'
                    }
                    $box = $word.BoundingRect
                    $words.Add(@{text = $word.Text; rect = @{
                        x = [double]$box.X; y = [double]$box.Y
                        width = [double]$box.Width; height = [double]$box.Height
                    }})
                }
            }
            $observation = @{
                width = [int]$decoder.PixelWidth; height = [int]$decoder.PixelHeight
                words = @($words.ToArray()); engine = 'windows.media.ocr:zh-Hans-CN'
                elapsed_ms = [int]$timer.ElapsedMilliseconds
            }
            $encoded = $observation | ConvertTo-Json -Depth 6 -Compress
            if ([System.Text.Encoding]::UTF8.GetByteCount($encoded) -gt 524288) {
                $script:failure = 'ocr_output_too_large'
                throw 'output'
            }
            return $observation
        } finally {
            foreach ($resource in @($converted, $bitmap, $writer, $stream)) {
                if ($null -ne $resource) {
                    try { $resource.Dispose() } catch { }
                }
            }
        }
    }

    $original = Read-OcrImage $ImagePath
    if ([string]::IsNullOrEmpty($NavigationImagePath)) {
        $output = $original | ConvertTo-Json -Depth 6 -Compress
        $limit = 524288
    } else {
        $region = Read-OcrImage $NavigationImagePath
        $output = @{observations = @($original, $region)} | ConvertTo-Json -Depth 8 -Compress
        $limit = 1048704
    }
    if ([System.Text.Encoding]::UTF8.GetByteCount($output) -gt $limit) {
        $script:failure = 'ocr_output_too_large'
        throw 'output'
    }
    [Console]::WriteLine($output)
} catch {
    # Never forward raw exceptions, file paths or host details.
    [Console]::WriteLine((@{ok = $false; code = $script:failure} | ConvertTo-Json -Compress))
}
