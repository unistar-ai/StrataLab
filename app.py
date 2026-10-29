"""Strata Lab v0.4 — local console for the Strata inference engine.

Zero dependency (stdlib only), binds 127.0.0.1. Backend for ui.html.

  GET  /                     the page
  GET  /api/status           health, VRAM, RAM, profiles, jobs, stale markers
  GET  /api/stats            serving counters parsed from the engine log
                             (this engine run / every run in that log)
  GET  /api/gateway          public API gateway config, counters and audit metadata
  POST /api/gateway          update optional gateway configuration
  POST /api/gateway/key      register a named API key (returned once, stored hashed)
  POST /api/gateway/key/update  rename, change quota, enable or disable a key
  POST /api/gateway/key/delete  permanently delete a key, retaining its audit history
  POST /api/gateway/clear    clear gateway audit and token counters
  GET  /api/meta             /props summary: build, model path, sampling defaults
  GET  /api/live             current request phase, progress and speed
  GET  /api/metrics          sampled time series + parsed engine statistics
  GET  /api/scan             model directories under the configured Strata root
  GET  /api/profiles         profile definitions
  POST /api/profiles         save profile definitions to profiles.json
  GET  /api/conn             endpoints + the client apps and their commands
  GET  /api/logfiles         available engine logs
  GET  /api/logs?file=&grep= tail (optionally filtered)
  GET  /api/history?limit=   recent start/bench/chat records
  GET  /api/progress         jobs (chat jobs carry the streamed text)
  POST /api/start            {id, force?}    start a profile
  POST /api/stop             stop the engine
  POST /api/bench            {prompt?, max_tokens?, effort?}
  POST /api/chat             {prompt, max_tokens?, effort?, system?}
  POST /api/cancel           {id}
  POST /api/history/clear    empty history.json
  POST /api/download         {id, limit_bytes?}   fetch a missing first shard
"""

import base64
import ctypes
import ctypes.wintypes
import datetime
import json
import msvcrt
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gateway import GatewayService

APP_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("STRATA_LAB_ROOT", str(APP_DIR.parent))).resolve()
LOGS = APP_DIR / "logs"
STATIC_FILES = ("xterm.js", "xterm.css", "addon-fit.js")

# The terminal's shell (and pi inside it) needs node and git on PATH. Putting them into the
# lab's own environment once is enough: term_host.py inherits it and the shell inherits from
# there — building a custom environment block for CreateProcessW broke the ConPTY attach.
_extra_path = os.environ.get("STRATA_LAB_EXTRA_PATH", "")
_EXTRA_PATH = (_extra_path.split(os.pathsep) if _extra_path else
               [str(ROOT.parent / "node"), str(ROOT.parent / "node" / "global")])
_cur_path = os.environ.get("PATH", "")
os.environ["PATH"] = os.pathsep.join(
    [p for p in _EXTRA_PATH if os.path.isdir(p)] + ([_cur_path] if _cur_path else []))
LOGS.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = APP_DIR / "history.json"
PROFILES_FILE = APP_DIR / "profiles.json"
HISTORY_MAX = 300

STRATA_CMD = ROOT / "Strata-data" / "terminal" / "current" / "bin" / "strata.cmd"
DATA = ROOT / "Strata-data"
FEED = ROOT / "feed"
MODELS_ROOT = ROOT / "models"
ENGINE_LOGS = Path(os.environ["LOCALAPPDATA"]) / "Strata" / "launch"
RUNTIME = Path(os.environ["LOCALAPPDATA"]) / "Strata" / "runtime"

PORT = 8765
BASE = "http://127.0.0.1:8080"
HF = "https://hf-mirror.com"
HF_REPO = "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF"

DEFAULT_PROFILES = {
    "fast": {
        "title": "Fast", "model_label": "IQ3_XXS · 3.2 bit",
        "family": "qwen", "model": "iq3_xxs", "path": str(MODELS_ROOT / "IQ3_XXS"),
        "served_id": "qwen3.8-flash-next-iq3_xxs",
        "ctx": 262144, "kv": "k8v4", "vision": "cpu", "parallel": 1, "extra": [],
        "desc": "日常主力。42.9 GB 专家全驻留内存，速度最快。",
        "speed": "实测 decode 60–78 tok/s · 预填 ~1550 tok/s",
        "shard": ["Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf", 47039860096],
        "repo_dir": "IQ3_XXS",
    },
    "balanced": {
        "title": "Balanced", "model_label": "IQ3_S · 3.5 bit",
        "family": "qwen", "model": "iq3_s", "path": str(MODELS_ROOT / "IQ3_S"),
        "served_id": "qwen3.8-flash-next-iq3_s",
        "ctx": 262144, "kv": "k8v4", "vision": "cpu", "parallel": 1, "extra": [],
        "desc": "质量与速度兼顾。官方评测打平满血，50.3 GB 专家池需全驻留。",
        "speed": "实测 decode ~67 tok/s · 预填 ~1428 tok/s",
        "shard": ["Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf", 54817524224],
        "repo_dir": "IQ3_S",
    },
    "quality": {
        "title": "Quality", "model_label": "UD-Q4_K_XL · 4 bit",
        "family": "unsloth", "model": "ud-q4_k_xl", "path": str(MODELS_ROOT / "UD-Q4_K_XL"),
        "served_id": "qwen3.8-flash-next-unsloth-ud-q4_k_xl",
        "ctx": 262144, "kv": "k8v4", "vision": "cpu", "parallel": 1,
        "extra": ["--set", "resident-budget-gib=44"],
        "desc": "分布保真度最高。64.5 GiB 专家池只能部分常驻（实测 28–43 GiB），其余按需从 SSD 读取，预填和生成都较慢，适合更看重质量的任务。",
        "speed": "实测 decode 25–27 tok/s · 预填 ~724 tok/s",
        "shard": ["Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf", 10946624],
        "repo_dir": "UD-Q4_K_XL",
    },
}

LOCK = threading.Lock()
JOBS = {}
JOB_ORDER = []
METRICS = deque(maxlen=300)
LAST_ENGINE = {}
GATEWAY = GatewayService(APP_DIR)

# Every subprocess call below runs a console tool (nvidia-smi / tasklist / strata.cmd).
# Without CREATE_NO_WINDOW each one allocates a console and the user sees a window
# flash — the sampler alone would do it every two seconds.
NOWINDOW = 0x08000000 if os.name == "nt" else 0
VRAM_CACHE = {}
HTTPD = None
LAST_BEAT = {"t": 0.0}
# Close the app window and the lab (and engine) go away. Override for testing:
#   set STRATA_LAB_EXIT_S=25
NO_WINDOW_EXIT_S = int(os.environ.get("STRATA_LAB_EXIT_S", "180"))


# ---------------------------------------------------------------- configuration

def load_profiles():
    if PROFILES_FILE.exists():
        try:
            data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                merged = {}
                for pid, p in data.items():
                    base = json.loads(json.dumps(DEFAULT_PROFILES.get(pid, {})))
                    base.update(p)
                    merged[pid] = base
                return merged
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_PROFILES))


PROFILES = load_profiles()


def save_profiles(data):
    global PROFILES
    PROFILES = data
    PROFILES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- engine helpers

def health(timeout=3):
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def mem_stats():
    """(free_gb, total_gb) straight from GlobalMemoryStatusEx — no subprocess."""
    class MEMSTAT(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("memoryLoad", ctypes.c_ulong),
                    ("totalPhys", ctypes.c_ulonglong), ("availPhys", ctypes.c_ulonglong),
                    ("totalPageFile", ctypes.c_ulonglong), ("availPageFile", ctypes.c_ulonglong),
                    ("totalVirtual", ctypes.c_ulonglong), ("availVirtual", ctypes.c_ulonglong),
                    ("availExtendedVirtual", ctypes.c_ulonglong)]
    m = MEMSTAT()
    m.length = ctypes.sizeof(MEMSTAT)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return round(m.availPhys / 2**30, 1), round(m.totalPhys / 2**30, 1)


def ram_free_gb():
    return mem_stats()[0]


def vram():
    """Serve the last line of one long-lived `nvidia-smi --loop-ms` process.

    Spawning nvidia-smi per poll was both wasteful and — before CREATE_NO_WINDOW on
    every call site — the source of a console window flashing every couple of seconds.
    A single loop process removes the spawn entirely."""
    now = time.time()
    if VRAM_CACHE and now - VRAM_CACHE.get("t", 0) < 15:
        c = dict(VRAM_CACHE)
        c.pop("t", None)
        return c
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=NOWINDOW)
        used, total = out.stdout.strip().split(",")
        used, total = int(used), int(total)
        VRAM_CACHE.update({"used": used, "total": total, "free": total - used, "t": now})
        return {"used": used, "total": total, "free": total - used}
    except Exception:
        return None


def vram_loop():
    """One nvidia-smi for the lifetime of the UI."""
    while True:
        try:
            p = subprocess.Popen(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                 "--format=csv,noheader,nounits", "--loop-ms=2000"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                creationflags=NOWINDOW)
            for line in p.stdout:
                parts = [x.strip() for x in line.strip().split(",")]
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    used, total = int(parts[0]), int(parts[1])
                    VRAM_CACHE.update({"used": used, "total": total,
                                       "free": total - used, "t": time.time()})
        except Exception:
            time.sleep(3)
        time.sleep(2)


def comfy_running():
    """ComfyUI's default port. A cheap, reliable signal that another GPU-heavy app is
    up: nvidia-smi's compute-apps list reports [N/A] memory on this driver and also
    lists desktop apps, so it cannot tell us who is actually competing for VRAM."""
    import socket
    for port in (8188, 8189):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return port
        except OSError:
            continue
    return 0


TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260)]


def process_names():
    """Enumerate process names with Toolhelp32 — no subprocess, so no console window."""
    names = set()
    try:
        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1:
            return names
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.add(entry.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
        k32.CloseHandle(snap)
    except Exception:
        pass
    return names


def engine_running():
    return "strata.exe" in process_names()


def pid_alive(pid):
    """OpenProcess, not os.kill: on Windows a reused PID would make os.kill lie."""
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        k32.CloseHandle(handle)
        return True
    except Exception:
        return False


def clean_stale_markers():
    """Clear what a killed launcher leaves in %LOCALAPPDATA%\\Strata\\runtime.

    Three kinds of leftovers, all of which make the next `strata launch` refuse:
      *.child.json / *.ready.json  — the live-runtime ownership records
      launch-<profile>.json        — the session record, which also makes `--stop`
                                     answer "lost host has no valid child ownership
                                     record" and exit 2
    A session record is only removed when the host_pid inside it is dead; if that
    process is still alive a launch really is in progress and we leave everything alone.
    """
    if engine_running():
        return 0
    removed = 0
    try:
        for pat in ("*.child.json", "*.ready.json"):
            for f in RUNTIME.glob(pat):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        for f in RUNTIME.glob("launch-*.json"):
            if f.name.endswith((".child.json", ".ready.json")):
                continue
            try:
                pid = json.loads(f.read_text(encoding="utf-8")).get("host_pid")
            except Exception:
                pid = None
            if pid and pid_alive(pid):
                continue
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


def ready(pid):
    p = PROFILES[pid]
    name, size = p["shard"][0], int(p["shard"][1])
    f = Path(p["path"]) / name
    have = f.stat().st_size if f.exists() else 0
    if have != size:
        return (False, have, size)
    match = re.search(r"-00001-of-(\d{5})\.gguf$", name)
    if match:
        for index in range(2, int(match.group(1)) + 1):
            part = f.with_name(name.replace("-00001-of-", f"-{index:05d}-of-"))
            if not part.is_file() or part.stat().st_size == 0:
                return (False, have, size)
    return (True, have, size)


def engine_log_for(pid):
    p = PROFILES[pid]
    return ENGINE_LOGS / f"{p['model']}-{int(p.get('ctx', 262144)) // 1024}k.engine.log"


def current_log(h=None):
    h = h or health()
    if h and h.get("model"):
        for pid, p in PROFILES.items():
            if p.get("served_id") == h["model"]:
                f = engine_log_for(pid)
                if f.exists():
                    return f
    files = sorted(ENGINE_LOGS.glob("*.engine.log"), key=lambda x: x.stat().st_mtime, reverse=True)
    return files[0] if files else None


def tail(path, n=80, grep=None):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if grep:
        rx = re.compile(grep, re.I)
        lines = [x for x in lines if rx.search(x)]
    return lines[-n:]


def stop_engine():
    try:
        subprocess.run([str(STRATA_CMD), "launch", "--stop"], capture_output=True,
                       timeout=120, creationflags=NOWINDOW)
    except Exception:
        pass


RE_PROMPT = re.compile(
    r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in (\d+) ms \(([\d.]+) tok/s\), "
    r"(\d+) generated in (\d+) ms \(([\d.]+) tok/s\)")
RE_CACHE = re.compile(r"decode expert cache hit rate: ([\d.]+)%")
RE_ANSWER = re.compile(r"answering: (\d+) of max (\d+) tokens, ([\d.]+) tok/s")
RE_WARN = re.compile(r"WARNING")


def engine_stats(path=None):
    """Parse the newest interesting lines out of the engine log."""
    f = path or current_log()
    stats = {"log": f.name if f else "", "prompt_tps": None, "decode_tps": None,
             "cache_hit": None, "last_prompt_tokens": None, "warnings": 0,
             "answering": None}
    if not f:
        return stats
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-800:]
    except OSError:
        return stats
    for line in reversed(lines):
        m = RE_PROMPT.search(line)
        if m and stats["decode_tps"] is None:
            stats["last_prompt_tokens"] = int(m.group(1))
            stats["prompt_tps"] = float(m.group(5))
            stats["decode_tps"] = float(m.group(8))
        m = RE_CACHE.search(line)
        if m and stats["cache_hit"] is None:
            stats["cache_hit"] = float(m.group(1))
        m = RE_ANSWER.search(line)
        if m and stats["answering"] is None:
            stats["answering"] = {"tokens": int(m.group(1)), "max": int(m.group(2)),
                                  "tps": float(m.group(3))}
        if stats["decode_tps"] is not None and stats["cache_hit"] is not None and stats["answering"]:
            break
    stats["warnings"] = sum(1 for x in lines if RE_WARN.search(x))
    return stats


# ---------------------------------------------------------------- serving counters
#
# The engine prints one summary line per finished request:
#
#   strata serve: prompt 24 tokens = 17 reused + 7 read in 245 ms (28.6 tok/s),
#   59 generated in 2020 ms (29.2 tok/s), drafts accepted 33 of 57, 1 checkpoints
#
# and that is the only place that knows about *every* client: DSH, pi, Kimi and this
# page all go through the same server, so counting here counts all of them. The log
# is read incrementally (byte offset + partial-line buffer) so a long session never
# re-parses the whole file on every poll.

COUNTERS = ("requests", "prompt", "reused", "read", "gen",
            "prompt_ms", "gen_ms", "draft_ok", "draft_all")


def blank_counters():
    return dict.fromkeys(COUNTERS, 0)


LOGSTAT = {
    "path": None, "offset": 0, "partial": "",
    "run": blank_counters(), "all": blank_counters(),
    "run_started": None, "expert": {"hit": None, "hits": 0, "lookups": 0},
    "last": None, "warnings": 0,
}
LOG_LOCK = threading.Lock()

RE_REQUEST = re.compile(
    r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in (\d+) ms \(([\d.]+) tok/s\), "
    r"(\d+) generated in (\d+) ms \(([\d.]+) tok/s\)(?:, drafts accepted (\d+) of (\d+))?")
RE_HIT = re.compile(r"decode expert cache hit rate: ([\d.]+)% \((\d+) hits / (\d+) lookups\)")
RE_BOOT = re.compile(r"^\[strata\] (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) engine started")


def scan_engine_log(f=None):
    """Fold new request lines into the counters; reads only the bytes that are new."""
    with LOG_LOCK:
        f = f or current_log()
        if not f:
            return
        key = str(f)
        try:
            size = f.stat().st_size
        except OSError:
            return
        if key != LOGSTAT["path"] or size < LOGSTAT["offset"]:
            # another engine, another log, or the file was truncated
            LOGSTAT.update(path=key, offset=0, partial="", run=blank_counters(),
                           all=blank_counters(), run_started=None, last=None, warnings=0,
                           expert={"hit": None, "hits": 0, "lookups": 0})
        if size == LOGSTAT["offset"]:
            return
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(LOGSTAT["offset"])
                chunk = fh.read()
                LOGSTAT["offset"] = fh.tell()
        except OSError:
            return
        text = LOGSTAT["partial"] + chunk
        lines = text.split("\n")
        LOGSTAT["partial"] = lines.pop() if lines else ""
        for line in lines:
            m = RE_BOOT.search(line)
            if m:
                try:
                    LOGSTAT["run_started"] = datetime.datetime.strptime(
                        m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    LOGSTAT["run_started"] = time.time()
                LOGSTAT["run"] = blank_counters()
                continue
            m = RE_REQUEST.search(line)
            if m:
                g = m.groups()
                row = {"prompt": int(g[0]), "reused": int(g[1]), "read": int(g[2]),
                       "prompt_ms": int(g[3]), "prefill_tps": float(g[4]),
                       "gen": int(g[5]), "gen_ms": int(g[6]), "decode_tps": float(g[7]),
                       "draft_ok": int(g[8] or 0), "draft_all": int(g[9] or 0),
                       "at": time.time()}
                for block in (LOGSTAT["all"], LOGSTAT["run"]):
                    block["requests"] += 1
                    for k in ("prompt", "reused", "read", "gen", "prompt_ms", "gen_ms"):
                        block[k] += row[k]
                    block["draft_ok"] += row["draft_ok"]
                    block["draft_all"] += row["draft_all"]
                LOGSTAT["last"] = row
                continue
            m = RE_HIT.search(line)
            if m:
                LOGSTAT["expert"] = {"hit": float(m.group(1)), "hits": int(m.group(2)),
                                     "lookups": int(m.group(3))}
            elif "WARNING" in line:
                LOGSTAT["warnings"] += 1


def stat_block(c):
    secs = c["prompt_ms"] / 1000.0
    gen_secs = c["gen_ms"] / 1000.0
    return {
        "requests": c["requests"],
        "prompt": c["prompt"], "reused": c["reused"], "read": c["read"], "gen": c["gen"],
        "tokens": c["prompt"] + c["gen"],
        # the engine's own tok/s numbers divide the tokens it actually computed by the
        # time spent, so a cached prefix is already excluded from the prefill rate.
        "prefill_tps": round(c["read"] / secs, 1) if secs > 0 else None,
        "decode_tps": round(c["gen"] / gen_secs, 1) if gen_secs > 0 else None,
        "cache_eff": round(100.0 * c["reused"] / c["prompt"], 1) if c["prompt"] else None,
        "draft_rate": round(100.0 * c["draft_ok"] / c["draft_all"], 1) if c["draft_all"] else None,
        "avg_seconds": round((secs + gen_secs) / c["requests"], 1) if c["requests"] else None,
    }


def stats_payload():
    scan_engine_log()
    with LOG_LOCK:
        return {
            "log": Path(LOGSTAT["path"]).name if LOGSTAT["path"] else "",
            "run_started": LOGSTAT["run_started"],
            "run": stat_block(LOGSTAT["run"]),
            "all": stat_block(LOGSTAT["all"]),
            "expert": dict(LOGSTAT["expert"]),
            "last": LOGSTAT["last"],
            "warnings": LOGSTAT["warnings"],
            "now": time.time(),
        }


META = {"t": 0.0, "data": {}}


def engine_meta():
    """A trimmed /props: the chat template in there is ~10 KB of jinja nobody needs."""
    now = time.time()
    if META["data"] and now - META["t"] < 30:
        return META["data"]
    try:
        with urllib.request.urlopen(BASE + "/props", timeout=3) as r:
            p = json.load(r)
    except Exception:
        return META["data"]
    gen = p.get("default_generation_settings") or {}
    params = gen.get("params") or {}
    META["data"] = {
        "build": p.get("build_info", ""),
        "model_path": p.get("model_path", ""),
        "alias": p.get("model_alias", ""),
        "slots": p.get("total_slots"),
        "n_ctx": gen.get("n_ctx"),
        "temperature": params.get("temperature"),
        "top_p": params.get("top_p"),
        "top_k": params.get("top_k"),
        "vision": bool((p.get("modalities") or {}).get("vision")),
        "sleeping": p.get("is_sleeping"),
    }
    META["t"] = now
    return META["data"]


LIVE_CACHE = {"t": 0.0, "data": None}
LIVE_LOCK = threading.Lock()


def engine_live():
    """The current request and recent rates, without prompts or other metrics history."""
    now = time.monotonic()
    with LIVE_LOCK:
        if LIVE_CACHE["data"] is not None and now - LIVE_CACHE["t"] < 0.4:
            return dict(LIVE_CACHE["data"])
        try:
            with urllib.request.urlopen(BASE + "/metrics", timeout=2) as r:
                payload = json.load(r)
            live = payload.get("live") or {}
            if not isinstance(live, dict):
                live = {}
            history = payload.get("history") or {}
            if not isinstance(history, dict):
                history = {}
            live["history"] = {name: list(history.get(name) or [])[-60:]
                               for name in ("tok_s", "prefill_tok_s_mean")}
            live["sampled_at"] = time.time()
            LIVE_CACHE.update(t=now, data=live)
        except Exception:
            LIVE_CACHE.update(t=now, data={"state": "unavailable", "sampled_at": time.time()})
        return dict(LIVE_CACHE["data"])


# ---------------------------------------------------------------- client apps
#
# Modelled on oMLX's dashboard: the useful thing on a local server's front page is
# "what do I type to point my tool at this?" — not another gauge. `strata connect`
# knows every harness Strata supports, so the console only has to name them and show
# whether their config is already on disk.

CLIENT_APPS = [
    {"id": "deepseek-harness", "name": "DeepSeek Harness",
     "sub": "桌面 GUI · profile patch 指向本机", "cmd": "strata connect deepseek-harness --write",
     "probe": [".dsh"]},
    {"id": "pi", "name": "Pi Agent",
     "sub": "终端 · 装了 powerline / TPS 插件", "cmd": "strata connect pi --write",
     "probe": [".pi/agent/models.json"]},
    {"id": "kimi", "name": "Kimi Code",
     "sub": "config.toml 里手写 openai provider", "cmd": 'base_url = "http://127.0.0.1:8080/v1"',
     "probe": [".kimi-code/config.toml"], "manual": True},
    {"id": "claude-code", "name": "Claude Code",
     "sub": "Anthropic 协议 · /v1/messages", "cmd": "strata connect claude-code --write",
     "probe": [".claude"]},
    {"id": "opencode", "name": "OpenCode",
     "sub": "opencode.json 已存在", "cmd": "strata connect opencode --write",
     "probe": [".config/opencode/opencode.json"]},
    {"id": "codex", "name": "Codex CLI",
     "sub": "OpenAI 协议", "cmd": "strata connect codex --write",
     "probe": [".codex"]},
]


def client_apps():
    home = Path(os.environ.get("USERPROFILE") or str(Path.home()))
    out = []
    for app in CLIENT_APPS:
        hit = next((home / p for p in app["probe"] if (home / p).exists()), None)
        out.append({"id": app["id"], "name": app["name"], "sub": app["sub"],
                    "cmd": app["cmd"], "manual": bool(app.get("manual")),
                    "configured": hit is not None,
                    "config_path": str(hit) if hit else str(home / app["probe"][0]),
                    "cli": bool(shutil.which(app["id"].replace("-code", "")))})
    return out


# ---------------------------------------------------------------- terminal (ConPTY)
#
# Windows has no POSIX pty. ConPTY (Windows 10 1809+) is the pseudo console the modern
# terminals are built on, and it is reachable from plain ctypes: CreatePseudoConsole
# plus one PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE entry in the child's attribute list.
# No pywinpty, no node-pty, no bundled DLL.
#
#   pipes:  we write in_w  ->  child reads in_r
#           child writes out_w  ->  we read out_r
#
# Two details that cost an afternoon each if you get them wrong:
#   * the attribute constant is 0x00020016 (x/sys/windows), not the naive macro
#     expansion 0x00160002 — the latter makes UpdateProcThreadAttribute fail with 50;
#   * do not pass CREATE_NO_WINDOW: the child's console *is* the pseudo console and it
#     has no window, so there is nothing to hide.
# Escape sequences are captured verbatim and rendered by xterm.js in the page.

W32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD), ("lpReserved", ctypes.wintypes.LPWSTR),
        ("lpDesktop", ctypes.wintypes.LPWSTR), ("lpTitle", ctypes.wintypes.LPWSTR),
        ("dwX", ctypes.wintypes.DWORD), ("dwY", ctypes.wintypes.DWORD),
        ("dwXSize", ctypes.wintypes.DWORD), ("dwYSize", ctypes.wintypes.DWORD),
        ("dwXCountChars", ctypes.wintypes.DWORD), ("dwYCountChars", ctypes.wintypes.DWORD),
        ("dwFillAttribute", ctypes.wintypes.DWORD), ("dwFlags", ctypes.wintypes.DWORD),
        ("wShowWindow", ctypes.wintypes.WORD), ("cbReserved2", ctypes.wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", ctypes.wintypes.HANDLE), ("hStdOutput", ctypes.wintypes.HANDLE),
        ("hStdError", ctypes.wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESSINFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.wintypes.HANDLE), ("hThread", ctypes.wintypes.HANDLE),
                ("dwProcessId", ctypes.wintypes.DWORD), ("dwThreadId", ctypes.wintypes.DWORD)]


class _SECURITYATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", ctypes.wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", ctypes.wintypes.BOOL)]


W32.CreatePseudoConsole.argtypes = [_COORD, ctypes.wintypes.HANDLE, ctypes.wintypes.HANDLE,
                                    ctypes.wintypes.DWORD,
                                    ctypes.POINTER(ctypes.wintypes.HANDLE)]
W32.CreatePseudoConsole.restype = ctypes.c_long
W32.ResizePseudoConsole.argtypes = [ctypes.wintypes.HANDLE, _COORD]
W32.ClosePseudoConsole.argtypes = [ctypes.wintypes.HANDLE]
W32.CreatePipe.argtypes = [ctypes.POINTER(ctypes.wintypes.HANDLE),
                           ctypes.POINTER(ctypes.wintypes.HANDLE),
                           ctypes.POINTER(_SECURITYATTRIBUTES), ctypes.wintypes.DWORD]
W32.CreateProcessW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPWSTR,
                               ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.BOOL,
                               ctypes.wintypes.DWORD, ctypes.c_void_p,
                               ctypes.wintypes.LPCWSTR, ctypes.c_void_p,
                               ctypes.POINTER(_PROCESSINFORMATION)]
W32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]

PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400

TERM_KINDS = ("cmd", "pwsh", "pi", "python")


def term_command(kind):
    """(command line, working directory) for a terminal preset."""
    cwd = str(ROOT)
    lab_python = DATA / "terminal" / "current" / "python" / "python.exe"
    if kind == "pwsh":
        exe = shutil.which("pwsh") or shutil.which("powershell")
        return '"%s" -NoLogo' % (exe or "powershell.exe"), cwd
    if kind == "pi":
        return 'cmd.exe /K pi', cwd
    if kind == "python" and lab_python.exists():
        return '"%s" -i' % lab_python, cwd
    return "cmd.exe /K", cwd


class TermHost:
    """Owns one `term_host.py` process and a byte ring the page polls.

    The pty lives in that child process on purpose (see term_host.py). The two sides talk
    through files under `run/` because the child's stdio pipes turned out to be unreliable
    while a pty is attached: <base>.out (pty bytes), <base>.in (keystrokes),
    <base>.ctl (resize / stop), <base>.alive (keep-alive).
    """

    MAX_BUF = 400_000

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.pid = 0
        self.cols, self.rows = 160, 40
        self.buf = bytearray()
        self.base = 0
        self.total = 0
        self.alive = False
        self.kind = ""
        self.err = ""
        self.gen = 0          # bumped on every start; stale reader threads must stop
        self.base_path = None
        self.out_fh = None
        self.in_fh = None
        self.ctl_fh = None

    # ---------------------------------------------------------------- start
    def start(self, kind="cmd", cols=160, rows=40):
        self.stop()
        kind = kind if kind in TERM_KINDS else "cmd"
        self.cols, self.rows = max(20, int(cols)), max(5, int(rows))
        host = APP_DIR / "term_host.py"
        if not host.exists():
            self.err = "term_host.py missing"
            return False
        run = APP_DIR / "run"
        run.mkdir(exist_ok=True)
        base = run / "terminal"
        for suffix in (".out", ".in", ".ctl", ".log"):
            try:
                Path(str(base) + suffix).unlink()
            except OSError:
                pass
        try:
            Path(str(base) + ".out").write_bytes(b"")
            Path(str(base) + ".in").write_bytes(b"")
            Path(str(base) + ".ctl").write_bytes(b"")
            Path(str(base) + ".alive").write_bytes(b"1")
        except OSError as exc:
            self.err = "prepare: %s" % exc
            return False

        py = sys.executable or "pythonw.exe"
        try:
            # No stdio of any kind. A host that owns *valid* handles (a log file, NUL, even
            # pipes) makes the shell it spawns write there instead of into the pseudo
            # console, and the terminal stays empty. term_host.py logs its own traceback
            # into run/terminal.log instead of relying on stderr.
            proc = subprocess.Popen(
                [py, str(host), kind, str(self.cols), str(self.rows), str(ROOT), str(base)],
                creationflags=subprocess.DETACHED_PROCESS)
        except OSError as exc:
            self.err = "spawn: %s" % exc
            log_ui("terminal host spawn failed: %s" % exc)
            return False

        self.base_path = str(base)
        self.out_fh = open(str(base) + ".out", "rb", buffering=0)
        self.in_fh = open(str(base) + ".in", "ab", buffering=0)
        self.ctl_fh = open(str(base) + ".ctl", "ab", buffering=0)
        self.proc = proc
        self.pid = proc.pid
        with self.lock:
            self.gen += 1
            gen = self.gen
            self.buf = bytearray()      # a new session starts with a clean screen
            self.base = 0
            self.total = 0
            self.alive = True
            self.kind = kind
            self.err = ""
        log_ui("terminal started: %s (host pid %d, %dx%d)" % (kind, proc.pid, self.cols, self.rows))
        threading.Thread(target=self._read_loop, args=(proc, gen), daemon=True).start()
        return True

    # ---------------------------------------------------------------- io
    def _read_loop(self, proc, gen):
        tick = 0
        while True:
            if self.gen != gen:        # a newer session took over
                return
            try:
                try:
                    data = self.out_fh.read()
                except (OSError, ValueError):
                    data = b""
                if data:
                    with self.lock:
                        self.buf += data
                        self.total += len(data)
                        if len(self.buf) > self.MAX_BUF:
                            drop = len(self.buf) - self.MAX_BUF
                            del self.buf[:drop]
                            self.base += drop
                else:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.03)
                tick += 1
                if tick % 20 == 0 and self.base_path:      # keep-alive for the host
                    try:
                        Path(self.base_path + ".alive").write_bytes(b"1")
                    except OSError:
                        pass
            except Exception:
                # A reader thread that dies stops the keep-alive, the host then shuts the
                # session down, and the page shows "未启动" while the shell is still running.
                time.sleep(0.1)
        with self.lock:
            if self.gen == gen:
                self.alive = False

    def read_since(self, since):
        with self.lock:
            if since < self.base:
                return bytes(self.buf), self.total
            start = since - self.base
            if start >= len(self.buf):
                return b"", self.total
            return bytes(self.buf[start:]), self.total

    def write(self, text):
        if not text or self.in_fh is None:
            return False
        data = text.encode("utf-8", "replace") if isinstance(text, str) else text
        try:
            self.in_fh.write(data)
            return True
        except (OSError, ValueError):
            return False

    def resize(self, cols, rows):
        cols, rows = max(20, int(cols)), max(5, int(rows))
        self.cols, self.rows = cols, rows
        if self.ctl_fh is None:
            return False
        try:
            self.ctl_fh.write(b"%dx%d\n" % (cols, rows))
            return True
        except (OSError, ValueError):
            return False

    def state(self):
        proc = self.proc
        alive = self.alive and proc is not None and proc.poll() is None
        with self.lock:
            return {"alive": alive, "pid": self.pid, "cols": self.cols, "rows": self.rows,
                    "total": self.total, "kind": self.kind, "err": self.err}

    # ---------------------------------------------------------------- stop
    def stop(self):
        proc = self.proc
        self.proc = None
        with self.lock:
            self.gen += 1
            self.alive = False
        if self.ctl_fh is not None:
            try:
                self.ctl_fh.write(b"stop\n")
                self.ctl_fh.close()
            except (OSError, ValueError):
                pass
        if proc is not None:
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for fh in (self.out_fh, self.in_fh):
            try:
                if fh is not None:
                    fh.close()
            except Exception:
                pass
        self.out_fh = self.in_fh = self.ctl_fh = None
        self.pid = 0


TERM = TermHost()


def sampler():
    while True:
        try:
            h = health(timeout=2)
            v = vram()
            METRICS.append({"t": time.time(), "loaded": bool(h and h.get("loaded")),
                            "vram": v["used"] if v else None,
                            "vram_total": v["total"] if v else None,
                            "ram": ram_free_gb()})
            LAST_ENGINE.update(engine_stats())
            scan_engine_log(current_log(h))
        except Exception:
            pass
        time.sleep(2)


def log_ui(msg):
    """Same idea as log_window, but for the lab process itself (pythonw has no console)."""
    try:
        with open(LOGS / "ui.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg + "\n")
    except OSError:
        pass


def keep_engine_after_no_heartbeat():
    """Heartbeat silence alone must not end the session: a visible window or a request
    from any client keeps it alive. Returns (keep, detail) for the watchdog log."""
    windows = lab_windows()
    if windows:
        return True, "%d app window(s) still open" % len(windows)
    live = engine_live()
    state = str(live.get("state") or "unavailable")
    try:
        queued = int(live.get("queued") or 0)
    except (TypeError, ValueError):
        queued = 0
    if state in ("reading", "generating") or queued > 0:
        return True, "engine is %s with %d queued" % (state, queued)
    return False, "no app window, engine %s, queue %d" % (state, queued)


def watchdog():
    """Heartbeat backstop. It stops the engine only when there is no app window AND no
    page heartbeat AND no request in flight for any client. A frozen page behind a still
    visible window used to kill 40 GB of working model; the window now vetoes that."""
    while True:
        time.sleep(5)
        t = LAST_BEAT["t"]
        age = int(time.time() - t) if t else 0
        if not t or age < NO_WINDOW_EXIT_S:
            LAST_BEAT.pop("hold", None)
            continue
        with LOCK:
            busy = any(j["state"] == "running" for j in JOBS.values())
        if busy:
            continue
        keep, detail = keep_engine_after_no_heartbeat()
        if keep:
            if LAST_BEAT.get("hold") != detail:
                log_ui("heartbeat silent %ss but %s — keeping engine" % (age, detail))
                LAST_BEAT["hold"] = detail
            continue
        LAST_BEAT.pop("hold", None)
        log_ui("no heartbeat for %ss; %s — stopping the engine and exiting" % (age, detail))
        try:
            print("no console window for %ss — stopping the engine and exiting" % age, flush=True)
        except Exception:
            pass
        TERM.stop()
        stop_engine()
        os._exit(0)


# ---------------------------------------------------------------- window lifetime
#
# Closing the window should kill the engine *now*, not three minutes later. The heartbeat
# only says "some page has not talked to me recently"; the top-level window is the direct
# signal, so we look for it with EnumWindows and watch it go away.

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def lab_windows():
    """Visible top-level windows that belong to the console (title + browser class)."""
    found = []
    user32 = ctypes.windll.user32

    def each(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 2)
            user32.GetWindowTextW(hwnd, buf, n + 2)
            title = buf.value
            if not title.startswith("Strata Lab"):
                return True
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cls, 128)
            # Chrome_WidgetWin_0/1 is chromium's top-level window class; matching on it
            # keeps a notepad or terminal that happens to be called Strata Lab out.
            if cls.value.startswith("Chrome_WidgetWin"):
                found.append((hwnd, title))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(each), 0)
    except Exception:
        pass
    return found


def maximize_lab_window():
    """Ask the window manager to maximize the app window.

    `--start-maximized` on the Chromium command line is not honoured on every launch (it is
    a startup flag, and a profile that already has a saved app-window geometry wins), so the
    window is maximized directly instead of guessing a pixel size. On this desktop any fixed
    size is either clipped (200% scaling) or cramped.
    """
    for _ in range(40):                       # up to ~10 s for the window to appear
        time.sleep(0.25)
        for hwnd, _title in lab_windows():
            try:
                ctypes.windll.user32.ShowWindow(hwnd, 3)      # SW_MAXIMIZE
                log_window("maximized hwnd=%#x" % hwnd)
                return True
            except Exception as exc:
                log_window("maximize failed: %s" % exc)
                return False
    log_window("maximize: no window found in 10 s")
    return False


def window_watcher():
    """The app window is the session. When it is gone — and stays gone, and no page is
    beating either — stop the engine and exit.

    Both signals are required on purpose: during startup a window can flicker (handoff to
    an already running browser, a window being recreated), and killing a 40 GB engine
    because of a two-second gap is much worse than exiting ten seconds late. The heartbeat
    also covers the deliberate case of the user opening the console in their own browser,
    where no app window exists but someone is still looking at it.
    """
    started = time.time()
    seen_at = None
    missed = 0
    while True:
        time.sleep(2)
        if lab_windows():
            if seen_at is None:
                seen_at = time.time()
                log_ui("app window present — engine follows it")
            missed = 0
            continue
        if seen_at is None:
            if time.time() - started > 45:
                log_ui("no app window after 45 s — the heartbeat watchdog takes over")
                return
            continue
        missed += 1
        silent = time.time() - LAST_BEAT["t"]
        if missed >= 3 and silent >= 15:
            log_ui("app window closed and no heartbeat for %ds — stopping the engine"
                   % int(silent))
            TERM.stop()
            stop_engine()
            os._exit(0)


# ---------------------------------------------------------------- jobs

def new_job(kind, **fields):
    job = {"id": uuid.uuid4().hex[:8], "kind": kind, "state": "running",
           "started": time.time(), "finished": None, "detail": "", "result": None,
           "cancel": threading.Event()}
    job.update(fields)
    with LOCK:
        JOBS[job["id"]] = job
        JOB_ORDER.append(job["id"])
        while len(JOB_ORDER) > 40:
            JOBS.pop(JOB_ORDER.pop(0), None)
    return job


def finish(job, state, detail="", result=None):
    job["state"] = state
    job["finished"] = time.time()
    if detail:
        job["detail"] = detail
    if result is not None:
        job["result"] = result


def job_public(job):
    out = {k: v for k, v in job.items() if k not in ("cancel", "proc")}
    if job["kind"] == "chat" and out.get("text"):
        out["text"] = out["text"][-12000:]
    return out


def recent_jobs(n=8):
    with LOCK:
        ids = list(JOB_ORDER[-n:])
    return [job_public(JOBS[i]) for i in reversed(ids) if i in JOBS]


def append_history(entry):
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    except Exception:
        data = []
    entry["ts"] = time.time()
    data.insert(0, entry)
    try:
        HISTORY_FILE.write_text(json.dumps(data[:HISTORY_MAX], ensure_ascii=False, indent=1),
                                encoding="utf-8")
    except OSError:
        pass


def read_history(n=30):
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))[:n]
    except Exception:
        return []


# ---------------------------------------------------------------- start / stop

def start_engine(pid, force=False):
    p = PROFILES[pid]
    h = health()
    if h and h.get("model") == p.get("served_id"):
        job = new_job("start", profile=pid, title=p["title"])
        finish(job, "ready", "引擎已在运行")
        return job
    if h and not force:
        job = new_job("start", profile=pid, title=p["title"])
        finish(job, "failed", f"端口 8080 上已有别的模型（{h.get('model')}）；force 才会切换")
        return job

    cleaned = clean_stale_markers()

    def run():
        if h:
            log_ui("switching: stopping %s first" % h.get("model"))
            stop_engine()
            for _ in range(60):                 # 51 GB of experts takes a moment to unload
                if not health():
                    break
                time.sleep(1)
            log_ui("switching: previous engine %s" % ("stopped" if not health() else "STILL UP"))
        args = [str(STRATA_CMD), "launch",
                "--model-path", p["path"], "--family", p["family"], "--model", p["model"],
                "--data-dir", str(DATA), "--context", str(int(p.get("ctx", 262144))),
                "--kv", p.get("kv", "k8v4"), "--vision", p.get("vision", "cpu"),
                "--parallel", str(int(p.get("parallel", 1))),
                "--tune", "skip", "--feed", str(FEED), "--access", "local",
                "--port", "8080", "--no-tui", "--yes"] + list(p.get("extra") or [])
        log_path = LOGS / f"launch-{pid}-{int(time.time())}.log"
        log = open(log_path, "w", encoding="utf-8")
        env = dict(os.environ, HF_ENDPOINT=HF)
        # CREATE_NO_WINDOW gives the launcher a console that has no window. Do NOT add
        # DETACHED_PROCESS here: a detached console program has no console at all, and the
        # first console API it touches makes Windows allocate a *visible* one — that is the
        # black "strata.py launch ..." window that used to pop up on every start.
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, env=env,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | NOWINDOW)
        job["proc"] = proc
        deadline = time.time() + 300
        while time.time() < deadline:
            if job["cancel"].is_set():
                finish(job, "cancelled", "已放弃等待（引擎可能仍在启动）")
                return
            code = proc.poll()
            if code is not None and code != 0:
                time.sleep(1)
                detail = "\n".join(tail(log_path, 12)) or f"strata.cmd 退出码 {code}"
                finish(job, "failed", detail)
                append_history({"kind": "start", "profile": pid, "ok": False,
                                "note": detail.splitlines()[-1][:200] if detail else ""})
                return
            hh = health()
            if hh and hh.get("loaded"):
                note = f"{hh.get('model')} · {hh.get('max_context', 0)//1024}K"
                if cleaned:
                    note += f" · 清理 {cleaned} 个 stale 标记"
                finish(job, "ready", note, result=hh)
                append_history({"kind": "start", "profile": pid, "ok": True, "note": note})
                return
            time.sleep(2)
        finish(job, "failed", "300 秒内没有 ready，请看引擎日志")

    job = new_job("start", profile=pid, title=p["title"], log=engine_log_for(pid).name)
    threading.Thread(target=run, daemon=True).start()
    return job


# ---------------------------------------------------------------- chat / bench

def stream_chat(job, messages, max_tokens=1024, effort="medium"):
    h = health()
    if not h or not h.get("loaded"):
        finish(job, "failed", "引擎没有运行")
        return
    payload = {"model": h["model"], "messages": messages, "max_tokens": max_tokens,
               "stream": True}
    if effort and effort != "default":
        payload["reasoning_effort"] = effort
    req = urllib.request.Request(
        BASE + "/v1/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer strata-local"})
    t0 = time.time()
    first = None
    pieces = 0
    usage = {}
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            for raw in r:
                if job["cancel"].is_set():
                    finish(job, "cancelled", "已放弃")
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        job["text"] = (job.get("text") or "") + piece
                        pieces += 1
                        if first is None:
                            first = time.time()
                    elif delta.get("reasoning_content"):
                        job["thinking"] = (job.get("thinking") or "") + delta["reasoning_content"]
                        if first is None:
                            first = time.time()
                job["tokens"] = pieces
    except Exception as exc:
        if job["cancel"].is_set():
            finish(job, "cancelled", "已放弃")
        else:
            finish(job, "failed", f"{type(exc).__name__}: {exc}")
        return
    wall = time.time() - t0
    done = (usage or {}).get("completion_tokens") or pieces
    tps = round(done / wall, 1) if wall > 0 else 0
    ttft = round((first - t0) * 1000) if first else None
    result = {"tps": tps, "tokens": done, "seconds": round(wall, 1), "ttft_ms": ttft,
              "prompt_tokens": (usage or {}).get("prompt_tokens", 0),
              "cached": ((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens", 0)}
    job["result"] = result
    finish(job, "ready", f"{tps} tok/s（{done} tok / {result['seconds']}s）", result=result)
    append_history({"kind": job["kind"], "ok": True, "note": f"{tps} tok/s · {done} tok",
                    "tps": tps, "tokens": done, "seconds": result["seconds"],
                    "ttft_ms": ttft, "cached": result["cached"]})


def start_chat(prompt, max_tokens=1024, effort="medium", system=None):
    job = new_job("chat", title="对话", text="", thinking="")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    threading.Thread(target=stream_chat, args=(job, messages, max_tokens, effort),
                     daemon=True).start()
    return job


BENCH_PROMPT = "用三句话介绍你自己，并说明你最擅长什么。"


def start_bench(prompt=None, max_tokens=200, effort="none"):
    job = new_job("bench", title="测速", text="")
    threading.Thread(target=stream_chat,
                     args=(job, [{"role": "user", "content": (prompt or BENCH_PROMPT)}],
                           max_tokens, effort), daemon=True).start()
    return job


# ---------------------------------------------------------------- models / download

def scan_models():
    out = []
    if not MODELS_ROOT.exists():
        return out
    for d in sorted(MODELS_ROOT.iterdir()):
        if not d.is_dir():
            continue
        files = list(d.glob("*.gguf"))
        if not files:
            continue
        total = sum(f.stat().st_size for f in files)
        parts = list(d.glob("*.part*"))
        empty = [f.name for f in files if f.stat().st_size == 0]
        out.append({"dir": str(d), "name": d.name, "files": len(files),
                    "total_bytes": total, "incomplete": len(parts) > 0 or bool(empty),
                    "parts": len(parts), "empty": empty[:4]})
    return out


def download_shard(job, pid, limit_bytes=None):
    p = PROFILES[pid]
    name, size = p["shard"][0], int(p["shard"][1])
    dest = Path(p["path"]) / name
    part = dest.with_suffix(dest.suffix + ".labpart")
    url = f"{HF}/{HF_REPO}/resolve/main/{p.get('repo_dir', pid)}/{name}"
    job["url"] = url
    job["total"] = size
    have = part.stat().st_size if part.exists() else 0
    job["bytes"] = have
    headers = {"User-Agent": "StrataLab/0.4"}
    if have:
        headers["Range"] = f"bytes={have}-"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r, open(part, "ab") as fh:
            total = int(r.headers.get("Content-Length") or 0) + have
            job["total"] = total or size
            last = time.time()
            start_bytes = have
            while True:
                if job["cancel"].is_set():
                    finish(job, "cancelled", f"已下载 {job['bytes']/2**30:.1f} GB，可续传")
                    return
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                job["bytes"] += len(chunk)
                now = time.time()
                if now - last > 1:
                    job["speed"] = round((job["bytes"] - start_bytes) / (now - job["started"]) / 2**20, 1)
                    last = now
                if limit_bytes and job["bytes"] >= limit_bytes:
                    finish(job, "ready", f"测试下载 {job['bytes']/2**20:.1f} MB 成功（未改名）",
                           result={"test": True, "bytes": job["bytes"]})
                    return
    except Exception as exc:
        finish(job, "failed", f"{type(exc).__name__}: {exc}")
        append_history({"kind": "download", "profile": pid, "ok": False, "note": str(exc)[:200]})
        return
    got = job["bytes"]
    if got != size:
        finish(job, "failed", f"大小不符：{got} / {size} 字节")
        return
    part.replace(dest)
    finish(job, "ready", f"{name} 下载完成（{size/2**30:.1f} GB）", result={"bytes": size})
    append_history({"kind": "download", "profile": pid, "ok": True,
                    "note": f"{name} {size/2**30:.1f} GB"})


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "StrataLab/0.4"

    def log_message(self, *args):
        pass

    def _send(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def reply(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", status)

    def origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")

    def body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    # ---------------------------------------------------------- GET
    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = {}
        for part in query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = urllib.request.unquote(v)

        if path in ("/", "/index.html"):
            return self._send((APP_DIR / "ui.html").read_bytes(), "text/html; charset=utf-8")

        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if name not in STATIC_FILES:
                return self.reply({"error": "not found"}, 404)
            f = APP_DIR / "static" / name
            if not f.exists():
                return self.reply({"error": "not found"}, 404)
            ctype = ("text/javascript; charset=utf-8" if name.endswith(".js")
                     else "text/css; charset=utf-8")
            return self._send(f.read_bytes(), ctype)

        if path == "/api/term/state":
            return self.reply(TERM.state())

        if path == "/api/term/output":
            try:
                since = int(params.get("since", 0))
            except ValueError:
                since = 0
            data, nxt = TERM.read_since(since)
            st = TERM.state()
            return self.reply({"data": base64.b64encode(data).decode("ascii"),
                               "next": nxt, "alive": st["alive"], "pid": st["pid"]})

        if path == "/api/status":
            h = health()
            profiles = []
            for pid, p in PROFILES.items():
                ok, have, need = ready(pid)
                profiles.append({"id": pid, "title": p["title"], "model_label": p["model_label"],
                                 "desc": p["desc"], "speed": p["speed"],
                                 "ctx": int(p.get("ctx", 262144)), "kv": p.get("kv"),
                                 "vision": p.get("vision"), "parallel": p.get("parallel"),
                                 "ready": ok, "have_bytes": have, "need_bytes": need,
                                 "repo_dir": p.get("repo_dir"),
                                 "active": bool(h and h.get("model") == p.get("served_id"))})
            return self.reply({"health": h, "vram": vram(), "ram_free_gb": ram_free_gb(),
                               "ram_total_gb": mem_stats()[1],
                               "page": {k: LAST_BEAT.get(k) for k in ("w", "h", "dpr", "sw", "sh")},
                               "profiles": profiles, "jobs": recent_jobs(6),
                               "engine": LAST_ENGINE,
                               "comfy_port": comfy_running(),
                               "stale_markers": (not engine_running())
                               and bool(list(RUNTIME.glob("*.child.json")))})

        if path == "/api/live":
            return self.reply(engine_live())

        if path == "/api/metrics":
            return self.reply({"series": list(METRICS)[-150:], "engine": engine_stats(),
                               "now": time.time()})

        if path == "/api/stats":
            return self.reply(stats_payload())

        if path == "/api/gateway":
            return self.reply(GATEWAY.monitor(params.get("key_id") or None))

        if path == "/api/meta":
            return self.reply(engine_meta())

        if path == "/api/scan":
            return self.reply({"models": scan_models()})

        if path == "/api/profiles":
            return self.reply({"profiles": PROFILES})

        if path == "/api/conn":
            h = health()
            m = engine_meta()
            return self.reply({"base": BASE, "v1": BASE + "/v1",
                               "anthropic": BASE,
                               "health": BASE + "/health",
                               "model": (h or {}).get("model", ""),
                               "ctx": (h or {}).get("max_context"),
                               "key": "（本机未设 key）", "lan": False,
                               "webui": f"http://127.0.0.1:{PORT}",
                               "build": m.get("build", ""),
                               "apps": client_apps()})

        if path == "/api/logfiles":
            files = sorted(ENGINE_LOGS.glob("*.engine.log"),
                           key=lambda x: x.stat().st_mtime, reverse=True)
            return self.reply({"files": [{"name": f.name, "mtime": f.stat().st_mtime,
                                          "kb": round(f.stat().st_size / 1024, 1)} for f in files]})

        if path == "/api/logs":
            name = params.get("file")
            grep = params.get("grep") or None
            if name and (Path(name).name != name or not name.endswith(".engine.log")):
                return self.reply({"error": "invalid log file"}, 400)
            f = (ENGINE_LOGS / name) if name else current_log()
            if not f or not f.exists():
                return self.reply({"file": "", "lines": ["(没有引擎日志)"]})
            return self.reply({"file": f.name, "lines": tail(f, 120, grep)})

        if path == "/api/history":
            n = int(params.get("limit", 30))
            items = read_history(n)
            if params.get("csv"):
                rows = ["ts,kind,ok,profile,tps,tokens,seconds,ttft_ms,cached,note"]
                for it in items:
                    rows.append(",".join(str(it.get(k, "")) for k in
                                         ("ts", "kind", "ok", "profile", "tps", "tokens",
                                          "seconds", "ttft_ms", "cached", "note")))
                body = "\n".join(rows).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=strata-history.csv")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            return self.reply({"items": items})

        if path == "/api/progress":
            return self.reply({"jobs": recent_jobs(10)})

        if path == "/api/beat":
            LAST_BEAT["t"] = time.time()
            # The page also reports its viewport: with pythonw there is no console and no
            # devtools, so this is the only way to know how big the window really is.
            for key in ("w", "h", "dpr", "sw", "sh"):
                if params.get(key):
                    try:
                        LAST_BEAT[key] = float(params[key])
                    except ValueError:
                        pass
            return self.reply({"ok": True, "exit_after_s": NO_WINDOW_EXIT_S})

        return self.reply({"error": "not found"}, 404)

    # ---------------------------------------------------------- POST
    def do_POST(self):
        if not self.origin_ok():
            return self.reply({"error": "cross-origin POST refused"}, 403)
        path = self.path.partition("?")[0]
        data = self.body()

        if path == "/api/gateway":
            try:
                return self.reply({"ok": True, "gateway": GATEWAY.configure(data)})
            except (ValueError, OSError) as exc:
                return self.reply({"ok": False, "error": str(exc)}, 400)

        if path == "/api/gateway/key":
            try:
                key, public = GATEWAY.generate_key(data.get("name"), data.get("daily_tokens"))
                return self.reply({"ok": True, "key": key, "gateway": public})
            except (ValueError, OSError) as exc:
                return self.reply({"ok": False, "error": str(exc)}, 400)

        if path == "/api/gateway/key/update":
            try:
                keys = GATEWAY.update_key(data.get("id") or "", data)
                return self.reply({"ok": True, "keys": keys})
            except (ValueError, OSError) as exc:
                return self.reply({"ok": False, "error": str(exc)}, 400)

        if path == "/api/gateway/key/delete":
            try:
                keys = GATEWAY.delete_key(data.get("id") or "")
                return self.reply({"ok": True, "keys": keys})
            except (ValueError, OSError) as exc:
                return self.reply({"ok": False, "error": str(exc)}, 400)

        if path == "/api/gateway/clear":
            try:
                GATEWAY.clear_audit()
                return self.reply({"ok": True})
            except OSError as exc:
                return self.reply({"ok": False, "error": str(exc)}, 500)

        if path == "/api/start":
            pid = data.get("id")
            if pid not in PROFILES:
                return self.reply({"ok": False, "error": "unknown profile"}, 400)
            ok, have, need = ready(pid)
            if not ok:
                return self.reply({"ok": False, "error": "模型文件未就绪",
                                   "have_bytes": have, "need_bytes": need}, 400)
            return self.reply({"ok": True,
                               "job": job_public(start_engine(pid, bool(data.get("force"))))})

        if path == "/api/stop":
            log_ui("stop requested via /api/stop")
            stop_engine()
            return self.reply({"ok": True})

        if path == "/api/quit":
            # Nothing else closes this process: with pythonw there is no window to close
            # and no console to Ctrl+C, so the console needs its own shutdown.
            # The engine is the console's child in spirit, so closing the lab stops it —
            # pass stop_engine: false to leave the server serving other clients.
            also_engine = data.get("stop_engine", True)
            log_ui("quit requested via /api/quit (stop_engine=%s)" % bool(also_engine))
            self.reply({"ok": True, "stopping_engine": bool(also_engine)})

            def bye():
                TERM.stop()
                if also_engine:
                    stop_engine()
                try:
                    if HTTPD is not None:
                        HTTPD.shutdown()
                except Exception:
                    pass
                time.sleep(0.2)
                os._exit(0)

            threading.Thread(target=bye, daemon=True).start()
            return

        if path == "/api/bench":
            return self.reply({"ok": True, "job": job_public(
                start_bench(data.get("prompt"), int(data.get("max_tokens", 200)),
                            data.get("effort", "none")))})

        if path == "/api/chat":
            prompt = (data.get("prompt") or "").strip()
            if not prompt:
                return self.reply({"ok": False, "error": "空提示"}, 400)
            return self.reply({"ok": True, "job": job_public(
                start_chat(prompt, int(data.get("max_tokens", 1024)),
                           data.get("effort", "medium"), data.get("system")))})

        if path == "/api/cancel":
            jid = data.get("id")
            with LOCK:
                job = JOBS.get(jid)
            if not job:
                return self.reply({"ok": False, "error": "unknown job"}, 404)
            job["cancel"].set()
            return self.reply({"ok": True})

        if path == "/api/history/clear":
            try:
                HISTORY_FILE.write_text("[]", encoding="utf-8")
            except OSError as exc:
                return self.reply({"ok": False, "error": str(exc)}, 500)
            return self.reply({"ok": True})

        if path == "/api/term/start":
            ok = TERM.start(data.get("kind") or "cmd",
                            int(data.get("cols") or 120), int(data.get("rows") or 32))
            return self.reply({"ok": ok, "state": TERM.state()}, 200 if ok else 500)

        if path == "/api/term/input":
            ok = TERM.write(data.get("text") or "")
            return self.reply({"ok": ok})

        if path == "/api/term/resize":
            ok = TERM.resize(int(data.get("cols") or 120), int(data.get("rows") or 32))
            return self.reply({"ok": ok})

        if path == "/api/term/stop":
            TERM.stop()
            return self.reply({"ok": True, "state": TERM.state()})

        if path == "/api/download":
            pid = data.get("id")
            if pid not in PROFILES:
                return self.reply({"ok": False, "error": "unknown profile"}, 400)
            job = new_job("download", profile=pid, title=f"下载 {pid}",
                          bytes=0, total=int(PROFILES[pid]["shard"][1]), speed=0)
            threading.Thread(target=download_shard,
                             args=(job, pid, data.get("limit_bytes")), daemon=True).start()
            return self.reply({"ok": True, "job": job_public(job)})

        if path == "/api/profiles":
            incoming = data.get("profiles")
            if not isinstance(incoming, dict):
                return self.reply({"ok": False, "error": "profiles 必须是对象"}, 400)
            merged = load_profiles()
            for pid, p in incoming.items():
                if pid not in merged:
                    return self.reply({"ok": False, "error": f"未知档位 {pid}"}, 400)
                if not isinstance(p, dict):
                    return self.reply({"ok": False, "error": f"{pid} 必须是对象"}, 400)
                for key, value in p.items():
                    if key not in ("ctx", "kv", "vision", "parallel"):
                        return self.reply({"ok": False, "error": f"不允许修改字段 {key}"}, 400)
                    if key in ("ctx", "parallel"):
                        if isinstance(value, bool) or not isinstance(value, int):
                            return self.reply({"ok": False, "error": f"{key} 必须是整数"}, 400)
                        if key == "ctx" and not (1024 <= value <= 262144 and value % 1024 == 0):
                            return self.reply({"ok": False, "error": "ctx 必须是 1024–262144 之间的 1024 倍数"}, 400)
                        if key == "parallel" and not 1 <= value <= 4:
                            return self.reply({"ok": False, "error": "parallel 必须在 1–4 之间"}, 400)
                    elif key == "kv" and value not in ("k8v4", "int8", "q4_0"):
                        return self.reply({"ok": False, "error": "不支持的 kv 格式"}, 400)
                    elif key == "vision" and value not in ("cpu", "off", "gpu"):
                        return self.reply({"ok": False, "error": "不支持的 vision 模式"}, 400)
                merged[pid].update(p)
            save_profiles(merged)
            return self.reply({"ok": True, "profiles": merged})

        return self.reply({"error": "not found"}, 404)


def port_in_use():
    """Windows lets a second socket bind the same port when SO_REUSEADDR is set, so a
    duplicate copy would silently share 8765 instead of failing. Probe first."""
    import socket
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

WINDOW_SIZE = (1600, 900)   # only a fallback if --start-maximized is ignored


def log_window(msg):
    """Every window decision goes to logs/window.log — with pythonw there is no console,
    so without this there is nothing to inspect when a window does not show up."""
    try:
        with open(LOGS / "window.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg + "\n")
    except OSError:
        pass


def open_window(url):
    """Open the console as its own window, the way ImageLab does.

    `--app=` gives a chromeless window with no tab strip, and a private `--user-data-dir`
    keeps it out of the browser the user browses with: its own session, its own taskbar
    entry, its own size, and closing it does not disturb any other tab. Plain
    `start http://...` would instead drop a tab into the everyday browser window.

    Two things make that window look like a browser instead of an app:
      * no favicon — Edge falls back to a globe in the title bar and the taskbar, so the
        page carries an inline SVG icon (see <link rel="icon"> in ui.html);
      * window geometry — it opens maximized (the user's choice: this desktop is only
        ~1050 DIP wide at 200% scaling, so any fixed size is either clipped or cramped).
        The page's layout switches to a two-row header at narrow widths so every tab stays
        reachable.
    """
    profile = APP_DIR / "_window_profile"
    for exe in BROWSERS:
        if not os.path.exists(exe):
            continue
        try:
            subprocess.Popen(
                [exe, "--app=" + url, "--start-maximized",
                 # No --force-device-scale-factor here: this desktop runs at 200%, so
                 # letting Chromium use the native scale is what makes the UI readable.
                 # Forcing scale 1 halves every glyph — that was a bad idea.
                 "--user-data-dir=" + str(profile),
                 "--no-first-run", "--no-default-browser-check",
                 "--disable-background-timer-throttling",
                 "--disable-backgrounding-occluded-windows",
                 "--disable-renderer-backgrounding",
                 "--disable-features=Translate,SleepingTabs"],
                creationflags=NOWINDOW)
            log_window("opened maximized with %s · %s" % (os.path.basename(exe), url))
            return True
        except OSError as exc:
            log_window("spawn failed with %s: %s" % (exe, exc))
            continue
    log_window("no Chromium found, falling back to the default browser (tab, not app window)")
    import webbrowser
    webbrowser.open(url)
    return False


def main():
    global HTTPD
    open_it = "--no-open" not in sys.argv
    url = f"http://127.0.0.1:{PORT}"
    # A second copy is the classic failure mode here: it cannot serve, and if something
    # keeps relaunching it the user sees windows appearing and disappearing. Say so once
    # and leave the first copy serving — but still raise the window, since a double-click
    # should always end with the console in front.
    if port_in_use():
        try:
            print(f"Strata Lab is already running on {url} — this copy only opens the window.")
        except Exception:
            pass
        log_ui("second copy: port %d busy, only raising the window" % PORT)
        if open_it:
            open_window(url)
        return
    HTTPD = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if GATEWAY.config["enabled"]:
        try:
            GATEWAY.start()
            log_ui("gateway listening on 127.0.0.1:%d" % GATEWAY.config["port"])
        except (OSError, ValueError) as exc:
            log_ui("gateway failed to start: %s" % exc)
    threading.Thread(target=vram_loop, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    if open_it or os.environ.get("STRATA_LAB_WATCHDOG") == "1":
        # Only a windowed lab follows the window's lifetime; a server started with
        # --no-open is deliberately left alone (API clients, scripts). The env switch
        # forces it on so the timeout can be tested without a real window.
        LAST_BEAT["t"] = time.time()
        threading.Thread(target=watchdog, daemon=True).start()
    log_ui("started on %s (window=%s, heartbeat backstop=%ss)"
           % (url, "yes" if open_it else "no", NO_WINDOW_EXIT_S))
    try:
        print(f"Strata Lab v0.4: {url}", flush=True)
    except Exception:
        pass

    def raise_window():
        # The window is the thing whose closing must end the session, so start watching
        # for it only once there is one.
        if open_window(url):
            threading.Thread(target=window_watcher, daemon=True).start()
            maximize_lab_window()

    if open_it:
        threading.Timer(0.8, raise_window).start()
    HTTPD.serve_forever()


if __name__ == "__main__":
    main()
