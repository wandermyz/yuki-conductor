# Cleanly restart the yuki-conductor Windows daemon.
#
# Must be launched DETACHED from the caller's process tree (see SKILL.md) — it
# kills every yuki-conductor process, which would otherwise include the caller.
#
# Usage:
#   pwsh -NoProfile -File restart-daemon.ps1 [-Port 2333] [-DelaySeconds 15]

param(
    [int]$Port = 2333,
    [int]$DelaySeconds = 15
)

$ErrorActionPreference = 'Continue'

$base = if ($env:YUKI_CONDUCTOR_DATA_DIR) { $env:YUKI_CONDUCTOR_DATA_DIR } else { Join-Path $HOME '.yuki-conductor' }
$log = Join-Path $base 'restart-daemon.log'
$stopFile = Join-Path $base '.stop'
$taskName = 'YukiConductor'

# Give the caller time to finish and exit before we kill its process tree.
Start-Sleep -Seconds $DelaySeconds

Start-Transcript -Path $log -Force | Out-Null

function Get-DaemonProcesses {
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -like '*yuki-conductor*' -and $_.CommandLine -notlike '*restart-daemon*' }
}

Write-Output "=== Before ==="
Get-DaemonProcesses | Select-Object ProcessId, ParentProcessId, Name, CreationDate | Format-Table -AutoSize | Out-String | Write-Output
Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalPort, OwningProcess | Format-Table -AutoSize | Out-String | Write-Output

# Tell the supervisor script this stop is intentional so it doesn't relaunch.
New-Item -ItemType File -Path $stopFile -Force | Out-Null

Write-Output "=== Stopping scheduled task ==="
schtasks /End /TN $taskName 2>&1 | Write-Output

Write-Output "=== Killing daemon processes ==="
foreach ($p in Get-DaemonProcesses) {
    Write-Output "Killing PID $($p.ProcessId) ($($p.Name))"
    taskkill /PID $p.ProcessId /T /F 2>&1 | Write-Output
}

Remove-Item -Path $stopFile -Force -ErrorAction SilentlyContinue

Write-Output "=== Freeing port $Port ==="
foreach ($c in (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
    Write-Output "Killing port holder PID $($c.OwningProcess)"
    taskkill /PID $c.OwningProcess /T /F 2>&1 | Write-Output
}

Start-Sleep -Seconds 2

Write-Output "=== Starting clean daemon via scheduled task ==="
schtasks /Run /TN $taskName 2>&1 | Write-Output

Start-Sleep -Seconds 10

Write-Output "=== After ==="
Get-DaemonProcesses | Select-Object ProcessId, ParentProcessId, Name, CreationDate | Format-Table -AutoSize | Out-String | Write-Output
Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalPort, OwningProcess | Format-Table -AutoSize | Out-String | Write-Output

Stop-Transcript | Out-Null
