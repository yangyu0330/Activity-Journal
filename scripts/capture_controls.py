from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"

DEFAULT_CAPTURE = {
    "enabled": True,
    "privacy_mode_until": None,
    "privacy_mode_reason": "",
}

DEFAULT_TRAY = {
    "enabled": True,
    "show_notifications": True,
}

SOURCE_PATHS = {
    "activity_watch": ("external_inputs", "activity_watch", "enabled"),
    "chatgpt_live": ("external_inputs", "chatgpt_live", "enabled"),
    "browser_history": ("external_inputs", "browser_history", "enabled"),
    "recent_files": ("external_inputs", "recent_files", "enabled"),
    "notion": ("notion", "enabled"),
}

EXCLUSION_KEYS = {
    "raw_block_domains",
    "raw_block_apps",
    "summary_hide_domains",
    "summary_hide_apps",
}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(config: dict[str, Any], path: Path = CONFIG_PATH, *, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        backup_path.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, prefix=path.name, suffix=".tmp") as handle:
        handle.write(payload)
        tmp_path = Path(handle.name)
    tmp_path.replace(path)


def configured_timezone(config: dict[str, Any]) -> ZoneInfo | None:
    timezone = config.get("timezone")
    if not timezone:
        return None
    try:
        return ZoneInfo(str(timezone))
    except ZoneInfoNotFoundError:
        return None


def now(config: dict[str, Any] | None = None) -> dt.datetime:
    timezone = configured_timezone(config or {})
    return dt.datetime.now(timezone or dt.timezone.utc).astimezone(timezone or dt.timezone.utc)


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


def ensure_config_shape(config: dict[str, Any]) -> dict[str, Any]:
    capture = config.get("capture")
    if not isinstance(capture, dict):
        capture = {}
    for key, value in DEFAULT_CAPTURE.items():
        capture.setdefault(key, value)
    config["capture"] = capture

    tray = config.get("tray")
    if not isinstance(tray, dict):
        tray = {}
    for key, value in DEFAULT_TRAY.items():
        tray.setdefault(key, value)
    config["tray"] = tray

    external = config.get("external_inputs")
    if not isinstance(external, dict):
        external = {}
    external.setdefault("enabled", True)
    for source in ["activity_watch", "chatgpt_live", "browser_history", "recent_files"]:
        settings = external.get(source)
        if not isinstance(settings, dict):
            settings = {}
        settings.setdefault("enabled", True)
        external[source] = settings
    config["external_inputs"] = external

    notion = config.get("notion")
    if not isinstance(notion, dict):
        notion = {}
    notion.setdefault("enabled", False)
    config["notion"] = notion

    privacy = config.get("privacy")
    if not isinstance(privacy, dict):
        privacy = {}
    exclusions = privacy.get("exclusions")
    if not isinstance(exclusions, dict):
        exclusions = {}
    for key in EXCLUSION_KEYS:
        values = exclusions.get(key)
        exclusions[key] = values if isinstance(values, list) else []
    privacy["exclusions"] = exclusions
    config["privacy"] = privacy
    return config


def config_value(config: dict[str, Any], path: tuple[str, ...], default: Any = None) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def set_config_value(config: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current: dict[str, Any] = config
    for key in path[:-1]:
        next_value = current.get(key)
        if not isinstance(next_value, dict):
            next_value = {}
            current[key] = next_value
        current = next_value
    current[path[-1]] = value


def privacy_mode_until(config: dict[str, Any]) -> dt.datetime | None:
    return parse_timestamp(config_value(config, ("capture", "privacy_mode_until")))


def privacy_mode_active(config: dict[str, Any], now_value: dt.datetime | None = None) -> bool:
    until = privacy_mode_until(config)
    if until is None:
        return False
    current = now_value or now(config)
    comparable = current if current.tzinfo else current.replace(tzinfo=dt.timezone.utc)
    return comparable.timestamp() < until.timestamp()


def capture_active(config: dict[str, Any], now_value: dt.datetime | None = None) -> bool:
    capture = config.get("capture", {})
    if isinstance(capture, dict) and not bool(capture.get("enabled", True)):
        return False
    return not privacy_mode_active(config, now_value)


def capture_pause_reason(config: dict[str, Any], now_value: dt.datetime | None = None) -> str:
    capture = config.get("capture", {}) if isinstance(config.get("capture"), dict) else {}
    if not bool(capture.get("enabled", True)):
        return "capture disabled"
    if privacy_mode_active(config, now_value):
        until = privacy_mode_until(config)
        reason = str(capture.get("privacy_mode_reason") or "privacy mode").strip()
        return f"{reason} until {until.isoformat(timespec='seconds') if until else 'unknown'}"
    return ""


def source_enabled(config: dict[str, Any], source: str) -> bool:
    path = SOURCE_PATHS.get(source)
    if path is None:
        return False
    if source != "notion" and not bool(config_value(config, ("external_inputs", "enabled"), True)):
        return False
    return bool(config_value(config, path, True))


def should_capture_source(config: dict[str, Any], source: str, now_value: dt.datetime | None = None) -> bool:
    return capture_active(config, now_value) and source_enabled(config, source)


def set_capture_enabled(config: dict[str, Any], enabled: bool) -> dict[str, Any]:
    ensure_config_shape(config)
    config["capture"]["enabled"] = bool(enabled)
    if enabled:
        config["capture"]["privacy_mode_until"] = None
        config["capture"]["privacy_mode_reason"] = ""
    return config


def set_source_enabled(config: dict[str, Any], source: str, enabled: bool) -> dict[str, Any]:
    ensure_config_shape(config)
    if source not in SOURCE_PATHS:
        raise ValueError(f"unknown source: {source}")
    set_config_value(config, SOURCE_PATHS[source], bool(enabled))
    return config


def pause_for(config: dict[str, Any], duration: dt.timedelta, *, reason: str = "privacy mode") -> dict[str, Any]:
    ensure_config_shape(config)
    until = now(config) + duration
    config["capture"]["privacy_mode_until"] = until.isoformat(timespec="seconds")
    config["capture"]["privacy_mode_reason"] = reason
    return config


def pause_until_end_of_day(config: dict[str, Any], *, reason: str = "privacy mode") -> dict[str, Any]:
    ensure_config_shape(config)
    current = now(config)
    until = dt.datetime.combine(current.date() + dt.timedelta(days=1), dt.time.min, tzinfo=current.tzinfo)
    config["capture"]["privacy_mode_until"] = until.isoformat(timespec="seconds")
    config["capture"]["privacy_mode_reason"] = reason
    return config


def resume(config: dict[str, Any]) -> dict[str, Any]:
    ensure_config_shape(config)
    config["capture"]["enabled"] = True
    config["capture"]["privacy_mode_until"] = None
    config["capture"]["privacy_mode_reason"] = ""
    return config


def exclusion_values(config: dict[str, Any], key: str) -> list[str]:
    ensure_config_shape(config)
    values = config["privacy"]["exclusions"].get(key, [])
    return [str(value).strip() for value in values if str(value).strip()] if isinstance(values, list) else []


def add_exclusion(config: dict[str, Any], key: str, value: str) -> dict[str, Any]:
    ensure_config_shape(config)
    if key not in EXCLUSION_KEYS:
        raise ValueError(f"unknown exclusion key: {key}")
    cleaned = value.strip()
    if not cleaned:
        return config
    values = exclusion_values(config, key)
    if cleaned.lower() not in {item.lower() for item in values}:
        values.append(cleaned)
    config["privacy"]["exclusions"][key] = values
    return config


def remove_exclusion(config: dict[str, Any], key: str, value: str) -> dict[str, Any]:
    ensure_config_shape(config)
    if key not in EXCLUSION_KEYS:
        raise ValueError(f"unknown exclusion key: {key}")
    target = value.strip().lower()
    config["privacy"]["exclusions"][key] = [item for item in exclusion_values(config, key) if item.lower() != target]
    return config


def status(config: dict[str, Any], now_value: dt.datetime | None = None) -> dict[str, Any]:
    ensure_config_shape(config)
    active = capture_active(config, now_value)
    return {
        "active": active,
        "reason": "" if active else capture_pause_reason(config, now_value),
        "privacy_mode_active": privacy_mode_active(config, now_value),
        "privacy_mode_until": config["capture"].get("privacy_mode_until"),
        "sources": {source: source_enabled(config, source) for source in SOURCE_PATHS},
    }


def validate_config(config: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    ensure_config_shape(config)
    until = config["capture"].get("privacy_mode_until")
    if until and parse_timestamp(until) is None:
        warnings.append("capture.privacy_mode_until is not a valid ISO timestamp.")
    for key in DEFAULT_TRAY:
        if not isinstance(config["tray"].get(key), bool):
            warnings.append(f"tray.{key} must be a boolean.")
    for key in EXCLUSION_KEYS:
        if not isinstance(config["privacy"]["exclusions"].get(key), list):
            warnings.append(f"privacy.exclusions.{key} must be a list.")
    return warnings


def current_pythonw() -> str:
    return "pythonw.exe" if os.name == "nt" else "python"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(CONFIG_PATH))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("resume")
    pause_parser = subparsers.add_parser("pause")
    pause_parser.add_argument("--minutes", type=int, default=30)
    set_parser = subparsers.add_parser("set-enabled")
    set_parser.add_argument("enabled", choices=["true", "false"])
    source_parser = subparsers.add_parser("set-source")
    source_parser.add_argument("source", choices=sorted(SOURCE_PATHS))
    source_parser.add_argument("enabled", choices=["true", "false"])
    args = parser.parse_args()

    path = Path(args.config)
    config = ensure_config_shape(load_config(path))
    if args.command == "status":
        print(json.dumps(status(config), ensure_ascii=False, indent=2))
        return
    if args.command == "resume":
        resume(config)
    elif args.command == "pause":
        pause_for(config, dt.timedelta(minutes=max(1, args.minutes)))
    elif args.command == "set-enabled":
        set_capture_enabled(config, args.enabled == "true")
    elif args.command == "set-source":
        set_source_enabled(config, args.source, args.enabled == "true")
    save_config(config, path)
    print(json.dumps(status(config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
