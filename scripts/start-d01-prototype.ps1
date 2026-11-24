# D-01-03 node-A clickable prototype launcher. Static files only, ASCII-only.
param(
  [int]$Port = 5176,
  [string]$NodeExe = 'node'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:D01_PROTOTYPE_PORT = "$Port"
Write-Output "D-01-03 synthetic prototype: http://127.0.0.1:$Port/"
Write-Output "State is synthetic. No API, database or model calls. Press Ctrl+C to stop."
& $NodeExe (Join-Path $projectRoot 'apps/web/prototype/serve.mjs')
