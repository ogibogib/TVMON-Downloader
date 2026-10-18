param(
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'

$pythonCommand = (Get-Command $Python -ErrorAction Stop).Source

$projectRoot = $PSScriptRoot
$sourceRoot = Join-Path $projectRoot 'source'
$specPath = Join-Path $projectRoot 'manager-build/TVMON-Download-Manager.spec'
$distRoot = Join-Path $projectRoot 'manager-search-build'
$workRoot = Join-Path $projectRoot 'manager-search-work'

if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot 'tvmon_downloader.py') -PathType Leaf)) {
    throw "Source entry point not found: $sourceRoot"
}

Push-Location -LiteralPath $sourceRoot
try {
    & $pythonCommand -X utf8 -m PyInstaller --noconfirm --distpath $distRoot --workpath $workRoot $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot 'manager/TVMON-Download-Manager/사용방법.txt') `
        -Destination (Join-Path $distRoot 'TVMON-Download-Manager/사용방법.txt')
    Write-Output "Build ready: $(Join-Path $distRoot 'TVMON-Download-Manager/TVMON-Download-Manager.exe')"
}
finally {
    Pop-Location
}
