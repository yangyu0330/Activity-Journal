<# Legacy script. Current supported flow is scripts/run_daily.ps1 -> scripts/open_codex_review.ps1.
   Kept for reference/compatibility; do not use for the normal daily review flow. #>
param(
    [string]$Date = (Get-Date -Format "yyyy-MM-dd"),
    [switch]$AllowLegacyRun
)

$ErrorActionPreference = "Stop"

if (-not $AllowLegacyRun) {
    Write-Host "run_codex_bridge.ps1 is a legacy script and is not part of the supported daily flow."
    Write-Host "Use scripts\run_daily.ps1 instead, or pass -AllowLegacyRun to run this legacy bridge intentionally."
    exit 2
}

$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$TemplatePath = Join-Path $Repo "prompts\codex_activity_review.md"
$QuestionsFile = Join-Path $Repo "journal\questions\$Date.md"
$DailyFile = Join-Path $Repo "journal\daily\$Date.md"
$RawFile = Join-Path $Repo "journal\raw\$Date.json"
$LogDir = Join-Path $Repo "journal\raw"
$PromptFile = Join-Path $LogDir "_codex_bridge_prompt_$Date.md"
$OutputFile = Join-Path $LogDir "_codex_bridge_output_$Date.txt"

if (-not (Test-Path -LiteralPath $TemplatePath)) {
    throw "Missing prompt template: $TemplatePath"
}

if (-not (Test-Path -LiteralPath $QuestionsFile)) {
    Write-Host "Codex bridge skipped: no questions file for $Date."
    exit 0
}

$questions = Get-Content -LiteralPath $QuestionsFile -Raw -Encoding UTF8
if ($questions -notmatch "Answer:\s*\S") {
    Write-Host "Codex bridge skipped: no answered questions for $Date."
    exit 0
}

$template = Get-Content -LiteralPath $TemplatePath -Raw -Encoding UTF8
$prompt = $template.Replace("{DATE}", $Date)
$prompt += "`n`nCurrent files:`n"
$prompt += "- Questions: $QuestionsFile`n"
$prompt += "- Daily log: $DailyFile`n"
$prompt += "- Raw evidence: $RawFile`n"

[System.IO.File]::WriteAllText($PromptFile, $prompt, [System.Text.UTF8Encoding]::new($false))

$command = "codex exec --skip-git-repo-check -C `"$Repo`" -s workspace-write - < `"$PromptFile`""
cmd.exe /c $command | Tee-Object -FilePath $OutputFile
if ($LASTEXITCODE -ne 0) {
    throw "Legacy Codex bridge failed with exit code $LASTEXITCODE."
}
