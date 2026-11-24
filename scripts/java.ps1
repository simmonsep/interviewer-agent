param(
    [ValidateSet('Versions', 'Test', 'Package', 'Run')]
    [string]$Action = 'Test',
    [switch]$Offline
)
$ErrorActionPreference = 'Stop'
$javaProjectRoot = Split-Path $PSScriptRoot -Parent
& (Join-Path $PSScriptRoot 'bootstrap-java.ps1')
$javaTools = Get-Content -LiteralPath (Join-Path $javaProjectRoot 'configs\java-toolchain.json') -Raw | ConvertFrom-Json
$jdkDirectory = Join-Path $javaProjectRoot ($javaTools.artifacts | Where-Object name -eq 'jdk').directory
$mavenDirectory = Join-Path $javaProjectRoot ($javaTools.artifacts | Where-Object name -eq 'maven').directory
$moduleDirectory = Join-Path $javaProjectRoot 'services\business-java'
$javaRuntimeTemp = Join-Path $javaProjectRoot '.private\java-tmp'
New-Item -ItemType Directory -Path $javaRuntimeTemp -Force | Out-Null
$javaPreviousHome = [Environment]::GetEnvironmentVariable('JAVA_HOME', 'Process')
$javaPreviousPath = $env:Path
try {
    $env:JAVA_HOME = $jdkDirectory
    $env:Path = (Join-Path $jdkDirectory 'bin') + ';' + $javaPreviousPath
    $javaExecutable = Join-Path $jdkDirectory 'bin\java.exe'
    $mavenExecutable = Join-Path $mavenDirectory 'bin\mvn.cmd'
    if ($Action -eq 'Run') {
        $jar = Join-Path $moduleDirectory 'target\interview-business-0.1.0.jar'
        if (-not (Test-Path -LiteralPath $jar)) { throw 'Package the Java module before running it.' }
        & $javaExecutable ('-Djdk.net.unixdomain.tmpdir=' + $javaRuntimeTemp) -jar $jar
    } else {
        $arguments = @('--batch-mode', '--no-transfer-progress', '-f', (Join-Path $moduleDirectory 'pom.xml'),
            '-s', (Join-Path $moduleDirectory '.mvn\settings.xml'), '-gs', (Join-Path $moduleDirectory '.mvn\settings.xml'),
            ('-Dmaven.repo.local=' + (Join-Path $javaProjectRoot '.m2\repository')))
        $arguments += ('-Djava.nio.tmpdir=' + $javaRuntimeTemp)
        if ($Offline) { $arguments += '-o' }
        switch ($Action) {
            'Versions' { $arguments = @('--version') }
            'Test' { $arguments += 'verify' }
            'Package' { $arguments += @('clean', 'package', '-DskipTests') }
        }
        & $mavenExecutable @arguments
    }
    if ($LASTEXITCODE -ne 0) { throw ('Java command failed with exit code ' + $LASTEXITCODE) }
} finally {
    [Environment]::SetEnvironmentVariable('JAVA_HOME', $javaPreviousHome, 'Process')
    $env:Path = $javaPreviousPath
}
