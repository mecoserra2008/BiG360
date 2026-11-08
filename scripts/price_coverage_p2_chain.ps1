<#
.SYNOPSIS  Price coverage P2: paced yfinance period=max backfills, one group at a time (single shared Yahoo pace).
  Groups: bench (ETFs + extra indices + futures), jp (TSE), kr (KOSPI/KOSDAQ), hk (HKEX main board).
  Each group is resumable (data\state\price_coverage\p2_yf_<group>.done) and waits while the EU-expansion chain is in
  its Yahoo steps (universe / prices). Re-run to resume.  Log: data\logs\price_coverage\p2_chain.log
  Launch detached:
    Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","<root>\scripts\price_coverage_p2_chain.ps1" -WindowStyle Hidden
#>
param([string[]]$Groups = @("bench", "jp", "kr", "hk"))
$ErrorActionPreference = "Continue"
$Root = Split-Path $PSScriptRoot -Parent
$Py   = Join-Path $Root ".venv\Scripts\python.exe"
$LogD = Join-Path $Root "data\logs\price_coverage"
New-Item -ItemType Directory -Force $LogD | Out-Null
$Log  = Join-Path $LogD "p2_chain.log"
Set-Content (Join-Path $LogD "p2_chain.pid") $PID
function Say($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Add-Content $Log }
$env:PYTHONIOENCODING = "utf-8"
Say "p2 chain start (pid $PID): $($Groups -join ',')"
foreach ($g in $Groups) {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Say "$g : start (attempt $attempt)"
        Push-Location (Join-Path $Root "backend")
        & $Py -m app.ingestion.price_coverage yf --group $g *>> (Join-Path $LogD "p2_yf_$g.log")
        $rc = $LASTEXITCODE
        Pop-Location
        Say "$g : exit=$rc"
        if ($rc -eq 0) { break }
        Start-Sleep (900 * $attempt)
    }
}
Say "p2 chain finished"
