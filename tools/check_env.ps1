[CmdletBinding()]
param(
    [string]$QtRoot = $env:QT_DIR,
    [string]$VcpkgRoot = $env:VCPKG_ROOT,
    [string]$VenvPath = '.venv',
    [string]$Python = 'python',
    [switch]$Prerequisites,
    [switch]$RuntimeOnly
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev_common.ps1')
foreach ($name in @('node', 'npm.cmd')) { Get-Command $name -ErrorAction Stop | Out-Null }
if ($Prerequisites) {
    Get-Command $Python -ErrorAction Stop | Out-Null
    Invoke-MiniImCommand $Python @('--version')
} else {
    $interpreter = Join-Path (Get-MiniImPath $VenvPath) 'Scripts/python.exe'
    Assert-MiniImFile $interpreter
    Invoke-MiniImCommand $interpreter @('-c', 'import aioquic, google.protobuf; print("Python runtime dependencies available")')
    if (-not $RuntimeOnly) { Invoke-MiniImCommand $interpreter @('-c', 'import grpc_tools.protoc') }
}
if (-not $RuntimeOnly) {
    Get-Command cmake -ErrorAction Stop | Out-Null
    if (-not $QtRoot) { throw 'Set QT_DIR or pass -QtRoot with the Qt MSVC installation directory' }
    $QtRoot = Get-MiniImPath $QtRoot
    foreach ($module in @('Core', 'Sql', 'Network', 'Widgets', 'WebChannel', 'WebEngineWidgets')) {
        Assert-MiniImFile (Join-Path $QtRoot "lib/cmake/Qt6$module/Qt6${module}Config.cmake")
    }
    Assert-MiniImFile (Join-Path $QtRoot 'bin/windeployqt.exe')
    if (-not $VcpkgRoot) { $VcpkgRoot = 'thirdparty_install/vcpkg' }
    $VcpkgRoot = Get-MiniImPath $VcpkgRoot
    Assert-MiniImFile (Join-Path $VcpkgRoot 'scripts/buildsystems/vcpkg.cmake')
    foreach ($package in @('msquic', 'protobuf')) {
        Assert-MiniImFile (Join-Path $VcpkgRoot "installed/x64-windows/share/$package/$package-config.cmake")
    }
}
foreach ($file in @('server/main.py', 'server/tools/generate_proto.py', 'web/package.json', 'client/CMakeLists.txt')) {
    Assert-MiniImFile (Join-Path $MiniImRoot $file)
}
Write-Host 'Environment checks passed.'
