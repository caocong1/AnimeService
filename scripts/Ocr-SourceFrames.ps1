param([Parameter(Mandatory=$true)][string]$Directory)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
function Await-Result($Operation, [Type]$ResultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null,@($Operation))
    return $task.GetAwaiter().GetResult()
}
$language = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | Where-Object { $_.LanguageTag -like 'zh*' } | Select-Object -First 1
if (-not $language) {
    @{status='unavailable';reason='No installed Chinese Windows OCR language'} | ConvertTo-Json -Compress
    exit 0
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
$frames = @()
foreach ($image in (Get-ChildItem -LiteralPath $Directory -File -Filter '*.png' | Sort-Object Name)) {
    $file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($image.FullName)) ([Windows.Storage.StorageFile])
    $stream = Await-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        try {
            $result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $frames += @{file=$image.Name;text=$result.Text}
        } finally { $bitmap.Dispose() }
    } finally { $stream.Dispose() }
}
@{status='ok';language=$language.LanguageTag;frames=$frames;timing_is_estimated=$true} | ConvertTo-Json -Depth 5 -Compress |
    ForEach-Object { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); [Console]::WriteLine($_) }
