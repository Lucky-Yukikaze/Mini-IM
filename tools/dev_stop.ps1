$ErrorActionPreference = 'SilentlyContinue'

$repoRoot = (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '..')).Path
$pidFile = Join-Path $repoRoot 'tools\.dev_processes.json'

if (-not (Test-Path $pidFile)) {
    Write-Host '未找到运行中的 dev 进程记录。' -ForegroundColor Yellow
    exit 0
}

$procInfo = Get-Content $pidFile | ConvertFrom-Json

foreach ($pid in @($procInfo.server_pid, $procInfo.web_pid)) {
    if ($pid -and (Get-Process -Id $pid -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $pid -Force
        Write-Host "已停止 PID: $pid" -ForegroundColor Green
    }
}

Remove-Item -Force $pidFile
Write-Host '开发环境已停止。' -ForegroundColor Green
