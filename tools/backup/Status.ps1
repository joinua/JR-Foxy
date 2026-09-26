$ErrorActionPreference = 'Stop'
$Config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
& $Config.python (Join-Path $PSScriptRoot 'client.py') status
