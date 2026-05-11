param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

$TaskNames = @(
    "Activity Journal Daily",
    "Activity Journal Weekly",
    "Activity Journal Watcher",
    "Activity Journal ChatGPT Receiver",
    "Activity Journal Codex Review",
    "Activity Journal Codex Bridge"
)
$StartupFolder = [Environment]::GetFolderPath("Startup")
$StartupLaunchers = @(
    (Join-Path $StartupFolder "Activity Journal Watcher.vbs"),
    (Join-Path $StartupFolder "Activity Journal ChatGPT Receiver.vbs"),
    (Join-Path $StartupFolder "Activity Journal Tray.vbs")
)
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Activity Journal"
$ShortcutPaths = @(
    (Join-Path $StartMenu "Activity Journal Settings.lnk"),
    (Join-Path $StartMenu "Activity Journal Dashboard.lnk"),
    (Join-Path $StartMenu "Activity Journal Tray.lnk")
)

foreach ($TaskName in $TaskNames) {
    if ($WhatIf) {
        Write-Host "Would unregister task: $TaskName"
    } else {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    }
}

foreach ($Path in $StartupLaunchers) {
    if ($WhatIf) {
        Write-Host "Would remove startup launcher: $Path"
    } elseif (Test-Path $Path) {
        Remove-Item -LiteralPath $Path -Force
    }
}

foreach ($Path in $ShortcutPaths) {
    if ($WhatIf) {
        Write-Host "Would remove Start Menu shortcut: $Path"
    } elseif (Test-Path $Path) {
        Remove-Item -LiteralPath $Path -Force
    }
}

if (-not $WhatIf -and (Test-Path $StartMenu) -and -not (Get-ChildItem $StartMenu -Force -ErrorAction SilentlyContinue)) {
    Remove-Item -LiteralPath $StartMenu -Force
}

Write-Host "Activity Journal local MVP uninstall complete. journal\ and config\ data were preserved."
