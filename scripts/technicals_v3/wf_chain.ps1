# Technicals v3 research chain (docs/TECHNICALS_V3_VALIDATION.md). One heavy process at a time; every step is resumable.
#   powershell -File scripts/technicals_v3/wf_chain.ps1 [-Steps build,eval,bo,econ,report] [-Regions UK,EU,JP,US]
param(
  [string[]]$Steps = @("build", "eval", "bo", "econ", "report"),
  [string[]]$Regions = @("UK", "EU", "JP", "US")
)
$ErrorActionPreference = "Continue"
$Steps = @($Steps | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
$Regions = @($Regions | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$py = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "data\logs\technicals_v3\wf_chain.log"
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Out-File -FilePath $log -Append -Encoding utf8 }
Set-Location $root
Log "chain start: steps $($Steps -join ',') regions $($Regions -join ',')"
foreach ($s in $Steps) {
  if ($s -eq "build") {
    foreach ($r in $Regions) {
      Log "build $r"
      & $py scripts/technicals_v3/wf_build.py --region $r 2>&1 | Out-File -FilePath (Join-Path $root "data\logs\technicals_v3\wf_build_$r.out") -Append -Encoding utf8
      Log "build $r exit $LASTEXITCODE"
    }
  } elseif ($s -eq "eval") {
    Log "eval"; & $py scripts/technicals_v3/wf_eval.py 2>&1 | Out-File -FilePath (Join-Path $root "data\logs\technicals_v3\wf_eval.out") -Append -Encoding utf8; Log "eval exit $LASTEXITCODE"
  } elseif ($s -eq "bo") {
    Log "bo"; & $py scripts/technicals_v3/wf_breakouts.py 2>&1 | Out-File -FilePath (Join-Path $root "data\logs\technicals_v3\wf_breakouts.out") -Append -Encoding utf8; Log "bo exit $LASTEXITCODE"
  } elseif ($s -eq "econ") {
    Log "econ"; & $py scripts/technicals_v3/econ.py 2>&1 | Out-File -FilePath (Join-Path $root "data\logs\technicals_v3\econ.out") -Append -Encoding utf8; Log "econ exit $LASTEXITCODE"
  } elseif ($s -eq "report") {
    Log "report"; & $py scripts/technicals_v3/report.py 2>&1 | Out-File -FilePath (Join-Path $root "data\logs\technicals_v3\report.out") -Append -Encoding utf8; Log "report exit $LASTEXITCODE"
  }
}
Log "chain done"
