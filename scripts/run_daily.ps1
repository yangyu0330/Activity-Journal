param(
    [string]$Date,
    [switch]$NonInteractive,
    [switch]$RefreshDrafts,
    [switch]$CatchUpMissed
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
$OpenCodexReviewScript = Join-Path $Repo "scripts\open_codex_review.ps1"
$ProjectReviewScript = Join-Path $Repo "scripts\project_review.py"
$AutoRecoverScript = Join-Path $Repo "scripts\auto_recover.py"
$NotionSyncScript = Join-Path $Repo "scripts\notion_sync.py"
$SqliteSyncScript = Join-Path $Repo "scripts\sync_sqlite.py"
$RetentionCleanupScript = Join-Path $Repo "scripts\retention_cleanup.py"
$ResolvedDate = $null

function Get-PreviousDate {
    param([string]$CurrentDate)
    return ([datetime]::ParseExact($CurrentDate, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)).AddDays(-1).ToString("yyyy-MM-dd")
}

function Test-DailyNeedsCatchUp {
    param([string]$TargetDate)

    $RawPath = Join-Path $Repo "journal\raw\$TargetDate.json"
    $DailyPath = Join-Path $Repo "journal\daily\$TargetDate.md"
    $QuestionsPath = Join-Path $Repo "journal\questions\$TargetDate.md"
    if (-not (Test-Path -LiteralPath $RawPath) -or -not (Test-Path -LiteralPath $DailyPath) -or -not (Test-Path -LiteralPath $QuestionsPath)) {
        return $true
    }

    $LateRunCutoff = ([datetime]::ParseExact($TargetDate, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)).AddHours(22)
    return (Get-Item -LiteralPath $RawPath).LastWriteTime -lt $LateRunCutoff
}

function Invoke-DailyWorkflow {
    param(
        [string]$TargetDate,
        [bool]$RefreshReviewDrafts,
        [bool]$OpenInteractiveReview
    )

    if ($RefreshReviewDrafts) {
        python $CollectScript --date $TargetDate --overwrite-review-files
    } else {
        python $CollectScript --date $TargetDate
    }
    Assert-NativeCommandSucceeded "Daily evidence collection"

    python $ProjectReviewScript --date $TargetDate
    Assert-NativeCommandSucceeded "Project review rollup"

    python $SqliteSyncScript --date $TargetDate
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "SQLite sync failed with exit code $LASTEXITCODE. Local journal files were preserved."
    }

    python $NotionSyncScript --finalize-due --through-date $TargetDate
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Notion sync failed with exit code $LASTEXITCODE. Local journal files were preserved."
    }

    if ($OpenInteractiveReview) {
        powershell -ExecutionPolicy Bypass -File $OpenCodexReviewScript -Date $TargetDate
        Assert-NativeCommandSucceeded "Codex Review launch"
    }

    python $AutoRecoverScript --date $TargetDate --no-open-codex --from-run-daily
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Auto recovery failed with exit code $LASTEXITCODE."
    }
    python $RetentionCleanupScript
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Retention cleanup failed with exit code $LASTEXITCODE. Raw logs were preserved."
    }
}

try {
    if ([string]::IsNullOrWhiteSpace($Date)) {
        $ResolvedDate = (python $CollectScript --print-default-date).Trim()
        Assert-NativeCommandSucceeded "Default date resolution"
    } else {
        $ResolvedDate = $Date
    }

    if ($CatchUpMissed -and [string]::IsNullOrWhiteSpace($Date)) {
        $MissedDate = Get-PreviousDate $ResolvedDate
        if (Test-DailyNeedsCatchUp $MissedDate) {
            Write-Host "Catching up missed Activity Journal daily run for $MissedDate."
            Invoke-DailyWorkflow -TargetDate $MissedDate -RefreshReviewDrafts $false -OpenInteractiveReview $false
        }
    }

    Invoke-DailyWorkflow -TargetDate $ResolvedDate -RefreshReviewDrafts ([bool]$RefreshDrafts) -OpenInteractiveReview (-not $NonInteractive)
} catch {
    Write-Error $_
    exit 1
}
