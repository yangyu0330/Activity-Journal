from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

import activity_db
import capture_controls
import project_health
import tray_app


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"

SOURCE_LABELS = {
    "activity_watch": "Active app/window",
    "chatgpt_live": "ChatGPT/Gemini live",
    "browser_history": "Browser history",
    "recent_files": "Recent files",
    "notion": "Notion sync",
}

EXCLUSION_LABELS = {
    "raw_block_domains": "Do not store domains",
    "raw_block_apps": "Do not store apps",
    "summary_hide_domains": "Hide domains from daily/Notion",
    "summary_hide_apps": "Hide apps from daily/Notion",
}


def load_config() -> dict[str, Any]:
    return capture_controls.ensure_config_shape(capture_controls.load_config(CONFIG_PATH))


def save_config(config: dict[str, Any]) -> None:
    capture_controls.save_config(capture_controls.ensure_config_shape(config), CONFIG_PATH)


def config_status_text(config: dict[str, Any]) -> str:
    status = capture_controls.status(config)
    lines = [
        f"Capture: {'running' if status['active'] else 'paused'}",
    ]
    if not status["active"]:
        lines.append(f"Reason: {status['reason']}")
    lines.append("")
    lines.append("Sources:")
    for source, enabled in status["sources"].items():
        lines.append(f"- {SOURCE_LABELS.get(source, source)}: {'on' if enabled else 'off'}")

    db = activity_db.inspect_database(config, root=ROOT, day=project_health.parse_date(None, config))
    if db.get("enabled"):
        lines.extend(
            [
                "",
                f"SQLite: {db.get('status')} ({db.get('path')})",
                f"Indexed events: {db.get('event_count', 0)}",
                f"DB size: {db.get('size_mb', 0)} MB",
            ]
        )
    tray = config.get("tray", {})
    deps = tray_app.dependency_status()
    lines.extend(
        [
            "",
            f"Tray: {'on' if tray.get('enabled', True) else 'off'}",
            f"Tray notifications: {'on' if tray.get('show_notifications', True) else 'off'}",
            f"Tray dependencies: {deps.get('status')}",
        ]
    )
    return "\n".join(lines)


class SettingsApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Activity Journal Settings")
        self.root.geometry("760x560")
        self.config_data = load_config()
        self.source_vars: dict[str, tk.BooleanVar] = {}
        self.exclusion_lists: dict[str, tk.Listbox] = {}
        self.exclusion_entries: dict[str, tk.Entry] = {}

        self.capture_var = tk.BooleanVar(value=bool(self.config_data["capture"].get("enabled", True)))
        self.status_text = tk.StringVar(value=config_status_text(self.config_data))

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)
        self.build_status_tab()
        self.build_recording_tab()
        self.build_privacy_tab()
        self.build_exclusions_tab()
        self.build_install_tab()

    def build_status_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="Status")
        text = tk.Text(frame, height=22, wrap="word")
        text.pack(fill="both", expand=True)
        text.insert("1.0", self.status_text.get())
        text.configure(state="disabled")
        self.status_widget = text
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Refresh", command=self.refresh_status).pack(side="left")
        ttk.Button(buttons, text="Run Health Check", command=self.run_health_check).pack(side="left", padx=(8, 0))

    def build_recording_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="Recording")
        ttk.Checkbutton(frame, text="Enable capture", variable=self.capture_var).pack(anchor="w")

        source_frame = ttk.LabelFrame(frame, text="Sources", padding=10)
        source_frame.pack(fill="x", pady=(14, 0))
        for source, label in SOURCE_LABELS.items():
            var = tk.BooleanVar(value=capture_controls.source_enabled(self.config_data, source))
            self.source_vars[source] = var
            ttk.Checkbutton(source_frame, text=label, variable=var).pack(anchor="w", pady=2)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(16, 0))
        ttk.Button(buttons, text="Save", command=self.save_recording).pack(side="left")
        ttk.Button(buttons, text="Reload", command=self.reload).pack(side="left", padx=(8, 0))

    def build_privacy_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="Privacy")
        ttk.Label(frame, text="Pause all raw capture temporarily. Existing local files are not deleted.").pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Pause 15 min", command=lambda: self.pause_minutes(15)).pack(side="left")
        ttk.Button(buttons, text="Pause 30 min", command=lambda: self.pause_minutes(30)).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Pause 1 hour", command=lambda: self.pause_minutes(60)).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Pause today", command=self.pause_today).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Resume now", command=self.resume_capture).pack(side="left", padx=(8, 0))
        self.privacy_status = ttk.Label(frame, text=self.privacy_status_text(), padding=(0, 18, 0, 0))
        self.privacy_status.pack(anchor="w")

    def build_exclusions_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="Exclusions")
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        for index, key in enumerate(EXCLUSION_LABELS):
            box = ttk.LabelFrame(frame, text=EXCLUSION_LABELS[key], padding=8)
            box.grid(row=index // 2, column=index % 2, sticky="nsew", padx=6, pady=6)
            listbox = tk.Listbox(box, height=8)
            listbox.pack(fill="both", expand=True)
            entry = ttk.Entry(box)
            entry.pack(fill="x", pady=(6, 0))
            button_row = ttk.Frame(box)
            button_row.pack(fill="x", pady=(6, 0))
            ttk.Button(button_row, text="Add", command=lambda k=key: self.add_exclusion(k)).pack(side="left")
            ttk.Button(button_row, text="Remove", command=lambda k=key: self.remove_exclusion(k)).pack(side="left", padx=(8, 0))
            self.exclusion_lists[key] = listbox
            self.exclusion_entries[key] = entry
        self.refresh_exclusions()

    def build_install_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="Install")
        ttk.Label(frame, text="Use these actions to install or repair scheduled tasks and shortcuts.").pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Preview Install", command=lambda: self.run_script("install_local.ps1", ["-WhatIf"])).pack(side="left")
        ttk.Button(buttons, text="Install/Repair", command=lambda: self.run_script("install_local.ps1", [])).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Preview Uninstall", command=lambda: self.run_script("uninstall_local.ps1", ["-WhatIf"])).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Start Tray", command=self.start_tray).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Check Tray Deps", command=self.check_tray_deps).pack(side="left", padx=(8, 0))

    def refresh_status(self) -> None:
        self.config_data = load_config()
        self.status_text.set(config_status_text(self.config_data))
        self.status_widget.configure(state="normal")
        self.status_widget.delete("1.0", "end")
        self.status_widget.insert("1.0", self.status_text.get())
        self.status_widget.configure(state="disabled")
        if hasattr(self, "privacy_status"):
            self.privacy_status.configure(text=self.privacy_status_text())

    def run_health_check(self) -> None:
        try:
            report = project_health.build_report(
                project_health.parse_date(None, self.config_data),
                self.config_data,
                include_scheduler=False,
                include_question_quality=False,
            )
        except Exception as exc:
            messagebox.showerror("Health Check", str(exc))
            return
        sections = report.get("sections", {})
        lines = [f"Overall: {report.get('overall')}"]
        for key, section in sections.items():
            lines.append(f"- {key}: {section.get('status')}")
        self.status_widget.configure(state="normal")
        self.status_widget.delete("1.0", "end")
        self.status_widget.insert("1.0", "\n".join(lines))
        self.status_widget.configure(state="disabled")

    def save_recording(self) -> None:
        capture_controls.ensure_config_shape(self.config_data)
        if self.capture_var.get():
            self.config_data["capture"]["enabled"] = True
        else:
            capture_controls.set_capture_enabled(self.config_data, False)
        for source, var in self.source_vars.items():
            capture_controls.set_source_enabled(self.config_data, source, var.get())
        save_config(self.config_data)
        self.refresh_status()
        messagebox.showinfo("Activity Journal", "Settings saved.")

    def reload(self) -> None:
        self.config_data = load_config()
        self.capture_var.set(bool(self.config_data["capture"].get("enabled", True)))
        for source, var in self.source_vars.items():
            var.set(capture_controls.source_enabled(self.config_data, source))
        self.refresh_exclusions()
        self.refresh_status()

    def privacy_status_text(self) -> str:
        status = capture_controls.status(self.config_data)
        if status["active"]:
            return "Capture is running."
        return f"Capture is paused: {status['reason']}"

    def pause_minutes(self, minutes: int) -> None:
        capture_controls.pause_for(self.config_data, dt.timedelta(minutes=minutes))
        save_config(self.config_data)
        self.capture_var.set(True)
        self.refresh_status()

    def pause_today(self) -> None:
        capture_controls.pause_until_end_of_day(self.config_data)
        save_config(self.config_data)
        self.capture_var.set(True)
        self.refresh_status()

    def resume_capture(self) -> None:
        capture_controls.resume(self.config_data)
        save_config(self.config_data)
        self.capture_var.set(True)
        self.refresh_status()

    def refresh_exclusions(self) -> None:
        for key, listbox in self.exclusion_lists.items():
            listbox.delete(0, "end")
            for value in capture_controls.exclusion_values(self.config_data, key):
                listbox.insert("end", value)

    def add_exclusion(self, key: str) -> None:
        value = self.exclusion_entries[key].get().strip()
        if not value:
            return
        capture_controls.add_exclusion(self.config_data, key, value)
        save_config(self.config_data)
        self.exclusion_entries[key].delete(0, "end")
        self.refresh_exclusions()
        self.refresh_status()

    def remove_exclusion(self, key: str) -> None:
        listbox = self.exclusion_lists[key]
        selection = listbox.curselection()
        if not selection:
            return
        value = str(listbox.get(selection[0]))
        capture_controls.remove_exclusion(self.config_data, key, value)
        save_config(self.config_data)
        self.refresh_exclusions()
        self.refresh_status()

    def run_script(self, script_name: str, args: list[str]) -> None:
        command = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / script_name), *args]
        result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        output = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode == 0:
            messagebox.showinfo(script_name, output or "Done.")
        else:
            messagebox.showerror(script_name, output or f"Failed with exit code {result.returncode}.")

    def start_tray(self) -> None:
        deps = tray_app.dependency_status()
        if deps["status"] != "OK":
            messagebox.showwarning("Activity Journal Tray", f"Missing dependencies. Run:\n{deps['install_command']}")
            return
        command = [tray_app.pythonw_path(), str(ROOT / "scripts" / "tray_app.py")]
        kwargs: dict[str, Any] = {"cwd": ROOT}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            subprocess.Popen(command, **kwargs)
        except OSError as exc:
            messagebox.showerror("Activity Journal Tray", str(exc))
            return
        messagebox.showinfo("Activity Journal Tray", "Tray started.")

    def check_tray_deps(self) -> None:
        deps = tray_app.dependency_status()
        lines = [f"Status: {deps['status']}"]
        for module, info in deps["dependencies"].items():
            lines.append(f"- {module}: {'OK' if info['available'] else 'missing'} ({info['package']})")
        if deps["status"] != "OK":
            lines.append(f"Install: {deps['install_command']}")
        messagebox.showinfo("Activity Journal Tray", "\n".join(lines))


def validate_config() -> int:
    config = load_config()
    warnings = capture_controls.validate_config(config)
    result = {"status": "OK" if not warnings else "Warning", "warnings": warnings}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not warnings else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-config", action="store_true", help="Validate config and exit without opening the GUI.")
    args = parser.parse_args()
    if args.validate_config:
        raise SystemExit(validate_config())
    root = tk.Tk()
    SettingsApp(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
