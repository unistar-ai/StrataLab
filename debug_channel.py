"""Localhost SSH entry for Kimi Code and Cursor CLI.

The lab UI stays on 127.0.0.1:8765 with no auth, so this channel is a separate
sshd that also binds only to loopback. Password login and TCP forwarding are
off. A public path, when the operator asks for one, is an extra FRP proxy —
never the model gateway.

Kimi Code's remote environment is SSH. Cursor CLI is used after that same
SSH session lands on this machine. Neither client can attach to the lab's
private HTTP terminal.
"""

import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path


PORT = 2222
LISTEN = "127.0.0.1"


def debug_snippets(user, key_path, server, remote_port, cwd, kimi_bin):
    """Client instructions. The private key is named, never inlined."""
    cwd_path = Path(cwd)
    ssh_cwd = str(cwd_path)
    kimi_cwd = ssh_cwd.replace("\\", "/")
    host = server or "云服务器地址"
    port = int(remote_port or 18022)
    local = (
        f'ssh -i "{key_path}" -p {PORT} '
        "-o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
        f"{user}@{LISTEN}"
    )
    ssh_config = (
        "Host strata-lab\n"
        f"    HostName {host}\n"
        f"    Port {port}\n"
        f"    User {user}\n"
        "    IdentityFile ~/.ssh/strata-lab\n"
        "    IdentitiesOnly yes\n"
    )
    remote_bin = f'\nremoteBin = "{kimi_bin.replace(chr(92), "/")}"' if kimi_bin else ""
    kimi = (
        "[environments.strata-lab]\n"
        'type = "ssh"\n'
        'host = "strata-lab"\n'
        f'defaultCwd = "{kimi_cwd}"'
        f"{remote_bin}\n"
    )
    cursor = f"ssh strata-lab\ncd /d {ssh_cwd}\nagent\n"
    return {"local": local, "ssh_config": ssh_config, "kimi": kimi, "cursor": cursor}


class DebugChannel:
    def __init__(self, app_dir, nowindow=0, cwd=""):
        self.app_dir = Path(app_dir)
        self.directory = self.app_dir / "debug"
        self.nowindow = nowindow
        self.cwd = str(cwd)
        self.lock = threading.RLock()
        self.proc = None
        self.installing = False
        self.install_error = ""
        self.test_ok = None
        self.test_error = ""
        self._stdout = None

    def sshd_path(self):
        root = os.environ.get("SystemRoot", r"C:\Windows")
        candidate = Path(root) / "System32" / "OpenSSH" / "sshd.exe"
        if candidate.is_file():
            return candidate
        found = shutil.which("sshd")
        return Path(found) if found else None

    def _ssh_bin(self, name):
        root = os.environ.get("SystemRoot", r"C:\Windows")
        candidate = Path(root) / "System32" / "OpenSSH" / name
        if candidate.is_file():
            return candidate
        found = shutil.which(Path(name).stem)
        return Path(found) if found else None

    def _settings_path(self):
        return self.directory / "debug.json"

    def remote_port(self):
        try:
            data = json.loads(self._settings_path().read_text(encoding="utf-8"))
            port = int(data.get("remote_port", 18022))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 18022
        return port if 1024 <= port <= 65535 else 18022

    def remember_port(self, port):
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self._settings_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"remote_port": int(port)}, indent=1), encoding="utf-8")
        tmp.replace(self._settings_path())

    def _tools(self):
        home = Path.home()
        kimi_candidates = [
            home / ".kimi-code" / "bin" / "kimi.exe",
            home / ".kimi-code" / "bin" / "kimi",
        ]
        agent_candidates = [
            home / ".local" / "bin" / "agent.exe",
            home / ".local" / "bin" / "agent.cmd",
            Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent" / "agent.cmd",
            Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent" / "agent.exe",
        ]
        kimi = next((str(path) for path in kimi_candidates if path.is_file()), "")
        if not kimi:
            kimi = shutil.which("kimi") or ""
        agent = next((str(path) for path in agent_candidates if path.is_file()), "")
        if not agent:
            agent = shutil.which("agent") or ""
        return {"kimi": kimi, "agent": agent, "user": getpass.getuser()}

    def _tail(self, path, count=40):
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return [line[:400] for line in lines[-count:]]

    def _running_locked(self):
        return self.proc is not None and self.proc.poll() is None

    def _service_pid(self):
        """PID of the LocalSystem sshd service, or 0 when it is not running.

        Password login has to go through that service. A user-mode sshd can
        accept a public key, but Windows will not let it create a logon token,
        so a correct password is followed by "closed by remote host".
        """
        if os.name != "nt":
            return 0
        try:
            result = subprocess.run(
                ["sc.exe", "queryex", "sshd"], capture_output=True, text=True,
                timeout=10, creationflags=self.nowindow)
        except (OSError, subprocess.TimeoutExpired):
            return 0
        output = result.stdout or ""
        state = re.search(r"STATE\s+:\s+\d+\s+(\w+)", output)
        pid = re.search(r"PID\s+:\s+(\d+)", output)
        if not state or state.group(1).upper() != "RUNNING" or not pid:
            return 0
        return int(pid.group(1))

    def public(self, server=""):
        with self.lock:
            tools = self._tools()
            key = self.directory / "id_ed25519"
            pub = ""
            try:
                pub = (self.directory / "id_ed25519.pub").read_text(encoding="utf-8").strip()
            except OSError:
                pub = ""
            service_pid = self._service_pid()
            user_running = self._running_locked()
            # Prefer the service when it is actually listening. A leftover
            # user-mode process cannot complete a password logon.
            running = bool(service_pid) or user_running
            port = self.remote_port()
            return {
                "installed": self.sshd_path() is not None,
                "installing": self.installing,
                "install_error": self.install_error,
                "running": running,
                "mode": "service" if service_pid else ("process" if user_running else ""),
                "pid": service_pid or (self.proc.pid if user_running else 0),
                "listen": f"{LISTEN}:{PORT}",
                "user": tools["user"],
                "key_path": str(key),
                "public_key": pub,
                "test_ok": self.test_ok,
                "test_error": self.test_error,
                "remote_port": port,
                "kimi": tools["kimi"],
                "agent": tools["agent"],
                "cwd": self.cwd,
                "snippets": debug_snippets(
                    tools["user"], key, server, port, self.cwd, tools["kimi"]),
                "logs": self._tail(self.directory / "sshd.log")
                + self._tail(self.directory / "sshd.stdout.log"),
            }

    def _tighten(self, path):
        if os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return
        user = getpass.getuser()
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R)"],
            capture_output=True, creationflags=self.nowindow)

    def _keygen(self, target, comment):
        binary = self._ssh_bin("ssh-keygen.exe")
        if binary is None:
            raise ValueError("找不到 ssh-keygen.exe")
        if target.exists() and Path(str(target) + ".pub").exists():
            return
        # A comment on the host key is unnecessary; the user key carries one.
        command = [str(binary), "-t", "ed25519", "-f", str(target), "-N", ""]
        if comment:
            command.extend(["-C", comment])
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=20,
            creationflags=self.nowindow)
        if result.returncode != 0 or not target.exists():
            detail = (result.stderr or result.stdout or "ssh-keygen 失败").strip()
            raise ValueError(detail[-400:])
        self._tighten(target)

    def ensure_keys(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self._keygen(self.directory / "ssh_host_ed25519_key", "")
        self._keygen(self.directory / "id_ed25519", "strata-lab-debug")
        private = self.directory / "id_ed25519"
        public = self.directory / "id_ed25519.pub"
        if not public.exists():
            raise ValueError("用户公钥没有生成")
        (self.directory / "authorized_keys").write_text(
            public.read_text(encoding="utf-8").strip() + "\n", encoding="utf-8")
        self._tighten(private)
        return private

    def _config_text(self, sshd):
        user = getpass.getuser()
        sftp = sshd.with_name("sftp-server.exe")
        lines = [
            f"Port {PORT}",
            f"ListenAddress {LISTEN}",
            "HostKey " + self._slash(self.directory / "ssh_host_ed25519_key"),
            "PidFile " + self._slash(self.directory / "sshd.pid"),
            "AuthorizedKeysFile " + self._slash(self.directory / "authorized_keys"),
            "PubkeyAuthentication yes",
            "PasswordAuthentication yes",
            "KbdInteractiveAuthentication yes",
            "PermitEmptyPasswords no",
            "AllowTcpForwarding no",
            "AllowAgentForwarding no",
            "X11Forwarding no",
            "PermitTunnel no",
            "GatewayPorts no",
            "PermitUserEnvironment no",
            "ClientAliveInterval 30",
            "ClientAliveCountMax 4",
            "UseDNS no",
            "PrintMotd no",
            "StrictModes no",
            "MaxAuthTries 3",
            "LogLevel INFO",
        ]
        if re.fullmatch(r"[A-Za-z0-9_.-]+", user or ""):
            lines.append(f"AllowUsers {user}")
        if sftp.is_file():
            lines.append("Subsystem sftp " + self._slash(sftp))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _slash(path):
        return str(path).replace("\\", "/")

    def _port_open(self):
        with socket.socket() as sock:
            sock.settimeout(0.3)
            return sock.connect_ex((LISTEN, PORT)) == 0

    def _command_line(self, pid):
        if os.name != "nt" or not pid:
            return ""
        query = '(Get-CimInstance Win32_Process -Filter "ProcessId=%d").CommandLine' % int(pid)
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", query],
                capture_output=True, text=True, timeout=15,
                creationflags=self.nowindow)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return (result.stdout or "").strip()

    def _pidfile_pid(self):
        try:
            return int((self.directory / "sshd.pid").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def _ours(self, pid):
        if not pid:
            return False
        config = self._slash(self.directory / "sshd_config")
        command = self._command_line(pid)
        return "sshd" in command.lower() and config.lower() in command.lower().replace("\\", "/")

    def _kill_pid(self, pid):
        if not pid:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, creationflags=self.nowindow)
        elif self.proc is not None and self.proc.pid == pid:
            self.proc.kill()

    def _close_stdout(self):
        handle = self._stdout
        self._stdout = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _service_installed(self):
        if os.name != "nt":
            return False
        try:
            result = subprocess.run(
                ["sc.exe", "query", "sshd"], capture_output=True, text=True,
                timeout=10, creationflags=self.nowindow)
        except (OSError, subprocess.TimeoutExpired):
            return False
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0 and "1060" not in output

    def _control_service(self, action):
        """Start or stop the LocalSystem sshd service. Elevate once if refused."""
        if action not in ("start", "stop"):
            raise ValueError("unknown service action")
        try:
            subprocess.run(
                ["sc.exe", action, "sshd"], capture_output=True, text=True,
                timeout=20, creationflags=self.nowindow)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("无法%s OpenSSH 服务：%s" % (
                "停止" if action == "stop" else "启动", exc))
        if self._wait_service(action):
            return
        script = self.directory / "service-control.ps1"
        self.directory.mkdir(parents=True, exist_ok=True)
        verb = "Stop-Service sshd -Force" if action == "stop" else "Start-Service sshd"
        script.write_text("$ErrorActionPreference = 'Stop'\n%s\n" % verb, encoding="utf-8")
        quoted = str(script).replace("'", "''")
        command = (
            "$p = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru "
            "-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','%s'); "
            "if ($null -eq $p) { exit 1 }; exit $p.ExitCode" % quoted)
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=120, creationflags=self.nowindow)
        except subprocess.TimeoutExpired:
            raise ValueError("等待管理员确认超时，OpenSSH 服务没有%s" % (
                "停止" if action == "stop" else "启动"))
        if not self._wait_service(action):
            raise ValueError("OpenSSH 服务没有%s。请在 UAC 提示里点“是”。" % (
                "停止" if action == "stop" else "启动"))

    def _wait_service(self, action):
        want_up = action == "start"
        for _ in range(40):
            up = bool(self._service_pid()) and self._port_open()
            if up == want_up:
                return True
            if not want_up and not self._service_pid() and not self._port_open():
                return True
            time.sleep(0.25)
        up = bool(self._service_pid()) and self._port_open()
        return up if want_up else (not self._service_pid() and not self._port_open())

    def stop(self):
        with self.lock:
            proc = self.proc
            pid = proc.pid if proc is not None else 0
            file_pid = self._pidfile_pid()
            if pid:
                self._kill_pid(pid)
            if file_pid and file_pid != pid and self._ours(file_pid):
                self._kill_pid(file_pid)
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            self.proc = None
            self._close_stdout()
            self.test_ok = None
            self.test_error = ""
            try:
                (self.directory / "sshd.pid").unlink()
            except OSError:
                pass
            if self._service_installed() and (self._service_pid() or self._port_open()):
                self._control_service("stop")
            if self._service_pid() or self._port_open():
                raise ValueError("通道还在监听 %s:%d" % (LISTEN, PORT))
            return self.public()

    def _wait_until_listening(self):
        for _ in range(30):
            if self.proc is not None and self.proc.poll() is not None:
                return False
            if self._port_open():
                return True
            time.sleep(0.1)
        return self._port_open()

    def _self_test(self, key):
        ssh = self._ssh_bin("ssh.exe")
        if ssh is None:
            self.test_ok = False
            self.test_error = "找不到 ssh.exe"
            return
        known = self.directory / "known_hosts"
        try:
            known.unlink()
        except OSError:
            pass
        command = [
            str(ssh), "-i", str(key), "-p", str(PORT),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=" + str(known),
            "-o", "ConnectTimeout=5",
            "-o", "PreferredAuthentications=publickey",
            f"{getpass.getuser()}@{LISTEN}",
            "echo strata-lab-debug-ok",
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=15,
                creationflags=self.nowindow)
        except subprocess.TimeoutExpired:
            self.test_ok = False
            self.test_error = "本机回环试连超时"
            return
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        self.test_ok = result.returncode == 0 and "strata-lab-debug-ok" in (result.stdout or "")
        self.test_error = "" if self.test_ok else output[-500:]

    def start(self):
        with self.lock:
            if self._running_locked() and not self._service_installed():
                return self.public()
            if self._service_installed():
                if not (self._service_pid() and self._port_open()):
                    self._control_service("start")
                if not self._port_open():
                    raise ValueError("OpenSSH 服务没有在 %s:%d 监听" % (LISTEN, PORT))
                key = self.ensure_keys()
                self._self_test(key)
                return self.public()
            sshd = self.sshd_path()
            if sshd is None:
                raise ValueError("没有 sshd.exe。先点「安装 OpenSSH Server」，装完再启动。")
            key = self.ensure_keys()
            if self._port_open():
                occupant = self._pidfile_pid() or (self.proc.pid if self.proc else 0)
                if self._ours(occupant):
                    self._kill_pid(occupant)
                    time.sleep(0.3)
                if self._port_open():
                    raise ValueError(f"{LISTEN}:{PORT} 已被其他程序占用")
            config = self.directory / "sshd_config"
            config.write_text(self._config_text(sshd), encoding="ascii")
            log_path = self.directory / "sshd.log"
            self._close_stdout()
            stdout_path = self.directory / "sshd.stdout.log"
            self._stdout = stdout_path.open("ab")
            command = [str(sshd), "-D", "-f", str(config), "-E", str(log_path)]
            self.proc = subprocess.Popen(
                command, cwd=str(self.directory), stdin=subprocess.DEVNULL,
                stdout=self._stdout, stderr=subprocess.STDOUT,
                creationflags=self.nowindow)
            if not self._wait_until_listening():
                code = self.proc.poll()
                detail = "\n".join(self._tail(log_path, 15) + self._tail(stdout_path, 15))
                self.stop()
                raise ValueError(detail or f"sshd 退出了（code {code}）")
            self._self_test(key)
            return self.public()

    def rekey(self):
        with self.lock:
            if self._running_locked():
                raise ValueError("先停止通道，再更换密钥")
            self.directory.mkdir(parents=True, exist_ok=True)
            for name in ("id_ed25519", "id_ed25519.pub", "authorized_keys"):
                try:
                    (self.directory / name).unlink()
                except OSError:
                    pass
            self.ensure_keys()
            self.test_ok = None
            self.test_error = ""
            return self.public()

    def _install_script(self):
        ok_path = str(self.directory / "install.ok").replace("'", "''")
        err_path = str(self.directory / "install.err").replace("'", "''")
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            "try {\n"
            "  Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null\n"
            "  $svc = Get-Service sshd -ErrorAction SilentlyContinue\n"
            "  if ($svc) {\n"
            "    Stop-Service sshd -Force -ErrorAction SilentlyContinue\n"
            "    Set-Service sshd -StartupType Disabled\n"
            "  }\n"
            "  Get-NetFirewallRule -Name '*OpenSSH*' -ErrorAction SilentlyContinue | ForEach-Object { Disable-NetFirewallRule -Name $_.Name }\n"
            f"  Set-Content -Encoding ascii -Path '{ok_path}' -Value 'ok'\n"
            "} catch {\n"
            f"  $_ | Out-File -FilePath '{err_path}' -Encoding utf8\n"
            "  exit 1\n"
            "}\n"
        )
        return script

    def install(self):
        """Ask Windows for the OpenSSH Server capability. UAC has to be accepted."""
        with self.lock:
            if self.sshd_path() is not None:
                return self.public()
            if self.installing:
                return self.public()
            self.directory.mkdir(parents=True, exist_ok=True)
            self.installing = True
            self.install_error = ""
            script = self._install_script()
            script_path = self.directory / "install-openssh.ps1"
            ok_path = self.directory / "install.ok"
            err_path = self.directory / "install.err"
            script_path.write_text(script, encoding="utf-8")
            for path in (ok_path, err_path):
                try:
                    path.unlink()
                except OSError:
                    pass
            threading.Thread(
                target=self._install_worker, args=(script_path, ok_path, err_path),
                daemon=True).start()
            return self.public()

    def _install_worker(self, script_path, ok_path, err_path):
        quoted = str(script_path).replace("'", "''")
        command = (
            "$p = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru "
            "-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','%s'); "
            "if ($null -eq $p) { exit 1 }; exit $p.ExitCode"
            % quoted
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", command],
                capture_output=True, text=True, timeout=900,
                creationflags=self.nowindow)
            detail = ((result.stderr or "") + (result.stdout or "")).strip()
        except subprocess.TimeoutExpired:
            result = None
            detail = "安装超时"
        except OSError as exc:
            result = None
            detail = str(exc)
        with self.lock:
            self.installing = False
            if self.sshd_path() is not None:
                self.install_error = ""
                return
            if Path(ok_path).is_file():
                self.install_error = "安装脚本结束了，但还是没有找到 sshd.exe"
                return
            err = ""
            try:
                err = Path(err_path).read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
            code = result.returncode if result is not None else 1
            self.install_error = (err or detail or f"安装失败（code {code}）")[-500:]
