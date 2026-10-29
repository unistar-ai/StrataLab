"""Optional, stdlib-only API gateway for the local Strata engine."""

import datetime
import hashlib
import http.client
import json
import secrets
import socket
import threading
import time
import urllib.parse
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


DEFAULT_CONFIG = {
    "enabled": False,
    "port": 18080,
    "public_url": "",
    "key_hash": "",
    "key_id": "",
    "rate_per_min": 30,
    "daily_tokens": 1000000,
    "max_body_bytes": 4194304,
    "max_tokens": 8192,
    "queue_size": 2,
    "queue_timeout_s": 30,
    "upstream_timeout_s": 1800,
}

LIMITS = {
    "port": (1024, 65535),
    "rate_per_min": (1, 1000),
    "daily_tokens": (1000, 1000000000),
    "max_body_bytes": (65536, 67108864),
    "max_tokens": (1, 32768),
    "queue_size": (0, 20),
    "queue_timeout_s": (1, 300),
    "upstream_timeout_s": (30, 86400),
}

ALLOWED_ROUTES = {
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/messages"),
}


def validate_config(data):
    if not isinstance(data, dict):
        raise ValueError("配置必须是对象")
    cfg = dict(DEFAULT_CONFIG)
    for key, value in data.items():
        if key in cfg:
            cfg[key] = value
    if not isinstance(cfg["enabled"], bool):
        raise ValueError("enabled 必须是布尔值")
    for key, (low, high) in LIMITS.items():
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} 必须是 {low}–{high} 之间的整数")
    if not isinstance(cfg["public_url"], str):
        raise ValueError("public_url 必须是字符串")
    url = cfg["public_url"].strip().rstrip("/")
    if url:
        if len(url) > 300:
            raise ValueError("公网地址过长")
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("公网地址必须是合法的 http(s) URL")
    cfg["public_url"] = url
    for key in ("key_hash", "key_id"):
        if not isinstance(cfg[key], str):
            raise ValueError(f"{key} 必须是字符串")
    return cfg


def hash_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class Reject(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class GatewayService:
    def __init__(self, app_dir, upstream="127.0.0.1", upstream_port=8080):
        self.app_dir = Path(app_dir)
        self.upstream = upstream
        self.upstream_port = upstream_port
        self.config_file = self.app_dir / "gateway.json"
        self.state_file = self.app_dir / "gateway-state.json"
        self.audit_file = self.app_dir / "gateway-audit.jsonl"
        self.lock = threading.RLock()
        self.slot = threading.Semaphore(1)
        self.active = 0
        self.waiting = 0
        self.httpd = None
        self.thread = None
        self.rate = deque()
        self.config = self._load_config()
        self.state = self._load_state()
        self.recent = deque(self._read_audit(), maxlen=500)

    def _load_config(self):
        try:
            return validate_config(json.loads(self.config_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return dict(DEFAULT_CONFIG)

    def _load_state(self):
        blank = {"total_requests": 0, "total_tokens": 0, "days": {}}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("days"), dict):
                blank.update(data)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return blank

    def _save_json(self, path, data):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def _read_audit(self):
        rows = []
        for path in (self.audit_file.with_suffix(".jsonl.1"), self.audit_file):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict):
                            rows.append(row)
                    except json.JSONDecodeError:
                        pass
            except OSError:
                pass
        return rows[-500:]

    @staticmethod
    def _day():
        return datetime.datetime.now().strftime("%Y-%m-%d")

    def public(self):
        with self.lock:
            cfg = {k: v for k, v in self.config.items() if k != "key_hash"}
            cfg.update({
                "running": self.httpd is not None,
                "listen": f"http://127.0.0.1:{self.config['port']}",
                "key_set": bool(self.config["key_hash"]),
                "active": self.active,
                "waiting": self.waiting,
            })
            return cfg

    def start(self):
        with self.lock:
            if self.httpd is not None:
                return
            if not self.config["key_hash"]:
                raise ValueError("请先生成 API Key")
            service = self

            class Handler(GatewayHandler):
                gateway = service

            self.httpd = GatewayServer(("127.0.0.1", self.config["port"]), Handler)
            self.thread = threading.Thread(target=self.httpd.serve_forever,
                                           name="strata-gateway", daemon=True)
            self.thread.start()

    def stop(self):
        with self.lock:
            httpd, thread = self.httpd, self.thread
            self.httpd = self.thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=3)

    def configure(self, data):
        if not isinstance(data, dict):
            raise ValueError("配置必须是对象")
        allowed = {k: v for k, v in data.items() if k in DEFAULT_CONFIG
                   and k not in ("key_hash", "key_id")}
        cfg = validate_config(dict(self.config, **allowed))
        if cfg["enabled"] and not cfg["key_hash"]:
            raise ValueError("启用前请先生成 API Key")
        was_running = self.httpd is not None
        restart = was_running and cfg["port"] != self.config["port"]
        if restart or (was_running and not cfg["enabled"]):
            self.stop()
        self.config = cfg
        self._save_json(self.config_file, cfg)
        if cfg["enabled"] and self.httpd is None:
            self.start()
        return self.public()

    def generate_key(self):
        key = "sk-strata-" + secrets.token_urlsafe(32)
        with self.lock:
            self.config["key_hash"] = hash_key(key)
            self.config["key_id"] = hash_key(key)[:10]
            self._save_json(self.config_file, self.config)
        return key, self.public()

    def _authenticated(self, handler):
        supplied = handler.headers.get("X-API-Key", "")
        authorization = handler.headers.get("Authorization", "")
        if authorization:
            scheme, _, value = authorization.partition(" ")
            supplied = value.strip() if scheme.lower() == "bearer" else ""
        if not supplied:
            raise Reject(401, "需要 Authorization: Bearer <API Key>")
        if not secrets.compare_digest(hash_key(supplied), self.config["key_hash"]):
            raise Reject(401, "API Key 无效")
        return self.config["key_id"]

    def _check_rate(self):
        now = time.monotonic()
        with self.lock:
            while self.rate and now - self.rate[0] >= 60:
                self.rate.popleft()
            if len(self.rate) >= self.config["rate_per_min"]:
                retry = max(1, int(60 - (now - self.rate[0])) + 1)
                raise Reject(429, "超过每分钟请求限制", retry)
            self.rate.append(now)

    def _check_quota(self):
        day = self.state["days"].get(self._day(), {})
        if int(day.get("tokens", 0)) >= self.config["daily_tokens"]:
            raise Reject(429, "已达到今日 token 额度")

    def _enter_queue(self):
        with self.lock:
            if self.active and self.waiting >= self.config["queue_size"]:
                raise Reject(429, "请求队列已满", 5)
            self.waiting += 1
        try:
            acquired = self.slot.acquire(timeout=self.config["queue_timeout_s"])
        finally:
            with self.lock:
                self.waiting -= 1
        if not acquired:
            raise Reject(429, "排队超时", 5)
        with self.lock:
            self.active += 1

    def _leave_queue(self):
        with self.lock:
            self.active = max(0, self.active - 1)
        self.slot.release()

    def _engine_model(self):
        conn = http.client.HTTPConnection(self.upstream, self.upstream_port, timeout=3)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            data = json.loads(response.read(65536).decode("utf-8"))
            if response.status != 200 or not data.get("loaded") or not data.get("model"):
                raise Reject(503, "Strata 引擎尚未就绪")
            return data["model"]
        except Reject:
            raise
        except (OSError, ValueError, json.JSONDecodeError):
            raise Reject(503, "无法连接 Strata 引擎")
        finally:
            conn.close()

    def _record(self, row):
        with self.lock:
            self.recent.append(row)
            day_name = self._day()
            day = self.state["days"].setdefault(day_name, {"requests": 0, "rejected": 0, "tokens": 0})
            day["requests"] = int(day.get("requests", 0)) + 1
            if row["status"] >= 400:
                day["rejected"] = int(day.get("rejected", 0)) + 1
            tokens = int(row.get("total_tokens") or 0)
            day["tokens"] = int(day.get("tokens", 0)) + tokens
            self.state["total_requests"] = int(self.state.get("total_requests", 0)) + 1
            self.state["total_tokens"] = int(self.state.get("total_tokens", 0)) + tokens
            days = self.state["days"]
            for old in sorted(days)[:-62]:
                days.pop(old, None)
            try:
                self._save_json(self.state_file, self.state)
                if self.audit_file.exists() and self.audit_file.stat().st_size > 8 * 1024 * 1024:
                    self.audit_file.replace(self.audit_file.with_suffix(".jsonl.1"))
                with self.audit_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def monitor(self):
        with self.lock:
            day = dict(self.state["days"].get(self._day(),
                         {"requests": 0, "rejected": 0, "tokens": 0}))
            return {
                "gateway": self.public(),
                "today": day,
                "total_requests": self.state.get("total_requests", 0),
                "total_tokens": self.state.get("total_tokens", 0),
                "recent": list(reversed(self.recent))[:100],
                "audit": "仅元数据；不保存 API Key、提示词或生成内容",
            }

    def clear_audit(self):
        with self.lock:
            self.recent.clear()
            self.state = {"total_requests": 0, "total_tokens": 0, "days": {}}
            self._save_json(self.state_file, self.state)
            for path in (self.audit_file, self.audit_file.with_suffix(".jsonl.1")):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StrataGateway/0.4"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _error(self, status, message, retry_after=None):
        body = json.dumps({"error": {"message": message, "type": "gateway_error"}},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _read_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise Reject(400, "不支持 Transfer-Encoding")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Reject(400, "Content-Length 无效")
        if size <= 0:
            raise Reject(400, "请求体不能为空")
        if size > self.gateway.config["max_body_bytes"]:
            raise Reject(413, "请求体超过大小限制")
        raw = self.rfile.read(size)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise Reject(400, "请求体必须是 UTF-8 JSON")
        if not isinstance(data, dict):
            raise Reject(400, "JSON 顶层必须是对象")
        return data

    @staticmethod
    def _usage(usage, chunk):
        if not isinstance(chunk, dict):
            return
        direct = chunk.get("usage")
        if isinstance(direct, dict):
            if isinstance(direct.get("prompt_tokens"), int):
                usage["prompt"] = direct["prompt_tokens"]
            if isinstance(direct.get("completion_tokens"), int):
                usage["completion"] = direct["completion_tokens"]
            if isinstance(direct.get("input_tokens"), int):
                usage["prompt"] = direct["input_tokens"]
            if isinstance(direct.get("output_tokens"), int):
                usage["completion"] = direct["output_tokens"]
        message = chunk.get("message")
        if isinstance(message, dict):
            GatewayHandler._usage(usage, {"usage": message.get("usage")})

    def _forward(self, method, path, data, stream, usage):
        cfg = self.gateway.config
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        headers = {
            "Accept": self.headers.get("Accept", "application/json"),
            "Authorization": "Bearer strata-local",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        for name in ("Anthropic-Version", "Anthropic-Beta"):
            if self.headers.get(name):
                headers[name] = self.headers[name]
        conn = http.client.HTTPConnection(self.gateway.upstream, self.gateway.upstream_port,
                                          timeout=cfg["upstream_timeout_s"])
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type")
                             or "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            payload = None
            if not stream:
                payload = response.read()
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.response_started = True
            self.close_connection = True
            if not stream:
                self.wfile.write(payload)
                try:
                    self._usage(usage, json.loads(payload.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            else:
                for raw in response:
                    self.wfile.write(raw)
                    self.wfile.flush()
                    if raw.startswith(b"data:"):
                        try:
                            chunk = json.loads(raw[5:].strip().decode("utf-8"))
                            self._usage(usage, chunk)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            pass
            return response.status
        finally:
            conn.close()

    def _handle(self):
        gateway = self.gateway
        started = time.monotonic()
        key_id = ""
        model = ""
        stream = False
        usage = {}
        status = 500
        self.response_started = False
        error = ""
        path = self.path.partition("?")[0]
        try:
            if self.command == "GET" and path == "/v1/models":
                pass
            elif self.command == "POST" and path in ("/v1/chat/completions", "/v1/messages"):
                pass
            elif path in {route for _method, route in ALLOWED_ROUTES}:
                raise Reject(405, "请求方法不允许")
            else:
                raise Reject(404, "端点不存在")
            key_id = gateway._authenticated(self)
            gateway._check_rate()
            data = None
            if self.command == "POST":
                data = self._read_json()
                model = data.get("model") or ""
                if not isinstance(model, str) or not model or len(model) > 200:
                    raise Reject(400, "model 必填")
                active_model = gateway._engine_model()
                if model != active_model:
                    raise Reject(400, f"model 必须是当前引擎模型：{active_model}")
                max_tokens = data.get("max_tokens")
                if (isinstance(max_tokens, bool) or not isinstance(max_tokens, int)
                        or not 1 <= max_tokens <= gateway.config["max_tokens"]):
                    raise Reject(400, f"max_tokens 必填，范围为 1–{gateway.config['max_tokens']}")
                if "stream" in data and not isinstance(data["stream"], bool):
                    raise Reject(400, "stream 必须是布尔值")
                stream = bool(data.get("stream"))
            gateway._enter_queue()
            try:
                gateway._check_quota()
                status = self._forward(self.command, path, data, stream, usage)
            finally:
                gateway._leave_queue()
        except Reject as exc:
            status = exc.status
            error = exc.message
            if not self.response_started:
                try:
                    self._error(exc.status, exc.message, exc.retry_after)
                except (BrokenPipeError, ConnectionResetError):
                    status = 499
        except (BrokenPipeError, ConnectionResetError):
            status = 499
            error = "client_disconnected"
        except (socket.timeout, TimeoutError):
            status = 504
            error = "upstream_timeout"
            if not self.response_started:
                try:
                    self._error(status, "上游请求超时")
                except (BrokenPipeError, ConnectionResetError):
                    status = 499
        except Exception as exc:
            status = 502
            error = type(exc).__name__
            if not self.response_started:
                try:
                    self._error(status, "Gateway 转发失败")
                except (BrokenPipeError, ConnectionResetError):
                    status = 499
        finally:
            prompt = int(usage.get("prompt") or 0)
            completion = int(usage.get("completion") or 0)
            gateway._record({
                "id": uuid.uuid4().hex[:12],
                "ts": time.time(),
                "key_id": key_id or "—",
                "route": path,
                "method": self.command,
                "model": model,
                "status": status,
                "ms": round((time.monotonic() - started) * 1000),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "stream": stream,
                "error": error,
            })
