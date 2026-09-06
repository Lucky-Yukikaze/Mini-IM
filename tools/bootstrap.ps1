$ErrorActionPreference = 'Stop'

Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location ..

Write-Host '执行环境检查...' -ForegroundColor Cyan
powershell -ExecutionPolicy Bypass -File .\tools\check_env.ps1

Write-Host '安装 Python 依赖...' -ForegroundColor Cyan
python -m pip install -r .\server\requirements.txt

Write-Host '生成 protobuf Python 代码...' -ForegroundColor Cyan
python .\server\tools\generate_proto.py

Write-Host '初始化 SQLite...' -ForegroundColor Cyan
python -c "from pathlib import Path; import sys; sys.path.insert(0, 'server'); from storage.sqlite.init_db import init_db; init_db(Path('server/storage/sqlite/miniim.db'))"

Write-Host '安装 Web 依赖...' -ForegroundColor Cyan
Set-Location .\web
npm install

Write-Host '构建 Web...' -ForegroundColor Cyan
npm run build
Set-Location ..

Write-Host '配置 Qt6 客户端...' -ForegroundColor Cyan
$vcpkgToolchain = Join-Path (Get-Location) 'thirdparty_install\vcpkg\scripts\buildsystems\vcpkg.cmake'
if (Test-Path $vcpkgToolchain)
{
    cmake -S .\client -B .\build\client_qt6 -DCMAKE_PREFIX_PATH=D:\Qt\6.8.0\msvc2019_64 -DCMAKE_TOOLCHAIN_FILE=$vcpkgToolchain
}
else
{
    Write-Warning "未找到 vcpkg toolchain: $vcpkgToolchain，将按无 toolchain 方式配置。"
    cmake -S .\client -B .\build\client_qt6 -DCMAKE_PREFIX_PATH=D:\Qt\6.8.0\msvc2019_64
}

Write-Host '编译 Qt6 客户端(Debug)...' -ForegroundColor Cyan
cmake --build .\build\client_qt6 --config Debug

Write-Host '部署 Qt6 运行时与平台插件...' -ForegroundColor Cyan
$windeployqt = 'D:\Qt\6.8.0\msvc2019_64\bin\windeployqt.exe'
$clientExe = Join-Path (Get-Location) 'build\client_qt6\Debug\mini_im_client.exe'
if ((Test-Path $windeployqt) -and (Test-Path $clientExe))
{
    & $windeployqt --debug --qmldir (Join-Path (Get-Location) 'web') $clientExe
}
else
{
    Write-Warning "跳过 windeployqt：未找到 $windeployqt 或 $clientExe"
}

Write-Host '完成。建议下一步：' -ForegroundColor Green
Write-Host '1) 启动开发环境: powershell -ExecutionPolicy Bypass -File .\tools\dev_start.ps1'
Write-Host '2) 停止开发环境: powershell -ExecutionPolicy Bypass -File .\tools\dev_stop.ps1'
