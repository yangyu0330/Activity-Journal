param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ConfigDir = Join-Path $Repo "config"
$ConfigPath = Join-Path $ConfigDir "activity-journal.json"
$ExampleConfigPath = Join-Path $ConfigDir "activity-journal.example.json"
$RequirementsPath = Join-Path $Repo "requirements.txt"
$SetupTaskScript = Join-Path $Repo "scripts\setup_task.ps1"
$SettingsAppScript = Join-Path $Repo "scripts\settings_app.py"
$TrayAppScript = Join-Path $Repo "scripts\tray_app.py"
$Python = "python"
$PythonwCommand = Get-Command "pythonw.exe" -ErrorAction SilentlyContinue
$Pythonw = if ($PythonwCommand) { $PythonwCommand.Source } else { "python" }
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Activity Journal"
$ShortcutPath = Join-Path $StartMenu "Activity Journal Settings.lnk"
$TrayShortcutPath = Join-Path $StartMenu "Activity Journal Tray.lnk"
$StartupFolder = [Environment]::GetFolderPath("Startup")
$TrayLauncherPath = Join-Path $StartupFolder "Activity Journal Tray.vbs"
$LocalAppData = if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    [Environment]::GetFolderPath("LocalApplicationData")
} else {
    $env:LOCALAPPDATA
}
$ExtensionArtifactDir = Join-Path $LocalAppData "ActivityJournal\browser_extension"
$ExtensionArtifactNames = @(
    "chatgpt-live-capture.pem",
    "chatgpt-live-capture.crx",
    "chatgpt-live-capture-update.xml"
)
$LocalDirectories = @(
    "journal",
    "journal\raw",
    "journal\daily",
    "journal\weekly",
    "journal\questions",
    "journal\projects",
    "imports",
    "imports\chatgpt",
    "inbox",
    "manual_notes"
)

function Assert-PythonReady {
    & $Python -c "import sys, tkinter, json; print(sys.version.split()[0])" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Python with tkinter is required. Install Python for Windows and make sure python.exe is on PATH."
    }
}

function Test-PythonModule {
    param([string]$ModuleName)
    & $Python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$ModuleName') else 1)" | Out-Null
    return $LASTEXITCODE -eq 0
}

function Ensure-TrayDependencies {
    $Missing = @()
    if (-not (Test-PythonModule "pystray")) {
        $Missing += "pystray"
    }
    if (-not (Test-PythonModule "PIL")) {
        $Missing += "pillow"
    }
    if ($Missing.Count -eq 0) {
        return
    }
    Write-Host "Installing tray dependencies: $($Missing -join ', ')"
    & $Python -m pip install --user pystray pillow
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install tray dependencies. Run: python -m pip install --user pystray pillow"
    }
}

function Initialize-LocalConfig {
    if (Test-Path $ConfigPath) {
        if ($WhatIf) {
            Write-Host "Would keep existing local config: $ConfigPath"
        }
        return
    }
    if (-not (Test-Path $ExampleConfigPath)) {
        throw "Missing example config: $ExampleConfigPath"
    }
    if ($WhatIf) {
        Write-Host "Would create local config from example: $ConfigPath"
        return
    }
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    Copy-Item -LiteralPath $ExampleConfigPath -Destination $ConfigPath
}

function Ensure-LocalDirectories {
    foreach ($RelativePath in $LocalDirectories) {
        $Path = Join-Path $Repo $RelativePath
        if ($WhatIf) {
            Write-Host "Would ensure local directory: $RelativePath"
            continue
        }
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
    }
}

function Ensure-PythonRequirements {
    if (-not (Test-Path $RequirementsPath)) {
        Ensure-TrayDependencies
        return
    }
    if ($WhatIf) {
        Write-Host "Would install Python requirements: $RequirementsPath"
        return
    }
    & $Python -m pip install --user -r $RequirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install Python requirements. Run: python -m pip install --user -r requirements.txt"
    }
}

function Move-ExtensionArtifacts {
    foreach ($Name in $ExtensionArtifactNames) {
        $Source = Join-Path (Join-Path $Repo "browser_extension") $Name
        if (-not (Test-Path -LiteralPath $Source)) {
            continue
        }
        $Destination = Join-Path $ExtensionArtifactDir $Name
        if ($WhatIf) {
            if (Test-Path -LiteralPath $Destination) {
                Write-Host "Would move extension artifact to timestamped backup because destination exists: $Source -> $ExtensionArtifactDir"
            } else {
                Write-Host "Would move extension artifact: $Source -> $Destination"
            }
            continue
        }
        New-Item -ItemType Directory -Force -Path $ExtensionArtifactDir | Out-Null
        if (Test-Path -LiteralPath $Destination) {
            $Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $Backup = Join-Path $ExtensionArtifactDir "$Name.$Timestamp.bak"
            Move-Item -LiteralPath $Source -Destination $Backup
            Write-Host "Moved extension artifact to backup because destination exists: $Backup"
        } else {
            Move-Item -LiteralPath $Source -Destination $Destination
            Write-Host "Moved extension artifact: $Destination"
        }
    }
}

function Write-SettingsShortcut {
    if ($WhatIf) {
        Write-Host "Would create Start Menu shortcut: $ShortcutPath"
        return
    }
    New-Item -ItemType Directory -Force -Path $StartMenu | Out-Null
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $Pythonw
    $Shortcut.Arguments = "`"$SettingsAppScript`""
    $Shortcut.WorkingDirectory = $Repo
    $Shortcut.Description = "Activity Journal Settings"
    $Shortcut.Save()
}

function Write-TrayShortcut {
    if ($WhatIf) {
        Write-Host "Would create Start Menu shortcut: $TrayShortcutPath"
        return
    }
    New-Item -ItemType Directory -Force -Path $StartMenu | Out-Null
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($TrayShortcutPath)
    $Shortcut.TargetPath = $Pythonw
    $Shortcut.Arguments = "`"$TrayAppScript`""
    $Shortcut.WorkingDirectory = $Repo
    $Shortcut.Description = "Activity Journal Tray"
    $Shortcut.Save()
}

function Write-TrayStartupLauncher {
    if ($WhatIf) {
        Write-Host "Would create startup launcher: $TrayLauncherPath"
        return
    }
    New-Item -ItemType Directory -Force -Path $StartupFolder | Out-Null
    $Command = "`"$Pythonw`" `"$TrayAppScript`""
    $EscapedCommand = $Command.Replace('"', '""')
    $Content = @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "$EscapedCommand", 0, False
"@
    Set-Content -LiteralPath $TrayLauncherPath -Value $Content -Encoding ASCII
}

if ($WhatIf) {
    Write-Host "Would verify Python tkinter support."
    Initialize-LocalConfig
    Ensure-LocalDirectories
    Ensure-PythonRequirements
    Move-ExtensionArtifacts
    Write-Host "Would register scheduled tasks with scripts\setup_task.ps1 -WhatIf."
    powershell -ExecutionPolicy Bypass -File $SetupTaskScript -WhatIf
    Write-SettingsShortcut
    Write-TrayShortcut
    Write-TrayStartupLauncher
    exit 0
}

Assert-PythonReady
Initialize-LocalConfig
Ensure-LocalDirectories
Ensure-PythonRequirements
Move-ExtensionArtifacts
powershell -ExecutionPolicy Bypass -File $SetupTaskScript
if ($LASTEXITCODE -ne 0) {
    throw "Task setup failed with exit code $LASTEXITCODE."
}
Write-SettingsShortcut
Write-TrayShortcut
Write-TrayStartupLauncher

Write-Host "Activity Journal local MVP installed."
Write-Host "Config: $ConfigPath"
Write-Host "Settings shortcut: $ShortcutPath"
Write-Host "Tray shortcut: $TrayShortcutPath"
Write-Host "Tray startup launcher: $TrayLauncherPath"
