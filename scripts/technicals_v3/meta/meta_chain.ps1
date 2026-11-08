# Technicals v3 #2 meta-labelling validation chain (resumable per step: a step whose output exists is skipped; set
# $env:TV3META_FORCE to a list of script names to re-run them). Logs: data/logs/technicals_v3/meta/.
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$py = Join-Path $root ".venv\Scripts\python.exe"
$rd = Join-Path $root "data\reports\technicals_v3\meta"
$log = Join-Path $root "data\logs\technicals_v3\meta\chain.log"
$steps = @(@("build_dataset.py", "..\..\..\scratchpad\tech_v3_meta\ds\ds_h40.parquet"), @("wf_meta.py", "wf_h40.json"),
           @("leakage_meta.py", "leakage.json"), @("econ_meta.py", "econ.json"), @("report_meta.py", "gate.json"))
Set-Location $root
foreach ($s in $steps) {
  $out = Join-Path $rd $s[1]
  if ((Test-Path $out) -and ($env:TV3META_FORCE -notlike "*$($s[0])*")) { Add-Content $log "$(Get-Date -Format s) skip $($s[0])"; continue }
  Add-Content $log "$(Get-Date -Format s) start $($s[0])"
  & $py (Join-Path $root "scripts\technicals_v3\meta\$($s[0])") *>> (Join-Path $root "data\logs\technicals_v3\meta\$($s[0]).out")
  Add-Content $log "$(Get-Date -Format s) end $($s[0]) exit $LASTEXITCODE"
}
