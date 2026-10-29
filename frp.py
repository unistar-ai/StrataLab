"""Manage the local FRPC process without exposing its credentials."""

import json
import os
import re
import subprocess
import threading
from pathlib import Path


class FrpcService:
    def __init__(self, root, app_dir, nowindow=0):
        self.root = Path(root)
        self.app_dir = Path(app_dir)
        self.directory = self.app_dir / "frp"
        self.executable = self.directory / ("frpc.exe" if os.name == "nt" else "frpc")
        self.config = self.directory / "frpc.toml"
        self.settings_file = self.app_dir / "frp.json"
        self.console_log = self.app_dir / "logs" / "frpc-console.log"
        self.nowindow = nowindow
        self.lock = threading.RLock()
        self.proc = None
        self.auto_start = self._load_auto_start()

    def _load_auto_start(self):
        try:
            return bool(json.loads(self.settings_file.read_text(encoding="utf-8")).get("auto_start", False))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _save_auto_start(self):
        tmp = self.settings_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"auto_start": self.auto_start}, indent=1), encoding="utf-8")
        tmp.replace(self.settings_file)

    def _connection(self):
        result = {"server": "", "local_port": None, "remote_port": None, "log_path": ""}
        try:
            text = self.config.read_text(encoding="utf-8")
        except OSError:
            return result
        patterns = {
            "server": r'^\s*serverAddr\s*=\s*["\']([^"\']+)["\']',
            "local_port": r'^\s*localPort\s*=\s*(\d+)',
            "remote_port": r'^\s*remotePort\s*=\s*(\d+)',
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, text, re.M)
            if match:
                result[key] = int(match.group(1)) if key.endswith("_port") else match.group(1)
        match = re.search(r'^\s*log\.to\s*=\s*["\']([^"\']+)["\']', text, re.M)
        if match:
            path = Path(match.group(1))
            result["log_path"] = str(path if path.is_absolute() else self.directory / path)
        return result

    def public(self):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            code = None if running else (self.proc.returncode if self.proc is not None else None)
            connection = self._connection()
            return {
                "installed": self.executable.is_file(),
                "configured": self.config.is_file(),
                "running": running,
                "pid": self.proc.pid if running else 0,
                "exit_code": code,
                "auto_start": self.auto_start,
                **connection,
            }

    def command(self):
        return [str(self.executable), "-c", str(self.config)]

    def start(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return self.public()
            if not self.executable.is_file():
                raise ValueError(f"找不到 FRPC：{self.executable}")
            if not self.config.is_file():
                raise ValueError(f"找不到 FRPC 配置：{self.config}")
            self.console_log.parent.mkdir(parents=True, exist_ok=True)
            with self.console_log.open("ab") as log:
                self.proc = subprocess.Popen(
                    self.command(), cwd=str(self.directory),
                    stdout=log, stderr=subprocess.STDOUT,
                    creationflags=self.nowindow)
            return self.public()

    def stop(self):
        with self.lock:
            proc = self.proc
            if proc is None:
                return self.public()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            self.proc = None
            return self.public()

    def set_auto_start(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("auto_start 必须是布尔值")
        with self.lock:
            self.auto_start = enabled
            self._save_auto_start()
            return self.public()

    def logs(self, count=20):
        path = self._connection()["log_path"]
        if not path:
            path = str(self.console_log)
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            return lines[-count:]
        except OSError:
            return []
