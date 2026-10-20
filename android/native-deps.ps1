$ErrorActionPreference = 'Stop'
$nativeRoot = Join-Path $PSScriptRoot 'build/native-deps/2.2.2'
$nativePackages = @(
    @{ Abi = 'arm64-v8a'; Target = 'aarch64'; Sha256 = '460a44dc6515cce77d7bbb477a284ea95f612c917ff8d85cd9a7f932a04e9809' },
    @{ Abi = 'x86_64'; Target = 'x86_64'; Sha256 = '4f5b48d90507593e80b250aaaf725ea01f7ea441589491e21e78240f657e118f' }
)
foreach ($nativePackage in $nativePackages) {
    $nativeDestination = Join-Path $nativeRoot $nativePackage.Abi
    $nativeArchive = Join-Path $nativeRoot ($nativePackage.Abi + '.tar.gz')
    New-Item -ItemType Directory -Path $nativeDestination -Force | Out-Null
    if (-not (Test-Path -LiteralPath $nativeArchive)) {
        $nativeUrl = 'https://github.com/lexiforest/curl-impersonate/releases/download/v2.2.2/libcurl-impersonate-v2.2.2.' + $nativePackage.Target + '-linux-android.tar.gz'
        Write-Output "Downloading libcurl-impersonate 2.2.2 ($($nativePackage.Abi))"
        Invoke-WebRequest -Uri $nativeUrl -OutFile $nativeArchive
    }
    if ((Get-FileHash -LiteralPath $nativeArchive -Algorithm SHA256).Hash -ne $nativePackage.Sha256) {
        throw "Native dependency checksum mismatch: $nativeArchive"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $nativeDestination 'libcurl-impersonate.a'))) {
        & tar.exe -xzf $nativeArchive -C $nativeDestination libcurl-impersonate.a include LICENSE LICENSE_BORINGSSL LICENSE_BROTLI LICENSE_CURL LICENSE_NGHTTP2 LICENSE_NGHTTP3 LICENSE_NGTCP2 LICENSE_ZLIB LICENSE_ZSTD
        if ($LASTEXITCODE -ne 0) { throw 'Native dependency extraction failed.' }
    }
}
$nativeNotices = Join-Path $PSScriptRoot 'build/native-deps/notices/native-licenses'
New-Item -ItemType Directory -Path $nativeNotices -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $nativeRoot 'arm64-v8a') -Filter 'LICENSE*' -File | Copy-Item -Destination $nativeNotices
