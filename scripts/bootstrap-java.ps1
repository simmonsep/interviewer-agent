param()
$ErrorActionPreference = 'Stop'
$javaProjectRoot = Split-Path $PSScriptRoot -Parent
$javaTools = Get-Content -LiteralPath (Join-Path $javaProjectRoot 'configs\java-toolchain.json') -Raw | ConvertFrom-Json
Add-Type -AssemblyName System.IO.Compression.FileSystem
foreach ($artifact in $javaTools.artifacts) {
    $archive = Join-Path $javaProjectRoot $artifact.archive
    $destination = Join-Path $javaProjectRoot ('.bootstrap\java\' + $artifact.name)
    $toolDirectory = Join-Path $javaProjectRoot $artifact.directory
    if (-not (Test-Path -LiteralPath $archive)) {
        New-Item -ItemType Directory -Path (Split-Path $archive -Parent) -Force | Out-Null
        $partial = $archive + '.part'
        Invoke-WebRequest -UseBasicParsing -Uri $artifact.url -OutFile $partial
        $downloadDigest = (Get-FileHash -LiteralPath $partial -Algorithm $artifact.hash_algorithm).Hash
        if ($downloadDigest.ToLowerInvariant() -ne $artifact.checksum.ToLowerInvariant()) {
            throw ('Downloaded Java toolchain checksum mismatch: ' + $artifact.name)
        }
        $allowedRoot = [IO.Path]::GetFullPath((Join-Path $javaProjectRoot '.bootstrap\java')) + [IO.Path]::DirectorySeparatorChar
        if (-not ([IO.Path]::GetFullPath($archive).StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase) -and
                  [IO.Path]::GetFullPath($partial).StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase))) {
            throw 'Java download paths must stay within the project toolchain directory.'
        }
        Move-Item -LiteralPath $partial -Destination $archive
    }
    $digest = (Get-FileHash -LiteralPath $archive -Algorithm $artifact.hash_algorithm).Hash
    if ($digest.ToLowerInvariant() -ne $artifact.checksum.ToLowerInvariant()) {
        throw ('Java toolchain checksum mismatch: ' + $artifact.name)
    }
    if (-not (Test-Path -LiteralPath $toolDirectory)) {
        [System.IO.Compression.ZipFile]::ExtractToDirectory($archive, $destination)
    }
}
Write-Output 'Project Java toolchain is ready; global JAVA_HOME and PATH were not changed.'
