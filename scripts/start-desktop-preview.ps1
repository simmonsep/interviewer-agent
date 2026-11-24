# Opens the D-01-02 native Tauri preview. The API (3100), the free scripted Worker
# and Vite (5174) must already be running: this launcher starts and owns nothing
# but its own window. Closing the window closes only that process.
# NOTE: keep this file ASCII-only; Windows PowerShell 5.1 reads .ps1 as ANSI.
param(
  [int]$DebugPort = 0,
  [switch]$SkipServiceCheck
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$hostExe = Join-Path $projectRoot '.private/d-01-02/tauri-target/debug/interview-desktop-probe.exe'
if (-not (Test-Path -LiteralPath $hostExe)) { throw "Run scripts/build-desktop-preview.ps1 first: $hostExe" }

if (-not $SkipServiceCheck) {
  foreach ($entry in @(@{ Name='FastAPI'; Url='http://127.0.0.1:3100/health' },
                       @{ Name='Vite'; Url='http://127.0.0.1:5174/' })) {
    try {
      $response = Invoke-WebRequest -UseBasicParsing -Uri $entry.Url -TimeoutSec 5
      Write-Output ("{0} reachable: HTTP {1}" -f $entry.Name, [int]$response.StatusCode)
    } catch {
      throw ("{0} is not reachable ({1}). Start the test API, the free scripted Worker and Vite first; see docs/usage/D-01-02-desktop-preview.md" -f $entry.Name, $entry.Url)
    }
  }
}

$env:INTERVIEW_DESKTOP_PREVIEW = '1'
$env:INTERVIEW_PROBE_OUTPUT = Join-Path $projectRoot '.private/d-01-02/preview-run'
Remove-Item Env:INTERVIEW_PROBE_AUTORUN -ErrorAction SilentlyContinue
Remove-Item Env:INTERVIEW_DESKTOP_PREVIEW_CDP_PORT -ErrorAction SilentlyContinue
if ($DebugPort -gt 0) {
  # Optional loopback WebView2 DevTools protocol port for automated acceptance.
  # The host enables it only when this variable is present; default launches have
  # no debug port at all.
  $env:INTERVIEW_DESKTOP_PREVIEW_CDP_PORT = "$DebugPort"
}
Write-Output "Opening native window: $hostExe"
& $hostExe
if ($LASTEXITCODE -ne 0) { throw "Desktop host exited: $LASTEXITCODE" }
