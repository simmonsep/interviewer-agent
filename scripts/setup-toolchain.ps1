$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This bootstrap currently supports Windows only.'
}
if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne 'X64') {
    throw 'This bootstrap currently supports Windows x64 only.'
}
$taskRoot = Split-Path $PSScriptRoot
$taskManifest = Get-Content -LiteralPath (Join-Path $taskRoot 'manifests\toolchain.json') -Raw | ConvertFrom-Json
$taskArchive = Join-Path $taskRoot $taskManifest.archive_relative_path
$taskChecksums = Join-Path $taskRoot $taskManifest.checksum_relative_path
$taskNode = Join-Path $taskRoot $taskManifest.node_relative_path
$taskNpm = Join-Path $taskRoot $taskManifest.npm_relative_path
New-Item -ItemType Directory -Path (Split-Path $taskArchive) -Force | Out-Null

if (-not (Test-Path -LiteralPath $taskChecksums)) {
    Invoke-WebRequest -Uri $taskManifest.checksum_url -OutFile $taskChecksums -TimeoutSec 30
}
$taskChecksumHash = (Get-FileHash -LiteralPath $taskChecksums -Algorithm SHA256).Hash.ToLowerInvariant()
if ($taskChecksumHash -ne $taskManifest.checksum_sha256) {
    throw 'Official checksum file differs from the pinned manifest. Stop and inspect; no automatic overwrite.'
}
$taskEntry = @(Get-Content -LiteralPath $taskChecksums | Where-Object {
    $_ -match '^[0-9a-f]{64}\s+node-v24\.19\.0-win-x64\.zip$'
})
if ($taskEntry.Count -ne 1 -or ($taskEntry[0] -split '\s+')[0] -ne $taskManifest.archive_sha256) {
    throw 'Archive checksum entry does not match the pinned manifest.'
}
if (-not (Test-Path -LiteralPath $taskArchive)) {
    Invoke-WebRequest -Uri $taskManifest.archive_url -OutFile $taskArchive -TimeoutSec 45
}
$taskArchiveHash = (Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($taskArchiveHash -ne $taskManifest.archive_sha256) {
    throw 'Archive SHA256 mismatch. Refusing extraction; preserve the failed artifact for inspection.'
}

$taskToolDirectory = Split-Path $taskNode
if (-not (Test-Path -LiteralPath $taskToolDirectory)) {
    Expand-Archive -LiteralPath $taskArchive -DestinationPath (Join-Path $taskRoot '.bootstrap')
}
if (-not (Test-Path -LiteralPath $taskNode) -or -not (Test-Path -LiteralPath $taskNpm)) {
    throw 'Existing toolchain is incomplete. Refusing to overwrite it automatically.'
}
$taskNodeVersion = & $taskNode --version
if ($LASTEXITCODE -ne 0 -or $taskNodeVersion -ne "v$($taskManifest.node_version)") {
    throw 'Project Node version does not match manifest.'
}
$taskNpmVersion = & $taskNode $taskNpm --version
if ($LASTEXITCODE -ne 0 -or $taskNpmVersion -ne $taskManifest.npm_version) {
    throw 'Project npm version does not match manifest.'
}
[ordered]@{
    node = $taskNodeVersion
    npm = $taskNpmVersion
    archive_sha256 = $taskArchiveHash
    verification = 'Pinned hash and official HTTPS checksum; GPG signature not verified'
    global_path_modified = $false
} | ConvertTo-Json
