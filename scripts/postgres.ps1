# Local Postgres 16 from the portable Windows binaries (no admin, no service). Run from the repo root:
#   powershell -File scripts\postgres.ps1 start | stop | status
# PGHOME (binaries) defaults to %USERPROFILE%\pgsql, PGDATA (data) to %USERPROFILE%\pgsql-data.
# One-time setup and the DSN for .env: README, "Postgres (long-term memory, frozen playbook)".
param([ValidateSet("start", "stop", "status")][string]$Action = "status")

$PgHome = if ($env:PGHOME) { $env:PGHOME } else { Join-Path $env:USERPROFILE "pgsql" }
$PgData = if ($env:PGDATA) { $env:PGDATA } else { Join-Path $env:USERPROFILE "pgsql-data" }
$PgCtl = Join-Path $PgHome "bin\pg_ctl.exe"
if (-not (Test-Path $PgCtl)) { Write-Error "pg_ctl not found at $PgCtl (set PGHOME)"; exit 1 }

switch ($Action) {
    "start" {
        New-Item -ItemType Directory -Force (Join-Path $PgData "log") | Out-Null
        & $PgCtl -D $PgData -l (Join-Path $PgData "log\server.log") -w start
    }
    "stop" { & $PgCtl -D $PgData -m fast -w stop }
    "status" { & $PgCtl -D $PgData status }
}
