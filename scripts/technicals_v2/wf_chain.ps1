# Technicals v2 T6 chain (resumable per step: a step whose output exists is skipped). Logs: data/logs/technicals_v2/.
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$py = Join-Path $root ".venv\Scripts\python.exe"
$rd = Join-Path $root "data\reports\technicals_v2"
$log = Join-Path $root "data\logs\technicals_v2\wf_chain.log"
$steps = @(@("wf_t2.py", "t2_summary.json"), @("wf_t4.py", "t4_summary.json"), @("wf_t3.py", "t3_summary.json"),
           @("wf_leakage.py", "leakage.json"), @("econ.py", "econ.json"), @("report.py", "gate.json"))
Set-Location $root
foreach ($s in $steps) {
  $out = Join-Path $rd $s[1]
  if ((Test-Path $out) -and ($env:TV2_FORCE -notlike "*$($s[0])*")) { Add-Content $log "$(Get-Date -Format s) skip $($s[0])"; continue }
  Add-Content $log "$(Get-Date -Format s) start $($s[0])"
  & $py (Join-Path $root "scripts\technicals_v2\$($s[0])") *>> (Join-Path $root "data\logs\technicals_v2\$($s[0]).out")
  Add-Content $log "$(Get-Date -Format s) end $($s[0]) exit $LASTEXITCODE"
}
