from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import collect_daily
import project_health


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"
RECOVERY_MD_PATH = ROOT / "journal" / "recovery_status.md"

CommandRunner = Callable[[list[str], Path], dict[str, Any]]


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def parse_date(value: str | None, config: dict[str, Any]) -> dt.date:
    return project_health.parse_date(value, config)


def now(config: dict[str, Any]) -> str:
    return project_health.now(config).isoformat(timespec="seconds")


def rel(path: Path, root: Path = ROOT) -> str:
    return project_health.rel(path, root)


def recovery_json_path(day: dt.date, root: Path = ROOT) -> Path:
    return root / "journal" / "raw" / f"recovery_{day.isoformat()}.json"


def run_command(args: list[str], root: Path = ROOT) -> dict[str, Any]:
    result = subprocess.run(
        args,
        cwd=str(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def python_script(script: str, day: dt.date) -> list[str]:
    return [sys.executable, f"scripts/{script}", "--date", day.isoformat()]


def powershell_script(script: str, day: dt.date) -> list[str]:
    return ["powershell", "-ExecutionPolicy", "Bypass", "-File", f"scripts\\{script}", "-Date", day.isoformat()]


def record_action(actions: list[dict[str, Any]], action_id: str, reason: str, command: list[str], no_write: bool, runner: CommandRunner, root: Path) -> bool:
    record: dict[str, Any] = {
        "id": action_id,
        "reason": reason,
        "command": command,
        "status": "planned" if no_write else "running",
    }
    if no_write:
        actions.append(record)
        return False
    result = runner(command, root)
    record.update(result)
    record["status"] = "completed" if result.get("exit_code") == 0 else "failed"
    actions.append(record)
    return record["status"] == "completed"


def is_missing_or_error(section: dict[str, Any]) -> bool:
    return not section.get("generated", True) or bool(section.get("error"))


def latest_mtime(paths: list[Path]) -> float:
    values: list[float] = []
    for path in paths:
        if path.exists():
            try:
                values.append(path.stat().st_mtime)
            except OSError:
                continue
    return max(values) if values else 0.0


def daily_source_paths(day: dt.date, root: Path) -> list[Path]:
    paths: list[Path] = []
    daily_dir = root / "journal" / "daily"
    if not daily_dir.exists():
        return paths
    for path in daily_dir.glob("*.md"):
        try:
            file_day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if file_day <= day:
            paths.append(path)
    metadata = root / "journal" / "projects" / "project_metadata.json"
    if metadata.exists():
        paths.append(metadata)
    return paths


def file_stale(output: Path, sources: list[Path]) -> bool:
    if not output.exists():
        return True
    try:
        output_mtime = output.stat().st_mtime
    except OSError:
        return True
    return latest_mtime(sources) > output_mtime


def question_sources(day: dt.date, root: Path) -> list[Path]:
    date = day.isoformat()
    return [
        root / "journal" / "raw" / f"{date}.json",
        root / "journal" / "daily" / f"{date}.md",
        root / "journal" / "raw" / f"project_rollup_{date}.json",
        root / "journal" / "projects" / "project_metadata.json",
    ]


def ensure_external_dirs(config: dict[str, Any], root: Path, no_write: bool, actions: list[dict[str, Any]]) -> None:
    external = config.get("external_inputs", {})
    if not external.get("enabled", False):
        return
    for key, default in [("inbox_dir", "inbox"), ("chatgpt_export_dir", "imports/chatgpt")]:
        path = collect_daily.configured_path(str(external.get(key, default)), root)
        if path.exists():
            continue
        record = {
            "id": f"create_{key}",
            "reason": f"Create missing external input directory: {rel(path, root)}",
            "path": rel(path, root),
            "status": "planned" if no_write else "completed",
        }
        if not no_write:
            path.mkdir(parents=True, exist_ok=True)
        actions.append(record)


def skipped_user_actions(report: dict[str, Any]) -> list[dict[str, str]]:
    skipped: list[dict[str, str]] = []
    sections = report.get("sections", {})
    if sections.get("notion_sync", {}).get("status") in {"Warning", "Action Needed"}:
        skipped.append({"id": "notion_setup", "reason": "Notion token/state/config issues require user action; auto recovery does not change Notion credentials or call the Notion API."})
    if sections.get("scheduler", {}).get("status") in {"Warning", "Action Needed"}:
        skipped.append({"id": "scheduler_setup", "reason": "Scheduler repair is not automatic; run scripts/setup_task.ps1 manually after reviewing."})
    access = sections.get("file_access", {})
    if access.get("access_denied"):
        skipped.append({"id": "file_permissions", "reason": "Access denied paths are reported only; auto recovery does not change permissions or delete paths."})
    external = sections.get("external_inputs", {})
    if external.get("chatgpt_count") == 0:
        skipped.append({"id": "chatgpt_export", "reason": "ChatGPT export request/download needs user action; auto recovery only creates the import folder."})
    return skipped


def high_priority_candidates(day: dt.date, root: Path) -> list[str]:
    path = root / "journal" / "raw" / f"question_candidates_{day.isoformat()}.json"
    if not path.exists():
        return []
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    candidates = report.get("candidates", [])
    if not isinstance(candidates, list):
        return []
    out: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_id = str(candidate.get("id") or "")
        severity = str(candidate.get("severity") or "").lower()
        confidence = str(candidate.get("confidence") or "").lower()
        if candidate_id.startswith("project_goal_") or (severity == "high" and confidence == "high"):
            out.append(candidate_id)
    return out


def codex_already_opened(day: dt.date, root: Path) -> bool:
    path = recovery_json_path(day, root)
    if not path.exists():
        return False
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return bool(report.get("codex_review", {}).get("opened"))


def maybe_open_codex(
    day: dt.date,
    root: Path,
    no_write: bool,
    open_codex: bool,
    from_run_daily: bool,
    actions: list[dict[str, Any]],
    runner: CommandRunner,
) -> dict[str, Any]:
    candidates = high_priority_candidates(day, root)
    result: dict[str, Any] = {
        "needed": bool(candidates),
        "candidate_ids": candidates,
        "opened": False,
        "reason": "",
    }
    if not candidates:
        result["reason"] = "No high-priority or project-goal question candidates remain."
        return result
    if no_write:
        result["reason"] = "Dry run; Codex Review not opened."
        return result
    if not open_codex:
        result["reason"] = "Codex Review opening disabled by option."
        return result
    if from_run_daily:
        result["reason"] = "Called from run_daily; interactive Codex Review is left to the scheduled review task."
        return result
    if codex_already_opened(day, root):
        result["reason"] = "Codex Review was already opened by a previous recovery run for this date."
        return result
    command = powershell_script("open_codex_review.ps1", day)
    opened = record_action(actions, "open_codex_review", "Open Codex Review for remaining user decisions.", command, no_write, runner, root)
    result["opened"] = opened
    result["reason"] = "Opened Codex Review." if opened else "Codex Review launch command failed."
    return result


def recover(
    day: dt.date,
    config: dict[str, Any],
    root: Path = ROOT,
    no_write: bool = False,
    open_codex: bool = True,
    from_run_daily: bool = False,
    runner: CommandRunner = run_command,
) -> dict[str, Any]:
    initial = project_health.build_report(day, config, root=root)
    actions: list[dict[str, Any]] = []

    daily = initial["sections"]["daily_collection"]
    if daily.get("missing"):
        record_action(actions, "collect_daily", "Missing raw/daily/questions files.", python_script("collect_daily.py", day), no_write, runner, root)

    weekly = initial["sections"]["weekly_review"]
    if weekly.get("status") != "OK":
        record_action(actions, "weekly_review", "Weekly review is missing.", python_script("weekly_review.py", day), no_write, runner, root)

    ensure_external_dirs(config, root, no_write, actions)

    project_rollup_path = root / "journal" / "raw" / f"project_rollup_{day.isoformat()}.json"
    project_section = initial["sections"]["project_review"]
    project_needs_refresh = is_missing_or_error(project_section) or file_stale(project_rollup_path, daily_source_paths(day, root))
    if project_needs_refresh:
        record_action(actions, "project_review", "Project rollup is missing or stale.", python_script("project_review.py", day), no_write, runner, root)

    question_path = root / "journal" / "raw" / f"question_candidates_{day.isoformat()}.json"
    question_section = initial["sections"]["question_quality"]
    question_needs_refresh = is_missing_or_error(question_section) or project_needs_refresh or file_stale(question_path, question_sources(day, root))
    if question_needs_refresh:
        record_action(actions, "question_quality", "Question candidates are missing or stale.", python_script("question_quality.py", day), no_write, runner, root)

    final_report = project_health.build_report(day, config, root=root)
    skipped = skipped_user_actions(final_report)
    codex_review = maybe_open_codex(day, root, no_write, open_codex, from_run_daily, actions, runner)
    report = {
        "report_type": "activity_journal_recovery",
        "generated_at": now(config),
        "checked_date": day.isoformat(),
        "initial_overall": initial["overall"],
        "final_overall": final_report["overall"],
        "no_write": no_write,
        "from_run_daily": from_run_daily,
        "actions": actions,
        "action_count": len([action for action in actions if action.get("status") == "completed"]),
        "failed_action_count": len([action for action in actions if action.get("status") == "failed"]),
        "skipped_user_actions": skipped,
        "remaining_user_action_count": len(skipped),
        "codex_review": codex_review,
    }
    if not no_write:
        write_recovery_report(report, root)
        project_health.write_report(project_health.build_report(day, config, root=root), root=root)
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Activity Journal Recovery Status - {report['checked_date']}",
        "",
        f"- Initial overall: {report['initial_overall']}",
        f"- Final overall: {report['final_overall']}",
        f"- Generated at: {report['generated_at']}",
        f"- Completed recovery actions: {report['action_count']}",
        f"- Failed recovery actions: {report['failed_action_count']}",
        f"- Remaining user-action items: {report['remaining_user_action_count']}",
        "",
        "## Recovery Actions",
    ]
    if report["actions"]:
        for action in report["actions"]:
            lines.append(f"- {action['id']}: {action['status']} ({action['reason']})")
    else:
        lines.append("- No recovery actions were needed.")
    lines.extend(["", "## Deferred User Actions"])
    if report["skipped_user_actions"]:
        for item in report["skipped_user_actions"]:
            lines.append(f"- {item['id']}: {item['reason']}")
    else:
        lines.append("- No deferred user actions.")
    review = report["codex_review"]
    lines.extend(["", "## Codex Review"])
    lines.append(f"- Needed: {'yes' if review.get('needed') else 'no'}")
    lines.append(f"- Opened: {'yes' if review.get('opened') else 'no'}")
    lines.append(f"- Reason: {review.get('reason', '')}")
    if review.get("candidate_ids"):
        lines.append(f"- Candidate IDs: {', '.join(review['candidate_ids'])}")
    return "\n".join(lines).rstrip() + "\n"


def write_recovery_report(report: dict[str, Any], root: Path = ROOT) -> None:
    json_path = recovery_json_path(dt.date.fromisoformat(report["checked_date"]), root)
    md_path = root / "journal" / "recovery_status.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date to recover, YYYY-MM-DD. Defaults to config-timezone today.")
    parser.add_argument("--json-only", action="store_true", help="Print recovery JSON to stdout.")
    parser.add_argument("--no-write", action="store_true", help="Dry-run recovery without writing files or running commands.")
    parser.add_argument("--no-open-codex", action="store_true", help="Do not open Codex Review even when user action remains.")
    parser.add_argument("--from-run-daily", action="store_true", help="Mark this run as called from run_daily.ps1; implies no interactive Codex launch.")
    args = parser.parse_args()
    config = load_config()
    try:
        day = parse_date(args.date, config)
    except ValueError:
        parser.error(f"--date must be YYYY-MM-DD, got: {args.date}")
    report = recover(
        day,
        config,
        no_write=args.no_write,
        open_codex=not args.no_open_codex,
        from_run_daily=args.from_run_daily,
    )
    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Auto recovery complete for {report['checked_date']}.")
        print(f"Completed actions: {report['action_count']}; failed: {report['failed_action_count']}; user actions: {report['remaining_user_action_count']}")


if __name__ == "__main__":
    main()
