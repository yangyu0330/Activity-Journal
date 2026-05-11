from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

import capture_controls
import privacy_filters

try:
    import uiautomation as auto
except Exception:  # pragma: no cover - optional Windows accessibility dependency.
    auto = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"
DEFAULT_LOG_PATH = ROOT / "journal" / "raw" / "activity_watch.jsonl"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DEFAULT_TEXT_PROCESSES = {
    "chrome.exe",
    "msedge.exe",
    "brave.exe",
    "vivaldi.exe",
    "firefox.exe",
    "notion.exe",
    "code.exe",
    "winword.exe",
    "powerpnt.exe",
    "excel.exe",
    "acrobat.exe",
    "acrord32.exe",
    "acrord64.exe",
    "hwp.exe",
}
TEXT_CONTROL_TYPES = {
    "DocumentControl",
    "EditControl",
    "TextControl",
    "ListItemControl",
    "TreeItemControl",
    "DataItemControl",
    "HyperlinkControl",
    "TabItemControl",
    "CustomControl",
    "GroupControl",
}


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def foreground_window(
    *,
    include_accessibility_text: bool = False,
    text_processes: set[str] | None = None,
    max_text_chars: int = 20000,
    max_text_nodes: int = 300,
) -> dict[str, Any]:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {"ts": utc_now(), "title": "", "process_id": None, "process": ""}

    title_length = user32.GetWindowTextLengthW(hwnd)
    title_buffer = ctypes.create_unicode_buffer(title_length + 1)
    user32.GetWindowTextW(hwnd, title_buffer, title_length + 1)

    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    process_path = process_image_path(int(process_id.value))
    process_name = Path(process_path).name if process_path else ""

    event: dict[str, Any] = {
        "ts": utc_now(),
        "title": title_buffer.value,
        "process_id": int(process_id.value) if process_id.value else None,
        "process": process_name,
    }
    if include_accessibility_text and should_capture_accessibility_text(process_name, text_processes):
        text, text_error = foreground_accessibility_text(int(hwnd), max_text_chars=max_text_chars, max_nodes=max_text_nodes)
        if text:
            event["text"] = text
            event["text_chars"] = len(text)
            event["text_hash"] = stable_hash(f"{process_name}\n{title_buffer.value}\n{text}")
        elif text_error:
            event["text_error"] = text_error
    return event


def should_capture_accessibility_text(process_name: str, text_processes: set[str] | None) -> bool:
    if text_processes is None:
        text_processes = DEFAULT_TEXT_PROCESSES
    if not text_processes:
        return True
    return process_name.lower() in {name.lower() for name in text_processes}


def foreground_accessibility_text(hwnd: int, *, max_text_chars: int, max_nodes: int) -> tuple[str, str | None]:
    if auto is None:
        return "", "uiautomation package is not installed"
    try:
        auto.SetGlobalSearchTimeout(1)
    except Exception:
        pass
    try:
        control = auto.ControlFromHandle(hwnd)
    except Exception as exc:
        return "", f"accessibility root unavailable: {exc}"

    parts: list[str] = []
    seen_texts: set[str] = set()
    queue = [control]
    visited = 0
    while queue and visited < max_nodes and sum(len(part) for part in parts) < max_text_chars:
        current = queue.pop(0)
        visited += 1
        try:
            control_type = str(getattr(current, "ControlTypeName", "") or "")
        except Exception:
            control_type = ""
        for candidate in control_text_candidates(current, control_type, max_text_chars - sum(len(part) for part in parts)):
            text = normalize_text(candidate)
            if text and text not in seen_texts:
                parts.append(text)
                seen_texts.add(text)
        try:
            queue.extend(current.GetChildren())
        except Exception:
            continue

    text = "\n".join(parts).strip()
    if len(text) > max_text_chars:
        text = text[: max_text_chars - 3].rstrip() + "..."
    return text, None


def control_text_candidates(control: Any, control_type: str, remaining_chars: int) -> list[str]:
    candidates: list[str] = []
    if remaining_chars <= 0:
        return candidates
    if control_type in TEXT_CONTROL_TYPES:
        try:
            candidates.append(str(getattr(control, "Name", "") or ""))
        except Exception:
            pass
    if auto is None:
        return candidates
    try:
        text_pattern = control.GetPattern(auto.PatternId.TextPattern)
        if text_pattern:
            candidates.append(str(text_pattern.DocumentRange.GetText(max(1, remaining_chars)) or ""))
    except Exception:
        pass
    try:
        value_pattern = control.GetPattern(auto.PatternId.ValuePattern)
        if value_pattern:
            candidates.append(str(value_pattern.Value or ""))
    except Exception:
        pass
    return candidates


def normalize_text(value: str) -> str:
    text = reflow_whitespace(value)
    if len(text) < 2:
        return ""
    return text


def reflow_whitespace(value: str) -> str:
    return " ".join(value.replace("\r", "\n").split())


def process_image_path(process_id: int) -> str:
    if process_id <= 0:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def should_store_event(config: dict[str, Any], event: dict[str, Any]) -> bool:
    return capture_controls.should_capture_source(config, "activity_watch") and not privacy_filters.should_block_raw_activity_event(config, event)


def parse_processes(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def watch(
    path: Path,
    interval_seconds: int,
    heartbeat_seconds: int,
    *,
    include_accessibility_text: bool = False,
    text_processes: set[str] | None = None,
    max_text_chars: int = 20000,
    max_text_nodes: int = 300,
) -> None:
    last_signature: tuple[str, str, str] | None = None
    last_write = 0.0
    while True:
        event = foreground_window(
            include_accessibility_text=include_accessibility_text,
            text_processes=text_processes,
            max_text_chars=max_text_chars,
            max_text_nodes=max_text_nodes,
        )
        signature = (
            str(event.get("process") or ""),
            str(event.get("title") or ""),
            str(event.get("text_hash") or ""),
        )
        now = time.monotonic()
        if signature != last_signature or now - last_write >= heartbeat_seconds:
            config = privacy_filters.load_config(CONFIG_PATH)
            if should_store_event(config, event):
                append_event(path, event)
                last_write = now
            last_signature = signature
        time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH), help="JSONL log path.")
    parser.add_argument("--interval", type=int, default=30, help="Polling interval in seconds.")
    parser.add_argument("--heartbeat", type=int, default=300, help="Write unchanged active window at least this often.")
    parser.add_argument("--include-accessibility-text", action="store_true", help="Capture visible accessibility text for selected foreground apps.")
    parser.add_argument(
        "--text-processes",
        default=",".join(sorted(DEFAULT_TEXT_PROCESSES)),
        help="Comma-separated process names eligible for accessibility text capture. Empty means all processes.",
    )
    parser.add_argument("--max-text-chars", type=int, default=20000, help="Maximum visible text characters stored per foreground event.")
    parser.add_argument("--max-text-nodes", type=int, default=300, help="Maximum accessibility nodes inspected per foreground event.")
    parser.add_argument("--once", action="store_true", help="Write one foreground-window event and exit.")
    args = parser.parse_args()

    log_path = Path(args.log)
    text_processes = parse_processes(args.text_processes)
    if args.once:
        event = foreground_window(
            include_accessibility_text=args.include_accessibility_text,
            text_processes=text_processes,
            max_text_chars=max(100, args.max_text_chars),
            max_text_nodes=max(10, args.max_text_nodes),
        )
        if should_store_event(privacy_filters.load_config(CONFIG_PATH), event):
            append_event(log_path, event)
        return
    watch(
        log_path,
        max(1, args.interval),
        max(1, args.heartbeat),
        include_accessibility_text=args.include_accessibility_text,
        text_processes=text_processes,
        max_text_chars=max(100, args.max_text_chars),
        max_text_nodes=max(10, args.max_text_nodes),
    )


if __name__ == "__main__":
    main()
