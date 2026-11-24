param([switch]$AutoRun)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:INTERVIEW_DESKTOP_RESOURCES = Join-Path $projectRoot '.private/d-00/dist/interview-sidecar'
$env:INTERVIEW_DESKTOP_CONFIG_DIR = Join-Path $projectRoot '.private/d-00/developer-settings'
if (-not (Test-Path -LiteralPath $env:INTERVIEW_DESKTOP_CONFIG_DIR)) {
  New-Item -ItemType Directory -Path $env:INTERVIEW_DESKTOP_CONFIG_DIR | Out-Null
  Copy-Item -LiteralPath (Join-Path $projectRoot '.private/postgres/test.json') -Destination (Join-Path $env:INTERVIEW_DESKTOP_CONFIG_DIR 'api.json')
  Copy-Item -LiteralPath (Join-Path $projectRoot '.private/redis/test-worker.json') -Destination (Join-Path $env:INTERVIEW_DESKTOP_CONFIG_DIR 'worker.json')
}
$env:INTERVIEW_PROBE_OUTPUT = Join-Path $projectRoot ('.private/d-00/tauri-http/' + [Guid]::NewGuid().ToString('N'))
if ($AutoRun) { $env:INTERVIEW_PROBE_AUTORUN = '1' } else { Remove-Item Env:INTERVIEW_PROBE_AUTORUN -ErrorAction SilentlyContinue }
$hostExe = Join-Path $projectRoot '.private/d-00/tauri-target/debug/interview-desktop-probe.exe'
Write-Output "Private receipt directory: $env:INTERVIEW_PROBE_OUTPUT"
& $hostExe
if ($LASTEXITCODE -ne 0) { throw "Desktop host exited: $LASTEXITCODE" }
