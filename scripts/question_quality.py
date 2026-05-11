from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

import collect_daily
import project_health
import project_review


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"


def configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
CATEGORY_ORDER = {
    "설정 문제": 0,
    "상태": 1,
    "다음 행동": 2,
    "배운 점": 3,
    "결정": 4,
    "작업명": 5,
}

PLACEHOLDERS = {
    "Studied": {"No study signal inferred."},
    "Built": {"No build signal inferred."},
    "Decisions": {"No decisions captured."},
    "Problems": {"No unresolved problems captured.", "Codex Review has not finalized this draft yet."},
    "Next Actions": {
        "Open Codex Review and answer only the missing clarification questions.",
        "No next actions captured.",
    },
}


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def parse_date(value: str | None, config: dict[str, Any]) -> dt.date:
    return project_health.parse_date(value, config)


def rel(path: Path, root: Path = ROOT) -> str:
    return project_health.rel(path, root)


def candidate_path(day: dt.date, root: Path = ROOT) -> Path:
    return root / "journal" / "raw" / f"question_candidates_{day.isoformat()}.json"


def read_json(path: Path, warnings: list[str]) -> dict[str, Any]:
    if not path.exists():
        warnings.append(f"missing file: {rel(path)}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        warnings.append(f"invalid JSON in {rel(path)}: {exc}")
        return {}
    if not isinstance(value, dict):
        warnings.append(f"unexpected JSON shape in {rel(path)}")
        return {}
    return value


def read_text(path: Path, warnings: list[str]) -> str:
    if not path.exists():
        warnings.append(f"missing file: {rel(path)}")
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warnings.append(f"cannot read {rel(path)}: {exc}")
        return ""


def normalize_bullet(line: str) -> str:
    text = line.strip()
    if text.startswith("-"):
        text = text[1:].strip()
    return text


def section_items(markdown: str, heading: str) -> list[str]:
    pattern = re.compile(rf"^## {re.escape(heading)}\s*$", re.M)
    match = pattern.search(markdown)
    if not match:
        return []
    start = markdown.find("\n", match.end())
    if start == -1:
        return []
    start += 1
    next_heading = re.search(r"^##\s+", markdown[start:], re.M)
    end = start + next_heading.start() if next_heading else len(markdown)
    items: list[str] = []
    for line in markdown[start:end].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("###"):
            continue
        if stripped.startswith("-"):
            items.append(normalize_bullet(stripped))
    return items


def section_is_empty_or_placeholder(items: list[str], heading: str) -> bool:
    if not items:
        return True
    placeholders = PLACEHOLDERS.get(heading, set())
    return all(not item or item in placeholders for item in items)


def section_contains(items: list[str], text: str) -> bool:
    return any(text in item for item in items)


def daily_status(markdown: str) -> str:
    match = re.search(r"^Status:\s*(.+?)\s*$", markdown, re.M)
    return match.group(1).strip() if match else ""


def answered_question_ids(day: dt.date, root: Path = ROOT) -> set[str]:
    path = root / "journal" / "questions" / f"{day.isoformat()}.md"
    if not path.exists():
        return set()
    markdown = path.read_text(encoding="utf-8", errors="replace")
    return {question["id"] for question in project_health.parse_question_blocks(markdown) if question.get("answered")}


def raw_summary(raw: dict[str, Any]) -> dict[str, Any]:
    git_activity = raw.get("git_activity", [])
    modified_files = raw.get("modified_files", {})
    codex = raw.get("codex", {})
    external = raw.get("external_inputs", {})
    projects = sorted(
        {
            str(item.get("project"))
            for item in git_activity
            if isinstance(item, dict) and item.get("project")
        }
        | {str(project) for project in modified_files}
    )
    modified_count = 0
    if isinstance(modified_files, dict):
        modified_count = sum(len(files) for files in modified_files.values() if isinstance(files, list))
    inbox_count = len(external.get("inbox", [])) if isinstance(external, dict) else 0
    chatgpt_count = len(external.get("chatgpt", [])) if isinstance(external, dict) else 0
    chatgpt_live_count = len(external.get("chatgpt_live", [])) if isinstance(external, dict) else 0
    browser_history_count = len(external.get("browser_history", [])) if isinstance(external, dict) else 0
    recent_file_count = len(external.get("recent_files", [])) if isinstance(external, dict) else 0
    activity_watch_count = len(external.get("activity_watch", [])) if isinstance(external, dict) else 0
    history_count = len(codex.get("history", [])) if isinstance(codex, dict) else 0
    session_count = len(codex.get("sessions", [])) if isinstance(codex, dict) else 0
    git_count = len(git_activity) if isinstance(git_activity, list) else 0
    return {
        "projects": projects,
        "modified_file_count": modified_count,
        "git_activity_count": git_count,
        "codex_history_count": history_count,
        "codex_session_count": session_count,
        "inbox_count": inbox_count,
        "chatgpt_count": chatgpt_count,
        "chatgpt_live_count": chatgpt_live_count,
        "browser_history_count": browser_history_count,
        "recent_file_count": recent_file_count,
        "activity_watch_count": activity_watch_count,
        "external_count": inbox_count + chatgpt_count + chatgpt_live_count + browser_history_count + recent_file_count + activity_watch_count,
        "has_activity": any([modified_count, git_count, history_count, session_count, inbox_count, chatgpt_count, chatgpt_live_count, browser_history_count, recent_file_count, activity_watch_count]),
    }


def project_phrase(summary: dict[str, Any]) -> str:
    projects = summary.get("projects") or []
    if projects:
        return ", ".join(projects[:2]) + " 프로젝트"
    return "오늘 수집된 작업"


def add_candidate(candidates: list[dict[str, Any]], answered_ids: set[str], candidate: dict[str, Any]) -> None:
    if candidate["id"] in answered_ids:
        return
    candidates.append(candidate)


def make_candidate(
    stable_id: str,
    category: str,
    severity: str,
    confidence: str,
    reason: str,
    evidence: list[str],
    recommended_answer: str,
) -> dict[str, Any]:
    return {
        "id": stable_id,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "reason": reason,
        "evidence": evidence,
        "recommended_answer": recommended_answer,
    }


def health_issue_summary(health: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for key, section in health.get("sections", {}).items():
        if key == "question_quality":
            continue
        status = section.get("status", "OK")
        if status == "Action Needed":
            issues.append(f"{key}: {status}")
        elif status == "Warning" and key in {"notion_sync", "external_inputs", "file_access"}:
            issues.append(f"{key}: {status}")
    return issues


def generate_candidates(day: dt.date, config: dict[str, Any], root: Path = ROOT) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    warnings: list[str] = []
    raw_path = root / "journal" / "raw" / f"{day.isoformat()}.json"
    daily_path = root / "journal" / "daily" / f"{day.isoformat()}.md"
    raw = read_json(raw_path, warnings)
    daily = read_text(daily_path, warnings)
    answered_ids = answered_question_ids(day, root)
    summary = raw_summary(raw)

    studied = section_items(daily, "Studied")
    built = section_items(daily, "Built")
    decisions = section_items(daily, "Decisions")
    problems = section_items(daily, "Problems")
    next_actions = section_items(daily, "Next Actions")
    candidates: list[dict[str, Any]] = []

    if summary["has_activity"] and section_is_empty_or_placeholder(studied, "Studied"):
        add_candidate(
            candidates,
            answered_ids,
            make_candidate(
                "daily_learning",
                "배운 점",
                "medium",
                "high",
                "수집된 작업 흔적은 있지만 Studied 섹션이 기본 placeholder 상태입니다.",
                [
                    f"modified files: {summary['modified_file_count']}",
                    f"codex history items: {summary['codex_history_count']}",
                    f"external inputs: {summary['external_count']}",
                ],
                f"오늘은 {project_phrase(summary)}를 다루면서 자동 기록 흐름의 부족한 부분을 점검하고 개선했다.",
            ),
        )

    if not section_is_empty_or_placeholder(built, "Built") and section_is_empty_or_placeholder(decisions, "Decisions"):
        add_candidate(
            candidates,
            answered_ids,
            make_candidate(
                "work_status",
                "상태",
                "medium",
                "medium",
                "Built 섹션에는 작업 결과가 있지만 완료/진행/실험 중인지 판단할 결정 기록이 비어 있습니다.",
                [f"built items: {len(built)}", f"daily status: {daily_status(daily) or 'missing'}"],
                "작업 상태는 진행 중이며, 오늘 만든 변경은 검증을 거쳐 기록 시스템 개선에 반영한다.",
            ),
        )

    if section_is_empty_or_placeholder(next_actions, "Next Actions") or section_contains(next_actions, "Open Codex Review"):
        add_candidate(
            candidates,
            answered_ids,
            make_candidate(
                "next_action",
                "다음 행동",
                "high",
                "high",
                "Next Actions가 기본 검토 안내 문구라서 다음에 무엇을 할지 기록으로 남기기 어렵습니다.",
                [f"next action items: {len(next_actions)}"],
                "다음에는 Codex Review에서 후보 질문을 확인하고, 실제로 부족한 답변만 반영해 Daily Log를 Confirmed로 전환한다.",
            ),
        )

    if section_contains(problems, "Codex Review has not finalized"):
        add_candidate(
            candidates,
            answered_ids,
            make_candidate(
                "unresolved_problem",
                "상태",
                "high",
                "high",
                "Problems 섹션에 Codex Review 미확정 기본 문구가 남아 있습니다.",
                ["Problems: Codex Review has not finalized this draft yet."],
                "Codex Review를 실행해 남은 후보 질문을 확인하고, 필요한 답변만 반영한 뒤 미확정 문구를 제거한다.",
            ),
        )

    studied_source_only = bool(studied) and all(item.startswith(("Inbox:", "ChatGPT:", "ChatGPT live:", "Browser activity:", "Recent files:", "App activity:")) for item in studied if item)
    if summary["external_count"] and (section_is_empty_or_placeholder(studied, "Studied") or studied_source_only):
        add_candidate(
            candidates,
            answered_ids,
            make_candidate(
                "external_learning",
                "배운 점",
                "medium",
                "medium",
                "inbox 또는 ChatGPT export 입력은 있지만 Daily에는 제목 수준의 신호만 있어 실제 학습/결정이 불명확합니다.",
                [
                    f"inbox items: {summary['inbox_count']}",
                    f"chatgpt conversations: {summary['chatgpt_count']}",
                    f"chatgpt live captures: {summary['chatgpt_live_count']}",
                    f"browser pages: {summary['browser_history_count']}",
                    f"recent files: {summary['recent_file_count']}",
                    f"app windows: {summary['activity_watch_count']}",
                ],
                "외부 입력에서 얻은 핵심 결론은 자동 기록 시스템을 믿고 맡기기 위해 수집 범위와 질문 품질을 분리해 관리해야 한다는 점이다.",
            ),
        )

    try:
        project_rollup = project_review.build_rollup(day, root=root, update_metadata=False)
    except RuntimeError as exc:
        warnings.append(str(exc))
        project_rollup = {"projects": {}}
    for project in project_rollup.get("projects", {}).values():
        if not isinstance(project, dict) or project.get("goal"):
            continue
        project_name = str(project.get("name") or "Unknown project")
        project_slug = str(project.get("slug") or "unknown")
        add_candidate(
            candidates,
            answered_ids,
            make_candidate(
                f"project_goal_{project_slug}",
                "결정",
                "medium",
                "high",
                "프로젝트 로그를 장기 회고로 쓰려면 프로젝트 목표가 필요하지만 아직 비어 있습니다.",
                [f"project: {project_name}", f"project log: journal/projects/{project_slug}.md"],
                f"{project_name}의 목표를 한 문장으로 정해주세요. 예: '{project_name}를 안정적으로 운영 가능한 결과물로 만든다.'",
            ),
        )

    health = project_health.build_report(day, config, root=root, include_scheduler=False, include_question_quality=False)
    health_issues = health_issue_summary(health)
    if health_issues:
        highest = "high" if any("Action Needed" in issue for issue in health_issues) else "medium"
        add_candidate(
            candidates,
            answered_ids,
            make_candidate(
                "system_issue",
                "설정 문제",
                highest,
                "high",
                "상태 리포트에 기록 신뢰도에 영향을 줄 수 있는 Warning 또는 Action Needed 항목이 있습니다.",
                health_issues,
                "상태 리포트의 Recommended Actions를 확인하고, 기록 누락에 직접 영향을 주는 항목부터 처리한다.",
            ),
        )

    return sort_and_dedupe(candidates), warnings, {"answered_ids": sorted(answered_ids), "health_overall": health["overall"]}


def sort_and_dedupe(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        current = deduped.get(candidate["id"])
        if current is None or candidate_sort_key(candidate) < candidate_sort_key(current):
            deduped[candidate["id"]] = candidate
    return sorted(deduped.values(), key=candidate_sort_key)


def candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        SEVERITY_ORDER.get(str(candidate.get("severity")), 9),
        CONFIDENCE_ORDER.get(str(candidate.get("confidence")), 9),
        CATEGORY_ORDER.get(str(candidate.get("category")), 9),
        str(candidate.get("id")),
    )


def build_report(day: dt.date, config: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    candidates, warnings, metadata = generate_candidates(day, config, root)
    return {
        "report_type": "activity_journal_question_candidates",
        "generated_at": project_health.now(config).isoformat(timespec="seconds"),
        "checked_date": day.isoformat(),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "answered_ids": metadata["answered_ids"],
        "health_overall": metadata["health_overall"],
        "source_files": {
            "raw": project_health.file_info(root / "journal" / "raw" / f"{day.isoformat()}.json", root),
            "daily": project_health.file_info(root / "journal" / "daily" / f"{day.isoformat()}.md", root),
            "questions": project_health.file_info(root / "journal" / "questions" / f"{day.isoformat()}.md", root),
            "system_status": project_health.file_info(root / "journal" / "raw" / "system_status.json", root),
        },
        "warnings": warnings,
    }


def write_report(report: dict[str, Any], root: Path = ROOT) -> Path:
    path = candidate_path(dt.date.fromisoformat(report["checked_date"]), root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> None:
    configure_utf8_console()
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date to check, YYYY-MM-DD. Defaults to config-timezone today.")
    parser.add_argument("--json-only", action="store_true", help="Print JSON to stdout.")
    parser.add_argument("--no-write", action="store_true", help="Do not write journal/raw/question_candidates_YYYY-MM-DD.json.")
    args = parser.parse_args()

    config = load_config()
    try:
        day = parse_date(args.date, config)
    except ValueError:
        parser.error(f"--date must be YYYY-MM-DD, got: {args.date}")

    report = build_report(day, config)
    if not args.no_write:
        path = write_report(report)
    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.no_write:
        print(f"Question candidates: {report['candidate_count']} for {report['checked_date']}")
    else:
        print(f"Wrote question candidates: {path}")
        print(f"Candidates: {report['candidate_count']}")


if __name__ == "__main__":
    main()
