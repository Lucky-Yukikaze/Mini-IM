$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '..')).Path
$pidFile = Join-Path $repoRoot 'tools\.dev_processes.json'

if (Test-Path $pidFile) {
    Write-Host '检测到已有 dev 进程记录，先尝试停止旧进程...' -ForegroundColor Yellow
    powershell -ExecutionPolicy Bypass -File (Join-Path $repoRoot 'tools\dev_stop.ps1')
}

$serverCmd = "Set-Location '$repoRoot'; python .\server\main.py"
$webCmd = "Set-Location '$repoRoot\web'; npm run dev -- --host 127.0.0.1 --port 5173 --strictPort"

$serverProc = Start-Process -FilePath powershell -ArgumentList @('-NoExit', '-Command', $serverCmd) -PassThru
$webProc = Start-Process -FilePath powershell -ArgumentList @('-NoExit', '-Command', $webCmd) -PassThru

$procInfo = [ordered]@{
    started_at = (Get-Date).ToString('o')
    server_pid = $serverProc.Id
    web_pid = $webProc.Id
}
($procInfo | ConvertTo-Json) | Set-Content -Encoding UTF8 $pidFile

Write-Host "server 窗口 PID: $($serverProc.Id)" -ForegroundColor Green
Write-Host "web 窗口 PID: $($webProc.Id)" -ForegroundColor Green
Write-Host '已启动开发环境。' -ForegroundColor Green
