$ErrorActionPreference = 'SilentlyContinue'

Write-Host '== Mini-IM 环境检查 ==' -ForegroundColor Cyan

function Test-Cmd($name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        Write-Host "[MISSING] $name" -ForegroundColor Yellow
        return $false
    }

    Write-Host "[OK] $name -> $($cmd.Source)" -ForegroundColor Green
    return $true
}

Test-Cmd python | Out-Null
Test-Cmd node | Out-Null
Test-Cmd npm | Out-Null
Test-Cmd cmake | Out-Null
Test-Cmd qmake | Out-Null
if (-not (Test-Cmd cl)) {
    Write-Host '[WARN] cl 不在当前 shell PATH，使用 CMake + VS 生成器仍可编译。' -ForegroundColor Yellow
}

$qt6Qmake = 'D:\Qt\6.8.0\msvc2019_64\bin\qmake.exe'
$qt6CoreConfig = 'D:\Qt\6.8.0\msvc2019_64\lib\cmake\Qt6\Qt6Config.cmake'
$qt6WebEngineConfig = 'D:\Qt\6.8.0\msvc2019_64\lib\cmake\Qt6WebEngineWidgets\Qt6WebEngineWidgetsConfig.cmake'
if (Test-Path $qt6Qmake) {
    Write-Host "[OK] qmake(Qt6) -> $qt6Qmake" -ForegroundColor Green
    & $qt6Qmake -v
} else {
    Write-Host '[MISSING] qmake(Qt6)' -ForegroundColor Yellow
}
Write-Host "[OK] Qt6Config: $(Test-Path $qt6CoreConfig)" -ForegroundColor Green
Write-Host "[OK] Qt6WebEngineWidgetsConfig: $(Test-Path $qt6WebEngineConfig)" -ForegroundColor Green

$protoGen = 'D:\Codex\Mini-IM\server\tools\generate_proto.py'
if (Test-Path $protoGen) {
    Write-Host "[OK] proto 脚本: $protoGen" -ForegroundColor Green
}

$serverMain = 'D:\Codex\Mini-IM\server\main.py'
$webPkg = 'D:\Codex\Mini-IM\web\package.json'
$clientCmake = 'D:\Codex\Mini-IM\client\CMakeLists.txt'
Write-Host "[OK] server: $(Test-Path $serverMain)" -ForegroundColor Green
Write-Host "[OK] web: $(Test-Path $webPkg)" -ForegroundColor Green
Write-Host "[OK] client: $(Test-Path $clientCmake)" -ForegroundColor Green

Write-Host '== 检查完成 ==' -ForegroundColor Cyan
