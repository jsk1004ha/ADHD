param([switch]$Compact)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
# No downloads, elevation or global execution-policy changes.
$Python = Get-Command py -ErrorAction SilentlyContinue
if ($Python) {
    & py -3 -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'"
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ required' }
    if ($Compact) { & py -3 .\adhd.py upgrade --compact } else { & py -3 .\adhd.py upgrade }
} else {
    & python -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'"
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ required' }
    if ($Compact) { & python .\adhd.py upgrade --compact } else { & python .\adhd.py upgrade }
}
if ($LASTEXITCODE -ne 0) { throw 'Installation failed; inspect the printed error. No provider fallback was attempted.' }
