# Build and assemble the Windows desktop product directory.
#
# PowerShell 5.1. Everything is process-scoped: no global PATH, rustup or cargo
# configuration, and no writes outside .private/f05 and the chosen destination.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-desktop-product.ps1 `
#     -NodeExe 'E:/codex/myagent/node24-local/node_modules/node/bin/node.exe'
#
# The result is a self-contained directory that can be copied outside the source tree:
#   <dest>/interview-desktop-probe.exe   host (window, startup, sidecar supervision)
#   <dest>/sidecar/                      frozen Python (API + 4 Workers)
#   <dest>/sidecar/web/                  built desktop frontend served by that API
#   <dest>/settings/                     external api.json / worker.json (not bundled)
#   <dest>/README.txt                    how to configure and run
param(
    [string]$Destination,
    [string]$NodeExe = 'node',
    [string]$PythonExe = 'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe',
    [string]$PyInstallerPath = 'E:/codex/myagent/outputs/python-interview/.bootstrap/d00-build-tools',
    [switch]$SkipWeb,
    [switch]$SkipSidecar,
    [switch]$SkipHost
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$stage = Join-Path $projectRoot '.private/f05'
$portableRoot = Join-Path $stage 'portable'
if (-not $Destination) { $Destination = Join-Path $portableRoot ([Guid]::NewGuid().ToString('N')) }
$Destination = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $Destination) { throw "Destination already exists; no overwrite performed: $Destination" }

# ---------------------------------------------------------------- process-scoped toolchain
$env:RUSTUP_HOME = 'E:/codex/toolchains/rustup'
$env:CARGO_HOME = 'E:/codex/toolchains/cargo'
$env:CARGO_TARGET_DIR = Join-Path $projectRoot '.private/d-01-02/tauri-target'
$env:CARGO_NET_OFFLINE = 'true'
$msvc = 'E:/codex/toolchains/VSBuildTools/VC/Tools/MSVC/14.51.36231'
$sdk = 'E:/Windows Kits/10'
$sdkVersion = '10.0.26100.0'
$kernel32 = Join-Path $sdk "Lib/$sdkVersion/um/x64/kernel32.Lib"
if (-not (Test-Path -LiteralPath $kernel32)) { throw "Windows SDK import library missing: $kernel32" }
$env:LIB = (@("$msvc/lib/x64", "$sdk/Lib/$sdkVersion/um/x64", "$sdk/Lib/$sdkVersion/ucrt/x64") -join ';')
$env:INCLUDE = (@("$msvc/include", "$sdk/Include/$sdkVersion/ucrt", "$sdk/Include/$sdkVersion/um",
                  "$sdk/Include/$sdkVersion/shared", "$sdk/Include/$sdkVersion/winrt") -join ';')
$env:PATH = (@("$msvc/bin/HostX64/x64", "$sdk/bin/$sdkVersion/x64", $env:PATH) -join ';')

# PowerShell 5.1 turns a native program's stderr into a terminating error while
# $ErrorActionPreference is 'Stop', which would abort on PyInstaller's INFO lines and on
# cargo's "Compiling ..." output. The exit code is the only signal that counts here.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [string]$Label)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Exe @Arguments } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)" }
}

# ---------------------------------------------------------------- 1. web build
$webDist = Join-Path $projectRoot 'apps/web/dist'
if (-not $SkipWeb) {
    Write-Output '[1/4] web: typecheck + production build'
    Push-Location (Join-Path $projectRoot 'apps/web')
    try {
        Invoke-Native $NodeExe @('node_modules/typescript/bin/tsc','--project','tsconfig.json') 'React typecheck'
        Invoke-Native $NodeExe @('node_modules/vite/bin/vite.js','build') 'React build'
        # The host embeds apps/desktop/dist (D-00 page + product startup page). It is a
        # build output, so it is regenerated here; the cargo build must run after it.
        Invoke-Native $NodeExe @('node_modules/vite/bin/vite.js','build','--config','vite.desktop.config.ts') 'Desktop host pages build'
    } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath (Join-Path $webDist 'index.html'))) { throw 'apps/web/dist/index.html missing' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'apps/desktop/dist/startup.html'))) { throw 'apps/desktop/dist/startup.html missing' }

# ---------------------------------------------------------------- 2. frozen sidecar
$sidecarDist = Join-Path $stage 'dist/interview-sidecar'
if (-not $SkipSidecar) {
    Write-Output '[2/4] sidecar: PyInstaller onedir'
    $oldPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $PyInstallerPath
        # Both fixture trees are read at runtime: pm0-05 backs the practice catalog and
        # question-banks backs the builtin banks. A missing one shows up as a 500 on the
        # bank list, so they are bundled explicitly rather than discovered later. Skills (F07Z-160)
        # are frozen into session payloads at creation, so they must ship too.
        $catalog = Join-Path $projectRoot 'fixtures/pm0-05'
        $banks = Join-Path $projectRoot 'fixtures/question-banks'
        $skills = Join-Path $projectRoot 'fixtures/skills'
        Invoke-Native $PythonExe @(
            '-X','utf8','-m','PyInstaller','--noconfirm','--clean','--onedir','--name','interview-sidecar',
            '--paths',(Join-Path $projectRoot 'backend/src'),
            '--add-data',"$catalog;fixtures/pm0-05",
            '--add-data',"$banks;fixtures/question-banks",
            '--add-data',"$skills;fixtures/skills",
            '--hidden-import','interview_agent.desktop.static_site',
            '--hidden-import','interview_agent.desktop.site_sync',
            '--hidden-import','interview_agent.desktop.agent_policy',
            '--hidden-import','interview_agent.desktop.key_vault',
            '--hidden-import','interview_agent.model_settings.routes',
            '--hidden-import','interview_agent.interviews.worker',
            '--hidden-import','interview_agent.materials.worker',
            '--hidden-import','interview_agent.career.worker',
            '--hidden-import','interview_agent.interview_prep.worker',
            # S00O: observability imports the OpenTelemetry SDK lazily (off by default), so
            # PyInstaller cannot see it from the import graph. Without these four the frozen
            # build silently degrades to the no-op tracer.
            '--hidden-import','opentelemetry.trace',
            '--hidden-import','opentelemetry.sdk.trace',
            '--hidden-import','opentelemetry.sdk.trace.export',
            '--hidden-import','opentelemetry.sdk.resources',
            # S05-P1 (decision D'): the MCP catalog is reached only through function-level
            # imports, so the import graph misses both project modules. Without these two the
            # build is green and `mcp-catalog` fails at runtime with ModuleNotFoundError.
            '--hidden-import','interview_agent.interview_prep.mcp_client',
            '--hidden-import','interview_agent.interview_prep.mcp_catalog',
            # mcp.client.stdio imports mcp.os.win32, which needs pywin32 on Windows.
            '--hidden-import','mcp',
            '--hidden-import','mcp.server',
            '--hidden-import','mcp.server.fastmcp',
            '--hidden-import','mcp.client',
            '--hidden-import','mcp.client.stdio',
            '--hidden-import','mcp.shared.exceptions',
            '--hidden-import','mcp.os.win32',
            '--hidden-import','pywintypes',
            '--hidden-import','win32timezone',
            '--distpath',(Join-Path $stage 'dist'),'--workpath',(Join-Path $stage 'build'),
            '--specpath',$stage,(Join-Path $projectRoot 'scripts/desktop-entry.py')) 'Sidecar build'
    } finally { $env:PYTHONPATH = $oldPythonPath }
}

# ---------------------------------------------------------------- 3. host
if (-not $SkipHost) {
    Write-Output '[3/4] host: cargo build --release (custom-protocol)'
    # F07Z-105: release, because `windows_subsystem = "windows"` only strips the console in
    # non-debug builds. The debug host is fine for development but shows a terminal window
    # when a user starts the product from Explorer.
    Invoke-Native 'E:/codex/toolchains/cargo/bin/cargo.exe' @('build','--release','--locked','--offline',
        '--manifest-path',(Join-Path $projectRoot 'apps/desktop/src-tauri/Cargo.toml'),
        '--features','custom-protocol') 'Tauri build'
}
$hostExe = Join-Path $env:CARGO_TARGET_DIR 'release/interview-desktop-probe.exe'
if (-not (Test-Path -LiteralPath $hostExe)) { throw "Host executable missing: $hostExe" }
if (-not (Test-Path -LiteralPath (Join-Path $sidecarDist 'interview-sidecar.exe'))) { throw "Sidecar executable missing: $sidecarDist" }

# ---------------------------------------------------------------- 4. assemble
Write-Output "[4/4] assemble: $Destination"
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
Copy-Item -LiteralPath $hostExe -Destination $Destination
Copy-Item -LiteralPath $sidecarDist -Destination (Join-Path $Destination 'sidecar') -Recurse
Copy-Item -LiteralPath $webDist -Destination (Join-Path $Destination 'sidecar/web') -Recurse
New-Item -ItemType Directory -Path (Join-Path $Destination 'settings') -Force | Out-Null
# Site progress sync reads its adapter rules from the same directory as model.json.
# Ship the editable template (local demo + a blank real-site template); it contains
# no credentials and no unverified site selectors.
Copy-Item -LiteralPath (Join-Path $projectRoot 'configs/tracking-adapters.json') `
    -Destination (Join-Path $Destination 'settings/tracking-adapters.json')
@'
Windows desktop build, 未签名，供本机验收使用。
The executable finds ./sidecar and ./settings relative to itself, independent of the
working directory, so the folder can be copied anywhere outside the source tree.
Put api.json and worker.json in ./settings (or point INTERVIEW_DESKTOP_CONFIG_DIR at
another directory). Only test-database, scripted-provider configuration is accepted.
The host starts the local API plus four Workers - interview, interview-prep, materials and
career (coordinator) - which is why five interview-sidecar processes appear in the process
list (src-tauri/src/main.rs start_workers). It then opens the workbench served by that same
API at http://127.0.0.1:3100.
Closing the window stops only the processes it started; a port already in use by
another program is reported, never adopted.
All four Workers run with the free scripted protocol provider. No live model is
started by this program, and no model request is made unless you configure one
yourself in ./settings/model.json.
Logs and state: ./run-data. The local frontend can no longer be served by Vite in this
build; use the source tree for development instead.
Model access and site progress sync are configured in ./settings/model.json and
./settings/tracking-adapters.json. Edit the adapter selectors against your own
logged-in page before syncing a real site; the bundled local-demo entry points at a
development fixture served on 127.0.0.1:8899 and will not resolve in this folder.
Sync never sends data anywhere: it reads the DOM of a page you opened in a separate
local browser window, and writes a status event only after you confirm each row.
'@ | Set-Content -LiteralPath (Join-Path $Destination 'README.txt') -Encoding utf8
Write-Output $Destination
