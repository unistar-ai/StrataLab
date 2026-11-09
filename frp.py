"""Manage the local FRPC process without exposing its credentials."""

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path


DEBUG_BEGIN = "# stratalab-debug-begin"
DEBUG_END = "# stratalab-debug-end"


def splice_debug_proxy(text, enabled, local_port=2222, remote_port=18022):
    """Add or remove the marked debug proxy. The auth token above it is left untouched."""
    pattern = re.compile(
        r"\n?" + re.escape(DEBUG_BEGIN) + r".*?" + re.escape(DEBUG_END) + r"\n?",
        re.S)
    cleaned = pattern.sub("\n", text).rstrip() + "\n"
    if not enabled:
        return cleaned
    local_port = int(local_port)
    remote_port = int(remote_port)
    if not (1024 <= local_port <= 65535 and 1024 <= remote_port <= 65535):
        raise ValueError("端口必须在 1024–65535")
    return cleaned + (
        f"\n{DEBUG_BEGIN}\n"
        "[[proxies]]\n"
        'name = "strata-debug"\n'
        'type = "tcp"\n'
        'localIP = "127.0.0.1"\n'
        f"localPort = {local_port}\n"
        f"remotePort = {remote_port}\n"
        f"{DEBUG_END}\n"
    )


def parse_debug_proxy(text):
    match = re.search(re.escape(DEBUG_BEGIN) + r"(.*?)" + re.escape(DEBUG_END), text or "", re.S)
    if not match:
        return {"exposed": False, "local_port": None, "remote_port": None}

    def grab(key):
        found = re.search(rf"^\s*{key}\s*=\s*(\d+)", match.group(1), re.M)
        return int(found.group(1)) if found else None

    return {"exposed": True, "local_port": grab("localPort"), "remote_port": grab("remotePort")}


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
        self._others = 0
        self._others_at = 0.0
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

    def _read_config(self):
        try:
            return self.config.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _write_config(self, text):
        if not self.config.is_file():
            raise ValueError(f"找不到 FRPC 配置：{self.config}")
        tmp = self.config.with_suffix(".toml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.config)

    def debug_proxy(self):
        with self.lock:
            return parse_debug_proxy(self._read_config())

    def set_debug_proxy(self, enabled, local_port=2222, remote_port=18022):
        """Rewrite only the marked debug block. Does not start or stop frpc."""
        with self.lock:
            text = self._read_config()
            if not text and not self.config.is_file():
                raise ValueError(f"找不到 FRPC 配置：{self.config}")
            if not enabled and DEBUG_BEGIN not in text:
                return {"exposed": False, "local_port": None, "remote_port": None}
            updated = splice_debug_proxy(text, bool(enabled), local_port, remote_port)
            if updated != text:
                self._write_config(updated)
            return parse_debug_proxy(updated)

    def public(self):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            code = None if running else (self.proc.returncode if self.proc is not None else None)
            connection = self._connection()
            now = time.monotonic()
            if now - self._others_at > 10:
                self._others_at = now
                self._others = len(self._siblings())
            return {
                "installed": self.executable.is_file(),
                "configured": self.config.is_file(),
                "running": running,
                "pid": self.proc.pid if running else 0,
                "exit_code": code,
                "others": self._others,
                "auto_start": self.auto_start,
                **connection,
            }

    def command(self):
        return [str(self.executable), "-c", str(self.config)]

    def _siblings(self):
        """Every frpc.exe running from this install, not just the one we started.

        A lab restart used to leave the previous tunnel alive, and the cloud
        server then let whichever client had the proxy keep forwarding even
        after "stop" killed our own process.
        """
        if os.name != "nt":
            return []
        query = ("Get-CimInstance Win32_Process -Filter \"Name='frpc.exe'\" "
                 "| Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress")
        try:
            result = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                                    capture_output=True, text=True, timeout=15,
                                    creationflags=self.nowindow)
            data = json.loads(result.stdout.strip() or "[]")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return []
        if isinstance(data, dict):
            data = [data]
        own = self.proc.pid if self.proc is not None else 0
        me = str(self.executable).lower()
        pids = []
        for item in data or []:
            try:
                pid = int(item.get("ProcessId") or 0)
            except (TypeError, ValueError):
                continue
            if pid and pid != own and str(item.get("ExecutablePath") or "").lower() == me:
                pids.append(pid)
        return pids

    def _kill_siblings(self):
        for pid in self._siblings():
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, creationflags=self.nowindow)

    def start(self):
        with self.lock:
            self._kill_siblings()
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
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            self.proc = None
            self._kill_siblings()
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
