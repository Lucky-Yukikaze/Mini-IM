[CmdletBinding()]
param(
    [string]$QtRoot = $env:QT_DIR,
    [string]$VcpkgRoot = $env:VCPKG_ROOT,
    [string]$VenvPath = '.venv',
    [string]$BuildDirectory = 'build/client-manifest',
    [string]$Python = 'python',
    [switch]$SkipInstall
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev_common.ps1')
& (Join-Path $PSScriptRoot 'check_env.ps1') -QtRoot $QtRoot -VcpkgRoot $VcpkgRoot -Python $Python -Prerequisites
$venv = Get-MiniImPath $VenvPath
$build = Get-MiniImPath $BuildDirectory
$interpreter = Join-Path $venv 'Scripts/python.exe'
if (-not $SkipInstall) {
    if (-not (Test-Path -LiteralPath $interpreter)) { Invoke-MiniImCommand $Python @('-m', 'venv', $venv) }
    Invoke-MiniImCommand $interpreter @('-m', 'pip', 'install', '-r', (Join-Path $MiniImRoot 'server/requirements.txt'))
    Invoke-MiniImCommand 'npm.cmd' @('--prefix', (Join-Path $MiniImRoot 'web'), 'ci')
}
& (Join-Path $PSScriptRoot 'check_env.ps1') -QtRoot $QtRoot -VcpkgRoot $VcpkgRoot -VenvPath $VenvPath
Invoke-MiniImCommand $interpreter @((Join-Path $MiniImRoot 'server/tools/generate_proto.py'))
$typeChecker = Join-Path $MiniImRoot 'web/node_modules/vue-tsc/bin/vue-tsc.js'
Assert-MiniImFile $typeChecker
Invoke-MiniImCommand 'node' @($typeChecker, '--noEmit', '-p', (Join-Path $MiniImRoot 'web/tsconfig.json'))
Invoke-MiniImCommand 'npm.cmd' @('--prefix', (Join-Path $MiniImRoot 'web'), 'run', 'build')
if (-not $VcpkgRoot) { $VcpkgRoot = 'thirdparty_install/vcpkg' }
$toolchain = Join-Path (Get-MiniImPath $VcpkgRoot) 'scripts/buildsystems/vcpkg.cmake'
Invoke-MiniImCommand 'cmake' @('-S', (Join-Path $MiniImRoot 'client'), '-B', $build, '-G', 'Visual Studio 17 2022', '-A', 'x64',
    "-DCMAKE_TOOLCHAIN_FILE=$toolchain", '-DVCPKG_TARGET_TRIPLET=x64-windows',
    '-DVCPKG_MANIFEST_MODE=ON', '-DVCPKG_MANIFEST_INSTALL=ON',
    "-DVCPKG_MANIFEST_DIR=$(Join-Path $MiniImRoot 'client')", "-DVCPKG_INSTALLED_DIR=$(Join-Path $build 'vcpkg_installed')",
    "-DCMAKE_PREFIX_PATH=$(Get-MiniImPath $QtRoot)", '-DBUILD_TESTING=ON')
Invoke-MiniImCommand 'cmake' @('--build', $build, '--config', 'Release')
$release = Join-Path $build 'Release'
$instance = Select-String -LiteralPath (Join-Path $build 'CMakeCache.txt') -Pattern '^CMAKE_GENERATOR_INSTANCE:INTERNAL=(.+)$'
if (-not $instance) { throw 'Configured Visual Studio installation is missing from CMakeCache.txt' }
$previousVcDirectory = $env:VCINSTALLDIR
try {
    $env:VCINSTALLDIR = Join-Path $instance.Matches[0].Groups[1].Value 'VC'
    Invoke-MiniImCommand (Join-Path (Get-MiniImPath $QtRoot) 'bin/windeployqt.exe') @('--release', '--compiler-runtime', '--dir', $release,
        (Join-Path $release 'mini_im_client.exe'), (Join-Path $release 'mini_im_native_driver.exe'))
} finally { $env:VCINSTALLDIR = $previousVcDirectory }
Assert-MiniImFile (Join-Path $release 'Qt6Sql.dll')
Assert-MiniImFile (Join-Path $release 'sqldrivers/qsqlite.dll')
Invoke-MiniImCommand 'ctest' @('--test-dir', $build, '-C', 'Release', '--output-on-failure')
Write-Host "Bootstrap completed: $release"
