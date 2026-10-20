param([switch]$Check)
$ErrorActionPreference = 'Stop'

if (-not $env:ANDROID_HOME -and -not $env:ANDROID_SDK_ROOT) {
    $apkSdk = Join-Path $env:LOCALAPPDATA 'Android/Sdk'
    if (Test-Path -LiteralPath $apkSdk) { $env:ANDROID_HOME = $apkSdk }
}
if (-not $env:JAVA_HOME) {
    $apkJdk = 'C:/Program Files/Android/Android Studio/jbr'
    if (Test-Path -LiteralPath $apkJdk) { $env:JAVA_HOME = $apkJdk }
}
$apkTasks = @(':app:assembleDebug')
& (Join-Path $PSScriptRoot 'native-deps.ps1')
if ($Check) { $apkTasks += @(':app:lintDebug', ':app:assembleDebugAndroidTest') }
& (Join-Path $PSScriptRoot 'gradlew.bat') -p $PSScriptRoot @apkTasks --console=plain
if ($LASTEXITCODE -ne 0) { throw "Android build failed ($LASTEXITCODE)." }
$apkOutput = Join-Path $PSScriptRoot 'artifacts'
New-Item -ItemType Directory -Path $apkOutput -Force | Out-Null
$apkDestination = Join-Path $apkOutput 'TVMON-Android-v0.1.10.apk'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'app/build/outputs/apk/debug/app-debug.apk') -Destination $apkDestination
Write-Output "APK: $apkDestination"
