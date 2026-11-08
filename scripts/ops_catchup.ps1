# O0 catch-up (docs/OPERATIONS_ROADMAP.md): GDELT backfill stopped -> nightly (since 10-12) -> weekly -> backfill restarted.
$ErrorActionPreference = "Continue"
$R = Split-Path $PSScriptRoot -Parent
$py = "$R\.venv\Scripts\python.exe"
$lg = "$R\data\logs\manual"
New-Item -ItemType Directory -Force $lg | Out-Null
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Add-Content "$lg\catchup.log" }
Set-Location "$R\backend"

Log "nightly start"
& $py -m app.cli jobs run-dag nightly --since 2026-10-12 --max-workers 4 --trigger manual *>> "$lg\catchup_nightly.log"
Log "nightly exit $LASTEXITCODE"

Log "weekly start"
& $py -m app.cli jobs run-dag weekly --max-workers 3 --trigger manual *>> "$lg\catchup_weekly.log"
Log "weekly exit $LASTEXITCODE"

Remove-Item "$R\data\state\events_v2\STOP" -ErrorAction SilentlyContinue
$ev = "$R\data\logs\events_v2"
Get-ChildItem "$ev\e6.stderr.log","$ev\e6.stdout.log" -ErrorAction SilentlyContinue | ForEach-Object { Rename-Item $_.FullName ($_.BaseName + ".$(Get-Date -Format yyyyMMdd_HHmm)" + $_.Extension) -ErrorAction SilentlyContinue }
$b = Start-Process $py -ArgumentList '-m','app.events_v2.backfill','--label','e6','--start','2020-01-01','--end','2026-07-13','--batch-days','7','--workers','3','--dl-threads','4','--retention' -WorkingDirectory "$R\backend" -WindowStyle Hidden -RedirectStandardOutput "$ev\e6.stdout.log" -RedirectStandardError "$ev\e6.stderr.log" -PassThru
$b.Id | Set-Content "$ev\e6.pid"
Start-Sleep 5
try { (Get-Process -Id $b.Id).PriorityClass = 'BelowNormal' } catch {}
Log "backfill restarted pid $($b.Id)"
Log "catchup done"
