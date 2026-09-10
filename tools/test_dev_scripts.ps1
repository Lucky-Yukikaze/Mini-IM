[CmdletBinding()]
param([string]$VenvPath = '.venv')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev_common.ps1')
$startScript = Join-Path $PSScriptRoot 'dev_start.ps1'
$stopScript = Join-Path $PSScriptRoot 'dev_stop.ps1'
$output = Join-Path $MiniImRoot ('tmp/dev-script-tests/' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$run = Join-Path $output "space ' & path"
$statePath = Join-Path $run 'processes.json'
[IO.Directory]::CreateDirectory($output) | Out-Null
$checks = [Collections.Generic.List[string]]::new()
function Require([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Free-Port([bool]$Udp) {
    if ($Udp) {
        $socket = [Net.Sockets.UdpClient]::new(0)
        try { return $socket.Client.LocalEndPoint.Port } finally { $socket.Dispose() }
    }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return $listener.LocalEndpoint.Port } finally { $listener.Stop() }
}
$serverPort = Free-Port $true
$webPort = Free-Port $false
$originalState = $null
try {
    $hostProgram = (Get-Process -Id $PID).Path
    $failed = $false
    try {
        Invoke-MiniImCommand $hostProgram @('-NoProfile', '-File', (Join-Path $PSScriptRoot 'check_env.ps1'),
            '-RuntimeOnly', '-VenvPath', (Join-Path $output 'missing')) *> (Join-Path $output 'missing-environment.log')
    } catch { $failed = $true }
    Require $failed 'Missing environment was accepted'
    $python = Join-Path (Get-MiniImPath $VenvPath) 'Scripts/python.exe'
    Invoke-MiniImCommand $python @('-c', 'import sys; print("diagnostic", file=sys.stderr)') *> (Join-Path $output 'successful-stderr.log')
    $failed = $false
    try { Invoke-MiniImCommand $hostProgram @('-NoProfile', '-Command', 'exit 7') } catch { $failed = $true }
    Require $failed 'Native failure did not propagate'
    $checks.Add('missing environment and command failures')
    Push-Location ([IO.Path]::GetTempPath())
    try { & $startScript -VenvPath $VenvPath -RunDirectory $run -DataRoot (Join-Path $run 'server data') -ServerPort $serverPort -WebPort $webPort }
    finally { Pop-Location }
    $originalState = Get-Content -Raw -Encoding UTF8 -LiteralPath $statePath
    $state = $originalState | ConvertFrom-Json
    Require ($state.processes.Count -eq 2) 'Both services were not recorded'
    Require (Test-Path -LiteralPath (Join-Path $run 'server data/storage/sqlite/miniim.db')) 'Database path changed'
    $checks.Add('start from another directory with spaces and punctuation')
    $failed = $false
    try { & $startScript -VenvPath $VenvPath -RunDirectory $run -ServerPort $serverPort -WebPort $webPort } catch { $failed = $true }
    Require $failed 'Duplicate start was accepted'
    foreach ($record in $state.processes) { Require ($null -ne (Get-MiniImOwnedProcess $record)) 'Duplicate start stopped a service' }
    $checks.Add('duplicate start preserves existing services')
    $state.processes[-1].started = '0'
    $state | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath $statePath
    $failed = $false
    try { & $stopScript -RunDirectory $run } catch { $failed = $true }
    Require $failed 'Changed identity was accepted'
    $originalState | Set-Content -Encoding UTF8 -LiteralPath $statePath
    $state = $originalState | ConvertFrom-Json
    foreach ($record in $state.processes) { Require ($null -ne (Get-MiniImOwnedProcess $record)) 'Stale record stopped a service' }
    $checks.Add('stale identity refuses all stops')
    & $stopScript -RunDirectory $run
    foreach ($record in $state.processes) { Require ($null -eq (Get-MiniImOwnedProcess $record)) 'Process remained after stop' }
    Require (-not (Test-Path -LiteralPath $statePath)) 'Process record remained after stop'
    $probe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $webPort)
    $probe.Start(); $probe.Stop()
    $udpProbe = [Net.Sockets.UdpClient]::new($serverPort); $udpProbe.Dispose()
    & $stopScript -RunDirectory $run
    $checks.Add('stop releases both ports and is repeatable')
    $occupied = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $webPort)
    $occupied.Start()
    try {
        $failed = $false
        try { & $startScript -VenvPath $VenvPath -RunDirectory $run -DataRoot (Join-Path $run 'server data') -ServerPort $serverPort -WebPort $webPort } catch { $failed = $true }
        Require $failed 'Occupied port was accepted'
        Require (-not (Test-Path -LiteralPath $statePath)) 'Failed startup retained state'
        $udpProbe = [Net.Sockets.UdpClient]::new($serverPort); $udpProbe.Dispose()
        Require $occupied.Server.IsBound 'Unrelated listener was stopped'
    } finally { $occupied.Stop() }
    $checks.Add('port conflict cleans up only new services')
    @{ successful=$true; checks=@($checks); output=$output } | ConvertTo-Json -Depth 4 |
        Set-Content -Encoding UTF8 (Join-Path $output 'results.json')
    Write-Host "Passed $($checks.Count) checks; evidence: $output"
} finally {
    if (Test-Path -LiteralPath $statePath) {
        if ($null -ne $originalState) { $originalState | Set-Content -Encoding UTF8 -LiteralPath $statePath }
        & $stopScript -RunDirectory $run
    }
}
