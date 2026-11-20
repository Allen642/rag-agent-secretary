#Requires -Version 5.1
<#
.SYNOPSIS
    Probe the local WeCom bot health endpoint. Intended for Task Scheduler.

.DESCRIPTION
    Requests http://127.0.0.1:8080/healthz. Exits with 0 when the response is
    HTTP 200, otherwise exits with 1 so Task Scheduler can alert or restart.

.PARAMETER Url
    Health endpoint. Default: http://127.0.0.1:8080/healthz

.PARAMETER TimeoutSec
    Request timeout in seconds. Default: 10.
#>

[CmdletBinding()]
param(
    [string]$Url = "http://127.0.0.1:8080/healthz",
    [int]$TimeoutSec = 10
)

try {
    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
}
catch {
    Write-Host "Health check failed: $($_.Exception.Message)"
    exit 1
}

if ($response.StatusCode -ne 200) {
    Write-Host "Health check failed: HTTP $($response.StatusCode)"
    exit 1
}

Write-Host "ok"
exit 0
