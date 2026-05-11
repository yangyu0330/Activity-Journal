from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import capture_controls
import privacy_filters


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"
CHROMIUM_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)


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


def collection_window(day: dt.date, config: dict[str, Any]) -> tuple[dt.datetime, dt.datetime]:
    timezone = configured_timezone(config)
    if timezone:
        start = dt.datetime.combine(day, dt.time.min, tzinfo=timezone)
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=timezone)
        return start, end
    return (
        dt.datetime.combine(day, dt.time.min),
        dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min),
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def run_git(repo: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout.strip()


def is_git_repo(path: Path) -> bool:
    return path.is_dir() and (path / ".git").exists()


def find_git_repos(config: dict[str, Any]) -> list[Path]:
    ignored = set(config["scan"]["ignored_dirs"])
    repos: list[Path] = []
    for root_value in config["project_roots"]:
        root = Path(root_value).expanduser()
        if not root.exists():
            continue
        if is_git_repo(root):
            repos.append(root)
            continue
        for current, dirs, _files in os.walk(root):
            current_path = Path(current)
            if ".git" in dirs:
                repos.append(current_path)
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in ignored]
    return sorted(set(repos), key=lambda p: str(p).lower())


def display_project(config: dict[str, Any], project: str) -> str:
    return dict(config.get("project_aliases", {})).get(project, project)


def project_name_for_path(root: Path, path: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return root.name
    if not rel.parts:
        return root.name
    if re.match(r"^\d{4}-\d{2}-\d{2}$", rel.parts[0]) and len(rel.parts) > 1:
        return rel.parts[1]
    return rel.parts[0]


def collect_git_activity(config: dict[str, Any], repos: list[Path], since: dt.datetime, until: dt.datetime) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    since_s = since.isoformat(timespec="seconds")
    until_s = until.isoformat(timespec="seconds")
    for repo in repos:
        branch = run_git(repo, ["branch", "--show-current"])
        commits_raw = run_git(
            repo,
            [
                "log",
                f"--since={since_s}",
                f"--until={until_s}",
                "--pretty=format:%h%x09%ad%x09%s",
                "--date=iso",
                "--max-count=20",
            ],
        )
        status_raw = run_git(repo, ["status", "--short"])
        changed_files = [line for line in status_raw.splitlines() if line.strip()]
        if commits_raw or changed_files:
            items.append(
                {
                    "repo": str(repo),
                    "project": display_project(config, repo.name),
                    "branch": branch,
                    "commits": commits_raw.splitlines() if commits_raw else [],
                    "changed_files": changed_files[:50],
                }
            )
    return items


def should_ignore(path: Path, ignored_dirs: set[str]) -> bool:
    return any(part in ignored_dirs for part in path.parts)


def collect_modified_files(config: dict[str, Any], since: dt.datetime) -> dict[str, list[str]]:
    ignored = set(config["scan"]["ignored_dirs"])
    max_files = int(config["scan"]["max_modified_files_per_project"])
    since_ts = since.timestamp()
    by_project: dict[str, list[str]] = {}
    for root_value in config["project_roots"]:
        root = Path(root_value).expanduser()
        if not root.exists():
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in ignored]
            current_path = Path(current)
            if should_ignore(current_path, ignored):
                continue
            for file_name in files:
                path = current_path / file_name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < since_ts:
                    continue
                project = display_project(config, project_name_for_path(root, path))
                project_files = by_project.setdefault(project, [])
                if len(project_files) < max_files:
                    project_files.append(str(path))
    return by_project


def is_iso_between(value: str, since: dt.datetime, until: dt.datetime) -> bool:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if since.tzinfo is not None or until.tzinfo is not None or parsed.tzinfo is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=since.tzinfo or until.tzinfo)
        return since.timestamp() <= parsed.timestamp() < until.timestamp()
    return since <= parsed < until


def collect_codex(config: dict[str, Any], since: dt.datetime, until: dt.datetime) -> dict[str, Any]:
    codex_home = Path(config["codex"]["home"]).expanduser()
    history = read_jsonl(codex_home / "history.jsonl")
    session_index = read_jsonl(codex_home / "session_index.jsonl")
    since_ts = since.timestamp()
    until_ts = until.timestamp()
    include_history_text = bool(config["codex"].get("include_history_text", False))
    history_items = [
        row
        for row in history
        if isinstance(row.get("ts"), (int, float)) and since_ts <= float(row["ts"]) < until_ts
    ]
    if not include_history_text:
        history_items = [{"session_id": row.get("session_id"), "ts": row.get("ts")} for row in history_items]
    max_items = int(config["codex"]["max_history_items"])
    sessions = [
        row
        for row in session_index
        if "updated_at" in row and is_iso_between(str(row["updated_at"]), since, until)
    ]
    if not include_history_text:
        sessions = sanitize_codex_sessions(sessions)
    return {"history": history_items[-max_items:], "sessions": sessions[-max_items:]}


def sanitize_codex_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": row.get("id"), "updated_at": row.get("updated_at")} for row in sessions]


def read_manual_notes(config: dict[str, Any], day: dt.date) -> str:
    notes_dir = Path(config.get("manual_notes_dir", "manual_notes"))
    if not notes_dir.is_absolute():
        notes_dir = ROOT / notes_dir
    path = notes_dir / f"{day.isoformat()}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def configured_path(value: str, root: Path = ROOT) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else root / path


def first_meaningful_line(text: str, fallback: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:180]
    return fallback


def truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def excerpt_text(text: str, max_chars: int = 700) -> str:
    return truncate_text(" ".join(text.split()), max_chars)


def include_text(item: dict[str, Any], text: str, config: dict[str, Any]) -> None:
    external = config.get("external_inputs", {})
    if not bool(external.get("include_raw_text", False)):
        return
    max_chars = int(external.get("max_text_chars_per_item", 12000))
    item["text"] = truncate_text(text, max_chars)


def file_modified_between(path: Path, since: dt.datetime, until: dt.datetime) -> bool:
    try:
        modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    except OSError:
        return False
    if since.tzinfo is None and until.tzinfo is None:
        local_modified = modified.astimezone().replace(tzinfo=None)
        return since <= local_modified < until
    return since.timestamp() <= modified.timestamp() < until.timestamp()


def collect_inbox(config: dict[str, Any], day: dt.date, since: dt.datetime, until: dt.datetime, root: Path = ROOT) -> tuple[list[dict[str, Any]], list[str]]:
    external = config.get("external_inputs", {})
    inbox_dir = configured_path(str(external.get("inbox_dir", "inbox")), root)
    warnings: list[str] = []
    if not inbox_dir.exists():
        return [], warnings
    candidates: list[Path] = []
    for suffix in [".md", ".txt"]:
        dated_file = inbox_dir / f"{day.isoformat()}{suffix}"
        if dated_file.exists():
            candidates.append(dated_file)
    dated_dir = inbox_dir / day.isoformat()
    if dated_dir.exists():
        candidates.extend(path for path in dated_dir.iterdir() if path.suffix.lower() in {".md", ".txt"} and path.is_file())
    candidates.extend(
        path
        for path in inbox_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".md", ".txt"} and file_modified_between(path, since, until)
    )
    unique_candidates = sorted(set(candidates), key=lambda path: str(path).lower())
    max_items = int(external.get("max_items_per_source", 50))
    items: list[dict[str, Any]] = []
    for path in unique_candidates[:max_items]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            warnings.append(f"Inbox file unreadable: {path} ({exc})")
            continue
        if not text:
            continue
        item: dict[str, Any] = {
            "path": display_path(path, root),
            "title": first_meaningful_line(text, path.stem),
            "modified_at": dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
        }
        include_text(item, text, config)
        items.append(item)
    if len(unique_candidates) > max_items:
        warnings.append(f"Inbox item limit reached: {len(unique_candidates)} found, {max_items} collected.")
    return items, warnings


def display_path(path: Path, root: Path = ROOT) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def parse_chatgpt_timestamp(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    return None


def display_clock(value: Any, config: dict[str, Any] | None = None) -> str:
    parsed = parse_chatgpt_timestamp(value)
    if parsed is None:
        return "time unknown"
    timezone = configured_timezone(config or {})
    if timezone:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone)
        else:
            parsed = parsed.astimezone(timezone)
    elif parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%H:%M")


def datetime_between(value: dt.datetime | None, since: dt.datetime, until: dt.datetime) -> bool:
    if value is None:
        return False
    if since.tzinfo is None and until.tzinfo is None:
        comparable = value.astimezone().replace(tzinfo=None) if value.tzinfo else value
        return since <= comparable < until
    comparable = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    return since.timestamp() <= comparable.timestamp() < until.timestamp()


def chatgpt_message_parts(content: dict[str, Any]) -> list[str]:
    parts = content.get("parts", [])
    out: list[str] = []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    out.append(text)
    text = content.get("text")
    if isinstance(text, str):
        out.append(text)
    return [part.strip() for part in out if part and part.strip()]


def chatgpt_conversation_text(conversation: dict[str, Any]) -> tuple[str, int]:
    messages: list[str] = []
    mapping = conversation.get("mapping")
    if isinstance(mapping, dict):
        nodes = mapping.values()
    else:
        nodes = conversation.get("messages", []) if isinstance(conversation.get("messages"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        message = node.get("message") if "message" in node else node
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, dict):
            continue
        messages.extend(chatgpt_message_parts(content))
    return "\n\n".join(messages).strip(), len(messages)


def read_chatgpt_conversations_json(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"ChatGPT export unreadable: {path} ({exc})"]
    if not isinstance(data, list):
        return [], [f"ChatGPT export has unexpected shape: {path}"]
    return [item for item in data if isinstance(item, dict)], []


def read_chatgpt_zip(path: Path, max_member_bytes: int = 50_000_000) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            members = [member for member in archive.infolist() if member.filename.endswith("conversations.json")]
            if not members:
                return [], [f"ChatGPT zip has no conversations.json: {path}"]
            member = members[0]
            if member.file_size > max_member_bytes:
                return [], [f"ChatGPT conversations.json too large: {path} ({member.file_size} bytes)"]
            with archive.open(member) as handle:
                data = json.loads(handle.read().decode("utf-8", errors="replace"))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        return [], [f"ChatGPT zip unreadable: {path} ({exc})"]
    if not isinstance(data, list):
        warnings.append(f"ChatGPT conversations.json has unexpected shape: {path}")
        return [], warnings
    return [item for item in data if isinstance(item, dict)], warnings


def datetime_to_chromium_timestamp(value: dt.datetime) -> int:
    if value.tzinfo is None:
        value = dt.datetime.fromtimestamp(value.timestamp(), tz=dt.timezone.utc)
    else:
        value = value.astimezone(dt.timezone.utc)
    return int((value - CHROMIUM_EPOCH).total_seconds() * 1_000_000)


def chromium_timestamp_to_datetime(value: Any) -> dt.datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return CHROMIUM_EPOCH + dt.timedelta(microseconds=float(value))
    except OverflowError:
        return None


def default_browser_history_paths() -> list[Path]:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return []
    user_data_dirs = [
        Path(local_app_data) / "Google" / "Chrome" / "User Data",
        Path(local_app_data) / "Microsoft" / "Edge" / "User Data",
        Path(local_app_data) / "BraveSoftware" / "Brave-Browser" / "User Data",
        Path(local_app_data) / "Vivaldi" / "User Data",
    ]
    paths: list[Path] = []
    for user_data_dir in user_data_dirs:
        try:
            profiles = [profile for profile in user_data_dir.iterdir() if profile.is_dir()]
        except OSError:
            continue
        for profile in profiles:
            history_path = profile / "History"
            if history_path.is_file():
                paths.append(history_path)
    return sorted(set(paths), key=lambda path: str(path).lower())


def configured_browser_history_paths(settings: dict[str, Any]) -> tuple[list[Path], bool]:
    configured = settings.get("history_paths")
    if not isinstance(configured, list) or not configured:
        return default_browser_history_paths(), False
    paths = [Path(os.path.expandvars(str(value))).expanduser() for value in configured]
    return paths, True


def browser_profile_name(history_path: Path) -> str:
    if len(history_path.parents) >= 3:
        return f"{history_path.parents[2].name}/{history_path.parent.name}"
    return history_path.parent.name


def browser_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.lower()
    return domain[4:] if domain.startswith("www.") else domain


def compact_browser_url(url: str, max_chars: int = 220) -> str:
    if len(url) <= max_chars:
        return url
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower().endswith("google.com") and parsed.path == "/search":
        query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
        if query:
            compact = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?q={urllib.parse.quote_plus(query)}"
            if len(compact) <= max_chars:
                return compact
    base = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    if base and len(base) <= max_chars:
        return base
    return url[: max_chars - 3] + "..."


def compact_title(value: Any, max_chars: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def should_collect_browser_url(url: str, include_non_web: bool) -> bool:
    if include_non_web:
        return bool(url)
    return url.startswith(("http://", "https://"))


def read_browser_history(
    history_path: Path,
    since: dt.datetime,
    until: dt.datetime,
    *,
    max_rows: int,
    include_non_web: bool,
    root: Path = ROOT,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if not history_path.is_file():
        return [], [f"Browser history file not found: {history_path}"]

    since_chrome = datetime_to_chromium_timestamp(since)
    until_chrome = datetime_to_chromium_timestamp(until)
    rows: list[tuple[str, str, int]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="activity-journal-browser-") as tmp_dir:
            copied_history = Path(tmp_dir) / "History"
            shutil.copy2(history_path, copied_history)
            conn = sqlite3.connect(copied_history)
            try:
                rows = conn.execute(
                    """
                    SELECT urls.url, urls.title, visits.visit_time
                    FROM visits
                    JOIN urls ON urls.id = visits.url
                    WHERE visits.visit_time >= ? AND visits.visit_time < ?
                    ORDER BY visits.visit_time ASC
                    LIMIT ?
                    """,
                    (since_chrome, until_chrome, max_rows * 5),
                ).fetchall()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as exc:
        return [], [f"Browser history unreadable: {history_path} ({exc})"]

    profile = browser_profile_name(history_path)
    by_url: dict[str, dict[str, Any]] = {}
    for url, title, visit_time in rows:
        if not isinstance(url, str) or not should_collect_browser_url(url, include_non_web):
            continue
        visited_at = chromium_timestamp_to_datetime(visit_time)
        if visited_at is None:
            continue
        item = by_url.setdefault(
            url,
            {
                "title": str(title or browser_domain(url) or url)[:180],
                "url": url,
                "domain": browser_domain(url),
                "profile": profile,
                "source_path": display_path(history_path, root),
                "first_visit_time": visited_at,
                "last_visit_time": visited_at,
                "visit_count": 0,
            },
        )
        item["visit_count"] += 1
        if visited_at < item["first_visit_time"]:
            item["first_visit_time"] = visited_at
        if visited_at > item["last_visit_time"]:
            item["last_visit_time"] = visited_at

    items = []
    for item in by_url.values():
        first_visit = item["first_visit_time"]
        last_visit = item["last_visit_time"]
        item["first_visit_time"] = first_visit.isoformat(timespec="seconds")
        item["last_visit_time"] = last_visit.isoformat(timespec="seconds")
        items.append(item)
    items.sort(key=lambda item: (item["last_visit_time"], item["url"]))
    return items, warnings


def collect_browser_history(config: dict[str, Any], since: dt.datetime, until: dt.datetime, root: Path = ROOT) -> tuple[list[dict[str, Any]], list[str]]:
    external = config.get("external_inputs", {})
    settings = external.get("browser_history", {})
    if not isinstance(settings, dict) or not settings.get("enabled", False):
        return [], []

    max_items = int(settings.get("max_items", external.get("max_items_per_source", 50)))
    include_non_web = bool(settings.get("include_non_web", False))
    history_paths, explicit_paths = configured_browser_history_paths(settings)
    warnings: list[str] = []
    if not history_paths:
        return [], ["No supported browser History files found."]

    items: list[dict[str, Any]] = []
    for history_path in history_paths:
        if explicit_paths and not history_path.exists():
            warnings.append(f"Browser history file not found: {history_path}")
            continue
        profile_items, profile_warnings = read_browser_history(
            history_path,
            since,
            until,
            max_rows=max_items,
            include_non_web=include_non_web,
            root=root,
        )
        items.extend(profile_items)
        warnings.extend(profile_warnings)

    items = [item for item in items if not privacy_filters.should_exclude_raw_domain(config, item)]
    items.sort(key=lambda item: (item["last_visit_time"], item["url"]))
    if len(items) > max_items:
        warnings.append(f"Browser history item limit reached: {len(items)} found, {max_items} collected.")
        items = items[-max_items:]
    return items, warnings


def default_recent_file_roots() -> list[Path]:
    user_profile = os.environ.get("USERPROFILE")
    if not user_profile:
        return []
    base = Path(user_profile)
    return [base / "Desktop", base / "Documents", base / "Downloads"]


def configured_recent_file_roots(settings: dict[str, Any]) -> tuple[list[Path], bool]:
    configured = settings.get("roots")
    if not isinstance(configured, list) or not configured:
        return default_recent_file_roots(), False
    return [Path(os.path.expandvars(str(value))).expanduser() for value in configured], True


def collect_recent_files(config: dict[str, Any], since: dt.datetime, until: dt.datetime, root: Path = ROOT) -> tuple[list[dict[str, Any]], list[str]]:
    external = config.get("external_inputs", {})
    settings = external.get("recent_files", {})
    if not isinstance(settings, dict) or not settings.get("enabled", False):
        return [], []

    max_items = int(settings.get("max_items", external.get("max_items_per_source", 50)))
    max_depth = int(settings.get("max_depth", 4))
    ignored = set(config.get("scan", {}).get("ignored_dirs", []))
    roots, explicit_roots = configured_recent_file_roots(settings)
    warnings: list[str] = []
    items: list[dict[str, Any]] = []
    seen: set[Path] = set()

    for search_root in roots:
        if not search_root.exists():
            if explicit_roots:
                warnings.append(f"Recent file root not found: {search_root}")
            continue
        try:
            base_depth = len(search_root.resolve().parts)
        except OSError:
            base_depth = len(search_root.parts)
        for current, dirs, files in os.walk(search_root):
            current_path = Path(current)
            dirs[:] = [dirname for dirname in dirs if dirname not in ignored and not dirname.startswith(".")]
            if len(current_path.parts) - base_depth >= max_depth:
                dirs[:] = []
            if should_ignore(current_path, ignored):
                continue
            for file_name in files:
                path = current_path / file_name
                if path in seen or path.name.startswith("~$"):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                modified = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc)
                if not datetime_between(modified, since, until):
                    continue
                seen.add(path)
                items.append(
                    {
                        "title": path.name,
                        "path": display_path(path, root),
                        "modified_at": modified.isoformat(timespec="seconds"),
                        "size_bytes": stat.st_size,
                    }
                )

    items.sort(key=lambda item: (item["modified_at"], item["path"]))
    if len(items) > max_items:
        warnings.append(f"Recent file item limit reached: {len(items)} found, {max_items} collected.")
        items = items[-max_items:]
    return items, warnings


def collect_activity_watch(config: dict[str, Any], since: dt.datetime, until: dt.datetime, root: Path = ROOT) -> tuple[list[dict[str, Any]], list[str]]:
    external = config.get("external_inputs", {})
    settings = external.get("activity_watch", {})
    if not isinstance(settings, dict) or not settings.get("enabled", False):
        return [], []

    log_path = configured_path(str(settings.get("log_path", "journal/raw/activity_watch.jsonl")), root)
    max_items = int(settings.get("max_items", external.get("max_items_per_source", 50)))
    max_text_chars = int(settings.get("max_text_chars_per_item", external.get("max_text_chars_per_item", 12000)))
    include_raw_text = bool(external.get("include_raw_text", False))
    if not log_path.exists():
        return [], []

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    warnings: list[str] = []
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_time = parse_chatgpt_timestamp(event.get("ts"))
                if not datetime_between(event_time, since, until):
                    continue
                title = compact_title(event.get("title"), 180)
                process = compact_title(event.get("process") or "unknown", 80)
                if not title and not process:
                    continue
                if privacy_filters.should_exclude_raw_app(config, {"process": process, "title": title}):
                    continue
                key = (process, title)
                bucket = buckets.setdefault(
                    key,
                    {
                        "process": process,
                        "title": title,
                        "first_seen": event_time,
                        "last_seen": event_time,
                        "sample_count": 0,
                        "text_capture_count": 0,
                        "text_hashes": [],
                    },
                )
                bucket["sample_count"] += 1
                if event_time and event_time < bucket["first_seen"]:
                    bucket["first_seen"] = event_time
                if event_time and event_time > bucket["last_seen"]:
                    bucket["last_seen"] = event_time
                text = str(event.get("text") or "").strip()
                if text:
                    text_hash = str(event.get("text_hash") or stable_hash(text))
                    bucket["text_capture_count"] += 1
                    if text_hash not in bucket["text_hashes"]:
                        bucket["text_hashes"].append(text_hash)
                    if not bucket.get("_text_time") or (event_time and event_time >= bucket["_text_time"]):
                        bucket["_text_time"] = event_time
                        bucket["text_chars"] = int(event.get("text_chars") or len(text))
                        bucket["text_excerpt"] = excerpt_text(text)
                        if include_raw_text:
                            bucket["text"] = truncate_text(text, max_text_chars)
    except OSError as exc:
        return [], [f"Activity watch log unreadable: {log_path} ({exc})"]

    items: list[dict[str, Any]] = []
    for bucket in buckets.values():
        first_seen = bucket["first_seen"]
        last_seen = bucket["last_seen"]
        bucket["first_seen"] = first_seen.isoformat(timespec="seconds") if first_seen else None
        bucket["last_seen"] = last_seen.isoformat(timespec="seconds") if last_seen else None
        bucket.pop("_text_time", None)
        items.append(bucket)
    items.sort(key=lambda item: (item.get("last_seen") or "", item.get("process") or "", item.get("title") or ""))
    if len(items) > max_items:
        warnings.append(f"Activity watch item limit reached: {len(items)} found, {max_items} collected.")
        items = items[-max_items:]
    return items, warnings


def collect_chatgpt_exports(config: dict[str, Any], since: dt.datetime, until: dt.datetime, root: Path = ROOT) -> tuple[list[dict[str, Any]], list[str]]:
    external = config.get("external_inputs", {})
    export_dir = configured_path(str(external.get("chatgpt_export_dir", "imports/chatgpt")), root)
    warnings: list[str] = []
    if not export_dir.exists():
        return [], warnings
    export_files = sorted(
        [path for path in export_dir.rglob("conversations.json") if path.is_file()]
        + [path for path in export_dir.rglob("*.zip") if path.is_file()],
        key=lambda path: str(path).lower(),
    )
    max_items = int(external.get("max_items_per_source", 50))
    items: list[dict[str, Any]] = []
    for export_file in export_files:
        if export_file.suffix.lower() == ".zip":
            conversations, file_warnings = read_chatgpt_zip(export_file)
        else:
            conversations, file_warnings = read_chatgpt_conversations_json(export_file)
        warnings.extend(file_warnings)
        for conversation in conversations:
            if len(items) >= max_items:
                warnings.append(f"ChatGPT item limit reached: {max_items} collected.")
                return items, warnings
            updated_at = parse_chatgpt_timestamp(conversation.get("update_time") or conversation.get("updated_at"))
            created_at = parse_chatgpt_timestamp(conversation.get("create_time") or conversation.get("created_at"))
            event_time = updated_at or created_at
            if not datetime_between(event_time, since, until):
                continue
            text, message_count = chatgpt_conversation_text(conversation)
            title = str(conversation.get("title") or "Untitled ChatGPT conversation").strip()
            item: dict[str, Any] = {
                "title": title[:180],
                "source_path": display_path(export_file, root),
                "create_time": created_at.isoformat(timespec="seconds") if created_at else None,
                "update_time": updated_at.isoformat(timespec="seconds") if updated_at else None,
                "message_count": message_count,
            }
            include_text(item, text, config)
            items.append(item)
    return items, warnings


def collect_chatgpt_live(config: dict[str, Any], since: dt.datetime, until: dt.datetime, root: Path = ROOT) -> tuple[list[dict[str, Any]], list[str]]:
    external = config.get("external_inputs", {})
    settings = external.get("chatgpt_live", {})
    if not isinstance(settings, dict) or not settings.get("enabled", False):
        return [], []

    log_path = configured_path(str(settings.get("log_path", "journal/raw/chatgpt_live.jsonl")), root)
    max_items = int(settings.get("max_items", external.get("max_items_per_source", 50)))
    max_text_chars = int(settings.get("max_text_chars_per_item", external.get("max_text_chars_per_item", 12000)))
    if not log_path.exists():
        return [], []

    include_raw_text = bool(external.get("include_raw_text", False))
    items_by_hash: dict[str, dict[str, Any]] = {}
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                captured_at = parse_chatgpt_timestamp(event.get("captured_at") or event.get("received_at"))
                if not datetime_between(captured_at, since, until):
                    continue
                text = str(event.get("text") or "").strip()
                if not text:
                    continue
                if privacy_filters.should_exclude_raw_domain(config, event):
                    continue
                content_hash = str(event.get("content_hash") or f"{event.get('url', '')}:{hash(text)}")
                item: dict[str, Any] = {
                    "title": compact_title(event.get("title") or "Untitled ChatGPT conversation", 180),
                    "url": str(event.get("url") or "")[:2000],
                    "app": compact_title(event.get("app") or "chatgpt", 80),
                    "conversation_id": str(event.get("conversation_id") or "")[:240],
                    "captured_at": captured_at.isoformat(timespec="seconds") if captured_at else None,
                    "content_hash": content_hash,
                    "source_path": display_path(log_path, root),
                    "text_chars": len(text),
                }
                if include_raw_text:
                    item["text"] = truncate_text(text, max_text_chars)
                items_by_hash[content_hash] = item
    except OSError as exc:
        return [], [f"ChatGPT live capture log unreadable: {log_path} ({exc})"]

    items = sorted(items_by_hash.values(), key=lambda item: (item.get("captured_at") or "", item.get("title") or ""))
    if len(items) > max_items:
        items = items[-max_items:]
    return items, []


def collect_external_inputs(config: dict[str, Any], day: dt.date, since: dt.datetime, until: dt.datetime, root: Path = ROOT) -> dict[str, Any]:
    external = config.get("external_inputs", {})
    if not external.get("enabled", False):
        return {"enabled": False, "inbox": [], "chatgpt": [], "chatgpt_live": [], "browser_history": [], "recent_files": [], "activity_watch": [], "warnings": []}
    if not capture_controls.capture_active(config):
        reason = capture_controls.capture_pause_reason(config) or "capture paused"
        return {
            "enabled": False,
            "capture_paused": True,
            "pause_reason": reason,
            "inbox": [],
            "chatgpt": [],
            "chatgpt_live": [],
            "browser_history": [],
            "recent_files": [],
            "activity_watch": [],
            "warnings": [f"External input capture paused: {reason}"],
        }
    inbox, inbox_warnings = collect_inbox(config, day, since, until, root)
    chatgpt, chatgpt_warnings = collect_chatgpt_exports(config, since, until, root)
    chatgpt_live, chatgpt_live_warnings = collect_chatgpt_live(config, since, until, root)
    browser_history, browser_warnings = collect_browser_history(config, since, until, root)
    recent_files, recent_file_warnings = collect_recent_files(config, since, until, root)
    activity_watch, activity_watch_warnings = collect_activity_watch(config, since, until, root)
    return {
        "enabled": True,
        "inbox": inbox,
        "chatgpt": chatgpt,
        "chatgpt_live": chatgpt_live,
        "browser_history": browser_history,
        "recent_files": recent_files,
        "activity_watch": activity_watch,
        "warnings": inbox_warnings + chatgpt_warnings + chatgpt_live_warnings + browser_warnings + recent_file_warnings + activity_watch_warnings,
    }


def render_list(values: list[str], empty: str) -> list[str]:
    if not values:
        return [f"- {empty}"]
    return [f"- {value}" for value in values]


def render_daily(day: dt.date, raw: dict[str, Any], manual_notes: str, config: dict[str, Any] | None = None) -> str:
    projects = sorted({item["project"] for item in raw["git_activity"]} | set(raw["modified_files"].keys()))
    built: list[str] = []
    for item in raw["git_activity"]:
        if item["commits"]:
            built.append(f"{item['project']}: {len(item['commits'])} commits on {item.get('branch') or 'unknown branch'}")
        elif item["changed_files"]:
            built.append(f"{item['project']}: {len(item['changed_files'])} changed files")
    for project, files in raw["modified_files"].items():
        if not any(entry.startswith(f"{project}:") for entry in built):
            built.append(f"{project}: {len(files)} modified files")

    render_config = config or {}
    external_inputs = raw.get("external_inputs", {})
    chatgpt_live_items = [
        item for item in external_inputs.get("chatgpt_live", []) if not privacy_filters.should_hide_summary_domain(render_config, item)
    ]
    browser_items = [
        item for item in external_inputs.get("browser_history", []) if not privacy_filters.should_hide_summary_domain(render_config, item)
    ]
    recent_file_items = external_inputs.get("recent_files", [])
    activity_watch_items = [
        item for item in external_inputs.get("activity_watch", []) if not privacy_filters.should_hide_summary_app(render_config, item)
    ]
    studied = [line.strip().lstrip("- ").strip() for line in manual_notes.splitlines() if line.strip()]
    studied.extend(f"Inbox: {item['title']}" for item in external_inputs.get("inbox", []))
    studied.extend(f"ChatGPT: {item['title']}" for item in external_inputs.get("chatgpt", []))
    studied.extend(f"ChatGPT live: {item['title']}" for item in chatgpt_live_items[:12])
    if browser_items:
        studied.append(f"Browser activity: {len(browser_items)} page(s) visited")
        for item in browser_items[:12]:
            title = compact_title(item.get("title") or item.get("domain") or item.get("url"))
            domain = item.get("domain") or "unknown domain"
            studied.append(f"Visited: {title} ({domain})")
    if recent_file_items:
        studied.append(f"Recent files: {len(recent_file_items)} file(s) changed")
        for item in recent_file_items[:12]:
            studied.append(f"File changed: {compact_title(item.get('title'))}")
    if activity_watch_items:
        studied.append(f"App activity: {len(activity_watch_items)} active window(s)")
        text_window_count = len([item for item in activity_watch_items if int(item.get("text_capture_count") or 0) > 0])
        if text_window_count:
            studied.append(f"Visible app text captured from {text_window_count} active window(s)")
        for item in activity_watch_items[:12]:
            label = compact_title(item.get("title") or item.get("process"))
            process = item.get("process") or "unknown"
            studied.append(f"Used app: {label} ({process})")
    lines = [
        f"# Daily Activity Log - {day.isoformat()}",
        "",
        "Status: Draft",
        "",
        "## Projects",
        *render_list(projects, "No project inferred."),
        "",
        "## Studied",
        *render_list(studied, "No study signal inferred."),
        "",
        "## Built",
        *render_list(built, "No build signal inferred."),
        "",
        "## Decisions",
        "- Automatic local evidence capture and Notion sync remain the active logging workflow.",
        "",
        "## Problems",
        "- No unresolved problems captured.",
        "",
        "## Next Actions",
        "- Continue scheduled automatic capture and Notion sync.",
        "",
        "## Sources",
    ]
    if raw["codex"]["history"]:
        lines.append(f"- Codex history items: {len(raw['codex']['history'])}")
    if raw["codex"]["sessions"]:
        lines.append(f"- Codex sessions updated: {len(raw['codex']['sessions'])}")
    for item in external_inputs.get("inbox", []):
        lines.append(f"- Inbox: `{item['path']}`")
    for source in sorted({item["source_path"] for item in external_inputs.get("chatgpt", [])}):
        lines.append(f"- ChatGPT export: `{source}`")
    for source in sorted({item["source_path"] for item in chatgpt_live_items}):
        lines.append(f"- ChatGPT live capture: `{source}`")
    for source in sorted({item["source_path"] for item in browser_items}):
        lines.append(f"- Browser history: `{source}`")
    if recent_file_items:
        lines.append(f"- Recent files tracked: {len(recent_file_items)}")
    if activity_watch_items:
        lines.append(f"- Activity watch windows: {len(activity_watch_items)}")
    for item in raw["git_activity"]:
        lines.append(f"- Git: {item['repo']}")
    lines.extend(["", "## Browser Activity"])
    if browser_items:
        for item in browser_items:
            clock = display_clock(item.get("last_visit_time"), render_config)
            title = item.get("title") or item.get("domain") or item.get("url")
            visits = int(item.get("visit_count") or 0)
            suffix = f" ({visits} visits)" if visits > 1 else ""
            lines.append(f"- {clock} - {title} - {compact_browser_url(str(item.get('url') or ''))}{suffix}")
    else:
        lines.append("- No browser activity captured.")
    lines.extend(["", "## ChatGPT Live Captures"])
    if chatgpt_live_items:
        for item in chatgpt_live_items:
            clock = display_clock(item.get("captured_at"), render_config)
            lines.append(f"- {clock} - {item.get('title')} - {compact_browser_url(str(item.get('url') or ''))}")
            text = str(item.get("text") or "").strip()
            if text:
                excerpt = " ".join(text.split())[:700]
                lines.append(f"  - Excerpt: {excerpt}")
    else:
        lines.append("- No ChatGPT live captures.")
    lines.extend(["", "## Recent Files"])
    if recent_file_items:
        for item in recent_file_items:
            clock = display_clock(item.get("modified_at"), render_config)
            lines.append(f"- {clock} - {item.get('title')} - `{item.get('path')}`")
    else:
        lines.append("- No recent files captured.")
    lines.extend(["", "## App Activity"])
    if activity_watch_items:
        for item in activity_watch_items:
            clock = display_clock(item.get("last_seen"), render_config)
            samples = int(item.get("sample_count") or 0)
            suffix = f" ({samples} samples)" if samples > 1 else ""
            title = item.get("title") or item.get("process") or "Untitled window"
            lines.append(f"- {clock} - {title} - {item.get('process')}{suffix}")
            text_excerpt = str(item.get("text_excerpt") or "").strip()
            if text_excerpt:
                lines.append(f"  - Visible text: {text_excerpt}")
    else:
        lines.append("- No app activity captured.")
    lines.extend(["", "## Git Details"])
    for item in raw["git_activity"]:
        lines.append(f"### {item['project']}")
        lines.append(f"- Repo: `{item['repo']}`")
        lines.append(f"- Branch: `{item.get('branch') or 'unknown'}`")
        if item["commits"]:
            lines.append("- Commits:")
            for commit in item["commits"]:
                lines.append(f"  - `{commit}`")
        if item["changed_files"]:
            lines.append("- Changed files:")
            for changed in item["changed_files"][:20]:
                lines.append(f"  - `{changed}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_question_file(day: dt.date) -> str:
    return "\n".join(
        [
            f"# Questions - {day.isoformat()}",
            "",
            "Codex Review가 raw evidence와 daily draft를 읽고 부족한 정보만 질문합니다.",
            "질문을 만들 때는 아래 형식을 사용합니다.",
            "",
            "```text",
            "## Q: <stable_id>",
            "Category: <배운 점|작업명|상태|결정|다음 행동|설정 문제>",
            "Confidence: <high|medium|low>",
            "",
            "Question:",
            "<사용자에게 물어볼 질문>",
            "",
            "Context:",
            "- <왜 묻는지>",
            "- <어떤 파일/상황을 근거로 묻는지>",
            "",
            "Recommendation:",
            "<추천 답변>",
            "",
            "Options:",
            "- 추천 답변으로 저장",
            "- 수정해서 저장: ...",
            "- 저장하지 않음",
            "",
            "Answer:",
            "```",
            "",
        ]
    )


def write_outputs(
    day: dt.date,
    raw: dict[str, Any],
    manual_notes: str,
    overwrite_review_files: bool = False,
    config: dict[str, Any] | None = None,
) -> None:
    daily_dir = ROOT / "journal" / "daily"
    questions_dir = ROOT / "journal" / "questions"
    raw_dir = ROOT / "journal" / "raw"
    for directory in [daily_dir, questions_dir, raw_dir]:
        directory.mkdir(parents=True, exist_ok=True)
    daily_path = daily_dir / f"{day.isoformat()}.md"
    questions_path = questions_dir / f"{day.isoformat()}.md"
    daily_status = write_review_file(daily_path, render_daily(day, raw, manual_notes, config), overwrite_review_files)
    questions_status = write_review_file(questions_path, render_question_file(day), overwrite_review_files)
    (raw_dir / f"{day.isoformat()}.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Daily journal: {daily_status} {daily_path}")
    print(f"Questions file: {questions_status} {questions_path}")


def write_review_file(path: Path, content: str, overwrite: bool = False) -> str:
    existed = path.exists()
    if existed and not overwrite:
        return "preserved"
    path.write_text(content, encoding="utf-8")
    return "overwritten" if existed else "created"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date to collect, YYYY-MM-DD. Defaults to today.")
    parser.add_argument(
        "--print-default-date",
        action="store_true",
        help="Print the config-timezone default date and exit without writing files.",
    )
    parser.add_argument(
        "--overwrite-review-files",
        action="store_true",
        help="Regenerate daily/questions Markdown for the date. This can overwrite reviewed answers.",
    )
    args = parser.parse_args()
    config = load_config()
    try:
        day = parse_date(args.date, config)
    except ValueError:
        parser.error(f"--date must be YYYY-MM-DD, got: {args.date}")
    if args.print_default_date:
        print(day.isoformat())
        return
    lookback_hours = int(config["scan"]["lookback_hours"])
    day_start, day_end = collection_window(day, config)
    until = day_end
    since = until - dt.timedelta(hours=lookback_hours)
    repos = find_git_repos(config)
    raw = {
        "date": day.isoformat(),
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "git_activity": collect_git_activity(config, repos, since, until),
        "modified_files": collect_modified_files(config, since),
        "codex": collect_codex(config, since, until),
        "external_inputs": collect_external_inputs(config, day, since, until),
    }
    manual_notes = read_manual_notes(config, day)
    write_outputs(day, raw, manual_notes, overwrite_review_files=args.overwrite_review_files, config=config)
    print(f"Wrote raw evidence for {day.isoformat()}")
    print("Questions: delegated to Codex Review")


if __name__ == "__main__":
    main()
