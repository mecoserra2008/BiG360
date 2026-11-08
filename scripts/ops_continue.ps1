# O0 continuation (docs/OPERATIONS_ROADMAP.md): nightly --resume (skipped analytics + serving on repaired prices) -> weekly (labels -> xgb -> ensemble).
$ErrorActionPreference = "Continue"
$R = Split-Path $PSScriptRoot -Parent
$py = "$R\.venv\Scripts\python.exe"
$lg = "$R\data\logs\manual"
New-Item -ItemType Directory -Force $lg | Out-Null
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Out-File -Append -Encoding utf8 "$lg\continue.log" }
function RunDag($name, $dagArgs) {
    Log "$name start"
    $p = Start-Process $py -ArgumentList $dagArgs -WorkingDirectory "$R\backend" -WindowStyle Hidden -Wait -PassThru `
        -RedirectStandardOutput "$lg\continue_$name.out.log" -RedirectStandardError "$lg\continue_$name.err.log"
    Log "$name exit $($p.ExitCode)"
}
RunDag "nightly" @('-m','app.cli','jobs','run-dag','nightly','--resume','--max-workers','4','--trigger','manual')
RunDag "weekly"  @('-m','app.cli','jobs','run-dag','weekly','--max-workers','3','--trigger','manual')
Log "continue done"
