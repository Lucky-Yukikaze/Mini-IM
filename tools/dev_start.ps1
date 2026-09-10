[CmdletBinding()]
param(
    [string]$VenvPath = '.venv',
    [string]$RunDirectory = 'tmp/dev',
    [string]$DataRoot = 'server',
    [ValidateRange(1, 65535)][int]$ServerPort = 4433,
    [ValidateRange(1, 65535)][int]$WebPort = 5173
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev_common.ps1')
& (Join-Path $PSScriptRoot 'check_env.ps1') -VenvPath $VenvPath -RuntimeOnly
$run = Get-MiniImPath $RunDirectory
[IO.Directory]::CreateDirectory($run) | Out-Null
$statePath = Join-Path $run 'processes.json'
$lock = [IO.File]::Open((Join-Path $run 'processes.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
$records = @()
try {
    if (Test-Path -LiteralPath $statePath) {
        $old = Get-Content -Raw -Encoding UTF8 -LiteralPath $statePath | ConvertFrom-Json
        if ($old.version -ne 1 -or $old.root -ne $MiniImRoot) { throw 'Development process record belongs to another format or workspace' }
        foreach ($record in $old.processes) {
            if ($null -ne (Get-MiniImOwnedProcess $record)) { throw 'Development services are already running; run dev_stop.ps1 first' }
        }
    }
    $python = Join-Path (Get-MiniImPath $VenvPath) 'Scripts/python.exe'
    $node = (Get-Command node -ErrorAction Stop).Source
    $vite = Join-Path $MiniImRoot 'web/node_modules/vite/bin/vite.js'
    Assert-MiniImFile $vite
    $launches = @(
        @{ name='server'; executable=$python; directory=$MiniImRoot; arguments=@('-u', (Join-Path $MiniImRoot 'server/main.py'), '--data-root', (Get-MiniImPath $DataRoot), '--port', "$ServerPort") },
        @{ name='web'; executable=$node; directory=(Join-Path $MiniImRoot 'web'); arguments=@($vite, '--host', '127.0.0.1', '--port', "$WebPort", '--strictPort') }
    )
    foreach ($launch in $launches) {
        $process = Start-Process -FilePath $launch.executable -ArgumentList (ConvertTo-MiniImArguments $launch.arguments) `
            -WorkingDirectory $launch.directory -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $run "$($launch.name).stdout.log") -RedirectStandardError (Join-Path $run "$($launch.name).stderr.log")
        $record = Get-MiniImProcessRecord $process
        $records += $record
        @{ version=1; root=$MiniImRoot; serverPort=$ServerPort; webPort=$WebPort; processes=$records } |
            ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath "$statePath.new"
        Move-Item -LiteralPath "$statePath.new" -Destination $statePath -Force
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        foreach ($record in $records) { if ($null -eq (Get-MiniImOwnedProcess $record)) { throw "Development process exited; inspect logs in $run" } }
        $serverReady = (Get-Content -Raw -LiteralPath (Join-Path $run 'server.stdout.log')) -match "listening at 127\.0\.0\.1:$ServerPort"
        $webReady = $false
        $webListening = (Get-Content -Raw -LiteralPath (Join-Path $run 'web.stdout.log')) -match ":$WebPort/"
        if ($webListening) {
            try { $webReady = (Invoke-WebRequest "http://127.0.0.1:$WebPort" -TimeoutSec 1 -UseBasicParsing).StatusCode -eq 200 } catch { }
        }
        if ($serverReady -and $webReady) { break }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not ($serverReady -and $webReady)) { throw "Development services did not become ready; inspect logs in $run" }
    foreach ($record in $records) { if ($null -eq (Get-MiniImOwnedProcess $record)) { throw 'Development process exited during readiness check' } }
    Write-Host "Ready: quic://127.0.0.1:$ServerPort and http://127.0.0.1:$WebPort; logs: $run"
} catch {
    foreach ($record in $records) { Stop-MiniImProcess $record }
    if ($records.Count -gt 0 -and (Test-Path -LiteralPath $statePath)) { Remove-Item -LiteralPath $statePath -Force }
    throw
} finally { $lock.Dispose() }
