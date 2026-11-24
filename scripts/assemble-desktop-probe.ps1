param([string]$Destination)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$portableRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot '.private/d-00/portable'))
if (-not $Destination) { $Destination = Join-Path $portableRoot ([Guid]::NewGuid().ToString('N')) }
$Destination = [IO.Path]::GetFullPath($Destination)
if (-not $Destination.StartsWith($portableRoot + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Use a new directory under .private/d-00/portable' }
if (Test-Path -LiteralPath $Destination) { throw 'Destination already exists; no overwrite performed' }
$hostExe = Join-Path $projectRoot '.private/d-00/tauri-target/debug/interview-desktop-probe.exe'
$sidecar = Join-Path $projectRoot '.private/d-00/dist/interview-sidecar'
if (-not (Test-Path -LiteralPath $hostExe) -or -not (Test-Path -LiteralPath (Join-Path $sidecar 'interview-sidecar.exe'))) { throw 'Build the host and sidecar first' }
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
Copy-Item -LiteralPath $hostExe -Destination $Destination
Copy-Item -LiteralPath $sidecar -Destination (Join-Path $Destination 'sidecar') -Recurse
@'
Private developer preview, not a public release or installer.
The executable discovers ./sidecar relative to itself, independent of the working directory.
Supply external api.json/worker.json through INTERVIEW_DESKTOP_CONFIG_DIR.
Set INTERVIEW_PROBE_OUTPUT to a separate writable directory for logs and synthetic test state.
Without configuration the window shows setup guidance and does not run a task.
Only scripted test database configurations are supported by this preview.
No database credentials or model keys are included in this program directory.
'@ | Set-Content -LiteralPath (Join-Path $Destination 'README.txt') -Encoding utf8
Write-Output $Destination
