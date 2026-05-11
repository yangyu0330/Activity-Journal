param(
    [string]$Date,
    [switch]$WhatIf,
    [switch]$AllowMissingInputs
)

$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$CollectScript = Join-Path $Repo "scripts\collect_daily.py"
$QuestionQualityScript = Join-Path $Repo "scripts\question_quality.py"

if ([string]::IsNullOrWhiteSpace($Date)) {
    $Date = (python $CollectScript --print-default-date).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Default date resolution failed with exit code $LASTEXITCODE."
    }
}

$CommandFile = Join-Path $Repo "journal\raw\_open_codex_review_$Date.cmd"
$RawFile = Join-Path $Repo "journal\raw\$Date.json"
$DailyFile = Join-Path $Repo "journal\daily\$Date.md"

$MissingInputs = @()
if (-not (Test-Path -LiteralPath $RawFile)) {
    $MissingInputs += $RawFile
}
if (-not (Test-Path -LiteralPath $DailyFile)) {
    $MissingInputs += $DailyFile
}

if ($MissingInputs.Count -gt 0) {
    Write-Host "Codex review inputs are missing for ${Date}:"
    foreach ($MissingInput in $MissingInputs) {
        Write-Host "  $MissingInput"
    }
    if (-not $AllowMissingInputs) {
        Write-Host "Run scripts\run_daily.ps1 -NonInteractive first, or pass -AllowMissingInputs to open review anyway."
        exit 2
    }
    Write-Host "Continuing because -AllowMissingInputs was provided."
}

if ($WhatIf) {
    Write-Host "Would generate question candidates for $Date using $QuestionQualityScript"
    Write-Host "Would open Codex review for $Date using $CommandFile"
    exit 0
}

try {
    python $QuestionQualityScript --date $Date
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Question candidate generation failed with exit code $LASTEXITCODE. Codex Review will continue."
    }
}
catch {
    Write-Warning "Question candidate generation failed. Codex Review will continue. $($_.Exception.Message)"
}

$TemplatePath = Join-Path $Repo "prompts\codex_activity_review.md"
$Prompt = (Get-Content -LiteralPath $TemplatePath -Raw -Encoding UTF8).Replace("{DATE}", $Date)

$EncodedPrompt = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Prompt))
$Command = @"
cd /d "$Repo"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$([char]36)p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$EncodedPrompt')); cmd /c codex -C `"$Repo`" -s workspace-write `"$([char]36)p`""
pause
"@

[System.IO.Directory]::CreateDirectory((Split-Path -Parent $CommandFile)) | Out-Null
[System.IO.File]::WriteAllText($CommandFile, $Command, [System.Text.Encoding]::ASCII)

Start-Process -FilePath "cmd.exe" -ArgumentList "/k `"$CommandFile`""
