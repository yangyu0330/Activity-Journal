from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import capture_controls
import privacy_filters


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_LOG_PATH = ROOT / "journal" / "raw" / "chatgpt_live.jsonl"
EXTENSION_ARTIFACT_ENV = "ACTIVITY_JOURNAL_EXTENSION_ARTIFACT_DIR"
EXTENSION_UPDATE_XML_NAME = "chatgpt-live-capture-update.xml"
EXTENSION_CRX_NAME = "chatgpt-live-capture.crx"
LEGACY_EXTENSION_UPDATE_XML_PATH = ROOT / "browser_extension" / EXTENSION_UPDATE_XML_NAME
LEGACY_EXTENSION_CRX_PATH = ROOT / "browser_extension" / EXTENSION_CRX_NAME
MAX_BODY_BYTES = 2_000_000


def extension_artifact_dir() -> Path:
    configured = os.environ.get(EXTENSION_ARTIFACT_ENV)
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ActivityJournal" / "browser_extension"
    return Path.home() / ".activity-journal" / "browser_extension"


def extension_artifact_path(file_name: str, legacy_path: Path) -> Path:
    artifact_path = extension_artifact_dir() / file_name
    if artifact_path.is_file():
        return artifact_path
    return legacy_path


def extension_update_xml_path() -> Path:
    return extension_artifact_path(EXTENSION_UPDATE_XML_NAME, LEGACY_EXTENSION_UPDATE_XML_PATH)


def extension_crx_path() -> Path:
    return extension_artifact_path(EXTENSION_CRX_NAME, LEGACY_EXTENSION_CRX_PATH)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text") or "").strip()
    url = str(payload.get("url") or "").strip()
    title = str(payload.get("title") or "Untitled ChatGPT conversation").strip()
    app = str(payload.get("app") or "chatgpt").strip()
    captured_at = str(payload.get("captured_at") or utc_now())
    content_hash = str(payload.get("content_hash") or stable_hash(f"{url}\n{text}"))
    return {
        "captured_at": captured_at,
        "received_at": utc_now(),
        "source": "browser_extension",
        "app": app[:80],
        "title": title[:240],
        "url": url[:2000],
        "conversation_id": str(payload.get("conversation_id") or "")[:240],
        "content_hash": content_hash,
        "text": text,
    }


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def should_store_event(config: dict[str, Any], event: dict[str, Any]) -> bool:
    return (
        bool(event.get("text"))
        and capture_controls.should_capture_source(config, "chatgpt_live")
        and not privacy_filters.should_block_raw_chat_event(config, event)
    )


class LiveCaptureHandler(BaseHTTPRequestHandler):
    server_version = "ActivityJournalChatGPTLive/1.0"

    def do_OPTIONS(self) -> None:
        self.send_cors_response(204, b"")

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_cors_response(200, b'{"ok":true}')
            return
        if self.path == "/extension/update.xml":
            self.send_file_response(extension_update_xml_path(), "application/xml; charset=utf-8")
            return
        if self.path == "/extension/chatgpt-live-capture.crx":
            self.send_file_response(extension_crx_path(), "application/x-chrome-extension")
            return
        if self.path.startswith("/extension-test"):
            body = (
                "<!doctype html><html><head><title>Activity Journal Extension Test</title></head>"
                "<body><main><h1>Activity Journal Extension Test</h1>"
                "<article>ACTIVITY JOURNAL EXTENSION TEST visible study text 918273645 "
                "ChatGPT Gemini capture verification page.</article></main></body></html>"
            ).encode("utf-8")
            self.send_cors_response(200, body, content_type="text/html; charset=utf-8")
            return
        self.send_cors_response(404, b'{"error":"not found"}')

    def do_POST(self) -> None:
        if self.path != "/events":
            self.send_cors_response(404, b'{"error":"not found"}')
            return
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_cors_response(413, b'{"error":"payload too large"}')
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self.send_cors_response(400, b'{"error":"invalid json"}')
            return
        if not isinstance(payload, dict):
            self.send_cors_response(400, b'{"error":"expected object"}')
            return
        event = normalize_payload(payload)
        config = privacy_filters.load_config(CONFIG_PATH)
        if not should_store_event(config, event):
            self.send_cors_response(204, b"")
            return
        append_jsonl(self.server.log_path, event)  # type: ignore[attr-defined]
        self.send_cors_response(202, b'{"ok":true}')

    def send_cors_response(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_file_response(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_cors_response(404, b'{"error":"not found"}')
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def run_server(host: str, port: int, log_path: Path) -> None:
    server = ThreadingHTTPServer((host, port), LiveCaptureHandler)
    server.log_path = log_path  # type: ignore[attr-defined]
    print(f"ChatGPT live capture server listening on http://{host}:{port}")
    print(f"Writing events to {log_path}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH))
    args = parser.parse_args()
    run_server(args.host, args.port, Path(args.log))


if __name__ == "__main__":
    main()
