[CmdletBinding()]
param(
    [string]$QtRoot = $env:QT_DIR,
    [string]$VcpkgRoot = $env:VCPKG_ROOT,
    [string]$VenvPath = '.venv',
    [string]$BuildDirectory = 'build/client',
    [switch]$Integration
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev_common.ps1')
& (Join-Path $PSScriptRoot 'bootstrap.ps1') -QtRoot $QtRoot -VcpkgRoot $VcpkgRoot `
    -VenvPath $VenvPath -BuildDirectory $BuildDirectory -SkipInstall
$interpreter = Join-Path (Get-MiniImPath $VenvPath) 'Scripts/python.exe'
Push-Location $MiniImRoot
try {
    Invoke-MiniImCommand $interpreter @('-m', 'unittest', 'discover', 'server/tests', '-v')
    Invoke-MiniImCommand 'npm.cmd' @('--prefix', 'web', 'test')
    & (Join-Path $PSScriptRoot 'test_dev_scripts.ps1') -VenvPath $VenvPath
    if ($Integration) {
        $driver = Join-Path (Get-MiniImPath $BuildDirectory) 'Release/mini_im_native_driver.exe'
        Invoke-MiniImCommand $interpreter @('tools/test_native_flow.py', '--client', $driver)
        Invoke-MiniImCommand $interpreter @('tools/test_server_restart.py', '--client', $driver)
    }
} finally { Pop-Location }
Write-Host 'Selected verification checks passed.'
