"""Tuning client window for Ubuntu and Windows.

The window asks for the server address and token, then keeps this computer
connected. Close the window to stop.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import queue
import re
import sys
import threading
from pathlib import Path

from apps.worker.doctor import run_doctor
from apps.worker.runner import identity, make_client, request_stop, serve, set_status_hook
from apps.worker.settings import SettingsError, WorkerSettings


def state_dir() -> Path:
    override = os.environ.get("MINOS_CLIENT_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "MinosClient"
    return Path.home() / ".minos-client"


def config_path() -> Path:
    return state_dir() / "client.json"


def load_config() -> dict[str, str] | None:
    path = config_path()
    if not path.is_file():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise SettingsError("Saved settings could not be read")
    return {str(key): str(value) for key, value in loaded.items()}


def save_config(values: dict[str, str]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")


def default_worker_id() -> str:
    return os.environ.get("COMPUTERNAME") or platform.node() or "windows"


def build_config(url: str, token: str, worker_id: str) -> dict[str, str]:
    clean_url = url.strip().rstrip("/")
    clean_token = token.strip()
    clean_id = worker_id.strip() or default_worker_id()
    if not clean_url.startswith("http://") and not clean_url.startswith("https://"):
        raise SettingsError("Server address must start with http:// or https://")
    if not clean_token:
        raise SettingsError("Enter the token from the Ubuntu machine")
    return {
        "coordinator_url": clean_url,
        "worker_token": clean_token,
        "worker_id": clean_id,
        "max_concurrent_jobs": "1",
    }


def apply_config(values: dict[str, str]) -> WorkerSettings:
    directory = state_dir()
    os.environ["COORDINATOR_URL"] = values["coordinator_url"]
    os.environ["WORKER_TOKEN"] = values["worker_token"]
    os.environ["WORKER_ID"] = values["worker_id"]
    # Idle until the server says which run is active. Saved files do not choose the score.
    os.environ["SCORING_VERSION"] = "v1"
    os.environ["EXECUTION_MODE"] = "mock"
    os.environ["MAX_CONCURRENT_JOBS"] = values.get("max_concurrent_jobs") or "1"
    os.environ["CACHE_DIR"] = str(directory / "cache")
    os.environ["WORK_DIR"] = str(directory / "work")
    return WorkerSettings.load(None)


def readiness_message(report: dict[str, object]) -> str | None:
    """Return one sentence when the PC cannot connect. None means it can."""
    checks = report.get("checks")
    if not isinstance(checks, list):
        return "This computer could not be checked."
    named = {item.get("name"): item for item in checks if isinstance(item, dict)}
    coordinator = named.get("coordinator")
    if isinstance(coordinator, dict) and not coordinator.get("ok"):
        return "The server did not answer. Check the address. Use the Ubuntu machine's address, not 127.0.0.1."
    if report.get("ok"):
        return None
    commit = named.get("minos_commit")
    if isinstance(commit, dict) and not commit.get("ok"):
        return "This program does not match the server."
    return "This computer is not ready to connect."


def resource_line(cpu_count: int, ram_mb: int, free_disk_mb: int) -> str:
    return f"{cpu_count} CPUs · {ram_mb / 1024:.1f} GB memory · {free_disk_mb / 1024:.1f} GB free disk"


def progress_percent(text: str) -> int | None:
    """Percent from a download status line, or None when the line is not a transfer."""
    if not text.startswith(("Downloading ", "Extracting ")):
        return None
    match = re.search(r"(\d+)%", text)
    if match is None:
        return None
    return max(0, min(100, int(match.group(1))))


def stamp_frozen_commit() -> None:
    if not getattr(sys, "frozen", False):
        return
    from apps.worker.frozen_commit import COMMIT

    os.environ["MINOS_COMMIT"] = COMMIT


def _configure_logging() -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=directory / "client.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _connect(values: dict[str, str], stop: threading.Event, messages: queue.Queue[tuple[str, str]]) -> None:
    set_status_hook(lambda text: messages.put(("status", text)))
    try:
        settings = apply_config(values)
        messages.put(("status", "Checking this computer…"))
        problem = readiness_message(run_doctor(settings, probe_coordinator=True))
        if problem:
            messages.put(("error", problem))
            return
        reported = identity(settings)
        messages.put(("resources", resource_line(reported.cpu_count, reported.ram_mb, reported.free_disk_mb)))
        messages.put(("status", "Connected. Leave this window open."))
        with make_client(settings) as client:
            serve(settings, client, stop_event=stop)
        messages.put(("stopped", "Stopped."))
    except Exception as exc:  # noqa: BLE001 - the window has to show any connection failure
        logging.exception("client connection failed")
        messages.put(("error", str(exc)))


def _screen_available() -> bool:
    if sys.platform == "win32":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def launch_gui() -> int:
    import tkinter as tk

    if not _screen_available():
        print(
            "Minos tuner opens a window. This terminal has no screen.",
            file=sys.stderr,
        )
        return 1

    paper = "#f7f4ef"
    ink = "#1d1a16"
    muted = "#5e584e"
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        if "display" in str(exc).lower():
            print("Minos tuner opens a window. This terminal has no screen.", file=sys.stderr)
            return 1
        raise
    root.title("Minos tuner")
    root.configure(bg=paper)

    frame = tk.Frame(root, bg=paper, padx=24, pady=22)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(0, weight=1)

    tk.Label(frame, text="Minos tuner", bg=paper, fg=ink, font=("Segoe UI", 20)).grid(row=0, column=0, sticky="w")
    tk.Label(frame, text="Connect this computer to the server.", bg=paper, fg=muted, font=("Segoe UI", 11)).grid(
        row=1, column=0, sticky="w", pady=(4, 16)
    )

    saved = None
    try:
        saved = load_config()
    except (OSError, json.JSONDecodeError, SettingsError):
        saved = None

    url = tk.StringVar(value=(saved or {}).get("coordinator_url", ""))
    token = tk.StringVar(value=(saved or {}).get("worker_token", ""))
    name = tk.StringVar(value=(saved or {}).get("worker_id") or default_worker_id())
    show_token = tk.BooleanVar(value=False)

    def add_field(row: int, label: str, variable: tk.StringVar, secret: bool = False) -> tk.Entry:
        tk.Label(frame, text=label, bg=paper, fg=ink, font=("Segoe UI", 11)).grid(row=row, column=0, sticky="w")
        entry = tk.Entry(frame, textvariable=variable, font=("Segoe UI", 12), relief="solid", bd=1)
        if secret:
            entry.configure(show="*")
        entry.grid(row=row + 1, column=0, sticky="ew", pady=(2, 10), ipady=4)
        return entry

    add_field(2, "Server address", url)
    tk.Label(frame, text="Example: http://192.168.1.20:8091", bg=paper, fg=muted, font=("Segoe UI", 10)).grid(
        row=4, column=0, sticky="w", pady=(0, 8)
    )
    token_entry = add_field(5, "Token", token, secret=True)

    def toggle_token() -> None:
        token_entry.configure(show="" if show_token.get() else "*")

    tk.Checkbutton(
        frame,
        text="Show token",
        variable=show_token,
        command=toggle_token,
        bg=paper,
        activebackground=paper,
        fg=ink,
        font=("Segoe UI", 10),
    ).grid(row=7, column=0, sticky="w", pady=(0, 8))
    add_field(8, "Computer name", name)

    buttons = tk.Frame(frame, bg=paper)
    buttons.grid(row=10, column=0, sticky="w", pady=(6, 16))
    start = tk.Button(buttons, text="Start", bg="#1b4332", fg="white", activebackground="#16382a", font=("Segoe UI", 12), padx=18, pady=6, relief="flat")
    stop = tk.Button(buttons, text="Stop", bg="#ffffff", fg=ink, font=("Segoe UI", 12), padx=18, pady=6, relief="solid", bd=1)
    start.grid(row=0, column=0, padx=(0, 8))
    stop.grid(row=0, column=1)
    stop.configure(state="disabled")

    status = tk.Label(frame, text="Not connected.", bg=paper, fg="#1b4332", font=("Segoe UI", 12), wraplength=400, justify="left")
    status.grid(row=11, column=0, sticky="w", pady=(4, 8))
    from tkinter import ttk

    bar = ttk.Progressbar(frame, maximum=100, mode="determinate")
    bar.grid(row=12, column=0, sticky="ew", pady=(0, 8))
    bar.grid_remove()
    resources = tk.Label(
        frame,
        text="CPU, memory, and free disk appear after Start.",
        bg=paper,
        fg=muted,
        font=("Segoe UI", 10),
        wraplength=380,
        justify="left",
    )
    resources.grid(row=13, column=0, sticky="w")

    messages: queue.Queue[tuple[str, str]] = queue.Queue()
    stop_event = threading.Event()
    worker: dict[str, threading.Thread | None] = {"thread": None}

    def set_running(running: bool) -> None:
        start.configure(state="disabled" if running else "normal")
        stop.configure(state="normal" if running else "disabled")

    def show_status(text: str, *, error: bool = False) -> None:
        status.configure(text=text, fg="#8c2f2a" if error else "#1b4332")
        percent = None if error else progress_percent(text)
        transferring = text.startswith(("Downloading ", "Extracting "))
        if error or not transferring:
            bar.stop()
            bar.grid_remove()
            return
        bar.grid()
        if percent is None:
            bar.configure(mode="indeterminate")
            bar.start(12)
            return
        bar.stop()
        bar.configure(mode="determinate")
        bar["value"] = percent

    def drain() -> None:
        try:
            while True:
                kind, text = messages.get_nowait()
                if kind == "resources":
                    resources.configure(text=text)
                elif kind == "error":
                    show_status(text, error=True)
                    set_running(False)
                elif kind == "stopped":
                    show_status(text)
                    set_running(False)
                else:
                    show_status(text)
        except queue.Empty:
            pass
        root.after(200, drain)

    def on_start() -> None:
        thread = worker["thread"]
        if thread is not None and thread.is_alive():
            return
        try:
            values = build_config(url.get(), token.get(), name.get())
        except SettingsError as exc:
            show_status(str(exc), error=True)
            return
        try:
            save_config(values)
        except OSError as exc:
            show_status(f"Could not save settings: {exc}", error=True)
            return
        stop_event.clear()
        set_running(True)
        show_status("Connecting…")
        thread = threading.Thread(target=_connect, args=(values, stop_event, messages), name="minos-client", daemon=True)
        worker["thread"] = thread
        thread.start()

    def on_stop() -> None:
        stop_event.set()
        request_stop(state_dir() / "work")
        show_status("Stopping…")

    def on_close() -> None:
        on_stop()
        root.destroy()

    start.configure(command=on_start)
    stop.configure(command=on_stop)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(200, drain)
    root.update_idletasks()
    root.geometry(f"460x{root.winfo_reqheight() + 40}")
    root.minsize(460, root.winfo_reqheight())
    root.mainloop()
    return 0


def launch_console(argv: list[str]) -> int:
    """Terminal client used on Ubuntu. Prompts once, then stays connected."""
    setup = len(argv) > 1 and argv[1] == "setup"
    saved = None
    if not setup:
        try:
            saved = load_config()
        except (OSError, json.JSONDecodeError, SettingsError):
            saved = None
    try:
        if saved is None:
            print("Minos tuner")
            print("Connect this computer to the server.")
            url = input("Server address: ").strip()
            token = input("Token: ").strip()
            default_name = default_worker_id()
            worker_id = input(f"Computer name [{default_name}]: ").strip()
            values = build_config(url, token, worker_id)
            save_config(values)
        else:
            values = build_config(saved.get("coordinator_url", ""), saved.get("worker_token", ""), saved.get("worker_id", ""))
    except (SettingsError, OSError, EOFError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Connecting {values['worker_id']} to {values['coordinator_url']}")
    try:
        settings = apply_config(values)
        print("Checking this computer…")
        problem = readiness_message(run_doctor(settings, probe_coordinator=True))
        if problem:
            print(problem, file=sys.stderr)
            return 1
        reported = identity(settings)
        print(resource_line(reported.cpu_count, reported.ram_mb, reported.free_disk_mb))
        print("Connected. Leave this terminal open. Press Ctrl+C to stop.")
        with make_client(settings) as client:
            serve(settings, client)
    except KeyboardInterrupt:
        request_stop(state_dir() / "work")
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("Stopped.")
    return 0


def run(argv: list[str]) -> int:
    stamp_frozen_commit()
    _configure_logging()
    if len(argv) > 1 and argv[1] == "stop":
        request_stop(state_dir() / "work")
        print("Stop requested.")
        return 0
    if sys.platform == "win32":
        return launch_gui()
    return launch_console(argv)


def main() -> None:
    sys.exit(run(sys.argv))


if __name__ == "__main__":
    main()
