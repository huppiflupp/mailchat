"""Start `mailchat serve` at login: systemd user unit on Linux, Startup folder on Windows."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

from . import config

UNIT = "mailchat.service"


def _linux_unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "systemd/user" / UNIT


def _windows_startup() -> Path:
    return (Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
            / "mailchat.cmd")


def _port_in_use() -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex((config.HOST, config.PORT)) == 0


def install() -> str:
    if config.WINDOWS:
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe")
        if not pythonw.exists():
            pythonw = exe
        path = _windows_startup()
        # start "" detaches, so the console window closes right away.
        path.write_text(f'@start "" "{pythonw}" -m mailchat serve\r\n', encoding="utf-8")
        if _port_in_use():
            return f"Created {path}. The server is already running."
        subprocess.Popen([str(pythonw), "-m", "mailchat", "serve"],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return f"Created {path} and started the server."
    if config.MACOS:
        raise SystemExit("Autostart is not implemented for macOS yet. Run `mailchat serve` "
                         "yourself, e.g. from a login item.")
    path = _linux_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""[Unit]
Description=mailchat: local search and chat server for the Thunderbird mailbox (port {config.PORT})

[Service]
ExecStart={sys.executable} -m mailchat serve
Restart=on-failure
RestartSec=5
Nice=5
IOSchedulingClass=idle

[Install]
WantedBy=default.target
""", encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", UNIT], check=True)
    return (f"Created {path}, enabled and started it.\n"
            f"Log: journalctl --user -u {UNIT} -f")


def remove() -> str:
    if config.WINDOWS:
        path = _windows_startup()
        path.unlink(missing_ok=True)
        return f"Removed {path}. A running server stops at the next logout."
    path = _linux_unit_path()
    subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], check=False)
    path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    return f"Stopped and removed {path}."
