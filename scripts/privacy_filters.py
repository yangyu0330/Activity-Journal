from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def exclusion_values(config: dict[str, Any], key: str) -> list[str]:
    privacy = config.get("privacy", {})
    if not isinstance(privacy, dict):
        return []
    exclusions = privacy.get("exclusions", {})
    if not isinstance(exclusions, dict):
        return []
    values = exclusions.get(key, [])
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def domain_from_value(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    parsed = urllib.parse.urlparse(text if "://" in text else f"//{text}")
    domain = parsed.netloc or parsed.path.split("/", 1)[0]
    if "@" in domain:
        domain = domain.rsplit("@", 1)[1]
    domain = domain.split(":", 1)[0].strip(".")
    return domain[4:] if domain.startswith("www.") else domain


def domains_match(domain: str, patterns: list[str]) -> bool:
    normalized = domain_from_value(domain)
    if not normalized:
        return False
    for pattern in patterns:
        candidate = domain_from_value(pattern)
        if candidate and (normalized == candidate or normalized.endswith(f".{candidate}")):
            return True
    return False


def apps_match(app: Any, patterns: list[str]) -> bool:
    normalized = str(app or "").strip().lower()
    if not normalized:
        return False
    return normalized in {str(pattern).strip().lower() for pattern in patterns if str(pattern).strip()}


def item_domain(item: dict[str, Any]) -> str:
    for key in ["url", "domain", "app"]:
        domain = domain_from_value(item.get(key))
        if domain:
            return domain
    return ""


def should_block_raw_chat_event(config: dict[str, Any], event: dict[str, Any]) -> bool:
    return domains_match(item_domain(event), exclusion_values(config, "raw_block_domains"))


def should_block_raw_activity_event(config: dict[str, Any], event: dict[str, Any]) -> bool:
    return apps_match(event.get("process"), exclusion_values(config, "raw_block_apps"))


def should_hide_summary_domain(config: dict[str, Any], item: dict[str, Any]) -> bool:
    return domains_match(item_domain(item), exclusion_values(config, "summary_hide_domains"))


def should_hide_summary_app(config: dict[str, Any], item: dict[str, Any]) -> bool:
    return apps_match(item.get("process") or item.get("app"), exclusion_values(config, "summary_hide_apps"))


def should_exclude_raw_domain(config: dict[str, Any], item: dict[str, Any]) -> bool:
    return domains_match(item_domain(item), exclusion_values(config, "raw_block_domains"))


def should_exclude_raw_app(config: dict[str, Any], item: dict[str, Any]) -> bool:
    return apps_match(item.get("process") or item.get("app"), exclusion_values(config, "raw_block_apps"))
