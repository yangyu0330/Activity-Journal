from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import activity_db


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"

LOG_TARGETS = {
    "chatgpt_live": {
        "settings_path": ("external_inputs", "chatgpt_live"),
        "default_log_path": "journal/raw/chatgpt_live.jsonl",
        "timestamp_fields": ("captured_at", "received_at"),
    },
    "activity_watch": {
        "settings_path": ("external_inputs", "activity_watch"),
        "default_log_path": "journal/raw/activity_watch.jsonl",
        "timestamp_fields": ("ts",),
    },
}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def configured_timezone(config: dict[str, Any]) -> ZoneInfo | None:
    timezone = config.get("timezone")
    if not timezone:
        return None
    try:
        return ZoneInfo(str(timezone))
    except ZoneInfoNotFoundError:
        return None


def now(config: dict[str, Any], now_value: dt.datetime | None = None) -> dt.datetime:
    if now_value is not None:
        return now_value
    timezone = configured_timezone(config)
    if timezone:
        return dt.datetime.now(timezone)
    return dt.datetime.now().astimezone()


def configured_path(value: str, root: Path = ROOT) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else root / path


def nested_dict(config: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    current: Any = config
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key, {})
    return current if isinstance(current, dict) else {}


def retention_settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("retention", {})
    if not isinstance(settings, dict):
        settings = {}
    return {
        "enabled": bool(settings.get("enabled", False)),
        "keep_recent_days": int(settings.get("keep_recent_days", 90)),
        "max_raw_mb": float(settings.get("max_raw_mb", 500)),
        "archive_dir": str(settings.get("archive_dir", "journal/archive/raw")),
        "delete_archives": bool(settings.get("delete_archives", False)),
    }


def parse_timestamp(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    return None


def event_timestamp(event: dict[str, Any], fields: tuple[str, ...]) -> dt.datetime | None:
    for field in fields:
        timestamp = parse_timestamp(event.get(field))
        if timestamp is not None:
            return timestamp
    return None


def log_sources(config: dict[str, Any], root: Path = ROOT) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for name, target in LOG_TARGETS.items():
        source_settings = nested_dict(config, target["settings_path"])
        log_path = source_settings.get("log_path", target["default_log_path"])
        sources.append(
            {
                "name": name,
                "path": configured_path(str(log_path), root),
                "timestamp_fields": target["timestamp_fields"],
            }
        )
    return sources


def line_month(timestamp: dt.datetime, timezone: ZoneInfo | dt.tzinfo | None) -> str:
    local = timestamp.astimezone(timezone) if timezone else timestamp.astimezone()
    return local.strftime("%Y-%m")


def should_archive(timestamp: dt.datetime, cutoff_date: dt.date, timezone: ZoneInfo | dt.tzinfo | None) -> bool:
    local = timestamp.astimezone(timezone) if timezone else timestamp.astimezone()
    return local.date() < cutoff_date


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return [line.rstrip("\r\n").lstrip("\ufeff") for line in handle if line.strip()]


def read_gzip_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
        return [line.rstrip("\r\n").lstrip("\ufeff") for line in handle if line.strip()]


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ("\n".join(lines) + "\n") if lines else ""
    path.write_text(text, encoding="utf-8")


def write_gzip_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with gzip.open(tmp_path, "wt", encoding="utf-8", newline="\n") as handle:
            if lines:
                handle.write("\n".join(lines) + "\n")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def merge_lines(existing: list[str], additions: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for line in [*existing, *additions]:
        if not line or line in seen:
            continue
        seen.add(line)
        merged.append(line)
    return merged


def partition_source(
    source: dict[str, Any],
    *,
    cutoff_date: dt.date,
    timezone: ZoneInfo | dt.tzinfo | None,
) -> dict[str, Any]:
    lines = read_lines(source["path"])
    keep_lines: list[str] = []
    archive_by_month: dict[str, list[str]] = {}
    invalid_count = 0
    unknown_time_count = 0

    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            keep_lines.append(line)
            invalid_count += 1
            continue
        if not isinstance(event, dict):
            keep_lines.append(line)
            invalid_count += 1
            continue
        timestamp = event_timestamp(event, source["timestamp_fields"])
        if timestamp is None:
            keep_lines.append(line)
            unknown_time_count += 1
            continue
        if should_archive(timestamp, cutoff_date, timezone):
            archive_by_month.setdefault(line_month(timestamp, timezone), []).append(line)
        else:
            keep_lines.append(line)

    return {
        "source_line_count": len(lines),
        "keep_lines": keep_lines,
        "archive_by_month": archive_by_month,
        "archive_line_count": sum(len(items) for items in archive_by_month.values()),
        "invalid_count": invalid_count,
        "unknown_time_count": unknown_time_count,
    }


def archive_source(
    source: dict[str, Any],
    *,
    archive_dir: Path,
    cutoff_date: dt.date,
    timezone: ZoneInfo | dt.tzinfo | None,
    dry_run: bool,
) -> dict[str, Any]:
    source_path = source["path"]
    result: dict[str, Any] = {
        "name": source["name"],
        "path": display_path(source_path),
        "exists": source_path.exists(),
        "source_line_count": 0,
        "kept_line_count": 0,
        "archived_line_count": 0,
        "invalid_line_count": 0,
        "unknown_time_line_count": 0,
        "archives": [],
        "error": None,
    }
    if not source_path.exists():
        return result

    try:
        partition = partition_source(source, cutoff_date=cutoff_date, timezone=timezone)
        result["source_line_count"] = partition["source_line_count"]
        result["kept_line_count"] = len(partition["keep_lines"])
        result["archived_line_count"] = partition["archive_line_count"]
        result["invalid_line_count"] = partition["invalid_count"]
        result["unknown_time_line_count"] = partition["unknown_time_count"]

        for month, lines in sorted(partition["archive_by_month"].items()):
            archive_path = archive_dir / month / f"{source_path.name}.gz"
            archive_info = {
                "path": display_path(archive_path),
                "month": month,
                "added_line_count": len(lines),
            }
            result["archives"].append(archive_info)
            if dry_run:
                continue
            merged = merge_lines(read_gzip_lines(archive_path), lines)
            write_gzip_lines(archive_path, merged)

        if not dry_run and partition["archive_line_count"]:
            write_lines(source_path, partition["keep_lines"])
    except OSError as exc:
        result["error"] = str(exc)
    return result


def display_path(path: Path, root: Path = ROOT) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def total_size(paths: list[Path]) -> int:
    size = 0
    for path in paths:
        if path.exists():
            try:
                size += path.stat().st_size
            except OSError:
                pass
    return size


def database_retention_report(
    config: dict[str, Any],
    *,
    root: Path,
    cutoff_date: dt.date,
    dry_run: bool,
) -> dict[str, Any]:
    settings = activity_db.database_settings(config, root)
    path = settings["path"]
    if not settings["enabled"]:
        return {
            "enabled": False,
            "path": display_path(path, root),
            "status": "Skipped",
            "prunable_event_count": 0,
            "pruned_event_count": 0,
            "error": None,
        }
    if not path.exists():
        return {
            "enabled": True,
            "path": display_path(path, root),
            "status": "Skipped",
            "exists": False,
            "prunable_event_count": 0,
            "pruned_event_count": 0,
            "error": None,
        }
    try:
        conn = activity_db.connect_database(path)
        try:
            prunable = activity_db.count_prunable_text(conn, cutoff_date=cutoff_date)
            pruned = 0 if dry_run else activity_db.prune_old_text(conn, cutoff_date=cutoff_date)
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {
            "enabled": True,
            "path": display_path(path, root),
            "status": "Action Needed",
            "exists": True,
            "prunable_event_count": 0,
            "pruned_event_count": 0,
            "error": str(exc),
        }
    return {
        "enabled": True,
        "path": display_path(path, root),
        "status": "OK",
        "exists": True,
        "prunable_event_count": prunable,
        "pruned_event_count": pruned,
        "error": None,
    }


def cleanup_report(
    config: dict[str, Any],
    *,
    root: Path = ROOT,
    dry_run: bool = False,
    now_value: dt.datetime | None = None,
) -> dict[str, Any]:
    settings = retention_settings(config)
    generated_at = now(config, now_value).isoformat(timespec="seconds")
    if not settings["enabled"]:
        return {
            "report_type": "activity_journal_retention",
            "generated_at": generated_at,
            "status": "Skipped",
            "enabled": False,
            "dry_run": dry_run,
            "recommended_actions": [],
        }

    current = now(config, now_value)
    timezone = configured_timezone(config) or current.tzinfo
    cutoff_date = current.astimezone(timezone).date() - dt.timedelta(days=settings["keep_recent_days"])
    archive_dir = configured_path(settings["archive_dir"], root)
    sources = log_sources(config, root)
    source_reports = [
        archive_source(
            source,
            archive_dir=archive_dir,
            cutoff_date=cutoff_date,
            timezone=timezone,
            dry_run=dry_run,
        )
        for source in sources
    ]
    database_report = database_retention_report(config, root=root, cutoff_date=cutoff_date, dry_run=dry_run)

    raw_size_bytes = total_size([source["path"] for source in sources])
    raw_size_mb = raw_size_bytes / 1_048_576
    errors = [source for source in source_reports if source.get("error")]
    if database_report.get("error"):
        errors.append(database_report)
    status = "OK"
    actions: list[str] = []
    if errors:
        status = "Action Needed"
        actions.append("Inspect retention cleanup errors before relying on raw log archiving.")
    elif settings["max_raw_mb"] > 0 and raw_size_mb > settings["max_raw_mb"]:
        status = "Warning"
        actions.append("Review recent raw logs; recent retained logs exceed retention.max_raw_mb.")

    return {
        "report_type": "activity_journal_retention",
        "generated_at": generated_at,
        "status": status,
        "enabled": True,
        "dry_run": dry_run,
        "keep_recent_days": settings["keep_recent_days"],
        "cutoff_date": cutoff_date.isoformat(),
        "archive_dir": display_path(archive_dir, root),
        "delete_archives": settings["delete_archives"],
        "max_raw_mb": settings["max_raw_mb"],
        "raw_size_mb": round(raw_size_mb, 3),
        "archived_line_count": sum(int(source.get("archived_line_count") or 0) for source in source_reports),
        "sources": source_reports,
        "database": database_report,
        "recommended_actions": actions,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report what would be archived without rewriting logs.")
    parser.add_argument("--json-only", action="store_true", help="Print JSON only.")
    args = parser.parse_args()
    config = load_config()
    report = cleanup_report(config, dry_run=args.dry_run)
    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        action = "Would archive" if args.dry_run else "Archived"
        print(f"Retention cleanup: {report['status']}")
        if report.get("enabled"):
            print(f"{action} {report.get('archived_line_count', 0)} raw log line(s).")
            print(f"Raw log size: {report.get('raw_size_mb')} MB")
            database = report.get("database", {})
            if database.get("enabled") and database.get("exists"):
                db_action = "Would prune" if args.dry_run else "Pruned"
                count = database.get("prunable_event_count", 0) if args.dry_run else database.get("pruned_event_count", 0)
                print(f"{db_action} SQLite text for {count} event(s).")
    if report["status"] == "Action Needed":
        sys.exit(1)


if __name__ == "__main__":
    main()
