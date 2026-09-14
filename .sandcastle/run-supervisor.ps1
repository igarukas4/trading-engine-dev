[CmdletBinding()]
param(
  [ValidateRange(1, 100)]
  [int]$MaxRetries = 12,
  [ValidateRange(1, 360)]
  [int]$InitialDelayMinutes = 15,
  [ValidateRange(1, 720)]
  [int]$MaxDelayMinutes = 120
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$logDirectory = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

$delayMinutes = $InitialDelayMinutes
for ($attempt = 0; $attempt -le $MaxRetries; $attempt++) {
  $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  $logPath = Join-Path $logDirectory "sandcastle-$timestamp.log"
  Push-Location $repoRoot
  try {
    # npm emits routine notices on stderr; cmd keeps those notices from
    # terminating this Stop-mode PowerShell script before exit capture.
    cmd.exe /d /c "npm run sandcastle 2>&1" | Tee-Object -FilePath $logPath
    $exitCode = $LASTEXITCODE
  } finally {
    Pop-Location
  }

  if ($exitCode -eq 0) {
    Write-Host "Sandcastle stopped normally. See $logPath"
    exit 0
  }

  $rateLimited = Select-String -LiteralPath $logPath -Pattern 'rate[ -]?limit|rate_limit|\b429\b|quota' -CaseSensitive:$false -Quiet
  $retryable = $exitCode -eq 75 -or $rateLimited
  if (-not $retryable -or $attempt -eq $MaxRetries) {
    Write-Error "Sandcastle needs attention (exit $exitCode). See $logPath"
    exit $exitCode
  }

  Write-Warning "Retryable Sandcastle failure. Retrying in $delayMinutes minute(s)."
  Start-Sleep -Seconds ($delayMinutes * 60)
  $delayMinutes = [Math]::Min($delayMinutes * 2, $MaxDelayMinutes)
}
