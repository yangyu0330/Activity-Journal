from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"


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


def today(config: dict[str, Any]) -> dt.date:
    timezone = configured_timezone(config)
    if timezone:
        return dt.datetime.now(timezone).date()
    return dt.datetime.now().date()


def parse_date(value: str | None, config: dict[str, Any] | None = None) -> dt.date:
    if value:
        return dt.date.fromisoformat(value)
    if config is not None:
        return today(config)
    return dt.datetime.now().date()


def week_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    start = day - dt.timedelta(days=day.weekday())
    return start, start + dt.timedelta(days=6)


def collect_daily_files(start: dt.date, end: dt.date) -> list[Path]:
    daily_dir = ROOT / "journal" / "daily"
    files: list[Path] = []
    current = start
    while current <= end:
        path = daily_dir / f"{current.isoformat()}.md"
        if path.exists():
            files.append(path)
        current += dt.timedelta(days=1)
    return files


def section(text: str, heading: str) -> list[str]:
    pattern = re.compile(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", re.M | re.S)
    match = pattern.search(text)
    if not match:
        return []
    lines = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and not is_placeholder(stripped):
            lines.append(stripped)
    return lines


def is_placeholder(line: str) -> bool:
    placeholders = [
        "-",
        "- No project inferred.",
        "- No study signal inferred.",
        "- No build signal inferred.",
        "- No decisions captured.",
        "- Codex Review has not finalized this draft yet.",
        "- Open Codex Review and answer only the missing clarification questions.",
        "- Answer pending clarification questions when prompted.",
        "- No unresolved problems captured.",
    ]
    return line in placeholders


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def answered_problem_keys(decisions: list[str]) -> set[str]:
    keys: set[str] = set()
    for decision in decisions:
        if " -> " not in decision:
            continue
        question, _answer = decision.split(" -> ", 1)
        keys.add(question.strip())
    return keys


def unresolved_problems(problems: list[str], decisions: list[str]) -> list[str]:
    answered = answered_problem_keys(decisions)
    return [problem for problem in problems if problem.strip() not in answered]


def cleanup_answered_daily(markdown: str) -> str:
    decisions = section(markdown, "Decisions")
    problems = section(markdown, "Problems")
    unresolved = unique(unresolved_problems(problems, decisions))
    replacement = "\n".join(unresolved) if unresolved else "- No unresolved problems captured."
    pattern = re.compile(r"(^## Problems\n)(.*?)(?=^## |\Z)", re.M | re.S)
    return pattern.sub(lambda match: f"{match.group(1)}{replacement}\n\n", markdown, count=1)


def render_weekly(day: dt.date) -> str:
    start, end = week_bounds(day)
    files = collect_daily_files(start, end)
    projects: list[str] = []
    studied: list[str] = []
    built: list[str] = []
    decisions: list[str] = []
    problems: list[str] = []
    next_actions: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        day_label = path.stem
        projects.extend(section(text, "Projects"))
        studied.extend(section(text, "Studied"))
        built.extend(f"- {day_label}: {item.lstrip('- ')}" for item in section(text, "Built"))
        decisions.extend(section(text, "Decisions"))
        problems.extend(section(text, "Problems"))
        next_actions.extend(section(text, "Next Actions"))
    projects = unique(projects)
    studied = unique(studied)
    built = unique(built)
    decisions = unique(decisions)
    unresolved = unique(unresolved_problems(problems, decisions))
    next_actions = unique(next_actions)
    iso_year, iso_week, _ = day.isocalendar()
    lines = [f"# Weekly Review - {iso_year}-W{iso_week:02d}", ""]
    lines.append(f"Range: {start.isoformat()} to {end.isoformat()}")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- Daily logs: {len(files)}")
    lines.append(f"- Projects: {len(projects)}")
    lines.append(f"- Studied: {len(studied)}")
    lines.append(f"- Built: {len(built)}")
    lines.append(f"- Decisions: {len(decisions)}")
    lines.append(f"- Unresolved problems: {len(unresolved)}")
    lines.append(f"- Next actions: {len(next_actions)}")
    lines.append("")
    lines.append("## Main Projects")
    lines.extend(render_list(projects, "No projects captured."))
    lines.append("")
    lines.append("## What I Studied")
    lines.extend(render_list(studied, "No study items captured."))
    lines.append("")
    lines.append("## What I Built")
    lines.extend(render_list(built, "No build items captured."))
    lines.append("")
    lines.append("## Key Decisions")
    lines.extend(render_list(decisions, "No decisions captured."))
    lines.append("")
    lines.append("## Repeated Problems")
    lines.extend(render_list(unresolved, "No repeated problems captured."))
    lines.append("")
    lines.append("## Next Week Priorities")
    lines.extend(render_list(next_actions, "No next actions captured."))
    lines.append("")
    lines.append("## Linked Daily Logs")
    if files:
        for path in files:
            lines.append(f"- `{display_path(path)}`")
    else:
        lines.append("- No daily logs found for this week.")
    return "\n".join(lines).rstrip() + "\n"


def render_list(values: list[str], empty: str) -> list[str]:
    if not values:
        return [f"- {empty}"]
    return values[:30]


def display_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Any date in the target week, YYYY-MM-DD. Defaults to today.")
    args = parser.parse_args()
    config = load_config()
    try:
        day = parse_date(args.date, config)
    except ValueError:
        parser.error(f"--date must be YYYY-MM-DD, got: {args.date}")
    iso_year, iso_week, _ = day.isocalendar()
    out_dir = ROOT / "journal" / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{iso_year}-W{iso_week:02d}.md"
    out_path.write_text(render_weekly(day), encoding="utf-8")
    print(f"Wrote weekly review: {out_path}")


if __name__ == "__main__":
    main()
