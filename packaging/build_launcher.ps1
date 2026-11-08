<#
.SYNOPSIS  Build BiG360.exe (launcher stub) with the .NET Framework C# compiler that ships with Windows, and copy
           BiG360.vbs / BiG360.cmd next to it.
.EXAMPLE   .\packaging\build_launcher.ps1                      # -> <project root>\BiG360.exe (development tree)
           .\packaging\build_launcher.ps1 -OutDir packaging\out\BiG360
#>
param(
    [string]$OutDir = (Split-Path $PSScriptRoot -Parent)
)
$ErrorActionPreference = "Stop"
$R = Split-Path $PSScriptRoot -Parent
$csc = @("$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe", "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $csc) { throw "csc.exe (.NET Framework 4.x) not found; use BiG360.vbs / BiG360.cmd instead" }
$ico = Join-Path $PSScriptRoot "assets\big360.ico"
if (-not (Test-Path $ico)) { & (Join-Path $R ".venv\Scripts\python.exe") (Join-Path $PSScriptRoot "make_icon.py") }
$ver = (Get-Content (Join-Path $R "VERSION") -Raw).Trim()
$v4 = (($ver -split "[^0-9]+" | Where-Object { $_ -ne "" }) + @("0", "0", "0", "0"))[0..3] -join "."
New-Item -ItemType Directory -Force $OutDir | Out-Null
$tmp = Join-Path $R "scratchpad\o3\launcher"
New-Item -ItemType Directory -Force $tmp | Out-Null
$ai = Join-Path $tmp "AssemblyVersion.cs"
Set-Content -Path $ai -Encoding UTF8 -Value @"
[assembly: System.Reflection.AssemblyVersion("$v4")]
[assembly: System.Reflection.AssemblyFileVersion("$v4")]
[assembly: System.Reflection.AssemblyInformationalVersion("$ver")]
"@
$exe = Join-Path $OutDir "BiG360.exe"
& $csc /nologo /target:winexe /optimize+ /platform:anycpu "/win32icon:$ico" "/out:$exe" `
    (Join-Path $PSScriptRoot "launcher\BiG360Launcher.cs") $ai
if ($LASTEXITCODE -ne 0) { throw "csc failed ($LASTEXITCODE)" }
Copy-Item (Join-Path $PSScriptRoot "launcher\BiG360.vbs"), (Join-Path $PSScriptRoot "launcher\BiG360.cmd") $OutDir -Force
Write-Host "launcher: $exe ($ver) + BiG360.vbs + BiG360.cmd"
