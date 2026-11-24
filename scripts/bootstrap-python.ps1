# Installs project-local tools and the locked environment; does not change PATH or registry.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$sources = Get-Content -LiteralPath (Join-Path $projectRoot 'manifests/PY-MIG-01-sources.json') -Raw | ConvertFrom-Json
$uvSource = $sources.packages | Where-Object { $_.name -eq 'uv' }
$uvDir = Join-Path $projectRoot '.bootstrap/uv'
$uvExe = Join-Path $uvDir 'uv.exe'
New-Item -ItemType Directory -Path $uvDir -Force | Out-Null

if (-not (Test-Path -LiteralPath $uvExe)) {
    $archive = Join-Path $uvDir $uvSource.windows_wheel.filename
    Invoke-WebRequest -Uri $uvSource.windows_wheel.url -OutFile $archive
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $uvSource.windows_wheel.sha256) {
        throw 'uv archive checksum mismatch'
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($archive)
    try {
        $entries = @($zip.Entries | Where-Object { $_.Name -eq 'uv.exe' })
        if ($entries.Count -ne 1) { throw 'Expected one uv.exe in the wheel' }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entries[0], $uvExe, $false)
    } finally {
        $zip.Dispose()
    }
}
if ((Get-FileHash -LiteralPath $uvExe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sources.uv_executable_sha256) {
    throw 'uv executable checksum mismatch'
}

$oldPythonDir = $env:UV_PYTHON_INSTALL_DIR
$oldCacheDir = $env:UV_CACHE_DIR
Push-Location $projectRoot
try {
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot '.bootstrap/python'
    $env:UV_CACHE_DIR = Join-Path $projectRoot '.uv-cache'
    $pythonVersion = (Get-Content -LiteralPath '.python-version' -Raw).Trim()
    & $uvExe --no-config python install $pythonVersion --no-bin --no-registry
    if ($LASTEXITCODE -ne 0) { throw "Python installation failed ($LASTEXITCODE)" }
    & $uvExe sync --locked
    if ($LASTEXITCODE -ne 0) { throw "Locked environment sync failed ($LASTEXITCODE)" }
} finally {
    Pop-Location
    $env:UV_PYTHON_INSTALL_DIR = $oldPythonDir
    $env:UV_CACHE_DIR = $oldCacheDir
}
