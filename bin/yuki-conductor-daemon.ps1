# yuki-conductor Windows daemon launcher.
# Invoked by the Task Scheduler "YukiConductor" task at logon. Redirects the
# supervised process's stdout/stderr to the data-dir log files (Task Scheduler
# does not capture stdio itself).
$ErrorActionPreference = "Continue"
$DataDir = if ($env:YUKI_CONDUCTOR_DATA_DIR) { $env:YUKI_CONDUCTOR_DATA_DIR } else { Join-Path $env:USERPROFILE ".yuki-conductor" }
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$LogOut = Join-Path $DataDir "daemon.log"
$LogErr = Join-Path $DataDir "daemon.err.log"
$Repo   = Split-Path -Parent $PSScriptRoot
& uv run --project $Repo yuki-conductor run *> $LogOut 2> $LogErr
