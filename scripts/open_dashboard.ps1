param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8776,
    [string]$Date,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DashboardScript = Join-Path $Repo "scripts\dashboard_app.py"
$ArgsList = @($DashboardScript, "--host", $HostName, "--port", $Port)

if (-not [string]::IsNullOrWhiteSpace($Date)) {
    $ArgsList += @("--date", $Date)
}
if ($NoBrowser) {
    $ArgsList += "--no-browser"
}

python @ArgsList
