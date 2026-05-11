from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import activity_db
import capture_controls
import collect_daily
import notion_sync
import project_review
import retention_cleanup
import tray_app


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"
STATUS_MD_PATH = ROOT / "journal" / "system_status.md"
STATUS_JSON_PATH = ROOT / "journal" / "raw" / "system_status.json"
CHROME_EXTENSION_PATH = ROOT / "browser_extension" / "chatgpt-live-capture"

STATUS_ORDER = {"OK": 0, "Skipped": 1, "Warning": 2, "Action Needed": 3}
EXPECTED_TASKS = {
    "Activity Journal Daily": ["scripts\\run_daily.ps1", "-NonInteractive", "-RefreshDrafts", "-CatchUpMissed"],
    "Activity Journal Weekly": ["scripts\\run_weekly.ps1"],
    "Activity Journal Watcher": ["scripts\\activity_watch.py", "--include-accessibility-text"],
    "Activity Journal ChatGPT Receiver": ["scripts\\chatgpt_live_server.py"],
    "Activity Journal Codex Review": ["scripts\\open_codex_review.ps1"],
}
EXPECTED_TASK_SETTINGS = {
    "Activity Journal Daily": {
        "StartWhenAvailable": True,
        "WakeToRun": True,
        "DisallowStartIfOnBatteries": False,
        "StopIfGoingOnBatteries": False,
    }
}


def configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def startup_launcher_path(name: str) -> Path | None:
    app_data = os.environ.get("APPDATA")
    if not app_data:
        return None
    return Path(app_data) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{name}.vbs"


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def configured_timezone(config: dict[str, Any]) -> ZoneInfo | None:
    timezone = config.get("timezone")
    if not timezone:
        return None
    try:
        return ZoneInfo(str(timezone))
    except ZoneInfoNotFoundError:
        return None


def now(config: dict[str, Any]) -> dt.datetime:
    timezone = configured_timezone(config)
    if timezone:
        return dt.datetime.now(timezone)
    return dt.datetime.now().astimezone()


def parse_date(value: str | None, config: dict[str, Any]) -> dt.date:
    if value:
        return dt.date.fromisoformat(value)
    return now(config).date()


def iso_week(day: dt.date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def rel(path: Path, root: Path = ROOT) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def file_info(path: Path, root: Path = ROOT) -> dict[str, Any]:
    exists = path.exists()
    info: dict[str, Any] = {"path": rel(path, root), "exists": exists}
    if exists:
        try:
            info["modified_at"] = dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        except OSError as exc:
            info["error"] = str(exc)
    return info


def section_status(items: list[dict[str, Any]]) -> str:
    status = "OK"
    for item in items:
        status = worst_status(status, item.get("status", "OK"))
    return status


def worst_status(left: str, right: str) -> str:
    return left if STATUS_ORDER.get(left, 0) >= STATUS_ORDER.get(right, 0) else right


def check_daily_collection(day: dt.date, root: Path) -> dict[str, Any]:
    date = day.isoformat()
    files = {
        "raw": file_info(root / "journal" / "raw" / f"{date}.json", root),
        "daily": file_info(root / "journal" / "daily" / f"{date}.md", root),
        "questions": file_info(root / "journal" / "questions" / f"{date}.md", root),
    }
    missing = [name for name, info in files.items() if not info["exists"]]
    status = "OK" if not missing else "Action Needed"
    actions = []
    if missing:
        actions.append(f"powershell -ExecutionPolicy Bypass -File scripts\\run_daily.ps1 -Date {date} -NonInteractive")
    return {"status": status, "files": files, "missing": missing, "recommended_actions": actions}


def strip_fenced_code_blocks(markdown: str) -> str:
    lines: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(line)
    return "\n".join(lines)


def parse_question_blocks(markdown: str) -> list[dict[str, Any]]:
    text = strip_fenced_code_blocks(markdown)
    starts = [match.start() for match in re.finditer(r"^## Q:\s*(.+?)\s*$", text, re.M)]
    blocks: list[dict[str, Any]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        block_text = text[start:end]
        header = re.search(r"^## Q:\s*(.+?)\s*$", block_text, re.M)
        question_id = header.group(1).strip() if header else "unknown"
        answer_match = re.search(r"^Answer:\s*$", block_text, re.M)
        answer = ""
        if answer_match:
            answer_text = block_text[answer_match.end() :]
            answer_lines = []
            for line in answer_text.splitlines():
                if line.startswith("## "):
                    break
                cleaned = line.strip()
                if cleaned and cleaned not in {"-", "```", "<답변>", "<answer>"}:
                    answer_lines.append(cleaned)
            answer = "\n".join(answer_lines).strip()
        blocks.append({"id": question_id, "answered": bool(answer), "answer_present": bool(answer)})
    return blocks


def check_codex_review(day: dt.date, root: Path) -> dict[str, Any]:
    path = root / "journal" / "questions" / f"{day.isoformat()}.md"
    if not path.exists():
        return {
            "status": "Action Needed",
            "questions_file": file_info(path, root),
            "total_questions": 0,
            "pending_questions": 0,
            "answered_questions": 0,
            "recommended_actions": [f"powershell -ExecutionPolicy Bypass -File scripts\\run_daily.ps1 -Date {day.isoformat()}"],
        }
    markdown = path.read_text(encoding="utf-8", errors="replace")
    questions = parse_question_blocks(markdown)
    pending = [question for question in questions if not question["answered"]]
    status = "OK" if not pending else "Warning"
    actions = []
    if pending:
        actions.append(f"powershell -ExecutionPolicy Bypass -File scripts\\open_codex_review.ps1 -Date {day.isoformat()}")
    return {
        "status": status,
        "questions_file": file_info(path, root),
        "total_questions": len(questions),
        "pending_questions": len(pending),
        "answered_questions": len(questions) - len(pending),
        "pending_ids": [question["id"] for question in pending],
        "recommended_actions": actions,
    }


def meaningful_question_candidate(candidate: dict[str, Any]) -> bool:
    severity = str(candidate.get("severity", "")).lower()
    confidence = str(candidate.get("confidence", "")).lower()
    return severity in {"high", "medium"} and confidence in {"high", "medium"}


def check_question_quality(day: dt.date, root: Path) -> dict[str, Any]:
    date = day.isoformat()
    path = root / "journal" / "raw" / f"question_candidates_{date}.json"
    questions_path = root / "journal" / "questions" / f"{date}.md"
    pending_questions = 0
    answered_ids: set[str] = set()
    if questions_path.exists():
        markdown = questions_path.read_text(encoding="utf-8", errors="replace")
        questions = parse_question_blocks(markdown)
        pending_questions = len([question for question in questions if not question["answered"]])
        answered_ids = {question["id"] for question in questions if question["answered"]}
    if not path.exists():
        return {
            "status": "OK",
            "generated": False,
            "candidate_file": file_info(path, root),
            "candidate_count": 0,
            "unresolved_candidate_count": 0,
            "pending_questions": pending_questions,
            "recommended_actions": [],
        }
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "status": "Warning",
            "generated": True,
            "candidate_file": file_info(path, root),
            "error": "invalid JSON",
            "candidate_count": 0,
            "unresolved_candidate_count": 0,
            "pending_questions": pending_questions,
            "recommended_actions": [f"python scripts\\question_quality.py --date {date}"],
        }
    if not isinstance(report, dict) or report.get("checked_date") != date:
        return {
            "status": "Warning",
            "generated": True,
            "candidate_file": file_info(path, root),
            "error": "stale or invalid candidate report",
            "candidate_count": 0,
            "unresolved_candidate_count": 0,
            "pending_questions": pending_questions,
            "recommended_actions": [f"python scripts\\question_quality.py --date {date}"],
        }
    candidates = report.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    unresolved = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and candidate.get("id") not in answered_ids
        and meaningful_question_candidate(candidate)
    ]
    actions: list[str] = []
    status = "OK"
    if unresolved:
        status = "Warning"
        actions.append(f"powershell -ExecutionPolicy Bypass -File scripts\\open_codex_review.ps1 -Date {date}")
    return {
        "status": status,
        "generated": True,
        "candidate_file": file_info(path, root),
        "candidate_count": len(candidates),
        "unresolved_candidate_count": len(unresolved),
        "pending_questions": pending_questions,
        "unresolved_ids": [str(candidate.get("id")) for candidate in unresolved],
        "recommended_actions": actions,
    }


def check_weekly_review(day: dt.date, root: Path) -> dict[str, Any]:
    week = iso_week(day)
    path = root / "journal" / "weekly" / f"{week}.md"
    status = "OK" if path.exists() else "Warning"
    actions = [] if path.exists() else [f"powershell -ExecutionPolicy Bypass -File scripts\\run_weekly.ps1 -Date {day.isoformat()}"]
    return {"status": status, "week": week, "weekly_file": file_info(path, root), "recommended_actions": actions}


def check_project_review(day: dt.date, root: Path) -> dict[str, Any]:
    date = day.isoformat()
    rollup_path = root / "journal" / "raw" / f"project_rollup_{date}.json"
    metadata_path = root / "journal" / "projects" / "project_metadata.json"
    project_dir = root / "journal" / "projects"
    if not rollup_path.exists():
        return {
            "status": "Warning",
            "generated": False,
            "rollup_file": file_info(rollup_path, root),
            "metadata_file": file_info(metadata_path, root),
            "project_count": 0,
            "missing_goal_count": 0,
            "recommended_actions": [f"python scripts\\project_review.py --date {date}"],
        }
    try:
        report = json.loads(rollup_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "status": "Warning",
            "generated": True,
            "rollup_file": file_info(rollup_path, root),
            "metadata_file": file_info(metadata_path, root),
            "error": "invalid JSON",
            "project_count": 0,
            "missing_goal_count": 0,
            "recommended_actions": [f"python scripts\\project_review.py --date {date}"],
        }
    if not isinstance(report, dict) or report.get("checked_date") != date:
        return {
            "status": "Warning",
            "generated": True,
            "rollup_file": file_info(rollup_path, root),
            "metadata_file": file_info(metadata_path, root),
            "error": "stale or invalid project rollup",
            "project_count": 0,
            "missing_goal_count": 0,
            "recommended_actions": [f"python scripts\\project_review.py --date {date}"],
        }
    projects = report.get("projects", {})
    if not isinstance(projects, dict):
        projects = {}
    missing_goal_projects = report.get("missing_goal_projects", [])
    if not isinstance(missing_goal_projects, list):
        missing_goal_projects = []
    missing_goal_count = len(missing_goal_projects)
    actions: list[str] = []
    status = "OK"
    if missing_goal_count:
        status = "Warning"
        actions.extend(
            [
                f"python scripts\\question_quality.py --date {date}",
                f"powershell -ExecutionPolicy Bypass -File scripts\\open_codex_review.ps1 -Date {date}",
            ]
        )
    return {
        "status": status,
        "generated": True,
        "rollup_file": file_info(rollup_path, root),
        "metadata_file": file_info(metadata_path, root),
        "project_dir": file_info(project_dir, root),
        "project_count": len(projects),
        "missing_goal_count": missing_goal_count,
        "missing_goal_projects": [str(item) for item in missing_goal_projects],
        "recommended_actions": actions,
    }


def check_recovery(day: dt.date, root: Path) -> dict[str, Any]:
    date = day.isoformat()
    path = root / "journal" / "raw" / f"recovery_{date}.json"
    if not path.exists():
        return {
            "status": "OK",
            "generated": False,
            "recovery_file": file_info(path, root),
            "action_count": 0,
            "failed_action_count": 0,
            "remaining_user_action_count": 0,
            "recommended_actions": [],
        }
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "status": "Warning",
            "generated": True,
            "recovery_file": file_info(path, root),
            "error": "invalid JSON",
            "action_count": 0,
            "failed_action_count": 0,
            "remaining_user_action_count": 0,
            "recommended_actions": [f"python scripts\\auto_recover.py --date {date} --no-open-codex"],
        }
    if not isinstance(report, dict) or report.get("checked_date") != date:
        return {
            "status": "Warning",
            "generated": True,
            "recovery_file": file_info(path, root),
            "error": "stale or invalid recovery report",
            "action_count": 0,
            "failed_action_count": 0,
            "remaining_user_action_count": 0,
            "recommended_actions": [f"python scripts\\auto_recover.py --date {date} --no-open-codex"],
        }
    failed = int(report.get("failed_action_count", 0) or 0)
    status = "Warning" if failed else "OK"
    actions = [f"python scripts\\auto_recover.py --date {date} --no-open-codex"] if failed else []
    return {
        "status": status,
        "generated": True,
        "recovery_file": file_info(path, root),
        "action_count": int(report.get("action_count", 0) or 0),
        "failed_action_count": failed,
        "remaining_user_action_count": int(report.get("remaining_user_action_count", 0) or 0),
        "codex_review_needed": bool(report.get("codex_review", {}).get("needed")),
        "codex_review_opened": bool(report.get("codex_review", {}).get("opened")),
        "recommended_actions": actions,
    }


def token_status() -> str:
    if os.environ.get("NOTION_TOKEN"):
        return "set"
    if sys.platform != "win32":
        return "missing"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _value_type = winreg.QueryValueEx(key, "NOTION_TOKEN")
            return "set" if value else "missing"
    except OSError:
        return "missing"


def validate_notion_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "valid": False, "error": "missing"}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"exists": True, "valid": False, "error": "invalid JSON"}
    valid = isinstance(state, dict) and all(isinstance(key, str) and isinstance(value, str) for key, value in state.items())
    keys = sorted(state.keys()) if valid else []
    return {"exists": True, "valid": valid, "entries": len(state) if isinstance(state, dict) else 0, "keys": keys, "error": None if valid else "invalid shape"}


def check_notion(root: Path, config: dict[str, Any], day: dt.date | None = None) -> dict[str, Any]:
    notion_config = config.get("notion", {})
    policy = notion_sync.sync_policy(config)
    enabled = bool(notion_config.get("enabled", False))
    parent_page_id_set = bool(notion_config.get("parent_page_id"))
    token = token_status()
    state = validate_notion_state(root / "journal" / "raw" / "notion_pages.json")
    status = "OK"
    actions: list[str] = []
    if not enabled:
        status = "Skipped"
    elif not parent_page_id_set:
        status = "Action Needed"
        actions.append("Add notion.parent_page_id to config\\activity-journal.json")
    elif token == "missing":
        status = "Warning"
        actions.append("powershell -ExecutionPolicy Bypass -File scripts\\set_notion_token.ps1")
    if enabled and state["exists"] and not state["valid"]:
        status = worst_status(status, "Action Needed")
        actions.append("Inspect journal\\raw\\notion_pages.json before the next Notion sync")
    elif enabled and not state["exists"] and policy["mode"] != "delayed_final":
        status = worst_status(status, "Warning")
    if enabled and day and policy["mode"] == "delayed_final":
        return check_notion_delayed(day, root, config, state, status, actions, parent_page_id_set, token)
    daily_synced = None
    daily_hash_matches = None
    weekly_synced = None
    weekly_hash_matches = None
    if enabled and day and state.get("valid"):
        keys = set(state.get("keys", []))
        state_values = load_notion_state_values(root / "journal" / "raw" / "notion_pages.json")
        daily_path = root / "journal" / "daily" / f"{day.isoformat()}.md"
        if daily_path.exists():
            daily_key = f"dbdaily:{day.isoformat()}" if f"dbdaily:{day.isoformat()}" in keys else f"daily:{day.isoformat()}"
            daily_synced = daily_key in keys
            if daily_synced:
                daily_hash_matches = notion_hash_matches(state_values, daily_key, daily_path, config)
            if not daily_synced and token != "missing" and parent_page_id_set:
                status = worst_status(status, "Warning")
                actions.append(f"python scripts\\notion_sync.py --date {day.isoformat()}")
            elif daily_hash_matches is False and token != "missing" and parent_page_id_set:
                status = worst_status(status, "Warning")
                actions.append(f"python scripts\\notion_sync.py --date {day.isoformat()}")
        week = iso_week(day)
        weekly_path = root / "journal" / "weekly" / f"{week}.md"
        if weekly_path.exists():
            weekly_key = f"dbweekly:{week}" if f"dbweekly:{week}" in keys else f"weekly:{week}"
            weekly_synced = weekly_key in keys
            if weekly_synced:
                weekly_hash_matches = notion_hash_matches(state_values, weekly_key, weekly_path, config)
            if not weekly_synced and token != "missing" and parent_page_id_set:
                status = worst_status(status, "Warning")
                actions.append(f"powershell -ExecutionPolicy Bypass -File scripts\\run_weekly.ps1 -Date {day.isoformat()}")
            elif weekly_hash_matches is False and token != "missing" and parent_page_id_set:
                status = worst_status(status, "Warning")
                actions.append(f"powershell -ExecutionPolicy Bypass -File scripts\\run_weekly.ps1 -Date {day.isoformat()}")
    return {
        "status": status,
        "enabled": enabled,
        "parent_page_id": "set" if parent_page_id_set else "missing",
        "token": token,
        "state_file": state,
        "daily_synced": daily_synced,
        "daily_hash_matches": daily_hash_matches,
        "weekly_synced": weekly_synced,
        "weekly_hash_matches": weekly_hash_matches,
        "recommended_actions": actions,
    }


def check_notion_delayed(
    day: dt.date,
    root: Path,
    config: dict[str, Any],
    state_info: dict[str, Any],
    base_status: str,
    base_actions: list[str],
    parent_page_id_set: bool,
    token: str,
) -> dict[str, Any]:
    policy = notion_sync.sync_policy(config)
    state_values = load_notion_state_values(root / "journal" / "raw" / "notion_pages.json")
    today = now(config).date()
    cutoff = today - dt.timedelta(days=int(policy["finalize_after_days"]))
    pending_finalization = day > cutoff
    date = day.isoformat()
    keys = set(state_values.keys())
    daily_key = f"dbdaily:{date}" if f"dbdaily:{date}" in keys else f"daily:{date}"
    daily_synced = daily_key in keys
    daily_finalized = notion_sync.finalized_key(daily_key) in state_values
    daily_hash_matches = notion_hash_matches(state_values, daily_key, root / "journal" / "daily" / f"{date}.md", config) if daily_synced else None
    failed_keys = sorted(key for key in keys if key.startswith("failed:"))
    due_dates = [
        due.isoformat()
        for due in notion_sync.eligible_daily_dates(today, policy, root)
        if notion_sync.daily_needs_final_sync(due, state_values)
    ]
    actions = list(base_actions)
    status = base_status
    if failed_keys:
        status = worst_status(status, "Warning")
        actions.append(f"python scripts\\notion_sync.py --finalize-due --through-date {today.isoformat()}")
    if not pending_finalization and (not daily_synced or not daily_finalized):
        status = worst_status(status, "Warning")
        if parent_page_id_set and token != "missing":
            actions.append(f"python scripts\\notion_sync.py --date {date}")
    elif daily_finalized and daily_hash_matches is False:
        status = worst_status(status, "Warning")
        if parent_page_id_set and token != "missing":
            actions.append(f"python scripts\\notion_sync.py --date {date} --force")
    return {
        "status": status,
        "enabled": True,
        "sync_policy": "delayed_final",
        "finalize_after_days": policy["finalize_after_days"],
        "pending_finalization": pending_finalization,
        "finalization_due_date": cutoff.isoformat(),
        "parent_page_id": "set" if parent_page_id_set else "missing",
        "token": token,
        "state_file": state_info,
        "daily_synced": daily_synced,
        "daily_finalized": daily_finalized,
        "daily_hash_matches": daily_hash_matches,
        "failed_retry_count": len(failed_keys),
        "failed_retry_keys": failed_keys,
        "next_due_dates": due_dates[:10],
        "recommended_actions": unique_actions(actions),
    }


def load_notion_state_values(path: Path) -> dict[str, str]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(state, dict):
        return {}
    return {str(key): str(value) for key, value in state.items() if isinstance(key, str) and isinstance(value, str)}


def notion_hash_matches(state: dict[str, str], key: str, path: Path, config: dict[str, Any]) -> bool | None:
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError:
        return None
    expected = notion_sync.notion_content_hash(markdown, notion_sync.notion_body_mode(config))
    return state.get(notion_sync.content_hash_key(key)) == expected


def scheduler_tasks() -> tuple[list[dict[str, Any]], str | None]:
    if sys.platform != "win32":
        return [], "Scheduler check skipped: not Windows."
    command = """
$names = @('Activity Journal Daily','Activity Journal Weekly','Activity Journal Watcher','Activity Journal ChatGPT Receiver','Activity Journal Codex Review')
$tasks = foreach ($name in $names) {
  Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
}
$tasks | Where-Object { $_ } | ForEach-Object {
  [PSCustomObject]@{
    TaskName=$_.TaskName
    State=[string]$_.State
    Execute=$_.Actions.Execute
    Arguments=$_.Actions.Arguments
    WorkingDirectory=$_.Actions.WorkingDirectory
    StartWhenAvailable=$_.Settings.StartWhenAvailable
    WakeToRun=$_.Settings.WakeToRun
    DisallowStartIfOnBatteries=$_.Settings.DisallowStartIfOnBatteries
    StopIfGoingOnBatteries=$_.Settings.StopIfGoingOnBatteries
  }
} | ConvertTo-Json -Depth 5
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return [], result.stderr.strip() or f"Get-ScheduledTask failed with exit code {result.returncode}."
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], "Get-ScheduledTask returned non-JSON output."
    if isinstance(parsed, dict):
        return [parsed], None
    if isinstance(parsed, list):
        return parsed, None
    return [], "Get-ScheduledTask returned an unexpected JSON shape."


def evaluate_scheduler_tasks(tasks: list[dict[str, Any]], root: Path) -> dict[str, Any]:
    by_name = {str(task.get("TaskName")): task for task in tasks}
    task_results: dict[str, Any] = {}
    status = "OK"
    actions: list[str] = []
    for name, expected_parts in EXPECTED_TASKS.items():
        task = by_name.get(name)
        if not task:
            if name in {"Activity Journal Watcher", "Activity Journal ChatGPT Receiver"}:
                startup_path = startup_launcher_path(name)
                if startup_path and startup_path.exists():
                    startup_status = evaluate_startup_launcher(startup_path, expected_parts)
                    task_results[name] = startup_status
                    if startup_status["status"] != "OK":
                        status = worst_status(status, startup_status["status"])
                    continue
            task_results[name] = {"status": "Action Needed", "error": "missing"}
            status = worst_status(status, "Action Needed")
            continue
        arguments = str(task.get("Arguments", ""))
        normalized = arguments.lower().replace("/", "\\")
        missing_parts = [part for part in expected_parts if part.lower() not in normalized]
        expected_settings = EXPECTED_TASK_SETTINGS.get(name, {"StartWhenAvailable": True})
        invalid_settings = {
            key: {"expected": expected, "actual": task.get(key)}
            for key, expected in expected_settings.items()
            if task.get(key) is not expected
        }
        task_status = "OK" if not missing_parts and not invalid_settings else "Action Needed"
        if missing_parts:
            task_results[name] = {"status": task_status, "error": f"missing expected arguments: {missing_parts}", "task": safe_task(task, root)}
        elif invalid_settings:
            task_results[name] = {"status": task_status, "error": f"unexpected task settings: {invalid_settings}", "task": safe_task(task, root)}
        else:
            task_results[name] = {"status": task_status, "task": safe_task(task, root)}
        status = worst_status(status, task_status)
    if status != "OK":
        actions.append("powershell -ExecutionPolicy Bypass -File scripts\\setup_task.ps1")
    return {"status": status, "tasks": task_results, "recommended_actions": actions}


def evaluate_startup_launcher(path: Path, expected_parts: list[str]) -> dict[str, Any]:
    try:
        content = path.read_text(encoding="ascii", errors="replace")
    except OSError as exc:
        return {"status": "Action Needed", "startup_launcher": str(path), "error": str(exc)}
    normalized = content.lower().replace("/", "\\")
    missing_parts = [part for part in expected_parts if part.lower() not in normalized]
    if missing_parts:
        return {
            "status": "Action Needed",
            "startup_launcher": str(path),
            "error": f"missing expected arguments: {missing_parts}",
        }
    return {"status": "OK", "startup_launcher": str(path)}


def safe_task(task: dict[str, Any], root: Path) -> dict[str, Any]:
    return {
        "state": task.get("State"),
        "execute": task.get("Execute"),
        "arguments": str(task.get("Arguments", "")).replace(str(root), "<repo>"),
        "working_directory": str(task.get("WorkingDirectory", "")).replace(str(root), "<repo>"),
        "start_when_available": task.get("StartWhenAvailable"),
        "wake_to_run": task.get("WakeToRun"),
        "disallow_start_if_on_batteries": task.get("DisallowStartIfOnBatteries"),
        "stop_if_going_on_batteries": task.get("StopIfGoingOnBatteries"),
    }


def check_scheduler(root: Path) -> dict[str, Any]:
    tasks, error = scheduler_tasks()
    if error:
        return {"status": "Warning", "error": error, "recommended_actions": ["powershell -ExecutionPolicy Bypass -File scripts\\setup_task.ps1"]}
    return evaluate_scheduler_tasks(tasks, root)


def probe_directory(path: Path, root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": rel(path, root), "exists": path.exists(), "readable": False, "writable": False}
    if not path.exists():
        result["status"] = "Action Needed"
        result["error"] = "missing"
        return result
    try:
        with os.scandir(path):
            result["readable"] = True
    except OSError as exc:
        result["error"] = str(exc)
    probe = path / "_project_health_write_test.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        result["writable"] = True
    except OSError as exc:
        result["write_error"] = str(exc)
        try:
            if probe.exists():
                probe.unlink()
        except OSError:
            pass
    result["status"] = "OK" if result["readable"] and result["writable"] else "Action Needed"
    return result


def scan_access_denied(path: Path, root: Path, depth: int = 0, max_depth: int = 2) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    try:
        entries = list(os.scandir(path))
    except PermissionError as exc:
        return [{"path": rel(path, root), "error": str(exc)}]
    except OSError as exc:
        return [{"path": rel(path, root), "error": str(exc)}]
    if depth >= max_depth:
        return issues
    for entry in entries:
        entry_path = Path(entry.path)
        if should_skip_access_scan(entry_path, root):
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError as exc:
            issues.append({"path": rel(entry_path, root), "error": str(exc)})
            continue
        if is_dir:
            issues.extend(scan_access_denied(entry_path, root, depth + 1, max_depth))
    return issues


def should_skip_access_scan(path: Path, root: Path) -> bool:
    try:
        rel_path = path.relative_to(root)
    except ValueError:
        return False
    parts = rel_path.parts
    return len(parts) >= 3 and parts[0] == "journal" and parts[1] == "raw" and parts[2].startswith(("tmp", "_tmp"))


def check_file_access(root: Path) -> dict[str, Any]:
    paths = [
        root / "journal",
        root / "journal" / "raw",
        root / "journal" / "daily",
        root / "journal" / "questions",
        root / "journal" / "weekly",
        root / "journal" / "projects",
    ]
    probes = [probe_directory(path, root) for path in paths]
    denied = scan_access_denied(root / "journal", root) if (root / "journal").exists() else []
    status = section_status(probes)
    if denied:
        status = worst_status(status, "Warning")
    actions = []
    if denied:
        actions.append("Review the access denied paths listed in journal\\system_status.md")
    if any(probe["status"] == "Action Needed" for probe in probes):
        actions.append("Run daily collection once to recreate missing journal directories")
    return {"status": status, "directories": probes, "access_denied": denied, "recommended_actions": actions}


def external_dir_status(path: Path, root: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"path": rel(path, root), "exists": path.exists(), "readable": False}
    if not path.exists():
        info["status"] = "Warning"
        return info
    try:
        with os.scandir(path):
            info["readable"] = True
        info["status"] = "OK"
    except OSError as exc:
        info["status"] = "Warning"
        info["error"] = str(exc)
    return info


def check_external_inputs(day: dt.date, root: Path, config: dict[str, Any]) -> dict[str, Any]:
    external = config.get("external_inputs", {})
    if not external.get("enabled", False):
        return {"status": "Skipped", "enabled": False, "recommended_actions": []}
    capture_status = capture_controls.status(config)
    if not capture_status["active"]:
        return {
            "status": "Skipped",
            "enabled": True,
            "capture_active": False,
            "capture_reason": capture_status["reason"],
            "recommended_actions": ["Open Activity Journal Settings and resume capture when ready."],
        }
    inbox_dir = collect_daily.configured_path(str(external.get("inbox_dir", "inbox")), root)
    chatgpt_dir = collect_daily.configured_path(str(external.get("chatgpt_export_dir", "imports/chatgpt")), root)
    inbox_status = external_dir_status(inbox_dir, root)
    chatgpt_status = external_dir_status(chatgpt_dir, root)
    since, until = collect_daily.collection_window(day, config)
    collected = collect_daily.collect_external_inputs(config, day, since, until, root)
    inbox_count = len(collected.get("inbox", []))
    chatgpt_count = len(collected.get("chatgpt", []))
    chatgpt_live_count = len(collected.get("chatgpt_live", []))
    browser_history_count = len(collected.get("browser_history", []))
    recent_file_count = len(collected.get("recent_files", []))
    activity_watch_count = len(collected.get("activity_watch", []))
    activity_text_count = len([item for item in collected.get("activity_watch", []) if int(item.get("text_capture_count") or 0) > 0])
    accessibility_text_enabled = bool(settings_value(external, ["activity_watch", "include_accessibility_text"], False))
    accessibility_dependency = "available" if importlib.util.find_spec("uiautomation") else "missing"
    status = "OK"
    actions: list[str] = []
    if inbox_status["status"] != "OK" or chatgpt_status["status"] != "OK":
        status = "Warning"
        actions.append("Create inbox and imports\\chatgpt directories if you want external input collection.")
    if chatgpt_count == 0 and browser_history_count == 0:
        status = worst_status(status, "Warning")
        actions.append("powershell -ExecutionPolicy Bypass -File scripts\\open_chatgpt_export.ps1")
    if collected.get("warnings"):
        status = worst_status(status, "Warning")
    if accessibility_text_enabled and accessibility_dependency == "missing":
        status = worst_status(status, "Warning")
        actions.append("python -m pip install --user uiautomation")
    return {
        "status": status,
        "enabled": True,
        "capture_active": True,
        "inbox_dir": inbox_status,
        "chatgpt_export_dir": chatgpt_status,
        "inbox_count": inbox_count,
        "chatgpt_count": chatgpt_count,
        "chatgpt_live_count": chatgpt_live_count,
        "browser_history_count": browser_history_count,
        "recent_file_count": recent_file_count,
        "activity_watch_count": activity_watch_count,
        "activity_text_count": activity_text_count,
        "accessibility_text_enabled": accessibility_text_enabled,
        "accessibility_dependency": accessibility_dependency,
        "warnings": collected.get("warnings", []),
        "recommended_actions": actions,
    }


def check_chrome_extension(day: dt.date, root: Path, config: dict[str, Any]) -> dict[str, Any]:
    external = config.get("external_inputs", {})
    settings = external.get("chatgpt_live", {})
    if not isinstance(settings, dict) or not settings.get("enabled", False):
        return {"status": "Skipped", "enabled": False, "recommended_actions": []}
    capture_status = capture_controls.status(config)
    if not capture_status["active"]:
        return {
            "status": "Skipped",
            "enabled": True,
            "capture_active": False,
            "capture_reason": capture_status["reason"],
            "recommended_actions": [],
        }

    manifest = check_chrome_extension_manifest(root)
    host = str(settings.get("server_host", "127.0.0.1"))
    port = int(settings.get("server_port", 8765))
    receiver = probe_chatgpt_receiver(host, port)
    log_path = collect_daily.configured_path(str(settings.get("log_path", "journal/raw/chatgpt_live.jsonl")), root)
    since, until = collect_daily.collection_window(day, config)
    captures, warnings = collect_daily.collect_chatgpt_live(config, since, until, root)
    recent_capture_count = len(captures)

    status = section_status([manifest, receiver])
    actions: list[str] = []
    if manifest["status"] != "OK":
        actions.append("powershell -ExecutionPolicy Bypass -File scripts\\open_chatgpt_capture_extension.ps1")
    if receiver["status"] != "OK":
        actions.append(f"pythonw scripts\\chatgpt_live_server.py --host {host} --port {port}")
    if recent_capture_count == 0:
        status = worst_status(status, "Warning")
        actions.append("Open ChatGPT or Gemini once, then confirm the Chrome extension is enabled.")
    if warnings:
        status = worst_status(status, "Warning")
    return {
        "status": status,
        "enabled": True,
        "manifest": manifest,
        "receiver": receiver,
        "log_file": file_info(log_path, root),
        "recent_capture_count": recent_capture_count,
        "warnings": warnings,
        "recommended_actions": unique_actions(actions),
    }


def check_chrome_extension_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "browser_extension" / "chatgpt-live-capture" / "manifest.json"
    info = file_info(manifest_path, root)
    if not manifest_path.exists():
        return {"status": "Action Needed", "file": info, "error": "missing"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "Action Needed", "file": info, "error": str(exc)}
    required_hosts = {"http://127.0.0.1:8765/*", "https://chatgpt.com/*", "https://gemini.google.com/*"}
    host_permissions = set(str(item) for item in manifest.get("host_permissions", []) if isinstance(item, str))
    content_matches: set[str] = set()
    content_scripts = manifest.get("content_scripts", [])
    if isinstance(content_scripts, list):
        for script in content_scripts:
            if isinstance(script, dict):
                content_matches.update(str(item) for item in script.get("matches", []) if isinstance(item, str))
    service_worker = manifest.get("background", {}).get("service_worker") if isinstance(manifest.get("background"), dict) else None
    missing_hosts = sorted(required_hosts - host_permissions)
    missing_matches = sorted({"https://chatgpt.com/*", "https://gemini.google.com/*"} - content_matches)
    errors: list[str] = []
    if missing_hosts:
        errors.append(f"missing host permissions: {missing_hosts}")
    if missing_matches:
        errors.append(f"missing content script matches: {missing_matches}")
    if service_worker != "background.js":
        errors.append("missing background.js service worker")
    return {
        "status": "OK" if not errors else "Action Needed",
        "file": info,
        "version": manifest.get("version"),
        "missing_hosts": missing_hosts,
        "missing_matches": missing_matches,
        "service_worker": service_worker,
        "errors": errors,
    }


def probe_chatgpt_receiver(host: str, port: int) -> dict[str, Any]:
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            body = response.read(200).decode("utf-8", errors="replace")
            ok = response.status == 200 and '"ok":true' in body.replace(" ", "")
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return {"status": "Warning", "url": url, "ok": False, "error": str(exc)}
    return {"status": "OK" if ok else "Warning", "url": url, "ok": ok}


def check_retention(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    try:
        report = retention_cleanup.cleanup_report(config, root=root, dry_run=True)
    except (OSError, ValueError, TypeError) as exc:
        return {
            "status": "Action Needed",
            "enabled": False,
            "error": str(exc),
            "recommended_actions": ["Inspect retention settings in config\\activity-journal.json"],
        }
    return {
        "status": report["status"],
        "enabled": report.get("enabled", False),
        "keep_recent_days": report.get("keep_recent_days"),
        "cutoff_date": report.get("cutoff_date"),
        "archive_dir": report.get("archive_dir"),
        "delete_archives": report.get("delete_archives"),
        "max_raw_mb": report.get("max_raw_mb"),
        "raw_size_mb": report.get("raw_size_mb"),
        "archived_line_count": report.get("archived_line_count", 0),
        "sources": report.get("sources", []),
        "database": report.get("database", {}),
        "recommended_actions": report.get("recommended_actions", []),
    }


def check_sqlite_database(day: dt.date, root: Path, config: dict[str, Any]) -> dict[str, Any]:
    try:
        return activity_db.inspect_database(config, root=root, day=day)
    except (OSError, ValueError, TypeError) as exc:
        return {
            "status": "Action Needed",
            "enabled": False,
            "error": str(exc),
            "recommended_actions": ["Inspect database settings in config\\activity-journal.json"],
        }


def check_tray(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    capture_controls.ensure_config_shape(config)
    settings = config.get("tray", {})
    enabled = bool(settings.get("enabled", True)) if isinstance(settings, dict) else True
    show_notifications = bool(settings.get("show_notifications", True)) if isinstance(settings, dict) else True
    dependencies = tray_app.dependency_status()
    actions: list[str] = []
    status = "OK" if enabled else "Skipped"

    startup_path = startup_launcher_path("Activity Journal Tray")
    if startup_path is None:
        startup: dict[str, Any] = {"exists": False, "error": "APPDATA is not available"}
        if enabled:
            status = worst_status(status, "Warning")
    elif startup_path.exists():
        startup = evaluate_startup_launcher(startup_path, ["scripts\\tray_app.py"])
        startup["exists"] = True
        if enabled:
            status = worst_status(status, startup.get("status", "Warning"))
    else:
        startup = {"status": "Warning", "exists": False, "startup_launcher": str(startup_path), "error": "missing"}
        if enabled:
            status = worst_status(status, "Warning")
            actions.append("powershell -ExecutionPolicy Bypass -File scripts\\install_local.ps1")

    if enabled and dependencies["status"] != "OK":
        status = worst_status(status, "Warning")
        actions.append(dependencies["install_command"])

    return {
        "status": status,
        "enabled": enabled,
        "show_notifications": show_notifications,
        "dependencies": dependencies["dependencies"],
        "startup_launcher": startup,
        "recommended_actions": unique_actions(actions),
    }


def settings_value(config: dict[str, Any], path: list[str], fallback: Any) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return fallback
        current = current[key]
    return current


def build_report(
    day: dt.date,
    config: dict[str, Any],
    root: Path = ROOT,
    include_scheduler: bool = True,
    include_question_quality: bool = True,
) -> dict[str, Any]:
    generated_at = now(config).isoformat(timespec="seconds")
    sections = {
        "daily_collection": check_daily_collection(day, root),
        "codex_review": check_codex_review(day, root),
        "question_quality": check_question_quality(day, root) if include_question_quality else {"status": "Skipped", "recommended_actions": []},
        "weekly_review": check_weekly_review(day, root),
        "project_review": check_project_review(day, root),
        "recovery": check_recovery(day, root),
        "notion_sync": check_notion(root, config, day),
        "external_inputs": check_external_inputs(day, root, config),
        "chrome_extension": check_chrome_extension(day, root, config),
        "sqlite_database": check_sqlite_database(day, root, config),
        "retention": check_retention(root, config),
        "tray": check_tray(root, config),
        "file_access": check_file_access(root),
    }
    if not include_question_quality:
        sections.pop("question_quality")
    if include_scheduler:
        sections["scheduler"] = check_scheduler(root)
    recommended_actions = unique_actions(
        action
        for section in sections.values()
        for action in section.get("recommended_actions", [])
    )
    overall = "OK"
    for section in sections.values():
        overall = worst_status(overall, section.get("status", "OK"))
    return {
        "report_type": "activity_journal_health",
        "generated_at": generated_at,
        "checked_date": day.isoformat(),
        "overall": overall,
        "sections": sections,
        "recommended_actions": recommended_actions,
    }


def unique_actions(actions: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for action in actions:
        if action and action not in seen:
            seen.add(action)
            out.append(action)
    return out


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Activity Journal System Status - {report['checked_date']}",
        "",
        f"- Overall: {report['overall']}",
        f"- Generated at: {report['generated_at']}",
        "",
    ]
    for title, key in [
        ("Daily Collection", "daily_collection"),
        ("Codex Review", "codex_review"),
        ("Question Quality", "question_quality"),
        ("Weekly Review", "weekly_review"),
        ("Project Review", "project_review"),
        ("Recovery", "recovery"),
        ("Notion Sync", "notion_sync"),
        ("External Inputs", "external_inputs"),
        ("Chrome Extension", "chrome_extension"),
        ("SQLite Database", "sqlite_database"),
        ("Retention", "retention"),
        ("Tray", "tray"),
        ("Scheduler", "scheduler"),
        ("File Access", "file_access"),
    ]:
        section = report["sections"].get(key)
        if not section:
            continue
        lines.extend([f"## {title}", f"- Status: {section['status']}"])
        lines.extend(render_section_details(key, section))
        lines.append("")
    lines.append("## Recommended Actions")
    if report["recommended_actions"]:
        lines.extend(f"- `{action}`" for action in report["recommended_actions"])
    else:
        lines.append("- No action needed.")
    return "\n".join(lines).rstrip() + "\n"


def render_section_details(key: str, section: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if key == "daily_collection":
        for name, info in section["files"].items():
            suffix = f", modified {info['modified_at']}" if info.get("modified_at") else ""
            lines.append(f"- {name}: {'present' if info['exists'] else 'missing'} ({info['path']}{suffix})")
    elif key == "codex_review":
        lines.append(f"- Questions: {section['total_questions']} total, {section['pending_questions']} pending, {section['answered_questions']} answered")
        if section.get("pending_ids"):
            lines.append(f"- Pending IDs: {', '.join(section['pending_ids'])}")
    elif key == "question_quality":
        lines.append(f"- Generated: {'yes' if section.get('generated') else 'not generated'}")
        lines.append(f"- Candidates: {section['candidate_count']} total, {section['unresolved_candidate_count']} unresolved")
        lines.append(f"- Pending question blocks: {section['pending_questions']}")
        if section.get("unresolved_ids"):
            lines.append(f"- Unresolved candidate IDs: {', '.join(section['unresolved_ids'])}")
        if section.get("error"):
            lines.append(f"- Error: {section['error']}")
    elif key == "weekly_review":
        info = section["weekly_file"]
        lines.append(f"- Week: {section['week']}")
        lines.append(f"- File: {'present' if info['exists'] else 'missing'} ({info['path']})")
    elif key == "project_review":
        lines.append(f"- Generated: {'yes' if section.get('generated') else 'not generated'}")
        lines.append(f"- Projects: {section['project_count']}")
        lines.append(f"- Missing goals: {section['missing_goal_count']}")
        if section.get("missing_goal_projects"):
            lines.append(f"- Missing goal projects: {', '.join(section['missing_goal_projects'])}")
        if section.get("error"):
            lines.append(f"- Error: {section['error']}")
    elif key == "recovery":
        lines.append(f"- Generated: {'yes' if section.get('generated') else 'not generated'}")
        lines.append(f"- Completed actions: {section['action_count']}")
        lines.append(f"- Failed actions: {section['failed_action_count']}")
        lines.append(f"- Remaining user actions: {section['remaining_user_action_count']}")
        lines.append(f"- Codex Review needed: {'yes' if section.get('codex_review_needed') else 'no'}")
        lines.append(f"- Codex Review opened: {'yes' if section.get('codex_review_opened') else 'no'}")
        if section.get("error"):
            lines.append(f"- Error: {section['error']}")
    elif key == "notion_sync":
        lines.append(f"- Token: {section['token']}")
        lines.append(f"- Parent page ID: {section['parent_page_id']}")
        state = section["state_file"]
        lines.append(f"- State file: {'valid' if state.get('valid') else state.get('error', 'invalid')}")
        if section.get("sync_policy") == "delayed_final":
            lines.append(f"- Sync policy: delayed final, D+{section.get('finalize_after_days')}")
            lines.append(f"- Pending finalization: {'yes' if section.get('pending_finalization') else 'no'}")
            lines.append(f"- Daily finalized: {'yes' if section.get('daily_finalized') else 'no'}")
            lines.append(f"- Failed retry items: {section.get('failed_retry_count', 0)}")
            if section.get("next_due_dates"):
                lines.append(f"- Next due dates: {', '.join(section['next_due_dates'])}")
        if section.get("daily_synced") is not None:
            lines.append(f"- Checked daily page: {'synced' if section['daily_synced'] else 'not synced'}")
            if section.get("daily_hash_matches") is not None:
                lines.append(f"- Daily content hash: {'current' if section['daily_hash_matches'] else 'stale'}")
        if section.get("weekly_synced") is not None:
            lines.append(f"- Checked weekly page: {'synced' if section['weekly_synced'] else 'not synced'}")
            if section.get("weekly_hash_matches") is not None:
                lines.append(f"- Weekly content hash: {'current' if section['weekly_hash_matches'] else 'stale'}")
    elif key == "external_inputs":
        if not section.get("enabled"):
            lines.append("- External input collection is disabled.")
        elif section.get("capture_active") is False:
            lines.append(f"- External input capture is paused: {section.get('capture_reason')}")
        else:
            lines.append(f"- Inbox: {section['inbox_count']} item(s) from {section['inbox_dir']['path']}")
            lines.append(f"- ChatGPT: {section['chatgpt_count']} conversation(s) from {section['chatgpt_export_dir']['path']}")
            lines.append(f"- ChatGPT live: {section.get('chatgpt_live_count', 0)} capture(s)")
            lines.append(f"- Browser history: {section.get('browser_history_count', 0)} page(s)")
            lines.append(f"- Recent files: {section.get('recent_file_count', 0)} file(s)")
            lines.append(f"- App activity: {section.get('activity_watch_count', 0)} window(s)")
            lines.append(f"- App visible text: {section.get('activity_text_count', 0)} window(s)")
            if section.get("accessibility_text_enabled"):
                lines.append(f"- Accessibility text dependency: {section.get('accessibility_dependency', 'unknown')}")
            for warning in section.get("warnings", []):
                lines.append(f"- Warning: {warning}")
    elif key == "chrome_extension":
        if not section.get("enabled"):
            lines.append("- Chrome live capture is disabled.")
        elif section.get("capture_active") is False:
            lines.append(f"- Chrome live capture is paused: {section.get('capture_reason')}")
        else:
            manifest = section.get("manifest", {})
            receiver = section.get("receiver", {})
            log_file = section.get("log_file", {})
            lines.append(f"- Manifest: {manifest.get('status')} ({manifest.get('file', {}).get('path')})")
            if manifest.get("version"):
                lines.append(f"- Extension version: {manifest.get('version')}")
            for error in manifest.get("errors", []):
                lines.append(f"- Manifest issue: {error}")
            lines.append(f"- Receiver: {receiver.get('status')} ({receiver.get('url')})")
            if receiver.get("error"):
                lines.append(f"- Receiver issue: {receiver['error']}")
            lines.append(f"- Live log: {'present' if log_file.get('exists') else 'missing'} ({log_file.get('path')})")
            lines.append(f"- Captures for checked date: {section.get('recent_capture_count', 0)}")
            for warning in section.get("warnings", []):
                lines.append(f"- Warning: {warning}")
    elif key == "sqlite_database":
        if not section.get("enabled"):
            lines.append("- SQLite database indexing is disabled.")
        else:
            lines.append(f"- Database: {'present' if section.get('exists') else 'missing'} ({section.get('path')})")
            if section.get("schema_version") is not None:
                lines.append(f"- Schema version: {section.get('schema_version')} / {section.get('expected_schema_version')}")
            if section.get("size_mb") is not None:
                lines.append(f"- Size: {section.get('size_mb')} MB")
            if section.get("event_count") is not None:
                lines.append(f"- Total indexed events: {section.get('event_count')}")
            if section.get("checked_event_count") is not None:
                lines.append(f"- Checked date events: {section.get('checked_event_count')}")
            if section.get("daily_snapshot") is not None:
                snapshot = section["daily_snapshot"]
                lines.append(f"- Daily snapshot: yes, {snapshot.get('event_count', 0)} event(s)")
            elif section.get("exists"):
                lines.append("- Daily snapshot: missing")
            if section.get("fts_available") is not None:
                lines.append(f"- Full-text search: {'available' if section.get('fts_available') else 'unavailable'}")
            if section.get("text_retention_days") is not None:
                lines.append(f"- Text retention: {section.get('text_retention_days')} day(s)")
            if section.get("last_ingest_run"):
                run = section["last_ingest_run"]
                lines.append(f"- Last ingest: {run.get('status')} at {run.get('finished_at')}")
            if section.get("error"):
                lines.append(f"- Error: {section['error']}")
    elif key == "retention":
        if not section.get("enabled"):
            lines.append("- Raw log retention is disabled.")
        else:
            lines.append(f"- Keep recent days: {section.get('keep_recent_days')}")
            lines.append(f"- Cutoff date: {section.get('cutoff_date')}")
            lines.append(f"- Archive dir: {section.get('archive_dir')}")
            lines.append(f"- Delete archives: {'yes' if section.get('delete_archives') else 'no'}")
            lines.append(f"- Raw size: {section.get('raw_size_mb')} MB / {section.get('max_raw_mb')} MB")
            lines.append(f"- Lines ready to archive: {section.get('archived_line_count', 0)}")
            for source in section.get("sources", []):
                name = source.get("name")
                path = source.get("path")
                archived = source.get("archived_line_count", 0)
                kept = source.get("kept_line_count", 0)
                exists = "present" if source.get("exists") else "missing"
                lines.append(f"- {name}: {exists}, {kept} kept, {archived} ready to archive ({path})")
                if source.get("error"):
                    lines.append(f"- Error: {source['error']}")
            database = section.get("database", {})
            if database.get("enabled"):
                exists = "present" if database.get("exists") else "missing"
                lines.append(
                    f"- SQLite text retention: {exists}, {database.get('prunable_event_count', 0)} event(s) ready to prune ({database.get('path')})"
                )
                if database.get("error"):
                    lines.append(f"- SQLite retention error: {database['error']}")
    elif key == "tray":
        if not section.get("enabled"):
            lines.append("- Tray is disabled in config.")
        else:
            lines.append(f"- Notifications: {'on' if section.get('show_notifications') else 'off'}")
            for module, dependency in section.get("dependencies", {}).items():
                lines.append(f"- Dependency {module}: {'available' if dependency.get('available') else 'missing'} ({dependency.get('package')})")
            startup = section.get("startup_launcher", {})
            lines.append(
                f"- Startup launcher: {'present' if startup.get('exists') else 'missing'} ({startup.get('startup_launcher', 'unknown')})"
            )
            if startup.get("error"):
                lines.append(f"- Startup issue: {startup['error']}")
    elif key == "scheduler":
        if section.get("error"):
            lines.append(f"- Error: {section['error']}")
        for name, task in section.get("tasks", {}).items():
            lines.append(f"- {name}: {task['status']}")
    elif key == "file_access":
        for directory in section["directories"]:
            lines.append(f"- {directory['path']}: {directory['status']}")
        for issue in section.get("access_denied", []):
            lines.append(f"- Access issue: {issue['path']} ({issue['error']})")
    return lines


def write_report(report: dict[str, Any], root: Path = ROOT) -> None:
    status_md = root / "journal" / "system_status.md"
    status_json = root / "journal" / "raw" / "system_status.json"
    status_md.parent.mkdir(parents=True, exist_ok=True)
    status_json.parent.mkdir(parents=True, exist_ok=True)
    status_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    status_md.write_text(render_markdown(report), encoding="utf-8")


def main() -> None:
    configure_utf8_console()
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date to check, YYYY-MM-DD. Defaults to config-timezone today.")
    parser.add_argument("--json-only", action="store_true", help="Print JSON to stdout.")
    parser.add_argument("--no-write", action="store_true", help="Do not write journal/system_status.md or journal/raw/system_status.json.")
    args = parser.parse_args()
    config = load_config()
    try:
        day = parse_date(args.date, config)
    except ValueError:
        parser.error(f"--date must be YYYY-MM-DD, got: {args.date}")
    report = build_report(day, config)
    if not args.no_write:
        write_report(report)
    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if args.no_write:
            print(f"Activity Journal health: {report['overall']} for {report['checked_date']}")
        else:
            print(f"Wrote system status: {STATUS_MD_PATH}")
            print(f"Wrote system status JSON: {STATUS_JSON_PATH}")
            print(f"Overall: {report['overall']}")


if __name__ == "__main__":
    main()
