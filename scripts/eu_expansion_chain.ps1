<#
.SYNOPSIS  Runs the remaining EU-expansion steps (identity -> universe -> prices -> fundamentals -> analytics) unattended.
.DESCRIPTION
  Safe to re-run: every step resumes from the cursors in data\state\eu_expansion.json and steps marked "done" are skipped.
  Launch detached so it survives the terminal / Claude session:
    Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","<root>\scripts\eu_expansion_chain.ps1" -WindowStyle Hidden
  Progress: data\logs\eu_expansion\chain.log
#>
$ErrorActionPreference = "Continue"
$Root  = Split-Path $PSScriptRoot -Parent
$Py    = Join-Path $Root ".venv\Scripts\python.exe"
$State = Join-Path $Root "data\state\eu_expansion.json"
$LogD  = Join-Path $Root "data\logs\eu_expansion"
$Log   = Join-Path $LogD "chain.log"
New-Item -ItemType Directory -Force $LogD | Out-Null
Set-Content (Join-Path $LogD "chain.pid") $PID
function Say($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Add-Content $Log }
function StepStatus($key) {
    try { ((Get-Content $State -Raw | ConvertFrom-Json).steps.$key.status) } catch { "unknown" }
}

Say "chain start (pid $PID)"
# 1) wait for a detached identity run (step 2) if one is alive
$idPidFile = Join-Path $LogD "identity.pid"
if (Test-Path $idPidFile) {
    $idPid = [int](Get-Content $idPidFile)
    while (Get-Process -Id $idPid -ErrorAction SilentlyContinue) { Start-Sleep 60 }
    Say "identity process $idPid exited; status=$(StepStatus '2_identity')"
}

$steps = @(@{k="2_identity"; s="identity"}, @{k="3_universe"; s="universe"}, @{k="4_prices"; s="prices"},
           @{k="5_fundamentals"; s="fundamentals"}, @{k="6_analytics"; s="analytics"})
foreach ($st in $steps) {
    if ((StepStatus $st.k) -eq "done") { Say "$($st.s): already done, skip"; continue }
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Say "$($st.s): start (attempt $attempt)"
        Push-Location (Join-Path $Root "backend")
        & $Py -m app.ingestion.eu_expansion $st.s *>> (Join-Path $LogD "chain_$($st.s).log")
        $rc = $LASTEXITCODE
        Pop-Location
        $status = StepStatus $st.k
        Say "$($st.s): exit=$rc status=$status"
        if ($status -eq "done") { break }
        Start-Sleep (900 * $attempt)   # likely Yahoo/source throttling: back off 15/30/45 min, then resume
    }
    if ((StepStatus $st.k) -ne "done") { Say "$($st.s): NOT done after 3 attempts -> stopping chain (re-run this script to resume)"; exit 1 }
}
Say "chain finished: steps 2-6 done; step 7 (report) is manual"
