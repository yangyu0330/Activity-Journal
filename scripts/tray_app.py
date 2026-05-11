from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import capture_controls


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"
APP_NAME = "Activity Journal"
DEPENDENCIES = {
    "pystray": "pystray",
    "PIL": "Pillow",
}
STATE_COLORS = {
    "recording": (39, 174, 96, 255),
    "paused": (241, 196, 15, 255),
    "disabled": (149, 165, 166, 255),
    "error": (192, 57, 43, 255),
}


class TrayDependencyError(RuntimeError):
    pass


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return capture_controls.ensure_config_shape(capture_controls.load_config(path))


def save_config(config: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    capture_controls.save_config(capture_controls.ensure_config_shape(config), path)


def tray_settings(config: dict[str, Any]) -> dict[str, Any]:
    capture_controls.ensure_config_shape(config)
    tray = config.get("tray", {})
    return tray if isinstance(tray, dict) else dict(capture_controls.DEFAULT_TRAY)


def dependency_status() -> dict[str, Any]:
    dependencies = {
        module: {
            "package": package,
            "available": importlib.util.find_spec(module) is not None,
        }
        for module, package in DEPENDENCIES.items()
    }
    status = "OK" if all(item["available"] for item in dependencies.values()) else "Missing"
    return {
        "status": status,
        "dependencies": dependencies,
        "install_command": dependency_install_command(),
    }


def dependency_install_command() -> str:
    return "python -m pip install --user pystray pillow"


def import_tray_dependencies() -> tuple[Any, Any, Any]:
    missing = [package for module, package in DEPENDENCIES.items() if importlib.util.find_spec(module) is None]
    if missing:
        raise TrayDependencyError(f"Missing tray dependencies: {', '.join(missing)}. Run `{dependency_install_command()}`.")
    import pystray
    from PIL import Image, ImageDraw

    return pystray, Image, ImageDraw


def capture_state(config: dict[str, Any]) -> str:
    status = capture_controls.status(config)
    if status["active"]:
        return "recording"
    if status.get("privacy_mode_active"):
        return "paused"
    return "disabled"


def short_reason(reason: str, limit: int = 72) -> str:
    reason = " ".join(reason.split())
    if len(reason) <= limit:
        return reason
    return reason[: limit - 3].rstrip() + "..."


def status_label(config: dict[str, Any] | None = None) -> str:
    current = load_config() if config is None else config
    status = capture_controls.status(current)
    if status["active"]:
        return f"{APP_NAME}: recording"
    reason = short_reason(status.get("reason") or "paused")
    return f"{APP_NAME}: paused ({reason})"


def status_payload(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = load_config(config_path)
    payload = capture_controls.status(config)
    payload["tray"] = tray_settings(config)
    payload["state"] = capture_state(config)
    return payload


def apply_pause(minutes: int, config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = load_config(config_path)
    capture_controls.pause_for(config, dt.timedelta(minutes=max(1, minutes)), reason=f"tray pause {max(1, minutes)} min")
    save_config(config, config_path)
    return status_payload(config_path)


def apply_pause_today(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = load_config(config_path)
    capture_controls.pause_until_end_of_day(config, reason="tray pause today")
    save_config(config, config_path)
    return status_payload(config_path)


def apply_resume(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = load_config(config_path)
    capture_controls.resume(config)
    save_config(config, config_path)
    return status_payload(config_path)


def pythonw_path() -> str:
    current = Path(sys.executable)
    if os.name == "nt":
        candidate = current.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
        found = shutil.which("pythonw.exe")
        if found:
            return found
    return sys.executable


def launch_settings() -> subprocess.Popen[str]:
    command = [pythonw_path(), str(ROOT / "scripts" / "settings_app.py")]
    kwargs: dict[str, Any] = {"cwd": ROOT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(command, **kwargs)


def launch_dashboard() -> subprocess.Popen[str]:
    command = [pythonw_path(), str(ROOT / "scripts" / "dashboard_app.py"), "--quiet"]
    kwargs: dict[str, Any] = {"cwd": ROOT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(command, **kwargs)


def health_summary(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    import project_health

    config = load_config(config_path)
    day = project_health.parse_date(None, config)
    report = project_health.build_report(day, config, include_question_quality=False)
    return {
        "overall": report.get("overall"),
        "checked_date": report.get("checked_date"),
        "recommended_action_count": len(report.get("recommended_actions", [])),
    }


def create_icon_image(state: str, image_module: Any, draw_module: Any) -> Any:
    image = image_module.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = draw_module.Draw(image)
    color = STATE_COLORS.get(state, STATE_COLORS["error"])
    draw.ellipse((7, 7, 57, 57), fill=color, outline=(255, 255, 255, 255), width=3)
    if state == "recording":
        draw.ellipse((25, 25, 39, 39), fill=(255, 255, 255, 255))
    elif state == "paused":
        draw.rectangle((23, 20, 29, 44), fill=(255, 255, 255, 255))
        draw.rectangle((35, 20, 41, 44), fill=(255, 255, 255, 255))
    elif state == "disabled":
        draw.line((21, 43, 43, 21), fill=(255, 255, 255, 255), width=6)
    else:
        draw.line((22, 22, 42, 42), fill=(255, 255, 255, 255), width=5)
        draw.line((42, 22, 22, 42), fill=(255, 255, 255, 255), width=5)
    return image


class TrayController:
    def __init__(self, config_path: Path = CONFIG_PATH) -> None:
        self.config_path = config_path
        self.pystray, self.image_module, self.draw_module = import_tray_dependencies()
        self.stop_event = threading.Event()

    def current_config(self) -> dict[str, Any]:
        return load_config(self.config_path)

    def make_icon_image(self) -> Any:
        return create_icon_image(capture_state(self.current_config()), self.image_module, self.draw_module)

    def refresh(self, icon: Any) -> None:
        config = self.current_config()
        icon.title = status_label(config)
        icon.icon = create_icon_image(capture_state(config), self.image_module, self.draw_module)
        icon.menu = self.build_menu()

    def notify(self, icon: Any, title: str, message: str) -> None:
        if not bool(tray_settings(self.current_config()).get("show_notifications", True)):
            return
        try:
            icon.notify(message, title)
        except Exception:
            return

    def action(self, callback: Any, message: str) -> Any:
        def handler(icon: Any, _item: Any) -> None:
            try:
                callback()
                self.refresh(icon)
                self.notify(icon, APP_NAME, message)
            except Exception as exc:
                self.notify(icon, f"{APP_NAME} error", str(exc))

        return handler

    def open_settings(self, icon: Any, _item: Any) -> None:
        try:
            launch_settings()
            self.notify(icon, APP_NAME, "Settings opened.")
        except Exception as exc:
            self.notify(icon, f"{APP_NAME} error", str(exc))

    def open_dashboard(self, icon: Any, _item: Any) -> None:
        try:
            launch_dashboard()
            self.notify(icon, APP_NAME, "Dashboard opened.")
        except Exception as exc:
            self.notify(icon, f"{APP_NAME} error", str(exc))

    def run_health_check(self, icon: Any, _item: Any) -> None:
        try:
            summary = health_summary(self.config_path)
            self.notify(
                icon,
                APP_NAME,
                f"Health: {summary['overall']} for {summary['checked_date']} ({summary['recommended_action_count']} action(s)).",
            )
        except Exception as exc:
            self.notify(icon, f"{APP_NAME} error", str(exc))

    def refresh_from_menu(self, icon: Any, _item: Any) -> None:
        self.refresh(icon)

    def stop(self, icon: Any, _item: Any) -> None:
        self.stop_event.set()
        icon.stop()

    def build_menu(self) -> Any:
        return self.pystray.Menu(
            self.pystray.MenuItem(status_label(self.current_config()), self.refresh_from_menu, enabled=False),
            self.pystray.Menu.SEPARATOR,
            self.pystray.MenuItem("Open Dashboard", self.open_dashboard),
            self.pystray.MenuItem("Open Settings", self.open_settings),
            self.pystray.MenuItem("Refresh Status", self.refresh_from_menu),
            self.pystray.MenuItem("Run Health Check", self.run_health_check),
            self.pystray.Menu.SEPARATOR,
            self.pystray.MenuItem("Pause 15 min", self.action(lambda: apply_pause(15, self.config_path), "Capture paused for 15 minutes.")),
            self.pystray.MenuItem("Pause 30 min", self.action(lambda: apply_pause(30, self.config_path), "Capture paused for 30 minutes.")),
            self.pystray.MenuItem("Pause 1 hour", self.action(lambda: apply_pause(60, self.config_path), "Capture paused for 1 hour.")),
            self.pystray.MenuItem("Pause today", self.action(lambda: apply_pause_today(self.config_path), "Capture paused until tomorrow.")),
            self.pystray.MenuItem("Resume now", self.action(lambda: apply_resume(self.config_path), "Capture resumed.")),
            self.pystray.Menu.SEPARATOR,
            self.pystray.MenuItem("Exit Tray", self.stop),
        )

    def refresh_loop(self, icon: Any) -> None:
        while not self.stop_event.wait(60):
            try:
                self.refresh(icon)
            except Exception:
                continue

    def run(self) -> int:
        config = self.current_config()
        if not bool(tray_settings(config).get("enabled", True)):
            print("Activity Journal tray is disabled in config.")
            return 0
        icon = self.pystray.Icon(APP_NAME, self.make_icon_image(), status_label(config), self.build_menu())
        threading.Thread(target=self.refresh_loop, args=(icon,), daemon=True).start()
        icon.run()
        return 0


def run_tray(config_path: Path = CONFIG_PATH) -> int:
    controller = TrayController(config_path)
    return controller.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(CONFIG_PATH))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check-deps", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--resume", action="store_true")
    group.add_argument("--pause-minutes", type=int)
    group.add_argument("--pause-today", action="store_true")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if args.check_deps:
        result = dependency_status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "OK" else 1
    if args.status:
        print(json.dumps(status_payload(config_path), ensure_ascii=False, indent=2))
        return 0
    if args.resume:
        print(json.dumps(apply_resume(config_path), ensure_ascii=False, indent=2))
        return 0
    if args.pause_minutes is not None:
        print(json.dumps(apply_pause(args.pause_minutes, config_path), ensure_ascii=False, indent=2))
        return 0
    if args.pause_today:
        print(json.dumps(apply_pause_today(config_path), ensure_ascii=False, indent=2))
        return 0
    try:
        return run_tray(config_path)
    except TrayDependencyError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
