$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:RUSTUP_HOME = 'E:/codex/toolchains/rustup'
$env:CARGO_HOME = 'E:/codex/toolchains/cargo'
$env:CARGO_TARGET_DIR = Join-Path $projectRoot '.private/d-00/tauri-target'
$env:HTTPS_PROXY = 'http://127.0.0.1:7897'
# Import only this child process environment from the selected E-drive MSVC.
$buildEnvironment = & cmd.exe /d /s /c '""E:\codex\toolchains\VSBuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul && set"'
if ($LASTEXITCODE -ne 0) { throw 'MSVC environment initialization failed' }
foreach ($line in $buildEnvironment) {
  if ($line -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1],$Matches[2],'Process') }
}
Push-Location (Join-Path $projectRoot 'apps/web')
try {
  & pnpm exec vite build --config vite.desktop.config.ts
  if ($LASTEXITCODE -ne 0) { throw 'Desktop React build failed' }
  & pnpm exec tsc --project tsconfig.json
  if ($LASTEXITCODE -ne 0) { throw 'React typecheck failed' }
} finally { Pop-Location }
& 'E:/codex/toolchains/cargo/bin/cargo.exe' build --locked --manifest-path (Join-Path $projectRoot 'apps/desktop/src-tauri/Cargo.toml') --features custom-protocol
if ($LASTEXITCODE -ne 0) { throw 'Tauri build failed' }
