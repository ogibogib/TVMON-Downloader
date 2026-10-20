param(
    [string]$Serial = 'emulator-5554',
    [switch]$Live,
    [switch]$Flow,
    [switch]$Native,
    [switch]$Ech
)
$ErrorActionPreference = 'Stop'
if (@($Live, $Flow, $Native, $Ech).Where({ [bool]$_ }).Count -gt 1) { throw 'Choose only one test mode.' }
& (Join-Path $PSScriptRoot 'build.ps1') -Check
$apkSdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } elseif ($env:ANDROID_SDK_ROOT) { $env:ANDROID_SDK_ROOT } else { Join-Path $env:LOCALAPPDATA 'Android/Sdk' }
$apkAdb = Join-Path $apkSdk 'platform-tools/adb.exe'
& $apkAdb -s $Serial install -r (Join-Path $PSScriptRoot 'app/build/outputs/apk/debug/app-debug.apk')
if ($LASTEXITCODE -ne 0) { throw 'App installation failed.' }
& $apkAdb -s $Serial install -r (Join-Path $PSScriptRoot 'app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk')
if ($LASTEXITCODE -ne 0) { throw 'Test installation failed.' }
$apkArguments = @('-s', $Serial, 'shell', 'am', 'instrument', '-w')
if ($Live) { $apkArguments += @('-e', 'live', 'true') }
if ($Flow) { $apkArguments += @('-e', 'flow', 'true') }
if ($Native) { $apkArguments += @('-e', 'native', 'true') }
if ($Ech) { $apkArguments += @('-e', 'ech', 'true') }
$apkArguments += 'com.tvmon.android.test/com.tvmon.android.AndroidChecks'
$apkResult = & $apkAdb @apkArguments 2>&1
$apkResult | Write-Output
if ($LASTEXITCODE -ne 0 -or ($apkResult -join "`n") -notmatch 'PASS: [0-9]+ checks') { throw 'Android checks failed.' }
