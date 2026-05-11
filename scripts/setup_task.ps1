param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PowerShell = "powershell"
$DailyScript = Join-Path $Repo "scripts\run_daily.ps1"
$OpenCodexReviewScript = Join-Path $Repo "scripts\open_codex_review.ps1"
$WeeklyScript = Join-Path $Repo "scripts\run_weekly.ps1"
$ActivityWatchScript = Join-Path $Repo "scripts\activity_watch.py"
$ChatGptLiveServerScript = Join-Path $Repo "scripts\chatgpt_live_server.py"
$ConfigPath = Join-Path $Repo "config\activity-journal.json"
$ExampleConfigPath = Join-Path $Repo "config\activity-journal.example.json"
$PythonwCommand = Get-Command "pythonw.exe" -ErrorAction SilentlyContinue
$PythonForWatcher = if ($PythonwCommand) { $PythonwCommand.Source } else { "python" }

function Get-ConfigValue {
    param(
        [object]$Object,
        [string]$Name,
        [object]$Default
    )
    if ($null -ne $Object -and $Object.PSObject.Properties.Name -contains $Name -and $null -ne $Object.$Name) {
        return $Object.$Name
    }
    return $Default
}

$Config = $null
if (Test-Path $ConfigPath) {
    $Config = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
} elseif (Test-Path $ExampleConfigPath) {
    $Config = Get-Content $ExampleConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
$ExternalInputs = Get-ConfigValue $Config "external_inputs" $null
$ActivityWatchSettings = Get-ConfigValue $ExternalInputs "activity_watch" $null
$ChatGptSettings = Get-ConfigValue $ExternalInputs "chatgpt_live" $null
$ActivityInterval = [int](Get-ConfigValue $ActivityWatchSettings "interval_seconds" 30)
$ActivityHeartbeat = [int](Get-ConfigValue $ActivityWatchSettings "heartbeat_seconds" 300)
$ActivityMaxTextChars = [int](Get-ConfigValue $ActivityWatchSettings "max_text_chars_per_item" 20000)
$ActivityIncludeText = [bool](Get-ConfigValue $ActivityWatchSettings "include_accessibility_text" $true)
$ChatGptHost = [string](Get-ConfigValue $ChatGptSettings "server_host" "127.0.0.1")
$ChatGptPort = [int](Get-ConfigValue $ChatGptSettings "server_port" 8765)

$DailyArgument = "-ExecutionPolicy Bypass -File `"$DailyScript`" -NonInteractive -RefreshDrafts -CatchUpMissed"
$WeeklyArgument = "-ExecutionPolicy Bypass -File `"$WeeklyScript`""
$CodexReviewArgument = "-ExecutionPolicy Bypass -File `"$OpenCodexReviewScript`""
$ActivityTextArgument = if ($ActivityIncludeText) { " --include-accessibility-text" } else { "" }
$ActivityWatchArgument = "`"$ActivityWatchScript`" --interval $ActivityInterval --heartbeat $ActivityHeartbeat$ActivityTextArgument --max-text-chars $ActivityMaxTextChars"
$ChatGptLiveServerArgument = "`"$ChatGptLiveServerScript`" --host $ChatGptHost --port $ChatGptPort"

function Write-StartupLauncher {
    param(
        [string]$Name,
        [string]$Command
    )

    $StartupFolder = [Environment]::GetFolderPath("Startup")
    $LauncherPath = Join-Path $StartupFolder "$Name.vbs"
    $EscapedCommand = $Command.Replace('"', '""')
    $Content = "Set WshShell = CreateObject(""WScript.Shell"")`r`nWshShell.Run ""$EscapedCommand"", 0, False`r`n"
    [System.IO.File]::WriteAllText($LauncherPath, $Content, [System.Text.Encoding]::ASCII)
    return $LauncherPath
}

if ($WhatIf) {
    Write-Host "Would register task: Activity Journal Daily"
    Write-Host "  $PowerShell $DailyArgument"
    Write-Host "  Settings: WakeToRun, AllowStartIfOnBatteries, DontStopIfGoingOnBatteries, StartWhenAvailable"
    Write-Host "Would register task: Activity Journal Weekly"
    Write-Host "  $PowerShell $WeeklyArgument"
    Write-Host "Would register task: Activity Journal Watcher"
    Write-Host "  $PythonForWatcher $ActivityWatchArgument"
    Write-Host "Would register task: Activity Journal ChatGPT Receiver"
    Write-Host "  $PythonForWatcher $ChatGptLiveServerArgument"
    Write-Host "Would register task: Activity Journal Codex Review"
    Write-Host "  $PowerShell $CodexReviewArgument"
    exit 0
}

$DailyAction = New-ScheduledTaskAction -Execute $PowerShell -Argument $DailyArgument -WorkingDirectory $Repo
$DailyTrigger = New-ScheduledTaskTrigger -Daily -At 23:50
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$WatcherSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$WeeklyAction = New-ScheduledTaskAction -Execute $PowerShell -Argument $WeeklyArgument -WorkingDirectory $Repo
$WeeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 23:55

$ActivityWatchAction = New-ScheduledTaskAction -Execute $PythonForWatcher -Argument $ActivityWatchArgument -WorkingDirectory $Repo
$ActivityWatchTrigger = New-ScheduledTaskTrigger -AtLogOn

$ChatGptLiveServerAction = New-ScheduledTaskAction -Execute $PythonForWatcher -Argument $ChatGptLiveServerArgument -WorkingDirectory $Repo
$ChatGptLiveServerTrigger = New-ScheduledTaskTrigger -AtLogOn

$CodexReviewAction = New-ScheduledTaskAction -Execute $PowerShell -Argument $CodexReviewArgument -WorkingDirectory $Repo
$CodexReviewTrigger = New-ScheduledTaskTrigger -Daily -At 23:51

Register-ScheduledTask -TaskName "Activity Journal Daily" -Action $DailyAction -Trigger $DailyTrigger -Principal $Principal -Settings $Settings -Description "Create daily activity journal evidence and draft." -Force | Out-Null
Register-ScheduledTask -TaskName "Activity Journal Weekly" -Action $WeeklyAction -Trigger $WeeklyTrigger -Principal $Principal -Settings $Settings -Description "Create weekly activity review and sync to Notion." -Force | Out-Null
try {
    Register-ScheduledTask -TaskName "Activity Journal Watcher" -Action $ActivityWatchAction -Trigger $ActivityWatchTrigger -Principal $Principal -Settings $WatcherSettings -Description "Capture foreground app/window activity for the activity journal." -Force | Out-Null
} catch {
    $LauncherPath = Write-StartupLauncher -Name "Activity Journal Watcher" -Command "`"$PythonForWatcher`" $ActivityWatchArgument"
    Write-Warning "Could not register Activity Journal Watcher scheduled task. Wrote startup launcher instead: $LauncherPath"
}
try {
    Register-ScheduledTask -TaskName "Activity Journal ChatGPT Receiver" -Action $ChatGptLiveServerAction -Trigger $ChatGptLiveServerTrigger -Principal $Principal -Settings $WatcherSettings -Description "Receive ChatGPT/Gemini browser extension captures for the activity journal." -Force | Out-Null
} catch {
    $LauncherPath = Write-StartupLauncher -Name "Activity Journal ChatGPT Receiver" -Command "`"$PythonForWatcher`" $ChatGptLiveServerArgument"
    Write-Warning "Could not register Activity Journal ChatGPT Receiver scheduled task. Wrote startup launcher instead: $LauncherPath"
}
Register-ScheduledTask -TaskName "Activity Journal Codex Review" -Action $CodexReviewAction -Trigger $CodexReviewTrigger -Principal $Principal -Settings $Settings -Description "Open Codex CLI to ask and refine activity journal questions interactively." -Force | Out-Null

try {
    Unregister-ScheduledTask -TaskName "Activity Journal Codex Bridge" -Confirm:$false -ErrorAction SilentlyContinue
} catch {
}

Write-Host "Registered Activity Journal Daily, Weekly, Watcher, ChatGPT Receiver, and Codex Review tasks."
