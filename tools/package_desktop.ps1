[CmdletBinding()]
param(
    [string]$QtRoot = $env:QT_DIR,
    [string]$BuildDirectory = 'build/client-manifest',
    [string]$OutputDirectory = ('build/packages/mini-im-' + (Get-Date -Format 'yyyyMMdd-HHmmss')),
    [string]$WindowsSdkRoot = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev_common.ps1')
if (-not $QtRoot) { throw 'Pass -QtRoot with the configured Qt MSVC kit directory' }
$qt = Get-MiniImPath $QtRoot
$build = Get-MiniImPath $BuildDirectory
$output = Get-MiniImPath $OutputDirectory
if (Test-Path -LiteralPath $output) { throw "Output must be a new directory: $output" }
$cache = Join-Path $build 'CMakeCache.txt'
Assert-MiniImFile $cache
Assert-MiniImFile (Join-Path $qt 'bin/windeployqt.exe')
$configuredQt = Select-String -LiteralPath $cache -Pattern '^Qt6_DIR:PATH=(.+)$'
if (-not $configuredQt -or (Get-MiniImPath $configuredQt.Matches[0].Groups[1].Value) -ne
    (Get-MiniImPath (Join-Path $qt 'lib/cmake/Qt6'))) { throw 'QtRoot must match the configured CMake Qt kit' }
$instance = Select-String -LiteralPath $cache -Pattern '^CMAKE_GENERATOR_INSTANCE:INTERNAL=(.+)$'
if (-not $instance) { throw 'Configured Visual Studio installation missing from CMakeCache.txt' }
$redist = Join-Path $instance.Matches[0].Groups[1].Value 'VC/Redist/MSVC'
$crt = Get-ChildItem -LiteralPath $redist -Directory | Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } |
    Sort-Object { [version]$_.Name } -Descending | ForEach-Object { Join-Path $_.FullName 'x64/Microsoft.VC143.CRT' } |
    Where-Object { Test-Path -LiteralPath (Join-Path $_ 'vcruntime140.dll') } | Select-Object -First 1
if (-not $crt) { throw 'Visual C++ x64 redistributable CRT directory not found' }
if (-not $WindowsSdkRoot) {
    $WindowsSdkRoot = (Get-ItemProperty 'HKLM:/SOFTWARE/Microsoft/Windows Kits/Installed Roots').KitsRoot10
}
$dxc = Join-Path (Get-MiniImPath $WindowsSdkRoot) 'Redist/D3D/x64'
foreach ($name in @('dxcompiler.dll', 'dxil.dll')) { Assert-MiniImFile (Join-Path $dxc $name) }
Invoke-MiniImCommand 'node' @((Join-Path $MiniImRoot 'web/node_modules/vue-tsc/bin/vue-tsc.js'),
    '--noEmit', '-p', (Join-Path $MiniImRoot 'web/tsconfig.json'))
Invoke-MiniImCommand 'npm.cmd' @('--prefix', (Join-Path $MiniImRoot 'web'), 'run', 'build')
Invoke-MiniImCommand 'cmake' @('--build', $build, '--config', 'Release', '--target', 'mini_im_client')
$release = Join-Path $build 'Release'
foreach ($name in @('mini_im_client.exe', 'Qt6WebEngineCore.dll', 'msquic.dll', 'libprotobuf.dll')) {
    Assert-MiniImFile (Join-Path $release $name)
}
New-Item -ItemType Directory -Path $output | Out-Null
Copy-Item -LiteralPath (Join-Path $release 'mini_im_client.exe') -Destination $output
Get-ChildItem -LiteralPath $release -Filter '*.dll' -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $output
}
Invoke-MiniImCommand (Join-Path $qt 'bin/windeployqt.exe') @('--release', '--no-compiler-runtime',
    '--no-system-dxc-compiler', '--include-plugins', 'qoffscreen', '--dir', $output, (Join-Path $output 'mini_im_client.exe'))
Get-ChildItem -LiteralPath $crt -Filter '*.dll' -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $output
}
foreach ($name in @('dxcompiler.dll', 'dxil.dll')) {
    Copy-Item -LiteralPath (Join-Path $dxc $name) -Destination $output
}
$web = New-Item -ItemType Directory -Path (Join-Path $output 'web')
Copy-Item -LiteralPath (Join-Path $MiniImRoot 'web/dist') -Destination $web.FullName -Recurse
Set-Content -LiteralPath (Join-Path $output 'qt.conf') -Value "[Paths]`nPrefix=." -Encoding UTF8
foreach ($name in @('platforms/qwindows.dll', 'platforms/qoffscreen.dll', 'sqldrivers/qsqlite.dll',
    'QtWebEngineProcess.exe', 'resources/qtwebengine_resources.pak', 'web/dist/index.html',
    'vcruntime140.dll', 'msvcp140.dll', 'dxcompiler.dll', 'dxil.dll')) {
    Assert-MiniImFile (Join-Path $output $name)
}
$files = @(Get-ChildItem -LiteralPath $output -File -Recurse | ForEach-Object {
    @{ path = $_.FullName.Substring($output.Length + 1).Replace('\', '/'); bytes = $_.Length;
       sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
})
$commit = & git -C $MiniImRoot rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw 'Cannot record source revision' }
$changes = @(& git -C $MiniImRoot status --short)
@{ format = 1; created = (Get-Date).ToString('o'); sourceCommit = $commit; sourceChanges = $changes;
   configuration = 'Release'; platform = 'windows-x64'; files = $files } |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $output 'package-manifest.json') -Encoding UTF8
Write-Host "Desktop package ready: $output"
