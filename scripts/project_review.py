from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"
METADATA_PATH = ROOT / "journal" / "projects" / "project_metadata.json"
VALID_STATUSES = {"Active", "Paused", "Done"}


def configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


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


def parse_date(value: str | None, config: dict[str, Any]) -> dt.date:
    if value:
        return dt.date.fromisoformat(value)
    return today(config)


def section(markdown: str, heading: str) -> list[str]:
    pattern = re.compile(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", re.M | re.S)
    match = pattern.search(markdown)
    if not match:
        return []
    items: list[str] = []
    for line in match.group(1).splitlines():
        cleaned = line.strip()
        if cleaned.startswith("- ") and not is_placeholder(cleaned):
            items.append(cleaned[2:].strip())
    return items


def is_placeholder(line: str) -> bool:
    return line in {
        "-",
        "- No project inferred.",
        "- No study signal inferred.",
        "- No build signal inferred.",
        "- No decisions captured.",
        "- Codex Review has not finalized this draft yet.",
        "- Open Codex Review and answer only the missing clarification questions.",
        "- Answer pending clarification questions when prompted.",
        "- No unresolved problems captured.",
        "- No next actions captured.",
    }


def safe_slug_base(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if slug:
        return slug[:80]
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    return f"project-{digest}"


def unique_project_slugs(project_names: list[str], metadata: dict[str, Any]) -> dict[str, str]:
    projects_meta = metadata.setdefault("projects", {})
    used: dict[str, str] = {}
    out: dict[str, str] = {}
    for name in sorted(project_names, key=str.lower):
        entry = projects_meta.get(name)
        existing_slug = entry.get("slug") if isinstance(entry, dict) else None
        base = str(existing_slug or safe_slug_base(name)).strip()
        slug = base
        if slug in used and used[slug] != name:
            slug = f"{safe_slug_base(name)}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:6]}"
        used[slug] = name
        out[name] = slug
    return out


def load_metadata(root: Path = ROOT) -> dict[str, Any]:
    path = root / "journal" / "projects" / "project_metadata.json"
    if not path.exists():
        return {"version": 1, "projects": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Project metadata is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Project metadata must be a JSON object: {path}")
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise RuntimeError(f"Project metadata projects must be an object: {path}")
    data.setdefault("version", 1)
    return data


def ensure_metadata_records(project_names: list[str], metadata: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False
    projects = metadata.setdefault("projects", {})
    slug_map = unique_project_slugs(project_names, metadata)
    for name in sorted(project_names, key=str.lower):
        entry = projects.get(name)
        if not isinstance(entry, dict):
            projects[name] = {"slug": slug_map[name], "goal": "", "status": "Active", "links": []}
            changed = True
            continue
        if not entry.get("slug"):
            entry["slug"] = slug_map[name]
            changed = True
        if "goal" not in entry:
            entry["goal"] = ""
            changed = True
        if "status" not in entry:
            entry["status"] = "Active"
            changed = True
        if "links" not in entry:
            entry["links"] = []
            changed = True
    return metadata, changed


def save_metadata(metadata: dict[str, Any], root: Path = ROOT) -> None:
    path = root / "journal" / "projects" / "project_metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def sanitize_status(value: Any) -> str:
    status = str(value or "Active")
    return status if status in VALID_STATUSES else "Active"


def clean_links(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def daily_files_until(day: dt.date, root: Path = ROOT) -> list[Path]:
    daily_dir = root / "journal" / "daily"
    if not daily_dir.exists():
        return []
    files: list[Path] = []
    for path in daily_dir.glob("*.md"):
        try:
            file_day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if file_day <= day:
            files.append(path)
    return sorted(files)


def weekly_path_for(day: dt.date, root: Path = ROOT) -> Path:
    year, week, _ = day.isocalendar()
    return root / "journal" / "weekly" / f"{year}-W{week:02d}.md"


def related_items(project: str, day_projects: list[str], items: list[str]) -> list[str]:
    if not items:
        return []
    if len(day_projects) == 1:
        return items
    project_lower = project.lower()
    return [item for item in items if project_lower in item.lower()]


def append_unique(target: list[str], values: list[str]) -> None:
    seen = set(target)
    for value in values:
        if value and value not in seen:
            target.append(value)
            seen.add(value)


def empty_project(name: str, slug: str, metadata: dict[str, Any]) -> dict[str, Any]:
    entry = metadata.get("projects", {}).get(name, {})
    if not isinstance(entry, dict):
        entry = {}
    goal = str(entry.get("goal") or "").strip()
    return {
        "name": name,
        "slug": slug,
        "status": sanitize_status(entry.get("status")),
        "goal": goal,
        "links": clean_links(entry.get("links")),
        "recent_progress": [],
        "key_decisions": [],
        "open_problems": [],
        "next_actions": [],
        "linked_daily_logs": [],
        "linked_weekly_reviews": [],
    }


def collect_projects(day: dt.date, metadata: dict[str, Any], root: Path = ROOT) -> dict[str, dict[str, Any]]:
    daily_paths = daily_files_until(day, root)
    project_names: set[str] = set()
    daily_data: list[tuple[Path, str, list[str], dict[str, list[str]]]] = []
    for path in daily_paths:
        markdown = path.read_text(encoding="utf-8", errors="replace")
        projects = section(markdown, "Projects")
        if not projects:
            continue
        project_names.update(projects)
        daily_data.append(
            (
                path,
                path.stem,
                projects,
                {
                    "studied": section(markdown, "Studied"),
                    "built": section(markdown, "Built"),
                    "decisions": section(markdown, "Decisions"),
                    "problems": section(markdown, "Problems"),
                    "next_actions": section(markdown, "Next Actions"),
                },
            )
        )
    slug_map = unique_project_slugs(list(project_names), metadata)
    projects = {name: empty_project(name, slug_map[name], metadata) for name in sorted(project_names, key=str.lower)}
    for path, day_label, day_projects, sections in daily_data:
        weekly_path = weekly_path_for(dt.date.fromisoformat(day_label), root)
        for project in day_projects:
            if project not in projects:
                continue
            entry = projects[project]
            append_unique(entry["linked_daily_logs"], [display_path(path, root)])
            if weekly_path.exists():
                append_unique(entry["linked_weekly_reviews"], [display_path(weekly_path, root)])
            progress = [f"{day_label}: {item}" for item in related_items(project, day_projects, sections["built"])]
            progress.extend(f"{day_label}: studied - {item}" for item in related_items(project, day_projects, sections["studied"]))
            append_unique(entry["recent_progress"], progress)
            append_unique(entry["key_decisions"], [f"{day_label}: {item}" for item in related_items(project, day_projects, sections["decisions"])])
            append_unique(entry["open_problems"], [f"{day_label}: {item}" for item in related_items(project, day_projects, sections["problems"])])
            append_unique(entry["next_actions"], [f"{day_label}: {item}" for item in related_items(project, day_projects, sections["next_actions"])])
    return projects


def display_path(path: Path, root: Path = ROOT) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def render_list(values: list[str], empty: str, limit: int = 40) -> list[str]:
    if not values:
        return [f"- {empty}"]
    return [f"- {value}" for value in values[:limit]]


def render_project_markdown(project: dict[str, Any], checked_date: dt.date) -> str:
    goal = project["goal"] or "Unknown"
    lines = [
        f"# Project Log - {project['name']}",
        "",
        f"Status: {project['status']}",
        f"Goal: {goal}",
        f"Last Updated: {checked_date.isoformat()}",
        "",
        "## Recent Progress",
        *render_list(project["recent_progress"], "No progress captured."),
        "",
        "## Key Decisions",
        *render_list(project["key_decisions"], "No decisions captured."),
        "",
        "## Open Problems",
        *render_list(project["open_problems"], "No open problems captured."),
        "",
        "## Next Actions",
        *render_list(project["next_actions"], "No next actions captured."),
        "",
        "## Repos / Links",
        *render_list(project["links"], "No links captured."),
        "",
        "## Linked Daily Logs",
        *render_list(project["linked_daily_logs"], "No daily logs linked."),
        "",
        "## Linked Weekly Reviews",
        *render_list(project["linked_weekly_reviews"], "No weekly reviews linked."),
    ]
    return "\n".join(lines).rstrip() + "\n"


def build_rollup(day: dt.date, root: Path = ROOT, update_metadata: bool = True) -> dict[str, Any]:
    metadata = load_metadata(root)
    initial_projects = collect_project_names(day, root)
    metadata, metadata_changed = ensure_metadata_records(initial_projects, metadata)
    if update_metadata and metadata_changed:
        save_metadata(metadata, root)
    projects = collect_projects(day, metadata, root)
    missing_goals = [project["name"] for project in projects.values() if not project["goal"]]
    return {
        "report_type": "activity_journal_project_rollup",
        "checked_date": day.isoformat(),
        "project_count": len(projects),
        "missing_goal_count": len(missing_goals),
        "missing_goal_projects": missing_goals,
        "projects": projects,
    }


def collect_project_names(day: dt.date, root: Path = ROOT) -> list[str]:
    names: set[str] = set()
    for path in daily_files_until(day, root):
        markdown = path.read_text(encoding="utf-8", errors="replace")
        names.update(section(markdown, "Projects"))
    return sorted(names, key=str.lower)


def write_rollup(report: dict[str, Any], root: Path = ROOT) -> None:
    checked_date = report["checked_date"]
    projects_dir = root / "journal" / "projects"
    raw_dir = root / "journal" / "raw"
    projects_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for project in report["projects"].values():
        path = projects_dir / f"{project['slug']}.md"
        path.write_text(render_project_markdown(project, dt.date.fromisoformat(checked_date)), encoding="utf-8")
    raw_path = raw_dir / f"project_rollup_{checked_date}.json"
    raw_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    configure_utf8_console()
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date to roll up through, YYYY-MM-DD. Defaults to config-timezone today.")
    parser.add_argument("--json-only", action="store_true", help="Print rollup JSON to stdout.")
    parser.add_argument("--no-write", action="store_true", help="Do not write project logs, metadata, or raw rollup.")
    args = parser.parse_args()
    config = load_config()
    try:
        day = parse_date(args.date, config)
    except ValueError:
        parser.error(f"--date must be YYYY-MM-DD, got: {args.date}")
    report = build_rollup(day, update_metadata=not args.no_write)
    if not args.no_write:
        write_rollup(report)
    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.no_write:
        print(f"Project rollup: {report['project_count']} project(s), {report['missing_goal_count']} missing goal(s).")
    else:
        print(f"Wrote project rollup for {report['checked_date']}: {report['project_count']} project(s)")


if __name__ == "__main__":
    main()
