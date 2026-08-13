# yuki-conductor Windows daemon launcher / supervisor.
#
# Invoked by the Task Scheduler "YukiConductor" task at logon. The task runs
# with a visible console, so this window doubles as the daemon's live log view:
# output streams to the console and is teed to daemon.err.log for later reading.
#
# daemon.log itself is written by the Python process (runtime.py attaches a
# FileHandler). Redirecting stdout here as well would put two writers on one
# file, which on Windows means an exclusive-handle fight on every restart.
#
# This script supervises: if the daemon exits for any reason it is relaunched
# after a backoff. Task Scheduler's own <RestartOnFailure> does NOT cover this —
# it only retries when the Scheduler cannot *launch* the action, so a daemon
# that starts fine and later dies (crash, Ctrl-C, closed console) would
# otherwise stay dead until the next logon.
$ErrorActionPreference = "Continue"
$DataDir = if ($env:YUKI_CONDUCTOR_DATA_DIR) { $env:YUKI_CONDUCTOR_DATA_DIR } else { Join-Path $env:USERPROFILE ".yuki-conductor" }
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$LogErr  = Join-Path $DataDir "daemon.err.log"
$StopFile = Join-Path $DataDir "daemon.stop"
$Repo    = Split-Path -Parent $PSScriptRoot

# Output goes through a pipe (Tee-Object), so Python would block-buffer stdout
# and the window would sit blank for minutes. Force line-by-line flushing.
$env:PYTHONUNBUFFERED = "1"

# Restart backoff: fast for one-off crashes, capped so a persistently broken
# daemon (bad config, port taken) doesn't spin hot.
$MinBackoff = 2
$MaxBackoff = 60
# A run lasting at least this long counts as "was healthy", so its exit is an
# isolated failure rather than part of a crash loop; backoff resets.
$HealthySeconds = 60

$Host.UI.RawUI.WindowTitle = "yuki-conductor daemon - $Repo"
Write-Host "yuki-conductor daemon" -ForegroundColor Cyan
Write-Host "repo:   $Repo"
Write-Host "logs:   $DataDir"
Write-Host "Auto-restarts on exit. Close this window or run 'daemon restart' to stop." -ForegroundColor Yellow
Write-Host ("-" * 60)

# A stale stop sentinel would silently prevent the daemon from ever starting.
Remove-Item $StopFile -Force -ErrorAction SilentlyContinue

$backoff = $MinBackoff
while ($true) {
    $started = Get-Date
    & uv run --project $Repo yuki-conductor run 2>&1 | Tee-Object -FilePath $LogErr -Append
    $code = $LASTEXITCODE
    $ranFor = ((Get-Date) - $started).TotalSeconds

    # `daemon restart`/`uninstall` drops this sentinel before killing us, so an
    # intentional stop is not fought by the supervisor relaunching underneath it.
    if (Test-Path $StopFile) {
        Remove-Item $StopFile -Force -ErrorAction SilentlyContinue
        Write-Host "Stop requested - supervisor exiting." -ForegroundColor Yellow
        break
    }

    if ($ranFor -ge $HealthySeconds) { $backoff = $MinBackoff }

    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $msg = "$stamp daemon exited (code=$code, uptime=$([int]$ranFor)s) - restarting in ${backoff}s"
    Write-Host $msg -ForegroundColor Red
    Add-Content -Path $LogErr -Value $msg

    Start-Sleep -Seconds $backoff
    $backoff = [Math]::Min($backoff * 2, $MaxBackoff)
}
