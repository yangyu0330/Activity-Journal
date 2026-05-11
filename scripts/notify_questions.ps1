<# Legacy script. Current supported flow is scripts/run_daily.ps1 -> scripts/open_codex_review.ps1.
   Kept for reference/compatibility; do not use for the normal daily review flow. #>
param(
    [string]$Date = (Get-Date -Format "yyyy-MM-dd")
)

$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$QuestionsFile = Join-Path $Repo "journal\questions\$Date.md"

if (-not (Test-Path -LiteralPath $QuestionsFile)) {
    exit 0
}

$Content = Get-Content -LiteralPath $QuestionsFile -Raw -Encoding UTF8
$HasQuestions = ($Content -notmatch "No questions") -and ($Content -match "Answer:")

if (-not $HasQuestions) {
    exit 0
}

$Message = "Activity Journal has questions for $Date. Answer them to finalize today's log."

$PreviousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$msgOutput = & msg.exe $env:USERNAME $Message 2>$null
$MsgExitCode = $LASTEXITCODE
$ErrorActionPreference = $PreviousErrorActionPreference

if ($MsgExitCode -ne 0) {
    Write-Host $Message
}

Start-Process notepad.exe -ArgumentList "`"$QuestionsFile`""
