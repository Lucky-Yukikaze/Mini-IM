[CmdletBinding()]
param([string]$RunDirectory = 'tmp/dev')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev_common.ps1')
$run = Get-MiniImPath $RunDirectory
$statePath = Join-Path $run 'processes.json'
if (-not (Test-Path -LiteralPath $statePath)) { Write-Host 'No development process record.'; return }
$lock = [IO.File]::Open((Join-Path $run 'processes.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
try {
    $state = Get-Content -Raw -Encoding UTF8 -LiteralPath $statePath | ConvertFrom-Json
    if ($state.version -ne 1 -or $state.root -ne $MiniImRoot) { throw 'Development process record belongs to another format or workspace' }
    foreach ($record in $state.processes) { Get-MiniImOwnedProcess $record | Out-Null }
    foreach ($record in $state.processes) { Stop-MiniImProcess $record }
    Remove-Item -LiteralPath $statePath -Force
    Write-Host 'Development services stopped.'
} finally { $lock.Dispose() }
