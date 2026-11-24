# D-00 minimum-validation probe only. The RELEASE sidecar comes from
# build-desktop-product.ps1; this script exists for the D-00 probe artifact.
param(
    [string]$PythonExe = 'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe',
    [string]$PyInstallerPath = 'E:/codex/myagent/outputs/python-interview/.bootstrap/d00-build-tools'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$oldPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $PyInstallerPath
    & $PythonExe -X utf8 -m PyInstaller --noconfirm --clean --onedir --name interview-sidecar --paths (Join-Path $root 'backend/src') --add-data "$(Join-Path $root 'fixtures/pm0-05');fixtures/pm0-05" --add-data "$(Join-Path $root 'fixtures/skills');fixtures/skills" --distpath (Join-Path $root '.private/d-00/dist') --workpath (Join-Path $root '.private/d-00/build') --hidden-import mcp --hidden-import mcp.server --hidden-import mcp.server.fastmcp --hidden-import mcp.client --hidden-import mcp.client.stdio --hidden-import mcp.shared.exceptions --hidden-import mcp.os.win32 --hidden-import pywintypes --hidden-import win32timezone --hidden-import interview_agent.interview_prep.mcp_catalog --hidden-import interview_agent.interview_prep.mcp_client --specpath (Join-Path $root '.private/d-00') (Join-Path $root 'scripts/desktop-entry.py')
    if ($LASTEXITCODE -ne 0) { throw 'Sidecar build failed' }
} finally { $env:PYTHONPATH = $oldPythonPath }
