# Shared paths and checked native commands for the Windows development scripts.
$MiniImRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

function Get-MiniImPath([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    return [IO.Path]::GetFullPath((Join-Path $MiniImRoot $Path))
}

function Assert-MiniImFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Required file missing: $Path" }
}

function Invoke-MiniImCommand([string]$Program, [string[]]$Arguments) {
    Get-Command $Program -ErrorAction Stop | Out-Null
    # Windows PowerShell treats redirected stderr as errors even when the program succeeds.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        if ($PSVersionTable.PSVersion.Major -lt 7) {
            $Arguments = @($Arguments | ForEach-Object { $_ -replace '(\\*)"', '$1$1\"' })
        }
        & $Program @Arguments
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($exitCode -ne 0) { throw "$Program exited with code $exitCode" }
}

function Get-MiniImProcessRecord($Process) {
    $Process.Refresh()
    return @{ id = $Process.Id; started = $Process.StartTime.ToUniversalTime().Ticks.ToString(); path = $Process.Path }
}

function Get-MiniImOwnedProcess($Record) {
    if (-not $Record.id -or -not $Record.started -or -not $Record.path) { throw 'Invalid development process record' }
    $process = Get-Process -Id $Record.id -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $started = $process.StartTime
    $executable = $process.Path
    if ($process.HasExited) { return $null }
    if ($null -eq $started -or $started.ToUniversalTime().Ticks.ToString() -ne $Record.started -or $executable -ne $Record.path) {
        throw "Process identity changed; refusing to stop process $($Record.id)"
    }
    return $process
}

function Stop-MiniImProcess($Record) {
    $process = Get-MiniImOwnedProcess $Record
    if ($null -eq $process) { return }
    # Python virtual-environment launchers and Vite can own child processes.
    $tree = [Collections.Generic.List[object]]::new()
    $tree.Add($Record)
    $snapshot = @(Get-CimInstance Win32_Process)
    for ($index = 0; $index -lt $tree.Count; $index++) {
        $parent = $tree[$index]
        foreach ($child in $snapshot) {
            if ($child.ParentProcessId -eq $parent.id) {
                $candidate = Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
                if ($null -ne $candidate -and $candidate.StartTime.ToUniversalTime().Ticks -ge [long]$parent.started) {
                    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($child.ProcessId)"
                    if ($null -ne $current -and $current.CreationDate -eq $child.CreationDate -and
                        $current.ParentProcessId -eq $parent.id) {
                        $tree.Add((Get-MiniImProcessRecord $candidate))
                    }
                }
            }
        }
    }
    for ($index = $tree.Count - 1; $index -ge 0; $index--) {
        $owned = Get-MiniImOwnedProcess $tree[$index]
        if ($null -ne $owned) { $owned | Stop-Process -Force; $owned.WaitForExit(5000) | Out-Null }
    }
}

function ConvertTo-MiniImArguments([string[]]$Values) {
    # Windows command-line quoting; paths and arguments never become shell code.
    return (($Values | ForEach-Object { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }) -join ' ')
}
