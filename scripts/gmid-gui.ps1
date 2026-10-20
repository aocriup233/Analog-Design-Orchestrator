param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $repoRoot 'src'
& $Python -m analog_agent.gmid_gui
exit $LASTEXITCODE
