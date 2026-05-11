param(
    [string]$ExtensionId = "fknjbnaceniahdkhckelhfdkcclojdac",
    [string]$UpdateUrl = "http://127.0.0.1:8765/extension/update.xml"
)

$ErrorActionPreference = "Stop"

$PolicyPath = "HKCU:\Software\Policies\Google\Chrome\ExtensionInstallForcelist"
New-Item -Path $PolicyPath -Force | Out-Null

$Existing = Get-ItemProperty -Path $PolicyPath -ErrorAction SilentlyContinue
$ValueName = $null
if ($Existing) {
    foreach ($Property in $Existing.PSObject.Properties) {
        if ($Property.Name -match "^\d+$" -and [string]$Property.Value -like "$ExtensionId;*") {
            $ValueName = $Property.Name
            break
        }
    }
}
if (-not $ValueName) {
    $Used = @()
    if ($Existing) {
        $Used = $Existing.PSObject.Properties |
            Where-Object { $_.Name -match "^\d+$" } |
            ForEach-Object { [int]$_.Name }
    }
    $Next = 1
    while ($Used -contains $Next) {
        $Next += 1
    }
    $ValueName = [string]$Next
}

New-ItemProperty -Path $PolicyPath -Name $ValueName -PropertyType String -Value "$ExtensionId;$UpdateUrl" -Force | Out-Null

Write-Host "Chrome ExtensionInstallForcelist policy set:"
Write-Host "  $ValueName = $ExtensionId;$UpdateUrl"
Write-Host "Open chrome://policy and reload policies, or restart Chrome, if Chrome does not pick it up immediately."
