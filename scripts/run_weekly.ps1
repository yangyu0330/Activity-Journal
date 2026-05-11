param(
    [string]$Date
)

$ErrorActionPreference = "Stop"

function Assert-NativeCommandSucceeded {
    param([string]$Step)

    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$CollectScript = Join-Path $Repo "scripts\collect_daily.py"
$WeeklyScript = Join-Path $Repo "scripts\weekly_review.py"
$ProjectReviewScript = Join-Path $Repo "scripts\project_review.py"
$NotionSyncScript = Join-Path $Repo "scripts\notion_sync.py"

if ([string]::IsNullOrWhiteSpace($Date)) {
    $ResolvedDate = (python $CollectScript --print-default-date).Trim()
    Assert-NativeCommandSucceeded "Default date resolution"
} else {
    $ResolvedDate = $Date
}

python $WeeklyScript --date $ResolvedDate
Assert-NativeCommandSucceeded "Weekly review generation"

python $ProjectReviewScript --date $ResolvedDate
Assert-NativeCommandSucceeded "Project review rollup"

python $NotionSyncScript --date $ResolvedDate
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Notion sync failed with exit code $LASTEXITCODE. Local journal files were preserved."
}
