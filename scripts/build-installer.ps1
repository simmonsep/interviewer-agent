# Build a Windows installer (NSIS) from the desktop product directory.
#
# The repository deliberately ships the product as a folder (`build-desktop-product.ps1`);
# this script adds the install path on top without changing that:
#   1. assemble the product folder into <Destination>/product
#   2. stage `sidecar/` and `settings/` as bundle resources
#   3. generate a bundle overlay (absolute resource paths, NSIS target)
#   4. `tauri build` and leave the installer under <Destination>/target/release/bundle/nsis
#
# The overlay is generated instead of committed because resource paths are absolute and the
# payload is a build artifact; the product config (`tauri.conf.json`) keeps `bundle.active=false`
# so nobody installs a development build by accident.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installer.ps1 `
#     -Destination .private/f07z84 -ConfigSource .private/f07z78/product13/settings
param(
    [string]$Destination = '',
    [string]$ConfigSource = '',
    [string]$NodeExe = 'E:/codex/myagent/workbuddy-d01-v2/.bootstrap/node-v24.19.0-win-x64/node.exe',
    [string]$ProductName = '求职工作台',
    [string]$Version = '0.1.0',
    [string]$Proxy = '',
    [string]$ProductDir = '',
    [switch]$SkipProductBuild
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Destination) { $Destination = Join-Path $projectRoot '.private/f07z84' }
$Destination = [IO.Path]::GetFullPath($Destination)
New-Item -ItemType Directory -Path $Destination -Force | Out-Null

# ---------------------------------------------------------------- toolchain (process scoped)
$env:RUSTUP_HOME = 'E:/codex/toolchains/rustup'
$env:CARGO_HOME = 'E:/codex/toolchains/cargo'
$env:CARGO_TARGET_DIR = Join-Path $Destination 'target'
$env:CARGO_NET_OFFLINE = 'true'
$msvc = 'E:/codex/toolchains/VSBuildTools/VC/Tools/MSVC/14.51.36231'
$sdk = 'E:/Windows Kits/10'
$sdkVersion = '10.0.26100.0'
$env:LIB = (@("$msvc/lib/x64", "$sdk/Lib/$sdkVersion/um/x64", "$sdk/Lib/$sdkVersion/ucrt/x64") -join ';')
$env:INCLUDE = (@("$msvc/include", "$sdk/Include/$sdkVersion/ucrt", "$sdk/Include/$sdkVersion/um",
                  "$sdk/Include/$sdkVersion/shared", "$sdk/Include/$sdkVersion/winrt") -join ';')
$env:PATH = (@("$msvc/bin/HostX64/x64", "$sdk/bin/$sdkVersion/x64",
               'E:/codex/toolchains/cargo/bin',
               (Split-Path -Parent $NodeExe), $env:PATH) -join ';')

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [string]$Label)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Exe @Arguments } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)" }
}

# ---------------------------------------------------------------- 1. product folder
$product = if ($ProductDir) { [IO.Path]::GetFullPath($ProductDir) } else { Join-Path $Destination 'product' }
if (-not $SkipProductBuild) {
    if (Test-Path -LiteralPath $product) { Remove-Item -LiteralPath $product -Recurse -Force }
    Write-Output '[1/4] product folder'
    Invoke-Native 'powershell' @('-NoProfile','-ExecutionPolicy','Bypass','-File',
        (Join-Path $PSScriptRoot 'build-desktop-product.ps1'),
        '-Destination',$product,'-NodeExe',$NodeExe) 'Product build'
}
if (-not (Test-Path -LiteralPath (Join-Path $product 'interview-desktop-probe.exe'))) {
    throw "Product folder missing: $product"
}

# ---------------------------------------------------------------- 2. stage resources
Write-Output '[2/4] stage resources'
$payload = Join-Path $Destination 'payload'
if (Test-Path -LiteralPath $payload) { Remove-Item -LiteralPath $payload -Recurse -Force }
New-Item -ItemType Directory -Path $payload -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $product 'sidecar') -Destination (Join-Path $payload 'sidecar') -Recurse
# `settings/` is the user's configuration: database address, model settings, adapter rules. It is
# NOT bundled. An installer that ships it replaces the user's file on every upgrade (measured:
# a marker added to api.json did not survive an upgrade), and copying the product folder's
# settings would put whatever the builder happened to have - test-database credentials in this
# repository - inside the artifact. Only the editable adapter template ships.
New-Item -ItemType Directory -Path (Join-Path $payload 'settings.template') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot 'configs/tracking-adapters.json') `
    -Destination (Join-Path $payload 'settings.template/tracking-adapters.json')
[IO.File]::WriteAllText((Join-Path $payload 'settings.template/README.txt'),
    "把 api.json、worker.json（以及可选的 model.json）放进同级的 settings 目录。" + [Environment]::NewLine +
    "tracking-adapters.json 可以从这里复制过去再改。" + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false))
# Mark the installed layout. The host uses this to decide where logs and state live; the folder
# build does not ship it, so a copied folder keeps its data beside itself.
$marker = [ordered]@{ layout = 'installed'; version = $Version } | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $payload 'build.json'), $marker, [Text.UTF8Encoding]::new($false))
if ($ConfigSource) {
    # For the acceptance run only: the same files a user would edit, kept out of the package.
    $source = [IO.Path]::GetFullPath($ConfigSource)
    if (-not (Test-Path -LiteralPath $source)) { throw "Config source missing: $source" }
    Write-Output "      test config to be copied after install (not bundled): $source"
}

# ---------------------------------------------------------------- 3. bundle overlay
Write-Output '[3/4] bundle overlay'
$overlay = Join-Path $Destination 'bundle.json'
$resources = [ordered]@{
    (Join-Path $payload 'sidecar') = 'sidecar'
    (Join-Path $payload 'settings.template') = 'settings.template'
    (Join-Path $payload 'build.json') = 'build.json'
}
$config = [ordered]@{
    productName = $ProductName
    version = $Version
    identifier = 'dev.simmonsep.interview.probe'
    bundle = [ordered]@{
        active = $true
        targets = @('nsis')
        icon = @('icons/icon.ico')
        resources = $resources
        shortDescription = '本地求职与面试训练工作台'
        longDescription = '本机运行；数据保存在本机，默认不调用付费模型。'
        windows = [ordered]@{
            nsis = [ordered]@{
                installMode = 'currentUser'
                languages = @('SimpChinese')
                displayLanguageSelector = $false
            }
        }
    }
}
$config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $overlay -Encoding utf8
Write-Output "      $overlay"

# ---------------------------------------------------------------- 4. installer
Write-Output '[4/4] tauri build (nsis)'
$tauri = Join-Path $projectRoot 'node_modules/.bin/tauri.cmd'
if (-not (Test-Path -LiteralPath $tauri)) { throw "Tauri CLI missing: $tauri (npm install --save-dev @tauri-apps/cli)" }
# The CLI downloads the NSIS toolchain once, into %LOCALAPPDATA%\tauri. That is the only network
# step in this script (cargo runs offline), and it needs a proxy on a restricted network.
$proxyVars = @{}
if ($Proxy) {
    $proxyVars['HTTPS_PROXY'] = $Proxy
    $proxyVars['HTTP_PROXY'] = $Proxy
    foreach ($name in $proxyVars.Keys) { Set-Item -Path "Env:$name" -Value $proxyVars[$name] }
    Write-Output "      proxy for the NSIS download: $Proxy"
}
try {
    Invoke-Native $tauri @('build','--config',$overlay,'--bundles','nsis') 'Tauri NSIS build'
} finally {
    foreach ($name in $proxyVars.Keys) { Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue }
}

$installer = Get-ChildItem -Path (Join-Path $env:CARGO_TARGET_DIR 'release/bundle/nsis') -Filter '*.exe' |
    Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $installer) { throw 'Installer not produced' }
Write-Output $installer.FullName
