<# Legacy script. Current supported flow is scripts/run_daily.ps1 -> scripts/open_codex_review.ps1.
   Kept for reference/compatibility; do not use for the normal daily review flow. #>
param(
    [string]$Date = (Get-Date -Format "yyyy-MM-dd"),
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"

function Test-WeakAnswer {
    param([string]$Answer)

    if ([string]::IsNullOrWhiteSpace($Answer)) {
        return $true
    }

    $normalized = $Answer.Trim().ToLowerInvariant()
    $weakAnswers = @(
        "aa",
        "n/a",
        "na",
        "idk",
        "i don't know",
        "i dont know",
        "don't know",
        "dont know",
        "not sure",
        "no idea",
        "nothing",
        "none"
    )

    if ($weakAnswers -contains $normalized) {
        return $true
    }

    if ($normalized -match "모르") {
        return $true
    }

    if ($normalized.Length -lt 4) {
        return $true
    }

    return $false
}

function Get-QuestionHelp {
    param([string]$Question)

    if ($Question -like "오늘 새로 배웠거나 명확해진 것은 무엇인가요?*") {
        return "오늘 이해한 개념, 새로 알게 된 점, 정리된 판단을 한 줄로 적어주세요. 이 내용은 Notion의 Studied 섹션에 들어갑니다."
    }
    if ($Question.StartsWith("[배운 점]")) {
        return "추천 답변을 그대로 저장하거나, 오늘 실제로 배운 점을 한 줄로 수정해서 답하세요."
    }
    if ($Question.StartsWith("[작업명]")) {
        return "추천 작업명을 그대로 쓰거나, 이 변경을 나중에 찾기 쉬운 프로젝트/작업명으로 바꿔 답하세요."
    }
    if ($Question.StartsWith("[상태]")) {
        return "변경사항이 완료, 진행 중, 실험, 정리 작업 중 어디에 가까운지 선택하거나 직접 적어주세요."
    }
    if ($Question.StartsWith("[수동 메모]")) {
        return "자동 수집되지 않은 공부/회의/브라우저 학습이 있으면 적고, 없으면 저장하지 않음을 선택하세요."
    }
    if ($Question -like "오늘 수정된 파일들은 어떤 프로젝트 작업으로 기록할까요?*") {
        return "파일은 바뀌었지만 git 기록만으로는 어떤 작업인지 알 수 없습니다. 이 변경을 어느 프로젝트/작업명으로 남길지 적어주세요."
    }
    if ($Question -like "*커밋 없이*파일이 변경됐습니다*") {
        return "여러 파일이 바뀌었지만 아직 커밋이 없습니다. 완료된 결과물인지, 진행 중 구현인지, 실험인지, 정리 작업인지 알려주세요."
    }
    if ($Question -like "Codex 대화에 탐색성 질문이 있었습니다*") {
        return "Codex와 탐색한 내용 중 나중에 다시 볼 결정이나 배운 점이 있으면 적어주세요. 없으면 '남길 내용 없음'이라고 답하세요."
    }
    if ($Question -like "오늘 자동 수집된 활동이 거의 없습니다*") {
        return "자동 수집으로는 활동이 잡히지 않았습니다. 다른 곳에서 공부/작업했다면 한 줄로 남기고, 없으면 '작업 없음'이라고 답하세요."
    }

    return "나중에 원본 파일을 다시 열지 않아도 이해할 수 있을 정도로 구체적으로 답하세요."
}

$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$QuestionsFile = Join-Path $Repo "journal\questions\$Date.md"
$DailyFile = Join-Path $Repo "journal\daily\$Date.md"

if (-not (Test-Path -LiteralPath $QuestionsFile)) {
    exit 0
}

$Lines = [System.Collections.Generic.List[string]]::new()
(Get-Content -LiteralPath $QuestionsFile -Encoding UTF8) | ForEach-Object { [void]$Lines.Add($_) }

$Questions = @()
for ($i = 0; $i -lt $Lines.Count; $i++) {
    if ($Lines[$i] -match '^\d+\.\s+(.+)$') {
        $questionText = $Matches[1]
        $answerIndex = $i + 1
        while ($answerIndex -lt $Lines.Count -and $Lines[$answerIndex] -notmatch '^\s+-\s+Answer:') {
            $answerIndex++
        }
        if ($answerIndex -lt $Lines.Count -and $Lines[$answerIndex] -match '^\s+-\s+Answer:\s*$') {
            $Questions += [pscustomobject]@{
                Question = $questionText
                AnswerIndex = $answerIndex
            }
        }
    }
}

if ($Questions.Count -eq 0) {
    exit 0
}

if ($NonInteractive) {
    foreach ($item in $Questions) {
        Write-Host "Pending question: $($item.Question)"
        Write-Host "  Help: $(Get-QuestionHelp -Question $item.Question)"
    }
    exit 0
}

Add-Type -AssemblyName Microsoft.VisualBasic

$Answers = @()
foreach ($item in $Questions) {
    $help = Get-QuestionHelp -Question $item.Question
    $prompt = "$($item.Question)`r`n`r`n왜 묻는지:`r`n$help"
    $answer = [Microsoft.VisualBasic.Interaction]::InputBox($prompt, "Activity Journal 질문", "")

    if (Test-WeakAnswer -Answer $answer) {
        $retryPrompt = "답변이 너무 모호해서 나중에 회고 기록으로 쓰기 어렵습니다.`r`n`r`n질문:`r`n$($item.Question)`r`n`r`n무엇을 말하는지:`r`n$help`r`n`r`n구체적인 프로젝트, 배운 점, 결정사항을 적거나 '남길 내용 없음'처럼 명확히 답하세요."
        $answer = [Microsoft.VisualBasic.Interaction]::InputBox($retryPrompt, "Activity Journal 추가 설명 필요", "")
    }

    if (-not (Test-WeakAnswer -Answer $answer)) {
        $Lines[$item.AnswerIndex] = "   - Answer: $answer"
        $Answers += [pscustomobject]@{
            Question = $item.Question
            Answer = $answer
        }
    }
}

if ($Answers.Count -eq 0) {
    exit 0
}

[System.IO.File]::WriteAllLines($QuestionsFile, $Lines, [System.Text.UTF8Encoding]::new($false))

if (Test-Path -LiteralPath $DailyFile) {
    $Daily = Get-Content -LiteralPath $DailyFile -Raw -Encoding UTF8
    $DecisionLines = ($Answers | ForEach-Object { "- $($_.Question) -> $($_.Answer)" }) -join "`n"

    if ($Daily -match '(?s)## Decisions\s*\r?\n-\s*\r?\n') {
        $Daily = [regex]::Replace($Daily, '(?s)## Decisions\s*\r?\n-\s*\r?\n', "## Decisions`n$DecisionLines`n", 1)
    } elseif ($Daily -match '(?s)(## Decisions\s*\r?\n)(.*?)(\r?\n## )') {
        $Daily = [regex]::Replace($Daily, '(?s)(## Decisions\s*\r?\n)(.*?)(\r?\n## )', "`${1}`$2$DecisionLines`n`${3}", 1)
    } else {
        $Daily += "`n## Decisions`n$DecisionLines`n"
    }

    [System.IO.File]::WriteAllText($DailyFile, $Daily, [System.Text.UTF8Encoding]::new($false))
}

Write-Host "Saved $($Answers.Count) answer(s) to $QuestionsFile and $DailyFile."
