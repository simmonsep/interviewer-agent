# D-01-02 development host build. Everything is process-scoped: no global PATH,
# no global rustup/cargo configuration, and an output directory separate from D-00.
# The default D-00 probe entry point (desktop.html / vite.desktop.config.ts) is
# still built, so the same binary can run both modes.
param(
  [string]$NodeExe = 'node'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:RUSTUP_HOME = 'E:/codex/toolchains/rustup'
$env:CARGO_HOME = 'E:/codex/toolchains/cargo'
$env:CARGO_TARGET_DIR = Join-Path $projectRoot '.private/d-01-02/tauri-target'
$env:HTTPS_PROXY = 'http://127.0.0.1:7897'
$env:CARGO_NET_OFFLINE = 'true'
# Process-scoped MSVC + Windows SDK environment. The D-00 script imports vcvars64,
# which calls reg.exe to discover the SDK; reg.exe is blocked by this session's
# program blacklist. We therefore set the known E-drive paths explicitly instead
# of launching the blocked program. Nothing is written to the global environment.
$msvc = 'E:/codex/toolchains/VSBuildTools/VC/Tools/MSVC/14.51.36231'
$sdk = 'E:/Windows Kits/10'
$sdkVersion = '10.0.26100.0'
$kernel32 = Join-Path $sdk "Lib/$sdkVersion/um/x64/kernel32.Lib"
if (-not (Test-Path -LiteralPath $kernel32)) { throw "Windows SDK import library missing: $kernel32" }
$env:LIB = (@("$msvc/lib/x64", "$sdk/Lib/$sdkVersion/um/x64", "$sdk/Lib/$sdkVersion/ucrt/x64") -join ';')
$env:INCLUDE = (@("$msvc/include", "$sdk/Include/$sdkVersion/ucrt", "$sdk/Include/$sdkVersion/um",
                   "$sdk/Include/$sdkVersion/shared", "$sdk/Include/$sdkVersion/winrt") -join ';')
$env:PATH = (@("$msvc/bin/HostX64/x64", "$sdk/bin/$sdkVersion/x64",
               'E:/codex/toolchains/VSBuildTools/Common7/IDE', $env:PATH) -join ';')
Write-Output ("MSVC/SDK environment (process only): kernel32.Lib found under " + $sdk)
Push-Location (Join-Path $projectRoot 'apps/web')
try {
  & $NodeExe 'node_modules/vite/bin/vite.js' build --config vite.desktop.config.ts
  if ($LASTEXITCODE -ne 0) { throw 'Desktop React build failed' }
  & $NodeExe 'node_modules/typescript/bin/tsc' --project tsconfig.json
  if ($LASTEXITCODE -ne 0) { throw 'React typecheck failed' }
} finally { Pop-Location }
& 'E:/codex/toolchains/cargo/bin/cargo.exe' build --locked --offline --manifest-path (Join-Path $projectRoot 'apps/desktop/src-tauri/Cargo.toml') --features custom-protocol
if ($LASTEXITCODE -ne 0) { throw 'Tauri build failed' }
Write-Output ("Built host: " + (Join-Path $env:CARGO_TARGET_DIR 'debug/interview-desktop-probe.exe'))
