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
    "keys": [],
    "quota_version": 3,
    "rate_per_min": 30,
    "daily_tokens": 1000000,
    "max_body_bytes": 4194304,
    "max_tokens": 8192,
    "short_queue_tokens": 262144,
    "long_queue_tokens": 1048576,
    "long_context_threshold": 32768,
    "queue_timeout_s": 30,
    "upstream_timeout_s": 1800,
}

LIMITS = {
    "port": (1024, 65535),
    "rate_per_min": (1, 1000),
    "max_body_bytes": (65536, 67108864),
    "max_tokens": (1, 32768),
    "short_queue_tokens": (1024, 268435456),
    "long_queue_tokens": (1024, 268435456),
    "long_context_threshold": (1024, 1048576),
    "queue_timeout_s": (1, 300),
    "upstream_timeout_s": (30, 86400),
}

ALLOWED_ROUTES = {
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/messages"),
}


def validate_quota(daily_tokens):
    if daily_tokens is None:
        return None
    if (isinstance(daily_tokens, bool) or not isinstance(daily_tokens, int)
            or not 0 <= daily_tokens <= 1000000000):
        raise ValueError("每日额度必须是 0–1000000000 之间的整数，null 为不限")
    return daily_tokens


def validate_money(daily_money):
    if daily_money is None:
        return None
    if not isinstance(daily_money, dict) or daily_money.get("currency") not in ("CNY", "USD"):
        raise ValueError("金额额度必须指定 CNY 或 USD")
    amount = daily_money.get("amount")
    if (isinstance(amount, bool) or not isinstance(amount, (int, float))
            or not 0 <= amount <= 1000000):
        raise ValueError("金额必须是 0–1000000 之间的数字")
    return {"currency": daily_money["currency"], "amount": float(amount)}


def validate_key(name, daily_tokens):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
        raise ValueError("Key 名称必须为 1–40 个字符")
    return name.strip(), validate_quota(daily_tokens)


def validate_config(data):
    if not isinstance(data, dict):
        raise ValueError("配置必须是对象")
    version = data.get("quota_version", 1)
    if version not in (1, 2, 3):
        raise ValueError("不支持的额度配置版本")
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
    if version == 1 and cfg["daily_tokens"] == 0:
        cfg["daily_tokens"] = None
    cfg["daily_tokens"] = validate_quota(cfg["daily_tokens"])
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
    if not isinstance(cfg["keys"], list):
        raise ValueError("keys 必须是数组")
    if not cfg["keys"] and cfg["key_hash"]:
        cfg["keys"] = [{"id": cfg["key_id"], "name": "主 Key", "hash": cfg["key_hash"],
                        "daily_tokens": cfg["daily_tokens"], "enabled": True,
                        "created_at": None}]
    keys = []
    ids = set()
    for item in cfg["keys"]:
        if not isinstance(item, dict):
            raise ValueError("Key 条目必须是对象")
        item = dict(item)
        if version == 1 and item.get("daily_tokens") == 0:
            item["daily_tokens"] = None
        key_id = item.get("id")
        key_hash = item.get("hash")
        if (not isinstance(key_id, str) or not key_id or len(key_id) > 64 or key_id in ids
                or not isinstance(key_hash, str) or len(key_hash) != 64):
            raise ValueError("Key ID 或哈希无效")
        name, daily = validate_key(item.get("name"), item.get("daily_tokens"))
        if not isinstance(item.get("enabled"), bool):
            raise ValueError("Key enabled 必须是布尔值")
        created = item.get("created_at")
        if created is not None and (isinstance(created, bool) or not isinstance(created, (int, float))):
            raise ValueError("Key created_at 无效")
        ids.add(key_id)
        keys.append({"id": key_id, "name": name, "hash": key_hash,
                     "daily_tokens": daily, "daily_money": validate_money(item.get("daily_money")),
                     "enabled": item["enabled"], "created_at": created})
    cfg["keys"] = keys
    cfg["key_hash"] = ""
    cfg["key_id"] = ""
    cfg["quota_version"] = 3
    return cfg


def hash_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def estimate_context_tokens(data):
    if not isinstance(data, dict):
        return 0
    state = {"bytes": 0, "images": 0}

    def measure(value, key=""):
        if isinstance(value, dict):
            kind = str(value.get("type") or value.get("kind") or "").lower()
            if kind in ("image", "image_url", "input_image"):
                state["images"] += 1
                return
            for child_key, child in value.items():
                measure(child, child_key)
        elif isinstance(value, list):
            for child in value:
                measure(child, key)
        elif isinstance(value, str):
            if value.startswith("data:image/"):
                state["images"] += 1
            elif key not in ("model", "role", "type", "kind"):
                state["bytes"] += len(value.encode("utf-8"))
        elif value is not None:
            state["bytes"] += 1

    measure(data)
    output = data.get("max_tokens") or 0
    return max(1, (state["bytes"] + 2) // 3) + state["images"] * 2048 + 32 + output


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
    def __init__(self, app_dir, upstream="127.0.0.1", upstream_port=8080,
                 models=None, on_switch=None):
        self.app_dir = Path(app_dir)
        self.upstream = upstream
        self.upstream_port = upstream_port
        self.models = models
        self.on_switch = on_switch
        self.config_file = self.app_dir / "gateway.json"
        self.state_file = self.app_dir / "gateway-state.json"
        self.audit_file = self.app_dir / "gateway-audit.jsonl"
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.queue = {}
        self.queue_tokens = {"short": 0, "long": 0}
        self.queue_sequence = 0
        self.schedule = 0
        self.active = 0
        self.active_kind = ""
        self.active_tokens = 0
        self.waiting = 0
        self.httpd = None
        self.thread = None
        self.rate = deque()
        self.config = self._load_config()
        self.state = self._load_state()
        self.recent = deque(self._read_audit(), maxlen=500)
        if self.config["keys"] and not self.state["key_days"]:
            today = self.state["days"].get(self._day())
            if today:
                self.state["key_days"][self.config["keys"][0]["id"]] = {self._day(): dict(today)}

    def _load_config(self):
        try:
            return validate_config(json.loads(self.config_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return dict(DEFAULT_CONFIG)

    def _load_state(self):
        blank = {"total_requests": 0, "total_tokens": 0, "days": {}, "key_days": {}}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("days"), dict):
                blank.update(data)
            if not isinstance(blank.get("key_days"), dict):
                blank["key_days"] = {}
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

    def _active_keys(self):
        return [item for item in self.config["keys"] if item["enabled"]]

    def _find_key(self, key_id):
        return next((item for item in self.config["keys"] if item["id"] == key_id), None)

    def keys_public(self):
        with self.lock:
            result = []
            for item in self.config["keys"]:
                day = self.state["key_days"].get(item["id"], {}).get(self._day(), {})
                result.append({
                    "id": item["id"], "name": item["name"],
                    "daily_tokens": item["daily_tokens"], "daily_money": item["daily_money"],
                    "enabled": item["enabled"], "created_at": item["created_at"],
                    "today_tokens": int(day.get("tokens", 0)),
                    "today_requests": int(day.get("requests", 0)),
                    "today_cost_cny": float(day.get("cost_cny", 0)),
                    "today_cost_usd": float(day.get("cost_usd", 0)),
                })
            return result

    def public(self):
        with self.lock:
            active_keys = self._active_keys()
            cfg = {k: v for k, v in self.config.items() if k not in ("key_hash", "keys")}
            cfg.update({
                "running": self.httpd is not None,
                "listen": f"http://127.0.0.1:{self.config['port']}",
                "key_set": bool(active_keys),
                "key_id": active_keys[0]["id"] if active_keys else "",
                "key_count": len(self.config["keys"]),
                "active": self.active,
                "active_kind": self.active_kind,
                "active_tokens": self.active_tokens,
                "waiting": self.waiting,
                "waiting_tokens": dict(self.queue_tokens),
            })
            return cfg

    def start(self):
        with self.lock:
            if self.httpd is not None:
                return
            if not self._active_keys():
                raise ValueError("请先注册 API Key")
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
                   and k not in ("key_hash", "key_id", "keys")}
        cfg = validate_config(dict(self.config, **allowed))
        if cfg["enabled"] and not any(item["enabled"] for item in cfg["keys"]):
            raise ValueError("启用前请先注册 API Key")
        was_running = self.httpd is not None
        restart = was_running and cfg["port"] != self.config["port"]
        if restart or (was_running and not cfg["enabled"]):
            self.stop()
        self.config = cfg
        self._save_json(self.config_file, cfg)
        if cfg["enabled"] and self.httpd is None:
            self.start()
        return self.public()

    def generate_key(self, name=None, daily_tokens=Ellipsis, daily_money=Ellipsis):
        with self.lock:
            if daily_tokens is Ellipsis:
                daily_tokens = self.config["daily_tokens"]
            if daily_money is Ellipsis:
                daily_money = None
            daily_money = validate_money(daily_money)
            name, daily_tokens = validate_key(name or f"API Key {len(self.config['keys']) + 1}",
                                              daily_tokens)
            if any(item["name"].casefold() == name.casefold() for item in self.config["keys"]):
                raise ValueError("Key 名称已存在")
            key = "sk-strata-" + secrets.token_urlsafe(32)
            digest = hash_key(key)
            key_id = digest[:10]
            while self._find_key(key_id):
                key = "sk-strata-" + secrets.token_urlsafe(32)
                digest = hash_key(key)
                key_id = digest[:10]
            self.config["keys"].append({
                "id": key_id, "name": name, "hash": digest,
                "daily_tokens": daily_tokens, "daily_money": daily_money,
                "enabled": True, "created_at": time.time(),
            })
            self._save_json(self.config_file, self.config)
            return key, self.public()

    def update_key(self, key_id, data):
        if not isinstance(data, dict):
            raise ValueError("配置必须是对象")
        with self.lock:
            item = self._find_key(key_id)
            if not item:
                raise ValueError("Key 不存在")
            name, daily = validate_key(data.get("name", item["name"]),
                                       data.get("daily_tokens", item["daily_tokens"]))
            daily_money = validate_money(data.get("daily_money", item["daily_money"]))
            if any(other["id"] != key_id and other["name"].casefold() == name.casefold()
                   for other in self.config["keys"]):
                raise ValueError("Key 名称已存在")
            enabled = data.get("enabled", item["enabled"])
            if not isinstance(enabled, bool):
                raise ValueError("enabled 必须是布尔值")
            item.update(name=name, daily_tokens=daily, daily_money=daily_money, enabled=enabled)
            self._save_json(self.config_file, self.config)
            return self.keys_public()

    def delete_key(self, key_id):
        with self.lock:
            remaining = [item for item in self.config["keys"] if item["id"] != key_id]
            if len(remaining) == len(self.config["keys"]):
                raise ValueError("Key 不存在")
            self.config["keys"] = remaining
            self._save_json(self.config_file, self.config)
            return self.keys_public()

    def _authenticated(self, handler):
        supplied = handler.headers.get("X-API-Key", "")
        authorization = handler.headers.get("Authorization", "")
        if authorization:
            scheme, _, value = authorization.partition(" ")
            supplied = value.strip() if scheme.lower() == "bearer" else ""
        if not supplied:
            raise Reject(401, "需要 Authorization: Bearer <API Key>")
        digest = hash_key(supplied)
        for item in self._active_keys():
            if secrets.compare_digest(digest, item["hash"]):
                return item["id"], item["name"]
        raise Reject(401, "API Key 无效")

    def _check_rate(self):
        now = time.monotonic()
        with self.lock:
            while self.rate and now - self.rate[0] >= 60:
                self.rate.popleft()
            if len(self.rate) >= self.config["rate_per_min"]:
                retry = max(1, int(60 - (now - self.rate[0])) + 1)
                raise Reject(429, "超过每分钟请求限制", retry)
            self.rate.append(now)

    def _check_quota(self, key_id):
        item = self._find_key(key_id)
        if not item:
            return
        day = self.state["key_days"].get(key_id, {}).get(self._day(), {})
        if item["daily_tokens"] is not None and int(day.get("tokens", 0)) >= item["daily_tokens"]:
            raise Reject(429, f"Key「{item['name']}」已达到今日 token 额度")
        money = item["daily_money"]
        if money is not None:
            currency = money["currency"]
            cost = float(day.get("cost_usd" if currency == "USD" else "cost_cny", 0))
            if cost >= money["amount"]:
                symbol = "$" if currency == "USD" else "¥"
                raise Reject(429, f"Key「{item['name']}」已达到今日金额额度 {symbol}{money['amount']:g}")

    def _next_ticket_locked(self):
        if self.active or not self.queue:
            return None
        kinds = {item["kind"] for item in self.queue.values()}
        if len(kinds) == 2:
            kind = "long" if self.schedule % 3 == 2 else "short"
        else:
            kind = next(iter(kinds))
        self.schedule += 1
        return min(ticket for ticket, item in self.queue.items() if item["kind"] == kind)

    def _enter_queue(self, context_tokens=0):
        kind = "long" if context_tokens >= self.config["long_context_threshold"] else "short"
        budget = self.config[kind + "_queue_tokens"]
        if context_tokens > budget:
            raise Reject(429, f"单请求超过{kind}上下文队列预算 {budget} tokens", 5)
        deadline = time.monotonic() + self.config["queue_timeout_s"]
        with self.condition:
            if self.queue_tokens[kind] + context_tokens > budget:
                raise Reject(429, f"{kind}上下文队列 token 预算已满", 5)
            self.queue_sequence += 1
            ticket = self.queue_sequence
            self.queue[ticket] = {"kind": kind, "tokens": context_tokens}
            self.queue_tokens[kind] += context_tokens
            self.waiting += 1
            acquired = False
            try:
                while True:
                    if self._next_ticket_locked() == ticket:
                        acquired = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.condition.wait(remaining)
                if not acquired:
                    raise Reject(429, "排队超时", 5)
                self.active = 1
                self.active_kind = kind
                self.active_tokens = context_tokens
            finally:
                item = self.queue.pop(ticket, None)
                if item is not None:
                    self.queue_tokens[kind] = max(0, self.queue_tokens[kind] - item["tokens"])
                self.waiting = max(0, self.waiting - 1)
                self.condition.notify_all()

    def _leave_queue(self):
        with self.condition:
            self.active = 0
            self.active_kind = ""
            self.active_tokens = 0
            self.condition.notify_all()

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

    def ensure_model(self, model):
        if self.on_switch is None:
            active_model = self._engine_model()
            if model != active_model:
                raise Reject(400, f"model 必须是当前引擎模型：{active_model}")
            return active_model
        if self.models is not None:
            available = {item.get("id") for item in self.models().get("data", [])}
            if model not in available:
                raise Reject(400, f"本机没有可用模型：{model}")
        try:
            return self.on_switch(model)
        except LookupError:
            raise Reject(400, f"本机没有可用模型：{model}")
        except Exception as exc:
            raise Reject(503, f"模型加载失败：{str(exc)[:300]}")

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
            day["cost_cny"] = float(day.get("cost_cny", 0)) + float(row.get("cost_cny") or 0)
            day["cost_usd"] = float(day.get("cost_usd", 0)) + float(row.get("cost_usd") or 0)
            self.state["total_requests"] = int(self.state.get("total_requests", 0)) + 1
            self.state["total_tokens"] = int(self.state.get("total_tokens", 0)) + tokens
            for old in sorted(self.state["days"])[:-62]:
                self.state["days"].pop(old, None)
            key_id = row.get("key_id")
            if key_id and key_id != "—":
                key_days = self.state["key_days"].setdefault(key_id, {})
                key_day = key_days.setdefault(day_name, {"requests": 0, "rejected": 0, "tokens": 0})
                key_day["requests"] = int(key_day.get("requests", 0)) + 1
                if row["status"] >= 400:
                    key_day["rejected"] = int(key_day.get("rejected", 0)) + 1
                key_day["tokens"] = int(key_day.get("tokens", 0)) + tokens
                key_day["cost_cny"] = float(key_day.get("cost_cny", 0)) + float(row.get("cost_cny") or 0)
                key_day["cost_usd"] = float(key_day.get("cost_usd", 0)) + float(row.get("cost_usd") or 0)
                for old in sorted(key_days)[:-62]:
                    key_days.pop(old, None)
            try:
                self._save_json(self.state_file, self.state)
                if self.audit_file.exists() and self.audit_file.stat().st_size > 8 * 1024 * 1024:
                    self.audit_file.replace(self.audit_file.with_suffix(".jsonl.1"))
                with self.audit_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def monitor(self, key_id=None):
        with self.lock:
            if key_id:
                day = dict(self.state["key_days"].get(key_id, {}).get(self._day(),
                             {"requests": 0, "rejected": 0, "tokens": 0}))
            else:
                day = dict(self.state["days"].get(self._day(),
                             {"requests": 0, "rejected": 0, "tokens": 0}))
            rows = [row for row in self.recent if not key_id or row.get("key_id") == key_id]
            return {
                "gateway": self.public(),
                "keys": self.keys_public(),
                "selected_key": key_id or "",
                "today": day,
                "total_requests": self.state.get("total_requests", 0),
                "total_tokens": self.state.get("total_tokens", 0),
                "recent": list(reversed(rows))[:100],
                "audit": "仅元数据；不保存 API Key、提示词或生成内容",
            }

    def clear_audit(self):
        with self.lock:
            self.recent.clear()
            self.state = {"total_requests": 0, "total_tokens": 0, "days": {}, "key_days": {}}
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
            details = direct.get("prompt_tokens_details")
            if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
                usage["cached"] = details["cached_tokens"]
            if isinstance(direct.get("cache_read_input_tokens"), int):
                usage["cached"] = direct["cache_read_input_tokens"]
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

    def _send_models(self, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.response_started = True
        self.close_connection = True
        self.wfile.write(raw)
        return 200

    def _handle(self):
        gateway = self.gateway
        started = time.monotonic()
        key_id = ""
        key_name = ""
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
            key_id, key_name = gateway._authenticated(self)
            gateway._check_rate()
            if self.command == "GET" and gateway.models is not None:
                gateway._check_quota(key_id)
                gateway._enter_queue(estimate_context_tokens(data))
                try:
                    return self._send_models(gateway.models())
                finally:
                    gateway._leave_queue()
            data = None
            if self.command == "POST":
                data = self._read_json()
                model = data.get("model") or ""
                if not isinstance(model, str) or not model or len(model) > 200:
                    raise Reject(400, "model 必填")
                if path == "/v1/chat/completions":
                    if "max_completion_tokens" in data:
                        alternate = data["max_completion_tokens"]
                        if "max_tokens" in data and data["max_tokens"] != alternate:
                            raise Reject(400, "max_tokens 与 max_completion_tokens 不能不一致")
                        data["max_tokens"] = alternate
                        data.pop("max_completion_tokens")
                    data.setdefault("max_tokens", gateway.config["max_tokens"])
                max_tokens = data.get("max_tokens")
                if (isinstance(max_tokens, bool) or not isinstance(max_tokens, int)
                        or not 1 <= max_tokens <= gateway.config["max_tokens"]):
                    raise Reject(400, f"max_tokens 范围为 1–{gateway.config['max_tokens']}；Anthropic 请求必填")
                if "stream" in data and not isinstance(data["stream"], bool):
                    raise Reject(400, "stream 必须是布尔值")
                stream = bool(data.get("stream"))
            gateway._enter_queue(estimate_context_tokens(data))
            try:
                gateway._check_quota(key_id)
                if self.command == "POST":
                    gateway.ensure_model(model)
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
            cached = min(prompt, max(0, int(usage.get("cached") or 0)))
            uncached = max(0, prompt - cached)
            cost_usd = (cached * 0.006 + uncached * 0.3 + completion * 1.2) / 1000000
            cost_cny = (cached * 0.04 + uncached * 2 + completion * 8) / 1000000
            gateway._record({
                "id": uuid.uuid4().hex[:12],
                "ts": time.time(),
                "key_id": key_id or "—",
                "key_name": key_name or "—",
                "route": path,
                "method": self.command,
                "model": model,
                "status": status,
                "ms": round((time.monotonic() - started) * 1000),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cached_tokens": cached,
                "total_tokens": prompt + completion,
                "cost_cny": cost_cny,
                "cost_usd": cost_usd,
                "stream": stream,
                "error": error,
            })
