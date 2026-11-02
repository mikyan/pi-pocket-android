param([Parameter(Mandatory=$true)][string]$Url, [Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Invoke-WebRequest -Uri $Url -OutFile $Destination -TimeoutSec 90 -UseBasicParsing
